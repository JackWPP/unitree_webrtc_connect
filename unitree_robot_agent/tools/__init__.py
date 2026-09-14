"""MCP tool registration.

Tools are registered conditionally on the robot's capabilities, so a Go2
never sees ``robot_arm_gesture`` and an R1 never sees ``go2_set_led``.
Read-only safe mode only removes tools that move the robot; the read tools
and ``robot_stop`` always stay.
"""

from __future__ import annotations

import logging

from .context import ToolContext

logger = logging.getLogger("unitree_robot_agent.tools")


def register_all(server, ctx: ToolContext) -> list:
    """Register every applicable tool. Returns the names registered."""
    from . import actions, common, go2, locomotion, media

    before = _tool_names(server)

    common.register(server, ctx)
    locomotion.register(server, ctx)
    actions.register(server, ctx)
    # Vision is registered unconditionally: even with no robot it can serve a
    # local file or camera, which is how the pipeline is developed offline.
    media.register(server, ctx)
    if ctx.profile.name == "Go2":
        go2.register(server, ctx)

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
