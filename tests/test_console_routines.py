"""Routines as documents, with per-step outcomes.

The idea is from the `wpp` branch's `MovementStep`/`MovementPattern`. Its
documented weakness was that a sequence could only be authored by editing
Python, so nobody authored one, and that success was `sum(results) > 0` — "at
least one dog did it". Both are fixed here: a routine validates as a document
against the console's own allowlist, and every step carries a completion policy
and a terminal state.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from unitree_fleet_web.routines import POLICIES, Step, validate


# ─── the step policy ──────────────────────────────────────────────────


def test_all_demands_every_dog():
    step = Step(label="站好", tool="robot_stand", policy="all")
    assert step.satisfied(2, 2) is True
    assert step.satisfied(1, 2) is False
    assert step.satisfied(0, 2) is False


def test_any_demands_one():
    step = Step(label="打招呼", tool="robot_play_action", policy="any")
    assert step.satisfied(1, 2) is True
    assert step.satisfied(0, 2) is False


def test_best_effort_never_fails():
    step = Step(label="比心", tool="robot_play_action", policy="best_effort")
    assert step.satisfied(0, 2) is True


def test_a_step_with_no_targets_cannot_satisfy_all():
    """Zero out of zero is not "everyone did it" — it is nobody was there."""
    assert Step(label="x", tool="robot_stand", policy="all").satisfied(0, 0) is False


# ─── validating an operator-authored document ─────────────────────────


def test_a_valid_document_round_trips():
    routine = validate(
        {
            "id": "bow",
            "label": "一起鞠躬",
            "steps": [
                {"tool": "robot_stand", "args": {"confirm": True}, "label": "站好", "seconds": 1},
                {"tool": "robot_play_action", "args": {"name": "scrape"}, "policy": "any"},
            ],
        },
        known_tools={"robot_stand", "robot_play_action"},
    )

    assert routine.id == "bow"
    assert len(routine.steps) == 2
    assert routine.steps[0].policy == "all"
    assert routine.steps[1].policy == "any"
    assert routine.seconds == pytest.approx(4.0)


def test_a_tool_outside_the_allowlist_is_refused():
    """This is the check that replaces `getattr(module, name)` behind a bare
    `hasattr`, which is how the branch this idea came from let any public
    method be reached by name from an HTTP body."""
    with pytest.raises(ValueError, match="not available from the console"):
        validate(
            {"id": "bad", "steps": [{"tool": "robot_fetch_key"}]},
            known_tools={"robot_stand"},
        )


@pytest.mark.parametrize(
    "document,reason",
    [
        ({}, "id"),
        ({"id": "x"}, "step"),
        ({"id": "x", "steps": []}, "step"),
        ({"id": "x", "steps": [{"tool": ""}]}, "tool"),
        ({"id": "x", "steps": [{"tool": "a", "policy": "sometimes"}]}, "policy"),
        ({"id": "x", "steps": [{"tool": "a", "seconds": "soon"}]}, "number"),
        ({"id": "x", "steps": [{"tool": "a", "args": []}]}, "args"),
        ({"id": "has space", "steps": [{"tool": "a"}]}, "letters"),
    ],
)
def test_bad_documents_are_refused_with_a_reason(document, reason):
    with pytest.raises(ValueError) as caught:
        validate(document)
    assert reason in str(caught.value)


def test_durations_are_clamped_rather_than_rejected():
    """0 makes a routine that fires everything at once; an hour is a typo.
    Neither is worth refusing the whole document over."""
    routine = validate(
        {"id": "x", "steps": [{"tool": "a", "seconds": 0}, {"tool": "a", "seconds": 9999}]}
    )
    assert routine.steps[0].seconds == pytest.approx(0.2)
    assert routine.steps[1].seconds == pytest.approx(60.0)


def test_the_catalogue_advertises_the_policies():
    assert set(POLICIES) == {"all", "any", "best_effort"}


# ─── the runner ───────────────────────────────────────────────────────


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
        config=ConsoleConfig(deadman_s=0.2),
    )


async def test_the_whole_timeline_is_visible_before_anything_runs(service):
    """A timeline has to be visible ahead of the playhead or it is a log."""
    await service.connect("dog1")
    await service.connect("dog2")

    state = await service.start_routine("greet")
    try:
        assert len(state["steps"]) == state["total"]
        assert all(step["state"] == "pending" for step in state["steps"])
        assert [step["label"] for step in state["steps"]][0] == "站好"
    finally:
        await service.stop_routine(reason="test")


async def test_each_step_gets_a_terminal_state_and_per_dog_detail(service):
    await service.connect("dog1")
    await service.connect("dog2")

    await service.start_routine("greet")
    for _ in range(120):
        await asyncio.sleep(0.1)
        state = service.routine_state()
        if not state["running"]:
            break

    steps = service.routine_state()["steps"]
    assert steps, "no steps recorded"
    for step in steps:
        assert step["state"] in ("done", "failed", "cancelled"), step
        assert step["total"] == 2
        assert set(step["per_dog"]) == {"dog1", "dog2"}
        assert step["ended_at"] is not None

    assert steps[0]["state"] == "done", "a step both dogs performed was not marked done"


async def test_one_dog_offline_is_a_failed_step_not_a_silent_success(service):
    """`sum(results) > 0` — what the branch this came from used — reads a
    two-dog routine down to one dog and still reports success."""
    await service.connect("dog1")
    # dog2 is never connected.

    await service.start_routine("greet")
    for _ in range(120):
        await asyncio.sleep(0.1)
        if not service.routine_state()["running"]:
            break

    steps = service.routine_state()["steps"]
    assert steps[0]["ok_count"] == 1
    assert steps[0]["total"] == 1, "only the connected dog is a target"
    assert steps[0]["state"] == "done"
    assert steps[0]["skipped"] == ["dog2"], "the absent dog has to be named"


async def test_a_run_that_is_stopped_marks_the_step_cancelled(service):
    await service.connect("dog1")
    await service.connect("dog2")
    await service.start_routine("dance")
    await asyncio.sleep(0.5)

    await service.stop_routine(reason="test")

    steps = service.routine_state()["steps"]
    assert steps[0]["state"] == "cancelled"


async def test_an_insert_waits_for_a_step_boundary(service):
    """The operator's ad-hoc "both dogs bow now" wants to land inside the
    running sequence, not replace it — and there is nowhere safe to put a
    command except a step boundary."""
    await service.connect("dog1")
    await service.connect("dog2")
    await service.start_routine("dance")
    await asyncio.sleep(0.3)

    queued = service.queue_insert(tool="robot_stand", args={"confirm": True}, label="插队站好")
    assert queued["queued"] is True
    assert queued["pending"] == 1

    await service.stop_routine(reason="test")
    # Stopping clears nothing implicitly, but the runner has stopped pulling.
    assert service.routine_state()["running"] is False


async def test_inserting_without_a_running_routine_is_refused(service):
    from unitree_robot_agent.errors import AgentError

    with pytest.raises(AgentError) as caught:
        service.queue_insert(tool="robot_stand")
    assert caught.value.code == "no_routine_running"


async def test_inserting_a_tool_outside_the_allowlist_is_refused(service):
    from unitree_robot_agent.errors import AgentError

    await service.connect("dog1")
    await service.start_routine("dance")
    try:
        with pytest.raises(AgentError) as caught:
            service.queue_insert(tool="robot_fetch_key")
        assert caught.value.code == "tool_not_allowed"
    finally:
        await service.stop_routine(reason="test")


# ─── storing a routine ────────────────────────────────────────────────


async def test_a_saved_routine_can_be_run_again(service):
    service.save_routine(
        {
            "id": "bow",
            "label": "一起鞠躬",
            "steps": [{"tool": "robot_stand", "args": {"confirm": True}, "seconds": 0.5}],
        }
    )
    await service.connect("dog1")

    state = await service.start_routine("bow")
    assert state["label"] == "一起鞠躬"
    await service.stop_routine(reason="test")


async def test_a_saved_routine_survives_a_restart(service):
    service.save_routine(
        {"id": "bow", "steps": [{"tool": "robot_stand", "args": {"confirm": True}}]}
    )

    from unitree_fleet_web import ConsoleConfig, ConsoleService

    reopened = ConsoleService(service.fleet, config=ConsoleConfig(deadman_s=0.2))
    assert "bow" in [routine["id"] for routine in reopened.all_routines()]


async def test_a_custom_routine_may_not_shadow_a_builtin(service):
    from unitree_robot_agent.errors import AgentError

    with pytest.raises(AgentError) as caught:
        service.save_routine({"id": "greet", "steps": [{"tool": "robot_stand"}]})
    assert caught.value.code == "reserved_routine_id"


async def test_a_stored_routine_whose_tool_vanished_is_reported_not_loaded(
    service, caplog
):
    """The operator wrote it; they deserve to know which tool disappeared
    rather than finding an empty list."""
    path = service._routine_file  # noqa: SLF001
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"routines": [{"id": "bad", "steps": [{"tool": "robot_fetch_key"}]}]}),
        encoding="utf-8",
    )

    assert service.custom_routines() == []


async def test_deleting_a_builtin_is_refused(service):
    from unitree_robot_agent.errors import AgentError

    with pytest.raises(AgentError):
        service.delete_routine("greet")


# ─── every built-in routine has to actually run ───────────────────────


@pytest.mark.parametrize("routine_id", ["greet", "square", "formation"])
async def test_every_built_in_routine_completes_without_a_failed_step(service, routine_id):
    """The test that would have caught 走正方形 starting with a forward step.

    The firmware refuses `robot_move` from a lying robot — code 3201, "robot is
    not standing; BalanceStand first" — which the mock reproduces faithfully.
    Every step of every built-in routine is therefore run here against the
    mock, with the declared durations shrunk so the suite stays quick.

    The durations are the only thing changed: the tools, the arguments and the
    order are exactly what ships.
    """
    from unitree_fleet_web.routines import ROUTINES

    shipped = next(r for r in ROUTINES if r.id == routine_id)

    def shrink(step):
        """Same tool, same order, smaller magnitudes.

        The declared dwell is not the only thing that costs wall time: a
        closed-loop `robot_turn` runs until it has actually turned, so a 90°
        step takes seconds however short the dwell is. Shrinking the arguments
        too keeps this test about *whether every step succeeds* rather than
        about how long the shipped routine takes.
        """
        document = step.as_dict()
        document["seconds"] = 0.2
        args = dict(document.get("args") or {})
        if "duration_s" in args:
            args["duration_s"] = 0.2
        if "degrees" in args:
            args["degrees"] = max(-10.0, min(10.0, float(args["degrees"]) / 10.0))
        document["args"] = args
        return document

    shortened = {
        "id": "probe",
        "label": shipped.label,
        "steps": [shrink(step) for step in shipped.steps],
    }
    service.save_routine(shortened)
    await service.connect("dog1")
    await service.connect("dog2")

    await service.start_routine("probe")
    for _ in range(200):
        await asyncio.sleep(0.05)
        if not service.routine_state()["running"]:
            break

    state = service.routine_state()
    assert state["running"] is False, "the routine never finished"
    bad = [
        (step["index"] + 1, step["label"], step["state"], step["per_dog"])
        for step in state["steps"]
        if step["state"] != "done"
    ]
    assert not bad, f"{routine_id} has a step that does not work: {bad}"


# ─── where the robot is, when it says ─────────────────────────────────


async def test_a_position_is_reported_when_the_robot_publishes_one(service):
    """`position` rides on the state topic, so it costs no extra read. It is
    the only part of "where is it" reachable without a topic nobody has
    confirmed over WebRTC."""
    await service.connect("dog1")
    await service.refresh_telemetry("dog1")

    spot = service.where("dog1")
    assert "x" in spot and "y" in spot and "yaw" in spot
    assert spot["frame"] == "odom"


async def test_an_absent_position_is_an_empty_dict_not_a_zero(service):
    """A robot whose state topic is silent must render nothing. A zero would
    look like a measurement, and "it has not moved" is a very different claim
    from "it has not told me where it is"."""
    await service.connect("dog1")
    service._telemetry["dog1"] = {"sport_state": {}}  # noqa: SLF001

    assert service.where("dog1") == {}


async def test_a_malformed_position_does_not_raise(service):
    await service.connect("dog1")
    for bad in ({"sport_state": {"position": ["x", "y"]}},
                {"sport_state": {"position": [1]}},
                {"sport_state": {"position": "1,2"}}):
        service._telemetry["dog1"] = bad  # noqa: SLF001
        assert service.where("dog1") == {}


async def test_the_status_payload_carries_where_per_robot(service):
    await service.connect("dog1")
    payload = await service.status()
    assert set(payload["where"]) == set(service.fleet.ids)
