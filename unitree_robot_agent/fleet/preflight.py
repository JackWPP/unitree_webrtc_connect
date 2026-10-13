"""Can we actually talk to these robots, and what will the handshake need?

The expensive failures in this project have all been *diagnostic*, not
mechanical: a `data2=3` robot with no key fails inside RSA parsing with a
traceback that reads like a network problem; a second Go2 handed the first
one's key fails a GCM tag check that reads like a hardware fault. Both cost
more time to identify than to fix.

So this answers the question before the handshake instead of after it:

* is the robot reachable at all, and on which signaling port?
* does its firmware use the static key (`data2=2`), a per-device key
  (`data2=3`), or no key (`data2=1`)?
* if it needs a per-device key, do we have one, and is it *this robot's*?

**It is read-only.** It performs `con_notify`, which returns a public key and
a path token, and then stops — it never posts an SDP offer to `con_ing_<path>`.
That matters because a Unitree robot accepts exactly one WebRTC client: a
preflight can therefore run while the official app is connected, and it will
not disturb anyone or claim the slot.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .keys import KeyStore
from .roster import Roster
from .spec import RobotSpec

logger = logging.getLogger("unitree_robot_agent.fleet.preflight")

#: Signaling ports, in the order the driver tries them.
CON_NOTIFY_PORT = 9991
LEGACY_OFFER_PORT = 8081

#: What the operator should do next, per outcome.
VERDICT_HINTS: Dict[str, str] = {
    "ok": "Ready to connect.",
    "no_key": (
        "This firmware needs the per-device AES-128 key. Fetch it with "
        "`unitree-fetch-aes-key --sn <sn> --quiet` and put it in the roster, "
        "or set the account so the server can fetch it on first connect."
    ),
    "key_rejected": (
        "A key is present but this robot rejected it (GCM tag check). Keys are "
        "per device — this one almost certainly belongs to a different robot. "
        "Re-fetch it for this serial number."
    ),
    "no_address": (
        "No IP and no serial number: nothing to probe. Give the robot one of "
        "them in the roster."
    ),
    "not_found": (
        "The robot did not answer multicast discovery. Check it is powered on "
        "and on the same network — or give its IP directly, which skips "
        "discovery entirely."
    ),
    "unreachable": (
        "Neither signaling port answered. Check the robot is powered on and on "
        "this network, and that no firewall is blocking 9991/8081."
    ),
    "legacy": (
        "Only the legacy port 8081 answered: pre-1.1.11 Go2 firmware. It needs "
        "no key, but some newer features will be unavailable."
    ),
    "probe_failed": (
        "The robot accepted a connection but its con_notify reply could not be "
        "read. It may be mid-boot, or another client may be mid-handshake."
    ),
}


@dataclass
class PreflightResult:
    """What we learned about one robot, without connecting to it."""

    robot_id: str
    model: str
    target: str
    ip: str = ""
    reachable: bool = False
    #: Signaling port that answered, if any.
    port: Optional[int] = None
    #: ``"con_notify"`` (port 9991) or ``"legacy"`` (port 8081).
    signaling: str = ""
    #: The robot's own answer: 1 = plaintext, 2 = static key, 3 = per-device.
    data2: Optional[int] = None
    key_required: bool = False
    key_present: bool = False
    #: Whether the key we hold actually decrypts this robot's payload.
    key_accepted: Optional[bool] = None
    verdict: str = "unreachable"
    detail: str = ""

    @property
    def hint(self) -> str:
        return VERDICT_HINTS.get(self.verdict, "")

    @property
    def ok(self) -> bool:
        return self.verdict in ("ok", "legacy")

    def as_dict(self) -> Dict[str, Any]:
        return {
            "robot_id": self.robot_id,
            "model": self.model,
            "target": self.target,
            "ip": self.ip,
            "reachable": self.reachable,
            "port": self.port,
            "signaling": self.signaling,
            "data2": self.data2,
            "key_required": self.key_required,
            "key_present": self.key_present,
            "key_accepted": self.key_accepted,
            "verdict": self.verdict,
            "ok": self.ok,
            "detail": self.detail,
            "hint": self.hint,
        }


def _probe_sync(spec: RobotSpec, aes_key: str) -> PreflightResult:
    """The blocking half. Always run through :func:`probe_robot`."""
    # Imported here so a missing driver install does not break the module.
    from unitree_webrtc_connect.unitree_auth import (
        AesKeyRejectedError,
        AesKeyRequiredError,
        _decrypt_data1_legacy,
        _decrypt_data1_v3,
        _probe_tcp_port,
        make_local_request,
    )

    result = PreflightResult(
        robot_id=spec.robot_id,
        model=spec.model,
        target=spec.target,
        ip=spec.ip,
        key_present=bool(aes_key),
    )

    if not spec.ip:
        result.verdict = "no_address"
        result.detail = "no IP address to probe"
        return result

    # 9991 first, matching the driver's own order.
    if _probe_tcp_port(spec.ip, CON_NOTIFY_PORT):
        result.reachable = True
        result.port = CON_NOTIFY_PORT
        result.signaling = "con_notify"
    elif _probe_tcp_port(spec.ip, LEGACY_OFFER_PORT):
        result.reachable = True
        result.port = LEGACY_OFFER_PORT
        result.signaling = "legacy"
        result.verdict = "legacy"
        result.detail = f"{spec.ip}:{LEGACY_OFFER_PORT} answered (/offer)"
        return result
    else:
        result.detail = f"no answer on {CON_NOTIFY_PORT} or {LEGACY_OFFER_PORT}"
        return result

    response = make_local_request(f"http://{spec.ip}:{CON_NOTIFY_PORT}/con_notify")
    if response is None:
        result.verdict = "probe_failed"
        result.detail = "con_notify did not return a usable response"
        return result

    try:
        decoded = json.loads(base64.b64decode(response.text).decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - any malformed reply is the same story
        result.verdict = "probe_failed"
        result.detail = f"con_notify reply was not the expected base64 JSON: {exc}"
        return result

    data1 = decoded.get("data1")
    data2 = decoded.get("data2")
    result.data2 = data2 if isinstance(data2, int) else None

    try:
        if data2 == 2:
            _decrypt_data1_legacy(data1)
            result.verdict = "ok"
            result.detail = "data2=2: static key, handled automatically"
        elif data2 == 3:
            result.key_required = True
            _decrypt_data1_v3(data1, aes_key)
            result.key_accepted = True
            result.verdict = "ok"
            result.detail = "data2=3: per-device key accepted"
        else:
            # data2 == 1 or absent: the payload is already plaintext.
            result.verdict = "ok"
            result.detail = f"data2={data2!r}: no key needed"
    except AesKeyRequiredError:
        result.key_required = True
        result.key_accepted = None
        result.verdict = "no_key"
        result.detail = "data2=3 and no key is configured for this robot"
    except AesKeyRejectedError:
        result.key_required = True
        result.key_accepted = False
        result.verdict = "key_rejected"
        result.detail = "data2=3 and the configured key failed the GCM tag check"
    except Exception as exc:  # noqa: BLE001
        result.verdict = "probe_failed"
        result.detail = f"{type(exc).__name__}: {exc}"

    return result


async def probe_robot(spec: RobotSpec, aes_key: str = "") -> PreflightResult:
    """Probe one robot off the event loop."""
    return await asyncio.to_thread(_probe_sync, spec, aes_key)


async def _resolve(spec: RobotSpec) -> RobotSpec:
    """Fill in a missing IP from multicast discovery, off the loop."""
    if spec.ip or not spec.serial_number:
        return spec
    from .connect import resolve_target

    address = await resolve_target(spec)
    if not address:
        return spec
    from dataclasses import replace

    return replace(spec, ip=address)


async def run_preflight(roster: Roster, *, base_state_dir=None) -> List[PreflightResult]:
    """Probe every robot on the roster, concurrently.

    The key lookup uses each robot's own store, so a second same-model robot
    cannot be handed the first one's key — which is the failure this is
    largely here to catch, and it would be ironic to reproduce it in the
    diagnostic.
    """
    from pathlib import Path

    base = Path(base_state_dir) if base_state_dir is not None else roster.state_dir

    async def one(spec: RobotSpec) -> PreflightResult:
        resolved = await _resolve(spec)
        if not resolved.ip:
            return PreflightResult(
                robot_id=spec.robot_id,
                model=spec.model,
                target=spec.target,
                verdict="not_found" if spec.serial_number else "no_address",
                detail=(
                    f"serial {spec.serial_number} did not answer discovery"
                    if spec.serial_number
                    else "no IP and no serial number to work from"
                ),
            )

        key = spec.aes_128_key
        if not key:
            # Per-robot state dir, exactly as the manager lays it out.
            store = KeyStore(Path(base).expanduser() / spec.robot_id / "keys.json")
            key = store.get(resolved) or ""
        return await probe_robot(resolved, key)

    return list(await asyncio.gather(*(one(spec) for spec in roster)))


def summarise(results: List[PreflightResult]) -> Dict[str, Any]:
    """A one-line verdict plus the per-robot detail."""
    blocked = [r for r in results if not r.ok]
    return {
        "count": len(results),
        "ready": [r.robot_id for r in results if r.ok],
        "blocked": [r.robot_id for r in blocked],
        "complete": not blocked,
        "results": [r.as_dict() for r in results],
        "note": (
            "Read-only: the handshake was started (con_notify) but no SDP was "
            "sent, so the robot's single WebRTC client slot was not claimed."
        ),
    }
