"""Find the robots, so nobody has to read a label off a dog's belly.

`wpp` had this and it was one of the few things in it worth keeping: a scan,
autofilled fields, and a bulk add. The friction it removed is real and it lands
on day one — this project's own runbook tells the operator to get the second
dog's IP and serial number "from the router's admin page or the label on the
dog", which is three minutes of crouching behind a robot with a phone torch.

Three sources, because no single one is enough on the networks this was
measured on:

* **Multicast** is the library's own way and the only one that yields a
  serial-to-address mapping directly. On the network this was built against it
  **never answered** — measured, repeatedly, on a Go2 that was sitting right
  there answering every other request. It is still tried first because when it
  works it is exact, and a miss costs two seconds.
* **A port sweep of the local /24** is what actually found the dog, on port
  9991. It cannot give a serial number — `con_notify` replies with `data1` and
  `data2` and nothing that identifies the robot — but it finds addresses, which
  is the half that cannot be guessed.
* **The account's device list** gives serial numbers and aliases without
  touching the robot, from the same cloud session the key fetch already uses.

## What ties a serial number to an address

Multicast does, when it answers. When it does not, the remaining link is the
**per-device key**: `data2 === 3` firmware encrypts its `data1` with the key for
its own serial number, so trying each account device's key against each address
tells you which is which. The key that decrypts is the robot. That is a real
measurement rather than a guess, and it is the reason this module can produce a
complete roster with nothing typed in.

Nothing here connects to a robot in the WebRTC sense, so **no client slot is
claimed** — the sweep is a TCP connect, and the handshake stops before an SDP
is sent. A robot that is in use by the official app can still be found.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import ipaddress
import logging
import os
import socket
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: The signaling port newer firmware listens on. The legacy `/offer` port is a
#: separate, older path; a robot on 8081 answers a different handshake entirely
#: and cannot be matched by key, so it is reported but not matched.
CON_NOTIFY_PORT = 9991
LEGACY_OFFER_PORT = 8081

#: How many addresses to probe at once. 254 sequential connects with a 0.4 s
#: timeout is a hundred seconds; this makes it about two. Bounded rather than
#: unbounded because a /24 belongs to somebody, and a few hundred simultaneous
#: SYNs is a port scan whatever the intent.
SWEEP_CONCURRENCY = 64
SWEEP_TIMEOUT_S = 0.4


@dataclass
class Found:
    """One robot, or one address that looks like a robot."""

    ip: str
    sn: str = ""
    alias: str = ""
    model: str = "Go2"
    #: "multicast" | "sweep" | "key" — how the serial number was established,
    #: or "" when all that is known is that a port answered.
    matched_by: str = ""
    port: int = 0
    #: Whether the robot's firmware wants a per-device key.
    data2: Optional[int] = None
    online: Optional[bool] = None

    @property
    def identified(self) -> bool:
        return bool(self.sn)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ip": self.ip,
            "sn": self.sn,
            "alias": self.alias,
            "model": self.model,
            "matched_by": self.matched_by,
            "port": self.port,
            "data2": self.data2,
            "online": self.online,
        }

    def to_spec(self, robot_id: str) -> Dict[str, Any]:
        """A roster entry, ready to paste.

        The IP is omitted when it is not known rather than written as an empty
        string: `target` may be a serial number, and a roster with
        `"ip": ""` in it looks like a mistake somebody will try to fix.
        """
        entry: Dict[str, Any] = {"id": robot_id, "model": self.model}
        if self.ip:
            entry["ip"] = self.ip
        if self.sn:
            entry["sn"] = self.sn
        return entry


# ─── the local network ────────────────────────────────────────────────


def local_ipv4() -> str:
    """This machine's address on the network it would use to reach the outside.

    No packets are sent: a UDP `connect` only sets the socket's peer, which is
    enough for the kernel to pick a source address and therefore a subnet.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 53))
        return sock.getsockname()[0]
    except OSError:
        return ""
    finally:
        sock.close()


def local_subnet() -> str:
    """The /24 this machine is on, in CIDR, or "" if it cannot be worked out.

    A /24 rather than the interface's own mask because the mask on a home or
    lab network is often a /16 or wider, and sweeping 65 000 addresses is not a
    scan, it is an incident.
    """
    address = local_ipv4()
    if not address:
        return ""
    octets = address.split(".")
    if len(octets) != 4:
        return ""
    return ".".join(octets[:3]) + ".0/24"


async def _port_open(ip: str, port: int, timeout: float) -> bool:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), timeout=timeout
        )
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except (OSError, asyncio.TimeoutError):
        pass
    del reader
    return True


async def sweep(subnet: str = "", *, timeout: float = SWEEP_TIMEOUT_S) -> List[Tuple[str, int]]:
    """Every address on `subnet` with a signaling port open.

    Returns `(ip, port)` pairs, port 9991 before 8081 for the same address so
    the caller can prefer the modern handshake without another round trip.
    """
    subnet = subnet or local_subnet()
    if not subnet:
        return []
    try:
        network = ipaddress.ip_network(subnet, strict=False)
    except ValueError:
        return []
    hosts = [str(host) for host in network.hosts()]
    if not hosts or len(hosts) > 1024:
        return []

    gate = asyncio.Semaphore(SWEEP_CONCURRENCY)

    async def probe(ip: str) -> List[Tuple[str, int]]:
        async with gate:
            for port in (CON_NOTIFY_PORT, LEGACY_OFFER_PORT):
                if await _port_open(ip, port, timeout):
                    return [(ip, port)]
        return []

    results = await asyncio.gather(*(probe(ip) for ip in hosts))
    return [pair for group in results for pair in group]


# ─── the account ──────────────────────────────────────────────────────


def account_devices(
    *, email: str = "", password: str = "", region: str = "cn", device_type: str = "Go2"
) -> List[Dict[str, Any]]:
    """Serial numbers bound to the account, with keys, without touching a robot.

    Uses the same cloud session the key fetch uses, so a machine that has ever
    fetched a key is already logged in. Credentials, when given, are used but
    never returned.
    """
    try:
        from unitree_webrtc_connect.unitree_cloud import UnitreeCloud
    except ImportError:  # pragma: no cover - the driver is a hard dependency
        return []

    # Credentials are required: the cloud session is per-process and there is
    # no token file to fall back on. A caller with nothing to log in with gets
    # an empty list and a note, because never having logged in is not an error.
    email = email or os.environ.get("UNITREE_EMAIL", "")
    password = password or os.environ.get("UNITREE_PASSWORD", "")
    if not (email and password):
        logger.info("no credentials for the account device list")
        return []

    try:
        client = UnitreeCloud(region=region, device_type=device_type)
        client.login_email(email, password)
        return [
            {
                "sn": device.sn, "alias": device.alias, "model": device.model,
                "online": device.online, "key": device.key,
            }
            for device in client.list_devices()
            if device.sn
        ]
    except Exception as exc:  # noqa: BLE001 - a cloud miss is not a failure
        logger.info("account device list unavailable: %r", exc)
        return []


# ─── matching ─────────────────────────────────────────────────────────


def _multicast(device_type: str, timeout: float) -> Dict[str, str]:
    try:
        from unitree_webrtc_connect.multicast_scanner import discover_ip_sn

        # The library prints "Discovering devices on the network..." itself.
        # Swallowed here because this module's output is consumed — by a report,
        # by `--json`, by a test — and a stray print in the middle of a
        # structured result is the kind of thing that gets parsed as data.
        with contextlib.redirect_stdout(io.StringIO()):
            return discover_ip_sn(timeout=timeout, device_type=device_type) or {}
    except Exception as exc:  # noqa: BLE001 - a miss is just a miss
        logger.info("multicast discovery failed: %r", exc)
        return {}


async def _match_by_key(
    addresses: Sequence[str], devices: Sequence[Dict[str, Any]], model: str
) -> List[Found]:
    """Which serial number belongs to which address, by trying the keys.

    `data2 === 3` firmware encrypts its handshake with the key for its own
    serial number, so the key that decrypts identifies the robot. This is a
    measurement, not a guess — and it is the only way to tell two identical
    Go2s apart when multicast is silent, which on the network this was built
    against it always was.
    """
    from .preflight import probe_robot
    from .spec import RobotSpec

    with_key = [device for device in devices if device.get("key")]
    if not with_key or not addresses:
        return []

    jobs = []
    for ip in addresses:
        for device in with_key:
            spec = RobotSpec.from_mapping(
                {"id": device["sn"], "model": model, "ip": ip, "sn": device["sn"]}
            )
            jobs.append((ip, device, spec))

    results = await asyncio.gather(
        *(probe_robot(spec, device["key"]) for _ip, device, spec in jobs),
        return_exceptions=True,
    )

    found: List[Found] = []
    taken: set = set()
    for (ip, device, _spec), result in zip(jobs, results):
        if isinstance(result, BaseException):
            continue
        if not getattr(result, "key_accepted", False):
            continue
        if ip in taken:
            # Two serial numbers cannot both be the robot at one address. The
            # first is reported and the second is dropped rather than producing
            # a roster with a robot listed twice.
            continue
        taken.add(ip)
        found.append(Found(
            ip=ip, sn=device["sn"], alias=device.get("alias", ""), model=model,
            matched_by="key", port=CON_NOTIFY_PORT, data2=result.data2,
            online=device.get("online"),
        ))
    return found


async def discover(
    *,
    device_type: str = "Go2",
    model: str = "Go2",
    subnet: str = "",
    timeout: float = 2.0,
    sweep_lan: bool = True,
    use_account: bool = True,
    email: str = "",
    password: str = "",
    region: str = "cn",
) -> Dict[str, Any]:
    """Everything that can be learned about which robots are here.

    Concurrency, not sequence, because the three sources are independent: the
    sweep is the slow one at a couple of seconds, and the account call is a
    network round trip that has nothing to do with the LAN.
    """
    multicast_task = asyncio.to_thread(_multicast, device_type, timeout)
    sweep_task = sweep(subnet) if sweep_lan else _nothing()
    account_task = (
        asyncio.to_thread(
            account_devices, email=email, password=password,
            region=region, device_type=device_type,
        )
        if use_account else _nothing()
    )
    multicast, swept, devices = await asyncio.gather(
        multicast_task, sweep_task, account_task
    )

    by_sn: Dict[str, Found] = {}
    for sn, ip in (multicast or {}).items():
        by_sn[sn] = Found(ip=ip, sn=sn, model=model, matched_by="multicast",
                          port=CON_NOTIFY_PORT)

    addresses = [ip for ip, port in swept if port == CON_NOTIFY_PORT]
    legacy = [ip for ip, _port in swept if _port == LEGACY_OFFER_PORT]

    # Multicast answers are authoritative; only sweep the leftovers for keys.
    unmatched = [ip for ip in addresses if ip not in {f.ip for f in by_sn.values()}]
    known_keys = {f.ip: f.sn for f in by_sn.values()}
    for found in await _match_by_key(unmatched, devices or [], model):
        by_sn[found.sn] = found

    # Addresses that still have no serial number: real robots, unknown names.
    claimed = {found.ip for found in by_sn.values()}
    unidentified = [
        Found(ip=ip, model=model, port=CON_NOTIFY_PORT, matched_by="",
              data2=3 if devices else None)
        for ip in addresses if ip not in claimed and ip not in known_keys
    ]
    unidentified += [
        Found(ip=ip, model=model, port=LEGACY_OFFER_PORT, matched_by="")
        for ip in legacy if ip not in claimed
    ]

    # A device the account knows about but the LAN did not answer for is still
    # a usable roster entry: `target` may be a serial number, and the driver
    # resolves it. Leaving these out was the difference between "found nothing"
    # and a roster that works, on a network where a silent multicast is normal.
    on_lan = {found.sn for found in by_sn.values() if found.sn}
    for device in devices or []:
        if device["sn"] in on_lan:
            continue
        by_sn[device["sn"]] = Found(
            ip="", sn=device["sn"], alias=device.get("alias", ""), model=model,
            matched_by="account", online=device.get("online"),
        )

    found_all = sorted(
        list(by_sn.values()) + unidentified, key=lambda item: (not item.identified, item.ip)
    )

    return {
        "subnet": subnet or local_subnet(),
        "local_ip": local_ipv4(),
        "multicast": multicast or {},
        "swept": [{"ip": ip, "port": port} for ip, port in swept],
        # The key itself never leaves this module: it is a credential, and this
        # result is printed, logged and pasted.  carries the only thing
        # the caller needs from it.
        "account": [
            {**{k: v for k, v in device.items() if k != "key"},
             "has_key": bool(device.get("key"))}
            for device in (devices or [])
        ],
        "found": [item.as_dict() for item in found_all],
        "roster": _roster_snippet(found_all),
        "notes": _notes(multicast or {}, swept, devices or []),
        "address_hint": (
            "账号里有 {n} 台设备只有序列号、没有 IP。roster 里可以只写 sn —— "
            "驱动会去解析；或者从路由器后台/官方 App 拿 IP 填上。".format(n=len(by_sn))
            if any(not item.ip for item in by_sn.values()) else ""
        ),
    }


async def _nothing() -> Any:
    return []


def _roster_snippet(found: Sequence[Found]) -> Dict[str, Any]:
    """A roster built from what was found, ready to paste.

    Only identified robots go in. An entry with an address and no serial
    number is a valid roster line, but it is also a robot the console cannot
    fetch a key for, so it is offered separately rather than mixed in.
    """
    identified = [item for item in found if item.identified]
    return {
        "default": identified[0].to_spec("dog1")["id"] if identified else "dog1",
        "robots": [
            item.to_spec(f"dog{index + 1}") for index, item in enumerate(identified)
        ],
    }


def _notes(
    multicast: Dict[str, str], swept: Sequence[Tuple[str, int]],
    devices: Sequence[Dict[str, Any]],
) -> List[str]:
    """What each source did, in words, because "found nothing" has causes."""
    notes: List[str] = []
    if multicast:
        notes.append(f"多播应答了 {len(multicast)} 只，可以给出 序列号↔IP 的对应。")
    else:
        notes.append(
            "多播没有应答。这台网络上一直如此 —— 不代表狗不在，只代表这条路走不通。"
        )
    if swept:
        notes.append(f"局域网扫描到 {len(swept)} 个信令端口。")
    else:
        notes.append("局域网扫描没有发现信令端口。确认和狗在同一网段。")
    if devices:
        notes.append(f"账号下有 {len(devices)} 台设备。")
        without_key = [d for d in devices if not d.get("key")]
        if without_key:
            notes.append(
                f"{len(without_key)} 台没有取到密钥，无法用密钥试配来确定 IP。"
            )
    else:
        notes.append("读不到账号设备列表 —— 没登录过、或者网络不通。")
    return notes
