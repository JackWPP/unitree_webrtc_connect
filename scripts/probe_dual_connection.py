#!/usr/bin/env python3
"""Probe: can N Unitree WebRTC connections coexist in one process?

Why this exists
---------------
`unitree_webrtc_connect/__init__.py` monkey-patches `aioice.Connection` to pin
`local_username` / `local_password` at **class** level, so every peer
connection in the process shares one ICE ufrag/password pair. That is a
deliberate workaround for a Go2 firmware quirk, but upstream never documents
or tests what happens with more than one connection — and a dual-dog console
is by definition two concurrent connections in one process.

This probe answers three questions, in order of how much they would hurt:

1. Do N connections actually get N independent transports? (they must)
2. Are the ICE credentials shared? (expected yes — the patch forces it)
3. Does closing one connection disturb the others? (it must not)

Offline mode needs no robot: it drives the real driver code path
(`UnitreeWebRTCConnection.init_webrtc`) and gathers ICE against an
unreachable address, which is enough to inspect local SDP.

    python scripts/probe_dual_connection.py                 # 3 offline PCs
    python scripts/probe_dual_connection.py --count 2
    python scripts/probe_dual_connection.py --ips 192.168.1.11,192.168.1.12

With `--ips` it stops being offline: it performs the real handshake against
each robot **concurrently** and reports per-robot wall time. That is the test
that catches the blocking-`connect()` hazard, where connecting robot #2
freezes robot #1's control loop.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import unitree_webrtc_connect as uwc  # noqa: E402
from unitree_webrtc_connect import (  # noqa: E402
    UnitreeWebRTCConnection,
    WebRTCConnectionMethod,
    WebRTCDataChannel,
)
from unitree_webrtc_connect.webrtc_audio import WebRTCAudioChannel  # noqa: E402
from unitree_webrtc_connect.webrtc_video import WebRTCVideoChannel  # noqa: E402
from aiortc import RTCPeerConnection  # noqa: E402

# The driver's own defaults, so an offline probe uses realistic addresses.
DEFAULT_IPS = ["192.168.31.245", "192.168.31.246"]


def _build_transport(conn: UnitreeWebRTCConnection) -> None:
    """Build just the local transport, skipping the signaling round trip.

    This mirrors `UnitreeWebRTCConnection.init_webrtc` lines 137-145 and
    nothing else. `init_webrtc` as a whole is the *full* connect — it also
    posts the SDP offer to the robot, so calling it offline raises
    `LocalSignalingPortError`. Everything we want to inspect (ICE agents,
    data channel, media channels) is created before that call.
    """
    conn.pc = RTCPeerConnection(conn.create_webrtc_configuration(None))
    conn.datachannel = WebRTCDataChannel(conn, conn.pc)
    conn.audio = WebRTCAudioChannel(conn.pc, conn.datachannel)
    conn.video = WebRTCVideoChannel(conn.pc, conn.datachannel)


def _sdp_facts(pc) -> dict:
    """Pull the ICE facts we care about out of the local SDP.

    Returns structured candidates rather than formatted strings: IPv6
    addresses contain colons, so `address:port/type` is not parseable.
    """
    sdp = pc.localDescription.sdp if pc.localDescription else ""
    ufrag = pwd = ""
    candidates: list[dict] = []
    for line in sdp.splitlines():
        if line.startswith("a=ice-ufrag:"):
            ufrag = line.split(":", 1)[1].strip()
        elif line.startswith("a=ice-pwd:"):
            pwd = line.split(":", 1)[1].strip()
        elif line.startswith("a=candidate:"):
            parts = line.split()
            # a=candidate:<foundation> <component> <transport> <priority>
            #              <address> <port> typ <type> ...
            if len(parts) >= 8:
                candidates.append(
                    {
                        "component": parts[1],
                        "transport": parts[2].lower(),
                        "address": parts[4],
                        "port": parts[5],
                        "type": parts[7],
                    }
                )
    return {"ufrag": ufrag, "pwd": pwd, "candidates": candidates}


async def _gather(pc, timeout: float) -> None:
    if pc.iceGatheringState == "complete":
        return
    done = asyncio.Event()

    @pc.on("icegatheringstatechange")
    def _changed() -> None:
        if pc.iceGatheringState == "complete":
            done.set()

    try:
        await asyncio.wait_for(done.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pass


async def offline(count: int, ips: list[str], timeout: float) -> int:
    print(f"offline probe: {count} peer connections, no robot needed\n")

    conns: list[UnitreeWebRTCConnection] = []
    for i in range(count):
        ip = ips[i % len(ips)]
        conn = UnitreeWebRTCConnection(
            WebRTCConnectionMethod.LocalSTA, ip=ip, aes_128_key=None
        )
        # Exactly the transport construction a real connect() performs,
        # without the signaling round trip (which needs a reachable robot).
        _build_transport(conn)
        conns.append(conn)

    distinct_pcs = len({id(c.pc) for c in conns})
    print(f"  distinct connections : {len({id(c) for c in conns})}/{count}")
    print(f"  distinct PeerConns   : {distinct_pcs}/{count}")

    for conn in conns:
        offer = await conn.pc.createOffer()
        await conn.pc.setLocalDescription(offer)
    await asyncio.gather(*(_gather(c.pc, timeout) for c in conns))

    facts = [_sdp_facts(c.pc) for c in conns]
    ufrags = {f["ufrag"] for f in facts}
    pwds = {f["pwd"] for f in facts}

    all_host_ports: list[str] = []
    for i, f in enumerate(facts):
        hosts = [c for c in f["candidates"] if c["type"] == "host"]
        v4 = sorted({c["port"] for c in hosts if ":" not in c["address"]})
        v6 = sorted({c["port"] for c in hosts if ":" in c["address"]})
        all_host_ports.extend(c["port"] for c in hosts)
        print(
            f"\n  PC{i}: ufrag={f['ufrag']!r} pwd_len={len(f['pwd'])} "
            f"candidates={len(f['candidates'])} "
            f"(host: {len(v4)} v4 port(s) {','.join(v4)}, {len(v6)} v6)"
        )
        print(f"        local v4 addresses: "
              f"{', '.join(sorted({c['address'] for c in hosts if ':' not in c['address']}))}")

    shared_creds = len(ufrags) == 1 and len(pwds) == 1
    independent_transport = distinct_pcs == count
    collisions = {p for p in all_host_ports if all_host_ports.count(p) > 1}

    print("\n── findings " + "─" * 50)
    print(
        f"  1. independent transports : "
        f"{'OK' if independent_transport else 'FAIL'} "
        f"({distinct_pcs}/{count} peer connections)"
    )
    print(
        f"  2. ICE creds shared       : "
        f"{'yes — expected, the library pins ufrag/pwd at class level' if shared_creds else 'no'}"
    )
    print(f"     distinct ufrag count   : {len(ufrags)}")
    print(
        f"  3. host candidate ports   : "
        f"{'all distinct' if not collisions else 'COLLIDING ' + str(sorted(collisions))} "
        f"({len(all_host_ports)} candidates total)"
    )

    # Closing one must not disturb the others.
    if count >= 2:
        await conns[0].pc.close()
        await asyncio.sleep(0.2)
        survivors = sum(
            1 for c in conns[1:] if c.pc.connectionState != "closed"
        )
        print(
            f"  4. close #0 leaves rest  : "
            f"{'OK' if survivors == count - 1 else 'FAIL'} "
            f"({survivors}/{count - 1} still alive)"
        )

    for conn in conns:
        if conn.pc and conn.pc.connectionState != "closed":
            await conn.pc.close()

    print(
        "\n  NOTE: shared ufrag is safe only while each connection talks to a\n"
        "  different remote. Two peers to the SAME robot would collide — that\n"
        "  is why the robot's single-client slot must be enforced, not raced."
    )
    return 0 if independent_transport and not collisions else 1


async def online(ips: list[str], timeout: float) -> int:
    print(f"online probe: connecting to {len(ips)} robot(s) concurrently")
    print("this performs the real handshake — the robots will report busy if")
    print("another WebRTC client (the official app) already holds the slot\n")

    keys = [None] * len(ips)

    async def one(index: int, ip: str) -> dict:
        started = time.monotonic()
        conn = UnitreeWebRTCConnection(
            WebRTCConnectionMethod.LocalSTA, ip=ip, aes_128_key=keys[index]
        )
        try:
            await conn.connect()
            return {
                "ip": ip,
                "ok": True,
                "seconds": time.monotonic() - started,
                "conn": conn,
            }
        except Exception as exc:  # noqa: BLE001 - a probe reports, it does not raise
            return {
                "ip": ip,
                "ok": False,
                "seconds": time.monotonic() - started,
                "error": f"{type(exc).__name__}: {exc}",
                "conn": conn,
            }

    started_all = time.monotonic()
    results = await asyncio.gather(*(one(i, ip) for i, ip in enumerate(ips)))
    wall = time.monotonic() - started_all

    print("\n── results " + "─" * 51)
    for r in results:
        status = "OK  " if r["ok"] else "FAIL"
        print(f"  [{status}] {r['ip']:<16} {r['seconds']:6.2f}s  {r.get('error', '')}")

    serial = sum(r["seconds"] for r in results)
    print(f"\n  wall clock (concurrent) : {wall:.2f}s")
    print(f"  sum of individual times : {serial:.2f}s")
    if wall > 0 and serial > wall * 1.5:
        print(
            "  → connections overlapped. If individual times are also large,\n"
            "    check whether one connect stalled the others' control loops;\n"
            "    a stopwatch on live telemetry is the real test."
        )
    else:
        print("  → connections look serialised; investigate blocking I/O in connect().")

    for r in results:
        conn = r["conn"]
        try:
            if conn.isConnected:
                await conn.disconnect()
        except Exception:  # noqa: BLE001
            pass
    return 0 if all(r["ok"] for r in results) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--count", type=int, default=3,
                        help="number of peer connections in offline mode (default: 3)")
    parser.add_argument("--ips", default="",
                        help="comma-separated robot IPs; enables online mode")
    parser.add_argument("--timeout", type=float, default=8.0,
                        help="ICE gathering timeout in seconds (default: 8)")
    args = parser.parse_args()

    if args.ips:
        ips = [ip.strip() for ip in args.ips.split(",") if ip.strip()]
        return asyncio.run(online(ips, args.timeout))
    return asyncio.run(offline(args.count, DEFAULT_IPS, args.timeout))


if __name__ == "__main__":
    raise SystemExit(main())
