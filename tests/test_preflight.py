"""Preflight: telling the operator what the handshake will need, before it runs.

Both of the expensive failures in this project were diagnostic rather than
mechanical — a `data2=3` robot with no key fails inside RSA parsing and reads
like a network problem, and a second Go2 handed the first one's key fails a
GCM tag check that reads like a hardware fault. So this is tested against the
*real* crypto: the fake robot builds actual `data2=2` and `data2=3` payloads
with the same library the driver uses, and the verdicts are asserted on what
those produce.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from unitree_robot_agent.fleet import preflight as preflight_module  # the module
from unitree_robot_agent.fleet.preflight import probe_robot, summarise
from unitree_robot_agent.fleet.roster import Roster
from unitree_robot_agent.fleet.spec import RobotSpec

GOOD_KEY = "0123456789abcdef0123456789abcdef"
OTHER_KEY = "fedcba9876543210fedcba9876543210"

#: 10 chars of body + 10 chars of path token, so `_calc_local_path_ending`
#: has something well-formed to chew on.
DATA1 = "PUBKEYPEMBODY" + "AABBCCDDEE"


def encrypt_like_the_robot(plaintext: str, key: bytes) -> str:
    """Build a `data1` the driver's decrypt path will accept.

    The layout the driver expects is `ciphertext || nonce || tag`, which is
    not what AESGCM.encrypt returns, so it is reassembled here.
    """
    nonce = os.urandom(12)
    sealed = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    raw = sealed[:-16] + nonce + sealed[-16:]
    return base64.b64encode(raw).decode()


class FakeRobot(BaseHTTPRequestHandler):
    """Answers POST /con_notify the way a real robot does."""

    payload: str = ""
    status: int = 200

    def do_POST(self):  # noqa: N802 - http.server's naming
        body = base64.b64encode(self.payload.encode()).decode()
        data = body.encode()
        self.send_response(self.status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # silence
        return


def start_server(payload: str, *, status: int = 200):
    handler = type("Handler", (FakeRobot,), {"payload": payload, "status": status})
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


@pytest.fixture
def robot_port(monkeypatch):
    """Point the probe at a throwaway port so tests can bind freely."""
    servers = []

    def start(payload: str, *, status: int = 200) -> int:
        server, port = start_server(payload, status=status)
        servers.append(server)
        monkeypatch.setattr(preflight_module, "CON_NOTIFY_PORT", port)
        monkeypatch.setattr(preflight_module, "LEGACY_OFFER_PORT", 1)  # nothing there
        return port

    yield start
    for server in servers:
        server.shutdown()


def payload_for(data2, plaintext: str = DATA1) -> str:
    if data2 == 2:
        from unitree_webrtc_connect.unitree_auth import _LEGACY_GCM_KEY

        return json.dumps({"data1": encrypt_like_the_robot(plaintext, _LEGACY_GCM_KEY), "data2": 2})
    if data2 == 3:
        return json.dumps(
            {"data1": encrypt_like_the_robot(plaintext, bytes.fromhex(GOOD_KEY)), "data2": 3}
        )
    return json.dumps({"data1": plaintext, "data2": data2})


def spec_for(port: int, *, sn: str = "B42D2000AAAA") -> RobotSpec:
    return RobotSpec(robot_id="dog1", model="Go2", ip="127.0.0.1", serial_number=sn)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


# ─── the verdicts ─────────────────────────────────────────────────────


async def test_a_legacy_key_robot_is_ready_with_no_configuration(robot_port):
    robot_port(payload_for(2))
    result = await probe_robot(spec_for(9991))

    assert result.reachable is True
    assert result.signaling == "con_notify"
    assert result.data2 == 2
    assert result.key_required is False
    assert result.verdict == "ok"


async def test_a_per_device_robot_without_a_key_says_so(robot_port):
    """The exact case that used to produce a traceback reading like a network
    problem."""
    robot_port(payload_for(3))
    result = await probe_robot(spec_for(9991), aes_key="")

    assert result.data2 == 3
    assert result.key_required is True
    assert result.key_present is False
    assert result.verdict == "no_key"
    assert "unitree-fetch-aes-key" in result.hint


async def test_a_per_device_robot_with_the_right_key_is_ready(robot_port):
    robot_port(payload_for(3))
    result = await probe_robot(spec_for(9991), aes_key=GOOD_KEY)

    assert result.key_required is True
    assert result.key_present is True
    assert result.key_accepted is True
    assert result.verdict == "ok"


async def test_a_per_device_robot_with_another_robots_key_is_named(robot_port):
    """The silent mis-assignment: the key belongs to a different robot and the
    only symptom is a GCM tag failure."""
    robot_port(payload_for(3))
    result = await probe_robot(spec_for(9991), aes_key=OTHER_KEY)

    assert result.key_present is True
    assert result.key_accepted is False
    assert result.verdict == "key_rejected"
    assert "different robot" in result.hint


async def test_a_plaintext_robot_needs_nothing(robot_port):
    robot_port(payload_for(1))
    result = await probe_robot(spec_for(9991))
    assert result.verdict == "ok"
    assert result.key_required is False


async def test_an_unreachable_robot_is_reported_as_such(monkeypatch):
    # Point at ports nothing is listening on.
    dead_a, dead_b = free_port(), free_port()
    monkeypatch.setattr(preflight_module, "CON_NOTIFY_PORT", dead_a)
    monkeypatch.setattr(preflight_module, "LEGACY_OFFER_PORT", dead_b)

    result = await probe_robot(RobotSpec(robot_id="dog1", model="Go2", ip="127.0.0.1"))

    assert result.reachable is False
    assert result.port is None
    assert result.verdict == "unreachable"
    # The message names the ports actually tried, which is what an operator
    # needs to check their firewall against.
    assert str(dead_a) in result.detail and str(dead_b) in result.detail
    assert "powered on" in result.hint


async def test_a_robot_with_no_address_is_reported_clearly():
    result = await probe_robot(RobotSpec(robot_id="dog1", model="Go2", connection="ap"))
    assert result.verdict == "no_address"
    assert "no IP" in result.detail


async def test_an_unreadable_reply_does_not_raise(robot_port):
    robot_port("this is not base64 json")
    result = await probe_robot(spec_for(9991))

    assert result.reachable is True
    assert result.verdict == "probe_failed"
    assert "base64" in result.detail


async def test_an_http_error_does_not_raise(robot_port):
    robot_port(payload_for(2), status=500)
    result = await probe_robot(spec_for(9991))
    assert result.verdict == "probe_failed"


# ─── the summary ──────────────────────────────────────────────────────


def test_the_summary_names_what_is_blocked():
    from unitree_robot_agent.fleet.preflight import PreflightResult

    results = [
        PreflightResult(robot_id="dog1", model="Go2", target="a", verdict="ok"),
        PreflightResult(robot_id="dog2", model="Go2", target="b", verdict="no_key"),
    ]
    summary = summarise(results)

    assert summary["complete"] is False
    assert summary["ready"] == ["dog1"]
    assert summary["blocked"] == ["dog2"]
    assert "not claimed" in summary["note"]


def test_the_summary_is_complete_when_everything_is_ready():
    from unitree_robot_agent.fleet.preflight import PreflightResult

    summary = summarise(
        [PreflightResult(robot_id="dog1", model="Go2", target="a", verdict="legacy")]
    )
    assert summary["complete"] is True


def test_nothing_in_a_result_exposes_the_key():
    from unitree_robot_agent.fleet.preflight import PreflightResult

    result = PreflightResult(
        robot_id="dog1", model="Go2", target="a", key_present=True, verdict="ok"
    )
    assert GOOD_KEY not in json.dumps(result.as_dict())


# ─── the CLI path ─────────────────────────────────────────────────────


def test_the_cli_reports_ready_and_returns_zero(robot_port, capsys):
    robot_port(payload_for(2))
    from unitree_fleet_web.cli import _run_preflight

    roster = Roster.from_mapping(
        {"robots": [{"id": "dog1", "model": "Go2", "ip": "127.0.0.1"}]}
    )
    assert _run_preflight(roster) == 0

    out = capsys.readouterr().out
    assert "[OK  ] dog1" in out
    assert "1/1 ready" in out
    assert "not claimed" in out


def test_the_cli_returns_nonzero_and_explains_when_blocked(robot_port, capsys):
    robot_port(payload_for(3))
    from unitree_fleet_web.cli import _run_preflight

    roster = Roster.from_mapping(
        {"robots": [{"id": "dog1", "model": "Go2", "ip": "127.0.0.1", "sn": "B42"}]}
    )
    assert _run_preflight(roster) == 1

    out = capsys.readouterr().out
    assert "no_key" in out
    assert "unitree-fetch-aes-key" in out
    assert "blocked: dog1" in out


def test_preflight_needs_no_backend_and_claims_no_robot(robot_port):
    """The property that makes it safe to run while the official app is
    connected: it must never post an SDP, so the robot's single client slot
    stays free."""
    port = robot_port(payload_for(3))
    import asyncio

    from unitree_robot_agent.fleet import run_preflight
    from unitree_robot_agent.fleet.roster import Roster

    roster = Roster.from_mapping(
        {"robots": [{"id": "dog1", "model": "Go2", "ip": "127.0.0.1"}]}
    )
    results = asyncio.run(run_preflight(roster))

    assert len(results) == 1
    assert results[0].signaling == "con_notify"
    # Only /con_notify was ever hit: no `con_ing_<path>` request, which is the
    # one that would claim the connection.
    assert results[0].verdict in ("no_key", "ok")
