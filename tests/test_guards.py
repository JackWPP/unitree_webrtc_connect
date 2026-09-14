"""Guard rails: the rules the model cannot switch off."""

from __future__ import annotations

import asyncio
import time

import pytest

from unitree_robot_agent.core.audit import AuditLog
from unitree_robot_agent.core.guards import Guard
from unitree_robot_agent.errors import ConfirmRequired, GuardRejected
from unitree_robot_agent.robots import get_profile

from .conftest import make_config


def make_guard(**overrides) -> Guard:
    config = make_config(**overrides)
    return Guard(config, AuditLog(enabled=False), get_profile(config.robot))


def test_movement_requires_explicit_confirmation():
    guard = make_guard()
    with pytest.raises(ConfirmRequired) as excinfo:
        guard.require_confirm(False, "robot_move")
    assert excinfo.value.code == "confirm_required"
    assert "confirm=true" in str(excinfo.value)
    guard.require_confirm(True, "robot_move")  # must not raise


def test_safe_mode_blocks_every_moving_tool():
    guard = make_guard(safe_mode=True)
    with pytest.raises(GuardRejected):
        guard.require_motion_enabled("robot_move")
    with pytest.raises(GuardRejected):
        guard.require_motion_enabled("robot_play_action")


def test_dangerous_actions_can_be_disabled_independently():
    guard = make_guard(allow_dangerous=False)
    with pytest.raises(GuardRejected):
        guard.require_dangerous_allowed("dance_1", dangerous=True)
    guard.require_dangerous_allowed("hello", dangerous=False)  # safe ones pass


def test_duration_is_clamped_to_the_hard_ceiling():
    guard = make_guard(max_move_duration_s=10.0)
    duration, note = guard.clamp_duration(900)
    assert duration == 10.0
    assert "clamped" in note
    assert guard.clamp_duration(2.5) == (2.5, "")
    assert guard.clamp_duration(None)[0] == 1.0


def test_invalid_durations_are_refused():
    guard = make_guard()
    for bad in (0, -3):
        with pytest.raises(GuardRejected):
            guard.clamp_duration(bad)
    with pytest.raises(GuardRejected):
        guard.clamp_duration("fast")


def test_non_finite_input_is_refused_not_clamped():
    """NaN compares false against every bound, so it used to sail through
    both clamps and reach the wire as a literal NaN."""
    guard = make_guard(max_move_duration_s=10.0)
    with pytest.raises(GuardRejected):
        guard.clamp_duration(float("nan"))
    with pytest.raises(GuardRejected):
        guard.clamp_speed(float("nan"), axis="vx", limit=1.0)
    # inf is still clamped to the ceiling, not refused.
    assert guard.clamp_duration(float("inf"))[0] == 10.0
    assert guard.clamp_speed(float("inf"), axis="vx", limit=1.0)[0] == 1.0


def test_speed_is_clamped_per_axis():
    guard = make_guard()
    value, note = guard.clamp_speed(5.0, axis="vx", limit=1.0)
    assert value == 1.0 and "clamped" in note
    assert guard.clamp_speed(-3.0, axis="vx", limit=1.0)[0] == -1.0
    assert guard.clamp_speed(None, axis="vx", limit=1.0)[0] == 0.0


def test_local_transition_check_blocks_what_the_robot_would_refuse():
    guard = make_guard()
    allowed = guard.check_transition(4, 601, action="robot_play_action")
    assert allowed.allowed is True
    refused = guard.check_transition(811, 601, action="robot_play_action")
    assert refused.allowed is False
    assert "Lock" in refused.hint


async def test_rate_limiter_enforces_the_minimum_interval():
    guard = make_guard(min_command_interval_s=0.15)
    start = time.monotonic()
    await guard.throttle()
    await guard.throttle()
    await guard.throttle()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.28, elapsed  # two enforced gaps


async def test_rate_limiter_is_off_when_interval_is_zero():
    guard = make_guard(min_command_interval_s=0.0)
    start = time.monotonic()
    for _ in range(5):
        await guard.throttle()
    assert time.monotonic() - start < 0.1


async def test_throttle_serialises_concurrent_callers():
    guard = make_guard(min_command_interval_s=0.1)
    start = time.monotonic()
    await asyncio.gather(*(guard.throttle() for _ in range(4)))
    assert time.monotonic() - start >= 0.28
