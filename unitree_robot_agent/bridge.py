"""Call a registered tool directly, without an MCP client.

The tool functions are the single implementation of every guarded operation:
argument validation, ``confirm`` requirements, the dangerous-action gate, safe
mode, rate limiting and the audit trail all live there. A second frontend that
reimplemented them would drift — the second implementation would be the one
that forgets a guard.

So the web console drives *these* functions rather than talking to the robot
itself. This module is the small piece of plumbing that makes that possible:
it looks a tool up by name on a built server and calls it.

Used by the CLI's own offline paths and by :mod:`unitree_fleet_web`. It was
previously a private helper in ``cli.py``; it moved here so the console could
share it instead of growing a copy.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional, Set

logger = logging.getLogger("unitree_robot_agent.bridge")


class ToolBridge:
    """Invoke registered tool functions by name."""

    def __init__(self, bundle: Any) -> None:
        self.bundle = bundle

    # ── lookup ───────────────────────────────────────────────────────

    def _manager(self):
        manager = getattr(self.bundle.server, "_tool_manager", None)
        if manager is not None:
            return manager
        try:
            return self.bundle.server.tool_manager
        except Exception:  # noqa: BLE001 - absent on some SDK builds
            return None

    def tool(self, name: str):
        manager = self._manager()
        if manager is None:
            return None
        for getter in ("get_tool", "get"):
            method = getattr(manager, getter, None)
            if method is None:
                continue
            try:
                found = method(name)
            except Exception:  # noqa: BLE001 - try the next accessor
                continue
            if found is not None:
                return found
        return None

    def names(self) -> Set[str]:
        """Every registered tool name."""
        manager = self._manager()
        if manager is None:
            return set(self.bundle.tool_names or [])
        try:
            return {tool.name for tool in manager.list_tools()}
        except Exception:  # noqa: BLE001
            return set(self.bundle.tool_names or [])

    # ── invocation ───────────────────────────────────────────────────

    async def call(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        """Call one tool and return its payload unchanged.

        Deliberately *not* normalised to a dict: `robot_snapshot` returns an
        MCP ``Image``, and wrapping it would break the caller that is supposed
        to render it. A missing tool is reported as ``tool_not_found`` rather
        than raising, because the caller is usually relaying this to a person
        or a model.
        """
        found = self.tool(name)
        if found is None:
            return {
                "ok": False,
                "error": "tool_not_found",
                "message": f"{name} is not registered on this server",
                "hint": "Call robot_roster or list the console's allowed tools.",
            }

        fn = getattr(found, "fn", None) or getattr(found, "func", None)
        if fn is None:
            return {
                "ok": False,
                "error": "tool_not_callable",
                "message": f"{name} has no callable attached",
            }

        result = fn(**(arguments or {}))
        if asyncio.iscoroutine(result):
            result = await result
        return result
