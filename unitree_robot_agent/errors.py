"""Unified error surface for the agent layer.

Every failure that can reach an LLM is normalised into an ``AgentError``
carrying a stable machine-readable ``code`` and a human/agent-readable
``hint``. The hint matters more than the message: an agent that gets
"robot_busy" plus "close unitree_ui and retry" can self-correct, whereas a
raw traceback just gets retried.

The mapping from the upstream library's typed exceptions lives in
:func:`classify_exception` so backends stay thin.
"""

from __future__ import annotations

from typing import Any, Optional


class AgentError(RuntimeError):
    """Base class for every error the agent layer raises."""

    code: str = "agent_error"
    #: Short, actionable next step for the caller.
    hint: str = ""
    #: True when retrying the same call later has a chance of working.
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        code: Optional[str] = None,
        hint: Optional[str] = None,
        retryable: Optional[bool] = None,
        detail: Any = None,
    ) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        if hint is not None:
            self.hint = hint
        if retryable is not None:
            self.retryable = retryable
        self.detail = detail

    def to_dict(self) -> dict:
        payload = {
            "ok": False,
            "error": self.code,
            "message": str(self),
            "retryable": self.retryable,
        }
        if self.hint:
            payload["hint"] = self.hint
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload


# ─── configuration / lifecycle ────────────────────────────────────────

class RobotNotConfigured(AgentError):
    code = "robot_not_configured"
    hint = (
        "Set the robot address first: export UNITREE_AGENT_ROBOT=R1 and "
        "UNITREE_ROBOT_IP=<ip> (or UNITREE_ROBOT_SN=<sn>), plus "
        "UNITREE_AES_128_KEY for R1 / Go2 >= 1.1.15. Run "
        "`unitree-robot-mcp doctor` to see what is missing."
    )


class RobotBusy(AgentError):
    code = "robot_busy"
    retryable = True
    hint = (
        "The robot accepts a single WebRTC client at a time and it already "
        "has one. Close the Unitree app / unitree_ui (or anything else "
        "holding the connection), wait a few seconds, then retry."
    )


class AesKeyMissing(AgentError):
    code = "aes_key_missing"
    hint = (
        "This robot needs the per-device AES-128 key (data2=3). Fetch it "
        "with `unitree-fetch-aes-key --device-type R1 --sn <sn> --quiet` "
        "(or --device-type Go2 for Go2), then export UNITREE_AES_128_KEY. "
        "G1/R1 share one Unitree Explorer account."
    )


class AesKeyRejected(AgentError):
    code = "aes_key_rejected"
    hint = (
        "The AES-128 key was rejected (GCM tag check failed). Re-fetch it "
        "for this exact serial number — keys are per device, and a key "
        "belonging to another robot will fail exactly like this."
    )


class ConnectFailed(AgentError):
    code = "connect_failed"
    retryable = True
    hint = (
        "Could not complete the WebRTC handshake. Check that the robot is "
        "powered on, on the same network, and reachable on port 9991/8081 "
        "(STA-L) or that you are joined to its AP (192.168.12.1)."
    )


class UnsupportedConnection(AgentError):
    code = "unsupported_connection"
    hint = "Use one of: ap, sta-l, sta-t."


class UnitreeCloudAuthError(AgentError):
    """Cloud login / token problems, kept separate from robot-link errors."""

    code = "cloud_auth_failed"
    hint = (
        "Check the account details. A common trap: an account registered in "
        "China answers 'email not register' on the global endpoint — use "
        "UNITREE_AGENT_REGION=cn (or leave it as 'auto' to let the server "
        "detect it)."
    )


class NotConnected(AgentError):
    code = "not_connected"
    retryable = True
    hint = "Call a tool that reconnects (any read such as robot_status) and retry."


class SessionClosed(AgentError):
    code = "session_closed"
    hint = "The server is shutting down; start a new session."


# ─── command / guard outcomes ─────────────────────────────────────────

class GuardRejected(AgentError):
    """A hard-coded local rule refused the call before it reached the robot."""

    code = "guard_rejected"
    hint = "Adjust the arguments as described in the message."


class ConfirmRequired(GuardRejected):
    code = "confirm_required"
    hint = (
        "This action can move or drop the robot. Re-issue the exact same "
        "call with confirm=true once the area around the robot is clear."
    )


class RobotRefused(AgentError):
    """The robot answered, but with a non-zero status code."""

    code = "robot_refused"

    def __init__(self, message: str, *, robot_code: int, topic: str = "", api_id: int = 0, **kw):
        self.robot_code = robot_code
        self.topic = topic
        self.api_id = api_id
        detail = kw.pop("detail", None)
        if detail is None:
            detail = {"topic": topic, "api_id": api_id, "robot_code": robot_code}
        super().__init__(message, detail=detail, **kw)


class CommandTimeout(AgentError):
    code = "command_timeout"
    retryable = True
    hint = "The robot did not answer in time. It may be busy or the link may be flaky; retry."


class FeatureUnavailable(AgentError):
    code = "feature_unavailable"
    hint = (
        "This capability is not available on the configured robot or the "
        "optional dependency is missing. See `unitree-robot-mcp doctor`."
    )


# Forward declaration of the FSM refusal texts keyed by the on-robot codes.
# 1001/1002 are white/black-list refusals, 1003 is "no such state".
# 127 was added after hardware testing: see the note below.
FSM_REFUSAL_HINTS = {
    1001: (
        "The current state refuses this transition. Most motions are only "
        "reachable from Lock (4): move to Lock first, then retry."
    ),
    1002: (
        "The current state refuses this transition (black list). Move to "
        "Lock (4) first, then retry."
    ),
    1003: (
        "That state does not exist on this firmware. Use "
        "`r1_list_actions` / `go2_list_actions` to see what is actually "
        "available on this chassis."
    ),
    127: (
        "Code 127 means the robot does not handle this api_id in its current "
        "state. Verified on an R1 Air: SET_VELOCITY (7105) answers 127 while "
        "the robot is in Loco20Dof (830) because that state has no speed "
        "handler. Drive it with robot_joystick (the rt/wirelesscontroller "
        "path the official app uses) instead — robot_move does this "
        "automatically."
    ),
}


def classify_exception(exc: BaseException) -> AgentError:
    """Map an arbitrary exception from the upstream library to an AgentError."""
    if isinstance(exc, AgentError):
        return exc

    # Imported lazily and defensively: the agent layer must stay importable
    # even if the upstream package layout changes.
    try:
        from unitree_webrtc_connect import unitree_auth as _auth
    except Exception:  # pragma: no cover - upstream always present
        _auth = None

    mapping = []
    if _auth is not None:
        mapping = [
            ("RobotBusyError", RobotBusy),
            ("AesKeyRequiredError", AesKeyMissing),
            ("AesKeyRejectedError", AesKeyRejected),
            ("LocalSignalingPortError", ConnectFailed),
            ("NoSdpAnswerError", ConnectFailed),
            ("DataChannelTimeoutError", ConnectFailed),
        ]
    for name, target in mapping:
        cls = getattr(_auth, name, None) if _auth is not None else None
        if cls is not None and isinstance(exc, cls):
            return target(str(exc))

    if isinstance(exc, (ConnectionError, OSError)):
        return ConnectFailed(str(exc))
    if isinstance(exc, TimeoutError):
        return CommandTimeout(str(exc))

    text = str(exc) or exc.__class__.__name__
    low = text.lower()
    if "reject" in low and "sdp" in low:
        return RobotBusy(text)
    if "data channel is not open" in low:
        return NotConnected(text)
    return AgentError(text, code="unexpected_error", detail={"type": exc.__class__.__name__})
