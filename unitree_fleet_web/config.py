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
    #: Serve the built-in single-page console.
    serve_ui: bool = True
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
