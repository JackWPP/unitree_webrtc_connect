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
