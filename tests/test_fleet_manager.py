"""FleetManager: N runtimes in one process, and honest fleet-wide reporting.

Two things are being pinned down here. First, that two robots stay fully
separated — own config, own state directory, own audit file, own key cache.
Second, that a fleet-level stop never rounds "dog 2 did not stop" up to
success.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from unitree_robot_agent.core.audit import AuditLog
from unitree_robot_agent.errors import FleetConfigError, UnknownRobot
from unitree_robot_agent.fleet import FleetManager, Roster

KEY1 = "0123456789abcdef0123456789abcdef"
KEY2 = "fedcba9876543210fedcba9876543210"
DOG1_SN = "B42D2000AAAA"
DOG2_SN = "B42D2000BBBB"


def two_dogs(state_dir: Path, **overrides) -> Roster:
    payload = {
        "state_dir": str(state_dir),
        "default": "dog1",
        "robots": [
            {
                "id": "dog1",
                "model": "Go2",
                "connection": "sta-l",
                "ip": "192.168.31.245",
                "sn": DOG1_SN,
                "aes_128_key": KEY1,
                "role": "lead",
            },
            {
                "id": "dog2",
                "model": "Go2",
                "connection": "sta-l",
                "ip": "192.168.31.246",
                "sn": DOG2_SN,
                "aes_128_key": KEY2,
                "role": "wing",
            },
        ],
    }
    payload.update(overrides)
    return Roster.from_mapping(payload)


@pytest.fixture
def fleet(tmp_path) -> FleetManager:
    """A two-dog fleet on mock backends — no robot, no network."""
    return FleetManager(
        two_dogs(tmp_path / "state"),
        backend="mock",
        audit_to_stderr=False,
    )


# ─── construction ─────────────────────────────────────────────────────


def test_each_robot_gets_its_own_runtime_and_state_directory(fleet):
    assert fleet.ids == ["dog1", "dog2"]
    assert fleet.get("dog1") is not fleet.get("dog2")

    d1 = fleet.get("dog1").config.state_dir
    d2 = fleet.get("dog2").config.state_dir
    assert d1 != d2
    assert d1.name == "dog1" and d2.name == "dog2"


def test_identity_is_carried_onto_every_runtime(fleet):
    cfg = fleet.get("dog2").config
    assert cfg.robot_id == "dog2"
    assert cfg.ip == "192.168.31.246"
    assert cfg.serial_number == DOG2_SN
    assert cfg.aes_128_key == KEY2


def test_audit_paths_are_per_robot_and_never_inherited(fleet):
    a1 = fleet.get("dog1").config.audit_log
    a2 = fleet.get("dog2").config.audit_log
    assert a1 != a2
    assert a1.parent == fleet.get("dog1").config.state_dir
    assert a2.parent == fleet.get("dog2").config.state_dir


def test_key_stores_are_per_robot(fleet):
    s1, s2 = fleet.key_store("dog1"), fleet.key_store("dog2")
    assert s1.path != s2.path
    assert s1.path.parent == fleet.get("dog1").config.state_dir
    assert s2.path.parent == fleet.get("dog2").config.state_dir

    # A key cached for one robot is invisible to the other. (The roster's own
    # keys never reach this cache — they come straight from the spec — so the
    # isolation is what matters here.)
    s1.put(fleet.spec("dog1"), KEY1)
    assert s1.get(fleet.spec("dog1")) == KEY1
    assert s2.get(fleet.spec("dog2")) is None


def test_roster_options_reach_the_config(tmp_path):
    roster = two_dogs(
        tmp_path / "state",
        defaults={"idle_disconnect_s": 45.0},
    )
    fleet = FleetManager(roster, backend="mock")
    assert fleet.get("dog1").config.idle_disconnect_s == 45.0


def test_an_unknown_roster_option_is_rejected_with_the_robot_named(tmp_path):
    roster = Roster.from_mapping(
        {
            "state_dir": str(tmp_path / "state"),
            "robots": [
                {"id": "dog1", "ip": "192.168.31.245", "options": {"not_a_field": 1}}
            ],
        }
    )
    with pytest.raises(FleetConfigError, match="dog1.*not_a_field"):
        FleetManager(roster, backend="mock")


def test_resolve_uses_the_roster_default(fleet):
    assert fleet.resolve("").config.robot_id == "dog1"
    assert fleet.resolve("dog2").config.robot_id == "dog2"


def test_resolve_refuses_to_guess_between_several_robots(fleet):
    fleet.roster = Roster.from_mapping(
        {
            "state_dir": str(fleet.roster.state_dir),
            "default": "",
            "robots": [
                {"id": "dog1", "ip": "192.168.31.245"},
                {"id": "dog2", "ip": "192.168.31.246"},
            ],
        }
    )
    with pytest.raises(UnknownRobot, match="robot_id` is required"):
        fleet.resolve("")


def test_get_rejects_an_unknown_robot(fleet):
    with pytest.raises(UnknownRobot):
        fleet.get("dog9")


def test_redacted_view_never_carries_key_material(fleet):
    payload = json.dumps(fleet.redacted())
    assert KEY1 not in payload and KEY2 not in payload
    assert '"has_key": true' in payload


# ─── audit isolation: the verified cross-contamination bug ────────────


def test_two_audit_logs_do_not_cross_write(tmp_path):
    """Regression: two instances used to share one logger and thus N handlers.

    Every record then landed in *both* files, so dog 1's movements appeared
    in dog 2's trail.
    """
    p1 = tmp_path / "dog1" / "audit.log"
    p2 = tmp_path / "dog2" / "audit.log"
    a1 = AuditLog(path=p1, robot_id="dog1")
    a2 = AuditLog(path=p2, robot_id="dog2")

    a1.record("robot_move", robot="Go2", ok=True, detail={"say": "from dog1"})
    a2.record("robot_stop", robot="Go2", ok=True, detail={"say": "from dog2"})

    for handler in (a1, a2):
        for h in handler._logger.handlers:  # noqa: SLF001 - deliberate white-box
            h.flush()

    lines1 = [json.loads(x) for x in p1.read_text().splitlines() if x.strip()]
    lines2 = [json.loads(x) for x in p2.read_text().splitlines() if x.strip()]

    assert len(lines1) == 1, f"dog1 trail has {len(lines1)} records: {lines1}"
    assert len(lines2) == 1, f"dog2 trail has {len(lines2)} records: {lines2}"
    assert lines1[0]["detail"]["say"] == "from dog1"
    assert lines2[0]["detail"]["say"] == "from dog2"


def test_every_record_carries_the_robot_id(tmp_path):
    log = AuditLog(path=tmp_path / "audit.log", robot_id="dog2")
    entry = log.record("robot_move", robot="Go2", ok=True)
    assert entry["robot_id"] == "dog2"
    assert entry["robot"] == "Go2"  # family stays as capability context


def test_a_second_audit_log_does_not_duplicate_into_the_first(tmp_path):
    """Three instances, the way a fleet is built."""
    logs = [AuditLog(path=tmp_path / f"dog{i}" / "audit.log", robot_id=f"dog{i}") for i in (1, 2, 3)]
    for i, log in enumerate(logs, start=1):
        log.record("robot_status", robot="Go2", detail={"n": i})

    for i, log in enumerate(logs, start=1):
        for h in log._logger.handlers:  # noqa: SLF001
            h.flush()
        lines = [json.loads(x) for x in (tmp_path / f"dog{i}" / "audit.log").read_text().splitlines() if x.strip()]
        assert [rec["detail"]["n"] for rec in lines] == [i]


# ─── fleet operations ─────────────────────────────────────────────────


async def test_stop_all_reports_a_disconnected_robot_honestly(fleet):
    """A robot we cannot reach must never be counted as stopped."""
    outcomes = await fleet.stop_all()
    by_id = {o.robot_id: o for o in outcomes}

    assert set(by_id) == {"dog1", "dog2"}
    for outcome in by_id.values():
        assert outcome.delivered is False
        assert outcome.link == "down"
        assert outcome.ok is False


async def test_stop_all_reports_a_connected_robot_as_delivered(fleet):
    fleet.get("dog1").session  # touch to make the intent explicit
    await fleet.get("dog1").session.ensure_connected()

    outcomes = await fleet.stop_all()
    by_id = {o.robot_id: o for o in outcomes}

    assert by_id["dog1"].link == "up"
    assert by_id["dog1"].delivered is True
    # dog2 was never connected, and still is not
    assert by_id["dog2"].link == "down"
    assert by_id["dog2"].delivered is False


async def test_status_all_returns_one_record_per_robot(fleet):
    records = await fleet.status_all()
    assert [r["robot_id"] for r in records] == ["dog1", "dog2"]
    assert all(r["connected"] is False for r in records)
    assert records[0]["model"] == "Go2"
    assert records[1]["role"] == "wing"


async def test_status_all_live_reads_each_robot_concurrently(fleet):
    await fleet.get("dog1").session.ensure_connected()

    records = await fleet.status_all(live=True)
    by_id = {r["robot_id"]: r for r in records}

    assert by_id["dog1"]["connected"] is True
    assert "state" in by_id["dog1"]
    assert by_id["dog2"]["state"] is None


async def test_one_failing_robot_does_not_abort_the_others(fleet):
    """Fan out must be per-robot fault tolerant."""

    async def boom(*args, **kwargs):
        raise RuntimeError("transport exploded")

    # Both dogs must actually be connected, otherwise `_stop_one` short
    # circuits to link="down" and never reaches the failing call.
    await fleet.get("dog1").session.ensure_connected()
    await fleet.get("dog2").session.ensure_connected()
    fleet.get("dog1").session.safe_stop = boom  # type: ignore[assignment]

    outcomes = await fleet.stop_all()
    by_id = {o.robot_id: o for o in outcomes}

    assert by_id["dog1"].link == "error"
    assert "transport exploded" in by_id["dog1"].detail
    # dog2 still got its own attempt
    assert by_id["dog2"].link == "up"


async def test_aclose_all_is_safe_and_idempotent(fleet):
    await fleet.aclose_all()
    await fleet.aclose_all()  # must not raise


async def test_runtimes_do_not_connect_until_asked(fleet):
    """Building a fleet must never touch the network."""
    assert all(not fleet.get(rid).session.is_connected for rid in fleet.ids)
