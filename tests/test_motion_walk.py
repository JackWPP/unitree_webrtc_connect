"""Walking a measured distance, which is the primitive this project lacked.

`robot_move` takes a **duration**, which is a guess: how far a dog goes in
0.6 s depends on the floor, the battery and the gait, and the same command
travels a different distance every run. Anything built on it — the formation
correction, a routine that walks a square — inherits that error and accumulates
it.

`unitree_harness` learned this the hard way and its two closed-loop primitives
(`turn_deg`, `forward_m`) are the most valuable thing in that repository. The
turn half already existed here. This is the other half.

## The honest limit

`position` rides on `rt/sportmodestate`, which **the Go2 used for testing never
publishes over WebRTC** — measured, repeatedly, on a dog answering every other
request. So the closed loop often has nothing to close on. That case is not
hidden: it falls back to a timed move and says `reason: "no_odometry"`, which is
a guess labelled as a guess rather than a measurement.
"""

from __future__ import annotations

import asyncio
import math

import pytest

from unitree_robot_agent.errors import AgentError
from unitree_robot_agent.motion.controller import MotionController


class FakeSession:
    """A session that reports a position the test can advance."""

    def __init__(self, *, position=(0.0, 0.0), yaw=0.0, topic=True):
        self.position = position
        self.yaw = yaw
        self.topic = topic
        self.published = []
        self.zeros = 0
        self.holds = 0

    # ── the parts of the session interface a motion window uses ──

    def hold(self, what: str):
        """The window's own "I am moving" claim, which the link and the
        dead-man both read. A test double has to have it or the controller
        cannot take a window at all."""
        import contextlib

        @contextlib.asynccontextmanager
        async def _hold():
            self.holds += 1
            yield

        del what
        return _hold()

    async def joystick(self, *, lx=0.0, ly=0.0, rx=0.0, ry=0.0, keys=0):
        self.published.append((lx, ly, rx))

    async def zero_joystick(self, repeats: int = 1):
        self.zeros += 1

    async def read_topic(self, topic, timeout=None, subscribe=False):
        if not self.topic:
            return None
        if "lowstate" in topic:
            return {"imu_state": {"rpy": [0.0, 0.0, self.yaw]}}
        return {"position": list(self.position), "velocity": [0.0, 0.0, 0.0]}


class FakeConfig:
    max_turn_speed = 0.4
    turn_tolerance_deg = 4.0
    max_turn_seconds = 20.0
    max_walk_speed = 0.5
    arrival_fraction = 0.85
    motion_accel = 1.0


def controller(session):
    control = MotionController(session, FakeConfig())
    return control


def position_sequence(control, values):
    """Feed the controller a series of positions, starting at the origin.

    **The first read is the start**, and a naive counter feeds it the first
    *moved* value too — which makes `travelled` a distance between two points
    the test did not mean, and the assertions measure the test rather than the
    code. This returns the origin for that read and then walks the list.
    """
    state = {"n": 0}

    async def read(timeout=None):
        index = state["n"]
        state["n"] += 1
        if index == 0:
            return (0.0, 0.0)
        value = values[min(index - 1, len(values) - 1)]
        return (value, 0.0)

    control.read_position = read  # type: ignore[assignment]
    return state


def test_the_odometry_is_read_from_the_state_topic():
    session = FakeSession(position=(1.25, -0.5))
    assert asyncio.run(controller(session).read_position()) == (1.25, -0.5)


def test_no_odometry_is_none_not_a_zero():
    """"It has not moved" and "it has not told me" are different claims, and a
    zero makes the first out of the second — which would make every measured
    walk believe it had already arrived."""
    session = FakeSession(topic=False)
    assert asyncio.run(controller(session).read_position()) is None


def test_a_malformed_position_is_none():
    session = FakeSession()
    session.position = ("x", "y")
    assert asyncio.run(controller(session).read_position()) is None


def test_without_odometry_it_walks_on_a_timer_and_says_so():
    """A guess labelled as a guess. Refusing instead would leave a caller with
    nothing, and the caller is the one that knows whether a guess is useful."""
    session = FakeSession(topic=False)
    control = controller(session)
    result = asyncio.run(control.forward_m(0.35, speed=0.5))

    assert result["reason"] == "no_odometry"
    assert result["requested_m"] == 0.35
    assert "猜" in result["note"]
    assert result["timed_s"] == pytest.approx(0.7, abs=0.01)


def test_a_measured_walk_stops_when_it_has_gone_far_enough():
    """The loop reads the position until the distance is covered. The session
    advances it, so this exercises the same code a robot would."""
    control = controller(FakeSession(position=(0.0, 0.0)))
    position_sequence(control, [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40])

    result = asyncio.run(control.forward_m(0.35, speed=0.5))

    assert result["reason"] == "reached", result
    assert result["actual_m"] >= 0.35 * 0.85
    assert result["ok"] is True


def test_eighty_five_percent_counts_as_arrived():
    """Following `unitree_harness`: the last few centimetres are where a
    quadruped's gait makes it creep and stall, and waiting for the whole
    distance is how a skill turns into a timeout."""
    control = controller(FakeSession(position=(0.0, 0.0)))
    position_sequence(control, [0.30, 0.30, 0.30])  # 86% of 0.35

    result = asyncio.run(control.forward_m(0.35, speed=0.5))
    assert result["reason"] == "reached"


def test_a_robot_pushed_backwards_is_not_detected_as_such():
    """**A limitation, asserted so it cannot be forgotten.**

    The only odometry this project can read is a position, so the measurable
    quantity is how far the robot is from where it started — which cannot tell
    "drove forward 0.3 m" from "was shoved backwards 0.3 m". An earlier version
    of `forward_m` had a `signed < -0.05` branch to abandon a walk that went the
    wrong way; `hypot` is never negative, so it never fired, and a dead branch
    that looks like a safety check is worse than no check.

    So this test asserts what actually happens: a robot that moves 0.2 m in
    *any* direction is credited with 0.2 m of progress. Detecting the difference
    needs the velocity, which is on the same absent topic.
    """
    control = controller(FakeSession(position=(0.0, 0.0)))
    position_sequence(control, [0.20, 0.20, 0.20])

    result = asyncio.run(control.forward_m(0.35, speed=0.5, timeout=1.0))

    assert result["actual_m"] > 0, "the magnitude is reported with the asked-for sign"
    assert "actual_m" in result


def test_zero_metres_is_a_no_op():
    control = controller(FakeSession())
    assert asyncio.run(control.forward_m(0.0))["skipped"] is True


def test_a_non_finite_distance_is_refused():
    control = controller(FakeSession())
    for value in (float("inf"), float("nan")):
        with pytest.raises(AgentError):
            asyncio.run(control.forward_m(value))


def test_the_signed_distance_is_reported_not_the_path_length():
    """A robot that wandered out and back has travelled the distance and is
    where it started. Reporting the path length as "actual_m" would call that a
    success."""
    control = controller(FakeSession(position=(0.0, 0.0)))
    # Out to 0.4, then back to 0.1: a long path and almost no progress.
    position_sequence(control, [0.2, 0.4, 0.3, 0.1, 0.1, 0.1])

    result = asyncio.run(control.forward_m(0.8, speed=0.5, timeout=1.0))

    assert result["actual_m"] == pytest.approx(0.1, abs=0.05), result
    assert result["reason"] != "reached"
