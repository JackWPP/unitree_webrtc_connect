"""`go2_recover`: the way back onto the feet, which did not exist before.

StandUp (1004) and RecoveryStand (1006) were in `SPORT_CMD` and handled by the
mock from the start, but `_DEDICATED` in the Go2 profile excludes them from the
action catalog — and the catalog is the only thing `robot_play_action` can
resolve names from. So a fallen dog had no route up through the MCP surface at
all. `robot_stand` is BalanceStand (1002), which only holds a robot that is
already upright.
"""

from __future__ import annotations

import pytest

from unitree_robot_agent.errors import AgentError
from unitree_robot_agent.robots.go2 import Go2Profile


@pytest.fixture
def profile():
    return Go2Profile()


# ─── the profile ──────────────────────────────────────────────────────


def test_both_recovery_modes_map_to_their_own_command(profile):
    assert profile.RECOVER_MODES == {"stand_up": 1004, "recovery_stand": 1006}
    assert profile.recover_command("stand_up")[1] == 1004
    assert profile.recover_command("recovery_stand")[1] == 1006


def test_recovery_is_not_the_same_command_as_standing(profile):
    """The distinction the whole tool exists for: BalanceStand holds an upright
    robot, it does not right a fallen one."""
    stand = profile.stand_command()[1]
    assert stand == 1002
    assert stand not in profile.RECOVER_MODES.values()


def test_recovery_commands_are_not_in_the_action_catalog(profile):
    """Which is exactly why a dedicated tool was needed — if these were
    reachable through robot_play_action there would be nothing to add."""
    catalogued = {info.value for info in profile.actions().values()}
    assert catalogued, "the catalog should not be empty"
    assert not (set(profile.RECOVER_MODES.values()) & catalogued)


def test_an_unknown_mode_raises_rather_than_guessing(profile):
    """A guessed api_id sent to a real robot is the failure mode to avoid."""
    with pytest.raises(KeyError):
        profile.recover_command("roll_over")


def test_the_recovery_topic_is_the_sport_api(profile):
    assert profile.recover_command("stand_up")[0] == "rt/api/sport/request"


# ─── the tool ─────────────────────────────────────────────────────────


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    monkeypatch.setenv("UNITREE_AGENT_BACKEND", "mock")
    from unitree_robot_agent.config import AgentConfig
    from unitree_robot_agent.runtime import create_runtime
    from unitree_robot_agent.server import build_server

    config = AgentConfig(
        robot="Go2", backend="mock", state_dir=tmp_path / "state", audit_enabled=False
    )
    return build_server(create_runtime(config), fleet_from_env=False)


def test_the_tool_is_registered_for_a_go2(bundle):
    assert "go2_recover" in bundle.tool_names


def test_the_tool_is_not_registered_for_an_r1(tmp_path, monkeypatch):
    monkeypatch.setenv("UNITREE_AGENT_BACKEND", "mock")
    from unitree_robot_agent.config import AgentConfig
    from unitree_robot_agent.runtime import create_runtime
    from unitree_robot_agent.server import build_server

    config = AgentConfig(
        robot="R1", backend="mock", state_dir=tmp_path / "state", audit_enabled=False
    )
    assert "go2_recover" not in build_server(
        create_runtime(config), fleet_from_env=False
    ).tool_names


async def test_it_refuses_without_confirmation(bundle, invoke):
    result = await invoke(bundle, "go2_recover", {"mode": "stand_up"})
    assert result["ok"] is False
    assert result["error"] == "confirm_required"


async def test_it_sends_stand_up(bundle, invoke):
    result = await invoke(bundle, "go2_recover", {"mode": "stand_up", "confirm": True})
    assert result["ok"] is True
    assert result["action"] == "recover"
    assert result["mode"] == "stand_up"
    assert result["code"] == 0


async def test_it_sends_recovery_stand(bundle, invoke):
    """The one that matters after a fall."""
    result = await invoke(
        bundle, "go2_recover", {"mode": "recovery_stand", "confirm": True}
    )
    assert result["ok"] is True
    assert result["mode"] == "recovery_stand"


async def test_an_unknown_mode_is_refused_not_sent(bundle, invoke):
    result = await invoke(bundle, "go2_recover", {"mode": "backflip", "confirm": True})
    assert result["ok"] is False
    assert result["error"] == "unknown_mode"
    assert "stand_up" in result["hint"] and "recovery_stand" in result["hint"]


async def test_safe_mode_refuses_it(bundle, invoke, monkeypatch):
    """It moves the robot, so safe mode has to stop it like any other motion."""
    bundle.context.config.safe_mode = True
    result = await invoke(bundle, "go2_recover", {"mode": "stand_up", "confirm": True})
    assert result["ok"] is False
    assert result["error"] in ("guard_rejected", "safe_mode")
