"""Movement, posture and stop tools — the ones that can actually hurt someone.

Every tool here is wrapped in :class:`~unitree_robot_agent.core.guards.Guard`:

* movement needs ``confirm=true`` and a bounded ``duration_s``,
* the duration ceiling is applied locally, not trusted to the model,
* the session is *held* for the duration so the idle reaper cannot cut the
  connection mid-walk,
* any failure path ends in a soft stop (never a silent return).
"""

from __future__ import annotations

import asyncio
import contextlib
import math
from typing import Any, Dict

from mcp.types import ToolAnnotations

from ..errors import GuardRejected
from ..robots.base import CAP_FSM_ACTIONS, CAP_VELOCITY
from .context import ToolContext, run_tool

MOTION = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False
)
STOP = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

#: Normalised velocity ceiling. The robot scales these by its own limit
#: (1.0 forward by default), so 1.0 means "as fast as the robot allows".
SPEED_LIMIT = 1.0

#: Furthest a single ``robot_walk`` may be asked to travel, in metres.
#:
#: **A named constant rather than a literal in the body** so the console can
#: offer a range that matches what the tool actually accepts. It was `5.0`
#: inline, which meant the only way to find it was to read this file — and a
#: parameter panel that guesses a range is one that offers values the tool will
#: reject.
MAX_WALK_M = 5.0

#: Largest single ``robot_turn``, in degrees, in either direction. Named for the
#: same reason as ``MAX_WALK_M``.
MAX_TURN_DEG = 1080.0


def register(server, ctx: ToolContext) -> None:
    # ── stop ─────────────────────────────────────────────────────────

    @server.tool(
        name="robot_stop",
        description=(
            "Stop moving immediately, keeping the current posture. Always "
            "available, needs no confirmation. This is the correct response to "
            "any doubt: it does NOT damp, so a standing humanoid stays standing."
        ),
        annotations=STOP,
    )
    async def robot_stop(robot_id: str = "") -> Dict[str, Any]:
        async def _impl():
            stopped = await ctx.session.safe_stop(reason="tool:robot_stop")
            if not stopped:
                return {
                    "ok": False,
                    "error": "stop_not_delivered",
                    "stopped": False,
                    "mode": "soft_stop",
                    "message": "The stop was NOT delivered to the robot.",
                    "hint": (
                        "Check the link with robot_status. If the robot may still be "
                        "moving, use the physical remote or the power switch."
                    ),
                }
            return ctx.ok(stopped=True, mode="soft_stop", note="Motion zeroed; posture unchanged.")

        return await run_tool(ctx, "robot_stop", {}, _impl)

    @server.tool(
        name="robot_emergency_stop",
        description=(
            "DAMP the robot (R1 state 1 / Go2 Damp 1001). This is the strongest "
            "stop: the robot goes limp and a standing humanoid WILL DROP. Use "
            "robot_stop unless the robot is doing something dangerous."
        ),
        annotations=MOTION,
    )
    async def robot_emergency_stop(confirm: bool = False, robot_id: str = "") -> Dict[str, Any]:
        args = {"confirm": confirm}

        async def _impl():
            ctx.session.guard.require_motion_enabled("robot_emergency_stop")
            ctx.session.guard.require_confirm(
                confirm, "robot_emergency_stop (damping — the robot may fall)"
            )
            ok = await ctx.session.emergency_stop(reason="tool:robot_emergency_stop")
            return ctx.ok(
                stopped=ok,
                mode="damping",
                warning="Damping removes motor torque; a standing robot will fall.",
            )

        return await run_tool(ctx, "robot_emergency_stop", args, _impl)

    # ── posture ──────────────────────────────────────────────────────

    @server.tool(
        name="robot_stand",
        description=(
            "Bring the robot into its normal standing/ready posture: R1 -> Lock "
            "(4), Go2 -> BalanceStand (1002). On the R1 this is also the hub you "
            "must pass through before most motions."
        ),
        annotations=MOTION,
    )
    async def robot_stand(confirm: bool = False, robot_id: str = "") -> Dict[str, Any]:
        args = {"confirm": confirm}

        async def _impl():
            ctx.session.guard.require_motion_enabled("robot_stand")
            ctx.session.guard.require_confirm(confirm, "robot_stand")
            await ctx.session.ensure_connected()

            if ctx.profile.name == "R1":
                from unitree_webrtc_connect.constants import LOCO_API, R1_FSM, RTC_TOPIC

                target = R1_FSM["Lock"]
                response = await ctx.session.call(
                    RTC_TOPIC["SPORT_MOD"], LOCO_API["SET_FSM_ID"], {"data": target}
                )
                if not response.ok:
                    raise ctx.session.refusal(
                        response, api_id=LOCO_API["SET_FSM_ID"], topic=RTC_TOPIC["SPORT_MOD"], action="robot_stand"
                    )
                await asyncio.sleep(1.0)
                return await ctx.result_of(
                    response, action="stand", state=(await ctx.state_after(target))["transition"]
                )

            topic, api_id, parameter = ctx.profile.stand_command()
            response = await ctx.session.call(topic, api_id, parameter)
            if not response.ok:
                raise ctx.session.refusal(response, api_id=api_id, topic=topic, action="robot_stand")
            await asyncio.sleep(1.0)
            return await ctx.result_of(response, action="stand")

        return await run_tool(ctx, "robot_stand", args, _impl)

    @server.tool(
        name="robot_lie_down",
        description=(
            "Put the robot down on the ground: R1 -> LieDown (702), Go2 -> "
            "StandDown (1005). The robot ends up lying down — that is intended, "
            "not a failure."
        ),
        annotations=MOTION,
    )
    async def robot_lie_down(confirm: bool = False, robot_id: str = "") -> Dict[str, Any]:
        args = {"confirm": confirm}

        async def _impl():
            ctx.session.guard.require_motion_enabled("robot_lie_down")
            ctx.session.guard.require_confirm(confirm, "robot_lie_down")
            ctx.session.guard.require_dangerous_allowed("lie_down", dangerous=True)
            await ctx.session.ensure_connected()

            if ctx.profile.name == "R1":
                from unitree_webrtc_connect.constants import LOCO_API, R1_FSM, RTC_TOPIC

                target = R1_FSM["LieDown"]
                check = ctx.session.guard.check_transition(
                    await ctx.session.reported_state(), target, action="robot_lie_down"
                )
                if not check.allowed:
                    raise GuardRejected(check.reason, hint=check.hint)
                response = await ctx.session.call(
                    RTC_TOPIC["SPORT_MOD"], LOCO_API["SET_FSM_ID"], {"data": target}
                )
                if not response.ok:
                    raise ctx.session.refusal(
                        response, api_id=LOCO_API["SET_FSM_ID"], topic=RTC_TOPIC["SPORT_MOD"], action="robot_lie_down"
                    )
                await asyncio.sleep(1.5)
                return await ctx.result_of(
                    response, action="lie_down", state=(await ctx.state_after(target))["transition"]
                )

            topic, api_id, parameter = ctx.profile.lie_command()
            response = await ctx.session.call(topic, api_id, parameter)
            if not response.ok:
                raise ctx.session.refusal(response, api_id=api_id, topic=topic, action="robot_lie_down")
            return await ctx.result_of(response, action="lie_down")

        return await run_tool(ctx, "robot_lie_down", args, _impl)

    # ── movement ─────────────────────────────────────────────────────

    @server.tool(
        name="robot_move",
        description=(
            "Move for a bounded time. vx (forward) / vy (left) / omega (turn); "
            "positive omega turns left. duration_s is clamped to the server's "
            "hard limit. The call returns once the movement window has elapsed "
            "and the robot has been told to stop. "
            "**UNITS ARE UNRESOLVED.** This sends Unitree's sport `Move` (1008), "
            "whose SDK signature takes metres per second and radians per second, "
            "while the WebRTC app protocol is widely described as taking a "
            "normalised stick. Both readings agree at 0 and differ by the "
            "robot's top speed — about 3.5x on a Go2 — and nothing in this "
            "repository establishes which applies over the data channel. Values "
            "are clamped to 1.0, which is safe either way (1 m/s, or full "
            "speed). **If your distance matters, use robot_walk**, which drives "
            "the joystick path and closes its loop on odometry. To settle it: "
            "`robot_move(vx=1.0, duration_s=2)` travels about 2 m if these are "
            "m/s and about 6-7 m if normalised."
        ),
        annotations=MOTION,
    )
    async def robot_move(
        vx: float = 0.0,
        vy: float = 0.0,
        omega: float = 0.0,
        duration_s: float = 1.0,
        confirm: bool = False,
        robot_id: str = "",
    ) -> Dict[str, Any]:
        args = {"vx": vx, "vy": vy, "omega": omega, "duration_s": duration_s, "confirm": confirm}

        async def _impl():
            guard = ctx.session.guard
            guard.require_motion_enabled("robot_move")
            guard.require_confirm(confirm, "robot_move")

            duration, clamp_note = guard.clamp_duration(duration_s, default=1.0)
            speed_x, note_x = guard.clamp_speed(vx, axis="vx", limit=SPEED_LIMIT)
            speed_y, note_y = guard.clamp_speed(vy, axis="vy", limit=SPEED_LIMIT)
            turn, note_w = guard.clamp_speed(omega, axis="omega", limit=SPEED_LIMIT)
            notes = [n for n in (clamp_note, note_x, note_y, note_w) if n]

            await ctx.session.ensure_connected()

            if ctx.profile.has(CAP_VELOCITY) and ctx.profile.name == "R1":
                result = await _r1_move(ctx, speed_x, speed_y, turn, duration, notes)
            else:
                result = await _go2_move(ctx, speed_x, speed_y, turn, duration, notes)
            return result

        return await run_tool(ctx, "robot_move", args, _impl)

    @server.tool(
        name="robot_joystick",
        description=(
            "Drive with a normalised virtual joystick for a bounded time. Axes "
            "are the raw stick values the robot maps as [ly, -lx, -rx] -> "
            "[vx, vy, omega], each in [-1, 1]: ly = forward, lx = right, "
            "rx = clockwise (turn right); pass a negative rx to turn left, or "
            "use robot_turn for an exact angle. Published continuously at 20 Hz "
            "with a ramp so the robot eases in and out, and zeroed before "
            "returning. On the Go2 this path is intercepted by the "
            "obstacle-avoidance service — prefer it when avoidance is enabled."
        ),
        annotations=MOTION,
    )
    async def robot_joystick(
        lx: float = 0.0,
        ly: float = 0.0,
        rx: float = 0.0,
        duration_s: float = 1.0,
        confirm: bool = False,
        robot_id: str = "",
    ) -> Dict[str, Any]:
        args = {"lx": lx, "ly": ly, "rx": rx, "duration_s": duration_s, "confirm": confirm}

        async def _impl():
            guard = ctx.session.guard
            guard.require_motion_enabled("robot_joystick")
            guard.require_confirm(confirm, "robot_joystick")

            duration, clamp_note = guard.clamp_duration(duration_s, default=1.0)
            notes = [clamp_note] if clamp_note else []
            speed_lx, n1 = guard.clamp_speed(lx, axis="lx", limit=SPEED_LIMIT)
            speed_ly, n2 = guard.clamp_speed(ly, axis="ly", limit=SPEED_LIMIT)
            speed_rx, n3 = guard.clamp_speed(rx, axis="rx", limit=SPEED_LIMIT)
            notes.extend(n for n in (n1, n2, n3) if n)

            await ctx.session.ensure_connected()
            stats = await _drive_joystick(
                ctx, lx=speed_lx, ly=speed_ly, rx=speed_rx, duration=duration
            )

            return ctx.ok(
                action="joystick",
                duration_s=round(duration, 2),
                command={"lx": speed_lx, "ly": speed_ly, "rx": speed_rx},
                stopped=True,
                aborted=stats.get("aborted", False),
                profile=stats.get("profile"),
                frames=stats.get("frames"),
                notes=notes,
            )

        return await run_tool(ctx, "robot_joystick", args, _impl)

    @server.tool(
        name="robot_turn",
        description=(
            "Rotate by an angle using the IMU as feedback: positive degrees = "
            "counter-clockwise (left), negative = clockwise (right). Unlike a "
            "timed joystick turn this stops when the robot has actually "
            "rotated that far, so it does not depend on guessing a rate. "
            "Returns the measured turn and the residual error — read "
            "residual_deg rather than assuming the turn was exact."
        ),
        annotations=MOTION,
    )
    async def robot_turn(
        degrees: float,
        speed: float = 0.0,
        confirm: bool = False,
        robot_id: str = "",
    ) -> Dict[str, Any]:
        args = {"degrees": degrees, "speed": speed, "confirm": confirm}

        async def _impl():
            guard = ctx.session.guard
            guard.require_motion_enabled("robot_turn")

            try:
                target = float(degrees)
            except (TypeError, ValueError):
                raise GuardRejected(
                    f"degrees must be a number, got {degrees!r}.",
                    detail={"requested": degrees},
                )
            if math.isnan(target):
                raise GuardRejected(
                    f"degrees must be a finite number, got {degrees!r}.",
                    detail={"requested": degrees},
                )

            # A zero turn moves nothing, so it needs no confirmation.
            if abs(target) < 1e-6:
                return ctx.ok(action="turn", skipped=True, note="0° is a no-op; nothing was sent.")

            guard.require_confirm(
                confirm, f"robot_turn({target:g}° — the robot will rotate in place)"
            )
            if abs(target) > MAX_TURN_DEG:
                raise GuardRejected(
                    f"{target:g}° is beyond the hard limit (±{MAX_TURN_DEG:g}°). "
                    f"Turn in smaller steps.",
                    detail={"requested": target, "limit": MAX_TURN_DEG},
                )

            await ctx.session.ensure_connected()
            peak = abs(float(speed or 0.0)) or None
            if peak is not None and math.isnan(peak):
                raise GuardRejected(
                    f"speed must be a finite number, got {speed!r}.",
                    detail={"requested": speed},
                )
            if peak is not None:
                peak = min(peak, SPEED_LIMIT)

            result = await ctx.motion.turn(
                target, speed=peak, timeout=guard.config.max_turn_seconds
            )
            payload = dict(result)
            payload["action"] = "turn"
            # `setdefault`: motion.turn may already carry its own note.
            payload.setdefault(
                "note",
                "Closed-loop on the torso IMU yaw. 'turned_deg' is what the robot "
                "reports having rotated; 'residual_deg' is what is left over.",
            )
            return ctx.ok(**payload)

        return await run_tool(ctx, "robot_turn", args, _impl)

    @server.tool(
        name="robot_walk",
        description=(
            "Walk a measured distance forward (negative for backward), closing "
            "the loop on the robot's own odometry — it stops when it has gone "
            "that far rather than after a guessed time. Use this instead of "
            "robot_move when the distance matters: a routine that walks a square "
            "accumulates the error of every timed step, and this one does not. "
            "Reports 'actual_m' and 'residual_m' rather than claiming success; "
            "when the robot publishes no odometry it falls back to a timed move "
            "and says so in 'reason'."
        ),
        annotations=MOTION,
    )
    async def robot_walk(
        meters: float,
        speed: float = 0.0,
        confirm: bool = False,
        robot_id: str = "",
    ) -> Dict[str, Any]:
        args = {"meters": meters, "speed": speed, "confirm": confirm}

        async def _impl():
            guard = ctx.session.guard
            guard.require_motion_enabled("robot_walk")

            try:
                target = float(meters)
            except (TypeError, ValueError):
                raise GuardRejected(f"meters must be a number, got {meters!r}.")
            if not math.isfinite(target):
                raise GuardRejected(f"meters must be finite, got {meters!r}.")
            if abs(target) < 1e-3:
                return ctx.ok(action="walk", skipped=True,
                              note="0 m is a no-op; nothing was sent.")
            if abs(target) > MAX_WALK_M:
                raise GuardRejected(
                    f"{target:g} m is beyond the ±{MAX_WALK_M:g} m limit. "
                    f"Walk in smaller legs.",
                    detail={"requested": target, "limit": MAX_WALK_M},
                )

            guard.require_confirm(confirm, f"robot_walk({target:g} m)")
            await ctx.session.ensure_connected()

            peak = abs(float(speed or 0.0)) or None
            result = await ctx.motion.forward_m(target, speed=peak)
            payload = dict(result)
            payload["action"] = "walk"
            return ctx.ok(**payload)

        return await run_tool(ctx, "robot_walk", args, _impl)

    # ── state machine (R1 / G1 only) ─────────────────────────────────

    if ctx.profile.has(CAP_FSM_ACTIONS):

        @server.tool(
            name="robot_switch_state",
            description=(
                "Switch the humanoid's FSM state by semantic name (Lock, Damping, "
                "Run, Dance1, StandUp, LieDown, ...). Use robot_list_actions for "
                "the full table. Damping and Lock need no confirmation; everything "
                "else moves the robot and does. On an R1 Air, Run is redirected by "
                "the firmware to Loco20Dof (830) — that is reported as a "
                "successful, redirected transition, not a failure."
            ),
            annotations=MOTION,
        )
        async def robot_switch_state(state: str, confirm: bool = False, robot_id: str = "") -> Dict[str, Any]:
            args = {"state": state, "confirm": confirm}

            async def _impl():
                guard = ctx.session.guard
                guard.require_motion_enabled("robot_switch_state")

                info = _resolve_state(ctx, state)
                if info is None:
                    return {
                        "ok": False,
                        "error": "unknown_state",
                        "message": f"'{state}' is not a known state on {ctx.profile.name}.",
                        "hint": "Call robot_list_actions and pick a name from the states list.",
                    }

                safe_pair = info.name in ("Damping", "Lock")
                if not safe_pair:
                    guard.require_confirm(confirm, f"robot_switch_state('{info.name}')")
                    guard.require_dangerous_allowed(info.name, dangerous=info.dangerous)

                await ctx.session.ensure_connected()
                current = await ctx.session.reported_state()
                check = guard.check_transition(current, info.fsm_id, action="robot_switch_state")
                if not check.allowed:
                    raise GuardRejected(check.reason, hint=check.hint)

                from unitree_webrtc_connect.constants import LOCO_API, RTC_TOPIC

                response = await ctx.session.call(
                    RTC_TOPIC["SPORT_MOD"], LOCO_API["SET_FSM_ID"], {"data": info.fsm_id}
                )
                if not response.ok:
                    raise ctx.session.refusal(
                        response,
                        api_id=LOCO_API["SET_FSM_ID"],
                        topic=RTC_TOPIC["SPORT_MOD"],
                        action=f"switch_state({info.name})",
                    )
                await asyncio.sleep(1.0)
                transition = (await ctx.state_after(info.fsm_id))["transition"]
                return await ctx.result_of(
                    response, action="switch_state", state=transition
                )

            return await run_tool(ctx, "robot_switch_state", args, _impl)


# ─── helpers ──────────────────────────────────────────────────────────


async def _drive_joystick(
    ctx: ToolContext, *, lx: float, ly: float, rx: float, duration: float
) -> dict:
    """Publish wireless-controller frames following a trapezoid profile.

    The robot consumes this topic as ``[ly, -lx, -rx] -> [vx, vy, omega]``
    (matching its own ``update_state()``), so callers pass raw joystick axes.
    The controller zeroes the sticks in ``finally``, so a failure can never
    leave the robot walking.
    """
    return await ctx.motion.drive(lx=lx, ly=ly, rx=rx, duration=duration)


def _resolve_state(ctx: ToolContext, name: str):
    from unitree_webrtc_connect.constants import R1_FSM

    target = (name or "").strip().lower().replace(" ", "").replace("_", "")
    for state_name, fsm_id in R1_FSM.items():
        if target == state_name.lower().replace("_", ""):
            return ctx.profile.states().get(fsm_id)
    for info in ctx.profile.states().values():
        for alias in info.aliases:
            if target == alias.lower().replace("_", ""):
                return info
    return None


async def _r1_move(ctx: ToolContext, vx: float, vy: float, omega: float, duration: float, notes) -> dict:
    from unitree_webrtc_connect.constants import LOCO_API, RTC_TOPIC

    await ctx.session.ensure_connected()
    current = await ctx.session.reported_state()
    state = ctx.profile.classify(current)

    async with ctx.session.hold("r1_move"):
        response = await ctx.session.call(
            RTC_TOPIC["SPORT_MOD"],
            LOCO_API["SET_VELOCITY"],
            {"velocity": [vx, vy, omega], "duration": duration},
        )

        # Code 127 = "this api_id has no handler in the current state".
        # Verified on an R1 Air: SET_VELOCITY is refused in Loco20Dof (830).
        # The robot definitely did NOT move, so falling back to the
        # wireless-controller path is safe and is what the official app does.
        if response.code == 127:
            notes = list(notes) + [
                "SET_VELOCITY (7105) answered 127 — no speed handler in the "
                f"current state ({state.get('state')}). Used the joystick "
                "path (rt/wirelesscontroller) instead."
            ]
            await _drive_joystick(ctx, lx=-vy, ly=vx, rx=-omega, duration=duration)
            return ctx.ok(
                action="move",
                transport="joystick_fallback",
                duration_s=round(duration, 2),
                command={"vx": vx, "vy": vy, "omega": omega},
                code=response.code,
                stopped=True,
                notes=notes,
            )

        if not response.ok:
            raise ctx.session.refusal(
                response, api_id=LOCO_API["SET_VELOCITY"], topic=RTC_TOPIC["SPORT_MOD"], action="robot_move"
            )
        if response.timed_out:
            # No reply is normal for some firmware builds. The window below is
            # settled locally either way, and we stop the robot afterwards.
            notes = list(notes) + [
                "SET_VELOCITY (7105) got no reply; the window was tracked "
                "locally and the robot was stopped when it elapsed."
            ]
        try:
            await asyncio.sleep(duration)
        finally:
            with contextlib.suppress(Exception):
                await ctx.session.safe_stop(reason="r1_move:window_elapsed")

    if state.get("state") == "Loco20Dof":
        notes = list(notes) + [
            "The robot is in Loco20Dof (830). Its speed handler is fixed at vx 1.0, "
            "so the requested speed may not be honoured — direction only."
        ]
    elif state.get("semantic") not in ("locomotion", "stance"):
        notes = list(notes) + [
            f"Movement was sent while the robot is in '{state.get('state')}'. "
            "R1 usually needs to be in Lock (4) or a locomotion state first."
        ]

    return ctx.ok(
        action="move",
        duration_s=round(duration, 2),
        command={"vx": vx, "vy": vy, "omega": omega},
        code=response.code,
        notes=notes,
    )


async def _go2_move(ctx: ToolContext, vx: float, vy: float, omega: float, duration: float, notes) -> dict:
    """Go2 ``Move`` (1008) has no duration, so we stop it ourselves."""
    from unitree_webrtc_connect.constants import SPORT_CMD, RTC_TOPIC

    try:
        async with ctx.session.hold("go2_move"):
            response = await ctx.session.call(
                RTC_TOPIC["SPORT_MOD"],
                SPORT_CMD["Move"],
                {"x": vx, "y": vy, "z": omega},
            )
            if not response.ok:
                raise ctx.session.refusal(
                    response, api_id=SPORT_CMD["Move"], topic=RTC_TOPIC["SPORT_MOD"], action="robot_move"
                )
            await asyncio.sleep(duration)
    finally:
        # Even if the request failed, make sure nothing keeps walking.
        with contextlib.suppress(Exception):
            await ctx.session.safe_stop(reason="go2_move:window_elapsed")

    notes = list(notes) + [
        "Go2 Move (1008) has no duration; StopMove (1003) was sent after the window."
    ]
    return ctx.ok(
        action="move",
        duration_s=round(duration, 2),
        command={"vx": vx, "vy": vy, "omega": omega},
        code=response.code,
        notes=notes,
    )
