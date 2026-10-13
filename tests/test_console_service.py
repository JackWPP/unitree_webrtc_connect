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
    assert result["axes"] == {"lx": 1.0, "ly": 0.0, "rx": 0.0, "ry": 0.0}


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


# ─── the full wireless-controller frame ───────────────────────────────


async def test_teleop_carries_all_five_fields_the_robot_accepts(service):
    """A gamepad that only steers is a gamepad that half-works.

    The robot's `rt/wirelesscontroller` frame has lx/ly/rx/ry/keys. Dropping
    `ry` and `keys` — which the console used to do — silently disables the
    body-rotation axis and every button on a physical controller.
    """
    result = await service.teleop("dog1", lx=0.5, ly=0.0, rx=0.0, ry=-0.25, keys=0x0102)

    assert result["axes"] == {"lx": 0.5, "ly": 0.0, "rx": 0.0, "ry": -0.25}
    assert result["keys"] == 0x0102


async def test_the_button_mask_is_a_mask_not_an_axis(service):
    """Running it through float clamping would mangle it."""
    result = await service.teleop("dog1", keys=0xFFFF)
    assert result["keys"] == 0xFFFF

    # Out-of-range values are clamped to the field, not to [-1, 1].
    assert (await service.teleop("dog1", keys=999999))["keys"] == 0xFFFF
    assert (await service.teleop("dog1", keys=-5))["keys"] == 0
    assert (await service.teleop("dog1", keys="junk"))["keys"] == 0


async def test_a_fleet_frame_carries_the_mask_to_every_target(service):
    await service.set_input_mode("both")
    result = await service.teleop_source(source="gamepad:0", ly=0.4, keys=0x0004)

    assert sorted(result["targets"]) == ["dog1", "dog2"]
    assert result["keys"] == 0x0004
    assert len(result["results"]) == 2


# ─── driving a disconnected robot ─────────────────────────────────────


async def test_teleop_starts_connecting_a_robot_that_is_not_up_yet(service):
    """Grabbing the sticks for a disconnected dog must do something.

    Before this, every frame returned `delivered=false` and the only clue was
    a pill that said 离线 — the operator would conclude the console was broken.
    """
    assert service.fleet.get("dog1").session.is_connected is False

    await service.teleop("dog1", ly=1.0)
    # The handshake is deliberately not awaited inside the 20 Hz loop, so let
    # the background task run.
    for _ in range(40):
        await asyncio.sleep(0.02)
        if service.fleet.get("dog1").session.is_connected:
            break

    assert service.fleet.get("dog1").session.is_connected is True
    # A later frame is actually delivered.
    assert (await service.teleop("dog1", ly=1.0))["delivered"] is True


async def test_teleop_does_not_start_a_connect_storm(service):
    """A held key calls this 20 times a second."""
    spawned = []
    original = asyncio.create_task

    def counting(coro, **kwargs):
        spawned.append(coro)
        return original(coro, **kwargs)

    service.fleet.get("dog1").session.ensure_connected = _slow_connect()  # type: ignore[assignment]
    asyncio.create_task = counting  # type: ignore[assignment]
    try:
        for _ in range(10):
            await service.teleop("dog1", ly=1.0)
    finally:
        asyncio.create_task = original  # type: ignore[assignment]
        for coro in spawned:
            coro.close()

    assert len(spawned) == 1, f"expected one connect attempt, got {len(spawned)}"


async def test_status_reports_a_robot_whose_link_is_coming_up(service):
    service.fleet.get("dog1").session.ensure_connected = _slow_connect(0.5)  # type: ignore[assignment]
    await service.teleop("dog1", ly=1.0)

    assert "dog1" in service._connecting  # noqa: SLF001 - deliberate white-box
    payload = await service.status()
    assert payload["connecting"] == ["dog1"]


async def test_autoconnect_never_blocks_the_frame(service):
    """A handshake takes seconds; the teleop loop runs at 20 Hz."""
    service.fleet.get("dog1").session.ensure_connected = _slow_connect(5.0)  # type: ignore[assignment]

    started = time.monotonic()
    for _ in range(5):
        await service.teleop("dog1", ly=1.0)
    elapsed = time.monotonic() - started

    assert elapsed < 0.5, f"teleop waited {elapsed:.2f}s on a handshake"
    await service.aclose()


def _slow_connect(delay: float = 10.0):
    async def _connect():
        await asyncio.sleep(delay)
    return _connect


# ─── video and the WebRTC client slot ─────────────────────────────────


async def test_fetching_a_frame_brings_a_robot_up_by_default(service):
    """"Both dogs visible in the browser" means the video path may connect."""
    assert service.config.video_autoconnect is True
    assert service.fleet.get("dog1").session.is_connected is False

    await service.frame("dog1")

    assert service.fleet.get("dog1").session.is_connected is True


async def test_the_console_can_be_a_pure_observer(service):
    """With autoconnect off, watching must not claim the robot's only slot."""
    service.config.video_autoconnect = False

    assert await service.frame("dog1") is None
    assert service.fleet.get("dog1").session.is_connected is False

    # And an explicit connect still works — that is the whole point of the
    # mode: the operator decides when to take the link.
    await service.connect("dog1")
    assert service.fleet.get("dog1").session.is_connected is True


async def test_observer_mode_still_serves_frames_for_a_connected_robot(service):
    """The flag is about whether video may *connect*, not whether it works."""
    await service.connect("dog1")
    service.config.video_autoconnect = False

    frame = await service.frame("dog1")
    assert frame is not None and frame[:2] == b"\xff\xd8"  # JPEG SOI


# ─── telemetry ────────────────────────────────────────────────────────


async def test_telemetry_reports_battery_and_gait(service):
    """These two panels used to be dead: the UI read them off the *session*,
    which carries connection counters and no robot state at all."""
    await service.connect("dog1")

    snapshot = await service.refresh_telemetry("dog1")

    assert snapshot is not None
    assert snapshot["battery"]["soc_percent"] is not None
    assert snapshot["sport_state"]["gait_type"]
    assert snapshot["posture"]["connected"] is True


async def test_telemetry_reaches_the_page_through_body_not_raw(service):
    await service.connect("dog1")
    await service.refresh_telemetry("dog1")

    payload = await service.status()
    assert "telemetry" not in payload
    assert payload["body"]["dog1"]["battery"]["soc_percent"] is not None
    # dog2 was never connected, so it has nothing rather than zeros.
    assert payload["body"]["dog2"] == {}


async def test_telemetry_is_dropped_when_a_robot_goes_away(service):
    await service.connect("dog1")
    await service.refresh_telemetry("dog1")
    assert "dog1" in service._telemetry  # noqa: SLF001

    await service.disconnect("dog1")
    await service.refresh_telemetry("dog1")

    assert "dog1" not in service._telemetry  # noqa: SLF001


async def test_the_status_push_does_not_read_the_robot(service):
    """The push runs at 4 Hz; robot I/O belongs on the slower sampler. A
    regression here would put a topic read inside a UI loop."""
    await service.connect("dog1")

    reads = []
    original = service.refresh_telemetry

    async def counting(robot_id):
        reads.append(robot_id)
        return await original(robot_id)

    service.refresh_telemetry = counting  # type: ignore[assignment]
    for _ in range(5):
        await service.status()

    assert reads == [], "status() must not refresh telemetry itself"


async def test_the_sampler_only_runs_while_someone_is_watching(service):
    service.config.telemetry_hz = 20.0
    await service.connect("dog1")
    await service.start()
    try:
        await asyncio.sleep(0.2)
        assert service._telemetry == {}, "nobody is watching; nothing to read"  # noqa: SLF001

        service.client_connected()
        for _ in range(40):
            await asyncio.sleep(0.05)
            if service._telemetry:
                break
        assert "dog1" in service._telemetry  # noqa: SLF001
    finally:
        for task in (service._telemetry_task, service._deadman, service._lease):  # noqa: SLF001
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task


async def test_telemetry_is_off_when_the_rate_is_zero(service):
    service.config.telemetry_hz = 0
    service.client_connected()
    await service.start()
    try:
        await asyncio.sleep(0.15)
        assert service._telemetry_task is None  # noqa: SLF001
    finally:
        for task in (service._deadman, service._lease):  # noqa: SLF001
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task


async def test_an_http_status_request_also_counts_as_watching(service):
    """The socket is the fast path, but a console polling because its socket
    could not open is watching just as much. Counting only sockets meant that
    in exactly that case the operator got no telemetry *and* no lease."""
    assert service.watching is False

    await service.status()  # what the polling fallback and the socket both do

    assert service.watching is True


async def test_the_watch_window_eventually_expires(service):
    """Otherwise a single stray request would pin the robots forever."""
    import time as _time

    await service.status()
    assert service.watching is True

    service._last_watch_at -= 3600  # noqa: SLF001
    assert service.watching is False


# ─── video health ─────────────────────────────────────────────────────


async def test_a_stream_that_never_produces_a_frame_says_so(service):
    """The case a blank panel cannot express: connected, answering commands,
    and never a picture — no camera on the chassis, the video channel not
    opened, or the track ended. All three look identical from the outside."""
    await service.connect("dog1")

    # A real camera-less chassis returns None from `snapshot_frame` forever;
    # this drives the same bookkeeping without needing one.
    for _ in range(3):
        service._note_frame("dog1", ok=False, error="no video track")  # noqa: SLF001

    health = service.video_status("dog1")
    assert health["connected"] is True
    assert health["state"] == "stalled", health
    assert health["failures"] == 3
    assert health["last_error"] == "no video track"
    assert health["age_s"] is None, "no frame has ever arrived"


async def test_a_frame_that_arrives_clears_the_failure_state(service):
    await service.connect("dog1")
    service._note_frame("dog1", ok=False, error="teething")  # noqa: SLF001
    assert service.video_status("dog1")["state"] == "stalled"

    await service.frame("dog1")

    health = service.video_status("dog1")
    assert health["state"] == "ok"
    assert health["failures"] == 0
    assert health["last_error"] == ""


async def test_a_working_camera_reports_ok(service):
    await service.connect("dog1")
    jpeg = await service.frame("dog1")

    assert jpeg is not None
    health = service.video_status("dog1")
    assert health["state"] == "ok"
    assert health["age_s"] is not None and health["age_s"] < 1.0
    assert health["failures"] == 0


async def test_video_health_is_offline_when_the_robot_is(service):
    health = service.video_status("dog1")
    assert health["state"] == "offline"
    assert health["connected"] is False
    assert health["age_s"] is None


async def test_video_health_is_disabled_when_the_operator_turned_it_off(service):
    service.set_video("dog1", False)
    assert service.video_status("dog1")["state"] == "disabled"


async def test_a_stale_frame_is_reported_once_it_ages_out(service):
    await service.connect("dog1")
    await service.frame("dog1")
    assert service.video_status("dog1")["state"] == "ok"

    # Age the last frame past the stale window. Frames *used* to arrive and
    # stopped, which is "stalled" — not "still starting up".
    service._video["dog1"]["last_frame_at"] -= 3600  # noqa: SLF001
    health = service.video_status("dog1")

    assert health["state"] == "stalled"
    assert health["age_s"] > 3000


async def test_video_health_never_raises_for_an_unknown_robot(service):
    assert service.video_status("ghost")["state"] == "offline"


async def test_the_status_payload_reports_video_health_per_robot(service):
    payload = await service.status()
    assert set(payload["video"]) == set(service.fleet.ids)
    assert payload["video"]["dog1"]["state"] == "offline"


# ─── the velocity ramp ────────────────────────────────────────────────


async def test_a_full_stick_does_not_reach_full_speed_in_one_frame(service):
    """The lurch this exists to remove: previously the stick position was
    published verbatim, so full deflection meant full speed immediately."""
    service.config.motion_hz = 200.0
    await service.connect("dog1")
    await service.start()
    try:
        first = None
        for _ in range(3):
            result = await service.teleop("dog1", ly=1.0)
            first = first if first is not None else result
            await asyncio.sleep(0.02)

        # Asked for full...
        assert first["axes"]["ly"] == 1.0
        # ...and the commanded value is still climbing toward it.
        commanded = first["motion"]["commanded"][0]
        assert 0.0 <= commanded < 1.0, "it went straight to full speed"
    finally:
        for task in (service._motion_task, service._deadman, service._lease):  # noqa: SLF001
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task


async def test_releasing_the_stick_leaves_the_ramp_to_bring_it_down(service):
    """A soft release decelerates. Cutting is what the e-stop is for.

    Driven through `teleop` on purpose: that is what registers the robot as
    being driven, and `release_drive` resolves *who to release* from that
    registry. Setting the controller directly would test a path no operator
    can take.
    """
    service.client_connected()
    await service.connect("dog1")
    await service.teleop("dog1", ly=1.0)
    controller = service.motion("dog1")
    for _ in range(40):
        controller.tick(0.05)
    assert controller.moving is True

    await service.release_drive()

    assert controller.target == (0.0, 0.0, 0.0)
    assert controller.moving is True, "the release cut the velocity instead of ramping"


async def test_a_latched_stop_cuts_rather_than_ramping(service):
    """`robot_stop` and the e-stop are the paths where the operator wants it
    over now, so they bypass the ramp."""
    await service.connect("dog1")
    await service.teleop("dog1", ly=1.0)
    controller = service.motion("dog1")
    for _ in range(40):
        controller.tick(0.05)
    assert controller.moving is True

    await service.stop_all()

    assert controller.moving is False
    assert controller.commanded == (0.0, 0.0, 0.0)


async def test_the_estop_cuts_the_ramp_too(service):
    await service.connect("dog1")
    await service.teleop("dog1", ly=1.0)
    controller = service.motion("dog1")
    for _ in range(40):
        controller.tick(0.05)

    await service.estop(reason="test")

    assert controller.commanded == (0.0, 0.0, 0.0)
    assert controller.state == "estopped"
    service.release_estop()
    assert service.routine_state()["running"] is False


async def test_the_status_payload_reports_the_drive_state_per_robot(service):
    await service.connect("dog1")
    payload = await service.status()

    assert set(payload["motion"]) == set(service.fleet.ids)
    assert payload["motion"]["dog1"]["state"] == "idle"
    assert "commanded" in payload["motion"]["dog1"]


async def test_the_stick_mapping_matches_the_one_robot_joystick_documents(service):
    """`[ly, -lx, -rx] -> [vx, vy, vyaw]`, inverted. Getting it backwards would
    make the robot accelerate when the operator brakes, so it is pinned here."""
    service.config.motion_hz = 500.0
    await service.connect("dog1")

    frames = []
    original = service._frame  # noqa: SLF001

    async def capture(robot_id, *, lx, ly, rx, ry=0.0, keys=0):
        frames.append((lx, ly, rx))
        return True

    service._frame = capture  # type: ignore[assignment]
    try:
        # Push forward and turn left: ly = +1, rx = +1.
        await service.teleop("dog1", ly=1.0, rx=1.0)
        controller = service.motion("dog1")
        for _ in range(40):
            controller.tick(0.05)
        await service._publish_commanded("dog1", controller, *controller.commanded)  # noqa: SLF001
    finally:
        service._frame = original  # type: ignore[assignment]

    assert frames, "nothing was published"
    lx, ly, rx = frames[-1]
    # Forward is +ly, and a left turn is +rx.
    assert ly > 0, f"forward mapped to ly={ly}"
    assert rx > 0, f"left turn mapped to rx={rx}"


async def test_the_browser_path_goes_through_the_ramp_too(service):
    """`/api/fleet/teleop` is what the page actually calls, and it was the one
    caller still publishing stick positions verbatim after `teleop` was
    converted — so the whole ramp was bypassed by the only path that matters.

    Caught by driving it and watching `commanded` stay at zero. Nothing in the
    unit tests would have noticed, because they exercised `teleop`.
    """
    service.client_connected()
    await service.connect("dog1")
    service.set_focus("dog1")

    await service.teleop_source(source="keyboard", ly=1.0)
    controller = service.motion("dog1")

    assert controller.target[0] == pytest.approx(1.0), "the intent never arrived"
    assert controller.commanded[0] == 0.0, "it published the stick position directly"

    for _ in range(4):
        controller.tick(0.05)
    assert 0.0 < controller.commanded[0] < 1.0, "no ramp on the browser path"


async def test_the_browser_path_reports_the_ramp_state(service):
    service.client_connected()
    await service.connect("dog1")
    service.set_focus("dog1")

    result = await service.teleop_source(source="keyboard", ly=1.0)

    assert result["results"][0]["motion"]["state"] in ("driving", "idle")
    assert "commanded" in result["results"][0]["motion"]
