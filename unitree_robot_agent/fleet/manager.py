"""One :class:`Runtime` per robot, and the fleet-level operations on them.

The per-robot machinery already handles a single robot correctly — lazy
connect, idle release, per-session guards, audited calls. Nothing in it is
hostile to N robots; it simply has to be *instantiated* N times. That is this
module's whole job.

Three deliberate choices:

**Wrap, don't modify.** ``create_runtime`` is called once per roster entry
and left alone. The 195-test suite covers it as-is, and the fleet layer is
additive.

**Fan out with per-robot error capture.** A fleet operation must never
report one robot's failure as the whole fleet's failure, and must never let
one bad robot abort the others. ``asyncio.gather(..., return_exceptions=True)``
plus an explicit per-robot result record.

**A stop is reported per robot, honestly.** ``RobotSession.safe_stop``
returns ``False`` *without touching the robot* when the link is down. Folding
that into a single fleet-level boolean would let "dog 2 was not stopped"
read as success, which is the one thing a stop must never do.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from ..config import AgentConfig
from ..errors import FleetConfigError
from ..runtime import Runtime, create_runtime
from .keys import KeyStore
from .roster import Roster
from .spec import RobotSpec

logger = logging.getLogger("unitree_robot_agent.fleet.manager")


def _config_field_names() -> set:
    return {f.name for f in AgentConfig.__dataclass_fields__.values()}


@dataclass
class RobotOutcome:
    """The result of a fleet operation for one robot."""

    robot_id: str
    #: ``delivered`` means the robot acknowledged the command. False is not
    #: the same as "the robot is moving" — see :func:`FleetManager.stop_all`.
    delivered: bool = False
    #: ``up`` | ``down`` | ``error``
    link: str = "up"
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.delivered and self.link == "up"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "robot_id": self.robot_id,
            "ok": self.ok,
            "delivered": self.delivered,
            "link": self.link,
            "detail": self.detail,
        }


class FleetManager:
    """Owns one :class:`Runtime` per robot on the roster.

    Runtimes are built eagerly (cheap — a ``RobotSession`` does not connect
    until something asks it to) but connect lazily, so constructing a fleet
    never touches the network.
    """

    def __init__(
        self,
        roster: Roster,
        *,
        base_config: Optional[AgentConfig] = None,
        backend: Optional[str] = None,
        audit_to_stderr: Optional[bool] = None,
    ) -> None:
        self.roster = roster
        self.base_config = base_config
        self._backend_override = backend
        self._audit_to_stderr = audit_to_stderr
        self._runtimes: Dict[str, Runtime] = {}

        for spec in roster:
            self._runtimes[spec.robot_id] = create_runtime(self.config_for(spec))

    # ── construction ─────────────────────────────────────────────────

    def config_for(self, spec: RobotSpec) -> AgentConfig:
        """Derive one robot's config from the roster.

        Every per-robot field is set explicitly, including ``state_dir`` and
        ``audit_log``. Inheriting either from a shared base is how two robots
        end up sharing a key cache and a log file.
        """
        state_dir = self.roster.state_dir_for(spec.robot_id)

        overrides: Dict[str, Any] = {
            "robot": spec.model,
            "robot_id": spec.robot_id,
            "chassis": spec.chassis,
            "connection": spec.connection,
            "ip": spec.ip or None,
            "serial_number": spec.serial_number or None,
            "aes_128_key": spec.aes_128_key or None,
            "device_type": spec.resolved_device_type,
            "region": spec.region,
            "state_dir": state_dir,
            # Never inherit: this must point inside *this* robot's directory.
            "audit_log": None,
        }

        merged = self.roster.options_for(spec)
        if merged:
            known = _config_field_names()
            unknown = sorted(set(merged) - known)
            if unknown:
                raise FleetConfigError(
                    f"Robot '{spec.robot_id}' sets unknown option(s): "
                    f"{', '.join(unknown)}.",
                    detail={"robot_id": spec.robot_id, "unknown": unknown},
                )
            overrides.update(merged)

        if self._backend_override:
            overrides["backend"] = self._backend_override
        if self._audit_to_stderr is not None:
            overrides["audit_to_stderr"] = self._audit_to_stderr

        if self.base_config is not None:
            return replace(self.base_config, **overrides)
        return AgentConfig(**overrides)

    # ── access ───────────────────────────────────────────────────────

    def __len__(self) -> int:
        return len(self._runtimes)

    @property
    def ids(self) -> List[str]:
        return list(self._runtimes.keys())

    def spec(self, robot_id: str) -> RobotSpec:
        return self.roster.get(robot_id)

    def get(self, robot_id: str) -> Runtime:
        """The runtime for *robot_id*, or raise :class:`UnknownRobot`."""
        self.roster.get(robot_id)  # validates, raises UnknownRobot
        return self._runtimes[robot_id]

    def resolve(self, robot_id: str = "") -> Runtime:
        """Turn an optional id into a runtime, using the roster's default."""
        return self._runtimes[self.roster.resolve(robot_id).robot_id]

    def key_store(self, robot_id: str) -> KeyStore:
        """This robot's per-device key cache (inside its own state dir)."""
        return KeyStore(self.get(robot_id).config.key_cache_path)

    # ── fleet operations ─────────────────────────────────────────────

    async def stop_all(self, *, reason: str = "fleet_stop") -> List[RobotOutcome]:
        """Stop every robot that is moving, and report each one separately.

        A robot whose link is down is reported as ``link="down"`` with
        ``delivered=False`` — *not* silently counted as stopped. An operator
        reading this list must be able to see which dogs are still a risk.
        """
        outcomes = await asyncio.gather(
            *(self._stop_one(rid, reason) for rid in self.ids),
            return_exceptions=True,
        )
        results: List[RobotOutcome] = []
        for robot_id, outcome in zip(self.ids, outcomes):
            if isinstance(outcome, BaseException):
                logger.warning("stop_all: %s raised %r", robot_id, outcome)
                results.append(
                    RobotOutcome(
                        robot_id=robot_id,
                        delivered=False,
                        link="error",
                        detail=f"{type(outcome).__name__}: {outcome}",
                    )
                )
            else:
                results.append(outcome)
        return results

    async def _stop_one(self, robot_id: str, reason: str) -> RobotOutcome:
        runtime = self._runtimes[robot_id]
        session = runtime.session
        if not session.is_connected:
            # Not connected means not moving under our command — but say so
            # rather than implying a stop was delivered.
            return RobotOutcome(
                robot_id=robot_id,
                delivered=False,
                link="down",
                detail="not connected; nothing was sent",
            )
        try:
            delivered = await session.safe_stop(reason=reason)
        except Exception as exc:  # noqa: BLE001 - report, never propagate
            return RobotOutcome(
                robot_id=robot_id,
                delivered=False,
                link="error",
                detail=f"{type(exc).__name__}: {exc}",
            )
        return RobotOutcome(
            robot_id=robot_id,
            delivered=bool(delivered),
            link="up",
            detail="stop delivered" if delivered else "robot did not acknowledge",
        )

    async def status_all(self, *, live: bool = False) -> List[Dict[str, Any]]:
        """One status record per robot.

        ``live`` additionally reads each connected robot's current state,
        concurrently. Keep it off for high-frequency polling: it talks to the
        robots.
        """
        records: List[Dict[str, Any]] = []
        states = [None] * len(self.ids)
        if live:
            states = await asyncio.gather(
                *(self._state_view(rid) for rid in self.ids),
                return_exceptions=True,
            )

        for index, robot_id in enumerate(self.ids):
            runtime = self._runtimes[robot_id]
            spec = self.roster.get(robot_id)
            record: Dict[str, Any] = {
                "robot_id": robot_id,
                "model": spec.model,
                "role": spec.role,
                "connection": spec.connection,
                "target": spec.target,
                "connected": runtime.session.is_connected,
                "session": runtime.session.status(),
            }
            if live:
                state = states[index]
                if isinstance(state, BaseException):
                    record["state_error"] = f"{type(state).__name__}: {state}"
                else:
                    record["state"] = state
            records.append(record)
        return records

    async def _state_view(self, robot_id: str) -> Optional[dict]:
        runtime = self._runtimes[robot_id]
        if not runtime.session.is_connected:
            return None
        return await runtime.session.state_view()

    async def aclose_all(self) -> None:
        """Release every robot. Safe to call twice; never raises."""
        await asyncio.gather(
            *(self._close_one(rid) for rid in self.ids), return_exceptions=True
        )

    async def _close_one(self, robot_id: str) -> None:
        try:
            await self._runtimes[robot_id].aclose()
        except Exception as exc:  # noqa: BLE001
            logger.warning("aclose %s failed: %r", robot_id, exc)

    # ── views ────────────────────────────────────────────────────────

    def redacted(self) -> Dict[str, Any]:
        """Fleet view safe for logs, tools and the console."""
        return {
            "count": len(self),
            "default": self.roster.default_id,
            "robots": [
                {
                    **self.roster.get(rid).redacted(),
                    "state_dir": str(self._runtimes[rid].config.state_dir),
                }
                for rid in self.ids
            ],
        }
