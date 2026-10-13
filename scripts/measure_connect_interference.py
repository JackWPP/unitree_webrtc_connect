#!/usr/bin/env python3
"""Measure whether connecting one robot stalls the others.

The last structural unknown in the dual-dog design, and one that cannot be
settled by reading code: `WebRTCBackend` offloads the driver constructor to a
worker thread and then awaits `conn.connect()` on the shared event loop, and
inside the driver that path does blocking I/O. If it blocks, every already
connected robot misses its control frames for the duration.

This connects every robot but one, then measures the event loop while
connecting the last. Run it once with two or three robots on the desk:

    python scripts/measure_connect_interference.py --ips 192.168.31.245,192.168.31.246

`--backend mock` exercises the harness with no hardware at all, which is how
you check the instrument before trusting its reading.

Read the `during.max_ms` figure against the baseline. At 20 Hz a joystick
frame is due every 50 ms, so a stall at or above that is the hazard.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from unitree_robot_agent.errors import AgentError  # noqa: E402
from unitree_robot_agent.fleet import (  # noqa: E402
    FleetManager,
    Roster,
    RobotSpec,
    measure_connect_impact,
)
from unitree_robot_agent.fleet.interference import STALL_ALERT_MS  # noqa: E402


def roster_from_args(args) -> Roster:
    if args.roster:
        from unitree_robot_agent.fleet import load_roster

        return load_roster(args.roster)

    if not args.ips:
        raise SystemExit("give --ips a,b[,c] or --roster <file>")

    specs = []
    for index, ip in enumerate(part.strip() for part in args.ips.split(",") if part.strip()):
        specs.append(
            RobotSpec.from_mapping(
                {
                    "id": args.prefix + str(index + 1),
                    "model": args.robot,
                    "connection": "sta-l",
                    "ip": ip,
                    "aes_128_key": args.key,
                },
                index=index,
            )
        )
    return Roster(robots=tuple(specs), default_id=specs[0].robot_id)


async def run(args) -> int:
    roster = roster_from_args(args)
    if len(roster) < 2:
        raise SystemExit("need at least two robots: the point is what connecting one does to another")

    fleet = FleetManager(roster, backend=args.backend or None)
    ids = list(fleet.ids)
    trigger = ids[-1]
    others = ids[:-1]

    print(f"fleet      : {', '.join(ids)}")
    print(f"trigger    : {trigger} (the one that will connect last)")
    print(f"observing  : {', '.join(others)} (already connected)\n")

    print("connecting the others first ...")
    for robot_id in others:
        try:
            await fleet.get(robot_id).session.ensure_connected()
            print(f"  {robot_id}: connected")
        except AgentError as exc:
            print(f"  {robot_id}: FAILED — {exc}")
            print("  (nothing to measure: the interference question needs a live loop)")
            await fleet.aclose_all()
            return 2

    print(f"\nmeasuring the loop while connecting {trigger} ...\n")
    try:
        report = await measure_connect_impact(
            lambda: fleet.get(trigger).session.ensure_connected(),
            interval=args.interval,
            baseline_s=args.baseline,
            settle_s=args.settle,
            alert_ms=args.alert_ms,
        )
    finally:
        await fleet.aclose_all()

    payload = report.as_dict()
    print(json.dumps(payload, indent=2))
    print()
    print(f"baseline worst wakeup lateness : {payload['baseline']['max_ms']:.1f} ms")
    print(f"during the connect             : {payload['during']['max_ms']:.1f} ms")
    print(f"connect took                   : {payload['connect_seconds']:.2f} s")
    print()
    print(report.verdict)

    if report.stalled:
        print()
        print(
            "This is the hazard the connect gate was built for. If the loop\n"
            "really does block for this long on hardware, the fix is to move\n"
            "the handshake off the shared loop (a thread with its own loop per\n"
            "robot) rather than to hope the window is small."
        )
    return 1 if report.stalled else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ips", help="comma-separated robot IPs")
    parser.add_argument("--roster", help="roster JSON file (instead of --ips)")
    parser.add_argument("--robot", default="Go2", help="model family for --ips (default: Go2)")
    parser.add_argument("--prefix", default="dog", help="id prefix for --ips (default: dog)")
    parser.add_argument("--key", default="", help="AES-128 key to use for every robot")
    parser.add_argument("--backend", default="", help="auto | webrtc | mock")
    parser.add_argument("--interval", type=float, default=0.01, help="sampler period (s)")
    parser.add_argument("--baseline", type=float, default=1.0, help="baseline window (s)")
    parser.add_argument("--settle", type=float, default=0.3, help="extra sampling after the connect (s)")
    parser.add_argument("--alert-ms", type=float, default=STALL_ALERT_MS,
                        help=f"report a stall at or above this (default: {STALL_ALERT_MS:.0f} ms)")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
