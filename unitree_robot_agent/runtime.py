"""Wiring: config + profile + backend + audit → one session object."""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from typing import Optional

from .backends.base import RobotBackend
from .backends.factory import build_backend
from .config import AgentConfig, config_from_env
from .core.audit import AuditLog
from .core.auth import CloudAuth
from .core.faults import FaultStore
from .core.session import RobotSession
from .motion import MotionController
from .robots.base import RobotProfile
from .robots.registry import get_profile
from .vision import VisionService

logger = logging.getLogger("unitree_robot_agent.runtime")


@dataclass
class Runtime:
    """Everything a server / CLI run needs."""

    config: AgentConfig
    profile: RobotProfile
    audit: AuditLog
    session: RobotSession
    backend: RobotBackend
    auth: CloudAuth
    vision: VisionService
    motion: MotionController
    #: The robot's active fault set, reconciled from the wire. Per-runtime,
    #: so two robots never share a fault picture.
    faults: FaultStore

    async def aclose(self) -> None:
        await self.session.aclose()


def create_runtime(config: Optional[AgentConfig] = None, **overrides) -> Runtime:
    """Build a ready-to-use :class:`Runtime`."""
    cfg = config or config_from_env(**overrides)

    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.WARNING),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    profile = get_profile(cfg.robot, chassis=cfg.chassis)
    audit = AuditLog(
        path=cfg.audit_log if cfg.audit_enabled else None,
        enabled=cfg.audit_enabled,
        to_stderr=cfg.audit_to_stderr,
        robot_id=cfg.robot_id,
    )
    auth = CloudAuth(cfg, audit)
    backend = build_backend(cfg, audit, auth)
    faults = FaultStore()
    # Push, not poll: the error ring is bounded and trimmed, so a reader that
    # samples it can miss notifications entirely. Duck-typed because the
    # backend Protocol stays minimal and a null backend simply has no errors.
    setter = getattr(backend, "set_error_listener", None)
    if callable(setter):
        with contextlib.suppress(Exception):
            setter(faults.feed_event)
    session = RobotSession(cfg, backend, profile, audit)
    vision = VisionService(cfg, backend, audit, session=session)
    motion = MotionController(session, cfg)
    session.set_motion_abort(motion.abort_window)

    logger.debug("runtime ready: %s", cfg.redacted())
    return Runtime(
        config=cfg,
        profile=profile,
        audit=audit,
        session=session,
        backend=backend,
        auth=auth,
        vision=vision,
        motion=motion,
        faults=faults,
    )
