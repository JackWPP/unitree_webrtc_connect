"""Robot session: connection lifecycle, command dispatch, state cache.

The single most important behaviour here is **not holding the robot**.

A Unitree robot accepts exactly one WebRTC client. A server that connects
once at startup and keeps the link forever locks the owner out of the
official app — they cannot take over by hand, which is precisely what you
want to be able to do when something goes wrong. So the session:

* connects lazily on the first call that needs the robot,
* disconnects after ``idle_disconnect_s`` of inactivity (default 45 s),
* releases the link *before* answering long calls that don't need it,
* and stops movement before releasing.

``hold()`` exempts a block from the idle reaper, so a deliberate
``move for 10 s`` is not cut short.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, Optional, Set

from ..backends.base import ApiResponse, RobotBackend
from ..errors import (
    AgentError,
    CommandTimeout,
    FSM_REFUSAL_HINTS,
    RobotRefused,
    SessionClosed,
    classify_exception,
)
from ..robots.base import StopPlan
from .audit import AuditLog
from .guards import Guard

logger = logging.getLogger("unitree_robot_agent.session")

WIRELESS_CONTROLLER_TOPIC = "rt/wirelesscontroller"


@dataclass
class SessionStats:
    connects: int = 0
    disconnects: int = 0
    commands: int = 0
    refusals: int = 0
    timeouts: int = 0
    bus_errors: int = 0
    last_error: Optional[str] = None
    last_command_at: Optional[float] = None
    connected_since: Optional[float] = None


class RobotSession:
    """Owns the backend, the guard and the robot's cached state."""

    def __init__(
        self,
        config,
        backend: RobotBackend,
        profile,
        audit: AuditLog,
    ) -> None:
        self.config = config
        self.backend = backend
        self.profile = profile
        self.audit = audit
        self.guard = Guard(config, audit, profile)
        self.stats = SessionStats()

        self._connect_lock = asyncio.Lock()
        self._closed = False
        self._watchdog: Optional[asyncio.Task] = None
        self._holders = 0
        self._last_activity = time.monotonic()
        self._subscribed: Set[str] = set()
        self._lock_notes: list = []
        self._motion_abort = None

    # ── lifecycle ────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        return bool(getattr(self.backend, "is_connected", False))

    @property
    def idle_seconds(self) -> float:
        return max(0.0, time.monotonic() - self._last_activity)

    def touch(self) -> None:
        self._last_activity = time.monotonic()
        self._lock_notes.clear()

    async def ensure_connected(self) -> None:
        """Connect if needed. Serialized: two tools can't race the handshake."""
        if self._closed:
            raise SessionClosed("The agent session is shutting down.")
        if self.config.backend != "mock":
            # The mock backend needs no address; everything else does.
            self.config.require_target()

        if self.is_connected:
            self.touch()
            return

        async with self._connect_lock:
            if self.is_connected:
                self.touch()
                return
            started = time.monotonic()
            try:
                await asyncio.wait_for(
                    self.backend.connect(), timeout=self.config.connect_timeout_s
                )
            except asyncio.TimeoutError as exc:
                raise CommandTimeout(
                    f"Connecting to {self.config.target_label()} timed out after "
                    f"{self.config.connect_timeout_s:g}s."
                ) from exc
            except Exception as exc:
                raise classify_exception(exc) from exc

            self.stats.connects += 1
            self.stats.connected_since = time.time()
            self._subscribed.clear()
            self.touch()
            self._start_watchdog()
            duration_ms = (time.monotonic() - started) * 1000
            logger.info("connected to %s in %.0f ms", self.config.target_label(), duration_ms)
            self.audit.record(
                "connect",
                phase="lifecycle",
                ok=True,
                detail={"target": self.config.target_label(), "duration_ms": round(duration_ms, 1)},
                robot=self.profile.name,
            )

    async def disconnect(self, *, reason: str = "explicit", stop_first: bool = True) -> None:
        if not self.is_connected:
            self.touch()
            return
        if stop_first:
            mode = self.config.stop_on_disconnect
            if mode == "damp":
                await self.emergency_stop(reason=f"disconnect:{reason}", raise_on_error=False)
            elif mode == "none":
                await self._preempt_motion()  # local publisher off, robot untouched
            else:
                await self.safe_stop(reason=f"disconnect:{reason}", raise_on_error=False)
        try:
            await self.backend.disconnect()
        except Exception as exc:  # never let teardown raise
            logger.warning("disconnect failed: %s", exc)
        self.stats.disconnects += 1
        self.stats.connected_since = None
        self._subscribed.clear()
        self.touch()
        self.audit.record(
            "disconnect", phase="lifecycle", ok=True, detail={"reason": reason}, robot=self.profile.name
        )

    @contextlib.asynccontextmanager
    async def hold(self, what: str = "operation") -> AsyncIterator[None]:
        """Keep the connection alive for the duration of the block."""
        self._holders += 1
        try:
            yield
        finally:
            self._holders = max(0, self._holders - 1)
            self.touch()

    def set_motion_abort(self, callback) -> None:
        """Register the motion controller's window-abort hook."""
        self._motion_abort = callback

    async def _preempt_motion(self) -> bool:
        """Stop any running motion window before applying a stop plan."""
        callback = self._motion_abort
        if callback is None:
            return True
        with contextlib.suppress(Exception):
            return bool(await callback())
        return False

    async def aclose(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watchdog
            self._watchdog = None
        # Disconnect *before* marking the session closed: the shutdown path
        # still needs a live link to zero the sticks and stop the robot.
        try:
            await self.disconnect(reason="shutdown")
        except Exception:
            pass
        self._closed = True
        with contextlib.suppress(Exception):
            await self.backend.aclose()

    # ── idle watchdog ────────────────────────────────────────────────

    def _start_watchdog(self) -> None:
        if self._watchdog is None or self._watchdog.done():
            self._watchdog = asyncio.create_task(self._idle_loop(), name="unitree-idle-watchdog")

    async def _idle_loop(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(1.0)
                if not self.is_connected or self._holders > 0:
                    continue
                idle_limit = self.config.idle_disconnect_s
                if idle_limit <= 0:
                    continue
                if self.idle_seconds >= idle_limit:
                    logger.info(
                        "releasing the robot after %.0fs idle so the official app can take over",
                        self.idle_seconds,
                    )
                    await self.disconnect(reason="idle")
        except asyncio.CancelledError:  # pragma: no cover - normal shutdown
            raise
        except Exception:  # pragma: no cover - defensive
            logger.exception("idle watchdog crashed")

    # ── command dispatch ─────────────────────────────────────────────

    async def call(
        self,
        topic: str,
        api_id: int,
        parameter: Any = None,
        *,
        timeout: Optional[float] = None,
        throttle: bool = True,
    ) -> ApiResponse:
        """Send an api_id request. Never raises for robot-side refusals."""
        await self.ensure_connected()
        if throttle:
            await self.guard.throttle()
        self.touch()

        effective = timeout or self.config.request_timeout_s
        try:
            response = await self.backend.api_request(
                topic, api_id, parameter, timeout=effective
            )
        except asyncio.TimeoutError:
            response = ApiResponse(code=-1, timed_out=True, message="no reply")
        except Exception as exc:
            classified = classify_exception(exc)
            self.stats.bus_errors += 1
            self.stats.last_error = str(classified)
            raise classified from exc

        self.stats.commands += 1
        self.stats.last_command_at = time.time()
        if response.timed_out:
            self.stats.timeouts += 1
        elif not response.ok:
            self.stats.refusals += 1
        self.touch()
        return response

    async def call_checked(
        self,
        topic: str,
        api_id: int,
        parameter: Any = None,
        *,
        timeout: Optional[float] = None,
        action: str = "",
    ) -> ApiResponse:
        """Like :meth:`call` but raises :class:`RobotRefused` on a non-zero code."""
        response = await self.call(topic, api_id, parameter, timeout=timeout)
        if response.ok:
            return response
        raise self.refusal(response, api_id=api_id, topic=topic, action=action)

    def refusal(self, response: ApiResponse, *, api_id: int, topic: str, action: str = "") -> AgentError:
        """Build the right error for a non-zero / missing reply."""
        if response.timed_out or response.code == -1:
            return CommandTimeout(
                f"The robot did not answer the '{action or api_id}' request within "
                f"{self.config.request_timeout_s:g}s. Command may or may not have been applied.",
                detail={"topic": topic, "api_id": api_id},
            )
        hint = FSM_REFUSAL_HINTS.get(response.code, "")
        return RobotRefused(
            f"The robot refused '{action or api_id}' with code {response.code}"
            + (f": {response.message}" if response.message else "."),
            robot_code=response.code,
            topic=topic,
            api_id=api_id,
            hint=hint or "Check the robot's current state with robot_status and retry.",
        )

    def publish(self, topic: str, data: Any = None, msg_type: Optional[str] = None) -> bool:
        delivered = self.backend.publish(topic, data, msg_type)
        self.touch()
        return bool(delivered)

    # ── state cache ──────────────────────────────────────────────────

    async def read_topic(
        self,
        topic: str,
        *,
        timeout: Optional[float] = None,
        subscribe: bool = True,
        ensure: bool = True,
    ) -> Optional[dict]:
        """Return the latest payload for ``topic`` (cache-first)."""
        if ensure:
            await self.ensure_connected()
        effective = self.config.state_read_timeout_s if timeout is None else timeout
        try:
            payload = await self.backend.latest(
                topic, subscribe=subscribe, timeout=effective
            )
        except Exception as exc:
            logger.debug("read_topic(%s) failed: %s", topic, exc)
            return None
        self.touch()
        return payload

    async def reported_state(self, *, timeout: Optional[float] = None) -> Optional[int]:
        """The state the robot says it is in, or None when it isn't reporting."""
        topic = self.profile.state_topic
        if not topic:
            return None
        # A stale cached frame is fine here: freezing is a known firmware
        # behaviour, and `classify()` already tells the model the report
        # cannot be trusted in those states.
        payload = await self.read_topic(topic, timeout=timeout, subscribe=True)
        if payload is None:
            return None
        try:
            return self.profile.reported_state(topic, payload)
        except Exception:
            return None

    async def state_view(self) -> dict:
        """State + posture + a confidence note, for the status tools."""
        fsm_id = await self.reported_state()
        view = self.profile.classify(fsm_id)
        view["connected"] = self.is_connected
        return view

    # ── stopping ─────────────────────────────────────────────────────

    async def joystick(
        self,
        *,
        lx: float = 0.0,
        ly: float = 0.0,
        rx: float = 0.0,
        ry: float = 0.0,
        keys: int = 0,
    ) -> bool:
        """Publish one wireless-controller frame (no reply expected)."""
        delivered = self.backend.publish(
            WIRELESS_CONTROLLER_TOPIC,
            {"lx": lx, "ly": ly, "rx": rx, "ry": ry, "keys": keys},
        )
        self.touch()
        return bool(delivered)

    async def zero_joystick(self, repeats: int = 3) -> bool:
        for _ in range(max(1, repeats)):
            if not await self.joystick():
                return False
            await asyncio.sleep(0.03)
        return True

    async def _apply_stop_plan(self, plan: StopPlan) -> bool:
        delivered = False
        if plan.zero_joystick and await self.zero_joystick():
            delivered = True
        if plan.topic_key and plan.api_id is not None:
            response = await self.call(plan.topic_key, plan.api_id, plan.parameter, timeout=2.0)
            delivered = delivered or response.ok
        return delivered

    async def safe_stop(self, *, reason: str = "", raise_on_error: bool = False) -> bool:
        """Stop *moving* while keeping the current posture.

        This is the default recovery: zeroing the sticks and issuing the
        posture-preserving stop. It does **not** damp — damping a standing
        humanoid makes it fall, which is a worse outcome than standing still.
        Returns False when the stop could not be delivered to the robot.
        """
        if not self.is_connected:
            return False
        await self._preempt_motion()
        plan = self.profile.soft_stop()
        try:
            delivered = await self._apply_stop_plan(plan)
            self.audit.record(
                "soft_stop",
                phase="safety",
                ok=delivered,
                detail={"reason": reason, "plan": plan.label, "delivered": delivered},
                robot=self.profile.name,
            )
            return delivered
        except Exception as exc:
            logger.warning("soft stop failed: %s", exc)
            self.audit.record(
                "soft_stop",
                phase="safety",
                ok=False,
                error=str(exc),
                detail={"reason": reason, "plan": plan.label},
                robot=self.profile.name,
            )
            if raise_on_error:
                raise
            return False

    async def emergency_stop(self, *, reason: str = "", raise_on_error: bool = False) -> bool:
        """Damp the robot. May make it drop — explicit use only."""
        await self.ensure_connected()
        await self._preempt_motion()
        plan = self.profile.emergency_stop()
        try:
            delivered = await self._apply_stop_plan(plan)
            self.audit.record(
                "emergency_stop",
                phase="safety",
                ok=delivered,
                detail={"reason": reason, "plan": plan.label, "delivered": delivered},
                robot=self.profile.name,
            )
            return delivered
        except Exception as exc:
            self.audit.record(
                "emergency_stop",
                phase="safety",
                ok=False,
                error=str(exc),
                detail={"reason": reason},
                robot=self.profile.name,
            )
            if raise_on_error:
                raise
            return False

    # ── introspection ────────────────────────────────────────────────

    def status(self) -> dict:
        payload: Dict[str, Any] = {
            "robot": self.profile.name,
            "device_type": self.config.device_type,
            "target": self.config.target_label(),
            "connection_method": self.config.connection,
            "connected": self.is_connected,
            "backend": getattr(self.backend, "name", self.backend.__class__.__name__),
            "idle_seconds": round(self.idle_seconds, 1),
            "idle_disconnect_s": self.config.idle_disconnect_s,
            "safe_mode": self.config.safe_mode,
            "allow_dangerous": self.config.allow_dangerous,
            "stats": {
                "connects": self.stats.connects,
                "disconnects": self.stats.disconnects,
                "commands": self.stats.commands,
                "refusals": self.stats.refusals,
                "timeouts": self.stats.timeouts,
                "last_error": self.stats.last_error,
            },
        }
        if self.stats.connected_since:
            payload["connected_for_s"] = round(time.time() - self.stats.connected_since, 1)
        extra = None
        if hasattr(self.backend, "extra_status"):
            with contextlib.suppress(Exception):
                extra = self.backend.extra_status()
        if extra:
            payload.update(extra)
        return payload
