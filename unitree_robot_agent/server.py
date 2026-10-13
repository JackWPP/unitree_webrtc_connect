"""MCP server assembly and transport selection.

Works with **mcp 2.x** (``from mcp.server.mcpserver import MCPServer``) and
falls back to the 1.x layout (``FastMCP``) when that is what is installed —
the two expose the same ``tool`` / ``resource`` / ``prompt`` / ``run``
surface, which is all this module uses.

Transport:

* ``stdio`` (default) — one client, launched by the agent host.
* ``streamable-http`` — several clients share one robot. The session's
  connect lock and idle reaper already make that safe: whoever calls first
  holds the link, and it is released 45 s after the last call so the owner
  can still take over with the official app.
"""

from __future__ import annotations

import contextlib
import json
import logging
from dataclasses import dataclass
from typing import Any, List, Optional

from .config import AgentConfig
from .runtime import Runtime, create_runtime
from .tools import register_all
from .tools.context import ToolContext

logger = logging.getLogger("unitree_robot_agent.server")

SERVER_NAME = "unitree-robot"

INSTRUCTIONS = """\
You control real Unitree robots (Go2 quadruped or R1 humanoid) through a
WebRTC link. Read this before calling anything that moves one.

MORE THAN ONE ROBOT?
Call robot_roster first. If several robots are configured, every tool takes a
`robot_id` argument and omitting it acts on the roster's default robot —
which is usually NOT what you want when the operator said "the other one".
Ids are not model names: two Go2s are "dog1" and "dog2", not "Go2".
- Stop everything: robot_stop_all. Read `not_stopped` — a robot whose link is
  down is listed there and was NOT stopped. Never report the fleet as safe
  without checking that list.
- Both robots accept one WebRTC client each, so a link held by the official
  app or another tool shows up as robot_busy for that robot only.

RULES THAT CANNOT BE TURNED OFF
- Anything that moves a robot needs confirm=true on the call, and the
  duration you pass is clamped to a hard server-side ceiling.
- Each robot only accepts ONE WebRTC client. Links are released
  automatically after ~45 s of inactivity so the owner can take over with
  the official app. Do not try to hold them open.
- Damping is irreversible in the sense that the robot goes limp: a standing
  humanoid WILL FALL. Prefer robot_stop; use robot_emergency_stop (or
  robot_emergency_stop_all) only when the robot is already doing something
  dangerous.
- Never move a robot without knowing what is around it — and with two of them
  in one space, "around it" includes the other robot. Ask the operator if you
  do not.

RECOMMENDED WORKFLOW
1. robot_roster, then robot_fleet_status (or robot_status for one robot) -
   what is connected, what model, is state reporting trustworthy right now?
2. robot_list_actions - what does this chassis actually expose?
3. robot_stand - bring it to a known standing posture.
4. Move or play an action, always with a bounded duration.
5. robot_stop when done, or robot_snapshot to verify visually.

INTERPRETING RESULTS HONESTLY
- R1: asking for Run (811) on an Air chassis gets you Loco20Dof (830). That
  is a SUCCESSFUL, redirected transition - do not retry it.
- R1: states 5/6/7/800/813/814 stop publishing state entirely, so the
  reported state is stale there. "confirmed: false" means exactly that -
  not that the command failed.
- A refusal with code 1001 means the current state will not accept the
  transition (most motions need Lock first). 1003 means the state does not
  exist on this firmware.
- "timed_out": some api_ids have no handler on some firmwares. That is
  silence, not rejection - check the robot physically before retrying.
- unknown_robot means the robot_id is not on the roster. Call robot_roster;
  do not guess an id.

Use robot_get_posture, robot_lowstate and robot_snapshot to ground your
reasoning in what the robot actually reports rather than what you expect.
"""


@dataclass
class ServerBundle:
    server: Any
    context: ToolContext
    tool_names: List[str]
    api: str  # "mcp2" | "mcp1"


def build_server(
    runtime: Optional[Runtime] = None,
    *,
    config: Optional[AgentConfig] = None,
    safe_mode: Optional[bool] = None,
    fleet: Optional[Any] = None,
    fleet_from_env: bool = True,
) -> ServerBundle:
    """Create the MCP server with every applicable tool registered.

    When a roster is configured (``UNITREE_AGENT_ROSTER``) the server drives
    the whole fleet: every tool gains a ``robot_id`` argument and the
    fleet-wide tools become real. Otherwise it is exactly the single-robot
    server it always was, and ``robot_id`` is accepted but optional.

    ``fleet_from_env=False`` makes a caller hermetic — it ignores the ambient
    roster. Self tests and other offline paths need that: without it, a
    developer with a roster in their environment suddenly gets a "self test"
    that talks to real robots.
    """
    rt = runtime or create_runtime(config)
    if safe_mode is not None:
        rt.config.safe_mode = bool(safe_mode)

    if fleet is None and fleet_from_env:
        fleet = _fleet_from_env(rt)
    ctx = ToolContext(
        rt,
        fleet=fleet,
        default_robot_id=(
            fleet.roster.default_id if fleet is not None else rt.config.robot_id
        ),
    )
    server, api = _make_server(rt.config)
    tool_names = register_all(server, ctx)
    _register_resources(server, ctx)
    _register_prompts(server, ctx)

    if rt.config.safe_mode:
        logger.warning(
            "read-only safe mode is ON: movement tools are present but will refuse"
        )
    if ctx.is_fleet:
        logger.info("fleet mode: %s", ", ".join(ctx.fleet.ids))
    return ServerBundle(server=server, context=ctx, tool_names=tool_names, api=api)


def _fleet_from_env(runtime: Runtime):
    """Build a FleetManager when a roster is configured, else ``None``.

    A roster of one is deliberately collapsed to the single-robot path: the
    extra indirection would buy nothing and the fleet-only error messages
    ("robot_id is required") would start firing for a single robot.
    """
    from .errors import FleetConfigError
    from .fleet import FleetManager, load_roster

    try:
        roster = load_roster()
    except FleetConfigError:
        return None
    if len(roster) < 2:
        return None
    models = {spec.model for spec in roster}
    if len(models) > 1:
        # The tool set is chosen from the *default* robot's profile, because
        # capability gating happens at registration time. A mixed fleet is
        # therefore addressable but its model-specific tools follow the
        # default — say so rather than letting it be discovered by a tool
        # mysteriously not existing.
        logger.warning(
            "roster mixes models %s: model-specific tools are registered from "
            "the default robot (%s) only",
            ", ".join(sorted(models)),
            roster.default_id or roster.ids[0],
        )
    logger.info("roster configured: %s", ", ".join(roster.ids))
    return FleetManager(roster)


def _make_server(config: AgentConfig):
    try:
        from mcp.server.mcpserver import MCPServer  # mcp >= 2

        return (
            MCPServer(
                name=SERVER_NAME,
                title="Unitree Robot Control",
                description="Safe, guarded control of a Unitree Go2 / R1 over WebRTC.",
                instructions=INSTRUCTIONS,
                version=_agent_version(),
            ),
            "mcp2",
        )
    except ImportError:
        from mcp.server.fastmcp import FastMCP  # mcp 1.x

        return FastMCP(name=SERVER_NAME, instructions=INSTRUCTIONS), "mcp1"


def _agent_version() -> str:
    with contextlib.suppress(Exception):
        from . import __version__

        return __version__
    return "0"


# ─── resources ────────────────────────────────────────────────────────


def fleet_payload(ctx: ToolContext) -> Dict[str, Any]:
    """The ``unitree://fleet/status`` body, as plain data.

    Split out from the resource handler so it can be asserted directly: the
    MCP resource API needs a request ``Context`` to invoke, which makes the
    handler awkward to exercise in a unit test, and what actually matters is
    the shape and the absence of secrets.
    """
    if ctx.fleet is None:
        return {
            "count": 1,
            "default": ctx.config.robot_id,
            "robots": [
                {
                    "robot_id": ctx.config.robot_id,
                    "model": ctx.profile.name,
                    "connection": ctx.config.connection,
                    "target": ctx.config.target_label(),
                    "connected": ctx.session.is_connected,
                }
            ],
            "note": "One robot is configured; robot_id may be omitted.",
        }

    redacted = ctx.fleet.redacted()
    return {
        "count": redacted["count"],
        "default": redacted["default"],
        "robots": [
            {
                **robot,
                "connected": ctx.fleet.get(robot["robot_id"]).session.is_connected,
            }
            for robot in redacted["robots"]
        ],
    }


def _register_resources(server, ctx: ToolContext) -> None:
    @server.resource(
        "unitree://robot/status",
        name="robot_status",
        description="Live connection + posture snapshot as JSON.",
        mime_type="application/json",
    )
    async def _status() -> str:
        status = ctx.session.status()
        with contextlib.suppress(Exception):
            status["posture"] = await ctx.session.state_view()
        return json.dumps(status, indent=2, ensure_ascii=False, default=str)

    @server.resource(
        "unitree://robot/actions",
        name="robot_actions",
        description="The action catalog for the configured robot, as JSON.",
        mime_type="application/json",
    )
    async def _actions() -> str:
        payload = {
            "robot": ctx.profile.name,
            "capabilities": sorted(ctx.profile.capabilities()),
            "actions": ctx.profile.list_actions(),
            "states": ctx.profile.list_states(),
        }
        return json.dumps(payload, indent=2, ensure_ascii=False, default=str)

    @server.resource(
        "unitree://robot/config",
        name="robot_config",
        description="Effective configuration with secrets redacted.",
        mime_type="application/json",
    )
    async def _config() -> str:
        return json.dumps(ctx.config.redacted(), indent=2, ensure_ascii=False, default=str)

    @server.resource(
        "unitree://fleet/status",
        name="fleet_status",
        description=(
            "Every configured robot, its id, model and connection state, plus "
            "the roster default. Read this instead of guessing a robot_id."
        ),
        mime_type="application/json",
    )
    async def _fleet_status() -> str:
        return json.dumps(fleet_payload(ctx), indent=2, ensure_ascii=False, default=str)


# ─── prompt ───────────────────────────────────────────────────────────


def _register_prompts(server, ctx: ToolContext) -> None:
    @server.prompt(
        name="unitree_safety_briefing",
        description="Pre-flight briefing to run before any session that moves the robot.",
    )
    def _briefing() -> str:
        return (
            "Before moving the robot, confirm all of the following:\n"
            "1. Is the area around the robot clear of people, pets and fragile objects?\n"
            "2. Is the robot on a level, non-slip surface (not a table, not stairs)?\n"
            "3. Is a human watching, who can cut power or press the remote's stop?\n"
            "4. Do you know which state the robot is in (robot_get_posture) and what\n"
            "   the next action requires (robot_list_actions)?\n"
            "5. Is the duration you are about to request the shortest that works?\n"
            "If any answer is no, stop and ask the operator.\n"
        )


# ─── transport ────────────────────────────────────────────────────────


async def serve_async(bundle: ServerBundle) -> None:
    """Run the server on the configured transport until interrupted."""
    config = bundle.context.config
    transport = (config.transport or "stdio").lower().replace("_", "-")
    if transport in ("http", "streamablehttp"):
        transport = "streamable-http"

    try:
        if transport == "stdio":
            await bundle.server.run_stdio_async()
        elif transport == "streamable-http":
            logger.info("serving streamable-http on %s:%s", config.host, config.port)
            await bundle.server.run_streamable_http_async(
                host=config.host, port=config.port
            )
        elif transport == "sse":
            logger.info("serving sse on %s:%s", config.host, config.port)
            await bundle.server.run_sse_async(host=config.host, port=config.port)
        else:
            raise ValueError(f"Unknown transport {config.transport!r}")
    finally:
        await bundle.context.runtime.aclose()


def run_server(bundle: ServerBundle) -> None:
    """Blocking entry point used by the CLI."""
    import anyio

    anyio.run(serve_async, bundle)
