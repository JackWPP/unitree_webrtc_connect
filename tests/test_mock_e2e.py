"""End-to-end through the real tool functions, against the mock backend.

This is the offline proxy for "does the MCP surface actually behave": the
CLI's ``selftest`` does the same thing, but here each behaviour is pinned
by an assertion so a regression fails loudly.
"""

from __future__ import annotations

from unitree_robot_agent.cli import _invoke
from unitree_robot_agent.runtime import create_runtime
from unitree_robot_agent.server import build_server

from .conftest import make_config


def bundle_for(robot: str, **overrides):
    return build_server(create_runtime(make_config(robot=robot, **overrides)))


async def test_r1_full_flow():
    bundle = bundle_for("R1")

    status = await _invoke(bundle, "robot_status", {})
    assert status["ok"] is True and status["robot"] == "R1"
    assert status["connection"]["connected"] is False  # lazy: nothing connected yet

    stand = await _invoke(bundle, "robot_stand", {"confirm": True})
    assert stand["ok"] is True

    # Air redirect: requesting Run lands in Loco20Dof and must read as confirmed.
    run = await _invoke(bundle, "robot_switch_state", {"state": "Run", "confirm": True})
    assert run["ok"] is True
    assert run["state"]["redirected"] is True
    assert run["state"]["confirmed"] is True
    assert run["state"]["reported"]["state"] == "Loco20Dof"

    posture = await _invoke(bundle, "robot_get_posture", {})
    assert posture["posture"]["state"] == "Loco20Dof"
    assert posture["posture"]["semantic"] == "locomotion"

    stopped = await _invoke(bundle, "robot_stop", {})
    assert stopped["stopped"] is True

    await bundle.context.runtime.aclose()


async def test_movement_needs_confirmation_and_is_clamped(monkeypatch=None):
    bundle = bundle_for("R1")

    refused = await _invoke(bundle, "robot_move", {"vx": 0.5})
    assert refused["ok"] is False
    assert refused["error"] == "confirm_required"
    assert "confirm=true" in refused["hint"]

    moved = await _invoke(bundle, "robot_move", {"vx": 0.5, "duration_s": 600, "confirm": True})
    assert moved["ok"] is True
    assert moved["duration_s"] <= 10.0
    assert any("clamped" in note for note in moved["notes"])

    await bundle.context.runtime.aclose()


async def test_move_falls_back_to_joystick_when_velocity_api_is_unsupported():
    """Hardware-verified: on an R1 Air, SET_VELOCITY(7105) answers 127 while
    the robot is in Loco20Dof (830) — no speed handler in that state. The
    move tool must fall back to rt/wirelesscontroller instead of failing."""
    bundle = bundle_for("R1")
    await _invoke(bundle, "robot_stand", {"confirm": True})                       # Lock
    await _invoke(bundle, "robot_switch_state", {"state": "Run", "confirm": True})  # -> 830 on Air

    result = await _invoke(bundle, "robot_move", {"vx": 0.1, "duration_s": 0.2, "confirm": True})
    assert result["ok"] is True
    assert result["transport"] == "joystick_fallback"
    assert result["code"] == 127
    assert any("127" in note for note in result["notes"])

    # And the velocity path must still be used where the handler exists.
    await _invoke(bundle, "robot_switch_state", {"state": "Damping"})
    direct = await _invoke(bundle, "robot_move", {"vx": 0.1, "duration_s": 0.2, "confirm": True})
    assert direct["ok"] is True
    assert "transport" not in direct
    await bundle.context.runtime.aclose()


async def test_local_whitelist_refuses_unreachable_motion():
    bundle = bundle_for("R1")
    await _invoke(bundle, "robot_stand", {"confirm": True})
    await _invoke(bundle, "robot_switch_state", {"state": "Run", "confirm": True})

    refused = await _invoke(bundle, "robot_switch_state", {"state": "Dance1", "confirm": True})
    assert refused["ok"] is False
    assert refused["error"] == "guard_rejected"
    assert "Lock" in refused["hint"]

    await bundle.context.runtime.aclose()


async def test_unknown_action_lists_the_alternatives():
    bundle = bundle_for("R1")
    result = await _invoke(bundle, "robot_play_action", {"name": "backflip", "confirm": True})
    assert result["ok"] is False
    assert result["error"] == "unknown_action"
    assert "dance_1" in result["hint"]
    await bundle.context.runtime.aclose()


async def test_read_only_safe_mode_blocks_control():
    bundle = bundle_for("R1", safe_mode=True)
    result = await _invoke(bundle, "robot_move", {"vx": 0.2, "confirm": True})
    assert result["ok"] is False
    assert result["error"] == "guard_rejected"
    # reads and the soft stop still work
    assert (await _invoke(bundle, "robot_status", {}))["ok"] is True
    await bundle.context.runtime.aclose()


async def test_dangerous_actions_can_be_disabled():
    bundle = bundle_for("R1", allow_dangerous=False)
    await _invoke(bundle, "robot_stand", {"confirm": True})
    result = await _invoke(bundle, "robot_play_action", {"name": "dance_1", "confirm": True})
    assert result["ok"] is False
    assert result["error"] == "guard_rejected"
    await bundle.context.runtime.aclose()


async def test_gestures_are_low_risk_but_audited():
    bundle = bundle_for("R1")
    result = await _invoke(bundle, "robot_arm_gesture", {"gesture": "handshake"})
    assert result["ok"] is True and result["action"] == "handshake"
    bad = await _invoke(bundle, "robot_arm_gesture", {"gesture": "dance_2"})
    assert bad["error"] == "unknown_gesture"
    await bundle.context.runtime.aclose()


async def test_go2_flow():
    bundle = bundle_for("Go2")
    state = await _invoke(bundle, "go2_get_state", {})
    assert state["ok"] is True

    avoid = await _invoke(bundle, "go2_obstacle_avoid", {"enable": True})
    assert avoid["enabled"] is True
    read_back = await _invoke(bundle, "go2_obstacle_avoid", {})
    assert read_back["enabled"] is True

    volume = await _invoke(bundle, "go2_set_volume", {"volume": 3})
    assert volume["volume"] == 3
    vui = await _invoke(bundle, "go2_get_vui", {})
    assert vui["volume"] == 3

    led = await _invoke(bundle, "go2_set_led", {"color": "purple", "seconds": 2})
    assert led["ok"] is True and led["color"] == "purple"
    bad = await _invoke(bundle, "go2_set_led", {"color": "chartreuse"})
    assert bad["error"] == "unknown_color"

    # Go2 must not be handed humanoid-only tools.
    assert "robot_switch_state" not in bundle.tool_names
    await bundle.context.runtime.aclose()


async def test_lowstate_reports_the_r1_motor_subset():
    bundle = bundle_for("R1")
    result = await _invoke(bundle, "robot_lowstate", {"include_motors": True})
    assert result["ok"] is True
    indices = [row["index"] for row in result["motors"]]
    assert indices == list(range(0, 12)) + list(range(15, 19)) + list(range(22, 26))
    assert result["battery"]["soc_percent"] == 87
    assert result["motors"][0]["temp_winding_c"] is not None
    await bundle.context.runtime.aclose()


async def test_snapshot_returns_image_content():
    bundle = bundle_for("R1")
    result = await _invoke(bundle, "robot_snapshot", {})
    assert type(result).__name__ == "Image"
    # A real JPEG, produced by the same encoder the robot path uses.
    assert result.data[:3] == b"\xff\xd8\xff"
    await bundle.context.runtime.aclose()
