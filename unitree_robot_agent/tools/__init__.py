"""MCP tool registration.

Tools are registered conditionally on the robot's capabilities, so a Go2
never sees ``robot_arm_gesture`` and an R1 never sees ``go2_set_led``.
Read-only safe mode only removes tools that move the robot; the read tools
and ``robot_stop`` always stay.

Every tool is wrapped so that its body runs bound to the robot named by its
``robot_id`` argument (empty meaning the default). That is the whole reason
the 31 existing tool bodies did not have to be rewritten: they still say
``ctx.session``, and the wrapper decides *which* session that is.

The alternative — a ``dog1_move`` / ``dog2_move`` prefix per robot — would
multiply the tool list by the fleet size, break the capability gating that
already exists per model, and hand the model a list it cannot reason about.
"""

from __future__ import annotations

import functools
import logging
from typing import Any, Callable

from ..errors import AgentError
from .context import ToolContext

logger = logging.getLogger("unitree_robot_agent.tools")


class _ScopedServer:
    """Proxy around the MCP server that binds each tool to a robot.

    ``functools.wraps`` is what makes this transparent: the MCP SDK builds
    each tool's argument schema from ``inspect.signature``, which follows
    ``__wrapped__``, so the declared parameters — including ``robot_id`` —
    still reach the model exactly as written.
    """

    def __init__(self, server: Any, ctx: ToolContext) -> None:
        self._server = server
        self._ctx = ctx

    def tool(self, *args: Any, **kwargs: Any) -> Callable:
        register = self._server.tool(*args, **kwargs)
        ctx = self._ctx

        def decorator(fn: Callable) -> Callable:
            @functools.wraps(fn)
            async def wrapper(*fn_args: Any, **fn_kwargs: Any) -> Any:
                robot_id = fn_kwargs.get("robot_id", "")
                try:
                    with ctx.scoped(robot_id):
                        return await fn(*fn_args, **fn_kwargs)
                except AgentError as exc:
                    # Resolving the robot happens *outside* `run_tool`'s own
                    # error handling — it has to, since the binding decides
                    # which audit log the record goes to. So the tool boundary
                    # catches here instead, and an unknown robot_id comes back
                    # as the usual {ok: false, hint} payload rather than a
                    # traceback the model cannot act on.
                    return exc.to_dict()

            return register(wrapper)

        return decorator

    def __getattr__(self, name: str) -> Any:
        # Anything else (resources, prompts, run) goes straight through.
        return getattr(self._server, name)


def register_all(server, ctx: ToolContext) -> list:
    """Register every applicable tool. Returns the names registered."""
    from . import actions, common, fleet, go2, locomotion, media

    before = _tool_names(server)
    scoped = _ScopedServer(server, ctx)

    common.register(scoped, ctx)
    locomotion.register(scoped, ctx)
    actions.register(scoped, ctx)
    # Vision is registered unconditionally: even with no robot it can serve a
    # local file or camera, which is how the pipeline is developed offline.
    media.register(scoped, ctx)
    if ctx.profile.name == "Go2":
        go2.register(scoped, ctx)
    # Fleet tools only make sense with more than one robot, but registering
    # `robot_roster` unconditionally means the model can always ask what it is
    # driving rather than inferring it.
    fleet.register(scoped, ctx)

    names = sorted(_tool_names(server) - before)
    logger.info("registered %d tools: %s", len(names), ", ".join(names))
    return names


def _tool_names(server) -> set:
    try:
        return {tool.name for tool in server._tool_manager.list_tools()}
    except Exception:
        pass
    for attribute in ("_tools", "_tool_manager"):
        manager = getattr(server, attribute, None)
        if manager is None:
            continue
        try:
            return set(manager)
        except TypeError:
            continue
    return set()
