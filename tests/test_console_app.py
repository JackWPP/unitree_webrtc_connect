"""The console's HTTP surface: auth, routing, and error shapes.

The auth tests matter more than they look. A console that can drive two
robots is a control surface, and the previous attempt (``wpp``'s
``web_server.py``) shipped twenty-three routes with no authentication at all,
bound to ``0.0.0.0``. These tests exist so that cannot come back quietly.
"""

from __future__ import annotations

import re
import time

import pytest
from starlette.testclient import TestClient

from unitree_robot_agent.fleet import FleetManager, Roster
from unitree_fleet_web.app import create_app
from unitree_fleet_web.config import ConsoleConfig
from unitree_fleet_web.service import ConsoleService

TOKEN = "test-token-that-is-long-enough"


def make_app(tmp_path, *, token: str = ""):
    roster = Roster.from_mapping(
        {
            "state_dir": str(tmp_path / "state"),
            "default": "dog1",
            "robots": [
                {"id": "dog1", "model": "Go2", "ip": "192.168.31.245", "sn": "B42D2000AAAA"},
                {"id": "dog2", "model": "Go2", "ip": "192.168.31.246", "sn": "B42D2000BBBB"},
            ],
        }
    )
    fleet = FleetManager(roster, backend="mock", audit_to_stderr=False)
    config = ConsoleConfig(host="127.0.0.1", token=token, deadman_s=0.2)
    service = ConsoleService(fleet, config=config)
    app = create_app(service, config)
    app._console_service = service
    return app


@pytest.fixture
def open_client(tmp_path):
    """No token: the loopback-only case."""
    with TestClient(make_app(tmp_path)) as client:
        yield client


@pytest.fixture
def locked_client(tmp_path):
    with TestClient(make_app(tmp_path, token=TOKEN)) as client:
        yield client



def _service_of(client):
    """Reach the ConsoleService behind a TestClient.

    The routes close over it; walking the app's middleware stack is brittle,
    so the fixture stashes it on the app instead.
    """
    return getattr(client.app, "_console_service", None)


AUTH = {"Authorization": f"Bearer {TOKEN}"}


# ─── authentication ───────────────────────────────────────────────────


def test_health_needs_no_token(locked_client):
    assert locked_client.get("/healthz").status_code == 200


def test_every_api_route_is_closed_without_a_token(locked_client):
    assert locked_client.get("/api/fleet").status_code == 401
    assert locked_client.get("/api/fleet/status").status_code == 401
    assert locked_client.post("/api/fleet/stop").status_code == 401
    assert locked_client.post("/api/fleet/estop").status_code == 401
    assert locked_client.post("/api/fleet/estop/release").status_code == 401
    assert locked_client.post("/api/robot/dog1/teleop", json={"ly": 1}).status_code == 401
    assert locked_client.post("/api/robot/dog1/stop").status_code == 401
    assert locked_client.post("/api/robot/dog1/move", json={"ly": 1}).status_code == 401
    assert locked_client.get("/api/robot/dog1/status").status_code == 401
    assert locked_client.get("/api/robot/dog1/stream.mjpeg").status_code == 401


def test_the_ui_itself_is_closed_too(locked_client):
    """Serving the page unauthenticated still leaks the roster and targets."""
    assert locked_client.get("/").status_code == 401


def test_a_wrong_token_is_rejected(locked_client):
    response = locked_client.get("/api/fleet", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_a_bearer_token_is_accepted(locked_client):
    assert locked_client.get("/api/fleet", headers=AUTH).status_code == 200


def test_the_x_console_token_header_is_accepted(locked_client):
    response = locked_client.get("/api/fleet", headers={"X-Console-Token": TOKEN})
    assert response.status_code == 200


def test_a_query_token_is_accepted_for_reads(locked_client):
    """`<img>` cannot set an Authorization header, so MJPEG needs this path."""
    assert locked_client.get(f"/api/fleet?token={TOKEN}").status_code == 200


def test_the_websocket_is_authenticated_as_well(tmp_path):
    """A bypassed socket would still leak telemetry — and accept commands."""
    with TestClient(make_app(tmp_path, token=TOKEN)) as client:
        with pytest.raises(Exception):
            with client.websocket_connect("/ws"):
                pass


def test_an_unauthenticated_console_works_on_loopback(open_client):
    assert open_client.get("/").status_code == 200
    assert open_client.get("/api/fleet").status_code == 200


# ─── reads ────────────────────────────────────────────────────────────


def test_fleet_snapshot_lists_both_robots_and_no_keys(open_client):
    payload = open_client.get("/api/fleet").json()
    ids = [r["robot_id"] for r in payload["fleet"]["robots"]]
    assert ids == ["dog1", "dog2"]
    assert "aes_128_key" not in open_client.get("/api/fleet").text
    assert payload["state"]["input_mode"] == "focus"
    assert payload["state"]["focused"] == "dog1"


def test_status_returns_a_record_per_robot(open_client):
    payload = open_client.get("/api/fleet/status").json()
    assert [r["robot_id"] for r in payload["robots"]] == ["dog1", "dog2"]


def test_the_ui_and_its_assets_are_served(open_client):
    page = open_client.get("/")
    assert page.status_code == 200
    assert "双狗控制台" in page.text
    assert open_client.get("/static/console.js").status_code == 200
    assert open_client.get("/static/console.css").status_code == 200


def test_the_page_has_no_inline_event_handlers(open_client):
    """The previous console built `onclick="removeDog('${name}')"` from a name
    the operator supplied. Inline handlers are how that becomes executable."""
    page = open_client.get("/").text
    assert not re.search(r"\son[a-z]+\s*=", page, re.IGNORECASE)


def test_the_javascript_never_assigns_html(open_client):
    """Check for real assignment, not for the word.

    The file *mentions* `innerHTML` in the comment explaining why it is never
    used, so a substring check would fail on its own documentation.
    """
    source = open_client.get("/static/console.js").text
    for pattern in (r"\.innerHTML\s*=", r"\.outerHTML\s*=", r"document\.write\s*\(",
                    r"insertAdjacentHTML\s*\("):
        assert not re.search(pattern, source), f"found {pattern} in console.js"


# ─── commands ─────────────────────────────────────────────────────────


def test_stop_all_names_what_it_could_not_stop(open_client):
    payload = open_client.post("/api/fleet/stop").json()
    assert payload["complete"] is False
    assert sorted(payload["not_stopped"]) == ["dog1", "dog2"]


def test_estop_latches_and_then_refuses_motion_with_409(open_client):
    latched = open_client.post("/api/fleet/estop", json={"reason": "test"})
    assert latched.status_code == 200
    assert latched.json()["estop"]["engaged"] is True

    blocked = open_client.post("/api/robot/dog1/teleop", json={"ly": 1.0})
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "motion_blocked"
    assert blocked.json()["hint"]


def test_releasing_the_estop_restores_motion(open_client):
    open_client.post("/api/fleet/estop", json={})
    released = open_client.post("/api/fleet/estop/release")
    assert released.json()["released"] is True
    assert open_client.post("/api/robot/dog1/teleop", json={"ly": 1.0}).status_code == 200


def test_teleop_through_the_fleet_endpoint_is_routed_by_the_server(open_client):
    open_client.post("/api/fleet/mode", json={"mode": "both"})
    payload = open_client.post("/api/fleet/teleop", json={"source": "keyboard", "ly": 1.0}).json()
    assert sorted(payload["targets"]) == ["dog1", "dog2"]


def test_an_unbound_source_drives_nothing(open_client):
    open_client.post("/api/fleet/mode", json={"mode": "split"})
    payload = open_client.post("/api/fleet/teleop", json={"source": "unbound", "ly": 1.0}).json()
    assert payload["targets"] == []


def test_binding_a_source_routes_it(open_client):
    open_client.post("/api/fleet/mode", json={"mode": "split"})
    open_client.post("/api/fleet/bind", json={"source": "ijkl", "robot_id": "dog2"})
    payload = open_client.post("/api/fleet/teleop", json={"source": "ijkl", "ly": 1.0}).json()
    assert payload["targets"] == ["dog2"]


def test_focus_can_be_changed(open_client):
    assert open_client.post("/api/fleet/focus", json={"robot_id": "dog2"}).json()["focused"] == "dog2"


# ─── error shapes ─────────────────────────────────────────────────────


def test_an_unknown_robot_is_a_404_with_a_hint(open_client):
    response = open_client.post("/api/robot/dog9/teleop", json={"ly": 1.0})
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "unknown_robot"
    assert "robot_roster" in body["hint"]


def test_an_unknown_input_mode_is_a_400(open_client):
    response = open_client.post("/api/fleet/mode", json={"mode": "sideways"})
    assert response.status_code == 400
    assert response.json()["code"] == "bad_input_mode"


def test_an_unknown_robot_stream_is_a_404(open_client):
    assert open_client.get("/api/robot/dog9/stream.mjpeg").status_code == 404


def test_an_empty_body_is_not_an_error(open_client):
    """A bare `curl -XPOST` should still work; a stop needs no parameters."""
    assert open_client.post("/api/fleet/stop", content=b"").status_code == 200
    assert open_client.post("/api/fleet/estop", content=b"").status_code == 200


def test_video_can_be_toggled_per_robot(open_client):
    payload = open_client.post("/api/robot/dog2/video", json={"enabled": False}).json()
    assert payload["video_enabled"]["dog2"] is False


def test_faults_reach_the_console_status_and_reconcile(open_client, tmp_path):
    """The console shows the *reconciled* set, not the raw event history.

    A fault cleared in a snapshot must disappear; a fault re-sent in a
    snapshot must not look newly added. The raw history cannot express either.
    """
    from unitree_robot_agent.core.faults import FaultStore

    # Drive the store the way the robot would. `at=` pins *our* clock, because
    # that is what first_seen/last_seen record: the robot's own timestamp is
    # discarded, since its clock has no NTP guarantee.
    store = FaultStore()
    store.apply("errors", [[1.0, 300, 1], [1.0, 200, 4]], at=100.0)
    store.apply("rm_error", [2.0, 200, 4], at=101.0)
    assert [f.key for f in store.active()] == ["300:1"]

    store.apply("errors", [[3.0, 300, 1], [3.0, 1000, 1]], at=102.0)
    keys = [f.key for f in store.active()]
    assert keys == ["300:1", "1000:1"]
    assert store.active()[0].first_seen == 100.0  # not re-stamped at 102.0
    assert store.active()[0].last_seen == 102.0

    labels = {f.key: f.label for f in store.active()}
    assert labels["300:1"] == "Overcurrent"
    assert labels["1000:1"] == "code 0x1"  # source is its own field


def test_status_payload_carries_fault_fields(open_client):
    """The UI reads these three keys per robot; if they vanish the panel
    silently shows nothing, which is worse than showing an error."""
    payload = open_client.get("/api/fleet/status").json()
    for robot in payload["robots"]:
        assert "faults" in robot
        assert "faults_authoritative" in robot
        assert "faults_revision" in robot
        # Nothing has arrived from a robot yet, so we must not claim "clear".
        assert robot["faults_authoritative"] is False


def test_an_injected_fault_reaches_the_console_status(tmp_path):
    """The whole path: robot notification -> fault store -> status -> UI.

    Faults are per-robot, so this also pins that dog1's fault does not show up
    on dog2 — the failure mode a shared store would produce, and the reason
    the store lives on the Runtime rather than in the console.
    """
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
    # Motor 1 overcurrent, reported as a per-motor source.
    fleet.get("dog1").backend.inject_fault("errors", 301, 1)

    config = ConsoleConfig(host="127.0.0.1", token="", deadman_s=0.2)
    service = ConsoleService(fleet, config=config)
    with TestClient(create_app(service, config)) as client:
        payload = client.get("/api/fleet/status").json()

    by_id = {r["robot_id"]: r for r in payload["robots"]}

    dog1 = by_id["dog1"]
    assert dog1["faults_authoritative"] is True
    assert len(dog1["faults"]) == 1
    fault = dog1["faults"][0]
    assert fault["label"] == "Overcurrent"
    assert fault["source_label"] == "Motor"
    assert fault["motor"] == 1          # named by joint, not by source number
    assert fault["code_hex"] == "0x1"

    dog2 = by_id["dog2"]
    assert dog2["faults"] == []
    assert dog2["faults_authoritative"] is False, "dog2 has sent no snapshot"


def test_the_console_javascript_renders_the_fault_fields(open_client):
    """Cheap guard: if the payload keys and the renderer drift apart the panel
    silently shows nothing, which reads as 'all clear'."""
    source = open_client.get("/static/console.js").text
    for field in ("faults", "faults_authoritative", "source_label", "motor", "code_hex"):
        assert field in source, f"console.js no longer reads {field}"


# ─── console tool bridge ──────────────────────────────────────────────


def test_the_console_exposes_an_allowlist_not_a_passthrough(open_client):
    """A generic "call any tool" endpoint reachable over HTTP would expose
    `robot_fetch_key` (which returns a credential) and the audit tail."""
    tools = open_client.get("/api/console/tools").json()["tools"]
    assert "robot_stand" in tools
    assert "robot_status" in tools
    for forbidden in ("robot_fetch_key", "robot_auth_status", "robot_doctor",
                      "robot_discover", "robot_audit_tail", "robot_snapshot",
                      "robot_move", "robot_joystick", "robot_turn"):
        assert forbidden not in tools, f"{forbidden} must not be console-reachable"


def test_an_off_allowlist_tool_is_refused(open_client):
    response = open_client.post(
        "/api/robot/dog1/tool", json={"tool": "robot_fetch_key", "args": {}}
    )
    assert response.status_code == 400
    assert response.json()["code"] == "tool_not_allowed"


def test_a_tool_call_goes_through_the_same_guards_as_mcp(open_client):
    """Not a reimplementation: the console calls the tool function itself, so
    `confirm`, the dangerous-action gate and safe mode all still apply."""
    refused = open_client.post(
        "/api/robot/dog1/tool", json={"tool": "robot_stand", "args": {}}
    ).json()
    assert refused["ok"] is False
    assert refused["error"] == "confirm_required"

    accepted = open_client.post(
        "/api/robot/dog1/tool",
        json={"tool": "robot_stand", "args": {"confirm": True}},
    ).json()
    assert accepted["ok"] is True
    assert accepted["robot_id"] == "dog1"


def test_a_tool_call_acts_only_on_the_named_robot(open_client):
    open_client.post(
        "/api/robot/dog2/tool",
        json={"tool": "robot_stand", "args": {"confirm": True}},
    )
    payload = open_client.get("/api/fleet/status").json()
    connected = {r["robot_id"]: r["connected"] for r in payload["robots"]}
    assert connected["dog2"] is True
    assert connected["dog1"] is False


def test_an_unknown_robot_tool_call_is_a_404(open_client):
    response = open_client.post(
        "/api/robot/dog9/tool", json={"tool": "robot_stand", "args": {"confirm": True}}
    )
    assert response.status_code == 404


def test_the_js_offers_only_the_tools_the_server_advertises(open_client):
    source = open_client.get("/static/console.js").text
    assert "/api/console/tools" in source
    assert "/api/robot/" in source and "/tool" in source


# ─── connection control ───────────────────────────────────────────────


def test_connect_and_disconnect_are_explicit_operations(open_client):
    """Taking the link back after the official app held it, or handing it
    back deliberately, must not require issuing a movement command."""
    up = open_client.post("/api/robot/dog1/connect").json()
    assert up == {"robot_id": "dog1", "connected": True}

    down = open_client.post("/api/robot/dog1/disconnect").json()
    assert down["connected"] is False


def test_a_failed_connect_is_a_normal_answer_not_a_server_error(open_client):
    """The console needs to *show why* (busy, no key, unreachable)."""
    from unitree_robot_agent.errors import RobotBusy

    svc = _service_of(open_client)
    assert svc is not None, "the fixture should stash the service on the app"

    async def busy():
        raise RobotBusy("another client holds the slot")

    svc.fleet.get("dog2").session.ensure_connected = busy  # type: ignore[assignment]
    response = open_client.post("/api/robot/dog2/connect")

    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is False
    assert body["error"] == "robot_busy"
    assert body["hint"]


def test_connect_an_unknown_robot_is_a_404(open_client):
    assert open_client.post("/api/robot/dog9/connect").status_code == 404


def test_the_snapshot_tells_the_ui_what_it_may_offer(open_client):
    """`tools` and `watching` drive the UI, so they are part of the contract."""
    payload = open_client.get("/api/fleet").json()
    assert payload["watching"] == 0
    assert "robot_stand" in payload["tools"]
    assert payload["limits"]["lease_s"] > 0


def test_an_open_websocket_arms_the_operator_lease(open_client):
    """The lease is armed by a *watching browser*, not by the process being up.

    Counting watchers is what stops connected robots releasing themselves
    mid-session; if this counter never moved, the console would drop every
    link after `idle_disconnect_s` and the operator would see the video die
    and the next command reconnect.
    """
    service = _service_of(open_client)
    assert service.watching is False

    with open_client.websocket_connect("/ws") as socket:
        first = socket.receive_json()
        assert first["type"] == "status"
        assert service.watching is True
        assert open_client.get("/api/fleet").json()["watching"] == 1

    # Closing the socket must drop the socket claim. `watching` also honours a
    # recent HTTP status request (the polling fallback is a browser watching
    # too), so it stays true for the watch window — what must go is the client
    # count, and the claim must expire rather than pin the robots forever.
    for _ in range(20):
        if service._clients == 0:  # noqa: SLF001 - deliberate white-box
            break
        time.sleep(0.05)
    assert service._clients == 0  # noqa: SLF001

    service._last_watch_at -= 3600  # noqa: SLF001
    assert service.watching is False


# ─── the HTML/JS contract ─────────────────────────────────────────────


def test_every_element_the_javascript_reaches_for_exists_in_the_page(open_client):
    """Guards a class of bug no Python test can otherwise see.

    Renaming a `data-role` in the template and forgetting the JavaScript
    leaves the console silently half-broken — the panel just never updates —
    and nothing here would fail. This is cheap insurance against that.
    """
    html = open_client.get("/").text
    js = open_client.get("/static/console.js").text

    html_ids = set(re.findall(r'id="([^"]+)"', html))
    js_ids = set(re.findall(r"getElementById\(['\"]([^'\"]+)['\"]\)", js))
    # `toast` is created by the script itself.
    missing = sorted(js_ids - html_ids - {"toast"})
    assert not missing, f"console.js looks for elements that are not there: {missing}"

    html_roles = set(re.findall(r'data-role="([^"]+)"', html))
    js_roles = set(re.findall(r'data-role="([^"]+)"', js))
    missing_roles = sorted(js_roles - html_roles)
    assert not missing_roles, f"console.js queries data-role values that are not there: {missing_roles}"


def test_the_javascript_parses(open_client, tmp_path):
    """A syntax error in the bundle would render a blank console, and every
    other test here would still pass because they only fetch the file."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; syntax is only checked when available")

    source = tmp_path / "console.js"
    source.write_text(open_client.get("/static/console.js").text, encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(source)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_the_console_js_loads_the_gamepad_module_and_uses_it(open_client):
    """`gamepad.js` is dual-target: the browser loads it as a script and the
    tests `require()` it. Both halves have to stay wired."""
    page = open_client.get("/").text
    assert "/static/gamepad.js" in page
    assert page.index("/static/gamepad.js") < page.index("/static/console.js")

    assert open_client.get("/static/gamepad.js").status_code == 200
    source = open_client.get("/static/console.js").text
    for used in ("UnitreeGamepad", "gamepad:0", "getGamepads", "frameFor"):
        assert used in source, f"console.js no longer uses {used}"


# ─── the audit trail ──────────────────────────────────────────────────


def test_the_audit_endpoint_returns_this_robots_records(open_client):
    """Only dog1 is exercised, so only dog1's trail may show anything."""
    # Drive through the console's own tool route — the same path the UI takes.
    open_client.post("/api/robot/dog1/tool", json={"tool": "robot_status", "args": {}})

    payload = open_client.get("/api/robot/dog1/audit?limit=10").json()
    assert payload["robot_id"] == "dog1"
    assert payload["count"] >= 1
    assert any(r["tool"] == "robot_status" for r in payload["records"])

    other = open_client.get("/api/robot/dog2/audit?limit=10").json()
    assert other["robot_id"] == "dog2"
    assert not any(r["tool"] == "robot_status" for r in other["records"])


def test_the_audit_endpoint_never_returns_key_material(open_client):
    """`AuditLog` redacts on write; this is the end-to-end check that it still
    does, because "already redacted" is a claim that quietly stops being true."""
    service = _service_of(open_client)
    service.fleet.get("dog1").audit.record(
        "robot_fetch_key",
        args={"aes_128_key": "deadbeef" * 4, "password": "hunter2"},
        ok=True,
    )

    body = open_client.get("/api/robot/dog1/audit?limit=20").text
    assert "deadbeef" not in body
    assert "hunter2" not in body


def test_the_audit_limit_is_clamped_and_an_unknown_robot_is_a_404(open_client):
    open_client.post("/api/robot/dog1/tool", json={"tool": "robot_status", "args": {}})

    # Nonsense and negative limits fall back rather than erroring.
    for query in ("limit=0", "limit=-5", "limit=junk"):
        assert open_client.get(f"/api/robot/dog1/audit?{query}").status_code == 200

    assert open_client.get("/api/robot/dog9/audit").status_code == 404


def test_the_audit_endpoint_is_authenticated(locked_client):
    assert locked_client.get("/api/robot/dog1/audit").status_code == 401


def test_the_status_payload_carries_telemetry(open_client):
    """The UI reads `telemetry[robot_id].battery` and `.sport_state`; if the
    key vanishes the panels silently go back to showing "—"."""
    payload = open_client.get("/api/fleet/status").json()
    assert "telemetry" in payload
    assert isinstance(payload["telemetry"], dict)


def test_the_console_js_reads_the_telemetry_it_is_given(open_client):
    source = open_client.get("/static/console.js").text
    for field in ("state.telemetry", "soc_percent", "gait_type", "posture"):
        assert field in source, f"console.js no longer reads {field}"


def test_the_status_payload_carries_video_health(open_client):
    """The UI reads `video[robot_id].state`; without it a robot that never
    produces a frame shows a blank box with the link pill saying 在线."""
    payload = open_client.get("/api/fleet/status").json()
    assert "video" in payload
    for robot_id, health in payload["video"].items():
        assert "state" in health and "age_s" in health
        assert health["state"] in ("ok", "starting", "stalled", "offline", "disabled")


def test_the_console_js_explains_a_missing_picture(open_client):
    source = open_client.get("/static/console.js").text
    for marker in ("videoHealth", "stalled", "无画面"):
        assert marker in source, f"console.js no longer explains {marker}"


# ─── discoverability ──────────────────────────────────────────────────


def test_the_readme_documents_the_console():
    """The work is only delivered if someone can find it.

    The README carried the MCP section but nothing at all about the console or
    the roster, so the console was undiscoverable from the repo's front door.
    """
    from pathlib import Path

    readme = Path(__file__).resolve().parent.parent / "README.md"
    text = readme.read_text(encoding="utf-8")

    assert "unitree-fleet-console" in text
    assert "UNITREE_AGENT_ROSTER" in text
    assert "--preflight" in text
    # The fields a reader must not have to guess.
    for field in ('"id"', '"ip"', '"sn"', '"role"'):
        assert field in text, f"README roster example is missing {field}"
    # And the two things that behave unlike a naive reader would expect.
    assert "not_stopped" in text
    assert "Latch" in text or "latches" in text


def test_the_skill_documents_the_fleet_surface():
    """The skill is the other entry point an agent may load instead of the
    server's own instructions. Without this it would omit `robot_id` — which
    means acting on the roster default, i.e. possibly the wrong dog."""
    from pathlib import Path

    skill = (
        Path(__file__).resolve().parent.parent
        / "skills" / "unitree-robot-control" / "SKILL.md"
    )
    text = skill.read_text(encoding="utf-8")

    for tool in ("robot_roster", "robot_fleet_status", "robot_stop_all",
                 "robot_preflight", "robot_emergency_stop_all"):
        assert tool in text, f"SKILL.md does not document {tool}"
    assert "robot_id" in text
    assert "not_stopped" in text
    # The trigger description has to mention multi-robot, or the skill will not
    # load when someone asks about two dogs.
    assert "双狗" in text or "多台机器人" in text
