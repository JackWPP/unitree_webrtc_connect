"""The active-fault picture: labels, and snapshot/delta reconciliation.

Two behaviours are worth pinning down, both of which the previous
implementation got wrong.

*Reconciliation.* The three message types are not peers. `errors` is an
authoritative snapshot and *replaces* the set; `add_error` / `rm_error` are
single deltas. The old code treated them as one append-only stream and derived
"active" by filtering out `rm_error`, so a fault cleared in a snapshot nobody
looked at stayed visible forever — and a reconnect, which re-sends the whole
snapshot, looked like a fresh batch of faults.

*Labels.* Sources 301–399 and 3000–3999 are per-motor: the same bit catalog as
source 300, reported for one joint. Without that synthesis an operator sees
"3000-1000000" where "Motor 7: ..." is the useful answer.
"""

from __future__ import annotations

import pytest

from unitree_robot_agent.backends.mock_backend import MockBackend
from unitree_robot_agent.core.faults import (
    describe_source,
    FaultStore,
    code_label,
    motor_index,
    source_label,
)
from unitree_robot_agent.runtime import create_runtime

from .conftest import make_config


# ─── labels ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "source,expected",
    [(301, 1), (305, 5), (399, 99), (3007, 7), (3123, 23), (300, None),
     (100, None), (1000, None)],
)
def test_motor_index_maths(source, expected):
    assert motor_index(source) == expected


def test_a_per_motor_source_is_named_by_its_joint():
    """The category and the joint are separate fields.

    Baking the joint into `source_label` would make a UI that shows both
    render "Motor 1 #1".
    """
    assert source_label(301) == "Motor"      # category only
    assert motor_index(301) == 1             # joint, separately
    assert describe_source(301) == "Motor 1"  # and combined on request
    assert describe_source(3007) == "Motor 7"
    assert describe_source(100) == "System"


def test_known_sources_get_their_name():
    assert source_label(100) == "System"
    assert source_label(300) == "Motor"
    # G1-only sources the driver's catalog has no entry for at all.
    assert source_label(800) == "Chassis"
    assert source_label(1000) == "Emergency stop"


def test_an_unknown_source_degrades_readably():
    assert source_label(4242) == "Source 4242"


def test_a_catalogued_code_gets_its_message():
    assert code_label(300, 1) == "Overcurrent"
    assert code_label(100, 1) == "DDS message timeout"


def test_a_per_motor_code_reuses_the_motor_catalog():
    """301/1 is motor 1's Overcurrent bit — same catalog as source 300."""
    assert code_label(301, 1) == "Overcurrent"
    assert code_label(3007, 1) == "Overcurrent"


def test_an_uncatalogued_code_stays_short_because_the_source_is_a_field():
    """The source and joint are their own fields; repeating them in the label
    makes a UI that shows all three read "Motor 1  Motor 1: code 0x1"."""
    assert code_label(300, 1 << 22) == "code 0x400000"


# ─── reconciliation ───────────────────────────────────────────────────


def test_a_snapshot_replaces_the_set():
    store = FaultStore()
    store.apply("errors", [[1.0, 300, 1], [1.0, 200, 2]])
    assert store.count() == 2

    # Second snapshot no longer mentions the fan fault: it is gone.
    store.apply("errors", [[2.0, 300, 1]])
    assert [f.key for f in store.active()] == ["300:1"]


def test_a_snapshot_marks_the_store_authoritative():
    store = FaultStore()
    assert store.authoritative is False  # we have heard nothing yet
    store.apply("add_error", [1.0, 300, 1])
    assert store.authoritative is False  # a delta is not a full picture
    store.apply("errors", [[1.0, 300, 1]])
    assert store.authoritative is True


def test_first_seen_survives_a_resnapshot():
    """Otherwise a console refreshing every second reports every fault as new."""
    store = FaultStore()
    store.apply("errors", [[100.0, 300, 1]], at=100.0)
    first = store.active()[0].first_seen

    store.apply("errors", [[200.0, 300, 1]], at=200.0)
    fault = store.active()[0]

    assert fault.first_seen == first == 100.0
    assert fault.last_seen == 200.0


def test_adding_an_active_fault_again_is_not_a_change():
    store = FaultStore()
    store.apply("add_error", [1.0, 300, 1])
    revision = store.revision

    delta = store.apply("add_error", [2.0, 300, 1])
    assert delta.changed is False
    assert store.revision == revision


def test_removing_reports_what_went_away():
    store = FaultStore()
    store.apply("add_error", [1.0, 300, 1])

    delta = store.apply("rm_error", [2.0, 300, 1])
    assert [f.key for f in delta.removed] == ["300:1"]
    assert store.count() == 0


def test_removing_something_that_was_never_active_is_harmless():
    store = FaultStore()
    delta = store.apply("rm_error", [1.0, 300, 1])
    assert delta.changed is False


def test_a_delta_and_a_snapshot_are_told_apart():
    store = FaultStore()
    assert store.apply("add_error", [1.0, 300, 1]).from_snapshot is False
    assert store.apply("errors", [[1.0, 300, 1]]).from_snapshot is True


def test_the_revision_bumps_only_when_something_changes():
    store = FaultStore()
    assert store.revision == 0
    store.apply("errors", [[1.0, 300, 1]])
    assert store.revision == 1
    store.apply("errors", [[1.0, 300, 1]])  # same set
    assert store.revision == 1
    store.apply("errors", [])
    assert store.revision == 2


def test_snapshot_then_delta_ends_in_the_delta_state():
    """Order matters, and the caller must preserve it."""
    store = FaultStore()
    store.apply("errors", [[1.0, 300, 1]])
    store.apply("add_error", [2.0, 200, 4])
    store.apply("rm_error", [3.0, 300, 1])
    assert [f.key for f in store.active()] == ["200:4"]


def test_clear_resets_everything_including_authority():
    store = FaultStore()
    store.apply("errors", [[1.0, 300, 1]])
    store.clear(reason="disconnect")
    assert store.count() == 0
    assert store.authoritative is False


# ─── wire shapes ──────────────────────────────────────────────────────


def test_a_single_triple_is_accepted_for_the_delta_types():
    """`add_error` / `rm_error` arrive as one triple, not a list of triples.

    The shapes differ per firmware, which is what upstream's `handle_error`
    fix (831ff0e) was about.
    """
    store = FaultStore()
    store.apply("add_error", [1.0, 300, 1])
    assert store.count() == 1


def test_junk_is_ignored_rather_than_crashing():
    store = FaultStore()
    for payload in (None, [], "nonsense", [[1.0, "x", "y"]], [[1.0]], [{}]):
        store.apply("add_error", payload)
    assert store.count() == 0


def test_an_unknown_message_type_changes_nothing():
    store = FaultStore()
    assert store.apply("heartbeat", [[1.0, 300, 1]]).changed is False


# ─── end to end through a runtime ─────────────────────────────────────


async def test_injected_faults_reach_the_runtime_store(tmp_path):
    config = make_config(robot="Go2", backend="mock", state_dir=tmp_path)
    runtime = create_runtime(config)
    backend = runtime.backend
    assert isinstance(backend, MockBackend)

    backend.inject_fault("errors", 300, 1)
    backend.inject_fault("add_error", 301, 1)
    assert [f.key for f in runtime.faults.active()] == ["300:1", "301:1"]
    assert runtime.faults.authoritative is True

    backend.inject_fault("rm_error", 300, 1)
    assert [f.key for f in runtime.faults.active()] == ["301:1"]
    await runtime.aclose()


def test_the_mock_emits_the_wire_shape_the_robot_does():
    backend = MockBackend("Go2")
    seen = []
    backend.set_error_listener(seen.append)

    snapshot = backend.inject_fault("errors", 300, 1)
    delta = backend.inject_fault("add_error", 200, 4)

    assert isinstance(snapshot["data"][0], list)      # [ts, src, code] list
    assert not isinstance(delta["data"][0], list)     # a bare [ts, src, code]
    assert isinstance(delta["data"][1], int) and isinstance(delta["data"][2], int)
    assert [e["type"] for e in seen] == ["errors", "add_error"]


async def test_the_errors_tool_reports_the_reconciled_set(tmp_path):
    from unitree_robot_agent.cli import _invoke
    from unitree_robot_agent.server import build_server

    runtime = create_runtime(make_config(robot="Go2", backend="mock", state_dir=tmp_path))
    bundle = build_server(runtime, fleet_from_env=False)
    runtime.backend.inject_fault("errors", 300, 1)

    result = await _invoke(bundle, "robot_errors", {})

    assert result["ok"] is True
    assert result["active_count"] == 1
    assert result["authoritative"] is True
    assert result["active"][0]["label"] == "Overcurrent"
    assert result["active"][0]["source_label"] == "Motor"
    # The raw history is still there for anyone who wants it.
    assert result["errors"]
    await runtime.aclose()


async def test_two_robots_do_not_share_a_fault_picture(tmp_path):
    from unitree_robot_agent.fleet import FleetManager, Roster

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
    fleet = FleetManager(roster, backend="mock", audit_to_stderr=False)

    fleet.get("dog1").backend.inject_fault("errors", 300, 1)
    fleet.get("dog2").backend.inject_fault("errors", 100, 1)

    assert [f.key for f in fleet.get("dog1").faults.active()] == ["300:1"]
    assert [f.key for f in fleet.get("dog2").faults.active()] == ["100:1"]
    await fleet.aclose_all()
