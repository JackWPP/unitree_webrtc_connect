"""The auto-stand gate: pressing W at a robot that is lying down.

The firmware refuses to walk a robot that is not standing — code 3201, "robot
is not standing; BalanceStand first" — and the operator sees nothing happen.
Every operator hits it the first time.

`unitree_harness` solves it with an `auto_stand` gate, but it has a DDS state
subscription to tell it the posture and this console does not: the Go2's state
topic is silent on at least one real firmware. It does not need one, because
**the console knows what it told the robot to do** — every posture change goes
through it. The belief is therefore learned, not observed, and the docstring
says so.
"""

from __future__ import annotations

import asyncio

import pytest

from unitree_fleet_web.posture import (
    DAMPING,
    LYING,
    SITTING,
    STANDING,
    UNKNOWN,
    PostureBelief,
)


# ─── the belief ───────────────────────────────────────────────────────


def test_a_fresh_robot_is_unknown_not_lying():
    """Guessing "lying" would make the first drive of every session send a
    stand the operator did not ask for."""
    belief = PostureBelief("dog1")
    assert belief.posture == UNKNOWN
    assert belief.walkable is False
    assert belief.known is False


def test_every_posture_tool_updates_it():
    belief = PostureBelief()
    belief.note("robot_lie_down")
    assert belief.posture == LYING and belief.walkable is False
    belief.note("robot_stand")
    assert belief.posture == STANDING and belief.walkable is True
    belief.note("go2_recover")
    assert belief.posture == STANDING
    belief.note("robot_emergency_stop")
    assert belief.posture == DAMPING and belief.walkable is False


def test_sit_and_rise_arrive_through_the_action_tool():
    """Posture is not only changed by posture tools: 坐下 and 从坐姿站起 are
    actions in the catalogue, and missing them would leave the belief wrong at
    exactly the moment the gate needs it."""
    belief = PostureBelief()
    belief.note("robot_play_action", {"name": "sit"})
    assert belief.posture == SITTING
    belief.note("robot_play_action", {"name": "rise_sit"})
    assert belief.posture == STANDING


def test_an_unrelated_action_does_not_change_the_belief():
    belief = PostureBelief()
    belief.note("robot_stand")
    belief.note("robot_play_action", {"name": "hello"})
    assert belief.posture == STANDING


def test_stopping_motion_is_not_a_posture_change():
    belief = PostureBelief()
    belief.note("robot_stand")
    belief.note("robot_stop")
    assert belief.posture == STANDING


def test_a_bounded_move_keeps_a_walking_robot_walking():
    belief = PostureBelief()
    belief.note("robot_stand")
    belief.note("robot_move")
    assert belief.posture == "moving"
    assert belief.walkable is True


def test_a_move_does_not_pretend_a_lying_robot_stood_up():
    belief = PostureBelief()
    belief.note("robot_lie_down")
    belief.note("robot_move")
    assert belief.posture == LYING


def test_a_fresh_link_forgets():
    belief = PostureBelief()
    belief.note("robot_stand")
    belief.forget()
    assert belief.posture == UNKNOWN


# ─── the gate ─────────────────────────────────────────────────────────


@pytest.fixture
def service(tmp_path):
    from unitree_robot_agent.fleet import FleetManager, Roster
    from unitree_fleet_web import ConsoleConfig, ConsoleService

    roster = Roster.from_mapping(
        {
            "state_dir": str(tmp_path / "state"),
            "default": "dog1",
            "robots": [
                {"id": "dog1", "model": "Go2", "ip": "192.168.31.245", "sn": "A"},
                {"id": "dog2", "model": "Go2", "ip": "192.168.31.246", "sn": "B"},
            ],
        }
    )
    return ConsoleService(
        FleetManager(roster, backend="mock", audit_to_stderr=False),
        config=ConsoleConfig(deadman_s=5.0, motion_hz=50.0, stand_timeout_s=5.0),
    )


async def test_the_gate_holds_a_drive_at_a_lying_robot(service):
    await service.connect("dog1")
    await service.start()
    try:
        await service.call_tool("dog1", "robot_lie_down", {"confirm": True})
        service.set_focus("dog1")

        for _ in range(6):
            await service.teleop_source(source="keyboard", ly=1.0)
            await asyncio.sleep(0.05)

        controller = service.motion("dog1")
        assert controller.state == "standing_wait"
        assert controller.commanded[0] == 0.0, "a velocity went out anyway"
    finally:
        for task in (service._motion_task, service._deadman, service._lease):  # noqa: SLF001
            if task is not None:
                task.cancel()


async def test_it_eventually_stands_and_drives(service):
    await service.connect("dog1")
    await service.start()
    try:
        await service.call_tool("dog1", "robot_lie_down", {"confirm": True})
        service.set_focus("dog1")

        for _ in range(60):
            await service.teleop_source(source="keyboard", ly=1.0)
            await asyncio.sleep(0.05)
            if service.motion("dog1").state == "driving":
                break

        assert service.posture("dog1").posture == STANDING
        assert service.motion("dog1").state == "driving"
        assert service.motion("dog1").commanded[0] > 0
    finally:
        for task in (service._motion_task, service._deadman, service._lease):  # noqa: SLF001
            if task is not None:
                task.cancel()


async def test_the_estop_latch_blocks_a_drive_before_the_gate_sees_it(service):
    """Which is why there is no "damped robot" branch in the gate.

    The only tool that damps is not on the console allowlist — the console has
    its own latching e-stop — and the latch refuses the drive while frames are
    being resolved. A `damping` branch would be unreachable code with a
    comforting comment on it.
    """
    from unitree_fleet_web.state import MotionBlocked

    await service.connect("dog1")
    await service.start()
    try:
        await service.estop(robot_id="dog1", reason="test")
        service.set_focus("dog1")

        with pytest.raises(MotionBlocked):
            await service.teleop_source(source="keyboard", ly=1.0)

        assert service.motion("dog1").state == "estopped"
    finally:
        for task in (service._motion_task, service._deadman, service._lease):  # noqa: SLF001
            if task is not None:
                task.cancel()


async def test_the_gate_does_not_stall_the_other_robot(service):
    """The regression this was caught by.

    The gate asks the robot to stand, and `robot_stand` waits a second for the
    firmware to acknowledge. Awaiting that inside the motion loop froze every
    robot's ramp for a measured 1.0 s — the same fault as a blocking WebRTC
    handshake on the shared event loop.
    """
    await service.connect("dog1")
    await service.connect("dog2")
    await service.start()
    try:
        # dog2 is stood up first so its posture is known: an `unknown` robot is
        # gated too, and the ~1 s that costs is the robot actually standing,
        # not a stall. The question here is whether dog1's gate costs dog2
        # anything once dog2 is free to move.
        await service.call_tool("dog2", "robot_stand", {"confirm": True})
        await service.call_tool("dog1", "robot_lie_down", {"confirm": True})
        await service.set_input_mode("both")

        # Sampled while it happens: dog1 gets auto-stood within about a second
        # and then drives, so a check afterwards would miss the window
        # entirely. What matters is that dog2's ramp kept moving *during* it.
        gated_while_ramping = 0.0
        for _ in range(24):
            await service.teleop_source(source="keyboard", ly=1.0)
            await asyncio.sleep(0.05)
            if service.motion("dog1").state == "standing_wait":
                gated_while_ramping = max(
                    gated_while_ramping, service.motion("dog2").commanded[0]
                )

        assert gated_while_ramping > 0, "dog1 was never gated, so nothing was measured"
        assert gated_while_ramping > 0.2, (
            f"dog2's ramp was stalled by dog1's auto-stand (reached only "
            f"{gated_while_ramping:.2f} while dog1 waited)"
        )
    finally:
        for task in (service._motion_task, service._deadman, service._lease):  # noqa: SLF001
            if task is not None:
                task.cancel()


async def test_the_gate_gives_up_and_says_why(service):
    """After the timeout the target is dropped and the state says so, rather
    than the console quietly asking a robot to stand forever."""
    await service.connect("dog1")
    service.config.stand_timeout_s = 0.2

    # The robot never gets up. Re-asserting the belief from the test raced the
    # background auto-stand: whichever landed last won, so the test passed
    # alone and failed under load. Making the ask a no-op tests the gate's
    # timeout instead of the scheduler's mood.
    async def never_stands(robot_id):
        return None

    service._ask_stand = never_stands  # type: ignore[assignment]
    await service.start()
    try:
        await service.call_tool("dog1", "robot_lie_down", {"confirm": True})
        service.set_focus("dog1")

        # The state alternates while the operator keeps holding the key: the
        # gate times out and drops the target, the next frame sets it again,
        # and a fresh gate starts. So the assertion is on what it *reports*,
        # not on which of the two states happens to be current when the loop
        # stops.
        seen = set()
        for _ in range(24):
            await service.teleop_source(source="keyboard", ly=1.0)
            await asyncio.sleep(0.05)
            seen.add(service.motion("dog1").state)

        assert "stand_timeout" in seen, f"the gate never gave up; saw {seen}"
        assert "driving" not in seen, (
            "a robot that never stood up should never have been driven"
        )
    finally:
        for task in (service._motion_task, service._deadman, service._lease):  # noqa: SLF001
            if task is not None:
                task.cancel()


async def test_a_refused_posture_command_does_not_change_the_belief(service):
    """A refused `robot_lie_down` did not lie the robot down. Believing it did
    would make the gate send a pointless stand on the next drive."""
    await service.connect("dog1")
    await service.call_tool("dog1", "robot_stand", {"confirm": True})
    assert service.posture("dog1").posture == STANDING

    # No confirm: the tool refuses.
    await service.call_tool("dog1", "robot_lie_down", {"confirm": False})

    assert service.posture("dog1").posture == STANDING


async def test_disconnecting_forgets_the_posture(service):
    await service.connect("dog1")
    await service.call_tool("dog1", "robot_stand", {"confirm": True})
    await service.disconnect("dog1")
    assert service.posture("dog1").posture == UNKNOWN


async def test_the_posture_belief_is_not_in_the_status_payload(service):
    """It used to be, four times a second, and **nothing read it** — the page's
    posture display comes from `body().fsm`, and the belief itself is internal.

    The payload budget test cannot catch that: it checks the total, and a key
    with no reader is a slice of it nobody would notice growing. The wiring
    audit found it by grepping the JavaScript for the name.
    """
    await service.connect("dog1")
    payload = await service.status()
    assert "posture" not in payload
    # ...and the belief is still maintained, because the standing gate needs it.
    assert service.posture("dog1").posture in (
        UNKNOWN, STANDING, LYING, SITTING, DAMPING, "moving"
    )


# ─── the live body ────────────────────────────────────────────────────


async def test_the_body_reports_attitude_feet_and_speed(service):
    """Everything here rides on the topics measured arriving on real hardware
    — lowstate, multiplestate and the state topics — so the panel is populated
    on the dog and not only under the mock."""
    await service.connect("dog1")
    await service.refresh_telemetry("dog1")

    body = service.body("dog1")

    assert "roll_deg" in body["attitude"] and "pitch_deg" in body["attitude"]
    assert len(body["feet"]) == 4
    assert all(isinstance(value, int) for value in body["feet"])
    assert "speed" in body
    assert body["gait"] == "trot"


async def test_attitude_carries_degrees_because_radians_are_not_readable(service):
    """The topic carries radians and a person reads degrees. Doing the
    conversion here rather than in the UI keeps the frontend free of unit
    knowledge, and means the tooltip and any future consumer agree."""
    await service.connect("dog1")
    await service.refresh_telemetry("dog1")

    attitude = service.body("dog1")["attitude"]
    assert attitude["roll_deg"] == pytest.approx(attitude["roll_rad"] * 57.2958, abs=0.1)


async def test_a_silent_robot_yields_an_empty_body_not_zeros(service):
    """"It is level" and "it has not told me" are different claims, and a zero
    makes the first out of the second."""
    await service.connect("dog1")
    service._telemetry["dog1"] = {}  # noqa: SLF001

    body = service.body("dog1")
    assert "attitude" not in body
    assert "feet" not in body
    assert "speed" not in body


async def test_a_malformed_payload_does_not_raise(service):
    await service.connect("dog1")
    for bad in (
        {"imu": {"roll_rad": "x"}},
        {"foot_force": [1, 2]},
        {"foot_force": ["a", "b", "c", "d"]},
        {"sport_state": {"velocity": [1]}},
        {"sport_state": {"velocity": "fast"}},
    ):
        service._telemetry["dog1"] = bad  # noqa: SLF001
        service.body("dog1")  # must not raise


async def test_the_status_payload_carries_the_body(service):
    await service.connect("dog1")
    payload = await service.status()
    assert set(payload["body"]) == set(service.fleet.ids)


# ─── what the robot is reporting ──────────────────────────────────────


async def test_an_unconnected_robot_reports_every_source_offline(service):
    for source in service.sources("dog1")["read"]:
        assert source["status"] == "offline"


async def test_a_connected_robot_reports_which_topics_answered(service):
    await service.connect("dog1")
    await service.refresh_telemetry("dog1")

    statuses = {s["id"]: s["status"] for s in service.sources("dog1")["read"]}
    assert statuses["lowstate"] == "live"
    assert statuses["multiplestate"] == "live"
    assert statuses["state"] == "live"


async def test_a_silent_topic_is_distinguished_from_an_unread_one(service):
    """The distinction that matters most.

    `rt/utlidar/robot_pose` is not read by anything here, so an empty map would
    otherwise look like "the robot cannot do this" when the truth is "nobody
    has checked". Conflating the two answers the operator's question wrongly.
    """
    await service.connect("dog1")
    service._telemetry["dog1"] = {}  # noqa: SLF001 - a silent link

    spec = service.sources("dog1")
    by_id = {s["id"]: s["status"] for s in spec["read"]}
    for topic_source in ("lowstate", "multiplestate", "state"):
        assert by_id[topic_source] == "silent", topic_source
    # Video is the exception on purpose: it has richer states of its own
    # (starting / stalled / disabled) and flattening them into "silent" would
    # lose the distinction the video panel exists to make.
    assert by_id["video"] in ("starting", "stalled", "silent")

    untried = {s["id"] for s in spec["not_attempted"]}
    assert untried == {"pose", "lidar", "map"}
    for source in spec["not_attempted"]:
        assert source["why"], "an unread source has to say why it is unread"
        # ...and it is a separate list, never a status.
        assert all(s["id"] != source["id"] for s in spec["read"])


async def test_the_status_payload_carries_the_sources(service):
    await service.connect("dog1")
    payload = await service.status()
    assert set(payload["sources"]) == set(service.fleet.ids)


# ─── how old the numbers are ──────────────────────────────────────────


async def test_telemetry_reports_its_own_age(service):
    """The panels show numbers sampled at four different rates — 50 Hz motion,
    10 Hz camera, 1 Hz telemetry, on-demand reads — and without an age the page
    cannot tell the operator which of them is the old one."""
    await service.connect("dog1")
    await service.refresh_telemetry("dog1")

    health = service.telemetry_health("dog1")
    assert health["age_s"] is not None
    assert health["age_s"] < 1.0
    assert health["stale"] is False
    assert health["refresh_hz"] == service.config.telemetry_hz


async def test_a_telemetry_snapshot_goes_stale_on_its_own(service):
    """1 Hz refresh, so past about 1.6 s the number is old enough that the page
    should stop presenting it as current. Asserted by moving the clock rather
    than by sleeping, because a test that sleeps for two seconds to check a
    two-second threshold is a test that gets slower every time somebody
    touches it."""
    await service.connect("dog1")
    await service.refresh_telemetry("dog1")

    service._telemetry_at["dog1"] -= 3.0  # noqa: SLF001
    health = service.telemetry_health("dog1")
    assert health["stale"] is True
    assert health["age_s"] >= 3.0

    # ...and the body panel, which is derived from the same snapshot, agrees.
    body = service.body("dog1")
    assert body["stale"] is True
    assert body["age_s"] >= 3.0


async def test_the_body_panel_carries_the_age_of_what_it_shows(service):
    """A speed readout is the specific trap: telemetry is 1 Hz and the arena
    map is 10 Hz, so the two are routinely a second apart, and a stale speed
    beside a fresh picture reads as "not moving" one second after it started."""
    await service.connect("dog1")
    await service.refresh_telemetry("dog1")

    body = service.body("dog1")
    assert "age_s" in body
    assert "stale" in body


async def test_disconnecting_forgets_the_time_as_well_as_the_data(service):
    """Otherwise the panels show a *stale* number rather than an absent one,
    which is worse: an absent number is obviously absent."""
    await service.connect("dog1")
    await service.refresh_telemetry("dog1")
    assert service.telemetry_health("dog1")["age_s"] is not None

    await service.disconnect("dog1")

    assert service.telemetry_health("dog1")["age_s"] is None
    assert service.telemetry_health("dog1")["stale"] is False
