"""The console's HTTP surface: auth, routing, and error shapes.

The auth tests matter more than they look. A console that can drive two
robots is a control surface, and the previous attempt (``wpp``'s
``web_server.py``) shipped twenty-three routes with no authentication at all,
bound to ``0.0.0.0``. These tests exist so that cannot come back quietly.
"""

from __future__ import annotations

import re

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
    return create_app(service, config)


@pytest.fixture
def open_client(tmp_path):
    """No token: the loopback-only case."""
    with TestClient(make_app(tmp_path)) as client:
        yield client


@pytest.fixture
def locked_client(tmp_path):
    with TestClient(make_app(tmp_path, token=TOKEN)) as client:
        yield client


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
