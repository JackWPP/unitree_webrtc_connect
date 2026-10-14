"""What a move **reports** has to match what it **commanded**.

## The class this closes

Two of the worst bugs found in this project were the same shape:

* **The sign bug.** `robot_walk(0.3)` published `ly = -0.28` — the yaw axis's
  sign copied onto the forward one — and reported `reason: "reached"`. It walked
  backwards.
* **The stick bug.** The no-odometry fallback published `ly = ±1.0` (full scale)
  while timing the move as if the stick were 0.28, and reported `timed_s`. It
  went 2.5× to 5.3× too far.

Both survived a full test suite, and the reason is the same in both cases: **the
tests read the return value while the return value came from a simulated
odometry the test itself was feeding.** A double that reports whatever it is told
cannot disagree with the code.

## What this file does instead

`IntegratedRobot` is a session that **integrates the joystick frames it is
given** into a pose, and reports that pose as its odometry. Nothing is handed to
it: the position is a consequence of the commands, so the code and the double
cannot agree by construction.

Then every claim a primitive makes is checked against the integral:

* `forward_m(m)` says `reached` → the commands must integrate to about `m`
* it says `actual_m` → that must be what the simulation actually moved
* `turn(deg)` says `turned_deg` → the commands must have produced that yaw
* a forward walk must command a **positive** forward stick, a backward one
  negative, and neither may exceed the speed asked for

**This is the general form of both bugs**, and it is the one test shape that
would have caught them without knowing which line was wrong.
"""

from __future__ import annotations

import asyncio
import math

import pytest

from unitree_robot_agent.motion.controller import TICK, MotionController

#: Metres per second at full stick, for the *simulation*. The code's own nominal
#: is 3.0 and the two have to be the same number or this file measures the
#: difference between them rather than a bug.
FULL_STICK_MPS = 3.0

#: Radians per second at full yaw stick. The controller asks for 0.4 rad/s and
#: the sim needs a number; the loop measures its own progress either way, so
#: this only has to be plausible and fixed.
YAW_RATE_AT_FULL_STICK = 1.0


class IntegratedRobot:
    """A session whose odometry is the integral of the sticks it was given.

    Deliberately knows nothing about distances, durations or what the caller
    intended. It applies `ly × FULL_STICK_MPS` for one tick per published frame
    and reports where that leaves it.
    """

    def __init__(self, *, x: float = 0.0, y: float = 0.0, yaw: float = 0.0):
        self.x, self.y, self.yaw = x, y, yaw
        self.frames: list = []
        self._pending = 0.0      # seconds of stick not yet integrated
        self.holds = 0

    # ── the session surface a motion window needs ──

    def hold(self, what: str):
        import contextlib

        @contextlib.asynccontextmanager
        async def _hold():
            self.holds += 1
            yield

        del what
        return _hold()

    async def joystick(self, *, lx=0.0, ly=0.0, rx=0.0, ry=0.0, keys=0):
        """Apply one tick of the command, immediately.

        Immediately rather than on a timer: the controller sleeps `TICK` between
        frames, so integrating one `TICK` per frame is the same clock without
        the wall time.
        """
        self.frames.append((lx, ly, rx))
        self._integrate(lx, ly, rx)

    async def zero_joystick(self, repeats: int = 1):
        for _ in range(max(1, repeats)):
            await self.joystick()

    #: The robot's own joystick mapping, from the docs and from
    #: `MotionController`'s comments: ``[ly, -lx, -rx] -> [vx, vy, omega]``.
    #:
    #: **The double has to model this or it is not a robot.** The first version
    #: applied `rx` directly as yaw rate — "it applies what it is given" — and a
    #: 60° turn came out as 120°, because the controller negates `rx` expecting
    #: the robot to negate it back. A double that does not reproduce the
    #: hardware's conventions cannot close a loop against code that does.
    #:
    #: `ly` going straight to `vx` with no flip is *why* the forward tests below
    #: passed on their own: the sign bug was on a different axis.
    def _integrate(self, lx: float, ly: float, rx: float) -> None:
        self.yaw += -rx * YAW_RATE_AT_FULL_STICK * TICK
        forward = ly * FULL_STICK_MPS * TICK
        left = -lx * FULL_STICK_MPS * TICK
        self.x += (forward * math.cos(self.yaw) - left * math.sin(self.yaw))
        self.y += (forward * math.sin(self.yaw) + left * math.cos(self.yaw))

    # ── the reads the controller uses ──

    async def read_topic(self, topic, timeout=None, subscribe=False):
        if "lowstate" in topic:
            return {"imu_state": {"rpy": [0.0, 0.0, self.yaw]}}
        return {"position": [self.x, self.y], "velocity": [0.0, 0.0, 0.0]}


class SimConfig:
    max_turn_speed = 0.4
    turn_tolerance_deg = 4.0
    max_turn_seconds = 20.0
    max_walk_speed = 0.28
    arrival_fraction = 0.85
    motion_accel = 1.0


def robot(**kwargs):
    session = IntegratedRobot(**kwargs)
    return session, MotionController(session, SimConfig())


# ─── forward ──────────────────────────────────────────────────────────


def test_a_reported_reach_is_a_reach_in_the_simulation():
    """**The sign bug, in its general form.**

    `forward_m(0.3)` published a *negative* forward stick and reported
    `reached`, because the yaw axis's sign had been copied onto the forward one.
    Every test passed: they read `reason` and `actual_m`, and both came from an
    odometry the test was feeding.

    Here the odometry is the integral of the commands, so a backwards command is
    a backwards position and the code cannot agree with itself.
    """
    session, control = robot()
    result = asyncio.run(control.forward_m(0.35, speed=0.5))

    assert result["reason"] == "reached", result
    # The simulation moved forward on its own, from the commands alone.
    assert session.x > 0.15, f"the commands moved it to x={session.x:.3f}"
    assert abs(session.y) < 0.02, f"a straight walk drifted to y={session.y:.3f}"


def test_a_backward_walk_moves_backward_in_the_simulation():
    """The mirror, so a flip cannot satisfy both."""
    session, control = robot()
    result = asyncio.run(control.forward_m(-0.35, speed=0.5))

    assert session.x < -0.15, f"x={session.x:.3f}"
    assert abs(session.y) < 0.02
    assert result["reason"] in ("reached", "stalled", "timeout"), result


def test_the_reported_distance_is_the_simulated_distance():
    """`actual_m` is a *claim about the world*. This makes the world answer.

    Before the stick fix the claim was `timed_s: 1.25` while the commands were
    full-scale — the simulation would have moved 0.87 m for a 0.35 m request.
    """
    # **The window is 85–100%, not ±0.05**, because "reached" is defined as
    # `arrival_fraction` of the distance (0.85 by default, following
    # `unitree_harness`) — the last few centimetres are where a quadruped's gait
    # makes it creep and stall. Asserting a tight band around the request would
    # be testing a stricter contract than the primitive has, and the first
    # version of this did exactly that.
    for requested in (0.2, 0.35, 0.6):
        session, control = robot()
        result = asyncio.run(control.forward_m(requested, speed=0.5))

        measured = math.hypot(session.x, session.y)
        # **The upper bound is one tick of travel, not zero.** The loop reads the
        # position between frames, so it can overshoot by however far one `TICK`
        # carries it — 7.5 cm at a 0.5 stick. Asserting a tighter band than the
        # discrete loop can honour is testing the test, and the first version did
        # (0.225 m for a 0.20 m request, which is the tick, not a bug).
        overshoot = 0.5 * FULL_STICK_MPS * TICK
        assert requested * 0.85 - 0.02 <= measured <= requested + overshoot + 0.01, (
            f"asked for {requested} m, the commands moved the robot "
            f"{measured:.3f} m — outside the 85%–(100% + one tick) window"
        )
        # The reported distance and the simulated one are the same number.
        assert result["actual_m"] == pytest.approx(measured, abs=0.02), (
            f"reported {result.get('actual_m')} m against a simulated "
            f"{measured:.3f} m"
        )


def test_a_walk_never_commands_more_stick_than_it_was_given():
    """**The stick bug, in its general form.**

    The fallback published `ly = ±1.0` while timing the move as if the stick were
    0.28. Nothing in the loop objected, because the loop never looked at what
    went out.
    """
    for speed in (0.2, 0.5, 1.0):
        session, control = robot()
        asyncio.run(control.forward_m(0.3, speed=speed))
        peaks = [abs(ly) for _lx, ly, _rx in session.frames]
        assert peaks, "nothing was commanded"
        assert max(peaks) <= speed + 1e-6, (
            f"asked for a stick of {speed}, commanded up to {max(peaks)}"
        )


def test_the_turn_axis_is_never_used_to_move_forward():
    """The intersection of the sign bug and its mirror: a walk must not touch
    `rx`, and a turn must not touch `ly`."""
    session, control = robot()
    asyncio.run(control.forward_m(0.3, speed=0.4))
    assert all(rx == 0.0 for _lx, _ly, rx in session.frames), (
        "a forward walk commanded yaw"
    )

    turning, turn_control = robot()
    turn_control.read_yaw  # noqa: B018 - the attribute exists on the controller
    asyncio.run(turn_control.turn(45.0))
    assert all(ly == 0.0 and lx == 0.0 for lx, ly, _rx in turning.frames), (
        "a turn moved the robot"
    )
    assert any(rx for _lx, _ly, rx in turning.frames), "a turn never turned"


class NoOdometry(IntegratedRobot):
    """A robot that publishes no position — the project's own documented case.

    **The fallback needs its own double.** `IntegratedRobot` answers
    `position`, so `forward_m` always takes the closed-loop branch and never
    reaches the timed one — which is why the first version of the stick test
    passed against the stick bug. Both of this project's own hardware notes say
    the Go2 does not publish odometry over WebRTC, so the fallback is the path
    that runs in practice.
    """

    async def read_topic(self, topic, timeout=None, subscribe=False):
        if "lowstate" in topic:
            return {"imu_state": {"rpy": [0.0, 0.0, self.yaw]}}
        # No `position` field at all — not a zero one. "It has not moved" and
        # "it has not told me" are different claims.
        return {"velocity": [0.0, 0.0, 0.0]}


def no_odometry(**kwargs):
    session = NoOdometry(**kwargs)
    return session, MotionController(session, SimConfig())


def test_the_timed_fallback_commands_the_stick_it_was_asked_for():
    """**The stick bug, on the path it actually happened on.**

    The fallback published `ly = ±1.0` (full scale) while timing the move as
    though the stick were 0.28. Two facts made this the worst place for it: the
    project's own hardware publishes no odometry, so this *is* the branch that
    runs, and `_apply_formation_step` prefers `robot_walk` for forward
    corrections — so every formation step was a full-stick sprint.
    """
    for speed in (0.2, 0.28, 0.5, 1.0):
        session, control = no_odometry()
        result = asyncio.run(control.forward_m(0.35, speed=speed))

        assert result["reason"] == "no_odometry", result
        peaks = [abs(ly) for _lx, ly, _rx in session.frames]
        assert peaks, "the fallback commanded nothing"
        assert max(peaks) <= speed + 1e-6, (
            f"asked for a stick of {speed}, the fallback commanded "
            f"{max(peaks)} — a full-scale stick is speed/1.0 times faster than "
            f"the duration assumes, and the error is the robot's top speed"
        )


def test_the_timed_fallback_covers_the_distance_it_was_asked_for():
    """And it has to *travel* the distance, which is the half the first fix
    missed: the same commands integrate to half as far when the duration ignores
    the profile's ramp."""
    for requested in (0.2, 0.35, 1.0):
        session, control = no_odometry()
        result = asyncio.run(control.forward_m(requested, speed=0.5))

        measured = math.hypot(session.x, session.y)
        # The fallback is a guess and is allowed to be a poor one; it is not
        # allowed to be half.
        assert requested * 0.6 <= measured <= requested * 1.4, (
            f"asked for {requested} m, the timed fallback moved the robot "
            f"{measured:.3f} m"
        )
        assert result["reason"] == "no_odometry"


def test_the_timed_fallback_says_which_way_it_went():
    """The sign bug on the fallback path, which the closed-loop tests cannot
    see."""
    forward, forward_control = no_odometry()
    asyncio.run(forward_control.forward_m(0.3, speed=0.5))
    assert forward.x > 0.05, f"a forward walk went to x={forward.x:.3f}"

    backward, backward_control = no_odometry()
    asyncio.run(backward_control.forward_m(-0.3, speed=0.5))
    assert backward.x < -0.05, f"a backward walk went to x={backward.x:.3f}"


# ─── turn ─────────────────────────────────────────────────────────────


def test_a_reported_turn_is_a_turn_in_the_simulation():
    """The same consistency demand on the other primitive. `turn` reports
    `turned_deg`; this asks the simulation how far it actually rotated."""
    for degrees in (45.0, -45.0, 90.0):
        session, control = robot()
        result = asyncio.run(control.turn(degrees, speed=0.4))

        turned = math.degrees(session.yaw)
        assert turned == pytest.approx(degrees, abs=6.0), (
            f"asked to turn {degrees}°, the commands rotated it {turned:.1f}°, "
            f"and the result says {result.get('turned_deg')}"
        )
        reported = result.get("turned_deg")
        if reported is not None:
            assert reported == pytest.approx(turned, abs=8.0), (
                f"reported {reported}° against a simulated {turned:.1f}°"
            )


def test_turning_left_and_right_go_opposite_ways():
    left, left_control = robot()
    asyncio.run(left_control.turn(60.0, speed=0.4))
    right, right_control = robot()
    asyncio.run(right_control.turn(-60.0, speed=0.4))

    assert left.yaw > 0.3, f"a left turn produced yaw={left.yaw:.3f}"
    assert right.yaw < -0.3, f"a right turn produced yaw={right.yaw:.3f}"
