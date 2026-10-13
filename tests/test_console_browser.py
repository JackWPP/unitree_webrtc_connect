"""Render the console in a real browser and look at the DOM.

Everything else in the suite checks the console's *inputs and outputs* — the
JSON it serves, the bytes it streams, the text of its assets. None of that
would notice if the page rendered nothing, or rendered but never updated.

This does, and it is not hypothetical: the first time this ran, the WebSocket
had closed and every live field — link pills, battery, faults, the fleet
summary — sat on its initial placeholder text forever. There was no fallback.
The assertion on the fleet summary below is the guard for that.

Skipped when no Chrome/Chromium is available; the rest of the suite still
covers the API.
"""

from __future__ import annotations

import html
import json
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from unitree_robot_agent.fleet import FleetManager, Roster
from unitree_fleet_web.app import create_app
from unitree_fleet_web.config import ConsoleConfig
from unitree_fleet_web.service import ConsoleService

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
]

RENDER_BUDGET_MS = 6000


def _chrome() -> str:
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    for name in ("google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return ""


CHROME = _chrome()
pytestmark = pytest.mark.skipif(not CHROME, reason="no Chrome/Chromium available")


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def console_url(tmp_path):
    """Serve a two-dog mock console on a real port for the browser to reach."""
    import uvicorn

    roster = Roster.from_mapping(
        {
            "state_dir": str(tmp_path / "state"),
            "default": "dog1",
            "robots": [
                {"id": "dog1", "model": "Go2", "ip": "192.168.31.245",
                 "sn": "B42D2000AAAA", "role": "lead"},
                {"id": "dog2", "model": "Go2", "ip": "192.168.31.246",
                 "sn": "B42D2000BBBB", "role": "wing"},
            ],
        }
    )
    fleet = FleetManager(roster, backend="mock", audit_to_stderr=False)
    config = ConsoleConfig(host="127.0.0.1", port=_free_port(), deadman_s=0.5)
    service = ConsoleService(fleet, config=config)
    app = create_app(service, config)

    server = uvicorn.Server(
        uvicorn.Config(app, host=config.host, port=config.port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    url = f"http://{config.host}:{config.port}/"
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "the console did not come up"

    yield url

    server.should_exit = True
    thread.join(timeout=10)


def render(url: str) -> str:
    result = subprocess.run(
        [
            CHROME,
            "--headless",
            "--disable-gpu",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            f"--virtual-time-budget={RENDER_BUDGET_MS}",
            "--dump-dom",
            url,
        ],
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return result.stdout


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    """One render, shared by the assertions below — Chrome is not cheap."""
    import uvicorn

    base = tmp_path_factory.mktemp("browser")
    roster = Roster.from_mapping(
        {
            "state_dir": str(base / "state"),
            "default": "dog1",
            "robots": [
                {"id": "dog1", "model": "Go2", "ip": "192.168.31.245",
                 "sn": "B42D2000AAAA", "role": "lead"},
                {"id": "dog2", "model": "Go2", "ip": "192.168.31.246",
                 "sn": "B42D2000BBBB", "role": "wing"},
            ],
        }
    )
    fleet = FleetManager(roster, backend="mock", audit_to_stderr=False)
    config = ConsoleConfig(host="127.0.0.1", port=_free_port(), deadman_s=0.5)
    service = ConsoleService(fleet, config=config)
    app = create_app(service, config)

    server = uvicorn.Server(
        uvicorn.Config(app, host=config.host, port=config.port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started

    try:
        yield render(f"http://{config.host}:{config.port}/")
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def pen_block(doc: str, robot_id: str) -> str:
    """The rendered markup for one pen, excluding the inert <template>."""
    marker = f'data-robot="{robot_id}"'
    assert marker in doc, f"{robot_id} was not rendered at all"
    return doc.split(marker, 1)[1].split("</section>", 1)[0]


def role_text(block: str, role: str):
    match = re.search(rf'data-role="{role}"[^>]*>([^<]*)<', block)
    return html.unescape(match.group(1)).strip() if match else None


# ─── the page renders ─────────────────────────────────────────────────


def test_both_dogs_are_rendered(rendered):
    """The objective's first clause, checked in a browser rather than assumed."""
    pens = re.findall(r'<section class="pen[^"]*" data-robot="([^"]+)"', rendered)
    assert pens == ["dog1", "dog2"]


def test_each_pen_shows_its_identity(rendered):
    for robot_id, role, target in [
        ("dog1", "lead", "B42D2000AAAA"),
        ("dog2", "wing", "B42D2000BBBB"),
    ]:
        block = pen_block(rendered, robot_id)
        assert role_text(block, "name") == robot_id
        assert role_text(block, "target") == target
        assert role in block


def test_each_pen_gets_its_own_video_stream(rendered):
    for robot_id in ("dog1", "dog2"):
        block = pen_block(rendered, robot_id)
        assert f"/api/robot/{robot_id}/stream.mjpeg" in block


def test_the_posture_buttons_were_built_from_the_server_tool_list(rendered):
    """Proves the whole chain ran: page → /api/console/tools → tool bridge →
    站立/趴下 buttons, per dog."""
    for robot_id in ("dog1", "dog2"):
        block = pen_block(rendered, robot_id)
        assert "站立" in block
        assert "趴下" in block


# ─── the regression ───────────────────────────────────────────────────


def test_live_status_reached_the_page(rendered):
    """The guard for the bug this test was written after.

    With the WebSocket closed and no fallback, every live field stayed on its
    initial placeholder: the summary read 连接中… forever and the link pills
    read —. Polling is the fallback, so a reachable API is always reflected.
    """
    summary = re.search(r'id="fleet-summary"[^>]*>([^<]*)<', rendered)
    assert summary, "no fleet summary element"
    assert summary.group(1).strip() == "2/2 在线", (
        "live status never reached the page — is the polling fallback gone?"
    )


def test_the_link_pills_are_no_longer_placeholders(rendered):
    for robot_id in ("dog1", "dog2"):
        block = pen_block(rendered, robot_id)
        assert role_text(block, "link") in ("在线", "连接中…"), (
            f"{robot_id} still shows the initial placeholder"
        )


def test_the_fault_panel_says_it_has_not_heard_yet(rendered):
    """An empty fault list is only reassuring once a snapshot has arrived, and
    the mock has not sent one. Saying so beats implying 'all clear'."""
    for robot_id in ("dog1", "dog2"):
        block = pen_block(rendered, robot_id)
        assert "尚未收到完整故障快照" in block


def test_the_template_was_not_rendered_as_a_pen(rendered):
    """The pen template must stay inert.

    Note the wrinkle this test exists around: `--dump-dom` serialises
    `<template>` content, and normalises the template's valueless `data-robot`
    to `data-robot=""`. So "how many pen sections are in the dump" is 3 while
    "how many dogs were rendered" is 2 — an empty id is the template, and any
    future change that paints from it would show up as a third *named* pen.
    """
    all_sections = re.findall(r'<section class="pen[^"]*" data-robot="([^"]*)"', rendered)
    assert len(all_sections) == 3, "expected two pens plus the inert template"
    assert sorted(s for s in all_sections if s) == ["dog1", "dog2"]
    assert all_sections.count("") == 1, "exactly one un-named section: the template"
