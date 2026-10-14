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
import time

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
    nothing, and the caller is the one that knows whether a guess is useful.

    **And it has to command what the closed loop commands.** This test used to
    assert only `reason`, `requested_m`, `timed_s` and the note — and the code it
    was passing computed the duration as `metres / speed_mps` while publishing a
    **full-scale stick**: `forward_m(0.35)` was 0.624 stick-seconds, which is
    0.87 m at 1.4 m/s and 1.87 m at 3 m/s, for a requested 0.35 m. The
    over-drive factor was the robot's top speed.

    Same shape as the sign bug on this same line: the test checked what was
    *reported*, not what was *commanded*.
    """
    session = FakeSession(topic=False)
    control = controller(session)
    result = asyncio.run(control.forward_m(0.35, speed=0.5))

    assert result["reason"] == "no_odometry"
    assert result["requested_m"] == 0.35
    assert "猜" in result["note"]

    # **The invariant is an upper bound, not equality.** `drive` runs a
    # trapezoid profile, so a duration short enough to be still accelerating
    # never reaches full scale — measured 0.4286 for a 0.5 request. What must
    # never happen is commanding *more* than the closed loop would, so the check
    # is `<= speed`, and `> 0` so a silent no-op cannot pass it.
    commanded = [abs(ly) for _lx, ly, _rx in session.published if ly]
    assert commanded, "nothing was published"
    assert 0.0 < max(commanded) <= 0.5 + 0.01, (
        f"the fallback commanded {max(commanded)} for a speed of 0.5 — the closed "
        f"loop commands exactly `speed`, and anything above it is the units bug: "
        f"a full stick is `speed / 1.0` times faster than the duration assumes, "
        f"and the error is the robot's top speed in metres per second"
    )


def test_the_timed_fallback_is_not_a_sprint():
    """The measured consequence, as an assertion.

    The review behind this found `forward_m(0.35)` producing 0.624 stick-seconds
    of *full-scale* command. The formation prefers `robot_walk` for forward
    corrections and the project's own hardware publishes no odometry, so every
    formation step's forward correction was that sprint — two dogs in a room,
    aimed at each other by a loop that believed it was shuffling 35 cm.
    """
    session = FakeSession(topic=False)
    control = controller(session)
    result = asyncio.run(control.forward_m(0.35, speed=0.28))

    # Stick-seconds of command: the integral the robot actually receives. With
    # the fix this is a fraction of the old 0.624, which was full-scale.
    integral = sum(abs(ly) for _lx, ly, _rx in session.published) * (1.0 / 50.0)
    assert integral < 0.35, (
        f"{integral:.3f} stick-seconds for a 0.35 m shuffle at 0.28 stick — the "
        f"full-scale version measured 0.624, which is 0.87 m at 1.4 m/s"
    )
    assert result["stick"] == pytest.approx(0.28, abs=0.01)


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


def test_a_forward_walk_commands_forward():
    """**The bug the first version had, and the reason this test exists.**

    The publish line was `ly=YAW_AXIS_SIGN * sign * speed`, copying the yaw
    axis's sign convention onto the forward one. `YAW_AXIS_SIGN` is -1.0, so
    `robot_walk(0.3)` walked **backwards** — and every existing test passed,
    because they checked `reason` and `actual_m` (both derived from a simulated
    odometry that moved whichever way it was told) and never the direction that
    was actually commanded.

    So this asserts the sign of what goes on the wire, which is the one thing
    the loop can be wrong about while still reporting success.
    """
    control = controller(FakeSession(position=(0.0, 0.0)))
    position_sequence(control, [0.05, 0.10, 0.20, 0.30, 0.40])

    asyncio.run(control.forward_m(0.35, speed=0.5))

    forward_frames = [ly for _lx, ly, _rx in control.session.published if ly]
    assert forward_frames, "nothing was published, so nothing was tested"
    assert all(ly > 0 for ly in forward_frames), (
        f"a forward walk commanded {forward_frames} — `ly` goes straight to "
        f"forward velocity and must not carry the yaw axis's sign"
    )


def test_a_backward_walk_commands_backward():
    """The mirror, so a sign flip cannot pass both."""
    control = controller(FakeSession(position=(0.0, 0.0)))
    position_sequence(control, [0.05, 0.10, 0.20])

    asyncio.run(control.forward_m(-0.2, speed=0.5))

    forward_frames = [ly for _lx, ly, _rx in control.session.published if ly]
    assert forward_frames and all(ly < 0 for ly in forward_frames), forward_frames


def test_a_turn_does_not_touch_the_forward_axis():
    """Turning is commanded on `rx` only, and the two conventions must not
    leak into each other in either direction."""
    session = FakeSession(yaw=0.0)
    control = controller(session)
    reads = {"n": 0}

    async def yaw_progress(timeout=None):
        reads["n"] += 1
        session.yaw = math.radians(min(90.0, reads["n"] * 20.0))
        return session.yaw

    control.read_yaw = yaw_progress  # type: ignore[assignment]
    asyncio.run(control.turn(90.0))

    assert session.published
    for lx, ly, _rx in session.published:
        assert lx == 0.0 and ly == 0.0, "a turn moved the robot"
    assert any(rx for _lx, _ly, rx in session.published), "a turn never turned"


def test_a_walk_that_is_going_nowhere_gives_up_early():
    """**Eight seconds per step is most of a routine**, and the operator is
    watching a progress bar that is not progressing. A robot held in place, on
    a slope it cannot climb, or reporting an odometry that does not move are all
    the same case: waiting does not help."""
    control = controller(FakeSession(position=(0.0, 0.0)))
    position_sequence(control, [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])

    started = time.monotonic()
    result = asyncio.run(control.forward_m(0.3, speed=0.5, timeout=30.0))
    elapsed = time.monotonic() - started

    assert result["reason"] == "stalled", result
    assert result["actual_m"] == pytest.approx(0.0, abs=0.01)
    assert elapsed < 5.0, f"it waited {elapsed:.1f}s on a robot that was not moving"


def test_a_slow_walk_is_not_mistaken_for_a_stalled_one():
    """The stall check compares against the best progress so far, not against
    the last tick — a robot that creeps is slow, not stuck."""
    control = controller(FakeSession(position=(0.0, 0.0)))
    # One centimetre every read: slower than the stall window would allow if it
    # were comparing consecutive samples.
    steps = [round(0.01 * n, 3) for n in range(1, 40)]
    position_sequence(control, steps)

    result = asyncio.run(control.forward_m(0.3, speed=0.05, timeout=30.0))

    assert result["reason"] in ("reached", "timeout"), result
    assert result["reason"] != "stalled", (
        "a creeping robot was called stuck"
    )


def test_every_exit_path_reports_its_own_reason():
    """**The fix for this was a tautology, and a review caught it.**

    `("timeout" if not aborted else "stopped")` was rewritten as
    `("stopped" if aborted else "timeout")` — **logically identical**, so all
    four exit paths came out byte-for-byte unchanged. The deadline still
    reported "stopped", because `while … else` runs when the loop exits without
    a `break`, and that sets `aborted`.

    `aborted` meant two things at once: "the operator stopped it" (the e-stop
    and the dead-man set `_abort`) and "it ran out of time". One flag, two
    meanings, and the word the operator reads was wrong for one of them.

    So this walks all four paths and asserts the word each one produces.
    """
    # 1. Arrived.
    control = controller(FakeSession(position=(0.0, 0.0)))
    position_sequence(control, [0.05, 0.10, 0.20, 0.30, 0.40])
    assert asyncio.run(control.forward_m(0.35, speed=0.5))["reason"] == "reached"

    # 2. Told to walk and not walking.
    control = controller(FakeSession(position=(0.0, 0.0)))
    position_sequence(control, [0.0] * 8)
    assert asyncio.run(
        control.forward_m(0.3, speed=0.5, timeout=30.0)
    )["reason"] == "stalled"

    # 3. Moving, but too slowly to arrive before the deadline.
    control = controller(FakeSession(position=(0.0, 0.0)))
    position_sequence(control, [round(0.001 * n, 4) for n in range(1, 4000)])
    assert asyncio.run(
        control.forward_m(5.0, speed=0.05, timeout=1.0)
    )["reason"] == "timeout"


async def test_an_abort_mid_walk_says_stopped_not_timeout():
    """The fourth path, and the one the tautology got wrong.

    The abort has to be set **while the walk is running**: `_window` clears
    `_abort` on entry, so setting it first does nothing — which made the first
    version of this probe report `timeout` and look like a code failure.
    """
    control = controller(FakeSession(position=(0.0, 0.0)))
    position_sequence(control, [round(0.0005 * n, 5) for n in range(1, 8000)])

    task = asyncio.create_task(control.forward_m(5.0, speed=0.05, timeout=5.0))
    await asyncio.sleep(0.25)
    control._abort.set()  # noqa: SLF001 - the e-stop's own flag
    result = await task

    assert result["reason"] == "stopped", result
    assert result["ok"] is False
