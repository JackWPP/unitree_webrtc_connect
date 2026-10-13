"""Go2-only tools: motion modes, VUI, obstacle avoidance and LiDAR.

Registered only when the configured robot is a Go2 (the server checks
``CAP_VUI`` / ``CAP_MOTION_SWITCHER`` / ``CAP_LIDAR``).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Optional

from mcp.types import ToolAnnotations

from ..errors import FeatureUnavailable
from ..robots.base import CAP_LIDAR, CAP_MOTION_SWITCHER, CAP_OBSTACLE_AVOID, CAP_VUI
from ..robots.go2 import MOTION_MODES
from .context import ToolContext, run_tool

MOTION = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False
)
GENTLE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

LED_COLORS = ("white", "red", "yellow", "blue", "green", "cyan", "purple")


def register(server, ctx: ToolContext) -> None:
    if ctx.profile.has(CAP_MOTION_SWITCHER):

        @server.tool(
            name="go2_motion_mode",
            description=(
                "Read or set the Go2 motion-control mode. Call with mode='' to "
                "read. Setting 'ai' hands control to the built-in AI modes (the "
                "robot stands up and behaves differently); requires confirm=true. "
                "Switching back to 'normal' restores the sport commands used by "
                "the other tools."
            ),
            annotations=MOTION,
        )
        async def go2_motion_mode(mode: str = "", confirm: bool = False, robot_id: str = "") -> Dict[str, Any]:
            args = {"mode": mode, "confirm": confirm}

            async def _impl():
                from unitree_webrtc_connect.constants import RTC_TOPIC

                target = (mode or "").strip().lower()
                if not target:
                    response = await ctx.session.call(RTC_TOPIC["MOTION_SWITCHER"], 1001)
                    if not response.ok:
                        raise ctx.session.refusal(
                            response, api_id=1001, topic=RTC_TOPIC["MOTION_SWITCHER"], action="motion_mode read"
                        )
                    payload = response.decoded(default={})
                    current = payload.get("name") if isinstance(payload, dict) else None
                    return ctx.ok(mode=current, modes=list(MOTION_MODES), code=response.code)

                if target not in MOTION_MODES:
                    return {
                        "ok": False,
                        "error": "unknown_mode",
                        "message": f"'{mode}' is not a motion mode.",
                        "hint": f"Use one of: {', '.join(MOTION_MODES)}.",
                    }

                ctx.session.guard.require_motion_enabled("go2_motion_mode")
                ctx.session.guard.require_confirm(
                    confirm, f"go2_motion_mode('{target}') — the robot changes behaviour and stands up"
                )
                response = await ctx.session.call(
                    RTC_TOPIC["MOTION_SWITCHER"], 1002, {"name": target}
                )
                if not response.ok:
                    raise ctx.session.refusal(
                        response, api_id=1002, topic=RTC_TOPIC["MOTION_SWITCHER"], action="motion_mode set"
                    )
                # The mode switch is slow (the robot stands up); give it room.
                await asyncio.sleep(5.0 if target == "normal" else 10.0)
                return await ctx.result_of(response, action="motion_mode", mode=target)

            return await run_tool(ctx, "go2_motion_mode", args, _impl)

    if ctx.profile.has(CAP_VUI):

        @server.tool(
            name="go2_set_volume",
            description="Set the Go2 speaker volume (0-10). Harmless, no confirmation needed.",
            annotations=GENTLE,
        )
        async def go2_set_volume(volume: int, robot_id: str = "") -> Dict[str, Any]:
            args = {"volume": volume}

            async def _impl():
                from unitree_webrtc_connect.constants import RTC_TOPIC

                level = max(0, min(10, int(volume)))
                response = await ctx.session.call(RTC_TOPIC["VUI"], 1003, {"volume": level})
                if not response.ok:
                    raise ctx.session.refusal(
                        response, api_id=1003, topic=RTC_TOPIC["VUI"], action="set_volume"
                    )
                return await ctx.result_of(response, volume=level)

            return await run_tool(ctx, "go2_set_volume", args, _impl)

        @server.tool(
            name="go2_set_brightness",
            description="Set the Go2 head-light brightness (0-10).",
            annotations=GENTLE,
        )
        async def go2_set_brightness(level: int, robot_id: str = "") -> Dict[str, Any]:
            args = {"level": level}

            async def _impl():
                from unitree_webrtc_connect.constants import RTC_TOPIC

                value = max(0, min(10, int(level)))
                response = await ctx.session.call(RTC_TOPIC["VUI"], 1005, {"brightness": value})
                if not response.ok:
                    raise ctx.session.refusal(
                        response, api_id=1005, topic=RTC_TOPIC["VUI"], action="set_brightness"
                    )
                return await ctx.result_of(response, brightness=value)

            return await run_tool(ctx, "go2_set_brightness", args, _impl)

        @server.tool(
            name="go2_set_led",
            description=(
                "Set the Go2 LED colour for N seconds, optionally flashing. "
                f"Colours: {', '.join(LED_COLORS)}. flash_cycle_ms must be between "
                "499 and seconds*1000."
            ),
            annotations=GENTLE,
        )
        async def go2_set_led(
            color: str = "white", seconds: int = 5, flash_cycle_ms: int = 0,
            robot_id: str = "",
        ) -> Dict[str, Any]:
            args = {"color": color, "seconds": seconds, "flash_cycle_ms": flash_cycle_ms}

            async def _impl():
                from unitree_webrtc_connect.constants import RTC_TOPIC

                name = (color or "").strip().lower()
                if name not in LED_COLORS:
                    return {
                        "ok": False,
                        "error": "unknown_color",
                        "message": f"'{color}' is not a supported LED colour.",
                        "hint": f"Use one of: {', '.join(LED_COLORS)}.",
                    }
                window = max(1, min(int(seconds), 60))
                parameter: Dict[str, Any] = {"color": name, "time": window}
                if flash_cycle_ms:
                    cycle = max(499, min(int(flash_cycle_ms), window * 1000))
                    parameter["flash_cycle"] = cycle
                response = await ctx.session.call(RTC_TOPIC["VUI"], 1007, parameter)
                if not response.ok:
                    raise ctx.session.refusal(
                        response, api_id=1007, topic=RTC_TOPIC["VUI"], action="set_led"
                    )
                return await ctx.result_of(response, **parameter)

            return await run_tool(ctx, "go2_set_led", args, _impl)

        @server.tool(
            name="go2_get_vui",
            description="Read the current Go2 volume and head-light brightness.",
            annotations=READ_ONLY,
        )
        async def go2_get_vui(robot_id: str = "") -> Dict[str, Any]:
            async def _impl():
                from unitree_webrtc_connect.constants import RTC_TOPIC

                volume = await ctx.session.call(RTC_TOPIC["VUI"], 1004)
                brightness = await ctx.session.call(RTC_TOPIC["VUI"], 1006)
                volume_payload = volume.decoded(default={}) if volume.ok else {}
                brightness_payload = brightness.decoded(default={}) if brightness.ok else {}
                return ctx.ok(
                    volume=(volume_payload or {}).get("volume") if isinstance(volume_payload, dict) else None,
                    brightness=(brightness_payload or {}).get("brightness")
                    if isinstance(brightness_payload, dict)
                    else None,
                )

            return await run_tool(ctx, "go2_get_vui", {}, _impl)

    if ctx.profile.has(CAP_OBSTACLE_AVOID):

        @server.tool(
            name="go2_obstacle_avoid",
            description=(
                "Read or set Go2 obstacle avoidance. Call with no argument (or "
                "enable=null semantics via omit) to read the current state. When "
                "enabled, the avoidance service intercepts joystick/move commands "
                "and filters them for safety."
            ),
            annotations=GENTLE,
        )
        async def go2_obstacle_avoid(enable: Optional[bool] = None, robot_id: str = "") -> Dict[str, Any]:
            args = {"enable": enable}

            async def _impl():
                from unitree_webrtc_connect.constants import OBSTACLES_AVOID_API, RTC_TOPIC

                topic = RTC_TOPIC["OBSTACLES_AVOID"]
                if enable is None:
                    response = await ctx.session.call(topic, OBSTACLES_AVOID_API["SWITCH_GET"])
                    if not response.ok:
                        raise ctx.session.refusal(
                            response,
                            api_id=OBSTACLES_AVOID_API["SWITCH_GET"],
                            topic=topic,
                            action="obstacle_avoid read",
                        )
                    payload = response.decoded(default={})
                    return ctx.ok(
                        enabled=payload.get("enable") if isinstance(payload, dict) else None
                    )

                ctx.session.guard.require_motion_enabled("go2_obstacle_avoid")
                response = await ctx.session.call(
                    topic, OBSTACLES_AVOID_API["SWITCH_SET"], {"enable": bool(enable)}
                )
                if not response.ok:
                    raise ctx.session.refusal(
                        response,
                        api_id=OBSTACLES_AVOID_API["SWITCH_SET"],
                        topic=topic,
                        action="obstacle_avoid set",
                    )
                return await ctx.result_of(response, enabled=bool(enable))

            return await run_tool(ctx, "go2_obstacle_avoid", args, _impl)

    @server.tool(
        name="go2_get_state",
        description=(
            "Aggregate Go2 state: sport state (mode, gait, velocity, body height) "
            "plus the multi-state frame (volume, brightness, speed level, "
            "avoidance toggle). This is the Go2 equivalent of 'what is it doing'."
        ),
        annotations=READ_ONLY,
    )
    async def go2_get_state(robot_id: str = "") -> Dict[str, Any]:
        async def _impl():
            from unitree_webrtc_connect.constants import RTC_TOPIC

            sport = await ctx.session.read_topic(RTC_TOPIC["SPORT_MOD_STATE"], timeout=1.5)
            multi = await ctx.session.read_topic(RTC_TOPIC["MULTIPLE_STATE"], timeout=1.0)
            if isinstance(multi, str):
                try:
                    multi = json.loads(multi)
                except ValueError:
                    pass
            return ctx.ok(
                sport_state=sport or None,
                multi_state=multi or None,
                note="Empty sections mean the robot has not published that topic yet.",
            )

        return await run_tool(ctx, "go2_get_state", {}, _impl)

    if ctx.profile.has(CAP_LIDAR):

        @server.tool(
            name="go2_lidar",
            description=(
                "Turn the Go2 LiDAR on/off and (when on) return a compact summary "
                "of the latest point cloud: point count, resolution, origin and "
                "frame id. The full cloud is not returned — it is far too large "
                "for a tool result."
            ),
            annotations=GENTLE,
        )
        async def go2_lidar(enable: Optional[bool] = None, robot_id: str = "") -> Dict[str, Any]:
            args = {"enable": enable}

            async def _impl():
                from unitree_webrtc_connect.constants import RTC_TOPIC

                conn = getattr(ctx.backend, "conn", None)
                if conn is None:
                    raise FeatureUnavailable(
                        "LiDAR control needs the live WebRTC backend "
                        "(the mock backend has no point cloud)."
                    )

                if enable is not None:
                    if enable:
                        try:
                            await conn.datachannel.disableTrafficSaving(True)
                        except Exception:
                            pass
                        try:
                            conn.datachannel.set_decoder("libvoxel")
                        except ImportError as exc:
                            raise FeatureUnavailable(
                                "LiDAR decoding needs the optional dependencies: "
                                "pip install 'unitree-webrtc-connect[go2]'.",
                                detail={"error": str(exc)},
                            ) from exc
                    conn.datachannel.pub_sub.publish_without_callback(
                        RTC_TOPIC["ULIDAR_SWITCH"], "on" if enable else "off"
                    )
                    await asyncio.sleep(0.5)

                payload = await ctx.session.read_topic(RTC_TOPIC["ULIDAR_ARRAY"], timeout=2.0)
                return ctx.ok(lidar=enable, summary=_summarize_lidar(payload))

            return await run_tool(ctx, "go2_lidar", args, _impl)


def _summarize_lidar(payload: Optional[dict]) -> dict:
    if not payload:
        return {"available": False, "note": "No LiDAR frame received (is the sensor on?)."}
    decoded = payload.get("data")
    count = None
    if isinstance(decoded, dict):
        count = decoded.get("point_count")
        if count is None and "points" in decoded:
            try:
                count = len(decoded["points"])
            except TypeError:
                count = None
    summary = {
        "available": True,
        "stamp": payload.get("stamp"),
        "frame_id": payload.get("frame_id"),
        "resolution_m": payload.get("resolution"),
        "origin": payload.get("origin"),
        "src_size_bytes": payload.get("src_size"),
        "point_count": count,
    }
    return summary
