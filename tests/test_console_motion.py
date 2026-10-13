"""The velocity ramp: what makes the robot start gently and stop firmly.

Written against the two failures it exists to prevent. Without a ramp, a stick
moved to full sends the robot from rest to its maximum in one frame — a lurch
that a quadruped answers by pitching forward. And without a *deceleration*
ramp, letting go cuts the commanded velocity to zero instantly, which is a skid
rather than a stop.
"""

from __future__ import annotations

import pytest

from unitree_fleet_web.motion import (
    DEADZONE,
    MotionController,
    MotionLimits,
    coupled_yaw_limit,
    ramp,
)


# ─── the primitive ────────────────────────────────────────────────────


def test_the_ramp_moves_at_most_one_step():
    assert ramp(0.0, 1.0, accel=0.5, decel=1.0, dt=0.1) == pytest.approx(0.05)


def test_the_ramp_uses_the_deceleration_rate_when_falling():
    """The asymmetry is the point: firm stop, gentle start."""
    assert ramp(1.0, 0.0, accel=0.5, decel=1.0, dt=0.1) == pytest.approx(0.9)


def test_reversing_is_a_deceleration_through_zero():
    """A full-reverse stick is a stop and then a start, so it must shed the old
    direction at the deceleration rate. Testing magnitudes alone gets this case
    wrong — |0.8| is not less than |0.8|, so it reads as an acceleration and
    the reversal snaps."""
    assert ramp(0.8, -0.8, accel=0.5, decel=1.0, dt=0.1) == pytest.approx(0.7)
    # ...while genuinely growing from rest is still an acceleration.
    assert ramp(0.0, 0.5, accel=0.5, decel=1.0, dt=0.1) == pytest.approx(0.05)


def test_the_ramp_lands_exactly_on_the_goal_rather_than_overshooting():
    assert ramp(0.98, 1.0, accel=0.5, decel=1.0, dt=0.1) == 1.0


def test_an_unchanged_goal_is_returned_as_is():
    assert ramp(0.4, 0.4, accel=0.5, decel=1.0, dt=0.1) == 0.4


# ─── turn coupling ────────────────────────────────────────────────────


def test_full_yaw_authority_at_a_standstill():
    assert coupled_yaw_limit(0.0) == 1.0


def test_yaw_is_reduced_at_speed_but_never_locked():
    """A robot that cannot be turned at speed cannot be steered out of
    trouble, so the floor is deliberate."""
    assert coupled_yaw_limit(1.0) == pytest.approx(0.25)
    assert coupled_yaw_limit(1.0) > 0


def test_the_coupling_is_monotonic():
    values = [coupled_yaw_limit(r) for r in (0.0, 0.25, 0.5, 0.75, 1.0)]
    assert values == sorted(values, reverse=True)


# ─── the controller ───────────────────────────────────────────────────


def test_a_full_stick_reaches_full_over_several_ticks_not_one():
    controller = MotionController(MotionLimits(accel=0.5, decel=1.0, turn_coupling=False))
    controller.set_target(vx=1.0, vy=0.0, vyaw=0.0)

    first = controller.tick(0.1)[0]
    assert first == pytest.approx(0.05), "it jumped straight to full speed"

    for _ in range(30):
        last = controller.tick(0.1)[0]
    assert last == pytest.approx(1.0)


def test_releasing_the_stick_decelerates_rather_than_cutting():
    controller = MotionController(MotionLimits(accel=0.5, decel=1.0, turn_coupling=False))
    controller.set_target(vx=1.0, vy=0.0, vyaw=0.0)
    for _ in range(30):
        controller.tick(0.1)

    controller.stop()
    step = controller.tick(0.1)[0]
    assert 0.0 < step < 1.0, "the commanded velocity was cut instead of ramped"
    assert step == pytest.approx(0.9)


def test_it_stops_completely_and_says_so():
    controller = MotionController(MotionLimits(decel=1.0, turn_coupling=False))
    controller.set_target(vx=1.0, vy=0.0, vyaw=0.0)
    for _ in range(40):
        controller.tick(0.1)
    controller.stop()
    for _ in range(40):
        controller.tick(0.1)

    assert controller.commanded == (0.0, 0.0, 0.0)
    assert controller.moving is False
    assert controller.state == "idle"


def test_the_deadzone_swallows_stick_drift_without_losing_travel():
    """Real hardware does not centre perfectly. A robot that creeps when you
    are not touching it is worse than one that ignores the first millimetre."""
    controller = MotionController(turn_coupling=False)
    controller.set_target(vx=DEADZONE / 2, vy=0.0, vyaw=0.0)
    assert controller.target[0] == 0.0

    # ...and just past it gives just-off-centre speed, not a jump.
    controller.set_target(vx=DEADZONE + (1 - DEADZONE) * 0.1, vy=0.0, vyaw=0.0)
    assert 0.0 < controller.target[0] < 0.2


def test_the_watchdog_decelerates_a_stick_nobody_is_holding():
    """A closed tab mid-throttle must coast down, not cut, and the reason has
    to have a name the UI can show."""
    controller = MotionController(
        MotionLimits(watchdog_s=0.5, accel=1.0, decel=1.0, turn_coupling=False)
    )
    now = 1000.0
    controller.set_target(vx=1.0, vy=0.0, vyaw=0.0, now=now)
    for _ in range(10):
        controller.tick(0.05, now=now)

    assert controller.state == "driving"

    # Time passes with no further frames.
    coasting = controller.tick(0.05, now=now + 1.0)
    assert controller.state == "watchdog"
    assert 0.0 < coasting[0] < 1.0, "the watchdog cut the velocity instead of ramping it"


def test_the_watchdog_does_not_fire_while_frames_keep_arriving():
    controller = MotionController(MotionLimits(watchdog_s=0.5, turn_coupling=False))
    now = 1000.0
    for _ in range(30):
        now += 0.05
        controller.set_target(vx=1.0, vy=0.0, vyaw=0.0, now=now)
        controller.tick(0.05, now=now)

    assert controller.state == "driving"


def test_a_latched_estop_does_not_ramp():
    """The deceleration ramp is exactly the delay an e-stop must not have."""
    controller = MotionController()
    controller.set_target(vx=1.0, vy=0.0, vyaw=0.0)
    for _ in range(30):
        controller.tick(0.05)

    controller.hard_stop()

    assert controller.commanded == (0.0, 0.0, 0.0)
    assert controller.target == (0.0, 0.0, 0.0)
    assert controller.state == "estopped"


def test_a_stalled_loop_cannot_teleport_the_velocity():
    """`dt` is real elapsed time, but it is capped: a loop blocked for five
    seconds must not apply five seconds of acceleration in one tick and lurch
    from rest to full speed. It advances by the cap and catches up over the
    following ticks — slow, but never a jump."""
    controller = MotionController(accel=0.5, turn_coupling=False)
    controller.set_target(vx=1.0, vy=0.0, vyaw=0.0)

    controller.tick(5.0)  # a five-second stall
    assert controller.commanded[0] == pytest.approx(0.25 * 0.5)

    for _ in range(20):
        controller.tick(0.25)
    assert controller.commanded[0] == pytest.approx(1.0)


def test_turn_coupling_reduces_the_yaw_target_at_speed():
    coupled = MotionController(MotionLimits(yaw_accel=5.0, turn_coupling=True))
    free = MotionController(MotionLimits(yaw_accel=5.0, turn_coupling=False))
    for controller in (coupled, free):
        controller.set_target(vx=1.0, vy=0.0, vyaw=1.0)
        for _ in range(60):
            controller.tick(0.1)

    assert coupled.commanded[2] < free.commanded[2]
    assert coupled.commanded[2] > 0, "the yaw was locked out entirely"


def test_the_snapshot_reports_asked_for_and_actually_sent():
    """A stick held at full while the robot creeps is a stall, and a stall is
    invisible if you only publish one of the two values."""
    controller = MotionController(turn_coupling=False)
    controller.set_target(vx=1.0, vy=0.0, vyaw=0.0)
    controller.tick(0.1)

    snap = controller.snapshot()
    assert snap["target"][0] == pytest.approx(1.0)
    assert snap["commanded"][0] == pytest.approx(0.05)
    assert snap["state"] == "driving"
    assert "accel" in snap["limits"]


def test_nonsense_input_does_not_produce_nonsense_motion():
    controller = MotionController(turn_coupling=False)
    controller.set_target(vx=float("nan"), vy=float("inf"), vyaw=-float("inf"))
    for _ in range(5):
        controller.tick(0.1)
    for value in controller.commanded:
        assert -1.0 <= value <= 1.0
