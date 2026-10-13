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
from .state import ConsoleState, EStopScope, InputMode, MotionBlocked

logger = logging.getLogger("unitree_fleet_web.service")

#: Joystick axes are normalised; the robot applies its own limits on top.
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
        "robot_move",
        "robot_turn",
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
        # Cancel in-flight auto-connects first: their whole point is to keep
        # running while the loop does other things, so nothing else stops them.
        for task in list(self._connect_tasks.values()):
            task.cancel()
        for task in list(self._connect_tasks.values()):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._connect_tasks.clear()
        self._connecting.clear()

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
        if targets and "robot_list_actions" in self.tools_available:
            try:
                from .controls import action_label

                listed = await self.call_tool(
                    targets[0], "robot_list_actions", {"include_dangerous": False}
                )
                for row in (listed or {}).get("actions") or []:
                    name = row.get("name")
                    if not name:
                        continue
                    # The catalogue descriptions are English debug text
                    # ("Wave hello (1016)."). On a button that is noise, and it
                    # would not match what the per-dog picker calls the same
                    # action — so both go through one label table.
                    label = action_label(name)
                    named.append({
                        "id": f"action_{name}", "tool": "robot_play_action",
                        "args": {"name": name, "confirm": True},
                        "label": label,
                        "key": name,
                        "title": row.get("description") or f"所有狗一起执行 {name}",
                    })
            except Exception as exc:  # noqa: BLE001 - an empty bar beats a broken page
                logger.info("fleet actions: catalogue unavailable (%r)", exc)

        return {
            "targets": targets,
            "skipped": [rid for rid in self.fleet.ids if rid not in targets],
            "posture": actions,
            "named": named,
        }

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
            "safe_mode": safe,
        }

    def telemetry_health(self, robot_id: str) -> Dict[str, Any]:
        """Whether the panels have real data, and why not if they do not."""
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
        return {
            "state": state,
            "error": error,
            "has_data": bool(snapshot),
            "battery": bool((snapshot or {}).get("battery")),
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
            "robots": await self.fleet.status_all(live=live),
            "telemetry": dict(self._telemetry),
            "flags": {rid: self.flags(rid) for rid in self.fleet.ids},
            "telemetry_health": {
                rid: self.telemetry_health(rid) for rid in self.fleet.ids
            },
            "video": {rid: self.video_status(rid) for rid in self.fleet.ids},
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
