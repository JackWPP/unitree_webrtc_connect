"""Fleet tools: addressing a specific robot, and stopping all of them.

The mechanism under test is that the 31 existing tool bodies were *not*
rewritten — they still say ``ctx.session`` — and the registration wrapper
binds each call to the robot named by its ``robot_id`` argument. So these
tests check the binding actually happens, and that a bad id produces a usable
message rather than a traceback.
"""

from __future__ import annotations

import json

import pytest

from unitree_robot_agent.cli import _invoke
from unitree_robot_agent.fleet import FleetManager, Roster
from unitree_robot_agent.server import build_server

DOG1_SN = "B42D2000AAAA"
DOG2_SN = "B42D2000BBBB"

FLEET_TOOLS = {
    "robot_roster",
    "robot_fleet_status",
    "robot_stop_all",
    "robot_emergency_stop_all",
}


def roster_mapping(tmp_path, *, models=("Go2", "Go2")):
    return {
        "state_dir": str(tmp_path / "state"),
        "default": "dog1",
        "robots": [
            {"id": "dog1", "model": models[0], "ip": "192.168.31.245",
             "sn": DOG1_SN, "role": "lead"},
            {"id": "dog2", "model": models[1], "ip": "192.168.31.246",
             "sn": DOG2_SN, "role": "wing"},
        ],
    }


@pytest.fixture
def fleet_bundle(tmp_path):
    roster = Roster.from_mapping(roster_mapping(tmp_path))
    fleet = FleetManager(roster, backend="mock", audit_to_stderr=False)
    bundle = build_server(fleet.get("dog1"), fleet=fleet)
    yield bundle, fleet


# ─── the tools exist and are advertised ───────────────────────────────


def test_fleet_tools_are_registered_even_for_one_robot():
    """`robot_roster` is useful with a single robot too: it says what the
    model is driving instead of making it infer."""
    from unitree_robot_agent.runtime import create_runtime

    from .conftest import make_config

    bundle = build_server(create_runtime(make_config(robot="Go2")))
    assert FLEET_TOOLS <= set(bundle.tool_names)


async def test_every_tool_advertises_robot_id(fleet_bundle):
    """The mechanism depends on MCP exposing the argument.

    The registration wrapper preserves the signature via `functools.wraps`,
    which is what lets the SDK build the schema from `__wrapped__`. If that
    ever stops holding, the model silently loses the ability to address a
    robot — so assert it on a representative sample.
    """
    bundle, _ = fleet_bundle
    tools = {t.name: t for t in await bundle.server.list_tools()}
    for name in ("robot_status", "robot_move", "robot_stop", "robot_roster"):
        schema = tools[name].input_schema
        assert "robot_id" in schema["properties"], f"{name} does not expose robot_id"


# ─── the roster ───────────────────────────────────────────────────────


async def test_roster_lists_both_robots_without_leaking_keys(fleet_bundle):
    bundle, _ = fleet_bundle
    result = await _invoke(bundle, "robot_roster", {})

    assert result["ok"] is True
    assert result["count"] == 2
    assert result["default"] == "dog1"
    assert [r["robot_id"] for r in result["robots"]] == ["dog1", "dog2"]
    assert result["robots"][1]["role"] == "wing"
    assert "aes_128_key" not in str(result)


async def test_roster_works_for_a_single_robot_too():
    from unitree_robot_agent.runtime import create_runtime

    from .conftest import make_config

    bundle = build_server(create_runtime(make_config(robot="Go2", ip="192.168.1.5")))
    result = await _invoke(bundle, "robot_roster", {})
    assert result["count"] == 1
    assert result["robots"][0]["robot_id"] == "192.168.1.5"


# ─── addressing one robot ─────────────────────────────────────────────


async def test_robot_id_selects_the_robot_a_tool_acts_on(fleet_bundle):
    """The whole point: two same-model robots stay distinguishable."""
    bundle, fleet = fleet_bundle

    first = await _invoke(bundle, "robot_status", {"robot_id": "dog1"})
    second = await _invoke(bundle, "robot_status", {"robot_id": "dog2"})

    assert first["robot_id"] == "dog1"
    assert second["robot_id"] == "dog2"
    assert first["robot_id"] != second["robot_id"]


async def test_omitting_robot_id_uses_the_roster_default(fleet_bundle):
    bundle, _ = fleet_bundle
    result = await _invoke(bundle, "robot_status", {})
    assert result["robot_id"] == "dog1"


async def test_an_unknown_robot_id_is_a_clean_error(fleet_bundle):
    """A model that names a robot that does not exist must be told so, with
    the valid names, rather than getting a stack trace."""
    bundle, _ = fleet_bundle
    result = await _invoke(bundle, "robot_status", {"robot_id": "dog9"})

    assert result["ok"] is False
    assert result["error"] == "unknown_robot"
    # The message names what *is* configured; the hint says how to look it up.
    assert "dog1" in result["message"] and "dog2" in result["message"]
    assert "robot_roster" in result["hint"]


async def test_a_movement_tool_is_bound_to_the_named_robot(fleet_bundle):
    bundle, fleet = fleet_bundle
    await _invoke(bundle, "robot_stand", {"confirm": True, "robot_id": "dog2"})

    # Only dog2 was touched; dog1 never connected.
    assert fleet.get("dog2").session.is_connected is True
    assert fleet.get("dog1").session.is_connected is False


async def test_each_robot_keeps_its_own_audit_trail(fleet_bundle):
    """Two same-model robots must be distinguishable in the trail.

    Regression: every AuditLog used to add a handler to one process-wide
    logger, so each record was written to *all* the robots' files.
    """
    bundle, fleet = fleet_bundle
    await _invoke(bundle, "robot_status", {"robot_id": "dog1"})
    await _invoke(bundle, "robot_status", {"robot_id": "dog2"})

    trail1 = fleet.get("dog1").audit.tail(10)
    trail2 = fleet.get("dog2").audit.tail(10)

    # Every record in each trail belongs to that robot and no other.
    assert trail1 and trail2
    assert {r["robot_id"] for r in trail1} == {"dog1"}
    assert {r["robot_id"] for r in trail2} == {"dog2"}
    assert "robot_status" in {r["tool"] for r in trail1}
    assert "robot_status" in {r["tool"] for r in trail2}


# ─── fleet status ─────────────────────────────────────────────────────


async def test_fleet_status_reports_every_robot(fleet_bundle):
    bundle, _ = fleet_bundle
    result = await _invoke(bundle, "robot_fleet_status", {})

    assert result["count"] == 2
    assert result["online"] == []
    assert sorted(result["offline"]) == ["dog1", "dog2"]


async def test_fleet_status_live_reads_state(fleet_bundle):
    bundle, fleet = fleet_bundle
    await fleet.get("dog1").session.ensure_connected()

    result = await _invoke(bundle, "robot_fleet_status", {"live": True})
    by_id = {r["robot_id"]: r for r in result["robots"]}

    assert by_id["dog1"]["connected"] is True
    assert "state" in by_id["dog1"]
    assert by_id["dog2"]["connected"] is False


# ─── fleet stops ──────────────────────────────────────────────────────


async def test_stop_all_names_the_robots_it_could_not_stop(fleet_bundle):
    """A single boolean would hide which dog is still a hazard."""
    bundle, _ = fleet_bundle
    result = await _invoke(bundle, "robot_stop_all", {})

    assert result["ok"] is True
    assert result["complete"] is False
    assert sorted(result["not_stopped"]) == ["dog1", "dog2"]
    assert result["stopped"] == []
    assert all(o["link"] == "down" for o in result["outcomes"])
    assert "dog1" in result["note"]


async def test_stop_all_reports_a_reachable_robot_as_stopped(fleet_bundle):
    bundle, fleet = fleet_bundle
    await fleet.get("dog1").session.ensure_connected()

    result = await _invoke(bundle, "robot_stop_all", {})
    by_id = {o["robot_id"]: o for o in result["outcomes"]}

    assert by_id["dog1"]["link"] == "up"
    assert by_id["dog1"]["ok"] is True
    assert result["stopped"] == ["dog1"]
    assert result["not_stopped"] == ["dog2"]


async def test_emergency_stop_all_requires_confirmation(fleet_bundle):
    bundle, _ = fleet_bundle
    refused = await _invoke(bundle, "robot_emergency_stop_all", {})
    assert refused["ok"] is False
    assert refused["error"] == "confirm_required"


async def test_emergency_stop_all_reports_undamped_robots(fleet_bundle):
    """Damping is the dangerous one: a robot we could not reach is NOT damped."""
    bundle, _ = fleet_bundle
    result = await _invoke(bundle, "robot_emergency_stop_all", {"confirm": True})

    assert sorted(result["not_damped"]) == ["dog1", "dog2"]
    assert result["damped"] == []
    assert result["complete"] is False
    assert "may still be moving" in result["note"]


async def test_emergency_stop_all_damps_a_connected_robot(fleet_bundle):
    bundle, fleet = fleet_bundle
    await fleet.get("dog1").session.ensure_connected()

    result = await _invoke(bundle, "robot_emergency_stop_all", {"confirm": True})

    assert result["damped"] == ["dog1"]
    assert result["not_damped"] == ["dog2"]


# ─── the fleet resource ───────────────────────────────────────────────


async def test_fleet_status_resource_is_registered(fleet_bundle):
    bundle, _ = fleet_bundle
    manager = getattr(bundle.server, "_resource_manager", None)
    assert manager is not None
    uris = {str(item.uri) for item in manager.list_resources()}
    assert "unitree://fleet/status" in uris


async def test_fleet_payload_lists_the_roster_without_keys(fleet_bundle):
    bundle, _ = fleet_bundle
    from unitree_robot_agent.server import fleet_payload

    payload = fleet_payload(bundle.context)

    assert payload["count"] == 2
    assert payload["default"] == "dog1"
    assert [r["robot_id"] for r in payload["robots"]] == ["dog1", "dog2"]
    assert all(r["connected"] is False for r in payload["robots"])
    # A resource must never hand out credentials.
    assert "aes_128_key" not in json.dumps(payload)


async def test_fleet_payload_works_for_a_single_robot():
    from unitree_robot_agent.runtime import create_runtime
    from unitree_robot_agent.server import fleet_payload

    from .conftest import make_config

    bundle = build_server(create_runtime(make_config(robot="Go2", ip="192.168.1.5")))
    payload = fleet_payload(bundle.context)
    assert payload["count"] == 1
    assert payload["robots"][0]["robot_id"] == "192.168.1.5"
    assert "robot_id may be omitted" in payload["note"]


# ─── single-robot behaviour is unchanged ──────────────────────────────


async def test_single_robot_accepts_its_own_id_but_not_another():
    from unitree_robot_agent.runtime import create_runtime

    from .conftest import make_config

    bundle = build_server(create_runtime(make_config(robot="Go2", ip="192.168.1.5")))

    ok = await _invoke(bundle, "robot_status", {"robot_id": "192.168.1.5"})
    assert ok["ok"] is True

    wrong = await _invoke(bundle, "robot_status", {"robot_id": "dog9"})
    assert wrong["ok"] is False
    assert wrong["error"] == "unknown_robot"
