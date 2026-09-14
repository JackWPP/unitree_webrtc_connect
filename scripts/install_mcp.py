#!/usr/bin/env python3
"""One-command installer for the Unitree robot MCP server.

Designed to be run by a *person or an agent* with a single line:

    python scripts/install_mcp.py --robot R1 --ip 192.168.10.225

What it does, in order, and it stops at the first failure:

1. installs this checkout into the current interpreter (`pip install -e ".[agent]"`),
2. runs the offline self test — if the server cannot boot, nothing is registered,
3. merges an `mcpServers` entry into the MCP config of each requested host,
   backing the original file up first and writing atomically,
4. prints exactly what changed and how to verify it.

Only the standard library is used, so it runs *before* the package is installed.
Nothing is written outside the config files it names, and `--dry-run` prints the
JSON without touching anything.

Why an installer instead of a README snippet: the fiddly parts are not the JSON,
they are (a) pointing the host at the *right* interpreter, (b) not clobbering an
existing `mcp.json`, and (c) knowing whether the server actually starts. Doing
those by hand is where one-line instructions usually fall over.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_NAME = "unitree-robot"

#: `python -m unitree_robot_agent.cli serve` is used instead of the console
#: script on purpose: it works even when the interpreter's bin/ directory is
#: not on PATH, which is common for agent hosts.
SERVER_ARGS = ["-m", "unitree_robot_agent.cli", "serve"]

DESCRIPTION = "Unitree Go2 / R1 robot control over WebRTC (guarded, offline-testable)"


# ─── host config discovery ────────────────────────────────────────────


def _home() -> Path:
    return Path.home()


def _claude_desktop_path() -> Path:
    if platform.system() == "Darwin":
        return _home() / "Library/Application Support/Claude/claude_desktop_config.json"
    if platform.system() == "Windows":
        return Path(os.environ.get("APPDATA", _home())) / "Claude/claude_desktop_config.json"
    return _home() / ".config/Claude/claude_desktop_config.json"


def host_paths(config_dir: Optional[Path] = None) -> Dict[str, Path]:
    """Where each supported host keeps its MCP server list."""
    base = Path(config_dir).expanduser() if config_dir else None
    return {
        # Verified on macOS: CodeBuddy (and CodeBuddy CN) read this file.
        "codebuddy": (base or _home() / ".codebuddy") / "mcp.json",
        "cursor": (base or _home() / ".cursor") / "mcp.json",
        "claude-desktop": (base / "claude_desktop_config.json")
        if base
        else _claude_desktop_path(),
        "workspace": REPO_ROOT / ".codebuddy" / "mcp.json",
    }


HOST_CHOICES = ("codebuddy", "cursor", "claude-desktop", "workspace", "auto", "none")


def detect_hosts(config_dir: Optional[Path] = None) -> List[str]:
    """Hosts that look installed on this machine (config dir or file exists)."""
    found = []
    for name, path in host_paths(config_dir).items():
        if name == "workspace":
            continue
        if path.exists() or path.parent.exists():
            found.append(name)
    return found


# ─── shell helpers ────────────────────────────────────────────────────


def run(cmd: List[str], *, cwd: Optional[Path] = None) -> Tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True
        )
    except OSError as exc:
        return 127, f"{exc.__class__.__name__}: {exc}"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def step(message: str) -> None:
    print(f"\n▸ {message}", flush=True)


def ok(message: str) -> None:
    print(f"  ✓ {message}", flush=True)


def warn(message: str) -> None:
    print(f"  ! {message}", flush=True)


def fail(message: str) -> None:
    print(f"  ✗ {message}", file=sys.stderr, flush=True)


# ─── steps ────────────────────────────────────────────────────────────


def install_package(python: str, *, editable: bool, dry_run: bool) -> bool:
    target = f"{REPO_ROOT}[agent]" if editable else str(REPO_ROOT)
    cmd = [python, "-m", "pip", "install", "--disable-pip-version-check", "-q", "-e", target]
    if dry_run:
        print("  would run:", " ".join(cmd))
        return True

    step(f"installing {REPO_ROOT.name}[agent] into {python}")
    code, output = run(cmd)
    if code != 0:
        # Editable installs need a writable source tree; fall back to a normal
        # install so read-only checkouts still work.
        warn("editable install failed, retrying without -e")
        code, output = run([c for c in cmd if c != "-e"])
    if code != 0:
        fail("pip install failed:")
        print("\n".join(output.splitlines()[-12:]), file=sys.stderr)
        return False
    ok("package installed")
    return True


def verify(python: str, *, robot: str, dry_run: bool) -> bool:
    if dry_run:
        print(f"  would run: {python} -m unitree_robot_agent.cli selftest --robot {robot}")
        return True
    step("verifying the server offline (mock backend, no robot needed)")
    code, output = run(
        [python, "-m", "unitree_robot_agent.cli", "selftest", "--robot", robot],
        cwd=REPO_ROOT,
    )
    tail = [line for line in output.splitlines() if line.strip()][-1:] or [""]
    if code != 0:
        fail(f"self test failed: {tail[0].strip()}")
        print("\n".join(output.splitlines()[-20:]), file=sys.stderr)
        return False
    ok(f"self test passed ({tail[0].strip()})")
    return True


def build_entry(
    python: str,
    *,
    robot: str,
    ip: str,
    sn: str,
    key: str,
    region: str,
    safe_mode: bool,
    extra_env: Dict[str, str],
) -> dict:
    env: Dict[str, str] = {"UNITREE_AGENT_ROBOT": robot}
    if ip:
        env["UNITREE_ROBOT_IP"] = ip
    if sn:
        env["UNITREE_ROBOT_SN"] = sn
    if key:
        env["UNITREE_AES_128_KEY"] = key
    if region:
        env["UNITREE_AGENT_REGION"] = region
    if safe_mode:
        env["UNITREE_AGENT_SAFE_MODE"] = "true"
    env.update(extra_env or {})
    # Absolute interpreter path: agent hosts do not inherit the shell's PATH.
    return {
        "type": "stdio",
        "command": python,
        "args": list(SERVER_ARGS),
        "env": env,
        "description": DESCRIPTION,
    }


def register(host: str, path: Path, entry: dict, *, dry_run: bool) -> bool:
    step(f"registering with {host} → {path}")

    existing: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError) as exc:
            fail(f"existing config is not valid JSON ({exc}); leaving it alone")
            return False
        if not isinstance(existing, dict):
            fail("existing config is not a JSON object; leaving it alone")
            return False

    servers = existing.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        fail("existing `mcpServers` is not an object; leaving it alone")
        return False

    replaced = SERVER_NAME in servers
    servers[SERVER_NAME] = entry

    if dry_run:
        print(f"  would write ({'replace' if replaced else 'add'}):")
        print(json.dumps({"mcpServers": servers}, indent=2, ensure_ascii=False))
        return True

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            backup = path.with_suffix(path.suffix + f".bak-{int(time.time())}")
            shutil.copy2(path, backup)
            ok(f"backed up existing config → {backup.name}")
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(existing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        fail(f"could not write {path}: {exc}")
        return False

    ok(f"{'replaced' if replaced else 'added'} `{SERVER_NAME}`")
    return True


def print_snippet(entry: dict) -> None:
    print("\nPaste this into your MCP client's config if it is not one of the")
    print("hosts above (`mcpServers` may need to be added alongside it):\n")
    print(json.dumps({"mcpServers": {SERVER_NAME: entry}}, indent=2, ensure_ascii=False))


# ─── main ─────────────────────────────────────────────────────────────


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="install_mcp.py",
        description="Install and register the Unitree robot MCP server.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python scripts/install_mcp.py --robot R1 --ip 192.168.10.225\n"
            "  python scripts/install_mcp.py --robot R1 --ip 192.168.10.225 --key 32hexchars\n"
            "  python scripts/install_mcp.py --robot Go2 --ip 192.168.8.181 --host codebuddy\n"
            "  python scripts/install_mcp.py --dry-run          # show, change nothing\n"
        ),
    )
    parser.add_argument("--robot", default="R1", choices=["R1", "Go2"], help="robot family")
    parser.add_argument("--ip", default="", help="robot IP (STA-L). Omit for AP mode")
    parser.add_argument("--sn", default="", help="robot serial number")
    parser.add_argument(
        "--key",
        default="",
        help="per-device AES-128 key (R1 / Go2 >= 1.1.15). Without it the server "
        "fetches the key from the cloud on first connect",
    )
    parser.add_argument(
        "--region", default="auto", choices=["auto", "global", "cn"], help="cloud region"
    )
    parser.add_argument(
        "--safe-mode",
        action="store_true",
        help="register the server in read-only mode (movement tools refuse)",
    )
    parser.add_argument(
        "--host",
        default="auto",
        choices=list(HOST_CHOICES),
        help="which MCP config to write; auto = every detected host",
    )
    parser.add_argument("--name", default=SERVER_NAME, help="server name in mcpServers")
    parser.add_argument("--python", default=sys.executable, help="interpreter to install into")
    parser.add_argument("--config-dir", default="", help="override the user config directory")
    parser.add_argument("--venv", default="", help="create/use a virtualenv at this path")
    parser.add_argument("--no-install", action="store_true", help="skip pip install")
    parser.add_argument("--no-verify", action="store_true", help="skip the self test")
    parser.add_argument("--no-editable", action="store_true", help="install a copy, not -e")
    parser.add_argument("--dry-run", action="store_true", help="print actions, change nothing")
    parser.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="K=V",
        help="extra environment variable for the server (repeatable)",
    )
    return parser.parse_args(argv)


def make_venv(path: Path, *, python: str, dry_run: bool) -> Optional[str]:
    if dry_run:
        print(f"  would create virtualenv at {path}")
        return str(path / ("Scripts/python.exe" if platform.system() == "Windows" else "bin/python"))
    step(f"creating a virtualenv at {path}")
    code, output = run([python, "-m", "venv", str(path)])
    if code != 0:
        fail("could not create the virtualenv:")
        print("\n".join(output.splitlines()[-8:]), file=sys.stderr)
        return None
    exe = path / ("Scripts/python.exe" if platform.system() == "Windows" else "bin/python")
    if not exe.exists():
        fail(f"virtualenv created but {exe} is missing")
        return None
    ok(f"virtualenv ready: {exe}")
    return str(exe)


def main(argv: Optional[List[str]] = None) -> int:
    global SERVER_NAME
    args = parse_args(argv)
    SERVER_NAME = args.name

    print("Unitree robot MCP installer")
    print(f"  repo    : {REPO_ROOT}")
    print(f"  robot   : {args.robot}" + (f"  ip: {args.ip}" if args.ip else "  (AP mode)"))
    if args.dry_run:
        print("  mode    : DRY RUN — nothing will be written")

    python = args.python
    if args.venv:
        created = make_venv(Path(args.venv).expanduser(), python=python, dry_run=args.dry_run)
        if created is None:
            return 1
        python = created

    extra_env: Dict[str, str] = {}
    for pair in args.env:
        key, _, value = pair.partition("=")
        if key:
            extra_env[key] = value

    if not args.no_install:
        if not install_package(python, editable=not args.no_editable, dry_run=args.dry_run):
            return 1
    if not args.no_verify:
        if not verify(python, robot=args.robot, dry_run=args.dry_run):
            return 1

    entry = build_entry(
        python,
        robot=args.robot,
        ip=args.ip,
        sn=args.sn,
        key=args.key,
        region=args.region,
        safe_mode=args.safe_mode,
        extra_env=extra_env,
    )

    config_dir = Path(args.config_dir) if args.config_dir else None
    paths = host_paths(config_dir)

    if args.host == "none":
        print_snippet(entry)
        return 0

    if args.host == "auto":
        targets = detect_hosts(config_dir)
        if not targets:
            warn("no MCP host config detected on this machine")
            print_snippet(entry)
            return 0
    else:
        targets = [args.host]

    written: List[str] = []
    for host in targets:
        path = paths.get(host)
        if path is None:
            warn(f"unknown host {host!r}, skipped")
            continue
        if register(host, path, entry, dry_run=args.dry_run):
            written.append(f"{host}: {path}")

    if args.host == "auto" and targets:
        # Always show the snippet too: it costs nothing and covers hosts we
        # cannot write to (Claude Code stores its list elsewhere, for example).
        print_snippet(entry)

    print("\nDone." if written else "\nNothing was registered.")
    if written:
        print("\nNext:")
        print("  1. restart / reload your MCP host so it picks up the new server")
        print("  2. ask the agent to call `robot_doctor`, then `robot_status`")
        print("  3. if the robot needs an AES key, run: unitree-robot-mcp fetch-key")
        if not args.key and args.robot == "R1":
            print("\nNote: R1 always needs the per-device AES key. Either pass --key,")
            print("      or configure UNITREE_AGENT_EMAIL/PASSWORD so the server can")
            print("      fetch and cache it on first use.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
