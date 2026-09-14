"""Transport backends for the agent layer."""

from .base import ApiResponse, NullBackend, RobotBackend
from .factory import build_backend
from .mock_backend import MockBackend

__all__ = [
    "ApiResponse",
    "MockBackend",
    "NullBackend",
    "RobotBackend",
    "build_backend",
]


def __getattr__(name):
    # Keep the real backend (and therefore the driver import chain) out of
    # `import unitree_robot_agent.backends` so offline/mock runs stay light.
    if name == "WebRTCBackend":
        from .webrtc_backend import WebRTCBackend

        return WebRTCBackend
    raise AttributeError(name)
