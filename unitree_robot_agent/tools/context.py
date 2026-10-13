"""Shared plumbing for the MCP tools.

Two conventions every tool follows:

* **Errors are returned, not raised.** An LLM handles
  ``{"ok": false, "error": "robot_busy", "hint": "close unitree_ui"}`` far
  better than an exception traceback, and the MCP call still succeeds so
  the model can reason about it.
* **Every call is audited.** :func:`run_tool` writes one record per
  invocation with the arguments (redacted), the outcome and the robot's
  status code when there is one.
"""

from __future__ import annotations

import contextlib
import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Iterator, Optional

from ..errors import AgentError, UnknownRobot
from ..runtime import Runtime

logger = logging.getLogger("unitree_robot_agent.tools")

#: The robot the current tool body is acting on.
#:
#: A context variable rather than a field, so the 31 existing tool bodies can
#: keep writing ``ctx.session`` unchanged while still being addressable per
#: robot. It is task-local, so two concurrent tool calls aimed at different
#: robots cannot see each other's binding.
_ACTIVE: ContextVar[Optional[Runtime]] = ContextVar("unitree_active_runtime", default=None)


@dataclass
class ToolContext:
    """Everything a tool needs: the runtime(s) plus small helpers.

    Two shapes are supported by the same object. A single-robot run passes one
    :class:`Runtime` and nothing else, exactly as before. A fleet passes the
    default robot's runtime *and* a :class:`FleetManager`; tools then reach a
    specific robot by declaring a ``robot_id`` argument, which the registration
    wrapper turns into the active binding.
    """

    runtime: Runtime
    #: Present only when more than one robot is configured.
    fleet: Optional[Any] = None
    default_robot_id: str = ""

    # ── robot resolution ─────────────────────────────────────────────

    @property
    def active(self) -> Runtime:
        """The runtime the current tool body is bound to."""
        return _ACTIVE.get() or self.runtime

    @property
    def is_fleet(self) -> bool:
        return self.fleet is not None and len(self.fleet) > 1

    def robot(self, robot_id: str = "") -> Runtime:
        """Resolve an optional ``robot_id`` to a runtime.

        Without a fleet, only this robot exists — naming it, or naming
        nothing, both work; naming a *different* robot is an error rather than
        a silent fallback to the wrong dog.
        """
        if self.fleet is None:
            if robot_id and robot_id not in ("", self.default_robot_id,
                                             self.runtime.config.robot_id):
                raise UnknownRobot(
                    f"No robot {robot_id!r}: this server drives "
                    f"{self.runtime.config.robot_id!r} only.",
                    detail={"robot_id": robot_id},
                )
            return self.runtime
        return self.fleet.resolve(robot_id)

    @contextlib.contextmanager
    def scoped(self, robot_id: str = "") -> Iterator[Runtime]:
        """Bind the tool body to one robot for the duration of the block."""
        runtime = self.robot(robot_id)
        token = _ACTIVE.set(runtime)
        try:
            yield runtime
        finally:
            _ACTIVE.reset(token)

    # ── delegates (all follow the active binding) ────────────────────

    @property
    def config(self):
        return self.active.config

    @property
    def profile(self):
        return self.active.profile

    @property
    def session(self):
        return self.active.session

    @property
    def audit(self):
        return self.active.audit

    @property
    def backend(self):
        return self.active.backend

    # ── the rest of the runtime, bound the same way ──────────────────
    #
    # **These are not conveniences.** `ctx.runtime` is the *default* robot's
    # runtime — captured when the tools were registered and never rebound — so a
    # tool body that reaches for `ctx.runtime.motion` drives the default dog no
    # matter which `robot_id` it was called with. The response is built by
    # `ok()`, which uses `self.config.robot_id` and therefore follows the
    # binding, so **the call reports the robot the operator clicked while acting
    # on a different one**:
    #
    #     call_tool("dog2", "robot_walk", {"meters": 0.15})
    #       -> {"ok": true, "robot_id": "dog2", "reason": "reached"}
    #     dog1 position: (0.149, 0.0)      <- it walked
    #     dog2 position: (0.0, 0.0)        <- it did not
    #
    # Measured. Everything below had the same defect, because `ctx.session` was
    # the only runtime attribute that followed the binding.

    @property
    def motion(self):
        return self.active.motion

    @property
    def faults(self):
        return self.active.faults

    @property
    def auth(self):
        return self.active.auth

    @property
    def vision(self):
        return self.active.vision

    # ── response helpers ─────────────────────────────────────────────

    def ok(self, **payload) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "ok": True,
            "robot": self.profile.name,
            "robot_id": self.config.robot_id,
        }
        out.update(payload)
        return out

    async def result_of(self, response, **payload) -> Dict[str, Any]:
        """Build a standard success payload for an ``ApiResponse``."""
        out = self.ok(code=response.code, **payload)
        if response.timed_out:
            out["confirmed"] = False
            out["note"] = (
                "The robot did not answer within the timeout. Some api_ids "
                "have no handler on some firmwares (that is silence, not a "
                "refusal) — verify the robot's physical state before retrying."
            )
        return out

    async def state_after(self, requested_id: Optional[int] = None) -> dict:
        """Read the state back and describe the outcome honestly."""
        reported = await self.session.reported_state()
        if requested_id is None:
            return {"state": self.profile.classify(reported)}
        return {"state": self.profile.classify(reported), "transition": self.profile.normalize(requested_id, reported)}


async def run_tool(
    ctx: ToolContext,
    name: str,
    args: Dict[str, Any],
    fn: Callable[[], Awaitable[Any]],
) -> Any:
    """Execute ``fn`` with auditing and uniform error reporting."""
    started = time.monotonic()
    try:
        result = await fn()
    except AgentError as exc:
        ctx.audit.record(
            name,
            args=args,
            ok=False,
            error=exc.code,
            detail=exc.detail,
            duration_ms=(time.monotonic() - started) * 1000,
            robot=ctx.profile.name,
        )
        return exc.to_dict()
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("tool %s crashed", name)
        ctx.audit.record(
            name,
            args=args,
            ok=False,
            error="internal_error",
            detail={"type": exc.__class__.__name__, "message": str(exc)[:200]},
            duration_ms=(time.monotonic() - started) * 1000,
            robot=ctx.profile.name,
        )
        return {
            "ok": False,
            "error": "internal_error",
            "message": str(exc) or exc.__class__.__name__,
            "hint": "This is a bug in unitree_robot_agent; the audit log has the details.",
        }

    code = result.get("code") if isinstance(result, dict) else None
    ctx.audit.record(
        name,
        args=args,
        ok=bool(result.get("ok", True)) if isinstance(result, dict) else True,
        code=code if isinstance(code, int) else None,
        detail=_audit_detail(result),
        duration_ms=(time.monotonic() - started) * 1000,
        robot=ctx.profile.name,
    )
    return result


def _audit_detail(result: Any) -> Any:
    """Keep audit records small: only a few interesting keys."""
    if not isinstance(result, dict):
        return None
    keys = ("action", "state", "kind", "duration_s", "velocity", "note", "confirmed")
    detail = {k: result[k] for k in keys if k in result}
    return detail or None
