"""Two dogs, one intent — and the ways they come apart.

`robot_turn(-90)` is closed loop: each robot turns until its own gyro says it
has turned ninety degrees. Two dogs on the same floor do not agree — different
friction, different IMU calibration, different payload — so they finish at
different moments and stop at different headings. Over four turns of 走正方形
the error accumulates and the pair is visibly out of formation. Nothing in the
console used to notice.

The honesty problem this file is built around: **each robot's odometry has its
own origin.** `position` is relative to wherever that dog's estimator started
and `yaw` to whatever heading it was facing. Comparing raw poses is meaningless.
What is meaningful is how much each has *changed* since a shared reference, and
the tests below pin exactly that distinction.
"""

from __future__ import annotations

import math

import pytest

from unitree_fleet_web.sync import (
    ALIGNED_DISTANCE_M,
    ALIGNED_YAW_DEG,
    DIVERGED_YAW_DEG,
    SyncMonitor,
    _wrap_degrees,
)

DEG = math.pi / 180.0


# ─── the angle arithmetic ─────────────────────────────────────────────


def test_the_shortest_way_round():
    """350° and -10° are 0° apart, not 360°. Getting this wrong turns a pair
    that is perfectly aligned into the worst divergence on the board."""
    assert _wrap_degrees(350.0) == pytest.approx(-10.0)
    assert _wrap_degrees(-370.0) == pytest.approx(-10.0)
    assert _wrap_degrees(0.0) == pytest.approx(0.0)
    assert abs(_wrap_degrees(350.0) - _wrap_degrees(-10.0)) == pytest.approx(0.0)


# ─── the reference ────────────────────────────────────────────────────


def test_an_unmarked_monitor_refuses_to_claim_alignment():
    """It has nothing to compare against, and "aligned" is a claim about the
    physical world."""
    monitor = SyncMonitor()
    summary = monitor.summary({"dog1": (0.0, 0.0, 0.0), "dog2": (0.0, 0.0, 0.0)})
    assert summary["state"] == "unmarked"
    assert summary["pairs"] == []


def test_two_dogs_at_their_own_origins_are_aligned_not_apart():
    """The distinction the whole module rests on.

    dog1's odometry sits at (0, 0) and dog2's at (3, 7) — because that is
    where each estimator happened to start, not because they are three metres
    apart. Compared naively they are 7.6 m apart; compared as *changes since
    the reference* they are exactly together, which is the truth.
    """
    monitor = SyncMonitor()
    monitor.mark({"dog1": (0.0, 0.0, 0.0), "dog2": (3.0, 7.0, 1.2)})

    summary = monitor.summary({"dog1": (0.0, 0.0, 0.0), "dog2": (3.0, 7.0, 1.2)})

    assert summary["state"] == "aligned"
    assert summary["worst"]["distance_spread_m"] == pytest.approx(0.0)
    assert summary["worst"]["yaw_spread_deg"] == pytest.approx(0.0)


def test_a_robot_that_joins_later_is_not_compared_to_a_point_it_missed():
    monitor = SyncMonitor()
    marked = monitor.mark({"dog1": (0.0, 0.0, 0.0), "dog2": (0.0, 0.0, 0.0)})
    assert marked["marked"] == ["dog1", "dog2"]

    # dog3 was not there when the reference was taken.
    summary = monitor.summary({
        "dog1": (0.0, 0.0, 0.0),
        "dog2": (0.0, 0.0, 0.0),
        "dog3": (9.0, 9.0, 0.0),
    })
    assert summary["members"] == ["dog1", "dog2"]
    assert {pair["a"] for pair in summary["pairs"]} | {
        pair["b"] for pair in summary["pairs"]
    } == {"dog1", "dog2"}


def test_a_marked_pair_with_no_poses_says_unknown_not_aligned():
    """Marked, but nobody is reporting a pose. That is "cannot tell", and a
    confident colour on a guess is worse than a question mark."""
    monitor = SyncMonitor()
    monitor.mark({"dog1": (0.0, 0.0, 0.0), "dog2": (0.0, 0.0, 0.0)})

    summary = monitor.summary({"dog1": None, "dog2": None})
    assert summary["state"] == "unknown"
    assert summary["marked"] is True


# ─── divergence ───────────────────────────────────────────────────────


def test_the_square_walk_turn_that_starts_it_all():
    """The concrete case: two dogs told to turn 90°, one getting 88° and the
    other 92°. Four degrees is inside the noise band, so it is `aligned` — and
    that is the right reading for one turn. It is four turns later that it
    stops being."""
    monitor = SyncMonitor()
    monitor.mark({"dog1": (0.0, 0.0, 0.0), "dog2": (0.0, 0.0, 0.0)})

    summary = monitor.summary({
        "dog1": (0.51, 0.0, 88 * DEG),
        "dog2": (0.49, 0.0, 92 * DEG),
    })

    assert summary["state"] == "aligned"
    assert summary["worst"]["yaw_spread_deg"] == pytest.approx(4.0, abs=0.1)


def test_four_turns_of_accumulated_error_is_diverged():
    """The same drift, after the square has been walked."""
    monitor = SyncMonitor()
    monitor.mark({"dog1": (0.0, 0.0, 0.0), "dog2": (0.0, 0.0, 0.0)})

    summary = monitor.summary({
        "dog1": (2.0, 0.0, 347 * DEG),
        "dog2": (2.05, 0.0, 375 * DEG),
    })

    assert summary["state"] == "diverged"
    assert summary["worst"]["yaw_spread_deg"] == pytest.approx(28.0, abs=0.5)


def test_drifting_is_the_band_between_aligned_and_diverged():
    monitor = SyncMonitor()
    monitor.mark({"dog1": (0.0, 0.0, 0.0), "dog2": (0.0, 0.0, 0.0)})

    summary = monitor.summary({
        "dog1": (0.0, 0.0, 0.0),
        "dog2": (0.0, 0.0, 12 * DEG),
    })
    assert summary["state"] == "drifting"
    assert ALIGNED_YAW_DEG <= summary["worst"]["yaw_spread_deg"] < DIVERGED_YAW_DEG


def test_distance_alone_can_divert_a_pair():
    """Two dogs that turned identically but walked different distances are
    still out of formation, and the yaw axis would never say so."""
    monitor = SyncMonitor()
    monitor.mark({"dog1": (0.0, 0.0, 0.0), "dog2": (0.0, 0.0, 0.0)})

    summary = monitor.summary({
        "dog1": (1.0, 0.0, 0.0),
        "dog2": (1.7, 0.0, 0.0),
    })

    assert summary["state"] == "diverged"
    assert summary["worst"]["distance_spread_m"] == pytest.approx(0.7, abs=0.01)


def test_the_leader_is_named_on_the_axis_that_is_actually_wrong():
    """A corrective action needs a target, so the pair has to say which dog is
    behind — and on *which* measure. A dog that turned further but walked less
    has no single answer."""
    monitor = SyncMonitor()
    monitor.mark({"dog1": (0.0, 0.0, 0.0), "dog2": (0.0, 0.0, 0.0)})

    # Turn spread (30°) is the bigger problem, so the leader is the one that
    # turned further, not the one that walked further.
    summary = monitor.summary({
        "dog1": (1.0, 0.0, 30 * DEG),
        "dog2": (0.6, 0.0, 0.0),
    })
    worst = summary["worst"]
    assert worst["leader"] == "dog1"
    assert worst["laggard"] == "dog2"


# ─── correction ───────────────────────────────────────────────────────


def test_an_aligned_pair_is_not_corrected():
    monitor = SyncMonitor()
    monitor.mark({"dog1": (0.0, 0.0, 0.0), "dog2": (0.0, 0.0, 0.0)})
    pair = monitor.pairs({"dog1": (0.4, 0.0, 89 * DEG), "dog2": (0.4, 0.0, 89 * DEG)})[0]
    assert SyncMonitor.correction(pair) is None


def test_the_laggard_is_nudged_towards_the_leader():
    monitor = SyncMonitor()
    monitor.mark({"dog1": (0.0, 0.0, 0.0), "dog2": (0.0, 0.0, 0.0)})
    pair = monitor.pairs({"dog1": (0.4, 0.0, 90 * DEG), "dog2": (0.4, 0.0, 72 * DEG)})[0]

    nudge = SyncMonitor.correction(pair)
    assert nudge is not None
    assert nudge["robot_id"] == "dog2", "the leader was nudged instead of the laggard"
    assert nudge["degrees"] > 0, "the laggard was turned away from the leader"
    assert 0 < nudge["degrees"] <= 12.0


def test_the_nudge_is_capped_and_never_a_loop():
    """Chasing a target this cannot verify it reached is how a correction
    becomes an oscillation, and two robots oscillating at each other is worse
    than two robots slightly out of step."""
    monitor = SyncMonitor()
    monitor.mark({"dog1": (0.0, 0.0, 0.0), "dog2": (0.0, 0.0, 0.0)})
    pair = monitor.pairs({"dog1": (0.4, 0.0, 120 * DEG), "dog2": (0.4, 0.0, 0.0)})[0]

    nudge = SyncMonitor.correction(pair, max_degrees=12.0)
    assert nudge["degrees"] == pytest.approx(12.0)
    assert abs(nudge["degrees"]) < pair.yaw_spread_deg


def test_a_negative_turn_is_corrected_the_negative_way():
    """Two dogs told to turn right both under-turn. The correction has to send
    the laggard right, not left — the sign is the whole difficulty."""
    monitor = SyncMonitor()
    monitor.mark({"dog1": (0.0, 0.0, 0.0), "dog2": (0.0, 0.0, 0.0)})
    pair = monitor.pairs({"dog1": (0.4, 0.0, -90 * DEG), "dog2": (0.4, 0.0, -70 * DEG)})[0]

    nudge = SyncMonitor.correction(pair)
    assert nudge["robot_id"] == "dog2"
    assert nudge["degrees"] < 0, "the laggard of a right turn was sent left"


# ─── through the service ──────────────────────────────────────────────


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
        config=ConsoleConfig(deadman_s=0.5),
    )


async def test_marking_needs_a_pose(service):
    """Nothing is marked automatically: a console that decided for itself when
    two dogs were aligned would be guessing about the physical world from two
    odometers that do not share an origin."""
    from unitree_robot_agent.errors import AgentError

    await service.connect("dog1")
    service._telemetry["dog1"] = {}  # noqa: SLF001

    with pytest.raises(AgentError) as caught:
        service.mark_sync()
    assert caught.value.code == "no_pose"


async def test_marking_reports_a_dog_it_could_not_cover(service):
    await service.connect("dog1")
    await service.connect("dog2")
    await service.refresh_telemetry("dog1")
    await service.refresh_telemetry("dog2")
    service._telemetry["dog2"] = {}  # noqa: SLF001 - silent

    result = service.mark_sync()
    assert result["marked"] == ["dog1"]
    assert result["skipped"] == ["dog2"]


async def test_the_status_payload_carries_the_sync_state(service):
    await service.connect("dog1")
    payload = await service.status()
    assert payload["sync"]["state"] == "unmarked"


async def test_the_service_reports_a_diverged_pair(service):
    await service.connect("dog1")
    await service.connect("dog2")
    await service.refresh_telemetry("dog1")
    await service.refresh_telemetry("dog2")
    service.mark_sync()

    service._telemetry["dog1"]["sport_state"]["position"] = [1.0, 0.0, 0.0]  # noqa: SLF001
    service._telemetry["dog2"]["sport_state"]["position"] = [1.0, 0.0, 0.5]  # noqa: SLF001

    state = service.sync_state()
    assert state["state"] == "diverged"
    assert state["worst"]["yaw_spread_deg"] == pytest.approx(28.6, abs=0.5)


# ─── the API surface ──────────────────────────────────────────────────


def _wait_for_poses(client, timeout_s: float = 8.0) -> bool:
    """Telemetry arrives on its own loop, so a pose takes a moment.

    Waiting is the real flow: the operator connects, sees the numbers fill in,
    and only then marks. Marking instantly is the case that legitimately
    refuses — there is nothing to mark yet.
    """
    import time

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        body = client.get("/api/fleet/status").json()["body"]
        if all(body.get(rid) for rid in ("dog1", "dog2")):
            return True
        time.sleep(0.1)
    return False


def test_marking_and_clearing_through_the_api(open_client):
    open_client.post("/api/robot/dog1/connect")
    open_client.post("/api/robot/dog2/connect")
    assert _wait_for_poses(open_client), "the robots never reported a pose"

    marked = open_client.post("/api/fleet/sync", json={})
    assert marked.status_code == 200
    assert set(marked.json()["marked"]) == {"dog1", "dog2"}

    status = open_client.get("/api/fleet/status").json()
    assert status["sync"]["marked"] is True
    assert status["sync"]["state"] in ("aligned", "unknown")

    cleared = open_client.request("DELETE", "/api/fleet/sync")
    assert cleared.status_code == 200
    assert cleared.json()["sync"]["state"] == "unmarked"


def test_marking_without_a_pose_is_a_typed_refusal(open_client):
    response = open_client.post("/api/fleet/sync", json={})
    assert response.status_code == 400
    assert response.json()["code"] == "no_pose"


def test_the_sync_row_is_in_the_page(open_client):
    html = open_client.get("/").text
    for marker in ("fleet-sync", "sync-mark", "sync-state", "sync-detail", "sync-clear"):
        assert marker in html, f"the sync row lost {marker}"


def test_the_console_js_renders_the_sync_state(open_client):
    source = open_client.get("/static/console.js").text
    for marker in ("renderSync", "SYNC_LABELS", "markSync", "refreshStatus"):
        assert marker in source, f"console.js lost {marker}"
    assert "pushOnce" not in source, "a function that does not exist is still called"


# ─── self-correction inside a routine ─────────────────────────────────


async def _marked_service(service, dog1_yaw_deg: float, dog2_yaw_deg: float):
    """Two connected dogs with a marked reference and a given heading each."""
    await service.connect("dog1")
    await service.connect("dog2")
    await service.refresh_telemetry("dog1")
    await service.refresh_telemetry("dog2")
    service.mark_sync()
    service._telemetry["dog1"]["sport_state"]["position"] = [  # noqa: SLF001
        0.0, 0.0, math.radians(dog1_yaw_deg)
    ]
    service._telemetry["dog2"]["sport_state"]["position"] = [  # noqa: SLF001
        0.0, 0.0, math.radians(dog2_yaw_deg)
    ]
    return service


def _turn_step():
    from unitree_fleet_web.routines import Step

    return Step(label="右转 90°", tool="robot_turn", args={"degrees": -90})


async def test_a_drifted_turn_is_corrected_once(service):
    """The concrete case the whole module exists for: two dogs told to turn,
    one getting 88° and the other 72°."""
    await _marked_service(service, 88.0, 72.0)
    targets = ["dog1", "dog2"]

    result = await service._correct_turn(  # noqa: SLF001
        _turn_step(), {"results": {t: {"ok": True} for t in targets}, "targets": targets}
    )

    assert result is not None
    applied = result["applied"]
    assert len(applied) == 1, "more than one nudge is a loop, not a correction"
    assert applied[0]["robot_id"] == "dog2"
    assert applied[0]["degrees"] == pytest.approx(12.0), "the cap did not hold"
    assert result["spread_before"] == pytest.approx(16.0, abs=0.5)


async def test_a_straight_step_is_not_corrected(service):
    """A distance error is as likely to be a wheel slipping as a dog lagging,
    and the only reference available is each dog's own odometry."""
    from unitree_fleet_web.routines import Step

    await _marked_service(service, 0.0, 0.0)
    service._telemetry["dog1"]["sport_state"]["position"] = [1.0, 0.0, 0.0]  # noqa: SLF001
    service._telemetry["dog2"]["sport_state"]["position"] = [0.4, 0.0, 0.0]  # noqa: SLF001
    targets = ["dog1", "dog2"]

    result = await service._correct_turn(  # noqa: SLF001
        Step(label="前进", tool="robot_move"),
        {"results": {t: {"ok": True} for t in targets}, "targets": targets},
    )
    assert result is None


async def test_a_turn_a_dog_refused_is_not_corrected(service):
    """A dog that refused the turn did not under-turn. Turning it again would
    be acting on a number that is not about what happened."""
    await _marked_service(service, 88.0, 72.0)
    targets = ["dog1", "dog2"]

    result = await service._correct_turn(  # noqa: SLF001
        _turn_step(),
        {"results": {"dog1": {"ok": True}, "dog2": {"ok": False}}, "targets": targets},
    )
    assert result is None


async def test_an_unmarked_pair_is_not_corrected(service):
    await service.connect("dog1")
    await service.connect("dog2")
    await service.refresh_telemetry("dog1")
    await service.refresh_telemetry("dog2")
    targets = ["dog1", "dog2"]

    result = await service._correct_turn(  # noqa: SLF001
        _turn_step(), {"results": {t: {"ok": True} for t in targets}, "targets": targets}
    )
    assert result is None


async def test_an_aligned_pair_is_not_corrected(service):
    await _marked_service(service, 90.0, 90.5)
    targets = ["dog1", "dog2"]

    result = await service._correct_turn(  # noqa: SLF001
        _turn_step(), {"results": {t: {"ok": True} for t in targets}, "targets": targets}
    )
    assert result is None


def test_the_timeline_shows_a_correction(open_client):
    """A step that nudged a dog back into formation says so. Silence would
    mean the console turned a robot without telling the operator."""
    source = open_client.get("/static/console.js").text
    assert "step-fix" in source
    assert "已纠正" in source
    assert "spread_before" in source
