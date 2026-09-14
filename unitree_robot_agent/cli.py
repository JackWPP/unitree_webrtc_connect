"""Command line entry point: ``unitree-robot-mcp``.

Subcommands:

* ``serve``      — run the MCP server (stdio by default)
* ``tools``      — list what this configuration would expose
* ``doctor``     — diagnose config / dependencies / connectivity
* ``selftest``   — run the whole tool surface against the offline mock backend
* ``discover``   — multicast scan for robots
* ``fetch-key``  — forward to the upstream ``unitree-fetch-aes-key`` CLI
* ``audit-tail`` — print the audit log
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import AgentConfig, config_from_env
from .runtime import create_runtime
from .server import ServerBundle, build_server, serve_async


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "serve":
        return _serve(args)
    if args.command == "tools":
        return _tools(args)
    if args.command == "doctor":
        return _run(_doctor(args))
    if args.command == "selftest":
        return _run(_selftest(args))
    if args.command == "discover":
        return _run(_discover(args))
    if args.command == "audit-tail":
        return _audit_tail(args)
    if args.command == "fetch-key":
        return _run(_fetch_key(args))
    if args.command == "auth-status":
        return _run(_auth_status(args))
    if args.command == "upstream-fetch-key":
        # Escape hatch to the original SDK CLI, kept for flag parity.
        from unitree_webrtc_connect._cli import main as upstream_main

        return upstream_main(args.rest)

    parser.print_help()
    return 2


# ─── parser ───────────────────────────────────────────────────────────


def _common_options() -> argparse.ArgumentParser:
    """Options accepted both before and after the subcommand.

    ``default=SUPPRESS`` is load-bearing: a subparser parses into a fresh
    namespace and copies *every* attribute into the parent's, so an ordinary
    default would clobber a flag given before the subcommand — `--safe-mode
    serve` used to start with read-only mode silently OFF.
    """
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--robot", default=argparse.SUPPRESS,
        help="R1 or Go2 (default: env UNITREE_AGENT_ROBOT or R1)",
    )
    common.add_argument("--backend", default=argparse.SUPPRESS, help="auto | webrtc | mock")
    common.add_argument("--ip", default=argparse.SUPPRESS, help="robot IP for STA-L")
    common.add_argument("--sn", default=argparse.SUPPRESS, help="robot serial number")
    common.add_argument(
        "--region", default=argparse.SUPPRESS,
        help="cloud region: auto | global | cn (default: auto)",
    )
    common.add_argument(
        "--safe-mode", action="store_true", default=argparse.SUPPRESS,
        help="read-only: refuse every moving tool",
    )
    return common


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="unitree-robot-mcp",
        description="MCP server exposing safe control of Unitree robots (Go2 / R1).",
        parents=[_common_options()],
        conflict_handler="resolve",
    )
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser(
        "serve", help="run the MCP server", parents=[_common_options()], conflict_handler="resolve"
    )
    serve.add_argument(
        "--transport",
        default=None,
        choices=["stdio", "streamable-http"],
        help="stdio for local agent hosts (default), streamable-http to share one robot",
    )
    serve.add_argument("--host", default=None, help="bind address for streamable-http")
    serve.add_argument("--port", type=int, default=None, help="port for streamable-http")

    sub.add_parser(
        "tools", help="list the tools this configuration exposes",
        parents=[_common_options()], conflict_handler="resolve",
    )
    sub.add_parser(
        "doctor", help="diagnose configuration and connectivity",
        parents=[_common_options()], conflict_handler="resolve",
    )
    sub.add_parser(
        "selftest", help="run the offline end-to-end self test (mock backend)",
        parents=[_common_options()], conflict_handler="resolve",
    )

    discover = sub.add_parser(
        "discover", help="multicast scan for robots",
        parents=[_common_options()], conflict_handler="resolve",
    )
    discover.add_argument("--sn-discover", default=None, help="serial number to target")
    discover.add_argument("--timeout", type=float, default=3.0)

    fetch = sub.add_parser(
        "fetch-key",
        help="fetch + cache the per-device AES-128 key from the Unitree cloud",
        parents=[_common_options()],
        conflict_handler="resolve",
    )
    fetch.add_argument("--list", action="store_true", help="also list every bound device")
    fetch.add_argument("-q", "--quiet", action="store_true", help="print only the bare key on stdout")

    sub.add_parser(
        "auth-status",
        help="show cloud auth state (region, cached session, credential sources)",
        parents=[_common_options()],
        conflict_handler="resolve",
    )

    upstream = sub.add_parser("upstream-fetch-key", help="delegate to the SDK's unitree-fetch-aes-key")
    upstream.add_argument("rest", nargs=argparse.REMAINDER)

    audit = sub.add_parser(
        "audit-tail", help="print the audit log",
        parents=[_common_options()], conflict_handler="resolve",
    )
    audit.add_argument("-n", "--lines", type=int, default=30)
    return parser


def _config_from_args(args) -> AgentConfig:
    overrides: Dict[str, Any] = {}
    if getattr(args, "robot", None):
        overrides["robot"] = args.robot
    if getattr(args, "backend", None):
        overrides["backend"] = args.backend
    if getattr(args, "ip", None):
        overrides["ip"] = args.ip
    if getattr(args, "sn", None):
        overrides["serial_number"] = args.sn
    if getattr(args, "region", None):
        overrides["region"] = args.region
    if getattr(args, "safe_mode", False):
        overrides["safe_mode"] = True
    if getattr(args, "transport", None):
        overrides["transport"] = args.transport
    if getattr(args, "host", None):
        overrides["host"] = args.host
    if getattr(args, "port", None):
        overrides["port"] = args.port
    return config_from_env(**overrides)


def _run(coro) -> int:
    try:
        return asyncio.run(coro)
    except KeyboardInterrupt:
        return 130


# ─── commands ─────────────────────────────────────────────────────────


def _serve(args) -> int:
    config = _config_from_args(args)
    runtime = create_runtime(config)
    bundle = build_server(runtime)

    target = config.target_label()
    print(
        f"unitree-robot-mcp: robot={config.robot} backend={config.backend} "
        f"target={target} transport={config.transport} tools={len(bundle.tool_names)}",
        file=sys.stderr,
    )
    if config.safe_mode:
        print("  read-only safe mode ON", file=sys.stderr)

    try:
        asyncio.run(serve_async(bundle))
    except KeyboardInterrupt:
        return 0
    return 0


def _tools(args) -> int:
    config = _config_from_args(args)
    bundle = build_server(create_runtime(config))
    print(f"robot={config.robot}  backend={config.backend}  api={bundle.api}")
    print(f"{len(bundle.tool_names)} tools:\n")
    for name in bundle.tool_names:
        tool = _get_tool(bundle, name)
        description = (getattr(tool, "description", "") or "").strip().splitlines()
        print(f"  {name}")
        if description:
            print(f"      {description[0][:110]}")
    return 0


async def _doctor(args) -> int:
    config = _config_from_args(args)
    bundle = build_server(create_runtime(config))
    result = await _invoke(bundle, "robot_doctor", {})
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0 if result.get("ok") else 1


async def _discover(args) -> int:
    config = _config_from_args(args)
    if config.backend == "mock":
        print("mock backend: discovery disabled")
        return 0
    from .backends.webrtc_backend import discover

    devices = await discover(
        device_type=config.device_type,
        sn=args.sn_discover or config.serial_number,
        timeout=args.timeout,
    )
    if not devices:
        print("no robots answered")
        if not (args.sn_discover or config.serial_number) and config.device_type in ("R1", "G1"):
            print("hint: R1 / G1 >= 1.5.1 only answer a query that names their serial number (--sn)")
        return 1
    for sn, ip in devices.items():
        print(f"{sn}  {ip}")
    return 0


async def _auth_status(args) -> int:
    config = _config_from_args(args)
    runtime = create_runtime(config)
    state = runtime.auth.describe()
    state["aes_key_cached"] = bool(config.load_cached_aes_key())
    print(json.dumps(state, indent=2, ensure_ascii=False, default=str))
    await runtime.aclose()
    return 0


async def _fetch_key(args) -> int:
    config = _config_from_args(args)
    runtime = create_runtime(config)
    quiet = bool(getattr(args, "quiet", False))

    lookup = await runtime.auth.resolve_aes_key(getattr(args, "sn", None) or None)

    if getattr(args, "list", False) or (not lookup.ok and not quiet):
        try:
            devices = await runtime.auth.list_devices()
        except Exception as exc:
            devices = []
            print(f"  device list failed: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        if devices:
            print(f"\nBound devices ({len(devices)}) — region={config.resolved_region or config.region}", file=sys.stderr)
            for d in devices:
                key = (d["key"][:4] + "****" + d["key"][-2:]) if d.get("key") else "(no key)"
                print(
                    f"  SN={d['sn']}  alias={d.get('alias') or '-'}  series={d.get('series')}  "
                    f"model={d.get('model')}  online={d.get('online')}  key={key}",
                    file=sys.stderr,
                )

    if not lookup.ok:
        print(f"  no key: {lookup.note or 'unknown reason'}", file=sys.stderr)
        print(
            "  hint: configure UNITREE_AGENT_EMAIL + UNITREE_AGENT_PASSWORD "
            "(or UNITREE_AGENT_ACCESS_TOKEN), or pass the key directly as "
            "UNITREE_AES_128_KEY.",
            file=sys.stderr,
        )
        await runtime.aclose()
        return 1

    if quiet:
        print(lookup.key)
    else:
        print("\nAES-128 key", file=sys.stderr)
        print(f"  SN     : {lookup.sn}", file=sys.stderr)
        print(f"  source : {lookup.source or 'unknown'}", file=sys.stderr)
        print(f"  region : {lookup.region or config.resolved_region}", file=sys.stderr)
        print(f"  cached : {config.key_cache_path}", file=sys.stderr)
        print(f"  key    : {lookup.key[:4]}…{lookup.key[-2:]} (masked; use -q to print it)", file=sys.stderr)

    await runtime.aclose()
    return 0


def _audit_tail(args) -> int:
    config = _config_from_args(args)
    runtime = create_runtime(config)
    records = runtime.audit.tail(args.lines)
    if not records:
        print(f"(no audit records at {config.audit_log})")
        return 0
    for record in records:
        print(json.dumps(record, ensure_ascii=False, default=str))
    return 0


# ─── self test ────────────────────────────────────────────────────────


async def _selftest(args) -> int:
    """Drive the real tool surface against the mock backend.

    This is the offline proof that the guard rails, the profile semantics
    and the tool wiring all work without a robot on the desk.
    """
    # Run against a throwaway state dir so a self test never touches the real
    # key/session caches or the snapshot archive.
    scratch = Path(tempfile.mkdtemp(prefix="unitree-selftest-"))
    config = config_from_env(
        **(args.robot and {"robot": args.robot} or {}),
        backend="mock",
        idle_disconnect_s=2.0,
        audit_enabled=False,
        state_dir=scratch,
    )
    runtime = create_runtime(config)
    bundle = build_server(runtime)
    checks: List[Tuple[str, bool, str]] = []
    robot = config.robot

    async def check(label: str, coro, predicate=None):
        try:
            result = await coro
        except Exception as exc:  # pragma: no cover - surfaced as a failure
            checks.append((label, False, f"raised {exc.__class__.__name__}: {exc}"))
            return None
        ok = True
        detail = ""
        if predicate is not None:
            ok, detail = predicate(result)
        checks.append((label, bool(ok), detail or _brief(result)))
        return result

    # 1. read-only surface
    await check("robot_status", _invoke(bundle, "robot_status", {}), lambda r: (r.get("ok") is True, ""))
    await check(
        "robot_list_actions",
        _invoke(bundle, "robot_list_actions", {}),
        lambda r: (bool(r.get("actions")), f"{len(r.get('actions', []))} actions"),
    )
    await check(
        "robot_capabilities",
        _ainvoke(bundle, "robot_doctor", {}),
        lambda r: (r.get("ok") is True, f"verdict={r.get('verdict')}"),
    )

    # 2. standing up
    await check(
        "robot_stand",
        _invoke(bundle, "robot_stand", {"confirm": True}),
        lambda r: (r.get("ok") is True, ""),
    )

    # 3. FSM: Air redirect must be reported as confirmed
    if robot == "R1":
        result = await check(
            "robot_switch_state(Run)",
            _invoke(bundle, "robot_switch_state", {"state": "Run", "confirm": True}),
            lambda r: (
                r.get("state", {}).get("redirected") is True
                and r.get("state", {}).get("confirmed") is True,
                f"redirected={r.get('state', {}).get('redirected')} "
                f"reported={r.get('state', {}).get('reported', {}).get('state')}",
            ),
        )
        del result
    else:
        await check(
            "go2_get_state",
            _invoke(bundle, "go2_get_state", {}),
            lambda r: (r.get("ok") is True, f"multi_state={r.get('multi_state') is not None}"),
        )
        await check(
            "go2_obstacle_avoid(read)",
            _invoke(bundle, "go2_obstacle_avoid", {"enable": False}),
            lambda r: (r.get("ok") is True and r.get("enabled") is False, f"enabled={r.get('enabled')}"),
        )
        await check(
            "go2_set_led",
            _invoke(bundle, "go2_set_led", {"color": "cyan", "seconds": 3}),
            lambda r: (r.get("ok") is True, ""),
        )

    # 4. guard: confirmation is mandatory
    result = await check(
        "guard: confirm required",
        _invoke(bundle, "robot_play_action", {"name": "dance_1"}),
        lambda r: (r.get("error") == "confirm_required", str(r.get("error"))),
    )
    del result

    # 5. guard: local white list refuses an unreachable motion
    if robot == "R1":
        await check(
            "guard: local whitelist",
            _invoke(bundle, "robot_switch_state", {"state": "Dance1", "confirm": True}),
            lambda r: (r.get("error") == "guard_rejected", str(r.get("error"))),
        )

    # 6. duration clamp
    result = await check(
        "guard: duration clamp",
        _invoke(bundle, "robot_move", {"vx": 0.2, "duration_s": 900, "confirm": True}),
        lambda r: (r.get("ok") is True and r.get("duration_s", 0) <= 10.0, f"duration={r.get('duration_s')}"),
    )
    del result

    # 6b. motion: profiled drive + closed-loop turn
    await check(
        "robot_joystick (profile)",
        _invoke(bundle, "robot_joystick", {"ly": 0.2, "duration_s": 0.4, "confirm": True}),
        lambda r: (r.get("ok") is True, f"{r.get('profile', {}).get('shape')} {r.get('frames')} frames"),
    )
    await check(
        "robot_turn (closed loop)",
        _invoke(bundle, "robot_turn", {"degrees": 45, "confirm": True}),
        lambda r: (
            r.get("ok") is True and abs(r.get("residual_deg", 99)) <= r.get("tolerance_deg", 0),
            f"turned {r.get('turned_deg')}° residual {r.get('residual_deg')}°",
        ),
    )

    # 7. stop
    await check("robot_stop", _invoke(bundle, "robot_stop", {}), lambda r: (r.get("stopped") is True, ""))

    # 8. posture read-back
    await check(
        "robot_get_posture",
        _invoke(bundle, "robot_get_posture", {}),
        lambda r: (r.get("ok") is True, str(r.get("posture", {}).get("state"))),
    )

    # 9. emergency stop
    await check(
        "robot_emergency_stop",
        _invoke(bundle, "robot_emergency_stop", {"confirm": True}),
        lambda r: (r.get("ok") is True, ""),
    )

    # 10. vision
    await check(
        "robot_vision_probe",
        _invoke(bundle, "robot_vision_probe", {"timeout_s": 4.0}),
        lambda r: ("webrtc" in r.get("working", []), f"working={r.get('working')}"),
    )
    await check(
        "robot_snapshot",
        _invoke(bundle, "robot_snapshot", {"timeout_s": 5.0}),
        lambda r: (type(r).__name__ == "Image", f"{type(r).__name__} {len(getattr(r, 'data', b'') or b'')}B"),
    )
    await check(
        "robot_snapshot_history",
        _invoke(bundle, "robot_snapshot_history", {"limit": 5}),
        lambda r: (r.get("count", 0) >= 1, f"{r.get('count')} archived"),
    )

    # 11. lowstate
    if robot == "R1":
        await check(
            "robot_lowstate",
            _invoke(bundle, "robot_lowstate", {"include_motors": False}),
            lambda r: (r.get("ok") is True and isinstance(r.get("battery"), dict), ""),
        )

    # 12. errors + audit
    await check("robot_errors", _invoke(bundle, "robot_errors", {}), lambda r: (r.get("ok") is True, ""))
    await check(
        "audit trail",
        _invoke(bundle, "robot_audit_tail", {"limit": 200}),
        lambda r: (len(r.get("records", [])) > 5, f"{len(r.get('records', []))} records"),
    )

    # 13. idle release
    state = runtime.session
    await state.disconnect(reason="selftest")
    checks.append(
        ("session released", not state.is_connected, "link free for the official app")
    )

    await runtime.aclose()
    shutil.rmtree(scratch, ignore_errors=True)

    width = max(len(label) for label, _, _ in checks)
    failed = 0
    print(f"\nunitree-robot-mcp selftest — robot={robot}, backend=mock\n")
    for label, ok, detail in checks:
        mark = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"  [{mark}] {label.ljust(width)}  {detail}")
    print(f"\n{len(checks) - failed}/{len(checks)} checks passed")
    return 1 if failed else 0


# ─── tool invocation helpers ──────────────────────────────────────────


def _get_tool(bundle: ServerBundle, name: str):
    manager = getattr(bundle.server, "_tool_manager", None)
    if manager is None:
        try:
            manager = bundle.server.tool_manager
        except Exception:
            return None
    for getter in ("get_tool", "get"):
        method = getattr(manager, getter, None)
        if method is None:
            continue
        try:
            return method(name)
        except Exception:
            continue
    return None


async def _invoke(bundle: ServerBundle, tool_name: str, arguments: Dict[str, Any]):
    """Call a registered tool directly (used by the CLI, not by the MCP client)."""
    tool = _get_tool(bundle, tool_name)
    if tool is None:
        return {"ok": False, "error": "tool_not_found", "message": f"{tool_name} is not registered"}
    fn = getattr(tool, "fn", None) or getattr(tool, "func", None)
    if fn is None:
        return {"ok": False, "error": "tool_not_callable", "message": f"{tool_name} has no callable"}
    result = fn(**arguments)
    if asyncio.iscoroutine(result):
        result = await result
    return result


async def _ainvoke(bundle: ServerBundle, tool_name: str, arguments: Dict[str, Any]):
    return await _invoke(bundle, tool_name, arguments)


def _brief(result: Any) -> str:
    if isinstance(result, dict):
        for key in ("error", "verdict", "action", "state"):
            if key in result:
                value = result[key]
                return f"{key}={value if not isinstance(value, dict) else value.get('state', '')}"
        return "ok" if result.get("ok") else ""
    return type(result).__name__


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        sys.exit(main())
