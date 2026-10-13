"""Named actions: humanoid gestures and (for both robots) the action catalog."""

from __future__ import annotations

import asyncio
from typing import Any, Dict

from mcp.types import ToolAnnotations

from ..errors import GuardRejected
from ..robots.base import CAP_ARM_GESTURES
from .context import ToolContext, run_tool

MOTION = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False
)
GENTLE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)


def register(server, ctx: ToolContext) -> None:
    @server.tool(
        name="robot_play_action",
        description=(
            "Play a named action from robot_list_actions: dances, martial arts, "
            "getting up, lying down (R1 FSM states) or Go2 sport moves. Requires "
            "confirm=true; the dangerous ones also require "
            "UNITREE_AGENT_ALLOW_DANGEROUS=true. On the R1, most actions are only "
            "reachable from Lock (4) — call robot_stand first if it is refused "
            "with code 1001."
        ),
        annotations=MOTION,
    )
    async def robot_play_action(name: str, confirm: bool = False, robot_id: str = "") -> Dict[str, Any]:
        args = {"name": name, "confirm": confirm}

        async def _impl():
            guard = ctx.session.guard
            guard.require_motion_enabled("robot_play_action")

            action = ctx.profile.resolve_action(name)
            if action is None:
                available = ", ".join(sorted(a["name"] for a in ctx.profile.list_actions()))
                return {
                    "ok": False,
                    "error": "unknown_action",
                    "message": f"'{name}' is not an action on {ctx.profile.name}.",
                    "hint": f"Available: {available}",
                }

            guard.require_dangerous_allowed(action.name, dangerous=action.dangerous)
            guard.require_confirm(confirm, f"robot_play_action('{action.name}')")
            await ctx.session.ensure_connected()

            if action.kind == "arm":
                return await _run_arm(ctx, action)

            if action.kind == "fsm":
                current = await ctx.session.reported_state()
                check = guard.check_transition(current, action.value, action="robot_play_action")
                if not check.allowed:
                    raise GuardRejected(check.reason, hint=check.hint)
                from unitree_webrtc_connect.constants import LOCO_API, RTC_TOPIC

                response = await ctx.session.call(
                    RTC_TOPIC["SPORT_MOD"], LOCO_API["SET_FSM_ID"], {"data": action.value}
                )
                if not response.ok:
                    raise ctx.session.refusal(
                        response,
                        api_id=LOCO_API["SET_FSM_ID"],
                        topic=RTC_TOPIC["SPORT_MOD"],
                        action=action.name,
                    )
                await asyncio.sleep(1.0)
                transition = (await ctx.state_after(action.value))["transition"]
                return await ctx.result_of(
                    response, action=action.name, kind=action.kind, state=transition
                )

            # Go2 sport command.
            response = await ctx.session.call(action.topic_key, action.value, None)
            if not response.ok:
                raise ctx.session.refusal(
                    response, api_id=action.value, topic=action.topic_key, action=action.name
                )
            return await ctx.result_of(response, action=action.name, kind=action.kind)

        return await run_tool(ctx, "robot_play_action", args, _impl)

    if ctx.profile.has(CAP_ARM_GESTURES):

        @server.tool(
            name="robot_arm_gesture",
            description=(
                "Play one upper-limb gesture (handshake, high five, wave, clap, "
                "hug, ...). Gestures only move the arms, so no confirmation is "
                "required — but keep the arm's reach clear of people and objects. "
                "Use gesture='release' to cancel whatever is running."
            ),
            annotations=GENTLE,
        )
        async def robot_arm_gesture(gesture: str, robot_id: str = "") -> Dict[str, Any]:
            args = {"gesture": gesture}

            async def _impl():
                ctx.session.guard.require_motion_enabled("robot_arm_gesture")
                action = ctx.profile.resolve_action(gesture)
                if action is None or action.kind != "arm":
                    available = ", ".join(
                        sorted(a["name"] for a in ctx.profile.list_actions() if a.get("kind") == "arm")
                    )
                    return {
                        "ok": False,
                        "error": "unknown_gesture",
                        "message": f"'{gesture}' is not an upper-limb gesture on {ctx.profile.name}.",
                        "hint": f"Available gestures: {available}",
                    }
                await ctx.session.ensure_connected()
                return await _run_arm(ctx, action)

            return await run_tool(ctx, "robot_arm_gesture", args, _impl)


async def _run_arm(ctx: ToolContext, action) -> Dict[str, Any]:
    from unitree_webrtc_connect.constants import LOCO_API, RTC_TOPIC

    response = await ctx.session.call(
        RTC_TOPIC["ARM_REQUEST"], LOCO_API["SET_ARM_TASK"], {"data": action.value}
    )
    if not response.ok:
        raise ctx.session.refusal(
            response,
            api_id=LOCO_API["SET_ARM_TASK"],
            topic=RTC_TOPIC["ARM_REQUEST"],
            action=action.name,
        )
    return await ctx.result_of(response, action=action.name, kind="arm")
