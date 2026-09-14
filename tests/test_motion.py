"""Motion: velocity profiles, profiled driving, IMU closed-loop turning.

The turn tests run against the mock's integrated yaw, so a real feedback
loop is exercised without a robot.
"""

from __future__ import annotations

import math

import pytest

from unitree_robot_agent.cli import _invoke
from unitree_robot_agent.errors import AgentError
from unitree_robot_agent.motion import MotionController, TrapezoidProfile, wrap_angle
from unitree_robot_agent.runtime import create_runtime
from unitree_robot_agent.server import build_server

from .conftest import make_config


def runtime_for(**overrides):
    return create_runtime(make_config(**overrides))


def record_joystick(runtime) -> list:
    """Capture every published joystick frame.

    `zero_joystick()` calls `session.joystick()` with no arguments, so the
    defaults have to be filled in here or the frames look empty.
    """
    frames: list = []
    original = runtime.session.joystick

    async def spy(**kwargs):
        frame = {"lx": 0.0, "ly": 0.0, "rx": 0.0}
        frame.update(kwargs)
        frames.append(frame)
        await original(**kwargs)

    runtime.session.joystick = spy
    return frames


# ─── profile ──────────────────────────────────────────────────────────


def test_long_move_is_a_trapezoid_that_reaches_the_peak():
    profile = TrapezoidProfile(peak=1.0, duration=4.0, accel=1.5)
    assert profile.shape == "trapezoid"
    assert profile.ramp == pytest.approx(1.0 / 1.5, rel=1e-6)
    assert profile.value_at(0) == 0.0
    assert profile.value_at(2.0) == pytest.approx(1.0)
    assert profile.value_at(4.0) == 0.0
    assert profile.value_at(0.5) == pytest.approx(0.75)


def test_short_move_becomes_a_triangle_but_still_reaches_the_peak():
    """The bug this replaced: a fixed-rate ramp meant a 0.3 s move never got
    anywhere near the requested speed, so the distance was unpredictable."""
    profile = TrapezoidProfile(peak=0.5, duration=0.3, accel=1.5)
    assert profile.shape == "triangle"
    assert profile.ramp == pytest.approx(0.15)
    assert profile.value_at(0.15) == pytest.approx(0.5), "must still hit the peak"
    assert profile.value_at(0.3) == 0.0


def test_profile_always_ends_at_zero():
    for duration in (0.05, 0.3, 1.0, 10.0):
        profile = TrapezoidProfile(peak=0.8, duration=duration)
        steps = profile.steps(0.05)
        assert steps[-1][1] == 0.0, duration
        assert profile.value_at(duration) == 0.0


def test_travel_is_the_area_under_the_curve():
    trapezoid = TrapezoidProfile(peak=1.0, duration=2.0, accel=2.0)
    # ramp 0.5s each side -> cruise 1.0s -> 1.0*1.0 + 1.0*0.5
    assert trapezoid.travel() == pytest.approx(1.5, rel=1e-9)


def test_zero_peak_or_duration_is_inert():
    for profile in (
        TrapezoidProfile(peak=0.0, duration=2.0),
        TrapezoidProfile(peak=1.0, duration=0.0),
    ):
        assert profile.shape == "none"
        assert profile.value_at(0.5) == 0.0
        assert profile.travel() == 0.0


def test_steps_are_paced_by_dt():
    profile = TrapezoidProfile(peak=0.4, duration=0.5, accel=1.5)
    steps = profile.steps(0.05)
    assert len(steps) == 11  # 10 ticks plus the closing zero
    assert [round(t, 2) for t, _ in steps][:3] == [0.0, 0.05, 0.1]


def test_wrap_angle_shortest_path():
    assert wrap_angle(0.0) == 0.0
    assert wrap_angle(math.pi / 2) == pytest.approx(math.pi / 2)
    assert wrap_angle(2 * math.pi) == pytest.approx(0.0)
    assert wrap_angle(math.pi + 0.1) == pytest.approx(-math.pi + 0.1)
    assert wrap_angle(-3 * math.pi) == pytest.approx(-math.pi)


# ─── drive ────────────────────────────────────────────────────────────


async def test_drive_publishes_a_profiled_ramp_and_ends_at_zero():
    runtime = runtime_for()
    frames = record_joystick(runtime)
    stats = await runtime.motion.drive(ly=0.6, duration=0.4)

    assert stats["frames"] > 4
    assert frames[0]["ly"] == 0.0, "must ease in"
    assert max(f["ly"] for f in frames) == pytest.approx(0.6, abs=1e-6), "must reach the peak"
    assert frames[-1]["ly"] == 0.0, "must finish stopped"
    assert stats["profile"]["shape"] == "triangle"
    await runtime.aclose()


async def test_drive_scales_all_axes_together_to_keep_the_heading():
    runtime = runtime_for()
    frames = record_joystick(runtime)
    await runtime.motion.drive(lx=0.5, ly=0.5, duration=0.3)

    moving = [f for f in frames if abs(f["ly"]) > 1e-9]
    assert moving
    for frame in moving:
        assert frame["lx"] == pytest.approx(frame["ly"]), "axes must share one scale"
    await runtime.aclose()


async def test_drive_zeroes_the_sticks_even_if_a_publish_fails():
    runtime = runtime_for()
    calls = record_joystick(runtime)
    original = runtime.session.joystick
    state = {"n": 0}

    async def flaky(**kwargs):
        state["n"] += 1
        if state["n"] == 3:
            raise RuntimeError("link hiccup")
        await original(**kwargs)

    runtime.session.joystick = flaky
    with pytest.raises(RuntimeError):
        await runtime.motion.drive(ly=0.5, duration=0.3)
    # zero_joystick() is what recovers; it must have been reached.
    assert calls[-1]["lx"] == 0.0 and calls[-1]["ly"] == 0.0
    await runtime.aclose()


# ─── closed-loop turn ─────────────────────────────────────────────────


async def test_turn_left_increases_yaw():
    runtime = runtime_for()
    await runtime.session.ensure_connected()
    start = await runtime.motion.read_yaw()
    result = await runtime.motion.turn(60, speed=0.5, timeout=8.0)
    end = await runtime.motion.read_yaw()

    assert result["turned_deg"] > 45, result
    assert abs(result["residual_deg"]) <= result["tolerance_deg"]
    assert result["within_tolerance"] is True
    assert math.degrees(end - start) > 45
    await runtime.aclose()


async def test_turn_right_decreases_yaw():
    runtime = runtime_for()
    await runtime.session.ensure_connected()
    start = await runtime.motion.read_yaw()
    result = await runtime.motion.turn(-50, speed=0.5, timeout=8.0)
    end = await runtime.motion.read_yaw()

    assert result["turned_deg"] < -40, result
    assert end < start
    await runtime.aclose()


async def test_turns_accumulate():
    runtime = runtime_for()
    await runtime.session.ensure_connected()
    await runtime.motion.turn(40, speed=0.5, timeout=8.0)
    after_first = await runtime.motion.read_yaw()
    await runtime.motion.turn(-20, speed=0.5, timeout=8.0)
    after_second = await runtime.motion.read_yaw()

    assert math.degrees(after_first) > 30
    assert after_second < after_first
    await runtime.aclose()


async def test_turn_watchdog_gives_up_and_reports_the_residual():
    """A turn that cannot make progress must end at the timeout, not hang.

    The yaw is readable but frozen (the robot is stuck), so the loop has to
    bail out on its own and tell the caller the truth: it did not get there.
    """
    runtime = runtime_for()

    async def frozen_yaw(*args, **kwargs):
        return 0.5

    runtime.motion.read_yaw = frozen_yaw
    result = await runtime.motion.turn(90, speed=0.4, timeout=0.6)

    assert result["ok"] is False, "a stuck turn must not claim success"
    assert result["frames"] > 0, "it should have tried"
    assert abs(result["residual_deg"]) > 80, result
    assert result["within_tolerance"] is False
    await runtime.aclose()


async def test_turn_requires_a_readable_imu():
    """No IMU means no closed loop — that must be an error, not a guess."""

    async def no_imu(*args, **kwargs):
        return None

    runtime = runtime_for()
    runtime.session.read_topic = no_imu
    with pytest.raises(AgentError) as excinfo:
        await runtime.motion.turn(30, timeout=0.5)
    assert excinfo.value.code == "no_imu"
    assert "robot_joystick" in excinfo.value.hint
    await runtime.aclose()


async def test_turn_zero_is_a_no_op():
    runtime = runtime_for()
    result = await runtime.motion.turn(0)
    assert result["ok"] is True and result["skipped"] is True
    await runtime.aclose()


# ─── through the tool layer ───────────────────────────────────────────


def bundle_for(**overrides):
    return build_server(create_runtime(make_config(**overrides)))


async def test_turn_tool_requires_confirmation():
    bundle = bundle_for()
    result = await _invoke(bundle, "robot_turn", {"degrees": 45})
    assert result["error"] == "confirm_required"
    await bundle.context.runtime.aclose()


async def test_turn_tool_zero_degrees_needs_no_confirmation():
    bundle = bundle_for()
    result = await _invoke(bundle, "robot_turn", {"degrees": 0})
    assert result["ok"] is True and result["skipped"] is True
    await bundle.context.runtime.aclose()


async def test_turn_tool_rejects_absurd_angles():
    bundle = bundle_for()
    result = await _invoke(bundle, "robot_turn", {"degrees": 5000, "confirm": True})
    assert result["error"] == "guard_rejected"
    assert "1080" in result["message"]
    await bundle.context.runtime.aclose()


async def test_turn_tool_reports_measured_motion():
    bundle = bundle_for()
    result = await _invoke(bundle, "robot_turn", {"degrees": 45, "confirm": True})
    assert result["ok"] is True
    assert result["turned_deg"] > 30
    assert "residual_deg" in result and "tolerance_deg" in result
    await bundle.context.runtime.aclose()


async def test_turn_is_blocked_in_safe_mode():
    bundle = bundle_for(safe_mode=True)
    result = await _invoke(bundle, "robot_turn", {"degrees": 45, "confirm": True})
    assert result["error"] == "guard_rejected"
    await bundle.context.runtime.aclose()


async def test_joystick_tool_exposes_the_profile_it_used():
    bundle = bundle_for()
    result = await _invoke(bundle, "robot_joystick", {"ly": 0.3, "duration_s": 0.5, "confirm": True})
    assert result["ok"] is True
    assert result["profile"]["shape"] in ("triangle", "trapezoid")
    assert result["profile"]["duration_s"] == pytest.approx(0.5)
    await bundle.context.runtime.aclose()
