"""Server assembly: which tools exist, and that secrets never leak."""

from __future__ import annotations

from unitree_robot_agent.core.audit import redact, summarize_args
from unitree_robot_agent.runtime import create_runtime
from unitree_robot_agent.server import build_server

from .conftest import make_config

R1_ONLY = {"robot_switch_state", "robot_arm_gesture"}
GO2_ONLY = {
    "go2_motion_mode",
    "go2_set_volume",
    "go2_set_brightness",
    "go2_set_led",
    "go2_get_vui",
    "go2_obstacle_avoid",
    "go2_get_state",
    "go2_lidar",
}
ALWAYS = {"robot_stop", "robot_status", "robot_move", "robot_doctor"}


def bundle_for(robot: str, **overrides):
    config = make_config(robot=robot, **overrides)
    return build_server(create_runtime(config))


def test_r1_exposes_humanoid_tools_and_not_go2_ones():
    names = set(bundle_for("R1").tool_names)
    assert R1_ONLY <= names
    assert names & GO2_ONLY == set()
    assert ALWAYS <= names


def test_go2_exposes_quadruped_tools_and_not_humanoid_ones():
    names = set(bundle_for("Go2").tool_names)
    assert GO2_ONLY <= names
    assert names & R1_ONLY == set()
    assert ALWAYS <= names


def test_safe_mode_keeps_read_tools_and_stop():
    names = set(bundle_for("R1", safe_mode=True).tool_names)
    assert {"robot_status", "robot_stop", "robot_doctor"} <= names


def test_every_tool_is_audited_and_redacted():
    assert redact({"aes_128_key": "deadbeef"})["aes_128_key"] == "***"
    assert redact({"password": "hunter2"})["password"] == "***"
    assert redact({"api_key_id": "x", "note": "ok"})["api_key_id"] == "***"
    nested = redact({"outer": {"token": "abc", "value": 1}})
    assert nested["outer"]["token"] == "***" and nested["outer"]["value"] == 1

    summary = summarize_args({"duration_s": 1.0, "aes_128_key": "x", "big": list(range(40))})
    assert summary["aes_128_key"] == "***"
    assert summary["big"] == "<40 items>"
    assert summary["duration_s"] == 1.0


def test_resources_and_prompts_are_registered():
    bundle = bundle_for("R1")
    assert bundle.api in ("mcp1", "mcp2")

    resources = getattr(bundle.server, "_resource_manager", None)
    assert resources is not None, "no resource manager on the server"
    uris = {str(item.uri) for item in resources.list_resources()}
    assert {"unitree://robot/status", "unitree://robot/actions", "unitree://robot/config"} <= uris

    prompts = getattr(bundle.server, "_prompt_manager", None)
    assert prompts is not None, "no prompt manager on the server"
    assert "unitree_safety_briefing" in {item.name for item in prompts.list_prompts()}


def test_tool_counts_are_stable():
    """Guards against accidentally dropping a tool during a refactor.

    These numbers are meant to be bumped *deliberately* when a tool is added;
    seeing them fail without having added one means something stopped being
    registered.

    Bumped 23/29 -> 27/33 for the four fleet tools (``robot_roster``,
    ``robot_fleet_status``, ``robot_stop_all``, ``robot_emergency_stop_all``),
    which register unconditionally so the model can always ask what it is
    driving. Bumped again to 28/35 for ``go2_recover`` (StandUp/RecoveryStand, which no
    tool could previously send at all). Bumped earlier to 28/34 for ``robot_preflight``, which answers "can
    I even reach these robots, and what will the handshake need" without
    connecting."""
    # 29/36 with `robot_walk`, which closes the loop on odometry instead of a
    # duration. `robot_move` stays, and the two answer different questions: a
    # timed nudge works on a robot that publishes no odometry, and a measured
    # walk does not accumulate the error of every step before it.
    assert len(bundle_for("R1").tool_names) == 29
    assert len(bundle_for("Go2").tool_names) == 36
