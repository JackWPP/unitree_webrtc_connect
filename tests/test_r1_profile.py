"""R1 profile: state table, normalization and the local white list."""

from __future__ import annotations

from unitree_robot_agent.robots import get_profile


def profile():
    return get_profile("R1")


def test_state_table_is_complete_and_numeric():
    states = profile().states()
    assert len(states) == 23
    assert all(isinstance(key, int) for key in states)
    assert states[811].name == "Run"
    assert states[830].name == "Loco20Dof"
    assert states[1].semantic == "damping"
    assert states[1].description.startswith("Soft stop")


def test_frozen_states_are_flagged():
    states = profile().states()
    for frozen in (5, 6, 7, 800, 813, 814):
        assert states[frozen].reports_state is False, frozen
    assert states[811].reports_state is True
    assert profile().classify(5)["reporting"] == "frozen"


def test_air_redirect_is_reported_as_success():
    result = profile().normalize(811, 830)
    assert result["redirected"] is True
    assert result["confirmed"] is True
    assert result["same_semantic_group"] is True
    assert "redirect" in result["note"].lower()


def test_missing_readback_is_not_a_failure():
    result = profile().normalize(811, None)
    assert result["confirmed"] is False
    assert result["reported"]["state"] == "unknown"
    assert "dispatched" in result["note"]


def test_unknown_id_is_reported_not_invented():
    info = profile().classify(4242)
    assert info["state"] == "unknown"
    assert "newer" in info["note"]


def test_motions_require_lock_but_damping_never_does():
    p = profile()
    assert p.validate_transition(4, 601).allowed is True
    refused = p.validate_transition(811, 601)
    assert refused.allowed is False
    assert "Lock" in refused.hint
    assert p.validate_transition(811, 1).allowed is True


def test_zero_torque_only_from_damping():
    p = profile()
    assert p.validate_transition(1, 0).allowed is True
    assert p.validate_transition(4, 0).allowed is False
    assert "Damping" in p.validate_transition(4, 0).reason


def test_nonexistent_state_is_refused_confidently():
    check = profile().validate_transition(4, 9999)
    assert check.allowed is False and check.confident is True


def test_actions_cover_fsm_and_gestures():
    p = profile()
    actions = p.actions()
    assert actions["run"].value == 811 and actions["run"].kind == "fsm"
    assert actions["stand_up"].value == 701
    assert actions["handshake"].kind == "arm"
    # G1-only gestures must not be offered on R1.
    assert "arm_heart" not in actions and "right_heart" not in actions


def test_aliases_resolve():
    p = profile()
    assert p.resolve_action("niuniuwu").name == "twist"
    assert p.resolve_action("gongfu").name == "kung_fu"
    assert p.resolve_action("qishen").name == "stand_up"
    assert p.resolve_action("highfive").name == "high_five"
    assert p.resolve_action("no-such-action") is None


def test_list_actions_can_hide_dangerous_ones():
    p = profile()
    all_rows = p.list_actions()
    safe_rows = p.list_actions(dangerous=False)
    assert len(safe_rows) < len(all_rows)
    assert all(row["dangerous"] is False for row in safe_rows)


def test_stop_plans_are_posture_aware():
    p = profile()
    soft = p.soft_stop()
    assert soft.api_id == 7105
    assert soft.parameter["velocity"] == [0.0, 0.0, 0.0]
    assert soft.drops_robot is False

    emergency = p.emergency_stop()
    assert emergency.api_id == 7101
    assert emergency.parameter == {"data": 1}
    assert emergency.drops_robot is True
