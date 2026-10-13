"""Velocity shaping: a slew-rate limiter between the sticks and the robot.

The console used to publish whatever the stick said, immediately. Full
deflection meant "full speed, now", and the only thing between that and a
runaway was the dead-man zeroing a frame later. A Go2 asked to jump from rest
to full speed lurches, tips, and is hard to steer at the speeds where steering
matters.

`unitree_harness` solves this in its mover thread and it is the single most
transferable thing in that repo: a 20 ms loop that ramps the *commanded*
velocity toward the *target* velocity, then re-sends at a fixed rate. Its
numbers — 1.0 m/s² accelerating, 2.0 m/s² decelerating — encode the right
asymmetry for a legged robot: **gentle to start, decisive to stop.**

One adaptation, because the two stacks differ. That project drives the SDK's
`SportClient.Move(vx, vy, vyaw)` with metres per second. This console drives the
*wireless-controller* topic, which is normalised: 1.0 means "as fast as this
robot will go", and the firmware picks the metres. So the limits here are in
normalised units per second, and they are honestly labelled as such rather than
pretending to be SI.

What is deliberately NOT copied from that repo: it ramps on the server *because
its client is a browser it does not trust*. Same reasoning here, and the same
benefit — a dropped Wi-Fi link now decelerates instead of stopping dead, a
second operator cannot jerk the robot, and the ramp is the prerequisite for
every other guard because they all act on the commanded value.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class MotionLimits:
    """Rates in normalised stick units per second (1.0 = the robot's maximum)."""

    #: How fast the commanded value may rise. Full stick takes ~2 s.
    accel: float = 0.5
    #: How fast it may fall. Twice the accel, following unitree_harness: a
    #: legged robot should start gently and stop firmly.
    decel: float = 1.0
    #: Yaw ramps on its own rate — a robot that can spin up instantly is one
    #: that can fall over sideways.
    yaw_accel: float = 1.0
    #: No target refresh within this window drops the target to zero, and the
    #: ramp then decelerates rather than cutting.
    watchdog_s: float = 1.0
    #: Scale the yaw ceiling down as linear speed rises, so a fast dog cannot
    #: also be asked to pivot hard. See `coupled_yaw_limit`.
    turn_coupling: bool = True

    def as_dict(self) -> Dict[str, float]:
        return {
            "accel": self.accel,
            "decel": self.decel,
            "yaw_accel": self.yaw_accel,
            "watchdog_s": self.watchdog_s,
            "turn_coupling": self.turn_coupling,
        }


#: Below this the sticks are treated as centred: real hardware has drift, and
#: a robot that creeps when you are not touching anything is worse than one
#: that ignores the first millimetre of travel.
DEADZONE = 0.06


def ramp(current: float, goal: float, accel: float, decel: float, dt: float) -> float:
    """Move `current` toward `goal` by at most one step of the slew rate.

    The decel test is on magnitudes, so a full-reverse stick is a deceleration
    through zero with no special case — which is exactly what it is.
    """
    if abs(goal - current) < 1e-9:
        return goal
    if current == 0.0:
        rate = accel
    elif (goal > 0.0) != (current > 0.0):
        # Crossing zero — the old direction is being shed, so this is a
        # deceleration even when |goal| equals or exceeds |current|. Testing
        # magnitudes alone gets "0.8 -> -0.8" wrong and makes a reversal snap.
        rate = decel
    elif abs(goal) > abs(current):
        rate = accel
    else:
        rate = decel
    step = rate * dt
    if goal > current:
        return min(goal, current + step)
    return max(goal, current - step)


def coupled_yaw_limit(speed_ratio: float, *, floor: float = 0.25) -> float:
    """How much of the yaw ceiling is available at this linear speed.

    From unitree_harness: full yaw authority at a standstill, `floor` of it at
    full speed, never fully locked — so the robot can always be turned to face
    the way out of trouble.
    """
    ratio = max(0.0, min(1.0, speed_ratio))
    return max(floor, 1.0 - 0.75 * ratio)


class MotionController:
    """Per-robot commanded velocity, ramped toward whatever the sticks ask.

    One instance per robot, because two dogs sharing a ramp would make each
    one's deceleration depend on the other's acceleration.
    """

    def __init__(self, limits: Optional[MotionLimits] = None, **overrides: float) -> None:
        base = limits or MotionLimits()
        self.limits = replace(base, **overrides) if overrides else base
        #: What the operator asked for.
        self.target: Tuple[float, float, float] = (0.0, 0.0, 0.0)
        #: What we are actually sending.
        self.commanded: Tuple[float, float, float] = (0.0, 0.0, 0.0)
        self.keys = 0
        self._target_at = 0.0
        #: Why the robot is or is not moving, for the UI. See `state`.
        self._state = "idle"
        self._last_publish = 0.0

    # ── input ────────────────────────────────────────────────────────

    def set_target(
        self,
        *,
        vx: float,
        vy: float,
        vyaw: float,
        keys: int = 0,
        now: Optional[float] = None,
    ) -> None:
        self.target = (
            _deadzone(vx),
            _deadzone(vy),
            _deadzone(vyaw),
        )
        self.keys = int(keys)
        self._target_at = now if now is not None else time.monotonic()

    def stop(self) -> None:
        """Drop the target. The ramp still runs, so this decelerates."""
        self.target = (0.0, 0.0, 0.0)
        self.keys = 0

    def hard_stop(self) -> None:
        """Zero the commanded value too — for a latched e-stop, where the
        deceleration ramp is exactly the delay you do not want."""
        self.target = (0.0, 0.0, 0.0)
        self.commanded = (0.0, 0.0, 0.0)
        self.keys = 0
        self._state = "estopped"

    # ── the loop ─────────────────────────────────────────────────────

    def tick(self, dt: float, now: Optional[float] = None) -> Tuple[float, float, float]:
        """Advance one step and return the commanded velocity.

        Called at a fixed rate by the console; `dt` is real elapsed time, so a
        stalled loop decelerates by the right amount rather than by one step.
        """
        now = time.monotonic() if now is None else now
        dt = max(0.0, min(0.25, dt))

        if (
            any(self.target)
            and self.limits.watchdog_s > 0
            and (now - self._target_at) > self.limits.watchdog_s
        ):
            # The operator went away mid-throttle. Decelerate, do not cut —
            # and say so, because "my dog coasted to a stop" needs a name.
            self.target = (0.0, 0.0, 0.0)
            self.keys = 0
            self._state = "watchdog"
        elif any(self.target):
            self._state = "driving"
        elif any(self.commanded):
            self._state = "stopping"
        else:
            self._state = "idle"

        vx = ramp(self.commanded[0], self.target[0], self.limits.accel, self.limits.decel, dt)
        vy = ramp(self.commanded[1], self.target[1], self.limits.accel, self.limits.decel, dt)

        vyaw_target = self.target[2]
        if self.limits.turn_coupling:
            # Scale the *target*, not the commanded value: limiting the target
            # lets the ramp handle the transition. Clamping the output would
            # make the yaw drop instantly when linear speed rises, which is
            # the same lurch this module exists to remove.
            speed_ratio = min(1.0, max(abs(vx), abs(vy)))
            vyaw_target *= coupled_yaw_limit(speed_ratio)
        vyaw = ramp(
            self.commanded[2], vyaw_target,
            self.limits.yaw_accel, self.limits.yaw_accel, dt,
        )

        self.commanded = (_clip(vx), _clip(vy), _clip(vyaw))
        return self.commanded

    @property
    def state(self) -> str:
        return self._state

    @property
    def moving(self) -> bool:
        return any(abs(value) > 1e-4 for value in self.commanded)

    def snapshot(self) -> Dict[str, object]:
        return {
            "state": self._state,
            # Both, because they answer different questions: "what did I ask
            # for" and "what is it actually being told". A stick held at full
            # while the robot creeps is a stall, and you cannot see a stall if
            # you only publish one of them.
            "target": [round(value, 3) for value in self.target],
            "commanded": [round(value, 3) for value in self.commanded],
            "keys": self.keys,
            "limits": self.limits.as_dict(),
        }


def _deadzone(value: float) -> float:
    value = _clip(value)
    if abs(value) < DEADZONE:
        return 0.0
    # Rescale so the deadzone does not cost the first slice of travel: a stick
    # pushed just past centre should give just-off-centre speed, not a jump.
    sign = 1.0 if value > 0 else -1.0
    return sign * min(1.0, (abs(value) - DEADZONE) / (1.0 - DEADZONE))


def _clip(value: float) -> float:
    if value != value:  # NaN
        return 0.0
    return max(-1.0, min(1.0, float(value)))
