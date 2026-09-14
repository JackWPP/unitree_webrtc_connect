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

import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

from ..errors import AgentError
from ..runtime import Runtime

logger = logging.getLogger("unitree_robot_agent.tools")


@dataclass
class ToolContext:
    """Everything a tool needs: the runtime plus small helpers."""

    runtime: Runtime

    @property
    def config(self):
        return self.runtime.config

    @property
    def profile(self):
        return self.runtime.profile

    @property
    def session(self):
        return self.runtime.session

    @property
    def audit(self):
        return self.runtime.audit

    @property
    def backend(self):
        return self.runtime.backend

    # ── response helpers ─────────────────────────────────────────────

    def ok(self, **payload) -> Dict[str, Any]:
        out: Dict[str, Any] = {"ok": True, "robot": self.profile.name}
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
