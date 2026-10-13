"""Tests for the console's declarative control table.

Two kinds of assertion live here:

* **shape** — the published API (keys per ``kind``, unique ids, group order,
  the ``read``/``confirm`` invariant, filtering by ``available_tools``);
* **provenance** — every tool name, option value, argument name and range is
  cross-checked against the module that actually implements it (the robot
  profiles, the Go2 tool module, ``constants.R1_FSM``) and against the real
  tool registry built by ``unitree_robot_agent.server.build_server``. If a tool
  is renamed or a range changes, these fail here rather than in a browser.
"""

from __future__ import annotations

import ast
import copy
import json
from pathlib import Path
from typing import Dict, Iterable, List, Set

import pytest

from unitree_fleet_web import controls

from .conftest import make_config

#: Keys every control carries.
BASE_KEYS = {"group", "id", "label", "title", "kind", "tool", "args", "confirm", "read"}

#: Keys a control adds on top of the base set, per ``kind``.
EXTRA_KEYS = {
    "button": set(),
    "toggle": {"arg_name", "on_value", "off_value"},
    "select": {"arg_name", "options"},
    "range": {"arg_name", "min", "max", "step"},
}

NUDGE_IDS = {"nudge_forward", "nudge_back", "nudge_left", "nudge_right", "turn_left", "turn_right"}

GO2_IDS = {
    "stand",
    "lie_down",
    "recover",
    "play_action",
    "gait_move",
    "motion_mode",
    "get_state",
    "lowstate",
    "obstacle_avoid",
    "lidar",
    "led",
    "brightness",
    "volume",
    "get_vui",
} | NUDGE_IDS

R1_IDS = {"stand", "lie_down", "switch_state", "arm_gesture", "get_posture", "lowstate"} | NUDGE_IDS

#: Tools an R1 never registers.
R1_ONLY_TOOLS = {"robot_switch_state", "robot_arm_gesture"}


# ─── fixtures ──────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def registry() -> Dict[str, Set[str]]:
    """The names the real MCP server registers, per model.

    Capability gating happens at registration, so this is the authority on
    "can this robot do it at all" — the same source ``ConsoleService
    .tools_available`` intersects with the console allow-list.
    """
    from unitree_robot_agent.runtime import create_runtime
    from unitree_robot_agent.server import build_server

    return {
        robot: set(build_server(create_runtime(make_config(robot=robot))).tool_names)
        for robot in ("Go2", "R1")
    }


@pytest.fixture(scope="module")
def all_tools(registry) -> Set[str]:
    return registry["Go2"] | registry["R1"]


def _by_id(model: str, available: Iterable[str], control_id: str) -> dict:
    for control in controls.controls_for(model, available):
        if control["id"] == control_id:
            return control
    raise AssertionError(f"{model} exposes no control {control_id!r}")


def _groups_in_payload(model: str, available: Iterable[str]) -> List[str]:
    """Group order the service payload would carry (first-seen order)."""
    seen: List[str] = []
    for control in controls.controls_for(model, available):
        if control["group"] not in seen:
            seen.append(control["group"])
    return seen


# ─── shape ─────────────────────────────────────────────────────────────


def test_control_groups_are_stable():
    assert controls.CONTROL_GROUPS == (
        "posture",
        "gait",
        "motion",
        "safety_net",
        "lighting",
        "media",
        "motion_nudge",
    )
    assert all(isinstance(group, str) and group for group in controls.CONTROL_GROUPS)
    assert len(set(controls.CONTROL_GROUPS)) == len(controls.CONTROL_GROUPS)


def test_returned_rows_are_sorted_by_control_groups(all_tools):
    order = {group: index for index, group in enumerate(controls.CONTROL_GROUPS)}
    for model in ("Go2", "R1"):
        indexes = [order[c["group"]] for c in controls.controls_for(model, all_tools)]
        assert indexes == sorted(indexes), f"{model} rows are not in display order"


def test_payload_group_order_is_a_subsequence_of_control_groups(all_tools):
    for model in ("Go2", "R1"):
        groups = _groups_in_payload(model, all_tools)
        assert groups == [g for g in controls.CONTROL_GROUPS if g in groups]
        assert len(groups) == len(set(groups))


@pytest.mark.parametrize("model", ["Go2", "R1"])
def test_every_control_has_exactly_the_required_keys(model, all_tools):
    seen_kinds = set()
    for control in controls.controls_for(model, all_tools):
        kind = control["kind"]
        seen_kinds.add(kind)
        assert kind in EXTRA_KEYS, f"{control['id']}: unknown kind {kind!r}"
        assert set(control) == BASE_KEYS | EXTRA_KEYS[kind], control["id"]
        assert control["group"] in controls.CONTROL_GROUPS
        assert isinstance(control["args"], dict)
        assert isinstance(control["confirm"], bool) and isinstance(control["read"], bool)
        assert isinstance(control["label"], str) and control["label"]
        assert isinstance(control["title"], str) and control["title"]
        assert isinstance(control["tool"], str) and control["tool"]
        assert control["id"] == control["id"].lower()
        assert " " not in control["id"] and control["id"].replace("_", "").isalnum()
    # The panel is meant to be richer than one widget type per robot.
    assert {"button", "select"} <= seen_kinds


@pytest.mark.parametrize("model", ["Go2", "R1"])
def test_ids_are_unique(model, all_tools):
    rows = controls.controls_for(model, all_tools)
    ids = [c["id"] for c in rows]
    assert len(ids) == len(set(ids)), "duplicate control id"
    per_group: Dict[str, List[str]] = {}
    for control in rows:
        per_group.setdefault(control["group"], []).append(control["id"])
    for group, group_ids in per_group.items():
        assert len(group_ids) == len(set(group_ids)), group


@pytest.mark.parametrize("model", ["Go2", "R1"])
def test_read_controls_never_confirm(model, all_tools):
    reads = 0
    for control in controls.controls_for(model, all_tools):
        if control["read"]:
            reads += 1
            assert control["confirm"] is False, control["id"]
            assert "confirm" not in control["args"], control["id"]
        # `confirm` is a property of the control, not of the widget: a control
        # that says True must actually send it, and one that says False must
        # not send an argument the tool may not even accept.
        assert control["args"].get("confirm", False) is control["confirm"], control["id"]
    assert reads >= 1, f"{model} exposes no read control at all"


@pytest.mark.parametrize("model", ["Go2", "R1"])
def test_select_and_range_controls_are_self_describing(model, all_tools):
    for control in controls.controls_for(model, all_tools):
        if control["kind"] == "select":
            assert isinstance(control["arg_name"], str) and control["arg_name"]
            assert control["options"], control["id"]
            values = []
            for option in control["options"]:
                assert set(option) == {"value", "label"}, control["id"]
                assert isinstance(option["label"], str) and option["label"]
                json.dumps(option["value"])  # the UI puts this in <option value>
                values.append(json.dumps(option["value"]))
            assert len(values) == len(set(values)), f"{control['id']}: duplicate option"
        elif control["kind"] == "range":
            assert isinstance(control["arg_name"], str) and control["arg_name"]
            assert control["min"] < control["max"]
            assert control["step"] > 0
            assert isinstance(control["min"], (int, float))
            assert isinstance(control["max"], (int, float))
        elif control["kind"] == "toggle":
            assert isinstance(control["arg_name"], str) and control["arg_name"]
            assert control["on_value"] != control["off_value"]


# ─── filtering ─────────────────────────────────────────────────────────


def test_empty_available_tools_removes_everything():
    assert controls.controls_for("Go2", []) == []
    assert controls.controls_for("R1", []) == []


def test_filtering_by_available_tools_works(all_tools):
    every = controls.controls_for("Go2", all_tools)
    without_lidar = controls.controls_for("Go2", all_tools - {"go2_lidar"})

    assert any(c["tool"] == "go2_lidar" for c in every)
    assert not any(c["tool"] == "go2_lidar" for c in without_lidar)
    assert {c["id"] for c in without_lidar} == {c["id"] for c in every} - {"lidar"}

    # Nothing survives that the robot does not have, whatever the model.
    for model in ("Go2", "R1"):
        subset = {"robot_stand", "robot_move"}
        rows = controls.controls_for(model, subset)
        assert {c["tool"] for c in rows} <= subset
        # robot_move backs every straight-line nudge, so they all come back.
        assert {c["id"] for c in rows} == {"stand"} | {
            "nudge_forward",
            "nudge_back",
            "nudge_left",
            "nudge_right",
        }


def test_filtering_accepts_any_iterable(all_tools):
    as_list = controls.controls_for("Go2", sorted(all_tools))
    assert controls.controls_for("Go2", frozenset(all_tools)) == as_list
    assert controls.controls_for("Go2", set(all_tools)) == as_list
    assert controls.controls_for("Go2", (name for name in sorted(all_tools))) == as_list


def test_r1_registers_no_go2_tool_and_gets_no_go2_control(registry, all_tools):
    assert not any(name.startswith("go2_") for name in registry["R1"])
    for available in (all_tools, registry["R1"]):
        rows = controls.controls_for("R1", available)
        assert not any(c["tool"].startswith("go2_") for c in rows)
        assert not any(c["group"] in ("gait", "motion", "lighting") for c in rows)
        assert not any(
            c["id"] in {"motion_mode", "obstacle_avoid", "lidar", "led", "brightness", "volume",
                        "get_vui", "get_state", "gait_move"}
            for c in rows
        )


def test_go2_gets_no_humanoid_only_control(registry, all_tools):
    assert R1_ONLY_TOOLS <= registry["R1"]
    assert not (R1_ONLY_TOOLS & registry["Go2"])
    for available in (all_tools, registry["Go2"]):
        rows = controls.controls_for("Go2", available)
        assert not any(c["tool"] in R1_ONLY_TOOLS for c in rows)
        assert "arm_gesture" not in {c["id"] for c in rows}


def test_model_name_is_case_insensitive_and_unknown_falls_back_to_go2(all_tools):
    for spelling in ("Go2", "go2", "GO2", " go2 "):
        assert controls.controls_for(spelling, all_tools) == controls.controls_for("Go2", all_tools)
    for spelling in ("R1", "r1", " R1"):
        assert controls.controls_for(spelling, all_tools) == controls.controls_for("R1", all_tools)
    # An unknown model gets the Go2 set: the conservative side of the two.
    assert controls.controls_for("G1", all_tools) == controls.controls_for("Go2", all_tools)
    assert controls.controls_for("", all_tools) == controls.controls_for("Go2", all_tools)


def test_controls_for_is_pure(all_tools, registry):
    first = controls.controls_for("Go2", all_tools)
    second = controls.controls_for("Go2", all_tools)
    assert first == second
    assert first[0] is not second[0], "controls_for must not hand out shared dicts"

    # Mutating a result (which is what a caller serialising it might do) must
    # not leak into the next call.
    first[0]["args"]["vx"] = 99
    first[0]["options"] = []
    assert controls.controls_for("Go2", all_tools) == second

    assert controls.controls_for("Go2", registry["Go2"]) == copy.deepcopy(
        controls.controls_for("Go2", registry["Go2"])
    )


# ─── the inventory ─────────────────────────────────────────────────────


def test_go2_inventory(all_tools, registry):
    rows = controls.controls_for("Go2", all_tools)
    assert {c["id"] for c in rows} == GO2_IDS
    assert len(rows) == len(GO2_IDS) == 20
    # Every one of them is a tool a real Go2 registers, so the panel never
    # offers something the robot cannot honour.
    assert {c["tool"] for c in rows} <= registry["Go2"]
    assert {c["id"] for c in controls.controls_for("Go2", registry["Go2"])} == GO2_IDS


def test_r1_inventory(all_tools, registry):
    rows = controls.controls_for("R1", all_tools)
    assert {c["id"] for c in rows} == R1_IDS
    assert len(rows) == len(R1_IDS) == 12
    assert {c["tool"] for c in rows} <= registry["R1"]
    assert {c["id"] for c in controls.controls_for("R1", registry["R1"])} == R1_IDS


def test_every_referenced_tool_is_a_real_tool(registry, all_tools):
    for model in ("Go2", "R1"):
        rows = controls.controls_for(model, all_tools)
        referenced = {c["tool"] for c in rows}
        assert referenced <= all_tools, sorted(referenced - all_tools)
        # Each model only ever references tools that model actually registers,
        # so no control can be offered to a robot that cannot honour it.
        assert referenced <= registry[model], sorted(referenced - registry[model])


# ─── provenance: values come from the source, and drift fails here ─────


def test_motion_modes_match_the_profile(all_tools):
    from unitree_robot_agent.robots.go2 import MOTION_MODES

    control = _by_id("Go2", all_tools, "motion_mode")
    assert control["tool"] == "go2_motion_mode"
    assert control["arg_name"] == "mode"
    assert [o["value"] for o in control["options"]] == list(MOTION_MODES)
    assert control["confirm"] is True
    assert control["args"] == {"confirm": True}


def test_led_colours_match_the_tool(all_tools):
    from unitree_robot_agent.tools.go2 import LED_COLORS

    control = _by_id("Go2", all_tools, "led")
    assert control["tool"] == "go2_set_led"
    assert control["arg_name"] == "color"
    assert [o["value"] for o in control["options"]] == list(LED_COLORS)
    # go2_set_led(color, seconds=5, flash_cycle_ms=0): steady light, 5 s window,
    # and flash_cycle_ms left at 0 so the 499..seconds*1000 rule cannot trip.
    assert control["args"] == {"seconds": 5, "flash_cycle_ms": 0}
    assert control["confirm"] is False


def test_brightness_and_volume_ranges_match_the_tools(all_tools):
    brightness = _by_id("Go2", all_tools, "brightness")
    assert brightness["tool"] == "go2_set_brightness"
    assert (brightness["arg_name"], brightness["min"], brightness["max"], brightness["step"]) == (
        "level",
        0,
        10,
        1,
    )
    volume = _by_id("Go2", all_tools, "volume")
    assert volume["tool"] == "go2_set_volume"
    assert (volume["arg_name"], volume["min"], volume["max"], volume["step"]) == (
        "volume",
        0,
        10,
        1,
    )


def test_toggles_match_the_tool_signatures(all_tools):
    obstacle = _by_id("Go2", all_tools, "obstacle_avoid")
    assert obstacle["tool"] == "go2_obstacle_avoid"
    assert (obstacle["arg_name"], obstacle["on_value"], obstacle["off_value"]) == (
        "enable",
        True,
        False,
    )
    assert obstacle["args"] == {} and obstacle["confirm"] is False

    lidar = _by_id("Go2", all_tools, "lidar")
    assert lidar["tool"] == "go2_lidar"
    assert (lidar["arg_name"], lidar["on_value"], lidar["off_value"]) == ("enable", True, False)


def test_go2_recovery_modes_match_the_profile(all_tools):
    from unitree_robot_agent.robots.go2 import Go2Profile

    control = _by_id("Go2", all_tools, "recover")
    assert control["tool"] == "go2_recover"
    assert control["arg_name"] == "mode"
    assert control["confirm"] is True
    assert control["args"] == {"confirm": True}
    # `go2_recover` validates the argument against the profile's table.
    values = sorted(o["value"] for o in control["options"])
    assert values == sorted(Go2Profile.RECOVER_MODES)
    # And it must not be confused with the BalanceStand button next to it.
    assert _by_id("Go2", all_tools, "stand")["tool"] == "robot_stand"


def test_go2_action_options_are_real_catalog_actions(all_tools):
    from unitree_robot_agent.robots.go2 import Go2Profile

    profile = Go2Profile()
    catalog = profile.actions()
    for control_id in ("play_action", "gait_move"):
        control = _by_id("Go2", all_tools, control_id)
        assert control["tool"] == "robot_play_action"
        assert control["arg_name"] == "name"
        assert control["confirm"] is True
        values = [o["value"] for o in control["options"]]
        assert values, control_id
        assert set(values) <= set(catalog), sorted(set(values) - set(catalog))

    # The default action list is the profile's own "safe" set, so the panel
    # cannot offer a flip by accident.
    safe = {name for name, action in catalog.items() if not action.dangerous}
    assert {o["value"] for o in _by_id("Go2", all_tools, "play_action")["options"]} <= safe


def test_r1_state_options_come_from_the_fsm_table(all_tools):
    from unitree_webrtc_connect.constants import R1_FSM

    control = _by_id("R1", all_tools, "switch_state")
    assert control["tool"] == "robot_switch_state"
    assert control["arg_name"] == "state"
    assert control["confirm"] is True
    values = [o["value"] for o in control["options"]]
    assert set(values) <= set(R1_FSM), sorted(set(values) - set(R1_FSM))
    # The hub and the escape hatch an operator needs most must be there.
    assert {"Lock", "Damping"} <= set(values)


def test_r1_gesture_options_come_from_the_profile(all_tools):
    from unitree_robot_agent.robots.r1 import R1Profile

    arm = {name for name, action in R1Profile().actions().items() if action.kind == "arm"}
    control = _by_id("R1", all_tools, "arm_gesture")
    assert control["tool"] == "robot_arm_gesture"
    assert control["arg_name"] == "gesture"
    values = [o["value"] for o in control["options"]]
    assert set(values) <= arm, sorted(set(values) - arm)
    assert "release" in values, "the cancel gesture must be reachable"


def test_nudge_controls_use_the_real_parameter_names_and_safe_bounds(all_tools):
    for model in ("Go2", "R1"):
        rows = {c["id"]: c for c in controls.controls_for(model, all_tools)}
        assert NUDGE_IDS <= set(rows)
        for control in rows.values():
            if control["group"] != "motion_nudge":
                continue
            assert control["confirm"] is True, control["id"]
            assert control["read"] is False
            if control["tool"] == "robot_move":
                # robot_move(vx, vy, omega, duration_s, confirm)
                assert set(control["args"]) == {"vx", "vy", "omega", "duration_s", "confirm"}
                assert 0 < control["args"]["duration_s"] <= 1.0
                assert abs(control["args"]["vx"]) <= 0.5
                assert abs(control["args"]["vy"]) <= 0.5
                assert control["args"]["omega"] == 0.0
            elif control["tool"] == "robot_turn":
                # robot_turn(degrees, speed, confirm) — `degrees`, not `angle_deg`.
                assert "degrees" in control["args"]
                assert 0 < abs(control["args"]["degrees"]) <= 90
                assert "angle_deg" not in control["args"]
            else:  # pragma: no cover - a new nudge tool must be reviewed here
                raise AssertionError(f"unexpected nudge tool {control['tool']!r}")
        # A nudge is a nudge: both directions must exist for each axis.
        assert rows["turn_left"]["args"]["degrees"] == -rows["turn_right"]["args"]["degrees"]
        assert rows["nudge_forward"]["args"]["vx"] == -rows["nudge_back"]["args"]["vx"]


def test_read_controls_cover_the_state_a_panel_shows(all_tools):
    go2_reads = {c["id"] for c in controls.controls_for("Go2", all_tools) if c["read"]}
    assert go2_reads == {"lowstate", "get_state", "get_vui"}
    r1_reads = {c["id"] for c in controls.controls_for("R1", all_tools) if c["read"]}
    assert r1_reads == {"lowstate", "get_posture"}


def test_module_imports_nothing_heavy():
    """The panel must be buildable without the agent extra (MCP, aiortc, ...).

    Checked statically so it holds even though importing the package pulls in
    the Starlette app first.
    """
    source = Path(controls.__file__).read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "copy", "typing"}, sorted(imported)
