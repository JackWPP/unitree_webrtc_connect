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
#:
#: **Only the yaw axis.** `ly` maps straight to forward velocity and `lx` to
#: lateral with one flip; reusing this constant on either of them inverts the
#: direction of travel, which is what happened here once.
YAW_AXIS_SIGN = -1.0

#: Forward and lateral sign conventions, named so they cannot be confused with
#: the yaw one. `ly` is forward-positive as written; `lx` is left-positive after
#: the robot's own flip, so a command for "move right" is a negative `lx`.
LATERAL_AXIS_SIGN = -1.0

#: Metres per second at **full stick**, on this class of robot. A guess.
#:
#: It exists because `forward_m` has two branches and only one of them can
#: measure anything. The closed-loop branch reads the odometry and stops when it
#: has gone far enough, so it never needs this. The fallback — for a robot that
#: publishes no position, which the project's own hardware does not — has to turn
#: a distance into a *duration*, and a duration needs a speed.
#:
#: **Sharing one constant between a stick value and a speed is how the fallback
#: went wrong.** It computed the duration as `metres / speed_mps` and then
#: commanded a *full-scale* stick, so a 0.35 m shuffle was a 0.87 m sprint at
#: 1.4 m/s and 1.87 m at 3.0 m/s — the over-drive factor being the robot's top
#: speed. The fallback now commands the same stick the closed loop does and uses
#: this to time it, and says in `note` that both numbers are assumptions.
NOMINAL_FULL_STICK_MPS = 3.0


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
        #: Which closed-loop move currently owns the robot. See `_window`.
        self._skill_owner = ""

    # ── windows ──────────────────────────────────────────────────────

    @property
    def window_active(self) -> bool:
        return not self._idle.is_set()

    @contextlib.asynccontextmanager
    async def _window(self, what: str):
        """Claim the robot for one closed-loop motion.

        **One at a time, and refusing rather than queueing.** `unitree_harness`
        calls this `_skill_lock` and returns `-11 skill busy`; the reason it does
        not queue is worth restating, because the failure is invisible.

        Two concurrent `forward_m` calls each read their own start position —
        both nearly zero — and each measures its progress from there. The robot
        walks once, and **both report `reached`**:

            task 1: reason=reached actual_m=0.183
            task 2: reason=reached actual_m=0.174
            final position: 0.183   (expected 0.4)

        Neither caller can tell, and the operator gets half a move that two
        layers called a success. A queue would at least run both eventually, but
        a step that fires ten seconds after it was asked for is a step aimed at
        where the robot used to be, so a refusal is the honest answer.

        The check and the claim sit here with no `await` between them, which is
        what makes them atomic on a single-threaded event loop.
        """
        if self._skill_owner:
            raise AgentError(
                f"机器人正在执行 {self._skill_owner}，不能同时开始 {what}。",
                code="skill_busy",
                hint="等它结束，或者用 robot_stop 打断它。",
            )
        self._skill_owner = what
        self._abort.clear()
        self._idle.clear()
        try:
            async with self.session.hold(what):
                yield
        finally:
            self._skill_owner = ""
            self._idle.set()

    @property
    def skill_owner(self) -> str:
        """Which closed-loop move holds the robot, or "" when none does."""
        return self._skill_owner

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

    async def read_position(self, *, timeout: Optional[float] = None):
        """Odometry (x, y) in metres from the state topic, or None.

        **Frequently None, and that is the honest state of this project.** The
        `position` field rides on `rt/sportmodestate`, which the Go2 used for
        testing **never publishes over WebRTC** — measured, repeatedly, on a dog
        answering every other request. So a caller that needs a closed loop has
        to check, and a caller that cannot get one has to say so rather than
        pretend a timed move was measured.
        """
        from unitree_webrtc_connect.constants import RTC_TOPIC

        for topic in (RTC_TOPIC.get("LF_SPORT_MOD_STATE"), RTC_TOPIC.get("SPORT_MOD_STATE")):
            if not topic:
                continue
            try:
                payload = await self.session.read_topic(topic, timeout=timeout, subscribe=True)
            except Exception:  # noqa: BLE001 - a missing topic is a missing topic
                continue
            if not isinstance(payload, dict):
                continue
            position = payload.get("position")
            if not isinstance(position, (list, tuple)) or len(position) < 2:
                continue
            try:
                return (float(position[0]), float(position[1]))
            except (TypeError, ValueError):
                continue
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


    async def forward_m(
        self,
        meters: float,
        *,
        speed: Optional[float] = None,
        arrival: Optional[float] = None,
        timeout: Optional[float] = None,
    ) -> dict:
        """Travel ``meters`` forward, closing the loop on odometry.

        **The primitive this project was missing.** `robot_move` takes a
        duration, which is a guess: how far a dog goes in 0.6 s depends on the
        floor, the battery and the gait, and the same command travels a
        different distance every run. Anything built on top of it — the
        formation correction, a routine that walks a square — inherits that
        error and accumulates it.

        Two things the closed loop does not fix, both stated rather than hidden:

        * **Odometry is often unavailable.** `position` rides on
          `rt/sportmodestate`, which the Go2 used for testing never publishes
          over WebRTC. When it is missing this falls back to a timed move and
          says so in `reason` — a guess labelled as a guess, not a measurement.
        * **Downstream odometry drifts.** It is dead reckoning, so a "metre"
          here is a metre according to the robot's own estimate of its motion,
          which is the best either of us has.

        ``arrival`` is the fraction of the requested distance that counts as
        arrived — 0.85 by default, following `unitree_harness`, because the last
        few centimetres are where a quadruped's gait makes it creep and stall.
        """
        target_m = float(meters)
        if not math.isfinite(target_m):
            raise AgentError("meters must be a finite number.", code="invalid_distance")
        if abs(target_m) < 1e-3:
            return {"ok": True, "skipped": True, "note": "0 m is a no-op."}

        speed = float(speed if speed is not None else getattr(self.config, "max_walk_speed", 0.28))
        if not math.isfinite(speed):
            raise AgentError("speed must be a finite number.", code="invalid_speed")
        # A joystick axis is in [-1, 1]. A caller passing `speed=5` used to reach
        # the wire as `ly=5`, which the robot clamps or refuses depending on the
        # firmware — neither of which is a thing to find out from a moving dog.
        speed = max(-1.0, min(1.0, speed))
        fraction = float(
            arrival if arrival is not None
            else getattr(self.config, "arrival_fraction", 0.85)
        )
        max_seconds = float(
            timeout if timeout is not None
            else max(8.0, abs(target_m) / max(0.05, speed) + 3.0)
        )

        start = await self.read_position()
        if start is None:
            # No odometry: drive for the time the distance *would* take and be
            # explicit that it is a guess. Better a labelled guess than a
            # refusal, because the caller may have nothing else.
            # **The same stick the closed loop commands**, not a full-scale one.
            # `speed` is a joystick magnitude in [-1, 1]; a full-scale stick is
            # `speed / 1.0` times faster than the timing assumes, and the size of
            # that error is the robot's top speed in metres per second.
            reach = max(0.05, min(1.0, abs(speed))) * NOMINAL_FULL_STICK_MPS
            seconds = abs(target_m) / reach
            result = await self.drive(
                ly=math.copysign(min(1.0, abs(speed)), target_m), duration=seconds
            )
            return {
                "ok": bool(result.get("ok", True)),
                "reason": "no_odometry",
                "requested_m": round(target_m, 3),
                "stick": round(min(1.0, abs(speed)), 3),
                "note": (
                    "机器人没有发布里程计，所以**速度和距离都是猜的**："
                    f"按满杆 {NOMINAL_FULL_STICK_MPS:g} m/s、杆量 {min(1.0, abs(speed)):.2f} "
                    f"折算出时长 {seconds:.2f}s。这是被标注为猜测的猜测，不是测量。"
                ),
                "timed_s": round(seconds, 2),
            }

        deadline = time.monotonic() + max_seconds
        sign = 1.0 if target_m > 0 else -1.0
        travelled = 0.0
        frames = 0
        aborted = False
        # Giving up on a robot that is not moving beats waiting out the timeout.
        # Eight seconds per step is most of a routine, and the operator is
        # watching a progress bar that is not progressing.
        stall_after = float(getattr(self.config, "walk_stall_seconds", 1.5))
        still_since = time.monotonic()
        last_travelled = 0.0
        try:
            async with self._window("motion.forward"):
                while time.monotonic() < deadline:
                    if self._abort.is_set():
                        aborted = True
                        break
                    current = await self.read_position(timeout=1.0)
                    if current is None:
                        break
                    # **A magnitude, and that is a real limitation.** The only
                    # odometry this project can read is a position, so the
                    # measurable quantity is how far the robot is from where it
                    # started — which cannot tell "drove forward 0.3 m" from
                    # "was shoved backwards 0.3 m". An earlier version of this
                    # had a `signed < -0.05` branch to abandon a walk that went
                    # the wrong way; `hypot` is never negative, so it never
                    # fired. **A dead branch that looks like a safety check is
                    # worse than no check**, so it is gone and the limitation is
                    # written down instead.
                    #
                    # Detecting it properly needs the velocity, which is on the
                    # same topic as the position and therefore equally absent on
                    # the hardware this was measured against.
                    travelled = math.hypot(current[0] - start[0], current[1] - start[1])
                    if travelled >= abs(target_m) * fraction:
                        break
                    if travelled > last_travelled + 0.005:
                        last_travelled = travelled
                        still_since = time.monotonic()
                    elif time.monotonic() - still_since > stall_after:
                        # It was told to walk and it is not walking. Held by
                        # something, on a slope it cannot climb, or reporting an
                        # odometry that does not move. None of those get better
                        # by waiting.
                        break
                    # **`ly`, not `YAW_AXIS_SIGN * ...`.** The joystick maps
                    # `[ly, -lx, -rx] -> [vx, vy, omega]`, so `ly` goes straight
                    # to forward velocity and the sign convention that flips an
                    # axis belongs to the yaw one only. Applying it here made
                    # `robot_walk(0.3)` walk *backwards*, and the tests did not
                    # catch it because they checked the reported distance and
                    # never the direction that was commanded.
                    await self._publish(lx=0.0, ly=sign * speed, rx=0.0)
                    frames += 1
                    await asyncio.sleep(TICK)
                else:
                    aborted = True
        finally:
            # Always, on every path out: a robot left with a live command keeps
            # walking, which is the failure this file exists to prevent.
            await self._zero()

        reached = travelled >= abs(target_m) * fraction
        stalled = (not reached) and (time.monotonic() - still_since) > stall_after
        return {
            "ok": reached and not self._abort.is_set(),
            # **The labels were swapped.** A run that hit its deadline said
            # "stopped" and a run that lost the odometry said "timeout", and
            # `robot_walk` hands `reason` straight to the caller — so the one
            # word a person reads about what happened was wrong on both paths.
            # `aborted` is the e-stop/dead-man flag, so it means "stopped".
            "reason": (
                "reached" if reached
                else ("stalled" if stalled
                      else ("stopped" if aborted else "timeout"))
            ),
            "requested_m": round(target_m, 3),
            # Always the requested sign, because the odometry only gives a
            # distance. Read it as "the robot reports having moved this far",
            # not as a displacement.
            "actual_m": round(travelled * sign, 3),
            "residual_m": round(abs(target_m) - travelled, 3),
            "frames": frames,
            "note": (
                "闭环在里程计上。'actual_m' 是机器人自己报告走了多远，"
                "'residual_m' 是还差多少 —— 不要拿 ok 当「正好」。"
            ),
        }
