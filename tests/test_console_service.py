"""Console rules: the e-stop latch, input arbitration, and zero-on-switch.

These are the tests that decide whether the console is safe to point at two
real dogs. The behaviours pinned down here all come from a specific failure:

* an e-stop that a later joystick frame can undo is not an e-stop;
* a dog you switch away from keeps walking on its last frame unless it is
  explicitly zeroed (this is the bug `unitree_ui` guards against in
  `app.ts:1173-1177`);
* a fleet-wide latch must not be narrowed, or releasing one dog would
  un-safety the fleet.
"""

from __future__ import annotations

import asyncio
import contextlib
import time

import pytest

from unitree_robot_agent.fleet import FleetManager, Roster
from unitree_robot_agent.errors import UnknownRobot
from unitree_fleet_web.config import ConsoleConfig
from unitree_fleet_web.service import ConsoleService
from unitree_fleet_web.state import (
    ConsoleState,
    EStopScope,
    InputMode,
    MotionBlocked,
)

DOG1_SN = "B42D2000AAAA"
DOG2_SN = "B42D2000BBBB"


def make_fleet(tmp_path, **overrides) -> FleetManager:
    payload = {
        "state_dir": str(tmp_path / "state"),
        "default": "dog1",
        "robots": [
            {"id": "dog1", "model": "Go2", "ip": "192.168.31.245", "sn": DOG1_SN},
            {"id": "dog2", "model": "Go2", "ip": "192.168.31.246", "sn": DOG2_SN},
        ],
    }
    payload.update(overrides)
    return FleetManager(Roster.from_mapping(payload), backend="mock", audit_to_stderr=False)


@pytest.fixture
def service(tmp_path) -> ConsoleService:
    fleet = make_fleet(tmp_path)
    # Default focus is derived from the roster; make it explicit for tests.
    return ConsoleService(fleet, config=ConsoleConfig(deadman_s=0.2))


# ─── plain state: no I/O ──────────────────────────────────────────────


def test_estop_latches_and_blocks_the_robot_it_covers():
    state = ConsoleState()
    state.estop.engage(scope=EStopScope.ROBOT, robot_id="dog1", reason="test")

    with pytest.raises(MotionBlocked):
        state.require_motion_allowed("dog1")
    # the other dog is unaffected by a per-robot latch
    state.require_motion_allowed("dog2")


def test_a_fleet_latch_covers_every_robot():
    state = ConsoleState()
    state.estop.engage(scope=EStopScope.ALL, robot_id="", reason="test")
    for robot_id in ("dog1", "dog2", "dog3"):
        with pytest.raises(MotionBlocked):
            state.require_motion_allowed(robot_id)


def test_a_fleet_latch_cannot_be_narrowed_by_a_later_per_robot_press():
    state = ConsoleState()
    state.estop.engage(scope=EStopScope.ALL, robot_id="", reason="fleet")
    state.estop.engage(scope=EStopScope.ROBOT, robot_id="dog1", reason="again")
    assert state.estop.scope is EStopScope.ALL
    with pytest.raises(MotionBlocked):
        state.require_motion_allowed("dog2")


def test_a_per_robot_release_cannot_clear_a_fleet_latch():
    """Otherwise one click un-safeties the whole fleet."""
    state = ConsoleState()
    state.estop.engage(scope=EStopScope.ALL, robot_id="", reason="fleet")
    assert state.estop.release(scope=EStopScope.ROBOT, robot_id="dog1") is False
    assert state.estop.engaged is True


def test_a_fleet_release_clears_everything():
    state = ConsoleState()
    state.estop.engage(scope=EStopScope.ALL, robot_id="", reason="fleet")
    assert state.estop.release(scope=EStopScope.ALL, robot_id="") is True
    state.require_motion_allowed("dog1")


def test_begin_driving_reports_only_the_robots_that_need_zeroing():
    state = ConsoleState()
    assert state.begin_driving(["dog1"]) == set()          # nothing prior
    assert state.begin_driving(["dog1"]) == set()          # same target, no switch
    assert state.begin_driving(["dog2"]) == {"dog1"}       # switched -> zero dog1
    assert state.begin_driving(["dog1", "dog2"]) == set()  # added dog1 back
    assert state.begin_driving(["dog2"]) == {"dog1"}       # dropped dog1 again


def test_deadman_expires_once_frames_stop():
    state = ConsoleState()
    state.begin_driving(["dog1", "dog2"], now=100.0)
    assert state.deadman_expired(now=100.1, timeout_s=0.5) == set()
    assert state.deadman_expired(now=100.9, timeout_s=0.5) == {"dog1", "dog2"}


def test_deadman_is_quiet_when_nothing_is_driving():
    assert ConsoleState().deadman_expired(now=1000.0, timeout_s=0.1) == set()


@pytest.mark.parametrize(
    "mode,source,expected",
    [
        (InputMode.FOCUS, "keyboard", ["dog1"]),
        (InputMode.BOTH, "keyboard", ["dog1", "dog2"]),
        # Split works with no configuration: first group -> first robot.
        (InputMode.SPLIT, "wasd", ["dog1"]),
        (InputMode.SPLIT, "ijkl", ["dog2"]),
        (InputMode.SPLIT, "unbound", []),          # an unknown group drives nothing
        (InputMode.PER_SOURCE, "gamepad", ["dog2"]),
        (InputMode.PER_SOURCE, "unknown", []),
    ],
)
def test_input_arbitration(mode, source, expected):
    state = ConsoleState(
        input_mode=mode,
        focused="dog1",
        source_bindings={"gamepad": "dog2"},
    )
    assert state.targets_for(source=source, robot_ids=["dog1", "dog2"]) == expected


def test_an_explicit_split_binding_overrides_the_default():
    state = ConsoleState(
        input_mode=InputMode.SPLIT, source_bindings={"ijkl": "dog1", "wasd": "dog2"}
    )
    ids = ["dog1", "dog2"]
    assert state.targets_for(source="wasd", robot_ids=ids) == ["dog2"]
    assert state.targets_for(source="ijkl", robot_ids=ids) == ["dog1"]


def test_split_with_a_single_robot_leaves_the_second_group_dead():
    state = ConsoleState(input_mode=InputMode.SPLIT)
    assert state.targets_for(source="wasd", robot_ids=["dog1"]) == ["dog1"]
    assert state.targets_for(source="ijkl", robot_ids=["dog1"]) == []


def test_focus_mode_with_an_unknown_focus_drives_nothing():
    state = ConsoleState(input_mode=InputMode.FOCUS, focused="ghost")
    assert state.targets_for(source="keyboard", robot_ids=["dog1"]) == []


# ─── config safety ────────────────────────────────────────────────────


def test_binding_beyond_loopback_without_a_token_is_refused():
    with pytest.raises(ValueError, match="Refusing to bind 0.0.0.0"):
        ConsoleConfig(host="0.0.0.0", token="").check()


def test_a_short_token_is_refused():
    with pytest.raises(ValueError, match="at least 16 characters"):
        ConsoleConfig(host="0.0.0.0", token="short").check()


def test_loopback_without_a_token_is_allowed():
    ConsoleConfig(host="127.0.0.1", token="").check()


def test_token_comparison_is_exact():
    config = ConsoleConfig(token="a" * 32)
    assert config.authorises("a" * 32) is True
    assert config.authorises("a" * 31) is False
    assert config.authorises("") is False
    assert config.authorises(None) is False


# ─── service: the async half ──────────────────────────────────────────


async def test_teleop_is_refused_while_the_estop_is_latched(service):
    await service.estop(robot_id="", reason="test")
    with pytest.raises(MotionBlocked):
        await service.teleop("dog1", ly=1.0)


async def test_estop_reports_a_robot_it_could_not_reach(service):
    """An unreachable dog is not a damped dog."""
    result = await service.estop(reason="test")
    assert result["estop"]["engaged"] is True
    assert set(result["not_damped"]) == {"dog1", "dog2"}
    assert all(o["link"] == "down" for o in result["outcomes"])


async def test_estop_damps_a_connected_robot(service):
    await service.fleet.get("dog1").session.ensure_connected()
    result = await service.estop(robot_id="dog1", reason="test")

    outcome = result["outcomes"][0]
    assert outcome["robot_id"] == "dog1"
    assert outcome["link"] == "up"
    assert outcome["ok"] is True
    assert result["not_damped"] == []


async def test_releasing_the_estop_allows_motion_again(service):
    await service.estop(robot_id="", reason="test")
    with pytest.raises(MotionBlocked):
        await service.teleop("dog1", ly=1.0)

    released = service.release_estop()
    assert released["released"] is True

    result = await service.teleop("dog1", ly=1.0)
    assert result["robot_id"] == "dog1"


async def test_switching_targets_zeroes_the_previous_robot(service):
    await service.fleet.get("dog1").session.ensure_connected()
    await service.fleet.get("dog2").session.ensure_connected()

    first = await service.teleop("dog1", ly=1.0)
    assert first["zeroed"] == []

    second = await service.teleop("dog2", ly=1.0)
    assert second["zeroed"] == ["dog1"]
    assert sorted(service.state.driven_robots()) == ["dog2"]


async def test_both_mode_drives_two_and_the_switch_releases_both(service):
    await service.fleet.get("dog1").session.ensure_connected()
    await service.fleet.get("dog2").session.ensure_connected()

    await service.set_input_mode("both")
    both = await service.teleop_source(source="keyboard", ly=1.0)
    assert sorted(both["targets"]) == ["dog1", "dog2"]
    assert sorted(service.state.driven_robots()) == ["dog1", "dog2"]

    # Switching to focus must not leave dog2 walking. The release happens at
    # the mode change — earlier than the next frame — so the window between
    # "operator let go" and "robot zeroed" is as small as it can be.
    switched = await service.set_input_mode("focus")
    assert sorted(switched["zeroed"]) == ["dog1", "dog2"]
    assert service.state.driven_robots() == set()

    service.set_focus("dog1")
    focused = await service.teleop_source(source="keyboard", ly=1.0)
    assert focused["targets"] == ["dog1"]


async def test_split_mode_drives_both_dogs_at_once(service):
    """The point of split mode: one operator, two dogs, simultaneously.

    Regression: drive ownership used to be a single flat set, so the second
    key group's frame cancelled the first one's robot and the mode could
    never actually drive two dogs at the same time.
    """
    await service.fleet.get("dog1").session.ensure_connected()
    await service.fleet.get("dog2").session.ensure_connected()
    await service.set_input_mode("split")

    left = await service.teleop_source(source="wasd", ly=1.0)
    assert left["targets"] == ["dog1"]
    assert left["zeroed"] == []

    right = await service.teleop_source(source="ijkl", ly=1.0)
    assert right["targets"] == ["dog2"]
    assert right["zeroed"] == [], "the other hand's dog must not be zeroed"

    # Both dogs are under command at the same time.
    assert sorted(service.state.driven_robots()) == ["dog1", "dog2"]
    state = service.state.as_dict()
    assert state["driving_by_source"]["wasd"] == ["dog1"]
    assert state["driving_by_source"]["ijkl"] == ["dog2"]


async def test_releasing_one_source_leaves_the_other_driving(service):
    await service.set_input_mode("split")
    await service.teleop_source(source="wasd", ly=1.0)
    await service.teleop_source(source="ijkl", ly=1.0)

    released = await service.release_drive(source="wasd")
    assert released["zeroed"] == ["dog1"]
    assert sorted(service.state.driven_robots()) == ["dog2"]


async def test_changing_mode_releases_whatever_was_being_driven(service):
    """A mode change changes what the held keys mean, so release the sticks.

    Otherwise a dog stays under command until the dead-man notices, which is
    a window that does not need to exist.
    """
    await service.set_input_mode("both")
    await service.teleop_source(source="keyboard", ly=1.0)
    assert sorted(service.state.driven_robots()) == ["dog1", "dog2"]

    result = await service.set_input_mode("focus")
    assert sorted(result["zeroed"]) == ["dog1", "dog2"]
    assert service.state.driven_robots() == set()


async def test_the_deadman_only_stops_the_source_that_went_quiet(service):
    """One hand resting must not stop the other hand's dog."""
    await service.set_input_mode("split")
    await service.teleop_source(source="wasd", ly=1.0)
    await service.teleop_source(source="ijkl", ly=1.0)

    # IJKL keeps sending; WASD goes quiet.
    service.state.last_teleop_at["wasd"] = time.monotonic() - 10.0
    stale = service.state.deadman_expired(now=time.monotonic(), timeout_s=0.5)

    assert stale == {"dog1"}
    assert sorted(service.state.driven_robots()) == ["dog2"]


async def test_release_drive_zeroes_everyone_driving(service):
    await service.set_input_mode("both")
    await service.teleop_source(source="keyboard", ly=1.0)

    released = await service.release_drive()
    assert sorted(released["zeroed"]) == ["dog1", "dog2"]
    assert service.state.driven_robots() == set()


async def test_deadman_zeroes_a_driver_that_goes_quiet(service):
    """A closed tab must not leave a dog walking."""
    await service.fleet.get("dog1").session.ensure_connected()
    await service.teleop("dog1", ly=1.0)
    assert service.state.driven_robots() == {"dog1"}

    await service.start()
    try:
        for _ in range(40):
            await asyncio.sleep(0.05)
            if not service.state.driven_robots():
                break
    finally:
        service._deadman.cancel()  # noqa: SLF001 - deliberate white-box
        with contextlib.suppress(asyncio.CancelledError):
            await service._deadman

    assert service.state.driven_robots() == set(), "dead-man did not release the sticks"


async def test_teleop_clamps_hostile_axis_values(service):
    """Finite values clamp; non-finite ones go neutral, not full-reverse.

    ``-inf`` is a protocol error, and reading it as "full reverse" would be
    the least safe interpretation available. ``99.0`` is merely out of range
    and does clamp to full ahead.
    """
    result = await service.teleop("dog1", lx=99.0, ly=float("nan"), rx=float("-inf"))
    assert result["axes"] == {"lx": 1.0, "ly": 0.0, "rx": 0.0}


async def test_teleop_accepts_an_out_of_range_negative_axis(service):
    result = await service.teleop("dog1", lx=-42.0)
    assert result["axes"]["lx"] == -1.0


async def test_stop_all_names_the_robots_it_could_not_stop(service):
    result = await service.stop_all()
    assert result["complete"] is False
    assert set(result["not_stopped"]) == {"dog1", "dog2"}
    assert set(result["stopped"]) == set()


async def test_stop_all_clears_drive_ownership(service):
    await service.set_input_mode("both")
    await service.teleop_source(source="keyboard", ly=1.0)
    assert service.state.driven_robots()

    await service.stop_all()
    assert service.state.driven_robots() == set()


async def test_snapshot_never_leaks_keys_and_lists_robots(service):
    import json

    payload = json.dumps(service.snapshot())
    assert "aes_128_key" not in payload
    assert [r["robot_id"] for r in service.snapshot()["fleet"]["robots"]] == ["dog1", "dog2"]


async def test_unknown_robot_is_rejected_before_anything_is_sent(service):
    with pytest.raises(UnknownRobot):
        await service.teleop("dog9", ly=1.0)
    with pytest.raises(UnknownRobot):
        service.set_focus("dog9")
    with pytest.raises(UnknownRobot):
        service.set_video("dog9", False)


async def test_video_can_be_switched_off_per_robot(service):
    service.set_video("dog2", False)
    assert service.state.video_enabled["dog2"] is False
    # the frame path short circuits without touching a robot
    assert await service.frame("dog2") is None


async def test_aclose_never_leaves_a_robot_driving(service):
    await service.fleet.get("dog1").session.ensure_connected()
    await service.teleop("dog1", ly=1.0)
    await service.aclose()
    assert service.state.driven_robots() == set()


# ─── operator lease ───────────────────────────────────────────────────


def test_the_lease_is_only_armed_while_a_browser_is_watching(service):
    assert service.watching is False
    assert service.client_connected() == 1
    assert service.watching is True
    assert service.client_connected() == 2
    assert service.client_disconnected() == 1
    assert service.watching is True
    assert service.client_disconnected() == 0
    assert service.watching is False


def test_disconnecting_more_clients_than_connected_does_not_go_negative(service):
    service.client_disconnected()
    service.client_disconnected()
    assert service._clients == 0  # noqa: SLF001 - deliberate white-box


async def test_the_lease_keeps_a_watched_robot_from_being_released(service):
    """A console session must not drop every 45 s under the operator.

    The idle release exists so the owner can take over with the official app.
    That is right for an agent asking one question and wrong for a console with
    the page open, where the link dropping kills the video and makes the next
    command reconnect.
    """
    runtime = service.fleet.get("dog1")
    await runtime.session.ensure_connected()
    # Pretend the robot has been idle far longer than the release window.
    runtime.session._last_activity -= 600  # noqa: SLF001

    service.client_connected()
    service.config.lease_s = 0.05
    await service.start()
    try:
        for _ in range(20):
            await asyncio.sleep(0.05)
            if runtime.session.idle_seconds < 60:
                break
    finally:
        for task in (service._lease, service._deadman):  # noqa: SLF001
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    assert runtime.session.idle_seconds < 60, "lease did not re-assert the claim"
    assert runtime.session.is_connected is True


async def test_the_lease_never_connects_anything_by_itself(service):
    """Only release is prevented. A robot nobody asked for stays offline."""
    service.client_connected()
    service.config.lease_s = 0.05
    await service.start()
    try:
        await asyncio.sleep(0.15)
    finally:
        for task in (service._lease, service._deadman):  # noqa: SLF001
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    assert all(
        not service.fleet.get(rid).session.is_connected for rid in service.fleet.ids
    )


# ─── explicit connection control ──────────────────────────────────────


async def test_connect_brings_one_robot_up(service):
    result = await service.connect("dog2")
    assert result == {"robot_id": "dog2", "connected": True}
    assert service.fleet.get("dog2").session.is_connected is True
    assert service.fleet.get("dog1").session.is_connected is False


async def test_disconnect_releases_one_robot_only(service):
    await service.connect("dog1")
    await service.connect("dog2")

    result = await service.disconnect("dog1")

    assert result["connected"] is False
    assert service.fleet.get("dog1").session.is_connected is False
    assert service.fleet.get("dog2").session.is_connected is True


async def test_disconnect_clears_the_drive_claim(service):
    """Releasing the robot must also give up the sticks, or a later frame
    would be sent to a link we no longer hold."""
    await service.connect("dog1")
    await service.teleop("dog1", ly=1.0)
    assert service.state.driven_robots() == {"dog1"}

    await service.disconnect("dog1")
    assert service.state.driven_robots() == set()


async def test_connect_reports_why_it_failed_rather_than_raising(service):
    """A busy robot is a normal answer the console should display."""
    from unitree_robot_agent.errors import RobotBusy

    async def busy():
        raise RobotBusy("another client holds the slot")

    service.fleet.get("dog1").session.ensure_connected = busy  # type: ignore[assignment]
    result = await service.connect("dog1")

    assert result["connected"] is False
    assert result["error"] == "robot_busy"
    assert "another client holds the slot" in result["message"]
    assert result["hint"]


async def test_connect_rejects_an_unknown_robot(service):
    with pytest.raises(UnknownRobot):
        await service.connect("dog9")
