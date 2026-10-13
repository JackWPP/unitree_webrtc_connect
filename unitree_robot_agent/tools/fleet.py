"""Fleet-level tools: who is on the roster, and stopping all of them.

These are the operations that only make sense for more than one robot, and
the two that safety depends on. Everything else stays a per-robot tool with a
``robot_id`` argument.

The important design point is what ``robot_stop_all`` returns. A single
boolean is not good enough: ``safe_stop`` returns False *without touching the
robot* when the link is down, so "all stopped: false" would hide which dog is
still a hazard. The result names them.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

from mcp.types import ToolAnnotations

from .context import ToolContext, run_tool

READ = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
#: Deliberately *not* read-only even though stopping is protective: a fleet
#: stop is a safety action, and the stop tools are the ones safe mode must
#: keep available.
STOP = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
)


def _spec_from_config(ctx: ToolContext):
    """A one-robot spec, so preflight works without a roster."""
    from ..fleet import RobotSpec

    cfg = ctx.config
    return RobotSpec(
        robot_id=cfg.robot_id,
        model=cfg.robot,
        connection=cfg.connection,
        ip=cfg.ip or "",
        serial_number=cfg.serial_number or "",
        aes_128_key=cfg.aes_128_key or "",
        device_type=cfg.device_type or cfg.robot,
        region=cfg.region,
    )


def register(server, ctx: ToolContext) -> None:
    @server.tool(
        name="robot_roster",
        description=(
            "List the robots this server can drive: their id, model, role and "
            "address, and which one is the default. Call this first when more "
            "than one robot is configured — every other tool takes a "
            "robot_id, and this says what the valid values are."
        ),
        annotations=READ,
    )
    async def robot_roster(robot_id: str = "") -> Dict[str, Any]:
        async def _impl():
            if ctx.fleet is None:
                spec = ctx.runtime.config
                return ctx.ok(
                    count=1,
                    default=spec.robot_id,
                    robots=[
                        {
                            "robot_id": spec.robot_id,
                            "model": spec.robot,
                            "connection": spec.connection,
                            "target": spec.target_label(),
                            "connected": ctx.session.is_connected,
                        }
                    ],
                    note=(
                        "One robot is configured; robot_id may be omitted "
                        "everywhere."
                    ),
                )

            robots: List[Dict[str, Any]] = []
            for spec in ctx.fleet.roster:
                runtime = ctx.fleet.get(spec.robot_id)
                robots.append(
                    {
                        **spec.redacted(),
                        "connected": runtime.session.is_connected,
                    }
                )
            return ctx.ok(
                count=len(robots),
                default=ctx.fleet.roster.default_id,
                robots=robots,
            )

        return await run_tool(ctx, "robot_roster", {"robot_id": robot_id}, _impl)

    @server.tool(
        name="robot_preflight",
        description=(
            "Check whether the configured robots can actually be reached and "
            "what their handshake will need — WITHOUT connecting to them. "
            "Reports, per robot: whether the signaling port answers, whether "
            "the firmware needs a per-device AES-128 key (data2=3), whether we "
            "hold a key, and whether that key is accepted. Read-only: it "
            "starts the handshake (con_notify) but never sends an SDP, so the "
            "robot's single WebRTC client slot is not claimed and you can run "
            "it while the official app is connected. Call this first when a "
            "connect fails, or before pointing the fleet at real hardware."
        ),
        annotations=READ,
    )
    async def robot_preflight(robot_id: str = "") -> Dict[str, Any]:
        async def _impl():
            from ..fleet import run_preflight, summarise

            if ctx.fleet is None:
                spec = ctx.runtime.config
                from ..fleet import Roster

                roster = Roster.single(
                    _spec_from_config(ctx), state_dir=ctx.config.state_dir.parent
                )
                results = await run_preflight(roster)
                payload = summarise(results)
            else:
                payload = summarise(await run_preflight(ctx.fleet.roster))

            return ctx.ok(**payload)

        return await run_tool(ctx, "robot_preflight", {"robot_id": robot_id}, _impl)

    @server.tool(
        name="robot_fleet_status",
        description=(
            "Status of every robot at once: connected or not, session counters, "
            "and optionally each robot's live state. Use this instead of "
            "calling robot_status once per robot. `live=true` talks to the "
            "robots, so leave it off when polling."
        ),
        annotations=READ,
    )
    async def robot_fleet_status(
        live: bool = False, robot_id: str = ""
    ) -> Dict[str, Any]:
        args = {"live": live, "robot_id": robot_id}

        async def _impl():
            if ctx.fleet is None:
                record = {
                    "robot_id": ctx.config.robot_id,
                    "model": ctx.profile.name,
                    "connected": ctx.session.is_connected,
                    "session": ctx.session.status(),
                }
                if live and ctx.session.is_connected:
                    record["state"] = await ctx.session.state_view()
                return ctx.ok(count=1, robots=[record])

            records = await ctx.fleet.status_all(live=live)
            online = [r["robot_id"] for r in records if r["connected"]]
            return ctx.ok(
                count=len(records),
                online=online,
                offline=[r["robot_id"] for r in records if not r["connected"]],
                robots=records,
            )

        return await run_tool(ctx, "robot_fleet_status", args, _impl)

    @server.tool(
        name="robot_stop_all",
        description=(
            "Stop every robot that is moving, keeping each one's posture. "
            "Returns a per-robot outcome plus a `not_stopped` list: a robot "
            "whose link is down is reported there rather than counted as "
            "stopped, so check that list before assuming the fleet is safe. "
            "Always available, including in safe mode."
        ),
        annotations=STOP,
    )
    async def robot_stop_all(robot_id: str = "") -> Dict[str, Any]:
        async def _impl():
            if ctx.fleet is None:
                delivered = await ctx.session.safe_stop(reason="tool:robot_stop_all")
                return ctx.ok(
                    outcomes=[
                        {
                            "robot_id": ctx.config.robot_id,
                            "ok": bool(delivered),
                            "delivered": bool(delivered),
                            "link": "up" if ctx.session.is_connected else "down",
                        }
                    ],
                    stopped=[ctx.config.robot_id] if delivered else [],
                    not_stopped=[] if delivered else [ctx.config.robot_id],
                    complete=bool(delivered),
                )

            outcomes = [
                outcome.as_dict()
                for outcome in await ctx.fleet.stop_all(reason="tool:robot_stop_all")
            ]
            stopped = [o["robot_id"] for o in outcomes if o["ok"]]
            not_stopped = [o["robot_id"] for o in outcomes if not o["ok"]]
            return ctx.ok(
                outcomes=outcomes,
                stopped=stopped,
                not_stopped=not_stopped,
                complete=not not_stopped,
                note=(
                    ""
                    if not not_stopped
                    else "These robots did NOT confirm a stop — verify them by "
                    "hand: " + ", ".join(not_stopped)
                ),
            )

        return await run_tool(ctx, "robot_stop_all", {"robot_id": robot_id}, _impl)

    @server.tool(
        name="robot_emergency_stop_all",
        description=(
            "Damp every connected robot at once, immediately. This makes them "
            "go limp and they may drop — use robot_stop_all to stop moving "
            "while keeping posture. Robots that could not be reached are named "
            "in `not_damped`; they were NOT stopped."
        ),
        annotations=STOP,
    )
    async def robot_emergency_stop_all(
        confirm: bool = False, reason: str = "tool:robot_emergency_stop_all"
    ) -> Dict[str, Any]:
        args = {"confirm": confirm, "reason": reason}

        async def _impl():
            if not confirm:
                return {
                    "ok": False,
                    "error": "confirm_required",
                    "message": "robot_emergency_stop_all damps every robot.",
                    "hint": "Re-issue with confirm=true.",
                }

            if ctx.fleet is None:
                targets = [(ctx.config.robot_id, ctx.runtime)]
            else:
                targets = [
                    (rid, ctx.fleet.get(rid)) for rid in ctx.fleet.ids
                ]

            async def damp(robot_id: str, runtime) -> Dict[str, Any]:
                if not runtime.session.is_connected:
                    return {
                        "robot_id": robot_id,
                        "ok": False,
                        "link": "down",
                        "detail": "not connected; damp was not sent",
                    }
                try:
                    delivered = await runtime.session.emergency_stop(reason=reason)
                except Exception as exc:  # noqa: BLE001 - report, never propagate
                    return {
                        "robot_id": robot_id,
                        "ok": False,
                        "link": "error",
                        "detail": f"{type(exc).__name__}: {exc}",
                    }
                return {
                    "robot_id": robot_id,
                    "ok": bool(delivered),
                    "link": "up",
                    "detail": "" if delivered else "robot did not acknowledge damp",
                }

            outcomes = await asyncio.gather(
                *(damp(rid, rt) for rid, rt in targets), return_exceptions=True
            )
            results = []
            for (rid, _), outcome in zip(targets, outcomes):
                if isinstance(outcome, BaseException):
                    results.append(
                        {
                            "robot_id": rid,
                            "ok": False,
                            "link": "error",
                            "detail": f"{type(outcome).__name__}: {outcome}",
                        }
                    )
                else:
                    results.append(outcome)

            not_damped = [o["robot_id"] for o in results if not o["ok"]]
            return ctx.ok(
                outcomes=results,
                damped=[o["robot_id"] for o in results if o["ok"]],
                not_damped=not_damped,
                complete=not not_damped,
                note=(
                    ""
                    if not not_damped
                    else "These robots were NOT damped — they may still be "
                    "moving: " + ", ".join(not_damped)
                ),
            )

        return await run_tool(ctx, "robot_emergency_stop_all", args, _impl)
