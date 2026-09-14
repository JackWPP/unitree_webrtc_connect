"""Backend selection."""

from __future__ import annotations

import logging

from ..errors import AgentError
from .base import NullBackend, RobotBackend
from .mock_backend import MockBackend

logger = logging.getLogger("unitree_robot_agent.backend")


def build_backend(config, audit=None, auth=None) -> RobotBackend:
    """Return the backend implied by ``config.backend``.

    ``auto`` prefers a live WebRTC link, but a missing address does not
    abort startup: it returns a :class:`NullBackend` that raises the
    actionable error on first use, so ``doctor`` and the read-only tools
    can still explain what is wrong.
    """
    mode = (config.backend or "auto").lower()

    if mode == "mock":
        logger.info("using mock backend for %s", config.robot)
        return MockBackend(config.robot)

    from .webrtc_backend import WebRTCBackend

    if mode == "webrtc":
        return WebRTCBackend(config, audit, auth)

    # auto
    try:
        config.require_target()
    except AgentError as exc:
        logger.warning("robot target is not configured: %s", exc)
        return NullBackend(str(exc))

    return WebRTCBackend(config, audit, auth)
