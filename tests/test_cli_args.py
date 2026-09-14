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
