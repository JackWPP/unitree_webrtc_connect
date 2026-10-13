"""The console's own settings, separate from any robot's.

Kept apart from ``AgentConfig`` on purpose: a robot's config is per-device
identity and credentials, while these are properties of the *operator
station* — where it listens, who may talk to it, and how fast it polls.
Sharing one object across the two is how a console-level mistake turns into
a robot-level one.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

#: Bind to loopback unless explicitly told otherwise. A console that can
#: drive two robots must not appear on the LAN by accident; the previous
#: attempt at this (`wpp`'s `web_server.py`) defaulted to `0.0.0.0` with no
#: authentication at all.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

#: Teleop frames older than this mean the operator has gone away, and
#: whatever is still moving gets zeroed. The browser publishes at ~20 Hz.
DEFAULT_DEADMAN_S = 0.5

#: Status push rate to the WebSocket.
DEFAULT_STATUS_HZ = 4.0

#: MJPEG frame rate ceiling per robot. Two live streams share one uplink.
DEFAULT_STREAM_FPS = 10.0

#: How often to re-assert the console's claim on a connected robot while a
#: browser is watching. Robot sessions release themselves after
#: `idle_disconnect_s` so the official app can take over; a console with the
#: page open is *not* idle, and having the link drop under the operator every
#: 45 seconds would make the video stream and the next command both fail.
#: 0 disables the lease.
DEFAULT_LEASE_S = 5.0

#: How often to refresh battery/IMU/gait per robot.
#:
#: Deliberately slower than the status push. Battery does not move at 4 Hz, and
#: doing topic reads on the push path would put robot I/O on a loop that is
#: otherwise pure memory. `0` disables telemetry, and the panels fall back to
#: the connection state alone.
DEFAULT_TELEMETRY_HZ = 1.0

#: Velocity re-send rate while driving. See DEFAULT_MOTION_HZ in service.py.
#: How often to look at the arena camera. Higher than the status push because
#: detection is cheap and a position that lags a moving dog by a fifth of a
#: second is a position that is wrong by wherever the dog went next.
DEFAULT_ARENA_HZ = 10.0

DEFAULT_MOTION_HZ = 50.0

#: How long the auto-stand gate waits before giving up and dropping the target.
#:
#: `unitree_harness` uses 5 s for the same thing. After this the drive state
#: reports `stand_timeout` instead of the console quietly asking a robot to
#: stand forever.
DEFAULT_STAND_TIMEOUT_S = 5.0

#: How long after the last browser goes away the console hands its links back.
#:
#: Without this the links wait out the session's own `idle_disconnect_s` (45 s)
#: plus a lease tick, so an owner standing next to a dog waits the better part
#: of a minute for the official app to get in — the opposite of "take over
#: safely". A closed tab is not a session anyone is coming back to.
#:
#: Not zero on purpose: a page refresh, a brief network blip or a background
#: tab that stops polling must not drop a link the operator is still using.
#: `0` disables it and falls back to plain idle release.
DEFAULT_UNWATCHED_RELEASE_S = 10.0


def _env(name: str) -> Optional[str]:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    return raw.strip()


@dataclass
class ConsoleConfig:
    """Where the console listens and how it behaves."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    #: Shared secret. Empty means "no authentication", which is only
    #: acceptable while bound to loopback — :meth:`check` enforces that.
    token: str = ""
    deadman_s: float = DEFAULT_DEADMAN_S
    status_hz: float = DEFAULT_STATUS_HZ
    stream_fps: float = DEFAULT_STREAM_FPS
    #: Re-assert the console's claim on connected robots this often, while at
    #: least one browser is watching. Zero disables the lease, so links
    #: release themselves when idle.
    lease_s: float = DEFAULT_LEASE_S
    #: Battery/IMU/gait refresh rate. See DEFAULT_TELEMETRY_HZ.
    telemetry_hz: float = DEFAULT_TELEMETRY_HZ
    #: Ramped-velocity re-send rate. `0` disables the ramp entirely and returns
    #: to publishing the stick position directly — not recommended, but it is
    #: the escape hatch if the ramp ever misbehaves on new firmware.
    motion_hz: float = DEFAULT_MOTION_HZ
    #: See DEFAULT_STAND_TIMEOUT_S.
    stand_timeout_s: float = DEFAULT_STAND_TIMEOUT_S
    #: Arena camera for the fiducial markers: a device index ("0"), a device
    #: path, or a video file. Empty disables it, and the console then says the
    #: map is unavailable rather than drawing an empty arena.
    arena_camera: str = ""
    #: JSON with the four calibration points. Without it there is no mapping
    #: from pixels to metres, so there is no position — only a bearing.
    arena_calibration: str = ""
    arena_hz: float = DEFAULT_ARENA_HZ
    #: Hand the links back this long after the last browser leaves. See
    #: DEFAULT_UNWATCHED_RELEASE_S.
    unwatched_release_s: float = DEFAULT_UNWATCHED_RELEASE_S
    #: Serve the built-in single-page console.
    serve_ui: bool = True
    #: Whether fetching a video frame may bring a robot up.
    #:
    #: True means opening the console connects the dogs, which is what "both
    #: visible in the browser" requires — and if the official app already
    #: holds the slot the connect fails with `robot_busy` and the pen shows
    #: offline, so nobody is kicked off. False makes the console a pure
    #: observer until the operator presses 连接.
    video_autoconnect: bool = True
    static_dir: Optional[Path] = None

    @property
    def is_loopback(self) -> bool:
        return self.host in ("127.0.0.1", "::1", "localhost")

    def check(self) -> None:
        """Refuse a configuration that exposes robot control to the network.

        The failure mode this prevents is the one the previous web attempt
        shipped: an unauthenticated control surface on ``0.0.0.0``.
        """
        if not self.is_loopback and not self.token:
            raise ValueError(
                f"Refusing to bind {self.host} without a token. Pass "
                f"--token (or set UNITREE_CONSOLE_TOKEN), or bind 127.0.0.1."
            )
        if self.token and len(self.token) < 16:
            raise ValueError(
                "The console token must be at least 16 characters; it is the "
                "only thing between the network and two robots."
            )
        if self.port <= 0 or self.port > 65535:
            raise ValueError(f"Invalid port: {self.port}")
        if self.deadman_s <= 0:
            raise ValueError("deadman_s must be positive")

    def authorises(self, presented: Optional[str]) -> bool:
        """Constant-time token check. No token configured ⇒ loopback only."""
        if not self.token:
            return True
        if not presented:
            return False
        return secrets.compare_digest(self.token, presented)

    @classmethod
    def from_env(cls, **overrides) -> "ConsoleConfig":
        values = {
            "host": _env("UNITREE_CONSOLE_HOST") or DEFAULT_HOST,
            "port": int(_env("UNITREE_CONSOLE_PORT") or DEFAULT_PORT),
            "token": _env("UNITREE_CONSOLE_TOKEN") or "",
        }
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)
