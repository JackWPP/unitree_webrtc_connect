"""Getting two dogs into formation, using the camera as the only reference.

The geometry is where the bugs are, so most of this file is arithmetic against
pictures that can be checked by hand. A wrong sign here sends the two dogs apart
while the console reports that it is bringing them together.

## The two conventions everything rests on

* **Heading is the nose direction**, 0 = +x, anticlockwise. That is what
  `detect.py` produces and what the printed arrow guarantees.
* **`vy` positive is to the dog's left.** `robot_move` follows Unitree's own
  convention where `y` is left; getting it backwards is at least immediately
  visible, which is more than can be said for most sign errors.

## Why a loop rather than a calculation

`robot_move` takes a **normalised** speed, so a move cannot be ordered as
"travel 0.42 metres" — only as "go forward at 30% for a while", and how far that
goes depends on a constant nobody has measured. The camera measures again after
every step, so a wrong constant costs iterations rather than accuracy.
"""

from __future__ import annotations

import math

import pytest

from unitree_fleet_web.formation import (
    DEFAULT_TOLERANCE_M,
    MAX_STEP_M,
    NOMINAL_SPEED_MPS,
    Formation,
    converged,
    plan,
    wrap_degrees,
)


def dog(x, y, heading):
    return {"x": x, "y": y, "heading_deg": heading}


# ─── the angle arithmetic ─────────────────────────────────────────────


def test_the_short_way_round():
    assert wrap_degrees(350.0) == pytest.approx(-10.0)
    assert wrap_degrees(-370.0) == pytest.approx(-10.0)
    assert wrap_degrees(190.0) == pytest.approx(-170.0)


# ─── already there ────────────────────────────────────────────────────


def test_a_formation_already_held_is_left_alone():
    """The most important case in practice: pressing the button twice must not
    shuffle two robots that are already where they should be."""
    poses = {"dog1": dog(1.0, 1.0, 0.0), "dog2": dog(2.0, 1.0, 0.0)}
    result = plan(poses, Formation(distance_m=1.0, bearing_deg=0.0))
    assert result.converged is True
    assert result.moves == []


def test_parallel_headings_count_as_converged():
    poses = {"dog1": dog(1.0, 1.0, 45.0), "dog2": dog(2.0, 1.0, 45.0)}
    assert plan(poses, Formation(distance_m=1.0, heading_spread_deg=0.0)).converged


def test_facing_each_other_is_a_spread_of_180():
    poses = {"dog1": dog(1.0, 1.0, 0.0), "dog2": dog(2.0, 1.0, 180.0)}
    assert plan(poses, Formation(distance_m=1.0, heading_spread_deg=180.0)).converged


# ─── too far apart ────────────────────────────────────────────────────


def test_too_far_apart_brings_b_forward_along_its_own_nose():
    """dog2 is one metre too far to the east and faces east, so its own forward
    axis is the direction it has to travel: forward positive, lateral zero."""
    poses = {"dog1": dog(1.0, 0.0, 0.0), "dog2": dog(3.0, 0.0, 0.0)}
    result = plan(poses, Formation(distance_m=1.0, bearing_deg=0.0, mover="b"))

    assert result.converged is False
    assert result.distance_error_m == pytest.approx(1.0, abs=0.01)
    move = next(m for m in result.moves if m.robot_id == "dog2")
    # Capped at one step.
    assert move.forward_m == pytest.approx(-MAX_STEP_M, abs=0.001)
    assert move.lateral_m == pytest.approx(0.0, abs=0.001)


def test_the_same_error_reached_from_a_different_heading():
    """dog2 faces north and has to travel west, and **west is a dog's left when
    it faces north** — so the lateral term is positive.

    Worth writing down because the opposite reads as obviously right until it is
    drawn: this is the sign that sends two dogs apart while the console reports
    it is bringing them together.
    """
    poses = {"dog1": dog(1.0, 0.0, 0.0), "dog2": dog(3.0, 0.0, 90.0)}
    result = plan(poses, Formation(distance_m=1.0, bearing_deg=0.0, mover="b"))

    move = next(m for m in result.moves if m.robot_id == "dog2")
    assert move.forward_m == pytest.approx(0.0, abs=0.01)
    assert move.lateral_m == pytest.approx(MAX_STEP_M, abs=0.01), (
        "facing north and travelling west is travelling left"
    )


def test_travelling_east_while_facing_north_is_travelling_right():
    """The mirror of the case above, so a sign flip cannot pass both."""
    poses = {"dog1": dog(1.0, 0.0, 0.0), "dog2": dog(0.0, 0.0, 90.0)}
    result = plan(poses, Formation(distance_m=1.0, bearing_deg=0.0, mover="b"))

    move = next(m for m in result.moves if m.robot_id == "dog2")
    assert move.lateral_m == pytest.approx(-MAX_STEP_M, abs=0.01)


def test_a_diagonal_gap_is_split_between_the_two_axes():
    poses = {"dog1": dog(0.0, 0.0, 0.0), "dog2": dog(1.0, 1.0, 0.0)}
    result = plan(poses, Formation(distance_m=0.0, bearing_deg=0.0, mover="b"))

    move = next(m for m in result.moves if m.robot_id == "dog2")
    # Equal components, capped so the *magnitude* is one step.
    assert move.forward_m == pytest.approx(-MAX_STEP_M / math.sqrt(2), abs=0.01)
    assert move.lateral_m == pytest.approx(-MAX_STEP_M / math.sqrt(2), abs=0.01)
    assert move.distance_m == pytest.approx(MAX_STEP_M, abs=0.01)


# ─── the bearing, not just the distance ───────────────────────────────


def test_the_bearing_moves_b_around_a_not_along_the_gap():
    """Two dogs a metre apart due north, told to stand in line to the east.

    The distance is already right and the direction is entirely wrong, so the
    correction is a quarter turn about A rather than anything along the line
    between them — which is the difference between a formation and a queue.
    """
    poses = {"dog1": dog(0.0, 0.0, 0.0), "dog2": dog(0.0, 1.0, 0.0)}
    result = plan(poses, Formation(distance_m=1.0, bearing_deg=0.0, mover="b"))

    move = next(m for m in result.moves if m.robot_id == "dog2")
    # It has to end up at (1, 0) from (0, 1): south-east, and further than one
    # step, so the step is capped and points that way.
    assert result.distance_error_m == pytest.approx(math.sqrt(2), abs=0.01)
    assert move.distance_m == pytest.approx(MAX_STEP_M, abs=0.01)
    assert move.forward_m > 0 and move.lateral_m < 0


# ─── headings ─────────────────────────────────────────────────────────


def test_a_heading_error_turns_only_b():
    """Turning both dogs to fix one spread is two chances to overshoot."""
    poses = {"dog1": dog(0.0, 0.0, 0.0), "dog2": dog(1.0, 0.0, 40.0)}
    result = plan(poses, Formation(distance_m=1.0, heading_spread_deg=0.0))

    assert result.yaw_error_deg == pytest.approx(-40.0, abs=0.1)
    by_id = {move.robot_id: move for move in result.moves}
    assert by_id["dog1"].turn_deg == 0.0
    assert by_id["dog2"].turn_deg == pytest.approx(-30.0, abs=0.1), "the cap did not hold"


def test_a_small_heading_error_is_ignored_rather_than_chased():
    """Turning a robot two degrees costs a second and a shuffle, and the
    measurement is not that precise anyway.

    The distance is wrong on purpose, so the plan has moves to inspect — an
    earlier version of this test had everything within tolerance and there was
    nothing to look at.
    """
    poses = {"dog1": dog(0.0, 0.0, 0.0), "dog2": dog(2.0, 0.0, 5.0)}
    result = plan(poses, Formation(distance_m=1.0, heading_spread_deg=0.0))

    by_id = {move.robot_id: move for move in result.moves}
    assert "dog2" in by_id, "nothing was planned, so nothing was tested"
    assert by_id["dog2"].turn_deg == 0.0
    assert result.converged is False


def test_the_spread_is_measured_against_a_not_against_north():
    """"Parallel to each other" must not become "both facing east". A pair at
    45° to the arena is still parallel, and a formation that turned them to
    north every time would rotate the whole pair whenever A moved."""
    poses = {"dog1": dog(1.0, 1.0, 45.0), "dog2": dog(2.0, 1.0, 45.0)}
    state = converged(poses, Formation(distance_m=1.0, heading_spread_deg=0.0))
    assert state["ready"] is True
    assert state["yaw_error_deg"] == pytest.approx(0.0, abs=0.1)


# ─── splitting the work ───────────────────────────────────────────────


def test_splitting_the_correction_moves_both_dogs():
    """Which is what a pair should do when neither is the reference: A backs off
    while B comes forward, and they meet in the middle."""
    poses = {"dog1": dog(0.0, 0.0, 0.0), "dog2": dog(3.0, 0.0, 0.0)}
    result = plan(poses, Formation(distance_m=1.0, bearing_deg=0.0, mover="both"))

    by_id = {move.robot_id: move for move in result.moves}
    assert set(by_id) == {"dog1", "dog2"}
    # B moves towards A, A moves towards B.
    assert by_id["dog2"].forward_m < 0
    assert by_id["dog1"].forward_m > 0
    assert result.distance_error_m == pytest.approx(2.0, abs=0.01)


def test_holding_one_still_is_available():
    poses = {"dog1": dog(0.0, 0.0, 0.0), "dog2": dog(3.0, 0.0, 0.0)}
    result = plan(poses, Formation(distance_m=1.0, bearing_deg=0.0, mover="b"))
    assert [move.robot_id for move in result.moves] == ["dog2"]


# ─── the move arguments ───────────────────────────────────────────────


def test_a_move_is_ordered_as_a_fraction_of_the_robots_own_speed():
    """`robot_move`'s `vx` is normalised, not metres per second — so the command
    cannot say "travel 0.42 m", only "go forward at this fraction for this
    long". The loop re-measures, which is what makes that survivable."""
    poses = {"dog1": dog(0.0, 0.0, 0.0), "dog2": dog(3.0, 0.0, 0.0)}
    result = plan(poses, Formation(distance_m=1.0, mover="b"))
    move = next(m for m in result.moves if m.robot_id == "dog2")

    args = move.move_args(speed_mps=NOMINAL_SPEED_MPS)
    assert 0.2 <= args["duration_s"] <= 2.5
    assert abs(args["vx"]) <= 0.6, "a wrong speed constant must not make a dog lurch"
    assert args["confirm"] is True
    # The direction survives the conversion.
    assert args["vx"] < 0
    # ...and the duration is what carries the distance, which is the conversion
    # the first version had backwards: it made shorter moves faster.
    assert args["duration_s"] == pytest.approx(move.distance_m / NOMINAL_SPEED_MPS,
                                               abs=0.05)


def test_a_shorter_move_is_not_ordered_faster():
    """The bug the first version had: scaling speed by the inverse of the
    duration meant a 20 cm shuffle was ordered at full tilt."""
    from unitree_fleet_web.formation import DogMove

    short = DogMove("d", 0.05, 0.0, 0.0, 0, 0, 0)
    far = DogMove("d", 0.35, 0.0, 0.0, 0, 0, 0)

    assert abs(short.move_args()["vx"]) == pytest.approx(abs(far.move_args()["vx"]))
    assert short.move_args()["duration_s"] < far.move_args()["duration_s"]


def test_a_move_that_is_already_done_asks_for_nothing():
    from unitree_fleet_web.formation import DogMove

    move = DogMove("dog2", 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    assert move.move_args() == {}


def test_the_speed_constant_is_a_parameter_because_it_is_a_guess():
    """A session that measures it once should converge in fewer moves, and the
    module says out loud that the default is not a measurement."""
    poses = {"dog1": dog(0.0, 0.0, 0.0), "dog2": dog(3.0, 0.0, 0.0)}
    move = next(
        m for m in plan(poses, Formation(mover="b")).moves if m.robot_id == "dog2"
    )
    fast = move.move_args(speed_mps=3.0)
    slow = move.move_args(speed_mps=0.6)
    # A higher travel speed covers the same distance in less time...
    assert fast["duration_s"] < slow["duration_s"]
    # ...at a larger fraction of the robot's maximum.
    assert abs(fast["vx"]) > abs(slow["vx"])


# ─── when it cannot be done ───────────────────────────────────────────


def test_one_dog_in_frame_is_not_a_formation():
    result = plan({"dog1": dog(0.0, 0.0, 0.0)}, Formation())
    assert result.moves == []
    assert result.notes, "a refusal has to say why"


def test_the_measurement_reports_what_it_is_short_of():
    poses = {"dog1": dog(1.0, 0.0, 0.0), "dog2": dog(2.6, 0.0, 0.0)}
    state = converged(poses, Formation(distance_m=1.0, heading_spread_deg=0.0))
    assert state["ready"] is False
    assert state["distance_error_m"] == pytest.approx(0.6, abs=0.01)
    assert state["distance_m"] == pytest.approx(1.6, abs=0.01)


def test_the_tolerance_is_what_decides_ready():
    poses = {"dog1": dog(0.0, 0.0, 0.0), "dog2": dog(1.05, 0.0, 0.0)}
    target = Formation(distance_m=1.0, heading_spread_deg=0.0)
    assert converged(poses, target)["ready"] is True
    assert converged(poses, target, tolerance_m=0.01)["ready"] is False
    assert DEFAULT_TOLERANCE_M == pytest.approx(0.10)
