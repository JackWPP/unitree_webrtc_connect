"""Routines with knobs, and the preview that shows what they will do.

`走正方形` walked a fixed 0.4 m leg because the number was a literal. The
operator who wants a bigger square had no way to say so; the one indoors with
two dogs in a room had no way to ask for a smaller one. Both are now parameters.

## The rule that shapes all of this

**"Parameters → steps" happens in exactly one place.** The page asks the server
what a routine would do and renders the answer; it does not compute it. Two
implementations of one fact drift — this project has already shipped a parallax
formula disagreeing with its own scale factor ten lines away, and a
`converged()` disagreeing with the `plan()` it was gating — and a preview that
has drifted from what actually runs is worse than no preview, because it is a
promise.
"""

from __future__ import annotations

import pytest


# ─── the model ────────────────────────────────────────────────────────


def test_the_defaults_reproduce_the_routines_that_shipped():
    """**The parameterised routine has to be the same routine.**

    Adding knobs must not change what the button does when nobody turns one.
    `走正方形` was nine steps and `列队前进` was seven; the first version of
    `_build_square` read `laps` as "legs" rather than "trips round the square"
    and quietly turned nine steps into three.
    """
    from unitree_fleet_web.routines import routine_by_id

    square = routine_by_id("square")
    assert len(square.steps) == 9, "走正方形 is 1 stand + 4 sides × (walk, turn)"
    assert square.steps[0].tool == "robot_stand"

    formation = routine_by_id("formation")
    assert len(formation.steps) == 7, "列队前进 is 1 stand + 3 × (walk, stop)"
    assert formation.steps[0].tool == "robot_stand"


def test_every_routine_that_declares_params_can_build_from_them():
    """A `Param` without a `build` is a knob wired to nothing."""
    from unitree_fleet_web.routines import ROUTINES

    for routine in ROUTINES:
        if routine.params:
            assert routine.build is not None, routine.id
            built = routine.with_params({p.key: p.default for p in routine.params})
            assert built.steps, routine.id
        else:
            assert routine.build is None, routine.id


def test_every_default_sits_inside_its_own_range():
    """A default outside its own bounds is a routine that refuses itself."""
    from unitree_fleet_web.routines import ROUTINES

    for routine in ROUTINES:
        for param in routine.params:
            assert param.minimum <= param.default <= param.maximum, (
                f"{routine.id}.{param.key}: default {param.default} is outside "
                f"[{param.minimum}, {param.maximum}]"
            )
            assert param.minimum < param.maximum, f"{routine.id}.{param.key}"
            assert param.label, f"{routine.id}.{param.key} has no label"


# ─── the parameters actually do something ─────────────────────────────


def test_the_side_length_reaches_the_step():
    """The knob has to reach the wire. A parameter that is validated, stored
    and then ignored looks exactly like one that works, until a dog walks."""
    from unitree_fleet_web.routines import routine_by_id

    square = routine_by_id("square")
    for leg in (0.2, 0.4, 0.8, 2.0):
        built = square.with_params({"leg_m": leg})
        walk = next(s for s in built.steps if s.tool == "robot_walk")
        assert walk.args["meters"] == pytest.approx(leg), leg
        # ...and the declared wait grows with it, because a longer walk takes
        # longer. The reverse would fire the turn mid-stride.
        assert walk.seconds >= leg * 4.0


def test_laps_multiply_the_legs_not_the_square():
    """`laps` counts trips round a four-sided square."""
    from unitree_fleet_web.routines import routine_by_id

    square = routine_by_id("square")
    for laps in (1, 2, 4):
        built = square.with_params({"laps": laps})
        assert len(built.steps) == 1 + laps * 4 * 2, laps
        # Every lap turns the same way, so four of them come back round.
        turns = [s for s in built.steps if s.tool == "robot_turn"]
        assert len(turns) == laps * 4
        assert all(t.args["degrees"] == -90 for t in turns)


def test_the_formation_knobs_reach_the_steps():
    from unitree_fleet_web.routines import routine_by_id

    formation = routine_by_id("formation")
    built = formation.with_params({"step_m": 0.7, "legs": 2, "pause_s": 2.5})
    assert len(built.steps) == 1 + 2 * 2
    walks = [s for s in built.steps if s.tool == "robot_walk"]
    stops = [s for s in built.steps if s.tool == "robot_stop"]
    assert all(w.args["meters"] == pytest.approx(0.7) for w in walks)
    assert all(s.seconds == pytest.approx(2.5) for s in stops)


# ─── refusals name the field ──────────────────────────────────────────


def test_a_non_numeric_parameter_names_the_field():
    """**A dataclass does not type-check.**

    `Formation(distance_m="far")` was accepted without complaint in this project
    once, and blew up inside a loop seconds later with an error that said
    nothing about the form the operator had filled in.
    """
    from unitree_fleet_web.routines import routine_by_id

    square = routine_by_id("square")
    with pytest.raises(ValueError) as caught:
        square.with_params({"leg_m": "far"})
    said = str(caught.value)
    assert "leg_m" in said, said
    assert "数字" in said, said


def test_an_out_of_range_parameter_says_the_range():
    from unitree_fleet_web.routines import routine_by_id

    square = routine_by_id("square")
    with pytest.raises(ValueError) as caught:
        square.with_params({"leg_m": 99})
    said = str(caught.value)
    assert "0.2" in said and "2" in said, said


def test_a_nan_or_infinity_is_refused():
    from unitree_fleet_web.routines import routine_by_id

    square = routine_by_id("square")
    for value in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            square.with_params({"leg_m": value})


def test_an_unknown_parameter_is_refused_rather_than_ignored():
    """Silently using the default would look like the setting had no effect —
    which is exactly how a page-side typo survives a whole session."""
    from unitree_fleet_web.routines import routine_by_id

    square = routine_by_id("square")
    with pytest.raises(ValueError) as caught:
        square.with_params({"side_m": 0.6})
    assert "side_m" in str(caught.value)
    # It should say what *is* available, or the operator is left guessing.
    assert "leg_m" in str(caught.value)


def test_a_routine_without_params_refuses_to_receive_any():
    from unitree_fleet_web.routines import routine_by_id

    with pytest.raises(ValueError):
        routine_by_id("greet").with_params({"x": 1})


def test_omitting_a_parameter_uses_its_default():
    from unitree_fleet_web.routines import routine_by_id

    square = routine_by_id("square")
    only_laps = square.with_params({"laps": 2})
    walk = next(s for s in only_laps.steps if s.tool == "robot_walk")
    assert walk.args["meters"] == pytest.approx(0.4), "leg_m should have defaulted"


# ─── the catalogue contract ───────────────────────────────────────────


def test_the_catalogue_declares_the_params(open_client):
    """The page draws the 设置 button from this, so a routine with knobs and no declared
    params is a feature nobody can reach."""
    listed = {
        r["id"]: r for r in open_client.get("/api/fleet/routines").json()["routines"]
    }
    square = listed["square"]
    assert [p["key"] for p in square["params"]] == ["leg_m", "laps"]
    assert square["params"][0]["unit"].strip() == "m"
    # A routine with no knobs still sends the key, so the contract has one shape.
    assert listed["greet"]["params"] == []


# ─── preview == run ───────────────────────────────────────────────────


def test_the_preview_and_the_run_are_the_same_steps(open_client):
    """**The one that matters.**

    A preview built by one code path and a run by another is a preview of
    something else — and it is a *promise*, so it is worse than no preview.
    Both go through `Routine.with_params`; this asserts they still do.
    """
    import asyncio

    from unitree_fleet_web.routines import routine_by_id

    params = {"leg_m": 0.8, "laps": 2}
    preview = open_client.post(
        "/api/fleet/routine/preview", json={"id": "square", "params": params}
    ).json()
    assert preview["total"] == 1 + 2 * 4 * 2

    built = routine_by_id("square").with_params(params)
    assert preview["total"] == len(built.steps)
    assert preview["seconds"] == pytest.approx(round(built.seconds, 1))
    assert [s["tool"] for s in preview["steps"]] == [s.tool for s in built.steps]
    assert [s["seconds"] for s in preview["steps"]] == [
        s.seconds for s in built.steps
    ]
    del asyncio


def test_the_preview_touches_no_robot(open_client):
    """It is called on every keystroke. If it moved anything that would be a
    console that walks the dogs while you type the distance."""
    open_client.post("/api/robot/dog1/connect")
    params = {"leg_m": 1.5}
    first = open_client.post(
        "/api/fleet/routine/preview", json={"id": "square", "params": params}
    ).json()
    second = open_client.post(
        "/api/fleet/routine/preview", json={"id": "square", "params": params}
    ).json()
    assert first == second
    # No routine started by previewing.
    assert open_client.get("/api/fleet/status").json()["routine"]["running"] is not True


def test_a_bad_parameter_is_a_typed_refusal_not_a_500(open_client):
    for body, why in (
        ({"id": "square", "params": {"leg_m": "far"}}, "a string"),
        ({"id": "square", "params": {"leg_m": 99}}, "out of range"),
        ({"id": "square", "params": {"nope": 1}}, "an unknown key"),
        ({"id": "nope", "params": {}}, "an unknown routine"),
    ):
        response = open_client.post("/api/fleet/routine/preview", json=body)
        assert response.status_code == 400, f"{why}: {response.text}"
        assert response.json()["code"] in ("bad_routine_params", "unknown_routine")


def test_starting_with_params_is_refused_the_same_way(open_client):
    """Preview and run validate through one path, so they must agree about what
    is acceptable — including when the preview is skipped entirely."""
    open_client.post("/api/robot/dog1/connect")
    response = open_client.post(
        "/api/fleet/routine", json={"id": "square", "params": {"leg_m": 99}}
    )
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "bad_routine_params"


# ─── the action groups ────────────────────────────────────────────────


def test_every_catalogued_action_is_offered_somewhere(open_client):
    """**Thirteen actions were reachable and never shown.**

    `fleet_actions` asked the robot for `include_dangerous=False`, so dances, the
    handstand, four step moves and four flips were filtered out of a console that
    would have run every one of them — `allow_dangerous` defaults to true. The
    capability existed for anyone willing to read `robots/go2.py`, and not for
    the operator.
    """
    from unitree_robot_agent.robots.go2 import _ACTIONS

    open_client.post("/api/robot/dog1/connect")
    spec = open_client.get("/api/fleet/actions").json()
    offered = {
        item["key"]
        for group in ("named", "hard")
        for item in spec[group]
    }
    catalogued = {name for name, _key, _dangerous, _desc in _ACTIONS}
    missing = sorted(catalogued - offered)
    assert not missing, f"these actions are reachable but not offered: {missing}"


def test_the_dangerous_actions_are_in_their_own_group(open_client):
    """They are in the payload, and the page draws them collapsed. Keeping them
    out of `named` is what makes that possible."""
    open_client.post("/api/robot/dog1/connect")
    spec = open_client.get("/api/fleet/actions").json()

    hard = {item["key"] for item in spec["hard"]}
    named = {item["key"] for item in spec["named"]}
    assert hard, "the hard group is empty"
    assert not (hard & named), "an action is in both groups"
    for flip in ("front_flip", "back_flip", "left_flip", "right_flip"):
        assert flip in hard, flip
    # The four safe dance-adjacent ones stay easy to reach.
    assert "hello" in named and "finger_heart" in named


def test_the_actions_that_need_space_are_marked(open_client):
    """A warning carried only by colour is invisible to a colour-blind operator,
    so the page draws a character — and this is where it learns which ones."""
    open_client.post("/api/robot/dog1/connect")
    spec = open_client.get("/api/fleet/actions").json()

    marked = {item["key"] for item in spec["hard"] if item.get("needs_space")}
    for dangerous_outdoors in ("front_flip", "back_flip", "front_jump"):
        assert dangerous_outdoors in marked, dangerous_outdoors
    # A dance does not need a runway.
    assert "dance_1" not in marked
    assert "handstand" not in marked


def test_the_action_order_does_not_change_between_loads(open_client):
    """A control surface whose buttons move is one you have to read every time
    instead of reaching for."""
    open_client.post("/api/robot/dog1/connect")
    first = open_client.get("/api/fleet/actions").json()
    second = open_client.get("/api/fleet/actions").json()
    for group in ("posture", "named", "hard"):
        # `posture` comes from a fixed table and carries `id`; the other two come
        # from the robot's catalogue and carry `key`. Both are stable orderings,
        # which is the property under test.
        field = "key" if first[group] and "key" in first[group][0] else "id"
        assert [i[field] for i in first[group]] == [
            i[field] for i in second[group]
        ], group
