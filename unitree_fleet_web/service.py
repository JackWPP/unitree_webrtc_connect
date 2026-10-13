"""What the console can actually do, with the safety rules applied.

This is the only layer the HTTP API talks to. Keeping the rules here rather
than in the browser is deliberate: a rule enforced in JavaScript is a rule
that any second client, `curl`, or a stale tab can bypass.

Every method returns a plain dict, so a response is always serialisable and
always carries per-robot outcomes rather than a single optimistic boolean.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any, AsyncIterator, Dict, List, Optional

from unitree_robot_agent.bridge import ToolBridge
from unitree_robot_agent.core.telemetry import collect_lowstate
from unitree_robot_agent.errors import AgentError, UnknownRobot
from unitree_robot_agent.fleet import FleetManager
from unitree_robot_agent.server import build_server

from .config import ConsoleConfig
from .state import ConsoleState, EStopScope, InputMode

logger = logging.getLogger("unitree_fleet_web.service")

#: Joystick axes are normalised; the robot applies its own limits on top.
AXIS_LIMIT = 1.0

#: A telemetry read may wait for a topic's first frame. Bounded so a robot
#: that has gone quiet cannot hold the sampler.
TELEMETRY_TIMEOUT_S = 2.0

#: How long a status request keeps counting as "someone is watching".
#:
#: The WebSocket is the fast path, but the console polls `/api/fleet/status`
#: whenever the socket is down — behind a proxy that blocks upgrades, for
#: instance. Counting only sockets meant that in exactly that case the operator
#: got no telemetry *and* no lease: the panels stayed dead and the links were
#: free to drop under them. A status request is a browser watching just as much
#: as a socket is. Comfortably longer than the 1 s poll interval.
WATCH_WINDOW_S = 5.0

#: The tools the console may invoke, by name.
#:
#: An allowlist rather than a passthrough, and deliberately so. The console is
#: reachable over HTTP; handing it "call any tool" would expose
#: `robot_fetch_key` (which returns a credential), `robot_doctor` and the audit
#: tail to anything that can reach the port. These are the operations an
#: operator legitimately needs, and every one of them still enforces its own
#: guards — `confirm`, the dangerous-action gate, safe mode, rate limits — and
#: still writes to the audit trail, because it is the same function the MCP
#: server exposes.
#:
#: Not listed, on purpose:
#:   * `robot_move` / `robot_joystick` / `robot_turn` — the console has its own
#:     teleop endpoints, which carry the dead-man watchdog. Two paths to the
#:     same actuators is one path too many.
#:   * `robot_snapshot` — served as an MJPEG stream instead.
#:   * `robot_fetch_key`, `robot_auth_status`, `robot_doctor`,
#:     `robot_discover`, `robot_audit_tail` — diagnostics and credentials.
CONSOLE_TOOLS = frozenset(
    {
        # reads
        "robot_status",
        "robot_get_posture",
        "robot_list_actions",
        "robot_lowstate",
        "robot_errors",
        "robot_fleet_status",
        "robot_roster",
        # posture and named actions
        "robot_stand",
        "robot_lie_down",
        "robot_play_action",
        "robot_switch_state",
        "robot_arm_gesture",
        # Go2 extras
        "go2_motion_mode",
        "go2_set_led",
        "go2_get_vui",
        "go2_obstacle_avoid",
        "go2_set_volume",
        "go2_set_brightness",
        "go2_get_state",
        # safety
        "robot_stop",
        "robot_stop_all",
    }
)


#: The wireless-controller button field is a 16-bit mask.
MAX_KEYS = 0xFFFF


def _keys(value: Any) -> int:
    """Coerce the button bitfield to a 16-bit integer.

    Kept separate from :func:`_clamp` because it is a mask, not an axis:
    running it through float clamping would silently mangle it.
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    if number < 0:
        return 0
    return min(number, MAX_KEYS)


def _clamp(value: Any, limit: float = AXIS_LIMIT) -> float:
    """Coerce one joystick axis into ``[-limit, limit]``.

    Finite out-of-range values are clamped (99.0 means "full ahead"). NaN and
    infinities are **not** clamped to the limit — they become neutral. An
    axis of ``-inf`` is a protocol error, not a request for full reverse, and
    the conservative reading of garbage is "stop", not "go".
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number or number in (float("inf"), float("-inf")):
        return 0.0
    return max(-limit, min(limit, number))


class ConsoleService:
    """Fleet operations as the console exposes them."""

    def __init__(
        self,
        fleet: FleetManager,
        *,
        state: Optional[ConsoleState] = None,
        config: Optional[ConsoleConfig] = None,
    ) -> None:
        self.fleet = fleet
        self.state = state or ConsoleState()
        self.config = config or ConsoleConfig()
        # One frame in flight per robot: the backend's snapshot path shares a
        # single frame waiter, so concurrent callers would race for frames.
        self._frame_locks: Dict[str, asyncio.Lock] = {
            rid: asyncio.Lock() for rid in fleet.ids
        }
        self._deadman: Optional[asyncio.Task] = None
        self._lease: Optional[asyncio.Task] = None
        #: Browsers currently watching. The lease exists only while > 0.
        self._clients = 0
        #: Robots with an auto-connect already in flight, so a held key cannot
        #: start a connect storm.
        self._connecting: set = set()
        #: Latest battery/IMU/gait snapshot per robot, refreshed by a slow
        #: loop. Kept here so the 4 Hz status push stays pure memory: doing
        #: topic reads on that path would put robot I/O inside a UI loop.
        self._telemetry: Dict[str, dict] = {}
        self._telemetry_task: Optional[asyncio.Task] = None
        #: Monotonic time of the last status request, socket or HTTP.
        self._last_watch_at = 0.0
        if not self.state.focused and fleet.ids:
            self.state.focused = fleet.roster.default_id or fleet.ids[0]
        for rid in fleet.ids:
            self.state.video_enabled.setdefault(rid, True)

        # A server bundle is built purely so the console can call the *tool*
        # functions. Those are the single implementation of every guarded
        # operation; a second copy here would be the one that forgets a guard.
        # `fleet_from_env=False` because the fleet is already decided.
        self._bridge: Optional[ToolBridge] = None
        try:
            default = fleet.resolve("") if fleet.ids else None
            if default is not None:
                bundle = build_server(default, fleet=fleet, fleet_from_env=False)
                self._bridge = ToolBridge(bundle)
        except Exception as exc:  # noqa: BLE001 - teleop must still work
            logger.warning("tool bridge unavailable: %r", exc)

    # ── tool access ──────────────────────────────────────────────────

    @property
    def tools_available(self) -> List[str]:
        """Console-invocable tools that actually registered on this server.

        Capability gating happens at registration, so a Go2 never offers
        `robot_arm_gesture` and an R1 never offers `go2_set_led` — the console
        should not offer a button that cannot work.
        """
        if self._bridge is None:
            return []
        present = self._bridge.names()
        return sorted(CONSOLE_TOOLS & present)

    async def call_tool(
        self, robot_id: str, tool: str, arguments: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Invoke one allow-listed tool, bound to ``robot_id``."""
        if tool not in CONSOLE_TOOLS:
            raise AgentError(
                f"{tool!r} is not available from the console.",
                code="tool_not_allowed",
                hint="Only the operations an operator needs are exposed here.",
            )
        self.fleet.get(robot_id)  # validates, raises UnknownRobot
        if self._bridge is None:
            raise AgentError(
                "The tool bridge is not available on this server.",
                code="tool_bridge_unavailable",
                hint="Install the `agent` extra, or use the dedicated endpoints.",
            )

        payload = dict(arguments or {})
        # The binding wrapper reads robot_id from the call arguments, so the
        # tool acts on the robot the operator clicked.
        payload["robot_id"] = robot_id
        result = await self._bridge.call(tool, payload)
        if isinstance(result, dict):
            return result
        return {"ok": True, "result": str(result)}

    # ── lifecycle ────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the dead-man watchdog and the operator lease."""
        if self._deadman is None or self._deadman.done():
            self._deadman = asyncio.create_task(
                self._deadman_loop(), name="console-deadman"
            )
        if self.config.lease_s > 0 and (
            self._lease is None or self._lease.done()
        ):
            self._lease = asyncio.create_task(self._lease_loop(), name="console-lease")
        if self.config.telemetry_hz > 0 and (
            self._telemetry_task is None or self._telemetry_task.done()
        ):
            self._telemetry_task = asyncio.create_task(
                self._telemetry_loop(), name="console-telemetry"
            )

    async def aclose(self) -> None:
        for task in (self._deadman, self._lease, self._telemetry_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._deadman = None
        self._lease = None
        self._telemetry_task = None
        # Never leave a robot walking because the console went away.
        with contextlib.suppress(Exception):
            await self._zero_driver()
        await self.fleet.aclose_all()

    # ── operator lease ───────────────────────────────────────────────

    def client_connected(self) -> int:
        """A browser is watching. Returns the number of watchers."""
        self._clients += 1
        logger.info("console client connected (%d watching)", self._clients)
        return self._clients

    def client_disconnected(self) -> int:
        """A browser went away. Returns the number still watching."""
        self._clients = max(0, self._clients - 1)
        logger.info("console client disconnected (%d watching)", self._clients)
        return self._clients

    @property
    def watching(self) -> bool:
        """Whether a browser is watching, over either transport.

        A socket counts while it is open; an HTTP status request counts for
        :data:`WATCH_WINDOW_S` afterwards. Without the second half, a console
        polling because its socket could not open would be treated as nobody
        watching — no telemetry, and nothing holding the links.
        """
        if self._clients > 0:
            return True
        if not self._last_watch_at:
            return False
        return (time.monotonic() - self._last_watch_at) < WATCH_WINDOW_S

    def note_watch(self) -> None:
        """Record that a browser asked for status. Called by :meth:`status`."""
        self._last_watch_at = time.monotonic()

    async def _lease_loop(self) -> None:
        """Keep connected robots from releasing themselves while watched.

        A robot session disconnects after ``idle_disconnect_s`` precisely so
        the owner can take over with the official app. That is right for an
        agent asking a question; it is wrong for an operator with the console
        open, where the link dropping mid-session kills the video stream and
        makes the next command reconnect.

        The lease only *prevents release*. It never connects anything: a robot
        nobody asked for stays disconnected, and when the last browser goes
        away the sessions release on their own.
        """
        interval = max(1.0, self.config.lease_s)
        while True:
            try:
                await asyncio.sleep(interval)
                if not self.watching:
                    continue
                for robot_id in self.fleet.ids:
                    runtime = self.fleet.get(robot_id)
                    if runtime.session.is_connected:
                        runtime.session.touch()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a lease must not crash
                logger.warning("lease loop error: %r", exc)

    # ── telemetry ────────────────────────────────────────────────────

    async def refresh_telemetry(self, robot_id: str) -> Optional[dict]:
        """Read one robot's battery/IMU/gait and cache it.

        Every read is cache-first — ``backend.latest`` returns the last
        subscribed frame immediately — so once a topic has arrived this is
        formatting rather than traffic.
        """
        runtime = self.fleet.get(robot_id)
        if not runtime.session.is_connected:
            self._telemetry.pop(robot_id, None)
            return None
        try:
            snapshot = await asyncio.wait_for(
                collect_lowstate(runtime.session, runtime.profile, include_motors=False),
                timeout=TELEMETRY_TIMEOUT_S,
            )
        except Exception as exc:  # noqa: BLE001 - a quiet robot is not an error
            logger.debug("telemetry(%s) failed: %r", robot_id, exc)
            return self._telemetry.get(robot_id)
        # The FSM view is a separate topic and is what says "standing" on a
        # humanoid. On a Go2 it reports n/a, where the gait below carries it.
        with contextlib.suppress(Exception):
            snapshot["posture"] = await asyncio.wait_for(
                runtime.session.state_view(), timeout=TELEMETRY_TIMEOUT_S
            )
        self._telemetry[robot_id] = snapshot
        return snapshot

    async def _telemetry_loop(self) -> None:
        interval = 1.0 / max(0.1, self.config.telemetry_hz)
        while True:
            try:
                if not self.watching:
                    # Nobody is looking. Not reading is also what keeps the
                    # robots releasable while the console merely sits open.
                    await asyncio.sleep(interval)
                    continue
                # Sample *before* sleeping, so the first reading appears as
                # soon as someone opens the console rather than up to a tick
                # later — and so the sampler is not racy against a page that
                # connects a robot while it loads.
                await asyncio.gather(
                    *(self.refresh_telemetry(rid) for rid in self.fleet.ids),
                    return_exceptions=True,
                )
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a sampler must survive
                logger.warning("telemetry loop error: %r", exc)

    # ── explicit connection control ──────────────────────────────────

    async def connect(self, robot_id: str) -> Dict[str, Any]:
        """Bring one robot up on demand."""
        runtime = self.fleet.get(robot_id)
        try:
            await runtime.session.ensure_connected()
        except Exception as exc:  # noqa: BLE001
            return {
                "robot_id": robot_id,
                "connected": False,
                "error": getattr(exc, "code", type(exc).__name__),
                "message": str(exc),
                "hint": getattr(exc, "hint", ""),
            }
        return {"robot_id": robot_id, "connected": runtime.session.is_connected}

    async def disconnect(self, robot_id: str, *, reason: str = "operator") -> Dict[str, Any]:
        """Release one robot deliberately.

        ``stop_first`` is the session's default and is what we want: a robot
        must not be left walking because the operator closed the lid.
        """
        runtime = self.fleet.get(robot_id)
        self.state.release_robot(robot_id)
        await runtime.session.disconnect(reason=reason, stop_first=True)
        return {"robot_id": robot_id, "connected": runtime.session.is_connected}

    async def _deadman_loop(self) -> None:
        """Zero whatever is driving once frames stop arriving.

        A closed browser tab, a laptop lid, a dropped Wi-Fi link — all of them
        look identical from here: teleop frames stop. The robot, however,
        keeps applying the last frame it received. This is the only thing
        standing between that and a runaway dog.
        """
        interval = max(0.05, self.config.deadman_s / 4.0)
        while True:
            try:
                await asyncio.sleep(interval)
                stale = self.state.deadman_expired(
                    now=time.monotonic(), timeout_s=self.config.deadman_s
                )
                if stale:
                    logger.warning("dead-man: no teleop for %s, zeroing", sorted(stale))
                    self.state.stop_driving()
                    await self._zero_many(stale)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - watchdog must survive
                logger.warning("dead-man loop error: %r", exc)

    # ── reads ────────────────────────────────────────────────────────

    def snapshot(self) -> Dict[str, Any]:
        """Static fleet view: who exists, plus current operator state."""
        return {
            "fleet": self.fleet.redacted(),
            "state": self.state.as_dict(),
            "limits": {
                "deadman_s": self.config.deadman_s,
                "stream_fps": self.config.stream_fps,
                "axis_limit": AXIS_LIMIT,
                "lease_s": self.config.lease_s,
            },
            "watching": self._clients,
            "connecting": sorted(self._connecting),
            "tools": self.tools_available,
        }

    async def status(self, *, live: bool = False) -> Dict[str, Any]:
        # Every status read — the socket loop and the HTTP route alike — is a
        # browser watching. The lease and the telemetry sampler both key off it.
        self.note_watch()
        return {
            "robots": await self.fleet.status_all(live=live),
            "telemetry": dict(self._telemetry),
            "state": self.state.as_dict(),
            # Robots whose link is coming up because the operator started
            # driving. Without this the console looks dead for the seconds a
            # handshake takes.
            "connecting": sorted(self._connecting),
        }

    # ── teleop ───────────────────────────────────────────────────────

    async def teleop(
        self,
        robot_id: str,
        *,
        lx: float = 0.0,
        ly: float = 0.0,
        rx: float = 0.0,
        ry: float = 0.0,
        keys: int = 0,
        source: str = "",
    ) -> Dict[str, Any]:
        """One joystick frame aimed explicitly at one robot.

        Zeroing the robot we are switching away from is the whole reason this
        is a method and not a bare publish.
        """
        self.fleet.get(robot_id)  # validate
        self.state.require_motion_allowed(robot_id)

        stale = self.state.begin_driving([robot_id], source=source or "explicit")
        zeroed = sorted(stale)
        if zeroed:
            await self._zero_many(zeroed)

        delivered = await self._frame(robot_id, lx=lx, ly=ly, rx=rx, ry=ry, keys=keys)
        return {
            "robot_id": robot_id,
            "delivered": delivered,
            "zeroed": zeroed,
            "axes": {
                "lx": _clamp(lx), "ly": _clamp(ly), "rx": _clamp(rx), "ry": _clamp(ry),
            },
            "keys": _keys(keys),
        }

    async def teleop_source(
        self,
        *,
        source: str,
        lx: float = 0.0,
        ly: float = 0.0,
        rx: float = 0.0,
        ry: float = 0.0,
        keys: int = 0,
    ) -> Dict[str, Any]:
        """One input frame, routed by the console's own arbitration rules.

        The browser sends *what it read from the keyboard* and names the
        source; the server decides which robots that means. Keeping the
        resolution here is what stops a stale tab, or a second client, from
        driving a robot the operator did not select.
        """
        targets = self.state.targets_for(source=source, robot_ids=self.fleet.ids)
        for robot_id in targets:
            self.state.require_motion_allowed(robot_id)

        # Scoped to this source, so in split mode one hand cannot cancel the
        # other hand's robot.
        stale = self.state.begin_driving(targets, source=source or "keyboard")
        zeroed = sorted(stale)
        if zeroed:
            await self._zero_many(zeroed)

        results = []
        for robot_id in targets:
            results.append(
                {
                    "robot_id": robot_id,
                    "delivered": await self._frame(
                        robot_id, lx=lx, ly=ly, rx=rx, ry=ry, keys=keys
                    ),
                }
            )
        return {
            "source": source,
            "targets": targets,
            "zeroed": zeroed,
            "axes": {
                "lx": _clamp(lx), "ly": _clamp(ly), "rx": _clamp(rx), "ry": _clamp(ry),
            },
            "keys": _keys(keys),
            "results": results,
        }

    async def _frame(
        self,
        robot_id: str,
        *,
        lx: float,
        ly: float,
        rx: float,
        ry: float = 0.0,
        keys: int = 0,
    ) -> bool:
        """Publish one wireless-controller frame.

        All five fields the robot accepts are passed through. Dropping `ry`
        and `keys` would silently disable the body-rotation axis and every
        button on a physical controller — a gamepad that appears to work
        except that half of it does nothing.
        """
        await self._autoconnect(robot_id)
        delivered = await self.fleet.get(robot_id).session.joystick(
            lx=_clamp(lx), ly=_clamp(ly), rx=_clamp(rx), ry=_clamp(ry), keys=_keys(keys)
        )
        return bool(delivered)

    async def release_drive(self, *, source: str = "") -> Dict[str, Any]:
        """Operator let go: zero whoever that source was driving.

        With no source named, every source is released — which is what a
        window blur or a mode change wants.
        """
        previous = self.state.stop_driving(source or None)
        await self._zero_many(previous)
        return {"zeroed": sorted(previous)}

    async def move(
        self,
        robot_id: str,
        *,
        lx: float = 0.0,
        ly: float = 0.0,
        rx: float = 0.0,
        duration: float = 1.0,
    ) -> Dict[str, Any]:
        """A profiled move that always ends at zero velocity."""
        runtime = self.fleet.get(robot_id)
        self.state.require_motion_allowed(robot_id)

        ceiling = float(getattr(runtime.config, "max_move_duration_s", 10.0) or 10.0)
        duration = max(0.05, min(float(duration or 1.0), ceiling))

        stale = self.state.begin_driving([robot_id], source="move")
        zeroed = sorted(stale)
        if zeroed:
            await self._zero_many(zeroed)

        try:
            result = await runtime.motion.drive(
                lx=_clamp(lx), ly=_clamp(ly), rx=_clamp(rx), duration=duration
            )
        finally:
            self.state.stop_driving("move")
        return {"robot_id": robot_id, "zeroed": zeroed, "motion": result}

    # ── stops ────────────────────────────────────────────────────────

    async def stop(self, robot_id: str, *, reason: str = "console_stop") -> Dict[str, Any]:
        runtime = self.fleet.get(robot_id)
        self.state.release_robot(robot_id)
        try:
            delivered = await runtime.session.safe_stop(reason=reason)
        except Exception as exc:  # noqa: BLE001
            return {
                "robot_id": robot_id,
                "ok": False,
                "delivered": False,
                "link": "error",
                "detail": f"{type(exc).__name__}: {exc}",
            }
        connected = runtime.session.is_connected
        return {
            "robot_id": robot_id,
            "ok": bool(delivered),
            "delivered": bool(delivered),
            "link": "up" if connected else "down",
            "detail": "" if delivered else "robot did not acknowledge the stop",
        }

    async def stop_all(self, *, reason: str = "console_stop_all") -> Dict[str, Any]:
        self.state.stop_driving()
        outcomes = [o.as_dict() for o in await self.fleet.stop_all(reason=reason)]
        stopped = [o["robot_id"] for o in outcomes if o["ok"]]
        not_stopped = [o["robot_id"] for o in outcomes if not o["ok"]]
        return {
            "outcomes": outcomes,
            "stopped": stopped,
            # Spelled out rather than implied by a single boolean, because
            # "dog 2 was not stopped" is the one thing a stop must not hide.
            "not_stopped": not_stopped,
            "complete": not not_stopped,
        }

    # ── emergency stop (latching) ────────────────────────────────────

    async def estop(self, *, robot_id: str = "", reason: str = "operator") -> Dict[str, Any]:
        """Latch the e-stop, then damp. The latch is what makes it an e-stop."""
        if robot_id:
            self.fleet.get(robot_id)  # validate
            self.state.estop.engage(
                scope=EStopScope.ROBOT, robot_id=robot_id, reason=reason
            )
            targets = [robot_id]
        else:
            self.state.estop.engage(scope=EStopScope.ALL, robot_id="", reason=reason)
            targets = list(self.fleet.ids)

        self.state.stop_driving()
        results = await asyncio.gather(
            *(self._damp(rid) for rid in targets), return_exceptions=True
        )
        outcomes = []
        for rid, result in zip(targets, results):
            if isinstance(result, BaseException):
                outcomes.append(
                    {
                        "robot_id": rid,
                        "ok": False,
                        "link": "error",
                        "detail": f"{type(result).__name__}: {result}",
                    }
                )
            else:
                outcomes.append(result)
        return {
            "estop": self.state.estop.as_dict(),
            "outcomes": outcomes,
            "not_damped": [o["robot_id"] for o in outcomes if not o["ok"]],
        }

    async def _damp(self, robot_id: str) -> Dict[str, Any]:
        runtime = self.fleet.get(robot_id)
        if not runtime.session.is_connected:
            # Say so. A robot we cannot reach is not a robot we damped.
            return {
                "robot_id": robot_id,
                "ok": False,
                "link": "down",
                "detail": "not connected; damp was not sent",
            }
        delivered = await runtime.session.emergency_stop(reason="console_estop")
        return {
            "robot_id": robot_id,
            "ok": bool(delivered),
            "link": "up",
            "detail": "" if delivered else "robot did not acknowledge damp",
        }

    def release_estop(self, *, robot_id: str = "") -> Dict[str, Any]:
        """Clear the latch. Sends nothing — the robot is already damped."""
        scope = EStopScope.ROBOT if robot_id else EStopScope.ALL
        changed = self.state.estop.release(scope=scope, robot_id=robot_id)
        return {
            "released": changed,
            "reason": "" if changed else "no matching latch was engaged",
            "estop": self.state.estop.as_dict(),
        }

    async def audit(self, robot_id: str, *, limit: int = 30) -> Dict[str, Any]:
        """What this robot has been asked to do recently.

        The trail is already redacted when written (`AuditLog.record` replaces
        anything matching a secret marker), so this is safe to serve — and
        there is a test asserting no key material comes out, because "already
        redacted" is the kind of claim that quietly stops being true.

        Note the asymmetry with the MCP tool allowlist, which deliberately
        excludes `robot_audit_tail`. That boundary is about what an *LLM*
        should be able to reach through a generic tool passthrough. This is
        the operator's own console, on loopback unless a token says otherwise,
        reading the operator's own log.
        """
        runtime = self.fleet.get(robot_id)
        limit = max(1, min(int(limit), 200))
        # Read off the loop: `tail` touches the filesystem, and the console
        # polls this.
        records = await asyncio.to_thread(runtime.audit.tail, limit)
        return {
            "robot_id": robot_id,
            "count": len(records),
            "records": records,
        }

    # ── operator settings ────────────────────────────────────────────

    async def set_input_mode(self, mode: str) -> Dict[str, Any]:
        """Change how input maps, and release whatever was being driven.

        A mode change is a change of control context: the keys the operator
        was holding no longer mean what they meant. Leaving the previous
        source's robots claimed would keep them travelling until the dead-man
        noticed, so release them here and say which were zeroed.
        """
        try:
            self.state.input_mode = InputMode(mode)
        except ValueError:
            raise AgentError(
                f"Unknown input mode {mode!r}. Expected one of: "
                f"{', '.join(m.value for m in InputMode)}.",
                code="bad_input_mode",
                hint="Use focus, both, split or per_source.",
            ) from None
        stale = self.state.stop_driving()
        await self._zero_many(stale)
        return {**self.state.as_dict(), "zeroed": sorted(stale)}

    def set_focus(self, robot_id: str) -> Dict[str, Any]:
        self.fleet.get(robot_id)
        self.state.focused = robot_id
        return self.state.as_dict()

    def bind_source(self, source: str, robot_id: str) -> Dict[str, Any]:
        source = (source or "").strip()
        if not source:
            raise AgentError("A source name is required.", code="bad_source")
        if robot_id:
            self.fleet.get(robot_id)
            self.state.source_bindings[source] = robot_id
        else:
            self.state.source_bindings.pop(source, None)
        return self.state.as_dict()

    def set_video(self, robot_id: str, enabled: bool) -> Dict[str, Any]:
        self.fleet.get(robot_id)
        self.state.video_enabled[robot_id] = bool(enabled)
        return self.state.as_dict()

    # ── video ────────────────────────────────────────────────────────

    async def frame(self, robot_id: str) -> Optional[bytes]:
        """One JPEG, single-flight per robot."""
        if not self.state.video_enabled.get(robot_id, True):
            return None
        lock = self._frame_locks.setdefault(robot_id, asyncio.Lock())
        runtime = self.fleet.get(robot_id)
        async with lock:
            if not runtime.session.is_connected and not self.config.video_autoconnect:
                # Observing only: the operator decides when to take the slot.
                return None
            try:
                await runtime.session.ensure_connected()
            except Exception as exc:  # noqa: BLE001
                logger.info("frame(%s): not connected (%r)", robot_id, exc)
                return None
            try:
                return await runtime.backend.snapshot_frame(timeout=2.0)
            except Exception as exc:  # noqa: BLE001
                logger.info("frame(%s) failed: %r", robot_id, exc)
                return None

    async def mjpeg(self, robot_id: str) -> AsyncIterator[bytes]:
        """A multipart MJPEG stream for one robot."""
        boundary = b"--unitree-fleet-frame"
        interval = 1.0 / max(0.5, self.config.stream_fps)
        while True:
            started = time.monotonic()
            jpeg = await self.frame(robot_id)
            if jpeg:
                yield (
                    boundary
                    + b"\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(jpeg)).encode()
                    + b"\r\n\r\n"
                    + jpeg
                    + b"\r\n"
                )
            else:
                # Keep the connection alive so the <img> tag does not tear
                # down and re-handshake on every hiccup.
                await asyncio.sleep(0.25)
                continue
            await asyncio.sleep(max(0.0, interval - (time.monotonic() - started)))

    # ── internals ────────────────────────────────────────────────────

    async def _autoconnect(self, robot_id: str) -> None:
        """Start connecting a robot the operator is trying to drive.

        Without this, grabbing the sticks for a dog that happens to be
        disconnected does *nothing*: every frame returns ``delivered=false``
        and the only clue is a pill that says 离线. "Point at the dog and
        drive" is the whole point of a console.

        Non-blocking on purpose — a handshake takes seconds and this is called
        from a 20 Hz loop, so the first frames are simply dropped while the
        link comes up. Attempts are de-duplicated so a held key cannot start a
        connect storm, and the failure is left to the session's own reporting
        rather than raised into the teleop path.
        """
        runtime = self.fleet.get(robot_id)
        if runtime.session.is_connected or robot_id in self._connecting:
            return
        self._connecting.add(robot_id)

        async def _run() -> None:
            try:
                await runtime.session.ensure_connected()
            except Exception as exc:  # noqa: BLE001 - the link pill shows it
                logger.info("teleop auto-connect for %s failed: %r", robot_id, exc)
            finally:
                self._connecting.discard(robot_id)

        asyncio.create_task(_run())

    async def _zero(self, robot_id: str) -> None:
        """Best-effort zero of one robot's sticks."""
        try:
            runtime = self.fleet.get(robot_id)
        except UnknownRobot:
            return
        with contextlib.suppress(Exception):
            await runtime.session.zero_joystick()

    async def _zero_many(self, robot_ids: Any) -> None:
        """Zero several robots concurrently; never let one failure stop another."""
        targets = [r for r in robot_ids if r]
        if not targets:
            return
        await asyncio.gather(
            *(self._zero(robot_id) for robot_id in targets), return_exceptions=True
        )

    async def _zero_driver(self) -> None:
        await self._zero_many(self.state.stop_driving())
