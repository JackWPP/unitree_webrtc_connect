"""Agent-facing control layer for Unitree robots (Go2 / R1).

This package turns ``unitree_webrtc_connect`` into something an LLM can
drive safely:

* :mod:`unitree_robot_agent.config` — environment-driven configuration
* :mod:`unitree_robot_agent.backends` — real WebRTC backend + offline mock
* :mod:`unitree_robot_agent.robots` — per-family semantics (R1, Go2)
* :mod:`unitree_robot_agent.core` — session lifecycle, guards, audit log
* :mod:`unitree_robot_agent.server` — the MCP server (stdio / streamable-http)

The design rule everywhere: the model gets *semantic* tools and the hard
safety rules live in code it cannot reconfigure.
"""

from __future__ import annotations

__version__ = "0.1.0"

from .errors import AgentError, GuardRejected, RobotBusy, RobotRefused
from .config import AgentConfig, config_from_env

__all__ = [
    "AgentConfig",
    "AgentError",
    "GuardRejected",
    "RobotBusy",
    "RobotRefused",
    "__version__",
    "config_from_env",
]


def __getattr__(name):
    # Lazy so `import unitree_robot_agent` stays cheap and side-effect free.
    if name == "RobotSession":
        from .core.session import RobotSession

        return RobotSession
    if name == "create_session":
        from .runtime import create_session

        return create_session
    raise AttributeError(name)
