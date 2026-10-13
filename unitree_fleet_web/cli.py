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
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args(argv)


def build(argv=None) -> tuple:
    """Build ``(config, service)`` without binding a socket. Used by tests."""
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    overrides = {}
    if args.host is not None:
        overrides["host"] = args.host
    if args.port is not None:
        overrides["port"] = args.port
    if args.token is not None:
        overrides["token"] = args.token
    config = ConsoleConfig.from_env(**overrides)
    config.serve_ui = not args.no_ui
    config.check()

    roster = load_roster(args.roster)
    if args.state_dir:
        roster = replace(roster, state_dir=Path(args.state_dir))

    fleet = FleetManager(roster, backend=args.backend)
    service = ConsoleService(fleet, config=config)
    return config, service


def main(argv=None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(
        level=getattr(logging, args.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
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


if __name__ == "__main__":
    raise SystemExit(main())
