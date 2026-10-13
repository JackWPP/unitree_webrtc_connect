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


# ─── readiness: the decisive questions ────────────────────────────────


def _mock_roster(tmp_path):
    from unitree_robot_agent.fleet import Roster

    return Roster.from_mapping({
        "state_dir": str(tmp_path / "state"),
        "default": "dog1",
        "robots": [
            {"id": "dog1", "model": "Go2", "ip": "192.168.31.245", "sn": "SN-ONE"},
            {"id": "dog2", "model": "Go2", "ip": "192.168.31.246", "sn": "SN-TWO"},
        ],
    })


async def test_readiness_answers_the_questions_that_change_what_to_do(tmp_path):
    """A report that only lists versions and topics leaves the operator to work
    out what it means. Each of these answers one question whose answer changes
    what the console can do."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from session_report import readiness

    from unitree_robot_agent.fleet import FleetManager
    from unitree_fleet_web import ConsoleConfig, ConsoleService

    roster = _mock_roster(tmp_path)
    service = ConsoleService(
        FleetManager(roster, backend="mock", audit_to_stderr=False),
        config=ConsoleConfig(deadman_s=2.0),
    )
    try:
        for spec in roster.robots:
            await service.connect(spec.robot_id)
        checks = await readiness(service, roster)
    finally:
        await service.aclose()

    by_id = {check["id"]: check for check in checks}
    # The odometry question is the one that decides whether `robot_walk` is a
    # measurement or a guess, so its absence would be the report failing at its
    # only job.
    assert "odometry" in by_id, sorted(by_id)
    assert by_id["odometry"]["verdict"] == "yes", by_id["odometry"]
    assert "imu" in by_id

    # The cross-robot check needs both dogs and is the hardware form of the
    # routing bug.
    assert "two_robots" in by_id, sorted(by_id)
    assert by_id["two_robots"]["verdict"] == "yes", by_id["two_robots"]


async def test_the_two_robot_check_does_not_cry_wolf_on_two_dogs_at_rest(tmp_path):
    """**The first version of this check reported a failure on a healthy pair.**

    It compared positions, and two mock dogs both sit at `(0.0, 0.0)` — which is
    *correct*: each odometry has its own origin, so two dogs at their own
    starting points read the same numbers. A readiness report that flags the
    normal case is worse than one that says nothing, because somebody will
    spend a session chasing it.

    What has to differ is which robot the two calls reached, so the check
    compares identity.
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from session_report import readiness

    from unitree_robot_agent.fleet import FleetManager
    from unitree_fleet_web import ConsoleConfig, ConsoleService

    roster = _mock_roster(tmp_path)
    service = ConsoleService(
        FleetManager(roster, backend="mock", audit_to_stderr=False),
        config=ConsoleConfig(deadman_s=2.0),
    )
    try:
        for spec in roster.robots:
            await service.connect(spec.robot_id)
        positions = {
            rid: await service.fleet.get(rid).motion.read_position()
            for rid in roster.ids
        }
        # The precondition that made the old version wrong: identical positions,
        # healthy pair.
        assert len(set(str(v) for v in positions.values())) == 1, positions

        checks = await readiness(service, roster)
    finally:
        await service.aclose()

    two = {c["id"]: c for c in checks}["two_robots"]
    assert two["verdict"] == "yes", (
        f"two dogs at their own origins were reported as a problem: {two}"
    )


async def test_every_per_robot_check_says_which_robot(tmp_path):
    """With two dogs the same question appears twice. An unlabelled list of
    identical headings is a report nobody can act on."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from session_report import readiness

    from unitree_robot_agent.fleet import FleetManager
    from unitree_fleet_web import ConsoleConfig, ConsoleService

    roster = _mock_roster(tmp_path)
    service = ConsoleService(
        FleetManager(roster, backend="mock", audit_to_stderr=False),
        config=ConsoleConfig(deadman_s=2.0),
    )
    try:
        for spec in roster.robots:
            await service.connect(spec.robot_id)
        checks = await readiness(service, roster)
    finally:
        await service.aclose()

    for check in checks:
        if check["id"] == "two_robots":
            continue  # it is about the pair, not one robot
        assert check.get("robot_id"), check
    # ...and both dogs are represented.
    assert {c.get("robot_id") for c in checks} >= {"dog1", "dog2"}


async def test_readiness_is_all_read_only(tmp_path):
    """**Nothing here commands the robot.** The first thing an operator does
    with a strange dog should not be to make it walk — so a readiness run must
    leave the position and the skill lock exactly as it found them."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from session_report import readiness

    from unitree_robot_agent.fleet import FleetManager
    from unitree_fleet_web import ConsoleConfig, ConsoleService

    roster = _mock_roster(tmp_path)
    fleet = FleetManager(roster, backend="mock", audit_to_stderr=False)
    service = ConsoleService(fleet, config=ConsoleConfig(deadman_s=2.0))
    try:
        for spec in roster.robots:
            await service.connect(spec.robot_id)
        before = {
            rid: await fleet.get(rid).motion.read_position() for rid in roster.ids
        }
        await readiness(service, roster)
        after = {
            rid: await fleet.get(rid).motion.read_position() for rid in roster.ids
        }
        assert before == after, f"readiness moved something: {before} -> {after}"
        for rid in roster.ids:
            assert fleet.get(rid).motion.skill_owner == ""
    finally:
        await service.aclose()


def test_the_firmware_check_does_not_claim_to_know_the_firmware(tmp_path):
    """It was `no` when no key was cached, which reads as "this dog does not
    need one" — **a claim about the robot made from a claim about a file.**
    Whether the handshake wants `data2=3` is only knowable by trying it."""
    import inspect
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import session_report

    source = inspect.getsource(session_report.readiness)
    firmware = source[source.index('"firmware"'):]
    firmware = firmware[:firmware.index("robot_id=robot_id")]
    assert '"info"' in firmware, (
        "the firmware check is asserting a verdict it cannot know from a config file"
    )
