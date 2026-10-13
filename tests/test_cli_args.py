"""CLI arguments: the common flags must work on either side of the subcommand.

A subparser parses into a fresh namespace and copies every attribute into the
parent's, so a subparser default silently overwrote a flag given *before* the
subcommand. `unitree-robot-mcp --safe-mode serve` started with read-only mode
off — a safety-relevant lie, so it is pinned here.
"""

from __future__ import annotations

from unitree_robot_agent.cli import _config_from_args, _build_parser


def config_for(argv):
    return _config_from_args(_build_parser().parse_args(argv))


def test_common_flags_before_the_subcommand_reach_the_config():
    cfg = config_for(
        ["--robot", "Go2", "--ip", "10.0.0.5", "--region", "cn", "--safe-mode", "doctor"]
    )
    assert (cfg.robot, cfg.ip, cfg.region, cfg.safe_mode) == ("Go2", "10.0.0.5", "cn", True)


def test_common_flags_after_the_subcommand_still_work():
    cfg = config_for(
        ["doctor", "--robot", "Go2", "--ip", "10.0.0.5", "--region", "cn", "--safe-mode"]
    )
    assert (cfg.robot, cfg.ip, cfg.region, cfg.safe_mode) == ("Go2", "10.0.0.5", "cn", True)


def test_absent_flags_fall_back_to_nothing():
    cfg = config_for(["doctor"])
    assert cfg.ip is None and cfg.safe_mode is False


def test_suppressed_flags_are_absent_rather_than_missing_attributes():
    """`--robot` and friends use `default=argparse.SUPPRESS` on purpose.

    The consequence is that the attribute does not exist when the flag is
    omitted, so any reader must use `getattr`. `_selftest` read `args.robot`
    directly, which made the documented `unitree-robot-mcp selftest` — the
    command the README and the manual install steps both give — die with an
    AttributeError. Only the installer's `--robot R1` form worked, which is
    why it went unnoticed.
    """
    for argv in (["selftest"], ["doctor"], ["serve"]):
        args = _build_parser().parse_args(argv)
        assert not hasattr(args, "robot"), f"{argv} unexpectedly gained args.robot"

    # And the config builder tolerates the missing attributes.
    cfg = config_for(["selftest"])
    assert cfg.robot in ("R1", "Go2")


async def test_the_offline_selftest_runs_without_any_flags():
    """The regression itself: no flags must not raise."""
    from unitree_robot_agent.cli import _selftest

    args = _build_parser().parse_args(["selftest"])
    assert await _selftest(args) == 0


async def test_the_offline_selftest_ignores_an_ambient_roster(tmp_path, monkeypatch):
    """Regression: a self test must not depend on the operator's environment.

    With `UNITREE_AGENT_ROSTER` pointing at a real fleet, the fleet layer
    became the tool context's default robot and the "offline" self test
    started driving real hardware — it fell from 20/20 to 9/20. Now the self
    test is hermetic: it ignores the ambient roster.
    """
    import json

    roster = tmp_path / "fleet.json"
    roster.write_text(
        json.dumps(
            {
                "state_dir": str(tmp_path / "state"),
                "default": "dog1",
                "robots": [
                    {"id": "dog1", "model": "Go2", "ip": "192.168.31.245"},
                    {"id": "dog2", "model": "Go2", "ip": "192.168.31.246"},
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("UNITREE_AGENT_ROSTER", str(roster))

    from unitree_robot_agent.cli import _selftest

    args = _build_parser().parse_args(["selftest"])
    assert await _selftest(args) == 0


async def test_explicitly_disabling_fleet_from_env_builds_a_single_robot_server():
    from unitree_robot_agent.runtime import create_runtime
    from unitree_robot_agent.server import build_server

    cfg = config_for(["doctor"])
    bundle = build_server(create_runtime(cfg), fleet_from_env=False)
    assert bundle.context.is_fleet is False
    assert bundle.context.fleet is None
