"""The console's HTTP and WebSocket surface.

Built on Starlette rather than Flask, and that is the point: the WebRTC
connections and the HTTP handlers must run on **one** event loop. The
previous generation of this console (`wpp`'s `web_server.py`) ran Flask in a
thread with a second asyncio loop in another thread and bridged them with
``run_coroutine_threadsafe`` plus fourteen blocking ``Future.result()`` calls.
Every one of those is a place where the UI can wedge, and they exist only
because the loop and the request handler were separated in the first place.

Starlette is also already present in this project through the ``mcp`` extra,
so the console needs no new runtime dependency beyond ``uvicorn``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import parse_qs

from starlette.applications import Starlette
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from unitree_robot_agent.errors import AgentError, UnknownRobot

from .config import ConsoleConfig
from .service import ConsoleService
from .state import MotionBlocked

logger = logging.getLogger("unitree_fleet_web.app")

STATIC_DIR = Path(__file__).parent / "static"
UI_FILENAME = "index.html"

#: Error code -> HTTP status. Anything unmapped is a 400.
_STATUS_FOR_CODE = {
    "unknown_robot": 404,
    "motion_blocked": 409,
    "bad_input_mode": 400,
    "bad_source": 400,
}


def _error_response(exc: Exception) -> JSONResponse:
    if isinstance(exc, AgentError):
        payload = {
            "ok": False,
            "code": exc.code,
            "error": str(exc),
            "hint": exc.hint,
            "retryable": exc.retryable,
        }
        status = _STATUS_FOR_CODE.get(exc.code, 400)
        if isinstance(exc, MotionBlocked):
            status = 409
    else:
        payload = {"ok": False, "code": "internal_error", "error": str(exc)}
        status = 500
    return JSONResponse(payload, status_code=status)


def _presented_token(scope: Dict[str, Any]) -> str:
    """Token from a header, or from the query string.

    The query-string path exists for the MJPEG endpoint: an ``<img>`` tag
    cannot set an Authorization header. It is a real leak vector — the token
    ends up in access logs — so it is accepted only for reads.
    """
    for name, value in scope.get("headers", []):
        if name == b"authorization":
            text = value.decode("latin-1")
            prefix = "bearer "
            if text.lower().startswith(prefix):
                return text[len(prefix):].strip()
            return text.strip()
        if name == b"x-console-token":
            return value.decode("latin-1").strip()
    query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
    values = query.get("token") or []
    return values[0].strip() if values else ""


class TokenAuthMiddleware:
    """Pure-ASGI auth for both HTTP and WebSocket.

    Written as raw ASGI rather than ``BaseHTTPMiddleware`` so the WebSocket
    path is covered by the same check — a console whose websocket bypassed
    authentication would still leak telemetry, and worse, accept commands.
    """

    def __init__(self, app: Callable, config: ConsoleConfig) -> None:
        self.app = app
        self.config = config

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        if scope["path"] in ("/healthz",):
            await self.app(scope, receive, send)
            return
        if self.config.authorises(_presented_token(scope)):
            await self.app(scope, receive, send)
            return

        logger.warning("rejected unauthenticated %s %s", scope["type"], scope.get("path"))
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4401})
            return
        body = json.dumps(
            {
                "ok": False,
                "code": "unauthorized",
                "error": "Missing or invalid console token.",
                "hint": "Send `Authorization: Bearer <token>` or ?token=<token>.",
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


async def _read_body(request) -> Dict[str, Any]:
    try:
        raw = await request.body()
    except Exception:  # noqa: BLE001
        return {}
    if not raw:
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def create_app(service: ConsoleService, config: Optional[ConsoleConfig] = None) -> Starlette:
    """Build the ASGI app. One event loop, one fleet, no thread bridges."""
    config = config or service.config

    # ── reads ────────────────────────────────────────────────────────

    async def healthz(request) -> Response:
        return JSONResponse({"ok": True, "robots": len(service.fleet)})

    async def get_fleet(request) -> Response:
        return JSONResponse(service.snapshot())

    async def get_status(request) -> Response:
        live = request.query_params.get("live", "").lower() in ("1", "true", "yes")
        try:
            return JSONResponse(await service.status(live=live))
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    # ── fleet commands ───────────────────────────────────────────────

    async def post_stop_all(request) -> Response:
        try:
            return JSONResponse(await service.stop_all())
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    async def post_estop_all(request) -> Response:
        body = await _read_body(request)
        try:
            return JSONResponse(
                await service.estop(reason=str(body.get("reason") or "operator"))
            )
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    async def post_estop_release(request) -> Response:
        try:
            return JSONResponse(service.release_estop())
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    async def post_mode(request) -> Response:
        body = await _read_body(request)
        try:
            return JSONResponse(
                await service.set_input_mode(str(body.get("mode") or ""))
            )
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    async def post_focus(request) -> Response:
        body = await _read_body(request)
        try:
            return JSONResponse(service.set_focus(str(body.get("robot_id") or "")))
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    async def post_bind(request) -> Response:
        body = await _read_body(request)
        try:
            return JSONResponse(
                service.bind_source(
                    str(body.get("source") or ""), str(body.get("robot_id") or "")
                )
            )
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    # ── per-robot commands ───────────────────────────────────────────

    async def get_console_tools(request) -> Response:
        """Which guarded operations this console can invoke.

        Capability-gated server-side, so the UI never offers a button that
        cannot work on this robot.
        """
        return JSONResponse({"tools": service.tools_available})

    async def post_robot_tool(request) -> Response:
        robot_id = request.path_params["robot_id"]
        body = await _read_body(request)
        tool = str(body.get("tool") or "")
        arguments = body.get("args") or {}
        if not isinstance(arguments, dict):
            return _error_response(
                AgentError("`args` must be an object.", code="bad_args")
            )
        try:
            return JSONResponse(await service.call_tool(robot_id, tool, arguments))
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    async def post_robot_connect(request) -> Response:
        robot_id = request.path_params["robot_id"]
        try:
            payload = await service.connect(robot_id)
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)
        # A failed connect is a 200 with connected=false and the reason, not a
        # 5xx: the console wants to show *why* (busy, no key, unreachable)
        # rather than treat it as a server fault.
        return JSONResponse(payload)

    async def post_robot_disconnect(request) -> Response:
        robot_id = request.path_params["robot_id"]
        try:
            return JSONResponse(await service.disconnect(robot_id))
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    async def get_robot_status(request) -> Response:
        robot_id = request.path_params["robot_id"]
        try:
            service.fleet.get(robot_id)
            records = await service.fleet.status_all(live=True)
            record = next(r for r in records if r["robot_id"] == robot_id)
            return JSONResponse({"robot_id": robot_id, "status": record})
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    async def post_robot_teleop(request) -> Response:
        robot_id = request.path_params["robot_id"]
        body = await _read_body(request)
        try:
            return JSONResponse(
                await service.teleop(
                    robot_id,
                    lx=body.get("lx", 0.0),
                    ly=body.get("ly", 0.0),
                    rx=body.get("rx", 0.0),
                    ry=body.get("ry", 0.0),
                    keys=body.get("keys", 0),
                    source=str(body.get("source") or ""),
                )
            )
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    async def post_fleet_teleop(request) -> Response:
        """One input frame; the server decides which robots it means."""
        body = await _read_body(request)
        try:
            return JSONResponse(
                await service.teleop_source(
                    source=str(body.get("source") or ""),
                    lx=body.get("lx", 0.0),
                    ly=body.get("ly", 0.0),
                    rx=body.get("rx", 0.0),
                    ry=body.get("ry", 0.0),
                    keys=body.get("keys", 0),
                )
            )
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    async def post_fleet_release(request) -> Response:
        body = await _read_body(request)
        try:
            return JSONResponse(
                await service.release_drive(source=str(body.get("source") or ""))
            )
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    async def post_robot_move(request) -> Response:
        robot_id = request.path_params["robot_id"]
        body = await _read_body(request)
        try:
            return JSONResponse(
                await service.move(
                    robot_id,
                    lx=body.get("lx", 0.0),
                    ly=body.get("ly", 0.0),
                    rx=body.get("rx", 0.0),
                    duration=body.get("duration", 1.0),
                )
            )
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    async def post_robot_stop(request) -> Response:
        robot_id = request.path_params["robot_id"]
        try:
            return JSONResponse(await service.stop(robot_id))
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    async def post_robot_estop(request) -> Response:
        robot_id = request.path_params["robot_id"]
        try:
            return JSONResponse(await service.estop(robot_id=robot_id))
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    async def post_robot_estop_release(request) -> Response:
        robot_id = request.path_params["robot_id"]
        try:
            return JSONResponse(service.release_estop(robot_id=robot_id))
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    async def post_robot_video(request) -> Response:
        robot_id = request.path_params["robot_id"]
        body = await _read_body(request)
        try:
            return JSONResponse(
                service.set_video(robot_id, bool(body.get("enabled", True)))
            )
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    async def get_console_controls(request) -> Response:
        robot_id = request.query_params.get("robot_id") or ""
        try:
            if not robot_id:
                return JSONResponse(
                    {
                        "robots": {
                            rid: service.controls_payload(rid) for rid in service.fleet.ids
                        }
                    }
                )
            return JSONResponse(service.controls_payload(robot_id))
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    async def get_robot_audit(request) -> Response:
        robot_id = request.path_params["robot_id"]
        try:
            limit = int(request.query_params.get("limit", 30))
        except ValueError:
            limit = 30
        try:
            return JSONResponse(await service.audit(robot_id, limit=limit))
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    async def get_robot_stream(request) -> Response:
        robot_id = request.path_params["robot_id"]
        try:
            service.fleet.get(robot_id)
        except UnknownRobot as exc:
            return _error_response(exc)
        return StreamingResponse(
            service.mjpeg(robot_id),
            media_type="multipart/x-mixed-replace; boundary=unitree-fleet-frame",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    # ── live push ────────────────────────────────────────────────────

    async def websocket_status(websocket: WebSocket) -> None:
        await websocket.accept()
        # Counting watchers is what arms the operator lease: while a browser is
        # here, connected robots must not release themselves mid-session.
        service.client_connected()
        interval = 1.0 / max(0.25, config.status_hz)
        try:
            while True:
                try:
                    payload = await service.status(live=False)
                except Exception as exc:  # noqa: BLE001
                    payload = {"error": str(exc)}
                await websocket.send_json({"type": "status", **payload})
                await asyncio.sleep(interval)
        except (WebSocketDisconnect, RuntimeError):
            return
        except asyncio.CancelledError:
            raise
        finally:
            service.client_disconnected()

    # ── UI ───────────────────────────────────────────────────────────

    async def index(request) -> Response:
        if not config.serve_ui:
            return PlainTextResponse("UI disabled", status_code=404)
        path = (config.static_dir or STATIC_DIR) / UI_FILENAME
        if not path.is_file():
            return PlainTextResponse("console UI not found", status_code=500)
        return HTMLResponse(path.read_text(encoding="utf-8"))

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        await service.start()
        try:
            yield
        finally:
            await service.aclose()

    routes = [
        Route("/healthz", healthz),
        Route("/", index),
        Route("/api/fleet", get_fleet),
        Route("/api/fleet/status", get_status),
        Route("/api/fleet/stop", post_stop_all, methods=["POST"]),
        Route("/api/fleet/estop", post_estop_all, methods=["POST"]),
        Route("/api/fleet/estop/release", post_estop_release, methods=["POST"]),
        Route("/api/fleet/mode", post_mode, methods=["POST"]),
        Route("/api/fleet/focus", post_focus, methods=["POST"]),
        Route("/api/fleet/bind", post_bind, methods=["POST"]),
        Route("/api/fleet/teleop", post_fleet_teleop, methods=["POST"]),
        Route("/api/fleet/release", post_fleet_release, methods=["POST"]),
        Route("/api/console/tools", get_console_tools),
        Route("/api/robot/{robot_id}/status", get_robot_status),
        Route("/api/robot/{robot_id}/connect", post_robot_connect, methods=["POST"]),
        Route("/api/robot/{robot_id}/disconnect", post_robot_disconnect, methods=["POST"]),
        Route("/api/robot/{robot_id}/tool", post_robot_tool, methods=["POST"]),
        Route("/api/robot/{robot_id}/audit", get_robot_audit),
        Route("/api/console/controls", get_console_controls),
        Route("/api/robot/{robot_id}/teleop", post_robot_teleop, methods=["POST"]),
        Route("/api/robot/{robot_id}/move", post_robot_move, methods=["POST"]),
        Route("/api/robot/{robot_id}/stop", post_robot_stop, methods=["POST"]),
        Route("/api/robot/{robot_id}/estop", post_robot_estop, methods=["POST"]),
        Route(
            "/api/robot/{robot_id}/estop/release",
            post_robot_estop_release,
            methods=["POST"],
        ),
        Route("/api/robot/{robot_id}/video", post_robot_video, methods=["POST"]),
        Route("/api/robot/{robot_id}/stream.mjpeg", get_robot_stream),
        WebSocketRoute("/ws", websocket_status),
    ]

    static_dir = config.static_dir or STATIC_DIR
    if static_dir.is_dir():
        routes.append(Mount("/static", app=StaticFiles(directory=str(static_dir))))

    app = Starlette(routes=routes, lifespan=lifespan)
    if config.token:
        app.add_middleware(TokenAuthMiddleware, config=config)
    return app
