"""Motion controller: profiled driving and IMU-closed-loop turning.

Two things the raw joystick publisher could not do:

* **Time-accurate, smooth movement** — see
  :class:`~unitree_robot_agent.motion.profile.TrapezoidProfile`. The old
  fixed-rate ramp meant a short move never reached its commanded speed, so
  "move for 0.3 s" travelled an unpredictable distance.
* **Turning by angle** — ``robot_turn(90)`` reads the IMU yaw in a loop and
  stops when the robot has actually rotated that far, instead of driving the
  sticks for a guessed duration. This is the first genuinely closed-loop
  behaviour in the tool surface, and it works on both robots because both
  publish ``imu_state.rpy`` on ``rt/lf/lowstate``.

Every path ends by publishing a zeroed joystick frame, including the failure
paths, and a watchdog bounds the total time so a stuck loop can never leave
the robot driving.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from typing import Optional

from ..errors import AgentError
from .profile import DEFAULT_ACCEL, TrapezoidProfile

logger = logging.getLogger("unitree_robot_agent.motion")

#: Joystick publish rate — matches the on-robot consumer.
TICK_HZ = 20.0
TICK = 1.0 / TICK_HZ

#: Yaw sign convention. The robot maps the joystick as
#: ``[ly, -lx, -rx] -> [vx, vy, omega]``, so a *negative* rx produces a
#: *positive* omega, and positive omega increases yaw (counter-clockwise).
#: Turning is therefore commanded with rx = -kp * yaw_error.
YAW_AXIS_SIGN = -1.0


def wrap_angle(radians: float) -> float:
    """Shortest signed representation of an angle, in (-pi, pi]."""
    return (radians + math.pi) % (2 * math.pi) - math.pi


class MotionController:
    """Drives the robot through the joystick path with profiles and feedback."""

    def __init__(self, session, config) -> None:
        self.session = session
        self.config = config
        #: Set by :meth:`abort_window`; the motion loops poll it.
        self._abort = asyncio.Event()
        #: Cleared while a window runs, so a stop can await its exit.
        self._idle = asyncio.Event()
        self._idle.set()

    # ── windows ──────────────────────────────────────────────────────

    @property
    def window_active(self) -> bool:
        return not self._idle.is_set()

    @contextlib.asynccontextmanager
    async def _window(self, what: str):
        """Mark a motion window: holds the link and answers abort_window()."""
        self._abort.clear()
        self._idle.clear()
        try:
            async with self.session.hold(what):
                yield
        finally:
            self._idle.set()

    async def abort_window(self, *, timeout: float = 2.0) -> bool:
        """Ask a running window to stop and wait for it to leave.

        Returns True when nothing was running or the window exited in
        time (its finally has zeroed the sticks by then). The 2 s budget
        covers turn(), which can sit in an IMU read for up to 1 s plus a
        tick before it sees the flag.
        """
        if self._idle.is_set():
            return True
        self._abort.set()
        try:
            await asyncio.wait_for(self._idle.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # ── low-level publishing ─────────────────────────────────────────

    async def _publish(self, *, lx: float, ly: float, rx: float) -> None:
        await self.session.joystick(lx=lx, ly=ly, rx=rx)

    async def _zero(self, repeats: int = 3) -> None:
        with contextlib.suppress(Exception):
            await self.session.zero_joystick(repeats)

    # ── profiled drive ───────────────────────────────────────────────

    async def drive(
        self,
        *,
        lx: float = 0.0,
        ly: float = 0.0,
        rx: float = 0.0,
        duration: float = 1.0,
        accel: Optional[float] = None,
    ) -> dict:
        """Publish a profiled joystick move; always ends at zero."""
        accel = float(accel or getattr(self.config, "motion_accel", DEFAULT_ACCEL) or DEFAULT_ACCEL)
        # Scale all three axes by the same profile so the heading is kept:
        # ramping them independently would curve the path.
        reference = TrapezoidProfile(peak=1.0, duration=duration, accel=accel)
        schedule = reference.steps(TICK)

        frames = 0
        aborted = False
        started = time.monotonic()
        try:
            async with self._window("motion.drive"):
                for _, scale in schedule:
                    if self._abort.is_set():
                        aborted = True
                        break
                    await self._publish(lx=lx * scale, ly=ly * scale, rx=rx * scale)
                    frames += 1
                    # Pace against the wall clock so a slow link stretches the
                    # publishes instead of silently shortening the move.
                    target = started + frames * TICK
                    drift = target - time.monotonic()
                    await asyncio.sleep(max(0.0, drift))
        finally:
            await self._zero()

        elapsed = time.monotonic() - started
        return {
            "frames": frames,
            "aborted": aborted,
            "duration_s": round(duration, 3),
            "elapsed_s": round(elapsed, 3),
            "command": {"lx": lx, "ly": ly, "rx": rx},
            "profile": reference.to_dict(),
        }

    # ── IMU feedback ─────────────────────────────────────────────────

    async def read_yaw(self, *, timeout: Optional[float] = None) -> Optional[float]:
        """Yaw in radians from the torso IMU, or None when unavailable."""
        from unitree_webrtc_connect.constants import RTC_TOPIC

        payload = await self.session.read_topic(
            RTC_TOPIC["LOW_STATE"], timeout=timeout, subscribe=True
        )
        if not isinstance(payload, dict):
            return None
        rpy = (payload.get("imu_state") or {}).get("rpy")
        if not isinstance(rpy, (list, tuple)) or len(rpy) < 3:
            return None
        try:
            return float(rpy[2])
        except (TypeError, ValueError):
            return None

    # ── closed-loop turn ─────────────────────────────────────────────

    async def turn(
        self,
        degrees: float,
        *,
        speed: Optional[float] = None,
        tolerance_deg: Optional[float] = None,
        timeout: Optional[float] = None,
    ) -> dict:
        """Rotate by ``degrees`` (positive = counter-clockwise / left).

        Runs a proportional loop on the IMU yaw. Returns a dict describing
        what actually happened, including the residual error — do not treat
        ``ok`` as "exactly right", treat ``residual_deg`` as the truth.
        """
        target_deg = float(degrees)
        if not math.isfinite(target_deg):
            raise AgentError(
                "degrees must be a finite number.",
                code="invalid_angle",
                hint="Pass a real angle in degrees, e.g. 90 or -45.",
            )
        if abs(target_deg) < 1e-6:
            return {"ok": True, "skipped": True, "note": "0 degrees is a no-op."}

        speed = float(speed if speed is not None else getattr(self.config, "max_turn_speed", 0.4))
        tolerance = float(
            tolerance_deg
            if tolerance_deg is not None
            else getattr(self.config, "turn_tolerance_deg", 4.0)
        )
        max_seconds = float(
            timeout
            if timeout is not None
            else min(
                getattr(self.config, "max_turn_seconds", 20.0),
                abs(target_deg) / max(5.0, abs(target_deg)) * 20.0 + 5.0,
            )
        )

        start_yaw = await self.read_yaw()
        if start_yaw is None:
            raise AgentError(
                "Cannot read the IMU yaw, so a closed-loop turn is impossible.",
                code="no_imu",
                hint=(
                    "The robot has not published rt/lf/lowstate yet. Retry in a "
                    "second, or use robot_joystick with a timed rx command instead."
                ),
            )

        target_yaw = start_yaw + math.radians(target_deg)
        deadline = time.monotonic() + max_seconds
        kp = 0.9
        frames = 0
        aborted = False
        last_error = math.radians(target_deg)

        try:
            async with self._window("motion.turn"):
                while time.monotonic() < deadline:
                    if self._abort.is_set():
                        aborted = True
                        break
                    current = await self.read_yaw(timeout=1.0)
                    if current is None:
                        break
                    error = wrap_angle(target_yaw - current)
                    last_error = error
                    if abs(math.degrees(error)) <= tolerance:
                        break
                    command = max(-speed, min(speed, kp * error))
                    # Below the dead-band the robot will not move anyway;
                    # nudge it to the minimum that does.
                    if 0 < abs(command) < speed * 0.35:
                        command = math.copysign(speed * 0.35, command)
                    await self._publish(lx=0.0, ly=0.0, rx=YAW_AXIS_SIGN * command)
                    frames += 1
                    await asyncio.sleep(TICK)
        finally:
            await self._zero()

        final_yaw = await self.read_yaw(timeout=1.0)
        residual = (
            math.degrees(wrap_angle(target_yaw - final_yaw))
            if final_yaw is not None
            else math.degrees(last_error)
        )
        turned = (
            math.degrees(wrap_angle(final_yaw - start_yaw)) if final_yaw is not None else None
        )
        return {
            "ok": abs(residual) <= max(tolerance * 2.0, tolerance + 1.0),
            "frames": frames,
            "aborted": aborted,
            "requested_deg": round(target_deg, 2),
            "turned_deg": round(turned, 2) if turned is not None else None,
            "residual_deg": round(residual, 2),
            "tolerance_deg": tolerance,
            "elapsed_s": round(max_seconds - max(0.0, deadline - time.monotonic()), 2),
            "within_tolerance": abs(residual) <= tolerance,
        }
