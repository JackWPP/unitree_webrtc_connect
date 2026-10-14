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
import json
import logging
from pathlib import Path
import math
import time
from typing import Any, AsyncIterator, Dict, List, Optional

from unitree_robot_agent.bridge import ToolBridge
from unitree_robot_agent.core.telemetry import collect_lowstate
from unitree_robot_agent.errors import AgentError, UnknownRobot
from unitree_robot_agent.fleet import FleetManager
from unitree_robot_agent.server import build_server

from .config import ConsoleConfig
from .formation import Formation, Plan
from .motion import MotionController, MotionLimits
from .posture import PostureBelief
from .routines import ROUTINES, validate as validate_routine
from .state import ConsoleState, EStopScope, InputMode, MotionBlocked
from .sync import SyncMonitor

logger = logging.getLogger("unitree_fleet_web.service")


def _maybe_round(value, digits: int):
    """`round`, or `None` if there is no number.

    The parallax bound is `None` when the calibration's geometry does not exist
    — a camera at or below tag height. Rounding that raises, and the raise would
    come out of an endpoint as a 500 rather than as the missing number it is.
    """
    return round(value, digits) if isinstance(value, (int, float)) else None


def _shoelace(points) -> float:
    """Twice the area of a quadrilateral, unsigned.

    Used to tell a real four-cornered shape from three points in a line, which
    is the input that produces a homography rather than an error: a degenerate
    set still solves to a matrix, and it inverts to nonsense rather than
    failing.
    """
    total = 0.0
    for index in range(len(points)):
        x1, y1 = points[index]
        x2, y2 = points[(index + 1) % len(points)]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0

#: Joystick axes are normalised; the robot applies its own limits on top.
#:
#: The authoritative copy of this and the rates beside it is in `config.py`,
#: which is what the console actually runs on. A duplicate `DEFAULT_MOTION_HZ`
#: used to live here with a comment in `config.py` pointing at it — so editing
#: the copy nobody read looked like it should change something, and did not.
AXIS_LIMIT = 1.0

#: Tools that call ``Guard.require_motion_enabled`` and so refuse in safe mode.
#:
#: Listed rather than inferred because there is no annotation for it — safe
#: mode is a runtime check inside each tool body (`core/guards.py`), not
#: something the tool schema carries. If a tool is added or loses its guard,
#: this list is what has to change, which is why there is a test asserting
#: every name here is a real control somewhere.
MOTION_GATED_TOOLS = frozenset(
    {
        "robot_stand",
        "robot_lie_down",
        "robot_move",
        "robot_turn",
        "robot_walk",
        "robot_play_action",
        "robot_switch_state",
        "go2_recover",
        "go2_motion_mode",
        "go2_obstacle_avoid",
    }
)

#: Tools that call ``Guard.require_dangerous_allowed`` and so refuse unless
#: ``UNITREE_AGENT_ALLOW_DANGEROUS`` is set.
DANGEROUS_GATED_TOOLS = frozenset({"robot_lie_down"})

#: How often the ramped velocity is re-sent while a robot is being driven.
#:
#: 50 Hz is what the official SDK's own examples use and what a physical
#: remote does. The robot is a best-effort channel with no retransmit, so a
#: single dropped frame must not be a visible stutter — it is re-sent anyway,
#: which is why this is a fixed rate and not send-on-change.


#: Ceiling on one robot's telemetry refresh.
#:
#: This has to clear `collect_lowstate`'s *own* worst case, because that
#: function waits per topic and silently omits whatever did not arrive:
#: Go2 reads three topics (2 + 1 + 1 s) and R1 four (2 + 2 + 1 + 1 s), so six
#: seconds before a model that answers nothing has finished answering.
#:
#: The first version of this was 2.0 s, which was wrong for both models and
#: wrong in the worst way: on a real Go2 whose `rt/sportmodestate` never
#: publishes — which is the case for the fleet this was built for — every
#: refresh timed out, the panel stayed empty, and the failure was logged at
#: DEBUG. The mock answers every topic instantly, so no test could have caught
#: it. There is now a test pinning this above the collector's budget.
TELEMETRY_TIMEOUT_S = 8.0

#: Asserted against the collector's own declared budget rather than a number
#: kept in step by hand. `telemetry.WORST_CASE_S` is 6 s today (an R1 reading
#: four topics); if a topic is added there, this fails loudly instead of the
#: panels going empty on hardware again.

#: How long without a video frame before the panel stops pretending.
#:
#: A robot can be connected, answering commands, and still never deliver a
#: frame — no camera on that chassis, the video channel not opened, or the
#: track silently ended. The browser would sit on a blank <img> forever while
#: the link pill said 在线. Five seconds is long enough not to flicker during a
#: normal hiccup.
VIDEO_STALE_S = 5.0

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
        "go2_lidar",
        "go2_recover",
        "go2_set_volume",
        "go2_set_brightness",
        "go2_get_state",
        # safety
        "robot_stop",
        "robot_stop_all",
        # Bounded, self-terminating movement. Deliberately allowed alongside
        # the dead-man teleop rather than instead of it: a nudge carries its
        # own duration and stops on its own, so it is the *safer* of the two
        # ways to move a robot — there is no key to release and nothing that
        # can be left held down.
        #
        # `robot_walk` is the same shape with a better instrument: it stops when
        # it has measured the distance, and every path out of it zeroes the
        # sticks. What it does *not* have is a duration the operator chose, so
        # its stop condition is the robot's own odometry — which is why it is
        # listed next to the timed moves rather than above them.
        "robot_move",
        "robot_turn",
        "robot_walk",
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
        #: The tasks themselves, so shutdown can cancel them. A handshake takes
        #: seconds, and a console that closes mid-connect must not leave a task
        #: waking up later against a session it has already closed.
        self._connect_tasks: Dict[str, asyncio.Task] = {}
        #: Latest battery/IMU/gait snapshot per robot, refreshed by a slow
        #: loop. Kept here so the 4 Hz status push stays pure memory: doing
        #: topic reads on that path would put robot I/O inside a UI loop.
        self._telemetry: Dict[str, dict] = {}
        self._telemetry_task: Optional[asyncio.Task] = None
        #: Monotonic time of the last status request, socket or HTTP.
        self._last_watch_at = 0.0
        #: Why the last telemetry refresh failed, per robot. An empty panel is
        #: indistinguishable from a robot that reports nothing, and those want
        #: different reactions from the operator.
        self._telemetry_error: Dict[str, str] = {}
        #: The running routine, if any. Plain state so the status push can
        #: report progress without reaching into the runner.
        self._routine: Optional[Dict[str, Any]] = None
        self._routine_task: Optional[asyncio.Task] = None
        #: One velocity ramp per robot. Sharing one would couple the dogs'
        #: accelerations to each other.
        self._motion: Dict[str, MotionController] = {}
        #: What the console believes each robot's posture is, learned from its
        #: own commands. See `posture.PostureBelief`.
        self._posture: Dict[str, PostureBelief] = {}
        #: How far apart the dogs have got, measured against a shared reference
        #: the operator marks. See `sync.SyncMonitor`.
        self._sync = SyncMonitor()
        #: The arena camera and what it currently sees. Empty until a camera is
        #: configured, and the panel says which of the reasons applies rather
        #: than drawing an arena with no dogs in it.
        self._camera = None
        self._calibration = None
        self._arena: Dict[str, Any] = {}
        self._arena_task: Optional[asyncio.Task] = None
        self._arena_error = ""
        #: The auto-stand gate's bookkeeping, per robot.
        self._gate: Dict[str, Dict[str, float]] = {}
        #: In-flight auto-stand requests, so shutdown can cancel them.
        self._gate_tasks: set = set()
        #: When each robot's telemetry snapshot was read. The panels show
        #: numbers from four different rates — 50 Hz motion, 10 Hz camera, 1 Hz
        #: telemetry, on-demand reads — and without this the page cannot tell
        #: the operator which of them is the old one.
        self._telemetry_at: Dict[str, float] = {}
        #: The running formation alignment, and what it is doing.
        self._formation: Dict[str, Any] = {}
        self._formation_task: Optional[asyncio.Task] = None
        self._motion_task: Optional[asyncio.Task] = None
        self._motion_limits = MotionLimits(
            accel=float(getattr(self.config, "motion_accel", MotionLimits.accel)),
            decel=float(getattr(self.config, "motion_decel", MotionLimits.decel)),
            watchdog_s=self.config.deadman_s,
        )
        #: Per-robot video health, so a stream that never produces frames can
        #: say so instead of showing an empty box.
        self._video: Dict[str, Dict[str, Any]] = {}
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

        # A latched emergency stop has to cover the panel, not just the
        # sticks. Without this the operator could hit 急停 and then press 站立
        # or 打招呼 and the robot would move: `call_tool` went straight to the
        # tool, which knows nothing about the console's latch. The tools have
        # their own guards (safe mode, confirm, the dangerous gate) but none of
        # them is this.
        if tool in MOTION_GATED_TOOLS and self.state.estop.covers(robot_id):
            raise MotionBlocked(
                f"急停已锁存，{tool} 被拒绝。",
                detail={"tool": tool, "robot_id": robot_id, "estop": self.state.estop.as_dict()},
            )

        payload = dict(arguments or {})
        # The binding wrapper reads robot_id from the call arguments, so the
        # tool acts on the robot the operator clicked.
        payload["robot_id"] = robot_id
        result = await self._bridge.call(tool, payload)
        if isinstance(result, dict):
            if result.get("ok"):
                # Only on success: a refused `robot_lie_down` did not lie the
                # robot down, and believing it did would make the standing gate
                # send a pointless stand on the next drive.
                self._note_posture(robot_id, tool, payload)
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
        if self.config.arena_camera and (
            self._arena_task is None or self._arena_task.done()
        ):
            self.start_arena()
            self._arena_task = asyncio.create_task(
                self._arena_loop(), name="console-arena"
            )
        if self.config.motion_hz > 0 and (
            self._motion_task is None or self._motion_task.done()
        ):
            self._motion_task = asyncio.create_task(
                self._motion_loop(), name="console-motion"
            )

    async def aclose(self) -> None:
        if self._formation_task is not None:
            self._formation_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._formation_task
            self._formation_task = None

        if self._arena_task is not None:
            self._arena_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._arena_task
            self._arena_task = None
        if self._camera is not None:
            # Explicitly, not on garbage collection: an open webcam is held
            # against every other program on the machine.
            await self._camera.aclose()
            self._camera = None

        for task in list(self._gate_tasks):
            task.cancel()
        for task in list(self._gate_tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._gate_tasks.clear()

        # A routine keeps issuing commands, so it goes before anything else —
        # including before the sessions it is driving are closed.
        if self._routine_task is not None and not self._routine_task.done():
            if self._routine is not None:
                self._routine["running"] = False
                self._routine["aborted"] = "控制台关闭"
            self._routine_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._routine_task
        self._routine_task = None

        # Cancel in-flight auto-connects next: their whole point is to keep
        # running while the loop does other things, so nothing else stops them.
        for task in list(self._connect_tasks.values()):
            task.cancel()
        for task in list(self._connect_tasks.values()):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._connect_tasks.clear()
        self._connecting.clear()

        for task in (self._deadman, self._lease, self._telemetry_task, self._motion_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._deadman = None
        self._lease = None
        self._telemetry_task = None
        self._motion_task = None
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
        """Hold connected robots while watched, and hand them back promptly
        when nobody is left.

        Two halves of the same idea.

        A robot session disconnects after ``idle_disconnect_s`` precisely so
        the owner can take over with the official app. That is right for an
        agent asking one question; it is wrong for an operator with the console
        open, where the link dropping mid-session kills the video stream and
        makes the next command pay for a fresh handshake. So while a browser is
        watching, the claim is re-asserted.

        The other half is what happens when the browser goes away. Waiting out
        the 45 s idle timer means an owner standing next to the dog waits the
        better part of a minute to take over by hand — so after
        ``unwatched_release_s`` the console releases the links itself.

        Neither half ever *connects* anything: a robot nobody asked for stays
        disconnected.
        """
        # A fixed one-second tick would quantise a short grace period into
        # "somewhere in the next second". Half the grace period, capped at a
        # second, keeps it accurate without spinning.
        grace = self.config.unwatched_release_s
        tick = 1.0 if grace <= 0 else min(1.0, max(0.1, grace / 2.0))
        lease_every = max(1, int(round(self.config.lease_s / tick)))
        ticks = 0
        unwatched_since: Optional[float] = None
        while True:
            try:
                await asyncio.sleep(tick)
                ticks += 1

                if self.watching:
                    unwatched_since = None
                    if ticks % lease_every == 0:
                        for robot_id in self.fleet.ids:
                            runtime = self.fleet.get(robot_id)
                            if runtime.session.is_connected:
                                runtime.session.touch()
                    continue

                if self.config.unwatched_release_s <= 0:
                    continue
                if unwatched_since is None:
                    unwatched_since = time.monotonic()
                    continue
                if time.monotonic() - unwatched_since >= self.config.unwatched_release_s:
                    await self._release_unwatched()
                    unwatched_since = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a lease must not crash
                logger.warning("lease loop error: %r", exc)

    async def _release_unwatched(self) -> None:
        """Hand every held link back, stopping each robot on the way out.

        ``stop_first`` is the session's default and is what is wanted here: a
        dog must not be left travelling because a browser was closed.
        """
        # A running routine is activity, whatever the watcher count says.
        #
        # Without this, closing the laptop lid — or a websocket blinking — while
        # a routine is mid-sequence released every robot under it, stopped them
        # (`stop_first`), and left the runner issuing commands at disconnected
        # robots for the rest of the sequence. Half-released is worse than
        # either extreme: the pair is neither doing the routine nor safely idle.
        #
        # The lease still re-asserts nothing here; it simply waits. If the
        # operator never comes back, the routine finishes, `stop_routine` runs
        # on the next release, and the links go back the ordinary way.
        if self._routine is not None and self._routine.get("running"):
            return

        released = []
        for robot_id in self.fleet.ids:
            runtime = self.fleet.get(robot_id)
            if not runtime.session.is_connected:
                continue
            with contextlib.suppress(Exception):
                await runtime.session.disconnect(
                    reason="console_unwatched", stop_first=True
                )
                released.append(robot_id)
        if released:
            logger.info(
                "released %s: nothing has been watching for %.0fs, so the "
                "official app can take over",
                ", ".join(released),
                self.config.unwatched_release_s,
            )
            # Freshness is meaningless for a link we no longer hold.
            for robot_id in released:
                self._telemetry.pop(robot_id, None)
                self._telemetry_error.pop(robot_id, None)

    # ── routine documents ────────────────────────────────────────────

    def _routine_with_params(self, routine, params: Optional[Dict[str, Any]]):
        """Apply the operator's parameter values, or refuse with the field named.

        The validation itself lives on ``Routine.with_params`` so that the
        preview and the run go through **the same code** — a preview built by one
        path and a run by another is a preview of something else.

        A ``ValueError`` from there is the operator's mistake, not the server's,
        so it becomes a typed refusal rather than the 500 the error mapper would
        otherwise produce.
        """
        if not params:
            return routine
        try:
            return routine.with_params(params)
        except ValueError as exc:
            raise AgentError(
                str(exc),
                code="bad_routine_params",
                hint="每个参数都要在它自己的范围内。GET /api/fleet/routines 里有范围和单位。",
            ) from exc

    def preview_routine(
        self, routine_id: str, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """What this routine would do, without doing it.

        The page shows the step list and the total time before the operator
        commits, and it needs them **for the parameters currently in the boxes**
        — 0.8 m sides is a different routine from 0.4 m ones.

        **The arithmetic deliberately happens here and not in the browser.**
        Computing "parameters → steps" in two places is two implementations of
        one fact, and they drift: this project has already shipped a parallax
        formula that disagreed with its own scale factor ten lines away, and a
        `converged()` that disagreed with the `plan()` it was gating. A preview
        is a pure function that touches no robot, so it is cheap to serve.
        """
        routine = self._resolve_routine(routine_id)
        if routine is None:
            raise AgentError(
                f"没有名为 {routine_id!r} 的编排。",
                code="unknown_routine",
                hint="GET /api/fleet/routines 列出可用的编排。",
            )
        built = self._routine_with_params(routine, params)
        return {
            "id": built.id,
            "label": built.label,
            "steps": [step.as_dict() for step in built.steps],
            "total": len(built.steps),
            "seconds": round(built.seconds, 1),
            "loop": built.loop,
            "needs_dangerous": built.needs_dangerous,
        }

    def _resolve_routine(self, routine_id: str):
        """A built-in first, then the operator's own. None if neither.

        Returning None rather than raising keeps the caller's error message in
        one place — and an unbound local in a `try/except` is exactly how the
        first version of this turned "no such routine" into `internal_error`.
        """
        from .routines import routine_by_id

        try:
            return routine_by_id(routine_id)
        except KeyError:
            pass
        document = next(
            (item for item in self.custom_routines() if item["id"] == routine_id), None
        )
        if document is None:
            return None
        try:
            return validate_routine(document, known_tools=CONSOLE_TOOLS)
        except ValueError as exc:
            # It validated when it was saved; a tool has since gone away.
            raise AgentError(
                f"编排 {routine_id!r} 已失效：{exc}",
                code="invalid_routine",
                hint="重新保存它，或删掉它。",
            )


    @property
    def _routine_file(self):
        from pathlib import Path

        return Path(self.fleet.roster.state_dir) / "routines.json"

    def custom_routines(self) -> List[Dict[str, Any]]:
        """Operator-authored routines, from disk.

        A routine is a document so it can be authored, saved, diffed and
        rendered — the branch this idea came from kept its sequences as Python
        lists, which is why none was ever written by anyone but its author.
        """
        try:
            raw = json.loads(self._routine_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        stored = raw.get("routines") if isinstance(raw, dict) else raw
        if not isinstance(stored, list):
            return []
        out = []
        for document in stored:
            try:
                out.append(validate_routine(document, known_tools=CONSOLE_TOOLS).as_dict())
            except ValueError as exc:
                # A saved routine that no longer validates is reported, not
                # silently dropped: the operator wrote it and deserves to know
                # which tool disappeared.
                logger.warning("stored routine rejected: %s", exc)
        return out

    def all_routines(self) -> List[Dict[str, Any]]:
        theirs = {routine["id"] for routine in self.custom_routines()}
        builtin = [r.as_dict() for r in ROUTINES if r.id not in theirs]
        return builtin + self.custom_routines()

    def save_routine(self, document: Dict[str, Any]) -> Dict[str, Any]:
        # `validate` raises ValueError, which the error mapper would turn into
        # a 500. A rejected document is the operator's mistake, not the
        # server's, so it is translated into a typed refusal.
        try:
            routine = validate_routine(document, known_tools=CONSOLE_TOOLS)
        except ValueError as exc:
            raise AgentError(
                str(exc),
                code="invalid_routine",
                hint="每一步都需要一个控制台可用的 tool，以及 all / any / best_effort 之一的 policy。",
            )
        if routine.id in {builtin.id for builtin in ROUTINES}:
            raise AgentError(
                f"{routine.id!r} 是内置编排的名字。",
                code="reserved_routine_id",
                hint="换一个 id；内置编排不能被覆盖。",
            )
        stored = self.custom_routines()
        stored = [item for item in stored if item["id"] != routine.id]
        stored.append(routine.as_dict())
        self._routine_file.parent.mkdir(parents=True, exist_ok=True)
        self._routine_file.write_text(
            json.dumps({"routines": stored}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return routine.as_dict()

    def delete_routine(self, routine_id: str) -> Dict[str, Any]:
        stored = self.custom_routines()
        remaining = [item for item in stored if item["id"] != routine_id]
        if len(remaining) == len(stored):
            raise AgentError(
                f"没有名为 {routine_id!r} 的自定义编排。",
                code="unknown_routine",
                hint="内置编排不能删除。",
            )
        self._routine_file.write_text(
            json.dumps({"routines": remaining}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return {"deleted": routine_id, "remaining": len(remaining)}

    # ── routines ─────────────────────────────────────────────────────

    async def start_routine(
        self, routine_id: str, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Run a sequence of steps against the whole fleet.

        The one control in this console that keeps sending commands after the
        operator has taken their hands off the keyboard, so it is also the one
        that most needs to be stoppable. Three things abort it: the stop button,
        a latched e-stop, and the console closing.

        ``params`` are the operator's values for whatever knobs this routine
        declares, validated here rather than in the page — see
        ``Routine.with_params``.
        """
        routine = self._resolve_routine(routine_id)
        if routine is None:
            raise AgentError(
                f"没有名为 {routine_id!r} 的编排。",
                code="unknown_routine",
                hint="GET /api/fleet/routines 列出可用的编排。",
            )
        routine = self._routine_with_params(routine, params)

        if self._routine_task is not None and not self._routine_task.done():
            raise AgentError(
                "已经有一条编排在跑。",
                code="routine_running",
                hint="先停止当前编排。",
            )
        if self._formation_task is not None and not self._formation_task.done():
            raise AgentError(
                "编队正在运行，不能同时开始编排。",
                code="formation_running",
                hint="先停止编队，或者等它跑完。",
            )
        if self.state.estop.engaged:
            raise MotionBlocked(
                "急停已锁存，无法开始编排。",
                detail={"estop": self.state.estop.as_dict()},
            )

        targets = [rid for rid in self.fleet.ids if self.fleet.get(rid).session.is_connected]
        if not targets:
            raise AgentError(
                "没有已连接的机器狗。",
                code="no_connected_robots",
                hint="先连接至少一只，再开始编排。",
            )

        self._routine = {
            "id": routine.id,
            "label": routine.label,
            "loop": routine.loop,
            "total": len(routine.steps),
            "step": -1,
            "step_label": "准备中",
            "running": True,
            "aborted": "",
            "needs_dangerous": routine.needs_dangerous,
            "dangerous_allowed": self._dangerous_allowed(),
            "seconds": round(routine.seconds, 1),
            "started_at": time.time(),
            # The whole sequence, before any of it runs. A timeline has to be
            # visible ahead of the playhead or it is just a log with a label.
            "steps": [
                {
                    "index": index,
                    "label": step.label,
                    "tool": step.tool,
                    "policy": step.policy,
                    "seconds": step.seconds,
                    "dangerous": step.dangerous,
                    "state": "pending",
                    "ok_count": 0,
                    "total": len(targets),
                    "per_dog": {},
                    "started_at": None,
                    "ended_at": None,
                }
                for index, step in enumerate(routine.steps)
            ],
            # Things the operator asked for mid-run, to be performed after the
            # current step. The branch this came from had the right instinct
            # (`dance_triggered`) but hid it in a bool that leaked across
            # restarts and was honoured by only one of its two loops.
            "pending": [],
        }
        self._routine_task = asyncio.create_task(
            self._run_routine(routine), name=f"console-routine-{routine.id}"
        )
        return self.routine_state()

    async def stop_routine(self, *, reason: str = "operator") -> Dict[str, Any]:
        """Abort the running routine and stop everything it was driving.

        Order matters: flag it stopped *first*, so the runner cannot issue
        another step while the stop commands are going out.
        """
        state = self._routine
        if state is not None:
            state["running"] = False
            state["aborted"] = reason
            state["step_label"] = "已停止"
        task = self._routine_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._routine_task = None
        stopped = await self.fleet.stop_all()
        return {
            "stopped": True,
            "reason": reason,
            "routine": self.routine_state(),
            "robots": [o.as_dict() for o in stopped],
        }

    def queue_insert(self, *, tool: str, args: Optional[Dict[str, Any]] = None,
                     label: str = "") -> Dict[str, Any]:
        """Ask for one more action after the current step finishes.

        The operator's ad-hoc "both dogs bow now" wants to land inside the
        running sequence rather than replace it. It waits for a step boundary
        because there is nowhere else safe to put it — a command in the middle
        of a dance is a command in the middle of a dance.
        """
        if tool not in CONSOLE_TOOLS:
            raise AgentError(
                f"{tool!r} is not available from the console.",
                code="tool_not_allowed",
                hint="Only the operations an operator needs are exposed here.",
            )
        state = self._routine
        if state is None or not state.get("running"):
            raise AgentError(
                "没有正在运行的编排。",
                code="no_routine_running",
                hint="直接执行这个动作即可，不需要排队。",
            )
        state["pending"].append({"tool": tool, "args": dict(args or {}), "label": label})
        return {"queued": True, "pending": len(state["pending"]),
                "routine": self.routine_state()}

    def routine_state(self) -> Dict[str, Any]:
        return dict(self._routine) if self._routine else {"running": False}

    def _dangerous_allowed(self) -> bool:
        for robot_id in self.fleet.ids:
            if getattr(self.fleet.get(robot_id).config, "allow_dangerous", False):
                return True
        return False

    async def _run_routine(self, routine) -> None:
        state = self._routine
        if state is None:
            return
        try:
            while state["running"]:
                for index, step in enumerate(routine.steps):
                    if not state["running"]:
                        break
                    if self.state.estop.engaged:
                        state["aborted"] = "急停锁存"
                        state["running"] = False
                        state["step_label"] = "已中止"
                        break

                    state["step"] = index
                    state["step_label"] = step.label
                    record = state["steps"][index] if index < len(state["steps"]) else None
                    if record is not None:
                        record["state"] = "running"
                        record["started_at"] = time.time()

                    result = await self.fleet_action(step.tool, dict(step.args))
                    ok_count = result["ok_count"]
                    total = len(result["targets"])
                    # The policy decides whether a partial result is a success.
                    # `sum(results) > 0` — what the branch this came from used —
                    # silently turns a two-dog routine into a one-dog routine
                    # and reports success either way.
                    satisfied = step.satisfied(ok_count, total)
                    if record is not None:
                        record["ok_count"] = ok_count
                        record["total"] = total
                        record["per_dog"] = result["results"]
                        record["skipped"] = result["skipped"]
                        record["state"] = "done" if satisfied else "failed"
                        record["ended_at"] = time.time()

                    # A turn is where two dogs come apart, because each one is
                    # closed-loop on its own gyro. Measure the spread the step
                    # left behind and nudge the laggard back — once, bounded.
                    correction = None
                    if satisfied and self._sync.marked:
                        correction = await self._correct_turn(step, result)
                        if record is not None and correction:
                            record["correction"] = correction

                    # Wait in slices, so a stop lands within a fraction of a
                    # second instead of after the whole step. A routine that
                    # cannot be interrupted mid-step is not really stoppable.
                    remaining = float(step.seconds)
                    while remaining > 0 and state["running"]:
                        if self.state.estop.engaged:
                            state["aborted"] = "急停锁存"
                            state["running"] = False
                            state["step_label"] = "已中止"
                            break
                        await asyncio.sleep(min(0.2, remaining))
                        remaining -= 0.2

                # Anything the operator asked for while that step was running,
                # performed now — at a step boundary, which is the only safe
                # place to insert into a sequence.
                while state["pending"] and state["running"]:
                    pending = state["pending"].pop(0)
                    state["step_label"] = pending.get("label") or "插入的动作"
                    await self.fleet_action(
                        pending.get("tool", ""), dict(pending.get("args") or {})
                    )

                if not routine.loop:
                    break
            state["running"] = False
            if not state["aborted"]:
                state["step_label"] = "已完成"
        except asyncio.CancelledError:
            state["running"] = False
            state["step_label"] = "已取消"
            for record in state["steps"]:
                if record["state"] == "running":
                    record["state"] = "cancelled"
                    record["ended_at"] = time.time()
            raise
        except Exception as exc:  # noqa: BLE001 - a routine must not kill the console
            logger.warning("routine %s failed: %r", routine.id, exc)
            state["running"] = False
            state["aborted"] = f"{type(exc).__name__}: {exc}"
            state["step_label"] = "出错停止"
            with contextlib.suppress(Exception):
                await self.fleet.stop_all()

    # ── fleet-wide actions ───────────────────────────────────────────

    async def fleet_action(self, tool: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Run one tool on every *connected* robot at once.

        The second of the two things this console is for. Driving two dogs is
        per-dog by nature — one operator, one set of sticks, one target — but
        "make them both dance" is a single intent that has no per-dog form, and
        doing it by clicking two buttons a second apart is not the same thing
        as doing it together.

        The calls go out concurrently rather than in a loop, so they land as
        close together as the network allows; a sequential loop would finish
        dog 1's dance before starting dog 2's.

        Robots that are **not connected** are named in ``skipped`` rather than
        silently omitted: an operator who clicked "一起站立" and sees two dogs
        standing needs to know a third was asleep, not nothing at all.
        """
        if tool not in CONSOLE_TOOLS:
            raise AgentError(
                f"{tool!r} is not available from the console.",
                code="tool_not_allowed",
                hint="Only the operations an operator needs are exposed here.",
            )
        targets = [
            rid for rid in self.fleet.ids if self.fleet.get(rid).session.is_connected
        ]
        skipped = [rid for rid in self.fleet.ids if rid not in targets]
        if not targets:
            raise AgentError(
                "没有已连接的机器狗。",
                code="no_connected_robots",
                hint="先连接至少一只，再执行舰队动作。",
            )

        args = dict(arguments or {})

        async def one(robot_id: str):
            try:
                return robot_id, await self.call_tool(robot_id, tool, args)
            except AgentError as exc:
                # A per-robot refusal is a result, not a failure of the whole
                # action — the other dogs may well have done it.
                return robot_id, {
                    "ok": False, "error": getattr(exc, "code", "error"),
                    "message": str(exc),
                }
            except Exception as exc:  # noqa: BLE001
                return robot_id, {
                    "ok": False, "error": type(exc).__name__, "message": str(exc),
                }

        pairs = await asyncio.gather(*(one(rid) for rid in targets))
        results = dict(pairs)
        succeeded = [rid for rid, value in results.items() if value.get("ok")]
        failed = [rid for rid, value in results.items() if not value.get("ok")]
        return {
            "tool": tool,
            "args": args,
            "targets": targets,
            "skipped": skipped,
            "results": results,
            "ok_count": len(succeeded),
            "failed": failed,
            "skipped_count": len(skipped),
            "all_ok": not failed,
        }

    async def fleet_actions(self) -> Dict[str, Any]:
        """The fleet-wide actions worth offering, and what they will run on.

        Built from the live catalogues rather than a hardcoded list, so a
        firmware that does not support 跳舞 simply does not get the button.
        """
        targets = [
            rid for rid in self.fleet.ids if self.fleet.get(rid).session.is_connected
        ]
        actions: List[Dict[str, Any]] = []

        # Posture first: standing a fleet up is the thing you reach for after
        # something went wrong, and it should not be below the dances.
        if "robot_stand" in self.tools_available:
            actions.append({
                "id": "stand", "tool": "robot_stand", "args": {"confirm": True},
                "label": "一起站立", "title": "BalanceStand：让所有已连接的狗站好",
            })
        if "go2_recover" in self.tools_available:
            actions.append({
                "id": "recover", "tool": "go2_recover",
                "args": {"mode": "recovery_stand", "confirm": True},
                "label": "一起恢复", "title": "摔倒恢复：侧翻/仰翻后翻身站起",
            })
        if "robot_lie_down" in self.tools_available:
            actions.append({
                "id": "lie_down", "tool": "robot_lie_down", "args": {"confirm": True},
                "label": "一起趴下", "title": "StandDown：所有狗一起趴下并收腿",
            })

        named: List[Dict[str, Any]] = []
        hard: List[Dict[str, Any]] = []
        if targets and "robot_list_actions" in self.tools_available:
            try:
                from .controls import (
                    _GO2_HARD_ACTIONS,
                    _GO2_NEEDS_SPACE,
                    _GO2_SAFE_ACTIONS,
                    action_label,
                )

                # **Ask without the dangerous filter.** `include_dangerous=False`
                # was hiding thirteen reachable actions — dances, the handstand,
                # four step moves and four flips — behind a console that simply
                # did not offer them. They have always run: `allow_dangerous`
                # defaults to true. The gate that matters is the group they land
                # in, not whether they are listed.
                listed = await self.call_tool(
                    targets[0], "robot_list_actions", {"include_dangerous": True}
                )
                labels = {
                    row.get("name"): row.get("description") or ""
                    for row in (listed or {}).get("actions") or []
                    if row.get("name")
                }
                # The dangerous ones, in the console's own risk order rather
                # than the catalogue's.
                for name, label in _GO2_HARD_ACTIONS:
                    if name not in labels:
                        # This firmware does not have it. The catalogue in
                        # `robots/go2.py` is a superset — an MCF or older build
                        # may not carry every flip.
                        continue
                    hard.append({
                        "id": f"action_{name}", "tool": "robot_play_action",
                        "args": {"name": name, "confirm": True},
                        "label": label,
                        "key": name,
                        "needs_space": name in _GO2_NEEDS_SPACE,
                        "title": labels.get(name) or f"所有狗一起执行 {name}",
                    })

                hard_names = {name for name, _label in _GO2_HARD_ACTIONS}
                safe_names = [name for name, _label in _GO2_SAFE_ACTIONS]
                # **In the console's order, not the catalogue's.** `labels` comes
                # back dict-ordered by the robot's own table, so the buttons
                # would shuffle between page loads — and a control surface whose
                # buttons move is one you have to read every time instead of
                # reaching for.
                ordered = [n for n in safe_names if n in labels] + [
                    n for n in labels if n not in hard_names and n not in safe_names
                ]
                for name in ordered:
                    if name in hard_names:
                        continue
                    # The catalogue descriptions are English debug text
                    # ("Wave hello (1016)."). On a button that is noise, and it
                    # would not match what the per-dog picker calls the same
                    # action — so both go through one label table.
                    named.append({
                        "id": f"action_{name}", "tool": "robot_play_action",
                        "args": {"name": name, "confirm": True},
                        "label": action_label(name),
                        "key": name,
                        "needs_space": name in _GO2_NEEDS_SPACE,
                        "title": labels[name] or f"所有狗一起执行 {name}",
                    })
            except Exception as exc:  # noqa: BLE001 - an empty bar beats a broken page
                logger.info("fleet actions: catalogue unavailable (%r)", exc)

        return {
            "targets": targets,
            "skipped": [rid for rid in self.fleet.ids if rid not in targets],
            "posture": actions,
            "named": named,
            "hard": hard,
        }

    # ── the velocity ramp ────────────────────────────────────────────

    def posture(self, robot_id: str) -> PostureBelief:
        belief = self._posture.get(robot_id)
        if belief is None:
            belief = PostureBelief(robot_id)
            self._posture[robot_id] = belief
        return belief

    def _note_posture(self, robot_id: str, tool: str, args: dict) -> None:
        self.posture(robot_id).note(tool, args)

    def motion(self, robot_id: str) -> MotionController:
        """The ramp for one robot, created on first use."""
        controller = self._motion.get(robot_id)
        if controller is None:
            controller = MotionController(self._motion_limits)
            self._motion[robot_id] = controller
        return controller

    async def _motion_loop(self) -> None:
        """Advance every robot's ramp and publish what it commands.

        The browser sets a target; the server owns what is actually sent. That
        is the whole point of doing the ramp here rather than in the page: a
        dropped link, a stalled tab or a second operator cannot produce a
        velocity the ramp did not authorise.
        """
        interval = 1.0 / max(5.0, self.config.motion_hz)
        last = time.monotonic()
        while True:
            try:
                await asyncio.sleep(interval)
                now = time.monotonic()
                dt = now - last
                last = now

                for robot_id, controller in list(self._motion.items()):
                    if await self._standing_gate(robot_id, controller, now):
                        # Not walkable: a stand has been asked for, and no
                        # velocity goes out this tick. Publishing it anyway is
                        # what produces 3201 and an operator deciding the
                        # console is broken.
                        continue
                    # **A tool holding a motion window owns the sticks.**
                    #
                    # `robot_walk`, `robot_turn` and `robot_move` publish
                    # wireless-controller frames directly, and so does this loop.
                    # Both write the same topic with no arbitration, so an
                    # operator holding W while a routine or a formation runs
                    # produces an interleaving of two intents — measured at 297
                    # frames from the tool with 3 from the loop spliced between
                    # them, which is a robot being told to walk and to stop
                    # alternately at 50 Hz.
                    #
                    # The window is the tool saying "I am driving"; the loop
                    # waits for it. The dead-man still applies, and the ramp
                    # keeps its state, so nothing is lost by standing aside.
                    with contextlib.suppress(Exception):
                        if self.fleet.get(robot_id).motion.window_active:
                            continue

                    # Compared against what was last **sent**, not against
                    # `commanded`. The dead-man calls `hard_stop()`, which
                    # zeroes `commanded` between ticks; comparing against that
                    # made the loop decide there was nothing to say and send
                    # nothing, so the robot went on applying the last frame it
                    # had received. That is the "alt-tab and the dog keeps
                    # walking" failure, and the watchdog existed to prevent it.
                    before = controller.published
                    vx, vy, vyaw = controller.tick(dt, now=now)
                    after = (round(vx, 6), round(vy, 6), round(vyaw, 6))
                    if after != before or controller.moving:
                        sent = await self._publish_commanded(robot_id, controller, vx, vy, vyaw)
                        if sent:
                            # Only on success: a dropped frame has to be retried
                            # on the next tick, or a momentary network hiccup
                            # would leave the robot on a stale value forever.
                            controller.published = after
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a mover must survive
                logger.warning("motion loop error: %r", exc)

    async def _standing_gate(
        self, robot_id: str, controller: MotionController, now: float
    ) -> bool:
        """Hold a drive back until the robot is standing. True means "held".

        `unitree_harness` calls this `auto_stand` and it is the difference
        between "I pressed W and it walked" and "I pressed W and nothing
        happened, and the robot did not say why".

        Bounded on purpose: after `stand_timeout_s` the target is dropped and
        the state says why, rather than the console quietly asking a robot to
        stand forever.
        """
        if not any(controller.target):
            self._gate.pop(robot_id, None)
            return False

        belief = self.posture(robot_id)
        if belief.walkable:
            self._gate.pop(robot_id, None)
            return False
        gate = self._gate.get(robot_id)
        if gate is None:
            gate = {"asked_at": now, "retry_at": now}
            self._gate[robot_id] = gate
            logger.info(
                "auto-stand: %s is %s, asking it to stand before driving",
                robot_id, belief.posture,
            )

        if now - gate["asked_at"] > self.config.stand_timeout_s:
            logger.warning("auto-stand: %s did not report standing, dropping target", robot_id)
            controller.stop()
            controller._state = "stand_timeout"  # noqa: SLF001
            self._gate.pop(robot_id, None)
            return True

        # Re-ask at most once a second. A robot takes a moment, and asking on
        # every 20 ms tick would be a flood.
        if now >= gate["retry_at"]:
            gate["retry_at"] = now + 1.0
            # Deliberately **not** awaited. `robot_stand` waits a second for
            # the firmware to acknowledge, so awaiting it here stalls the whole
            # motion loop — every other robot's ramp stops too, which is the
            # same fault as a blocking WebRTC handshake. It measured as a full
            # second of frozen output before this was noticed.
            task = asyncio.create_task(self._ask_stand(robot_id))
            self._gate_tasks.add(task)
            task.add_done_callback(self._gate_tasks.discard)
        controller._state = "standing_wait"  # noqa: SLF001
        return True

    async def _ask_stand(self, robot_id: str) -> None:
        """Ask a robot to stand, off the motion loop."""
        try:
            await self.call_tool(robot_id, "robot_stand", {"confirm": True})
        except Exception as exc:  # noqa: BLE001 - a refusal is not fatal
            logger.info("auto-stand: %s refused to stand (%r)", robot_id, exc)

    async def _correct_turn(
        self, step, result: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Nudge the laggard after a turn step, once.

        Only for turns. A straight-line step can be corrected the same way in
        principle, but the only reference available is each dog's own odometry
        and a distance error is as likely to be a wheel slipping as a dog
        lagging — turning is where the divergence is both real and cheap to
        fix, because the correction is a turn of a few degrees.

        Returns what it did, or None. The caller records it on the step so the
        timeline can show that a routine corrected itself rather than silently
        doing something the operator did not ask for.
        """
        if step.tool != "robot_turn":
            return None
        if not all((result.get("results") or {}).get(rid, {}).get("ok")
                   for rid in result.get("targets") or []):
            # A dog that refused the turn did not under-turn; turning it again
            # would be acting on a number that is not about what happened.
            return None

        summary = self.sync_state()
        if summary.get("state") in ("unmarked", "unknown", "aligned"):
            return None

        applied: List[Dict[str, Any]] = []
        for pair in summary.get("pairs") or []:
            from .sync import Pair

            nudge = SyncMonitor.correction(Pair(
                a=pair["a"], b=pair["b"],
                yaw_spread_deg=pair["yaw_spread_deg"],
                distance_spread_m=pair["distance_spread_m"],
                state=pair["state"],
                leader=pair["leader"], laggard=pair["laggard"],
                detail=pair["detail"],
            ))
            if not nudge:
                continue
            logger.info(
                "sync: correcting %s by %s° after %s",
                nudge["robot_id"], nudge["degrees"], step.label,
            )
            try:
                response = await self.call_tool(
                    nudge["robot_id"], "robot_turn",
                    {"degrees": nudge["degrees"], "confirm": True},
                )
            except Exception as exc:  # noqa: BLE001 - a failed nudge is not fatal
                logger.info("sync: correction for %s failed (%r)", nudge["robot_id"], exc)
                continue
            applied.append({**nudge, "ok": bool(response.get("ok", True))})
            # One nudge per step, whatever the pair count: a second one would
            # be chasing a number this cannot verify it reached.
            break

        if not applied:
            return None
        return {
            "applied": applied,
            "spread_before": summary.get("worst", {}).get("yaw_spread_deg"),
        }

    async def _publish_commanded(
        self, robot_id: str, controller: MotionController, vx: float, vy: float, vyaw: float
    ) -> bool:
        """Send the ramped value as a wireless-controller frame.

        The stick mapping is the inverse of the one `robot_joystick` documents:
        `[ly, -lx, -rx] -> [vx, vy, vyaw]`. Getting it backwards would make the
        robot accelerate when the operator brakes, so it is written once, here,
        and covered by a test.
        """
        try:
            await self._frame(
                robot_id,
                lx=-vy, ly=vx, rx=-vyaw, ry=0.0, keys=controller.keys,
            )
            return True
        except Exception as exc:  # noqa: BLE001 - a dropped frame is not fatal
            logger.debug("motion publish(%s) failed: %r", robot_id, exc)
            return False

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
            self._telemetry_error.pop(robot_id, None)
            return None
        try:
            snapshot = await asyncio.wait_for(
                collect_lowstate(runtime.session, runtime.profile, include_motors=False),
                timeout=TELEMETRY_TIMEOUT_S,
            )
        except Exception as exc:  # noqa: BLE001 - a quiet robot is not an error
            detail = (
                f"timed out after {TELEMETRY_TIMEOUT_S:.0f}s"
                if isinstance(exc, asyncio.TimeoutError)
                else f"{type(exc).__name__}: {exc}"
            )
            # Warning, not debug: this is why a panel is empty, and it is the
            # one thing the operator cannot deduce from the page.
            logger.warning("telemetry(%s) failed: %s", robot_id, detail)
            self._telemetry_error[robot_id] = detail
            return self._telemetry.get(robot_id)
        self._telemetry_error.pop(robot_id, None)
        # The FSM view is a separate topic and is what says "standing" on a
        # humanoid. On a Go2 it reports n/a, where the gait below carries it.
        with contextlib.suppress(Exception):
            snapshot["posture"] = await asyncio.wait_for(
                runtime.session.state_view(), timeout=TELEMETRY_TIMEOUT_S
            )
        self._telemetry[robot_id] = snapshot
        self._telemetry_at[robot_id] = time.monotonic()
        return snapshot

    def flags(self, robot_id: str) -> Dict[str, Any]:
        """What the robot says it is currently set to.

        Derived here rather than in the browser on purpose: the mapping from a
        tool's return shape to a switch value is model knowledge, and putting
        it in the frontend would mean one formatter per tool per model. The
        UI gets flat keys and renders chips.

        Sourced from the telemetry snapshot, which the slow sampler already
        reads — so this costs no extra robot I/O on the status path.
        """
        snapshot = self._telemetry.get(robot_id) or {}
        aggregate = snapshot.get("aggregate_state") or {}
        sport = snapshot.get("sport_state") or {}

        out: Dict[str, Any] = {}
        for key, value in (
            ("brightness", aggregate.get("brightness")),
            ("volume", aggregate.get("volume")),
            ("obstacle_avoid", aggregate.get("obstaclesAvoidSwitch")),
            ("motion_mode", sport.get("mode")),
        ):
            if value is not None:
                out[key] = value
        return out

    def where(self, robot_id: str) -> Dict[str, Any]:
        """Where the robot says it is, when it says anything at all.

        `position` rides on the state topic — the same one that carries the
        gait — so this costs no extra read and simply does not appear on a
        robot that never publishes it. That is the honest shape for a WebRTC
        capability: the data is either in the frame we already have or the
        field is absent, and the UI renders nothing rather than a zero that
        looks like a measurement.

        Frame caveat, stated because it matters: each robot's own odometry
        starts at its own zero, so two dogs' positions are **not** in a shared
        frame. They can be compared to their own start, never to each other,
        without something that gives a common origin — which the confirmed
        topics do not have.
        """
        snapshot = self._telemetry.get(robot_id) or {}
        sport = snapshot.get("sport_state") or {}
        position = sport.get("position")
        if not isinstance(position, (list, tuple)) or len(position) < 2:
            return {}

        try:
            x, y = float(position[0]), float(position[1])
            yaw = float(position[2]) if len(position) > 2 else 0.0
        except (TypeError, ValueError):
            return {}

        out: Dict[str, Any] = {
            "x": round(x, 3),
            "y": round(y, 3),
            "yaw": round(yaw, 3),
            "distance": round((x * x + y * y) ** 0.5, 3),
            "frame": "odom",
        }
        velocity = sport.get("velocity")
        if isinstance(velocity, (list, tuple)) and len(velocity) >= 3:
            try:
                out["speed"] = round(
                    (float(velocity[0]) ** 2 + float(velocity[1]) ** 2) ** 0.5, 3
                )
            except (TypeError, ValueError):
                pass
        return out

    def body(self, robot_id: str) -> Dict[str, Any]:
        """The robot's live physical state, from the topics we know work.

        Everything here rides on `rt/lf/lowstate`, `rt/multiplestate` and the
        state topics — the three that were measured arriving on real hardware.
        Nothing speculative, so this panel is populated on the dog rather than
        only under the mock.

        Three things an operator cannot otherwise see, all of which answer a
        question they will actually ask:

        * **attitude** — is it level, and is it tipping? Pitch is the number
          that says a quadruped is about to go over.
        * **feet** — which feet are on the ground. A foot force that has gone
          to zero mid-walk is the earliest sign of a stumble.
        * **speed, against what was commanded** — a stick held at full with the
          robot at zero is a stall, and it is invisible if only one of the two
          numbers is on screen.
        """
        snapshot = self._telemetry.get(robot_id) or {}
        out: Dict[str, Any] = {}

        taken = self._telemetry_at.get(robot_id)
        if taken is not None:
            age = time.monotonic() - taken
            out["age_s"] = round(age, 2)
            out["stale"] = age > 1.6

        imu = snapshot.get("imu") or {}
        attitude = {
            key: round(float(imu[key]), 4)
            for key in ("roll_rad", "pitch_rad", "yaw_rad")
            if isinstance(imu.get(key), (int, float))
        }
        if attitude:
            # Degrees too: radians are what the topic carries, degrees are what
            # a person reads. Both, rather than making the UI convert.
            attitude["roll_deg"] = round(attitude.get("roll_rad", 0.0) * 57.2958, 1)
            attitude["pitch_deg"] = round(attitude.get("pitch_rad", 0.0) * 57.2958, 1)
            out["attitude"] = attitude

        feet = snapshot.get("foot_force")
        if isinstance(feet, (list, tuple)) and len(feet) >= 4:
            try:
                out["feet"] = [int(value) for value in feet[:4]]
            except (TypeError, ValueError):
                pass

        sport = snapshot.get("sport_state") or {}
        for source, name in ((sport, "sport"), ):
            velocity = source.get("velocity")
            if isinstance(velocity, (list, tuple)) and len(velocity) >= 3:
                try:
                    vx, vy = float(velocity[0]), float(velocity[1])
                    out["speed"] = round((vx * vx + vy * vy) ** 0.5, 3)
                    out["velocity"] = [round(float(v), 3) for v in velocity[:3]]
                except (TypeError, ValueError):
                    pass
        if sport.get("gait_type"):
            out["gait"] = str(sport["gait_type"])
        for key in ("body_height", "foot_raise_height"):
            if isinstance(sport.get(key), (int, float)):
                out[key] = round(float(sport[key]), 3)

        aggregate = snapshot.get("aggregate_state") or {}
        if isinstance(aggregate.get("speedLevel"), (int, float)):
            out["speed_level"] = int(aggregate["speedLevel"])

        battery = snapshot.get("battery") or {}
        if battery:
            # Moved here from the raw snapshot: battery *is* body state, and
            # having the page read it out of a second structure is what made
            # the whole 1.7 KB snapshot part of a 4 Hz payload.
            out["battery"] = {
                key: value for key, value in battery.items() if value is not None
            }

        # The FSM classification, which is what a humanoid shows as "what it is
        # doing". Named `fsm` rather than `posture`: `posture` in this payload
        # already means what the console *believes* it told the robot.
        fsm = snapshot.get("posture")
        if isinstance(fsm, dict) and fsm:
            out["fsm"] = fsm

        return out

    #: What the console reads, and what it deliberately does not.
    #:
    #: Three different absences, kept apart because conflating them tells the
    #: operator something untrue:
    #:
    #:   live          — data arrived on this link this session
    #:   silent        — the console asked and the topic did not answer
    #:   not attempted — the console does not read it at all
    #:
    #: The third is the one that matters most. `rt/utlidar/robot_pose` is not
    #: read by anything here, so an empty map would otherwise look like "the
    #: robot cannot do this" when the truth is "nobody has checked".
    READ_SOURCES = (
        ("lowstate", "电量 / IMU / 足端", "rt/lf/lowstate"),
        ("multiplestate", "音量 / 亮度 / 避障开关", "rt/multiplestate"),
        ("state", "步态 / 模式 / 速度 / 位置",
         "rt/lf/sportmodestate 或 rt/sportmodestate"),
        ("video", "视频", "videohub (MJPEG)"),
    )

    NOT_ATTEMPTED = (
        ("pose", "绝对位姿", "rt/utlidar/robot_pose",
         "这条 topic 是否在 WebRTC 上发布，尚未在真机确认"),
        ("lidar", "激光雷达点云", "rt/utlidar/voxel_map_compressed",
         "未确认；`robot_lidar` 工具能读摘要，控制台没有接"),
        ("map", "占用栅格地图", "rt/mapping/grid_map", "未确认"),
    )

    def sources(self, robot_id: str) -> Dict[str, Any]:
        """What this robot is reporting right now, and what nobody has asked it.

        Derived from the telemetry snapshot rather than from a separate probe:
        each field below comes from exactly one topic, so its presence *is* the
        evidence that the topic answered. That keeps it free — no extra reads —
        and keeps it honest, because it reports what actually arrived rather
        than what a config file claims should.
        """
        snapshot = self._telemetry.get(robot_id) or {}
        video = self.video_status(robot_id)
        connected = bool(self.fleet.get(robot_id).session.is_connected)

        live_map = {
            "lowstate": bool(
                snapshot.get("battery") or snapshot.get("imu") or snapshot.get("foot_force")
            ),
            "multiplestate": bool(snapshot.get("aggregate_state")),
            "state": bool(snapshot.get("sport_state")),
            "video": video.get("state") == "ok",
        }

        read = []
        for source_id, label, topic in self.READ_SOURCES:
            is_live = live_map[source_id]
            if not connected:
                status = "offline"
            elif is_live:
                status = "live"
            elif source_id == "video":
                # Video has its own richer states; mirror them rather than
                # flattening "starting" and "stalled" into one word.
                status = video.get("state") or "silent"
            else:
                status = "silent"
            read.append({
                "id": source_id, "label": label, "topic": topic, "status": status,
            })

        return {
            "connected": connected,
            "read": read,
            "not_attempted": [
                {"id": source_id, "label": label, "topic": topic, "why": why}
                for source_id, label, topic, why in self.NOT_ATTEMPTED
            ],
        }

    def source_status(self, robot_id: str) -> Dict[str, Any]:
        """Just the statuses, for the 4 Hz push.

        The labels, topic names and reasons are static strings — around 1.5 KB
        of them — and sending that four times a second was 21% of the status
        payload for text that changes when a topic starts or stops answering
        and at no other time. The catalogue lives in `/api/console/controls`,
        which the page fetches once.
        """
        spec = self.sources(robot_id)
        return {
            "connected": spec["connected"],
            "status": {item["id"]: item["status"] for item in spec["read"]},
        }

    def source_catalogue(self) -> Dict[str, Any]:
        """The static half of the source list."""
        return {
            "read": [
                {"id": source_id, "label": label, "topic": topic}
                for source_id, label, topic in self.READ_SOURCES
            ],
            "not_attempted": [
                {"id": source_id, "label": label, "topic": topic, "why": why}
                for source_id, label, topic, why in self.NOT_ATTEMPTED
            ],
        }

    # ── two-dog synchronisation ──────────────────────────────────────

    def _pose_of(self, robot_id: str) -> Optional[tuple]:
        """(x, y, yaw) for a robot, or None when it is not reporting one."""
        spot = self.where(robot_id)
        if not spot:
            return None
        return (spot["x"], spot["y"], spot["yaw"])

    @property
    def sync(self) -> SyncMonitor:
        return self._sync

    def mark_sync(self, robot_ids: Optional[Iterable[str]] = None) -> Dict[str, Any]:
        """Take the alignment reference: "the dogs are together *now*".

        Everything the sync monitor reports is a change since this moment, so
        the operator marks it once the dogs are lined up. Nothing is marked
        automatically — a console that decided for itself when two dogs were
        aligned would be guessing about the physical world from two odometers
        that do not share an origin.
        """
        wanted = list(robot_ids) if robot_ids else list(self.fleet.ids)
        poses: Dict[str, Any] = {}
        for robot_id in wanted:
            pose = self._pose_of(robot_id)
            if pose is not None:
                poses[robot_id] = pose
        if not poses:
            raise AgentError(
                "没有机器狗在报告位姿，无法记录对齐基准。",
                code="no_pose",
                hint="位姿来自状态 topic；机器人没发布它时无法对齐。",
            )
        result = self._sync.mark(poses)
        result["skipped"] = [rid for rid in wanted if rid not in poses]
        logger.info("sync reference marked for %s", result["marked"])
        return result

    def sync_state(self) -> Dict[str, Any]:
        return self._sync.summary({rid: self._pose_of(rid) for rid in self.fleet.ids})

    # ── the arena camera ─────────────────────────────────────────────

    def start_arena(self) -> Dict[str, Any]:
        """Open the arena camera if one is configured. Idempotent.

        Returns what happened rather than raising: a console with no camera is a
        console that drives robots, which is still the point, and refusing to
        start because a webcam is unplugged would be the wrong trade.
        """
        if not self.config.arena_camera:
            self._arena_error = "no camera configured"
            return {"started": False, "reason": self._arena_error}
        if self._camera is not None:
            return {"started": True, "reason": "already running"}

        from unitree_robot_agent.vision import ArenaCamera, Calibration

        if not self.config.arena_calibration:
            # Without a path there is nowhere to *save* a calibration either, so
            # the panel could not fix this. That one is worth refusing.
            self._arena_error = "没有指定标定文件路径"
            return {"started": False, "reason": self._arena_error}

        path = Path(self.config.arena_calibration)
        if not path.exists():
            # **Not an error.** The file is written by the console's own
            # calibration panel, so its absence is the state every arena starts
            # in — and refusing to start because it is missing would mean the
            # panel could never run to create it. The camera still opens, so the
            # page has a frame to click four corners on.
            self._arena_error = "还没有标定 —— 在场地视图里点「标定场地」"
            logger.info("arena: no calibration at %s yet", path)
        else:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                self._calibration = Calibration.from_dict(raw)
            except Exception as exc:  # noqa: BLE001
                # A file that exists and cannot be read is a different thing: it
                # is a calibration somebody made, and quietly ignoring it would
                # leave them wondering why the map is blank.
                self._arena_error = f"标定文件读不了：{type(exc).__name__}: {exc}"
                logger.warning("arena calibration: %s", self._arena_error)

        self._camera = ArenaCamera(self.config.arena_camera)
        self._camera.start()
        # "starting", not "opened": `start()` only spawns the thread, and the
        # device is opened on it. A log that says a camera opened a line before
        # it fails to is a log somebody reads under pressure and mis-trusts.
        logger.info("arena camera %s starting", self.config.arena_camera)
        if self._calibration is None:
            return {"started": True, "reason": self._arena_error, "calibrated": False}
        return {
            "started": True,
            "reason": "",
            "calibrated": True,
            "residual_m": _maybe_round(self._calibration.residual_m, 3),
            "scale_factor": _maybe_round(self._calibration.scale_factor, 3),
        }

    async def _arena_loop(self) -> None:
        """Look at the camera and work out where the dogs are.

        Detection is cheap and the map is only as good as its freshness, so this
        runs faster than the status push. A frame that has not changed since the
        last look is skipped — a still arena does not need re-reading, and
        spending the CPU on it would make the console sluggish for no gain.
        """
        from unitree_robot_agent.vision.detect import locate

        interval = 1.0 / max(1.0, self.config.arena_hz)
        last_frame_at = 0.0
        while True:
            try:
                await asyncio.sleep(interval)
                camera = self._camera
                if camera is None:
                    continue
                if self._calibration is None:
                    # Frames still arrive, so the calibration page has something
                    # to click on — which is the only way it stops being None.
                    self._arena = {
                        "state": "uncalibrated",
                        "detail": self._arena_error or "还没有标定",
                    }
                    continue
                if not self.robots_with_markers():
                    self._arena = {
                        "state": "no_markers",
                        "detail": "还没有标记的登记信息",
                    }
                    continue

                frame = camera.latest()
                if frame is None or frame.at == last_frame_at:
                    continue
                last_frame_at = frame.at

                result = await asyncio.to_thread(
                    locate, frame.image, self._calibration, self._marker_manifest()
                )
                self._arena = {**result.as_dict(), "state": "ok", "age_s": round(frame.age_s, 2)}
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a map must not kill the console
                self._arena = {"state": "error", "detail": f"{type(exc).__name__}: {exc}"}
                logger.warning("arena loop error: %r", exc)

    def _marker_manifest(self) -> Dict[str, Any]:
        """Which tag belongs to which robot.

        **The printed sheet owns this mapping, not the roster.** Markers are
        printed once and stuck on the dogs; the roster is a JSON file that
        somebody reorders. Deriving the map from `list(self.fleet.ids)` — which
        is what this did — means that a roster listing `dog2` first attributes
        every pose the camera reports to the other dog. Positions, headings and
        the separation all look completely plausible, and `align_formation` then
        steers each physical dog by the other one's error.

        So the manifest written next to the printed tags is read when it is
        configured, and the roster is cross-checked against it. The fallback
        still exists — a console with no manifest has to do *something* — but it
        says which mapping it used, and the page shows it.
        """
        from unitree_robot_agent.vision.markers import default_markers, load_manifest

        path = getattr(self.config, "marker_manifest", "") or ""
        positional = {
            "family": "DICT_APRILTAG_36h11",
            "markers": [
                {"tag_id": m.tag_id, "robot_id": m.robot_id, "size_m": m.size_m}
                for m in default_markers(list(self.fleet.ids))
            ],
        }
        if not path:
            positional["source"] = "roster"
            positional["note"] = (
                "没有配置 markers.json，所以标签→狗的映射是**按名单顺序推出来的**。"
                "打印的标签纸如果和名单顺序不一致，每只狗的位置都会归到另一只身上。"
                "用 --marker-manifest docs/markers/markers.json 指定打印时写出的清单。"
            )
            return positional

        try:
            document = load_manifest(path)
        except Exception as exc:  # noqa: BLE001 - a bad path is a finding, not a crash
            positional["source"] = "roster"
            positional["note"] = (
                f"markers.json 读不了（{type(exc).__name__}: {exc}），"
                f"退回按名单顺序推断 —— 这个映射可能和打印的标签纸不一致。"
            )
            return positional

        markers = [
            {
                "tag_id": int(m["tag_id"]),
                "robot_id": str(m["robot_id"]),
                "size_m": float(m.get("size_m") or document.get("size_m") or 0.15),
            }
            for m in (document.get("markers") or [])
            if isinstance(m, dict) and "tag_id" in m and "robot_id" in m
        ]
        if not markers:
            positional["source"] = "roster"
            positional["note"] = f"{path} 里没有可用的标记，退回按名单顺序推断。"
            return positional

        out = {
            "family": document.get("family") or positional["family"],
            "markers": markers,
            "source": "manifest",
            "path": str(path),
        }
        # A manifest that names robots this console does not have is worth
        # saying out loud: it usually means the sheet was printed for a
        # different pair.
        known = set(self.fleet.ids)
        named = {m["robot_id"] for m in markers}
        strangers = sorted(named - known)
        unseen = sorted(known - named)
        if strangers or unseen:
            out["note"] = (
                f"清单和名单对不上：清单里有 {strangers or '无'}，"
                f"名单里有 {unseen or '无'} 不在清单里。"
                f"**贴错标签或者打印的是另一对狗。**"
            )
        return out

    def robots_with_markers(self) -> List[str]:
        return list(self.fleet.ids)

    def arena_state(self) -> Dict[str, Any]:
        """What the camera currently sees, and why it sees nothing when it does not.

        The distinctions are the same ones the source list makes, and for the
        same reason: "no camera", "camera but no calibration" and "camera
        working, dogs out of shot" want three different reactions from the
        operator, and one empty picture answers none of them.
        """
        camera = self._camera
        # **Which tag belongs to which robot, computed before the early return.**
        #
        # It is a property of the printed sheet and the roster, not of whether a
        # frame arrived — and a console with the camera off still deserves to
        # know its mapping was guessed at. It was produced and exposed on
        # `/api/fleet` and read by nothing, so a console deriving tag→robot from
        # roster order, and therefore attributing every pose to the other dog,
        # looked exactly like one reading the printed sheet.
        manifest = self._marker_manifest()
        mapping = {
            "marker_source": manifest.get("source", "roster"),
            "marker_note": (
                manifest.get("note")
                if manifest.get("source") != "manifest" or manifest.get("note")
                else ""
            ),
        }
        if camera is None:
            return {
                "state": "off",
                "detail": self._arena_error,
                "poses": {},
                "missing": [],
                **mapping,
            }

        status = camera.status()
        out = {
            "camera": status,
            "calibration": (
                {
                    "residual_m": _maybe_round(self._calibration.residual_m, 3),
                    "scale_factor": _maybe_round(self._calibration.scale_factor, 3),
                    "adequate": self._calibration.adequate,
                    # The corners, so the map can be drawn to scale instead of
                    # being given a size by the frontend that the calibration
                    # disagrees with.
                    "bounds": [
                        round(min(x for x, _y in self._calibration.world_points), 3),
                        round(min(y for _x, y in self._calibration.world_points), 3),
                        round(max(x for x, _y in self._calibration.world_points), 3),
                        round(max(y for _x, y in self._calibration.world_points), 3),
                    ],
                }
                if self._calibration is not None else None
            ),
        }
        if self._calibration is None:
            # Answered here rather than left to the detection loop, so the state
            # is right the instant the console asks — a page that has to wait
            # for a frame to learn there is no calibration shows a blank map and
            # no reason for as long as the camera takes to produce one.
            return {**out, "state": "uncalibrated",
                    "detail": self._arena_error or "还没有标定",
                    "poses": {}, "missing": list(self.fleet.ids)}
        if status["state"] == "failed":
            return {**out, "state": "camera_failed", "detail": status["error"],
                    "poses": {}, "missing": []}
        if status["state"] == "stale":
            return {**out, "state": "camera_stale", "detail": "",
                    "poses": {}, "missing": list(self.fleet.ids)}
        if not self._arena:
            return {**out, "state": "starting", "detail": "",
                    "poses": {}, "missing": list(self.fleet.ids)}

        # The separation is computed here rather than in the page: it is the
        # number the feature exists to produce, and doing it server-side keeps
        # one implementation of it instead of one per consumer.
        poses = self._arena.get("poses") or {}
        gap = self._separation_from(poses)
        # Surfaced next to the measurement, because it is a property of the
        # measurement rather than of the dogs: the position is real, the
        # *identity* is not certain.
        illegible = sorted(
            name for name, pose in poses.items() if pose.get("legible") is False
        )
        # **Which tag→robot mapping is in force, and whether it was guessed.**
        #
        # The mapping was computed above, before the early return.
        return {
            **out,
            **self._arena,
            "separation": gap,
            "illegible": illegible,
            **mapping,
        }

    @staticmethod
    def _separation_from(poses: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Distance, bearing and heading spread between the two dogs."""
        names = sorted(poses)
        if len(names) < 2:
            return None
        a, b = poses[names[0]], poses[names[1]]
        dx = b["x"] - a["x"]
        dy = b["y"] - a["y"]
        return {
            "a": names[0],
            "b": names[1],
            "distance_m": round(math.hypot(dx, dy), 3),
            "yaw_spread_deg": round(
                abs((a["heading_deg"] - b["heading_deg"] + 180.0) % 360.0 - 180.0), 1
            ),
            "bearing_deg_from_a_to_b": round(math.degrees(math.atan2(dy, dx)), 1),
        }

    def arena_frame_jpeg(self, *, max_width: int = 960) -> Optional[bytes]:
        """The camera's newest frame, as JPEG, for the calibration page.

        Encoded here rather than sent as raw pixels because the point of the
        page is to *see* where the four corners are, and a 1280x720 greyscale
        array is 900 KB of JSON.
        """
        camera = self._camera
        if camera is None:
            return None
        frame = camera.latest()
        if frame is None:
            return None
        import cv2

        image = frame.image
        height, width = image.shape[:2]
        if width > max_width:
            scale = max_width / width
            image = cv2.resize(
                image, (max_width, int(height * scale)), interpolation=cv2.INTER_AREA
            )
        ok, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 82])
        return buffer.tobytes() if ok else None

    def arena_frame_size(self) -> Optional[List[int]]:
        """Native frame size, so the page can map a click back to a pixel.

        Without this the four points are recorded in whatever coordinate system
        the browser happened to be showing, and the calibration is wrong by the
        ratio between the two — a mistake that produces a map which looks
        plausible and is off by a factor.
        """
        camera = self._camera
        frame = camera.latest() if camera is not None else None
        if frame is None:
            return None
        height, width = frame.image.shape[:2]
        return [width, height]

    def calibrate_arena(self, document: Dict[str, Any]) -> Dict[str, Any]:
        """Validate a calibration from the page and start using it.

        Saved to the configured path so a session does not have to be redone,
        and reloaded into the running camera so the map appears without a
        restart — the operator has just measured something and wants to see
        whether it worked.
        """
        from unitree_robot_agent.vision import Calibration

        try:
            calibration = Calibration.from_dict(document)
        except (KeyError, TypeError, ValueError) as exc:
            raise AgentError(
                f"标定数据不完整：{exc}",
                code="bad_calibration",
                hint="需要四个图像点和四个场地坐标点，按同一顺序。",
            )

        # **A degenerate set still produces a matrix.** Three points in a line,
        # or a duplicate, solve to something that inverts to nonsense rather
        # than failing — so the four points are checked as a quadrilateral
        # before the homography is built, rather than by hoping the result looks
        # wrong afterwards.
        for label, points in (("画面", calibration.image_points),
                              ("场地", calibration.world_points)):
            if len(set(map(tuple, points))) < 4:
                raise AgentError(
                    f"{label}上的四个点里有重复的。",
                    code="bad_calibration",
                    hint="四个角必须互不相同，而且按环绕顺序给出。",
                )
            area = _shoelace(points)
            span_x = max(p[0] for p in points) - min(p[0] for p in points)
            span_y = max(p[1] for p in points) - min(p[1] for p in points)
            # A real quadrilateral fills a decent share of its own bounding box.
            # Three points in a line, or four in a plus shape, do not.
            if span_x <= 0 or span_y <= 0 or area < 0.05 * span_x * span_y:
                raise AgentError(
                    f"{label}上的四个点几乎共线或者顺序不对（面积只占外接矩形的 "
                    f"{area / max(span_x * span_y, 1e-9):.1%}）。",
                    code="bad_calibration",
                    hint="按左上、右上、右下、左下围成四边形，不要交叉。",
                )

        # **A camera below the tags is not a calibration, it is a typo.**
        #
        # The panel collects both heights as free numbers and nothing
        # cross-checked them. With the camera lower than the tags the parallax
        # geometry does not exist — `scale_factor` is infinite and
        # `residual_m` has no value — and that state used to reach the API,
        # where `round(inf)`/`round(None)` raised and **every endpoint touching
        # the calibration returned 500**, including the four-a-second status
        # push. The page's `JSON.parse` then failed inside a `try` and the live
        # console **froze silently**, which is the worst way for it to fail.
        #
        # `_maybe_round` at the call sites keeps that from ever being a 500
        # again; this keeps the state from being created in the first place,
        # which is better, because a calibration nobody can compute is not one
        # to save.
        tag_h = float(calibration.tag_height_m or 0.0)
        camera_h = float(calibration.camera_height_m or 0.0)
        if tag_h > 0 and camera_h > 0 and camera_h <= tag_h:
            raise AgentError(
                f"相机比标签还低（标签 {tag_h:.2f} m，相机 {camera_h:.2f} m）——"
                f"这个几何关系不存在。",
                code="bad_calibration",
                hint="两个高度是「标签离地」和「相机离地」，相机必须更高。"
                     "很可能是把两个数字填反了。",
            )

        path = self.config.arena_calibration
        if not path:
            raise AgentError(
                "没有配置标定文件路径。",
                code="no_calibration_path",
                hint="用 --arena-calibration 指定一个可写的路径。",
            )
        # **A mirror guard was written here and removed, and the reason is worth
        # keeping.** Clicking the four corners in the reverse order does produce
        # a valid homography that mirrors the arena — measured: the same camera,
        # the same arena, only the order reversed, every distance identical and
        # every direction wrong. The formation steers by direction, so it would
        # drive the two dogs apart while reporting that it was bringing them
        # together.
        #
        # The guard used the sign of the transform's determinant. **It was
        # inverted.** The image's y axis points down and the arena's +y points
        # away from the camera, so the *correct* calibration is
        # orientation-reversing; measured against the order the page generates
        # (`defaultWorldPoints`), the guard called the correct order mirrored
        # and would have refused every legitimate calibration.
        #
        # There is no independent signal to check it against: the world corners
        # are generated from the arena size, not measured, so nothing in the
        # data says which way round is right. The mitigation is the one that
        # already exists — the panel prescribes the order and the printed sheet
        # shows it. See `Calibration.mirrored` for the measurement.
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(
                json.dumps(calibration.as_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            raise AgentError(
                f"标定文件写不了：{exc}",
                code="calibration_write_failed",
                hint=f"检查 {path} 的权限。",
            ) from exc

        self._calibration = calibration
        # `%s`, not `%.2f`: either number can be absent, and a logging call that
        # raises on a legitimate state is a crash in the one place that is
        # supposed to explain what happened.
        logger.info(
            "arena calibration saved: residual %s m, scale x%s",
            _maybe_round(calibration.residual_m, 3),
            _maybe_round(calibration.scale_factor, 3),
        )
        return {
            "saved": True,
            "path": str(path),
            "calibration": calibration.as_dict(),
            "adequate": calibration.adequate,
            "residual_m": _maybe_round(calibration.residual_m, 3),
        }

    # ── formation ────────────────────────────────────────────────────

    def formation_state(self) -> Dict[str, Any]:
        """What the alignment run is doing, for the 4 Hz push.

        Kept small on purpose: the payload budget is 6 KB and this is one of a
        dozen things in it. The step history is summarised rather than shipped —
        the operator wants to know whether it is working, not to read the log.
        """
        if not self._formation:
            return {"state": "idle", "steps": 0}
        return dict(self._formation)

    def start_formation(self, target: Dict[str, Any]) -> Dict[str, Any]:
        """Begin aligning, in the background.

        A run is up to six steps of up to 2.5 s each plus settling, so it cannot
        be a request the browser waits on — and the operator needs to watch it
        happen and be able to stop it.
        """
        from .formation import parse_formation

        if self._formation_task is not None and not self._formation_task.done():
            raise AgentError(
                "已经在编队中。",
                code="formation_running",
                hint="先等它结束，或者停下来。",
            )
        if self._routine is not None and self._routine.get("running"):
            # Both drive every connected robot, so one would fight the other for
            # the same dogs — and the operator would be watching two progress
            # displays that disagree about what is happening. The motion window
            # stops them colliding at the *tool* level; this stops them being
            # started together at all, which is the part a person has to decide.
            raise AgentError(
                "编排正在运行，不能同时编队。",
                code="routine_running",
                hint="先停止编排，或者等它跑完。",
            )
        try:
            parsed = parse_formation(target or {})
        except ValueError as exc:
            raise AgentError(
                f"编队参数不对：{exc}",
                code="bad_formation",
                hint="间距、方位、朝向差都要数字，mover 是 both 或 b。",
            ) from exc

        self._formation = {
            "state": "running",
            "target": parsed.as_dict(),
            "steps": 0,
            "detail": "",
            "started_at": time.time(),
            "history": [],
        }
        self._formation_task = asyncio.create_task(
            self._run_formation(parsed), name="console-formation"
        )
        return self.formation_state()

    async def stop_formation(self, *, reason: str = "operator") -> Dict[str, Any]:
        task = self._formation_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._formation_task = None
        if self._formation:
            self._formation["state"] = "stopped"
            self._formation["detail"] = reason
        await self.stop_all()
        return self.formation_state()

    async def _run_formation(self, target: "Formation") -> None:
        try:
            result = await self.align_formation(target)
            self._formation = {
                **(self._formation or {}),
                "state": result["outcome"],
                "detail": result["detail"],
                "converged": result["converged"],
                "steps": len(result["steps"]),
                "history": [step["measured"] for step in result["steps"]],
                "ended_at": time.time(),
            }
        except asyncio.CancelledError:
            self._formation = {
                **(self._formation or {}),
                "state": "cancelled",
                "detail": "已取消",
                "ended_at": time.time(),
            }
            raise
        except Exception as exc:  # noqa: BLE001 - a formation must not kill the console
            self._formation = {
                **(self._formation or {}),
                "state": "error",
                "detail": f"{type(exc).__name__}: {exc}",
                "ended_at": time.time(),
            }
            logger.warning("formation failed: %r", exc)

    async def align_formation(
        self,
        target: "Formation",
        *,
        # The default lives in `formation.py` with the reasoning for the number;
        # a second copy here was dead and would have drifted.
        max_steps: int = 0,
        tolerance_m: float = 0.10,
        tolerance_deg: float = 8.0,
        settle_s: float = 0.6,
    ) -> Dict[str, Any]:
        """Drive the pair into formation, measuring after every step.

        **Closed loop, because the open-loop version cannot exist.** A move is
        ordered as a fraction of the robot's own maximum speed for a duration,
        and nobody has measured what that maximum is — so a step lands somewhere
        near where it was aimed and the next measurement decides what to do
        about it.

        Four ways this stops, and all of them matter more than the fifth step
        would:

        * **Converged** — within tolerance.
        * **No progress** — the error did not shrink, so the constants are wrong
          in a way another step will not fix. Continuing is how a formation loop
          walks two robots into a wall while chasing a number.
        * **Lost the measurement** — a dog left the frame, or the camera went
          stale. Driving on from a stale position is driving blind.
        * **E-stop** — checked every step, because a latched stop must stop
          this too.

        Every step is recorded, so the operator can see what it did and why it
        stopped rather than being told "not converged".
        """
        from .formation import DEFAULT_MAX_STEPS, Formation, converged, plan

        if not isinstance(target, Formation):
            target = Formation(**target)

        steps: List[Dict[str, Any]] = []

        for index in range(max(1, max_steps or DEFAULT_MAX_STEPS)):
            if self.state.estop.engaged:
                return self._formation_result(steps, target, "estopped",
                                              "急停锁存，编队中止")

            poses = dict(self._arena.get("poses") or {})
            state = self.arena_state()
            if state.get("state") != "ok" or len(poses) < 2:
                return self._formation_result(
                    steps, target, "no_measurement",
                    "看不到两只狗 —— 相机没画面、出画了、或者没标定",
                )

            # **A tag too small to read is worse than one that is missing.**
            #
            # Below `MIN_PX_PER_CELL × CELLS_ACROSS` the detector does not fail,
            # it starts misreading ids — and two tags four bits apart in
            # `DICT_APRILTAG_36h11` decode to each other under blur. A misread
            # id puts one dog's position on the other, from data that looks
            # entirely plausible, and this loop would then steer each physical
            # dog by the wrong error. Missing data stops the loop; wrong data
            # drives it.
            #
            # `tag_px` was already computed, documented as "the honest measure
            # of how much to trust the numbers beside it", and put on the wire —
            # and compared to nothing.
            unclear = sorted(
                name for name, pose in poses.items()
                if pose.get("legible") is False
            )
            if unclear:
                detail = ", ".join(
                    f"{name} 的标签只有 {poses[name].get('tag_px', 0):.0f}px"
                    for name in unclear
                )
                return self._formation_result(
                    steps, target, "illegible",
                    f"标签太小，认不出是哪只狗（{detail}）—— "
                    f"读错 id 会把一只狗的位置算到另一只身上，比看不到更糟。"
                    f"让狗离相机近一点，或者用更大的标签。",
                )

            current = converged(poses, target,
                                tolerance_m=tolerance_m, tolerance_deg=tolerance_deg)
            if current["ready"]:
                return self._formation_result(steps, target, "converged", "")

            if steps:
                # Progress is measured on the number that is supposed to shrink.
                before = steps[-1].get("distance_error_m")
                if before is not None and abs(current["distance_error_m"]) >= abs(before) - 0.01:
                    return self._formation_result(
                        steps, target, "no_progress",
                        f"间距差没有变小（{abs(before):.2f} m → "
                        f"{abs(current['distance_error_m']):.2f} m）—— 再走也不会更好",
                    )

            move_plan = plan(poses, target, tolerance_m=tolerance_m,
                             tolerance_deg=tolerance_deg)
            if not move_plan.moves:
                return self._formation_result(steps, target, "no_move",
                                              "规划不出可执行的修正")

            await self._apply_formation_step(move_plan)
            await asyncio.sleep(max(0.0, settle_s))

            # Wait for a frame taken *after* the moves, so the next decision is
            # made on what is there now rather than on what was there before.
            await self._wait_for_fresh_frame(timeout_s=2.5)

            steps.append({
                "index": index,
                **move_plan.as_dict(),
                "measured": current,
            })
            if self._formation:
                # Visible while it is happening: an alignment that goes quiet
                # for twenty seconds is one the operator stops and does by hand.
                self._formation.update({
                    "steps": len(steps),
                    "detail": f"第 {len(steps)} 步：间距差 "
                              f"{current['distance_error_m']:+.2f} m",
                    "history": [step["measured"] for step in steps],
                })

        return self._formation_result(steps, target, "budget", "步数用完了")

    @staticmethod
    def _formation_result(
        steps: List[Dict[str, Any]], target: "Formation", outcome: str, detail: str
    ) -> Dict[str, Any]:
        return {
            "outcome": outcome,
            "detail": detail,
            "converged": outcome == "converged",
            "target": target.as_dict(),
            "steps": steps,
        }

    async def _apply_formation_step(self, move_plan: "Plan") -> None:
        """Send one step's commands, all at once.

        Concurrently, for the same reason `fleet_action` is: the two dogs are
        supposed to act together, and a sequential loop would finish dog 1's
        shuffle before starting dog 2's.
        """
        async def one(move) -> None:
            # **Walk when the correction is mostly straight ahead.**
            # `robot_walk` closes the loop on odometry, so the distance is
            # measured rather than guessed from a speed constant this project
            # does not know — which is exactly the error the formation loop
            # otherwise spends three steps absorbing. When the correction is
            # mostly sideways there is no forward distance to close a loop on
            # and the body-frame move is the honest instrument.
            mostly_forward = (
                abs(move.forward_m) > 0.02
                and abs(move.lateral_m) < abs(move.forward_m)
            )
            if mostly_forward:
                with contextlib.suppress(Exception):
                    await self.call_tool(
                        move.robot_id, "robot_walk",
                        {"meters": round(move.forward_m, 3), "confirm": True},
                    )
            else:
                args = move.move_args()
                if args:
                    with contextlib.suppress(Exception):
                        await self.call_tool(move.robot_id, "robot_move", args)
            if abs(move.turn_deg) >= 1.0:
                with contextlib.suppress(Exception):
                    await self.call_tool(
                        move.robot_id, "robot_turn",
                        {"degrees": move.turn_deg, "confirm": True},
                    )

        await asyncio.gather(*(one(move) for move in move_plan.moves))

    async def _wait_for_fresh_frame(self, *, timeout_s: float = 2.5) -> bool:
        """Block until the arena has been re-measured, or give up.

        `_arena` is only replaced when the detection loop has seen a *new* frame
        and re-run, so comparing the timestamp is what makes the next decision
        use a picture taken after the moves rather than before them.
        """
        camera = self._camera
        if camera is None:
            return False
        frame = camera.latest()
        before = frame.at if frame is not None else 0.0
        deadline = time.monotonic() + max(0.0, timeout_s)
        while time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            frame = camera.latest()
            if frame is not None and frame.at > before:
                # A frame arrived; give the loop a moment to have processed it.
                await asyncio.sleep(0.15)
                return True
        return False

    def controls_payload(self, robot_id: str) -> Dict[str, Any]:
        """The control descriptors this robot can actually honour.

        Safe mode does not unregister movement tools — it refuses them when
        called. So a panel that simply listed the registered tools would offer
        buttons that can only fail. Controls whose tool is motion-gated are
        marked ``blocked`` instead, and the UI greys them out: hiding them
        would misrepresent what the robot can do once safe mode is off.
        """
        runtime = self.fleet.get(robot_id)
        model = getattr(runtime.config, "robot", "") or ""
        try:
            from .controls import controls_for

            controls = controls_for(model, self.tools_available)
        except Exception as exc:  # noqa: BLE001 - a missing panel beats a dead page
            logger.warning("controls(%s) unavailable: %r", robot_id, exc)
            controls = []

        safe = bool(getattr(runtime.config, "safe_mode", False))
        if safe:
            for control in controls:
                if control["tool"] in MOTION_GATED_TOOLS:
                    control["blocked"] = "安全模式已开启"
                elif not control.get("read") and control["tool"] in DANGEROUS_GATED_TOOLS:
                    control["blocked"] = "需要 UNITREE_AGENT_ALLOW_DANGEROUS"

        groups: List[str] = []
        for control in controls:
            if control["group"] not in groups:
                groups.append(control["group"])
        return {
            "robot_id": robot_id,
            "model": model,
            "groups": groups,
            "controls": controls,
            "flags": self.flags(robot_id),
            "sources": self.source_catalogue(),
            "safe_mode": safe,
        }

    def telemetry_health(self, robot_id: str) -> Dict[str, Any]:
        """Whether the panels have real data, why not, **and how old it is**.

        The age is the part that matters more than it looks. Telemetry refreshes
        at 1 Hz and the arena camera at 10 Hz, so the panels are routinely
        showing numbers sampled a second apart — and an operator comparing a
        fresh picture with a stale speed concludes the dog is not moving, one
        second after it started.

        Nothing here pretends the numbers are simultaneous. Each one carries
        when it was taken, and the page says so beside the ones that are old
        enough to mislead.
        """
        try:
            connected = self.fleet.get(robot_id).session.is_connected
        except Exception:  # noqa: BLE001
            connected = False
        snapshot = self._telemetry.get(robot_id)
        error = self._telemetry_error.get(robot_id, "")

        if not connected:
            state = "offline"
        elif error and not snapshot:
            state = "failed"
        elif error:
            state = "degraded"
        elif snapshot:
            state = "ok"
        else:
            state = "starting"
        taken = self._telemetry_at.get(robot_id)
        age = (time.monotonic() - taken) if taken else None
        return {
            "state": state,
            "error": error,
            "has_data": bool(snapshot),
            "battery": bool((snapshot or {}).get("battery")),
            "age_s": round(age, 2) if age is not None else None,
            # One refresh at 1 Hz plus a little: past this the number is old
            # enough that the page should stop presenting it as current.
            "stale": bool(age is not None and age > 1.6),
            "refresh_hz": round(self.config.telemetry_hz, 2),
        }

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
        # A fresh link might reach a robot somebody has since moved. Forgetting
        # is the honest state; guessing is not.
        self.posture(robot_id).forget()
        self._telemetry_at.pop(robot_id, None)
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
                "unwatched_release_s": self.config.unwatched_release_s,
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
            # `compact=True`: the page reads `role`, `target`, `connected`,
            # `faults` and `faults_authoritative` from these rows and **nothing
            # else** — `console.js` has no `session.<field>` in it at all. The
            # session block is ~450 B per robot and was going out four times a
            # second. `/api/fleet` still carries it, and that is fetched once.
            "robots": await self.fleet.status_all(live=live, compact=True),
            # `telemetry` is deliberately absent. It is the raw snapshot —
            # 1.7 KB, a quarter of what this used to weigh — and everything the
            # page shows from it is already derived into `body`, `flags`,
            # `where` and `sources`. Pushing it four times a second was paying
            # for the same numbers twice.
            "flags": {rid: self.flags(rid) for rid in self.fleet.ids},
            "telemetry_health": {
                rid: self.telemetry_health(rid) for rid in self.fleet.ids
            },
            "video": {rid: self.video_status(rid) for rid in self.fleet.ids},
            "routine": self.routine_state(),
            "motion": {rid: self.motion(rid).snapshot() for rid in self.fleet.ids},
            "where": {rid: self.where(rid) for rid in self.fleet.ids},
            # `posture` is deliberately absent: the belief is internal, and the
            # page's posture display reads `body().fsm` instead. It was sent
            # four times a second to nobody, which the payload budget test
            # cannot see — that only checks the total, and a key with no reader
            # is a slice of it nobody would notice growing.
            # "where" and "body" carry everything the pens draw.
            "body": {rid: self.body(rid) for rid in self.fleet.ids},
            "sources": {rid: self.source_status(rid) for rid in self.fleet.ids},
            "sync": self.sync_state(),
            "arena": self.arena_state(),
            "formation": self.formation_state(),
            # How many browsers are watching, over either transport. It was in
            # `snapshot()` and read by `renderStatus`, which runs on this
            # payload — so the watcher line could never light up. The page reads
            # it here, so it is sent here.
            "watching": self._clients,
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

        # Pressing a key at a robot that is not up yet brings it up. Without
        # this the ramp would be set on a robot with no link and the operator
        # would see nothing happen at all.
        await self._autoconnect(robot_id)

        # Set the target; the motion loop ramps toward it and publishes. The
        # operator's stick position is an *intent*, not a velocity — that is
        # what makes "start gently, stop firmly" possible at all.
        controller = self.motion(robot_id)
        controller.set_target(
            vx=_clamp(ly), vy=-_clamp(lx), vyaw=-_clamp(rx), keys=_keys(keys)
        )
        # `delivered` means the intent reached a live link, not that the ramp
        # has finished getting there. A robot that is not up must report false:
        # telling the operator "delivered" for a dog with no link is the kind
        # of lie this console has been careful not to tell.
        connected = bool(self.fleet.get(robot_id).session.is_connected)
        return {
            "robot_id": robot_id,
            "delivered": connected,
            "zeroed": zeroed,
            # What was asked for and what is being sent differ while the ramp
            # is moving. Publishing both is what makes a stall visible: a stick
            # at full with `commanded` near zero is not "driving".
            "axes": {
                "lx": _clamp(lx), "ly": _clamp(ly), "rx": _clamp(rx), "ry": _clamp(ry),
            },
            "motion": controller.snapshot(),
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
            for robot_id in zeroed:
                controller = self._motion.get(robot_id)
                if controller is not None:
                    controller.stop()
            await self._zero_many(zeroed, ramp=True)

        results = []
        for robot_id in targets:
            await self._autoconnect(robot_id)
            # The ramp, not a direct publish. This is the path the *browser*
            # takes, and it was the one still publishing stick positions
            # verbatim after `teleop` was converted — so the entire ramp was
            # bypassed by the only caller that matters. Found by driving it and
            # watching the commanded value stay at zero.
            controller = self.motion(robot_id)
            controller.set_target(
                vx=_clamp(ly), vy=-_clamp(lx), vyaw=-_clamp(rx), keys=_keys(keys)
            )
            results.append(
                {
                    "robot_id": robot_id,
                    "delivered": bool(self.fleet.get(robot_id).session.is_connected),
                    "motion": controller.snapshot(),
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
        # Decelerate rather than cut: drop the target and let the motion loop
        # bring it down. `robot_stop` and the e-stop still cut, because those
        # are the paths where the operator wants it over now.
        for robot_id in previous:
            controller = self._motion.get(robot_id)
            if controller is not None:
                controller.stop()
        await self._zero_many(previous, ramp=True)
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
        # Abort the routine first. Leaving it running would mean the fleet
        # stops and then the sequence immediately issues its next step — the
        # operator's stop would look like it did nothing.
        if self._routine_task is not None and not self._routine_task.done():
            self._routine_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._routine_task
            self._routine_task = None
            if self._routine is not None:
                self._routine["running"] = False
                self._routine["aborted"] = reason
                self._routine["step_label"] = "已停止"
        # **And the formation, for exactly the same reason.**
        #
        # It was missed, and the shape of the miss is worth naming: `stop_all`
        # cancels the routine with a comment explaining that leaving it running
        # would make the stop look like it did nothing — and then does not touch
        # the other loop that drives the dogs. `_run_formation` only checks the
        # e-stop latch, not a STOP ALL, so it plans again on its next iteration
        # and calls `robot_walk`. Reproduced: one command before STOP ALL, two
        # three seconds later, with `_formation["state"]` still `running`.
        #
        # STOP ALL is the button an operator reaches for. It has to stop
        # everything that moves.
        if self._formation_task is not None and not self._formation_task.done():
            self._formation_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._formation_task
            self._formation_task = None
            if self._formation is not None:
                self._formation["state"] = "stopped"
                self._formation["reason"] = reason
        self.state.stop_driving()
        # Cut every ramp first. The motion loop re-publishes whatever the ramp
        # commands 50 times a second, so a stop that only tells the fleet to
        # stop would be overwritten on the next tick — the operator would press
        # STOP ALL and watch the dogs keep going.
        await self._zero_many(list(self.fleet.ids), ramp=False)
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

        # Cancel the routine here rather than letting it discover the latch at
        # its next step boundary. A step can be waiting out eight seconds of
        # dance; an emergency stop that takes eight seconds is not one.
        if self._routine_task is not None and not self._routine_task.done():
            self._routine_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._routine_task
            self._routine_task = None
            if self._routine is not None:
                self._routine["running"] = False
                self._routine["aborted"] = "急停"
                self._routine["step_label"] = "已中止"

        self.state.stop_driving()
        # Same reasoning as STOP ALL, and more urgent: an e-stop whose velocity
        # is re-published on the next tick is not an emergency stop. Named
        # `estopped` because this one *does* latch.
        await self._zero_many(targets, ramp=False, state="estopped")
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
        if changed:
            # The latch is gone, so the marker it left has to go with it —
            # otherwise the panel keeps saying 已急停 after the operator has
            # released it.
            for controller in self._motion.values():
                if controller.state == "estopped":
                    controller.hard_stop(state="idle")
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
        # Validate before touching any per-robot map. The HTTP route checks
        # too, but a lock created for a robot that does not exist is a leak
        # that only shows up after enough requests, and the service should not
        # depend on a caller for that.
        runtime = self.fleet.get(robot_id)
        lock = self._frame_locks.setdefault(robot_id, asyncio.Lock())
        async with lock:
            if not runtime.session.is_connected and not self.config.video_autoconnect:
                # Observing only: the operator decides when to take the slot.
                return None
            try:
                await runtime.session.ensure_connected()
            except Exception as exc:  # noqa: BLE001
                logger.info("frame(%s): not connected (%r)", robot_id, exc)
                self._note_frame(robot_id, ok=False, error=f"connect failed: {exc}")
                return None
            try:
                jpeg = await runtime.backend.snapshot_frame(timeout=2.0)
            except Exception as exc:  # noqa: BLE001
                logger.info("frame(%s) failed: %r", robot_id, exc)
                self._note_frame(robot_id, ok=False, error=f"{type(exc).__name__}: {exc}")
                return None
            self._note_frame(robot_id, ok=bool(jpeg))
            return jpeg

    # ── video health ─────────────────────────────────────────────────

    def _note_frame(self, robot_id: str, *, ok: bool, error: str = "") -> None:
        entry = self._video.setdefault(
            robot_id, {"last_frame_at": 0.0, "failures": 0, "last_error": ""}
        )
        if ok:
            entry["last_frame_at"] = time.time()
            entry["failures"] = 0
            entry["last_error"] = ""
        else:
            entry["failures"] += 1
            if error:
                entry["last_error"] = error

    def video_status(self, robot_id: str) -> Dict[str, Any]:
        """Whether this robot's stream is actually producing pictures.

        Distinct from "connected". The cases a blank panel cannot distinguish:
        no camera on the chassis, the video channel never opened, or the track
        ended — all of which look identical to the operator.
        """
        entry = self._video.get(robot_id) or {}
        last = float(entry.get("last_frame_at") or 0.0)
        failures = int(entry.get("failures") or 0)
        enabled = bool(self.state.video_enabled.get(robot_id, True))

        try:
            connected = self.fleet.get(robot_id).session.is_connected
        except Exception:  # noqa: BLE001 - unknown robot is simply not connected
            connected = False

        age = (time.time() - last) if last else None
        if not enabled:
            state = "disabled"
        elif not connected:
            state = "offline"
        elif age is not None and age <= VIDEO_STALE_S:
            state = "ok"
        elif last or failures:
            # Either frames used to arrive and stopped, or none ever arrived
            # and something has already been tried. Both are "no picture", and
            # neither is "still starting up".
            state = "stalled"
        else:
            state = "starting"

        return {
            "state": state,
            "enabled": enabled,
            "connected": connected,
            "age_s": round(age, 1) if age is not None else None,
            "failures": failures,
            "last_error": entry.get("last_error") or "",
        }

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

        task = asyncio.create_task(_run(), name=f"console-autoconnect-{robot_id}")
        self._connect_tasks[robot_id] = task

        def _forget(finished: asyncio.Task, robot_id: str = robot_id) -> None:
            if self._connect_tasks.get(robot_id) is finished:
                self._connect_tasks.pop(robot_id, None)

        task.add_done_callback(_forget)

    async def _zero(self, robot_id: str) -> None:
        """Best-effort zero of one robot's sticks."""
        try:
            runtime = self.fleet.get(robot_id)
        except UnknownRobot:
            return
        with contextlib.suppress(Exception):
            await runtime.session.zero_joystick()

    async def _zero_many(
        self, robot_ids: Any, *, ramp: bool = False, state: str = "stopped"
    ) -> None:
        """Zero several robots concurrently; never let one failure stop another.

        `ramp=True` drops the motion target and leaves the deceleration to the
        ramp. `ramp=False` (the default) also publishes an immediate zero,
        which is what every safety path wants: an e-stop that decelerates over
        a second is not an e-stop.
        """
        targets = [r for r in robot_ids if r]
        if not targets:
            return
        for robot_id in targets:
            # `self.motion` rather than a bare lookup: a robot that has never
            # been driven has no controller yet, and skipping it meant an
            # e-stop on a fresh page left nothing marked — the panel showed
            # 空闲 a moment after the operator pressed 急停.
            controller = self.motion(robot_id)
            if ramp:
                controller.stop()
            else:
                controller.hard_stop(state=state)
        if ramp:
            return
        await asyncio.gather(
            *(self._zero(robot_id) for robot_id in targets), return_exceptions=True
        )

    async def _zero_driver(self) -> None:
        await self._zero_many(self.state.stop_driving())
