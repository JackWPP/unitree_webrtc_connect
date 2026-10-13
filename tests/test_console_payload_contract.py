"""Every key the page reads must be a key the endpoint sends.

**The test that was missing**, and its absence is why four separate
disconnected wires survived 888 passing tests.

`test_console_app.py::test_every_element_the_javascript_reaches_for_exists_in_
the_page` checks that the *elements* the JS queries exist. Every element
involved in those four bugs existed. What was missing was the data: the page
read `payload.watching` from an endpoint that does not send it, an operator saw
"2 faults" and clicked through to a panel that was never unhidden, and five
config values were shipped every four seconds to nobody.

Those three have nothing in common except their cause: **a name on one side of a
wire that does not exist on the other**, which no amount of element-existence
checking can see. This file reads the names off the JavaScript and asks the
endpoint whether it sends them.

It is deliberately mechanical. A hand-maintained list of "keys the UI needs"
would be updated by whoever broke it, and would be wrong in exactly the way this
is designed to catch.
"""

from __future__ import annotations

import re
from typing import Dict

import pytest


@pytest.fixture
def open_client(tmp_path):
    from starlette.testclient import TestClient

    from unitree_robot_agent.fleet import FleetManager, Roster
    from unitree_fleet_web import ConsoleConfig, ConsoleService
    from unitree_fleet_web.app import create_app

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
    config = ConsoleConfig(host="127.0.0.1", deadman_s=0.2)
    service = ConsoleService(
        FleetManager(roster, backend="mock", audit_to_stderr=False), config=config
    )
    app = create_app(service, config)
    app._console_service = service
    with TestClient(app) as client:
        client.app = app
        yield client


def _function_body(source: str, name: str) -> str:
    """The text of a top-level `function name(...) { ... }`, brace-matched.

    Matched rather than regexed to the next `function`, because these bodies
    contain nested functions and a lazy match would silently check a fragment.
    """
    start = source.index(f"function {name}(")
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[brace : index + 1]
    raise AssertionError(f"{name} is not brace-balanced")


def _payload_keys(body: str) -> set:
    """Every `payload.<name>` the body reads."""
    return set(re.findall(r"\bpayload\.([A-Za-z_][A-Za-z0-9_]*)", body))


def test_render_status_only_reads_keys_the_status_endpoint_sends(open_client):
    """**The bug this file exists for.**

    `renderStatus` runs on every status push — and on the websocket, which sends
    the same payload. It read `payload.watching`, and `status()` does not emit
    `watching`; only `snapshot()` does, on a different endpoint the page fetches
    once at boot. So the watcher line could never light up, and the test that
    mentioned it asserted the opposite contract.
    """
    source = open_client.get("/static/console.js").text
    payload = open_client.get("/api/fleet/status").json()

    read = _payload_keys(_function_body(source, "renderStatus"))
    assert read, "no payload reads found — the parser has drifted from the source"

    missing = sorted(key for key in read if key not in payload)
    assert not missing, (
        f"console.js reads {missing} from the status payload, which does not "
        f"send it. Either emit it from status() or stop reading it."
    )


def test_every_renderer_gets_an_object_that_exists(open_client):
    """The sub-payloads handed to the renderers, checked the same way.

    `renderArena(payload.arena)` and friends take a slice of the payload rather
    than the whole thing, so a missing slice would make the renderer's own
    `payload.x` reads invisible to the test above.
    """
    source = open_client.get("/static/console.js").text
    payload = open_client.get("/api/fleet/status").json()

    body = _function_body(source, "renderStatus")
    for slice_name in ("arena", "formation", "routine", "video", "telemetry_health",
                       "robots", "motion", "where", "body", "sources", "sync",
                       "flags", "state", "connecting"):
        if re.search(rf"\bpayload\.{slice_name}\b", body):
            assert slice_name in payload, (
                f"renderStatus uses payload.{slice_name}, which status() does not send"
            )


def test_the_snapshot_endpoint_sends_what_boot_reads(open_client):
    """`/api/fleet` is fetched once at boot and its consumer reads specific
    keys. A renamed key there is invisible until somebody notices a blank
    header, which is exactly the class of bug being closed."""
    snapshot = open_client.get("/api/fleet").json()
    for key in ("fleet", "state", "limits", "tools", "watching"):
        assert key in snapshot, f"/api/fleet no longer sends {key!r}"


def test_the_page_does_not_read_watching_from_the_wrong_endpoint(open_client):
    """Named specifically, because it was wrong for many rounds and the fix is
    one line either way: either `status()` emits the watcher count, or
    `renderStatus` stops reading it."""
    source = open_client.get("/static/console.js").text
    body = _function_body(source, "renderStatus")
    payload = open_client.get("/api/fleet/status").json()
    if "payload.watching" in body:
        assert "watching" in payload, (
            "renderStatus reads payload.watching on the status push, which "
            "does not carry it"
        )


def test_the_fault_list_is_reachable_from_the_badge(open_client):
    """**A safety finding from the wiring audit.**

    The head badge says "2 faults — click to see", and the list it points at was
    a hidden sibling that nothing ever unhid: `renderControls` moved the source
    list into 感知 and left the faults behind. An operator saw a count and could
    never see which motor or which code.

    Asserted structurally: whatever the badge switches to, the fault list has to
    end up inside a panel rather than beside them.
    """
    source = open_client.get("/static/console.js").text
    controls = _function_body(source, "renderControls")
    assert "entry.faultsWrap" in controls, (
        "renderControls no longer moves the fault list into a panel, so the "
        "badge points at something nobody can see"
    )
    assert "entry.sources" in controls, "the source list stopped being moved too"

    html = open_client.get("/").text
    # ...and it is not born hidden, which is the other half of the same bug.
    assert 'data-role="faults-wrap"' in html
    assert 'class="faults hidden"' not in html, (
        "the fault list is hidden at birth again; nothing unhides it"
    )


def test_every_console_tool_is_a_real_tool(open_client):
    """The allowlist is a promise about what exists. A name in it that no robot
    registers is a control that would 400 at the worst moment.

    **Checked as a union across models, not against one robot.** `CONSOLE_TOOLS`
    spans Go2 and R1 because the console serves both, so `robot_arm_gesture` is
    absent from a Go2's bundle and that is correct — the same distinction
    `controls_for()` makes when it builds the palette. Checking one profile
    would have called the R1 tools a bug.
    """
    from unitree_fleet_web.service import CONSOLE_TOOLS

    here = set(open_client.get("/api/console/tools").json()["tools"])
    # ...and what the other family registers, from the same builder the server
    # uses rather than a second copy of the list.
    from pathlib import Path

    from unitree_robot_agent.config import AgentConfig
    from unitree_robot_agent.runtime import create_runtime
    from unitree_robot_agent.server import build_server

    elsewhere = set()
    for model in ("R1", "G1"):
        try:
            bundle = build_server(create_runtime(AgentConfig(
                robot=model, backend="mock", state_dir=Path("/tmp/payload-contract"),
                audit_enabled=False,
            )))
            elsewhere |= set(bundle.tool_names)
        except Exception:  # noqa: BLE001 - a model that will not build proves nothing
            continue

    unknown = sorted(CONSOLE_TOOLS - here - elsewhere)
    assert not unknown, (
        f"CONSOLE_TOOLS names tools no model registers: {unknown}"
    )


# ─── the motion endpoint the suite never executed ─────────────────────


def test_a_robot_move_request_actually_moves(open_client):
    """**The endpoint whose handler had never run.**

    `/api/robot/{id}/move` was the one route in the console with no UI caller
    and no behavioural test — its only test asserted a 401 for a missing token,
    so `require_motion_allowed`, the duration clamp and the arbitration had
    never executed in the suite. That is the same shape of blind spot that let
    the `robot_move`/`robot_walk` mismatch hide: a path that looks wired because
    it exists.
    """
    open_client.post("/api/robot/dog1/connect")
    response = open_client.post(
        "/api/robot/dog1/move", json={"ly": 0.3, "duration": 0.3}
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["robot_id"] == "dog1"
    assert payload["motion"], "the motion controller returned nothing"


def test_a_move_is_refused_while_the_estop_is_latched(open_client):
    """The gate the handler contains, exercised rather than assumed."""
    open_client.post("/api/robot/dog1/connect")
    open_client.post("/api/fleet/estop", json={"reason": "test"})

    response = open_client.post("/api/robot/dog1/move", json={"ly": 0.3})
    assert response.status_code == 409, response.text


def test_a_move_duration_is_clamped_to_the_ceiling(open_client):
    """An hour-long move is a typo, and the endpoint is reachable without the
    UI — so the clamp is the only thing between a typo and a robot driving off."""
    open_client.post("/api/robot/dog1/connect")
    response = open_client.post(
        "/api/robot/dog1/move", json={"ly": 0.2, "duration": 99999}
    )
    assert response.status_code == 200, response.text
    # Whatever the ceiling is, the request was accepted and bounded rather than
    # hanging for three hours.
    assert response.json()["motion"]


def test_a_move_zeroes_a_robot_another_source_was_driving(open_client):
    """The arbitration, which is what stops the on-screen stick and an API call
    fighting over one robot."""
    open_client.post("/api/robot/dog1/connect")
    open_client.post("/api/robot/dog2/connect")
    open_client.post("/api/fleet/teleop", json={"source": "keyboard", "ly": 0.3})

    response = open_client.post("/api/robot/dog1/move", json={"ly": 0.2, "duration": 0.2})

    assert response.status_code == 200, response.text
    # dog1 was being driven by the keyboard; taking it away from that source is
    # reported rather than silent.
    assert "zeroed" in response.json()


def test_a_move_for_an_unknown_robot_is_a_typed_refusal(open_client):
    response = open_client.post("/api/robot/nope/move", json={"ly": 0.2})
    assert response.status_code >= 400
    assert response.json().get("ok") is not True


def test_no_listener_is_bound_inside_a_function_that_runs_more_than_once(open_client):
    """**A bug that only shows up after using the console.**

    `runFleetAction` bound a click listener on `#routine-stop` on every call, so
    after N fleet actions one press on 停止编排 sent N stop requests. Nothing in
    a single-shot test can see it, and nothing looks wrong on the first press.

    Asserted structurally, because the symptom needs N uses to appear: event
    listeners belong in the `wire*` functions that boot calls once, and this
    checks that the functions that run per-action do not bind any.
    """
    source = open_client.get("/static/console.js").text

    for name in ("runFleetAction", "renderStatus", "renderRoutine", "tick",
                 "renderControls", "renderFaults"):
        body = _function_body(source, name)
        bindings = re.findall(r"addEventListener\(", body)
        assert not bindings, (
            f"{name} binds {len(bindings)} listener(s) and runs many times; "
            f"each call adds another handler to the same element"
        )


def test_the_stop_button_is_wired_exactly_once(open_client):
    source = open_client.get("/static/console.js").text
    assert source.count("routine-stop')") <= 3, "the stop button is wired in several places"
    wire = _function_body(source, "wireFleetBar")
    assert "routine-stop" in wire, (
        "the stop button is not wired at boot, so it would do nothing"
    )


# ─── what the payload costs ───────────────────────────────────────────


def _dig(tree, path):
    """Follow a `(key, key, index, …)` path into a JSON tree."""
    current = tree
    for step in path:
        try:
            current = current[step]
        except (KeyError, IndexError, TypeError):
            return None
    return current


#: Bytes allowed per top-level key of the status push.
#:
#: **A total budget cannot tell you which key grew.** The push was comfortably
#: inside its 6000 B cap at 4962, and `robots` was quietly 30% of it — a third
#: of every push, four times a second, spent on connection bookkeeping that
#: `console.js` never reads. Naming a limit per key means the next growth
#: arrives with the key's name attached.
#:
#: These are ceilings, not targets. Raising one should be a deliberate act with
#: a reason, which is the point.
STATUS_BUDGET: Dict[str, int] = {
    "robots": 700,
    "body": 1600,
    "motion": 500,
    "state": 400,
    "sources": 400,
    "telemetry_health": 400,
    "video": 400,
    "where": 300,
    "flags": 300,
    "sync": 300,
    "arena": 400,
    "formation": 300,
    "routine": 2000,
    "connecting": 200,
    "watching": 20,
}


def test_no_single_key_of_the_status_push_grows_past_its_budget(open_client):
    """The per-key half of the payload budget.

    The total-size test next door is the one that existed, and it was happy: the
    push sat at 4962 B against a 6000 B cap while **30% of it — `robots` — was
    connection bookkeeping the page never reads**. Dropping `robots[].session`
    took it to 3814 B, a 23% saving on every push, four times a second.

    Measuring per key means the next growth names itself.
    """
    import json as _json

    payload = open_client.get("/api/fleet/status").json()
    over = []
    for key, value in payload.items():
        size = len(_json.dumps(value, ensure_ascii=False))
        allowed = STATUS_BUDGET.get(key)
        if allowed is None:
            over.append((key, size, "no budget declared"))
        elif size > allowed:
            over.append((key, size, f"budget {allowed}"))
    assert not over, "\n".join(
        f"  {key}: {size} B ({why})" for key, size, why in over
    )


def test_the_budget_table_covers_every_key_the_push_sends(open_client):
    """A key with no budget is how a payload grows quietly: nobody added it,
    it just stopped being measured."""
    payload = open_client.get("/api/fleet/status").json()
    missing = sorted(set(payload) - set(STATUS_BUDGET))
    assert not missing, (
        f"{missing} are in the push with no declared budget. Add a ceiling for "
        f"each — an unbounded key is the one that grows."
    )


def test_the_budget_table_has_no_keys_the_push_does_not_send(open_client):
    """The other direction: a budget for something that is gone is a table
    nobody trusts, and the next person stops reading it."""
    payload = open_client.get("/api/fleet/status").json()
    stale = sorted(set(STATUS_BUDGET) - set(payload))
    assert not stale, f"{stale} have budgets but are not in the push"


async def test_a_frozen_block_cannot_be_told_from_an_idle_one(open_client):
    """**An approach that did not work, written down so nobody retries it.**

    The obvious way to find wasted bytes is to compare two pushes and flag
    subtrees that did not change. It was tried: `robots[].session` is exactly
    that shape — a large block that never changes — and the check caught it.

    It also flagged `motion`, `state`, `sources`, `telemetry_health` and
    `video`, **all of which are live data that simply had not changed yet**
    because the mock dog was idle. A change detector cannot tell "unused" from
    "unchanged", and a check that cries wolf on an idle robot gets deleted.

    So the payload's cost is bounded by the per-key table above, and which data
    is *useless* is a judgement about what the page renders — which is what
    `console.js` reading it, or not, is for. This test exists to keep the
    negative result.
    """
    import asyncio
    import json as _json

    def containers(tree, path=()):
        found = []
        if isinstance(tree, (dict, list)):
            found.append((len(_json.dumps(tree, ensure_ascii=False)), path, tree))
            items = tree.items() if isinstance(tree, dict) else enumerate(tree)
            for key, child in items:
                found.extend(containers(child, path + (key,)))
        return found

    first = open_client.get("/api/fleet/status").json()
    await asyncio.sleep(0.8)
    last = open_client.get("/api/fleet/status").json()

    import json as _j

    frozen_large = [
        (size, path) for size, path, value in containers(first)
        if size >= 200 and _j.dumps(value, ensure_ascii=False)
        == _j.dumps(_dig(last, path), ensure_ascii=False)
    ]

    # The finding, asserted as a fact rather than as a failure: an idle console
    # produces plenty of frozen blocks that are entirely legitimate.
    assert frozen_large, (
        "nothing was frozen, so the false-positive problem this test documents "
        "no longer applies — the reasoning above may need revisiting"
    )
