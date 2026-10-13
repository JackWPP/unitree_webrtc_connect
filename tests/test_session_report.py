"""The one command that gathers what a hardware session needs to report.

It exists because the runbook asks for six things from four tools while the
operator is standing next to a robot that wants to go back in its box. Asking
for six commands means getting four, or getting them with a typo in one, or
getting them an hour later from memory.

The test that matters most is the last one. The whole point is that the output
can be pasted somewhere whole, and **a pasted report is exactly where a leaked
AES key ends up**.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: A key of the shape the real ones are: 32 hex characters.
SECRET = "0123456789abcdef0123456789abcdef"


@pytest.fixture(scope="module")
def report_module():
    spec = importlib.util.spec_from_file_location(
        "session_report", ROOT / "scripts" / "session_report.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["session_report"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def roster_file(tmp_path):
    path = tmp_path / "dogs.json"
    path.write_text(
        json.dumps({
            "default": "dog1",
            "state_dir": str(tmp_path / "state"),
            "robots": [
                {"id": "dog1", "model": "Go2", "ip": "192.168.31.188",
                 "sn": "B42N6000Q3UC1M8C", "key": SECRET},
            ],
        }),
        encoding="utf-8",
    )
    return path


def test_the_environment_block_names_what_decides_a_link(report_module):
    """A link that works on one machine and not another is usually this list
    rather than the robot, so it has to be in the report."""
    env = report_module.environment()
    assert env["python"]
    for package in ("aiortc", "aioice"):
        assert package in env["packages"]


def test_the_report_never_contains_the_key(report_module, roster_file, monkeypatch):
    """**The reason this file exists.**

    The report is meant to be pasted whole into a chat. `roster.redacted()` is
    used rather than a filter written here, because a hand-rolled filter is the
    one that leaks — and the leak would be discovered by reading a chat log.
    """
    from unitree_robot_agent.fleet import load_roster

    roster = load_roster(str(roster_file))
    assert roster.robots[0].aes_128_key == SECRET, "the fixture is not testing anything"

    report = {"environment": report_module.environment(), "roster": roster.redacted()}
    text = report_module.render(report)

    assert SECRET not in text
    # ...and not merely absent because nothing was rendered: the robot is there.
    assert "dog1" in text
    assert "B42N6000Q3UC1M8C" in text
    assert "has_key" in text
    # A bare 32-hex string is what a key looks like; none should survive.
    assert not re.findall(r"\b[0-9a-fA-F]{32}\b", text)


def test_the_rendered_report_survives_a_total_probe_failure(report_module):
    """A report that crashes while printing a failure is worse than no report —
    and it would crash at exactly the moment it was needed."""
    text = report_module.render({
        "environment": report_module.environment(),
        "roster": {"robots": []},
        "preflight": {"error": "boom"},
        "probe": {"error": "AgentError: no route"},
        "live_sample": {"error": "AgentError: no route"},
    })
    assert "AgentError: no route" in text


def test_the_probe_is_not_reimplemented(report_module):
    """Two copies of a topic list is how two tools end up disagreeing about what
    a firmware publishes, and the disagreement would be found on a robot, by
    somebody who then cannot trust either answer."""
    source = (ROOT / "scripts" / "session_report.py").read_text(encoding="utf-8")
    assert "probe_topics" in source
    assert "rt/utlidar" not in source, "the topic list has been copied in"
    assert "rt/lf/lowstate" not in source, "the topic list has been copied in"


def test_the_probe_exposes_data_as_well_as_printing_it():
    """Refactored out of a function that returned an exit code: the report needs
    the rows, and printing them is one of two uses."""
    source = (ROOT / "scripts" / "probe_webrtc_topics.py").read_text(encoding="utf-8")
    assert "async def probe_topics(" in source, "the data function is gone"
    assert "def render_probe(" in source, "printing was not separated from gathering"
    assert "return {\n        \"ip\": ip," in source


@pytest.mark.parametrize("backend", ["mock"])
def test_it_runs_end_to_end_without_a_robot(report_module, roster_file, backend, capsys):
    """`--backend mock` is the reason this can be maintained between sessions.
    A report tool that can only be tested on a robot is a report tool nobody
    knows is broken until they need it."""
    code = _run(report_module, ["--roster", str(roster_file), "--backend", backend,
                                "--timeout", "1", "--json"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    payload = json.loads(captured.out)
    assert payload["probe"]["dog1"]["total"] == 13
    assert payload["probe"]["dog1"]["delivered"] >= 1


def _run(module, argv):
    original = sys.argv
    sys.argv = ["session_report.py", *argv]
    try:
        return module.main()
    finally:
        sys.argv = original
