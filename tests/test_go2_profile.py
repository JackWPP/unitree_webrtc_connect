"""Go2 profile: no FSM, curated sport actions, Go2-shaped stop plans."""

from __future__ import annotations

from unitree_robot_agent.robots import get_profile
from unitree_robot_agent.robots.base import (
    CAP_FSM_ACTIONS,
    CAP_LIDAR,
    CAP_MOTION_SWITCHER,
    CAP_VUI,
)


def profile():
    return get_profile("Go2")


def test_go2_has_no_state_machine():
    p = profile()
    assert p.states() == {}
    # Anything the profile cannot judge must be left to the robot.
    assert p.validate_transition(None, 1044).allowed is True
    assert p.classify(None)["state"] == "n/a"
    assert p.reported_state("rt/sportmodestate", {"mode": 3}) is None
    assert CAP_FSM_ACTIONS not in p.capabilities()


def test_capabilities_match_the_hardware():
    caps = profile().capabilities()
    for expected in (CAP_VUI, CAP_MOTION_SWITCHER, CAP_LIDAR):
        assert expected in caps


def test_sport_action_ids_match_the_constants():
    actions = profile().actions()
    assert actions["front_flip"].value == 1030
    assert actions["back_flip"].value == 1044
    assert actions["hello"].value == 1016
    assert actions["sit"].value == 1009
    # Commands with a dedicated tool must not be duplicated as actions.
    assert "damp" not in actions and "stop_move" not in actions
    assert "balance_stand" not in actions


def test_ambiguous_upstream_ids_are_not_exposed():
    # SPORT_CMD maps both LeadFollow and FreeWalk to 1045 upstream, so the
    # name cannot be resolved unambiguously — it must not be offered.
    actions = profile().actions()
    assert "lead_follow" not in actions
    assert "free_walk" not in actions


def test_alias_resolution():
    p = profile()
    assert p.resolve_action("flip").name == "front_flip"
    assert p.resolve_action("moonwalk").name == "moon_walk"
    assert p.resolve_action("wave").name == "hello"


def test_stop_plans_use_stopmove_and_damp():
    p = profile()
    assert p.soft_stop().api_id == 1003
    assert p.soft_stop().drops_robot is False
    assert p.emergency_stop().api_id == 1001
    assert p.emergency_stop().drops_robot is True
    assert p.stand_command()[1] == 1002
    assert p.lie_command()[1] == 1005
