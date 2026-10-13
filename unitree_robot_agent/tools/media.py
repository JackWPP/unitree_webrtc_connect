"""Vision tools: capture a frame, find out which source works, browse history.

The camera situation in the field is messier than the feature table
suggests — the R1 Air we tested has no usable camera at all (no WebRTC
track, no RTSP mount, no still-photo reply). So instead of one tool that
either works or doesn't, this exposes:

* ``robot_snapshot`` — get a picture, trying every configured source;
* ``robot_vision_probe`` — report *why* each source failed, so a human or an
  agent can fix the configuration instead of guessing;
* ``robot_snapshot_history`` — read back what was captured earlier, which is
  what makes "what did it look like when I sent that command" answerable.
"""

from __future__ import annotations

from typing import Any, Dict

from mcp.types import ToolAnnotations

from .context import ToolContext, run_tool

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=False, open_world_hint=False
)


def register(server, ctx: ToolContext) -> None:
    @server.tool(
        name="robot_snapshot",
        description=(
            "Capture one frame and return it as an image. source='auto' tries "
            "the robot's video track, then the firmware still-photo api, then "
            "RTSP, then any configured local file/camera. Frames are also "
            "archived — use robot_snapshot_history to get the file path of a "
            "previous shot. If nothing works, robot_vision_probe explains why."
        ),
        annotations=READ_ONLY,
    )
    async def robot_snapshot(
        source: str = "auto",
        timeout_s: float = 6.0,
        save: bool = True,
        max_width: int = 0,
        robot_id: str = "",
    ) -> Any:
        args = {"source": source, "timeout_s": timeout_s, "save": save, "max_width": max_width}

        async def _impl():
            window = max(1.0, min(float(timeout_s), 30.0))
            width = int(max_width) if max_width and int(max_width) > 0 else None
            result = await ctx.vision.capture(
                source, timeout=window, save=save, max_width=width
            )
            if not result.ok:
                return {
                    "ok": False,
                    "error": "no_frame",
                    "source": result.source,
                    "message": result.error or "no frame was captured",
                    "detail": result.note,
                    "hint": (
                        "Run robot_vision_probe to see every source and why it "
                        "failed. For offline work set UNITREE_AGENT_SNAPSHOT_FILE "
                        "to an image/video, or UNITREE_AGENT_CAMERA to a local "
                        "camera index."
                    ),
                }
            image = _image_from_bytes(result.image)
            if image is None:
                return {
                    "ok": False,
                    "error": "encode_failed",
                    "message": "A frame arrived but could not be delivered as an image.",
                    "saved_path": result.saved_path,
                }
            return image

        return await run_tool(ctx, "robot_snapshot", args, _impl)

    @server.tool(
        name="robot_vision_probe",
        description=(
            "Try every frame source and report which ones work, how long they "
            "take, and the exact failure reason for the rest. Read-only and "
            "safe; the first thing to run when a snapshot fails."
        ),
        annotations=READ_ONLY,
    )
    async def robot_vision_probe(timeout_s: float = 5.0, robot_id: str = "") -> Dict[str, Any]:
        args = {"timeout_s": timeout_s}

        async def _impl():
            window = max(1.0, min(float(timeout_s), 20.0))
            statuses = await ctx.vision.probe(timeout=window)
            working = [s.name for s in statuses if s.available]
            return ctx.ok(
                sources=[s.to_dict() for s in statuses],
                working=working,
                configured_source=ctx.config.video_source,
                hint=(
                    "Set UNITREE_AGENT_VIDEO_SOURCE to the working one, or leave "
                    "it as auto. robot-side sources are unverified upstream: the "
                    "R1 Air has no camera, so only a robot with a camera can "
                    "confirm them."
                )
                if not working
                else "robot_snapshot will use these in order.",
            )

        return await run_tool(ctx, "robot_vision_probe", args, _impl)

    @server.tool(
        name="robot_snapshot_history",
        description=(
            "The most recent archived frames: timestamp, source, dimensions and "
            "file path. Use it to reference a specific earlier shot."
        ),
        annotations=READ_ONLY,
    )
    async def robot_snapshot_history(limit: int = 10, robot_id: str = "") -> Dict[str, Any]:
        args = {"limit": limit}

        async def _impl():
            records = ctx.vision.history(limit)
            return ctx.ok(
                records=records,
                store=str(ctx.vision.store_dir),
                count=len(records),
            )

        return await run_tool(ctx, "robot_snapshot_history", args, _impl)


def _image_from_bytes(payload):
    """Wrap bytes into the MCP ``Image`` helper, detecting the container."""
    if not payload:
        return None
    try:
        from mcp.server.mcpserver import Image
    except Exception:  # pragma: no cover - older SDK layout
        try:
            from mcp.server.fastmcp import Image  # type: ignore
        except Exception:
            return None

    if payload[:2] == b"\xff\xd8":
        fmt = "jpeg"
    elif payload[:4] == b"\x89PNG":
        fmt = "png"
    else:
        fmt = None
    try:
        return Image(data=payload, format=fmt)
    except Exception:
        return None
