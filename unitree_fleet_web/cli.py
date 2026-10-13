"""``unitree-fleet-console`` — start the dual-dog web console.

Thin on purpose: parse arguments, build the fleet, hand it to uvicorn. All
the behaviour lives in :mod:`unitree_fleet_web.service` so it can be tested
without a socket.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import replace
from pathlib import Path
from typing import Optional

from unitree_robot_agent.errors import AgentError
from unitree_robot_agent.fleet import FleetManager, load_roster

from .app import create_app
from .config import ConsoleConfig
from .service import ConsoleService


def _parse_args(argv) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="unitree-fleet-console",
        description="Browser console for driving two Unitree robots at once.",
    )
    parser.add_argument("--host", default=None,
                        help="bind address (default: 127.0.0.1). Binding a "
                             "non-loopback address requires --token")
    parser.add_argument("--port", type=int, default=None,
                        help="port (default: 8765)")
    parser.add_argument("--token", default=None,
                        help="shared secret; required to bind beyond loopback. "
                             "Falls back to $UNITREE_CONSOLE_TOKEN")
    parser.add_argument("--roster", default=None,
                        help="roster JSON file or inline JSON (default: "
                             "$UNITREE_AGENT_ROSTER)")
    parser.add_argument("--backend", default=None,
                        choices=["auto", "webrtc", "mock"],
                        help="force a backend; 'mock' runs the whole console "
                             "with no robot attached")
    parser.add_argument("--state-dir", default=None,
                        help="override the roster's per-robot state directory")
    parser.add_argument("--no-ui", action="store_true",
                        help="serve the API only, not the built-in page")
    parser.add_argument("--preflight", action="store_true",
                        help="check each robot's reachability and what its "
                             "handshake will need, then exit. Read-only: it "
                             "never claims the robot's WebRTC client slot, so "
                             "it is safe to run while the official app is "
                             "connected")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args(argv)


def _roster_from_args(args):
    roster = load_roster(args.roster)
    if args.state_dir:
        roster = replace(roster, state_dir=Path(args.state_dir))
    return roster


def _config_from_args(args) -> ConsoleConfig:
    overrides = {}
    if args.host is not None:
        overrides["host"] = args.host
    if args.port is not None:
        overrides["port"] = args.port
    if args.token is not None:
        overrides["token"] = args.token
    config = ConsoleConfig.from_env(**overrides)
    config.serve_ui = not args.no_ui
    return config


def build(argv=None) -> tuple:
    """Build ``(config, service)`` without binding a socket. Used by tests."""
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    config = _config_from_args(args)
    config.check()

    fleet = FleetManager(_roster_from_args(args), backend=args.backend)
    return config, ConsoleService(fleet, config=config)


def main(argv=None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(
        level=getattr(logging, args.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    try:
        if args.preflight:
            # Deliberately before `build`: a preflight is a diagnostic, and
            # constructing a fleet and a 34-tool server to run one would make
            # the diagnostic the most complicated thing in the picture.
            return _run_preflight(_roster_from_args(args))
        config, service = build(argv)
    except AgentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if exc.hint:
            print(f"hint:  {exc.hint}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    app = create_app(service, config)
    robots = ", ".join(f"{r['robot_id']}({r['model']})" for r in service.snapshot()["fleet"]["robots"])
    print(f"fleet console on http://{config.host}:{config.port}  robots: {robots}")
    if not config.token:
        print("no token configured — reachable from this machine only")
    if config.host not in ("127.0.0.1", "::1", "localhost"):
        print(f"WARNING: bound to {config.host}; the token is your only protection")

    import uvicorn

    uvicorn.run(app, host=config.host, port=config.port, log_level=args.log_level.lower())
    return 0


def _run_preflight(roster) -> int:
    """Print what each robot will need, then exit.

    Exists so that "the dogs are finally on the network" is one command
    instead of a debugging session: the two failures that have cost the most
    time here both surface as something other than what they are.
    """
    import asyncio

    from unitree_robot_agent.fleet import run_preflight, summarise

    results = asyncio.run(run_preflight(roster))
    summary = summarise(results)

    for record in summary["results"]:
        mark = "OK  " if record["ok"] else "FAIL"
        print(f"[{mark}] {record['robot_id']:<8} {record['model']:<4} "
              f"{record['ip'] or record['target']:<16} {record['verdict']}")
        if record["detail"]:
            print(f"         {record['detail']}")
        if not record["ok"] and record["hint"]:
            print(f"         → {record['hint']}")

    print()
    print(f"{len(summary['ready'])}/{summary['count']} ready"
          + (f"; blocked: {', '.join(summary['blocked'])}" if summary["blocked"] else ""))
    print(summary["note"])
    return 0 if summary["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
