"""Serialising and de-blocking robot connects.

Two problems, both invisible with one robot and both real with two.

**Multicast discovery is not concurrency-safe.** ``discover_ip_sn`` binds UDP
port 10134 with ``SO_REUSEADDR`` and then collects *every* robot that answers
— it does not filter replies against the serial number it asked for. So two
robots doing discovery at the same time read each other's answers, and one of
them can be handed the other's address. The reply that matters is not
correlated with the request that caused it, which means this cannot be fixed
by retrying; it has to be serialised.

**The handshake blocks the shared event loop.** ``WebRTCBackend`` offloads the
constructor to a worker thread but then ``await conn.connect()`` directly on
the loop, and inside the driver that path performs blocking I/O: a UDP
discovery wait, and blocking ``requests`` calls for the SDP exchange. While it
runs, *every other robot's control loop is frozen* — including a 20 Hz
joystick stream. Connecting robot 3 stalling robots 1 and 2 is not a
theoretical concern; it is what the code does.

The two fixes here are deliberately narrow. Serialising the whole handshake
process-wide is cheap and removes the cross-talk entirely. Resolving
serial-number to address *before* entering the driver removes the largest
blocking window (a full ``timeout``-second UDP wait) from the loop, and is
what lets the roster work by serial number without stalling anything.

What is left blocking on the loop is the local SDP POST, which is a single
request to a robot on the same LAN. That is a real but small window, and it
is measured rather than assumed — see :meth:`ConnectGate.stats`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Dict, Optional

logger = logging.getLogger("unitree_robot_agent.fleet.connect")

#: Default discovery wait when resolving a serial number off-loop.
DISCOVERY_TIMEOUT_S = 3.0


@dataclass
class ConnectGate:
    """Allows one robot handshake at a time, process-wide.

    ``stagger_s`` inserts a gap between consecutive connects. Robots accept a
    single client each, and the firmware reacts badly to two near-simultaneous
    handshakes from one host, so a small gap is cheap insurance.
    """

    stagger_s: float = 0.15
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _last_release: float = field(default=0.0, repr=False)
    #: Per-robot wall time of the last handshake, for diagnostics.
    stats: Dict[str, float] = field(default_factory=dict, repr=False)

    @asynccontextmanager
    async def hold(self, robot_id: str = ""):
        """Serialise one handshake and record how long it took."""
        async with self._lock:
            if self.stagger_s and self._last_release:
                gap = self.stagger_s - (time.monotonic() - self._last_release)
                if gap > 0:
                    await asyncio.sleep(gap)
            started = time.monotonic()
            try:
                yield
            finally:
                elapsed = time.monotonic() - started
                self._last_release = time.monotonic()
                if robot_id:
                    self.stats[robot_id] = round(elapsed, 3)
                logger.debug("connect %s took %.2fs", robot_id or "?", elapsed)


#: The gate every WebRTC backend shares, so the protection is process-wide
#: rather than per-fleet. Two FleetManagers in one process would otherwise
#: each think they were alone.
DEFAULT_GATE = ConnectGate()


async def resolve_target(config) -> str:
    """Resolve a serial number to an address, off the event loop.

    Returns ``""`` when there is nothing to resolve or the robot did not
    answer. The caller should then fall back to letting the driver try —
    a miss here is not proof the robot is absent, only that this one scan
    did not hear it.
    """
    if getattr(config, "ip", None):
        return config.ip
    serial = getattr(config, "serial_number", None)
    if not serial:
        return ""

    device_type = getattr(config, "device_type", "") or getattr(config, "robot", "Go2")

    def _scan() -> Dict[str, str]:
        # Imported here so a missing driver install does not break the module,
        # and so tests can patch the scanner in one obvious place.
        from unitree_webrtc_connect.multicast_scanner import discover_ip_sn

        return discover_ip_sn(
            timeout=DISCOVERY_TIMEOUT_S, device_type=device_type, sn=serial
        )

    try:
        found = await asyncio.to_thread(_scan)
    except Exception as exc:  # noqa: BLE001 - a failed scan is just a miss
        logger.info("discovery for %s failed: %r", serial, exc)
        return ""

    address = (found or {}).get(serial, "")
    if address:
        logger.info("resolved %s -> %s", serial, address)
    else:
        logger.info("no answer for serial %s on the %s groups", serial, device_type)
    return address


@asynccontextmanager
async def exclusive_connect(robot_id: str = "", gate: Optional[ConnectGate] = None):
    """Hold the process-wide connect gate for one handshake."""
    async with (gate or DEFAULT_GATE).hold(robot_id):
        yield


def reset_default_gate() -> None:
    """Fresh gate — for tests that need a clean lock."""
    global DEFAULT_GATE
    DEFAULT_GATE = ConnectGate()
