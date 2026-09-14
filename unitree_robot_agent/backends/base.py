"""Transport-agnostic robot backend.

Everything above this layer talks to the robot only through
:class:`RobotBackend`. Two implementations exist:

* :class:`~unitree_robot_agent.backends.webrtc_backend.WebRTCBackend` — the
  real one, wrapping ``unitree_webrtc_connect``.
* :class:`~unitree_robot_agent.backends.mock_backend.MockBackend` — an
  offline simulator that reproduces the behaviours that make the real
  robot awkward (811→830 redirection, white/black-list refusals, frozen
  state reporting, single-client exclusion). It powers the offline
  self-test and the unit tests.

Keeping the protocol this small is deliberate: it is the whole surface a
new transport (e.g. a future DDS bridge) has to implement.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable


@dataclass
class ApiResponse:
    """Normalised reply to an ``api_id`` request.

    ``code`` is the robot's own status code (``0`` = accepted). ``-1`` is
    used when no reply arrived at all, which is distinct from a refusal —
    the humanoid FSM freezes its state reporting in several states, and
    "no answer" must not be reported as "rejected".
    """

    code: int = -1
    data: Any = None
    raw: dict = field(default_factory=dict)
    message: str = ""

    #: Set when the caller should not trust ``code`` at all.
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def answered(self) -> bool:
        """True when the robot actually replied (even with an error code)."""
        return not self.timed_out and self.code != -1

    def decoded(self, default: Any = None) -> Any:
        """``data`` is frequently a JSON string; decode it when it is."""
        value = self.data
        if isinstance(value, str):
            try:
                return json.loads(value)
            except ValueError:
                return default if default is not None else value
        return value if value is not None else default

    def to_dict(self) -> dict:
        out: dict = {"code": self.code, "ok": self.ok}
        if self.timed_out:
            out["timed_out"] = True
        if self.message:
            out["message"] = self.message
        data = self.decoded(default=None)
        if data is not None:
            out["data"] = data
        return out


@runtime_checkable
class RobotBackend(Protocol):
    """The minimal surface the session layer needs."""

    #: Robot family this backend is talking to ("R1" / "Go2" / "G1").
    robot_name: str

    @property
    def is_connected(self) -> bool:
        ...

    async def connect(self) -> None:
        """Open the transport. Raises AgentError subclasses on failure."""

    async def disconnect(self) -> None:
        """Close the transport. Must be idempotent and never raise."""

    async def api_request(
        self,
        topic: str,
        api_id: int,
        parameter: Any = None,
        *,
        timeout: Optional[float] = None,
    ) -> ApiResponse:
        """Send an ``api_id`` request and wait for the matching reply."""

    def publish(self, topic: str, data: Any = None, msg_type: Optional[str] = None) -> None:
        """Fire-and-forget publish (no reply expected)."""

    async def latest(
        self,
        topic: str,
        *,
        subscribe: bool = True,
        timeout: Optional[float] = None,
        max_age: Optional[float] = None,
    ) -> Optional[dict]:
        """Return the most recent message for ``topic``.

        ``subscribe=True`` makes the backend register the topic on first
        use (and remember it) so repeated reads are cache hits rather than
        N+1 subscriptions. Returns ``None`` when nothing has arrived yet —
        callers must treat that as "unknown", never as "unchanged".
        """

    async def snapshot_frame(self, *, timeout: float = 5.0) -> Optional[bytes]:
        """Grab a single JPEG frame from the robot camera, if supported."""

    def error_events(self) -> list:
        """Robot error notifications seen on the data channel.

        The driver reports errors as unsolicited messages (``errors`` /
        ``add_error`` / ``rm_error``), not as replies, so they are captured
        out-of-band into a small ring buffer."""

    async def aclose(self) -> None:
        """Release background tasks/handles (called once at shutdown)."""


class NullBackend:
    """Backend used when the configuration is incomplete.

    Every operation raises :class:`RobotNotConfigured` with the actionable
    hint, so a misconfigured server still starts and can be diagnosed with
    ``doctor`` instead of crashing at import time."""

    robot_name = "unknown"

    def __init__(self, reason: str = "") -> None:
        self.reason = reason

    @property
    def is_connected(self) -> bool:
        return False

    def _raise(self):
        from ..errors import RobotNotConfigured

        raise RobotNotConfigured(self.reason or "Robot is not configured.")

    async def connect(self) -> None:
        self._raise()

    async def disconnect(self) -> None:
        return None

    async def api_request(self, topic, api_id, parameter=None, *, timeout=None):  # noqa: D102
        self._raise()

    def publish(self, topic, data=None, msg_type=None) -> None:  # noqa: D102
        self._raise()

    async def latest(self, topic, *, subscribe=True, timeout=None, max_age=None):  # noqa: D102
        self._raise()

    async def snapshot_frame(self, *, timeout: float = 5.0):  # noqa: D102
        self._raise()

    def error_events(self) -> list:  # noqa: D102
        return []

    async def aclose(self) -> None:
        return None
