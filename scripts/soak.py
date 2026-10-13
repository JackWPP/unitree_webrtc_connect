#!/usr/bin/env python3
"""Run the console for a while with everything on, and measure what grows.

**Long running is a requirement, and nothing measured it.** The soak tests in
`tests/test_console_soak.py` check that specific structures stay bounded —
reconnects, telemetry history, the audit trail, the payload size. All of them
count *things*; none of them looks at the process.

That is the gap this fills. A console left open for an eight-hour session leaks
in ways a five-second test cannot see: a task created per status push, a client
list that only grows, a frame buffer retained per reconnect, a file descriptor
per video stream. Each of those is invisible until the session is long enough
that the machine starts swapping.

## What it measures

* **Peak RSS.** `ru_maxrss` is a high-water mark, so it can only go up — which
  makes it a valid leak detector: if the peak keeps climbing, the process
  genuinely reached that size and did not give it back. (It is bytes on macOS
  and kilobytes on Linux; both are normalised here.)
* **Live asyncio tasks.** A background loop that leaks one task per iteration
  shows up here long before it shows up in memory.
* **Open file descriptors.** Every video stream, every socket, every log file.
* **The console's own bookkeeping**, read from the live service: connected
  clients, motion controllers, pending stand gates, routine history length.
* **The status payload size**, because a payload that grows with time is a
  browser that gets slower every minute.

Usage::

    scripts/soak.py --minutes 10
    scripts/soak.py --minutes 1 --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import resource
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _rss_bytes() -> int:
    """Peak resident set size in bytes, normalised across platforms.

    A high-water mark rather than a current reading, deliberately: it cannot
    oscillate, so a rise is a rise.
    """
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS reports bytes, Linux kilobytes. 100 MB is a safe dividing line for a
    # Python process — anything above it is already in bytes.
    return int(raw) if raw > 100_000_000 else int(raw) * 1024


def _open_fds() -> int:
    for directory in ("/dev/fd", "/proc/self/fd"):
        try:
            return len(os.listdir(directory))
        except OSError:
            continue
    return -1


def _tasks() -> int:
    return len(asyncio.all_tasks())


def _sample(service, started: float) -> Dict[str, Any]:
    """One reading of everything that should be flat."""
    row: Dict[str, Any] = {
        "t": round(time.monotonic() - started, 1),
        "rss_mb": round(_rss_bytes() / 1024 / 1024, 1),
        "tasks": _tasks(),
        "fds": _open_fds(),
    }
    try:
        row["clients"] = len(getattr(service, "_clients", ()) or ())
        row["motion"] = len(getattr(service, "_motion", {}) or {})
        row["gates"] = len(getattr(service, "_gate_tasks", ()) or ())
        row["routine_steps"] = len(
            (getattr(service, "_routine", None) or {}).get("steps") or ()
        )
        row["payload_bytes"] = len(
            json.dumps(asyncio.run_coroutine_threadsafe if False else None) or ""
        )
    except Exception:  # noqa: BLE001 - a missing internal is not a failure
        pass
    return row


async def _payload_bytes(service) -> int:
    try:
        return len(json.dumps(await service.status(), ensure_ascii=False))
    except Exception:  # noqa: BLE001
        return -1


async def soak(minutes: float, *, hz: float = 4.0, quiet: bool = False) -> Dict[str, Any]:
    """Exercise everything the console does, for a while, and watch the process."""
    from unitree_robot_agent.fleet import FleetManager, Roster
    from unitree_fleet_web import ConsoleConfig, ConsoleService

    roster = Roster.from_mapping({
        "state_dir": str(Path(os.environ.get("TMPDIR", "/tmp")) / "unitree-soak"),
        "default": "dog1",
        "robots": [
            {"id": "dog1", "model": "Go2", "ip": "192.168.31.245", "sn": "SOAK-1"},
            {"id": "dog2", "model": "Go2", "ip": "192.168.31.246", "sn": "SOAK-2"},
        ],
    })
    service = ConsoleService(
        FleetManager(roster, backend="mock", audit_to_stderr=False),
        config=ConsoleConfig(deadman_s=1.0, telemetry_hz=1.0, motion_hz=50.0),
    )
    samples: List[Dict[str, Any]] = []
    errors: List[str] = []
    started = time.monotonic()
    deadline = started + minutes * 60

    await service.connect("dog1")
    await service.connect("dog2")
    await service.start()

    round_number = 0
    try:
        while time.monotonic() < deadline:
            round_number += 1
            # Every input path the console has, each round: a status push, a
            # telemetry read, a stick command, a routine, and a browser joining
            # and leaving. A soak that only idles tests the idle path.
            try:
                await service.status()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"status: {type(exc).__name__}: {exc}")
            try:
                await service.refresh_telemetry("dog1")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"telemetry: {type(exc).__name__}: {exc}")
            try:
                await service.teleop("dog1", ly=0.2 if round_number % 2 else -0.2)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"teleop: {type(exc).__name__}: {exc}")
            try:
                await service.release_drive()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"release: {type(exc).__name__}: {exc}")
            if round_number % 5 == 0:
                try:
                    await service.call_tool("dog1", "robot_walk",
                                            {"meters": 0.05, "confirm": True})
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"walk: {type(exc).__name__}: {exc}")
            if round_number % 20 == 0:
                try:
                    await service.start_routine("greet")
                    await asyncio.sleep(0.1)
                    await service.stop_routine(reason="soak")
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"routine: {type(exc).__name__}: {exc}")

            row = _sample(service, started)
            row["round"] = round_number
            row["payload_bytes"] = await _payload_bytes(service)
            samples.append(row)
            if not quiet and round_number % 10 == 0:
                print(
                    f"  t={row['t']:>6.1f}s  rss={row['rss_mb']:>7.1f}MB  "
                    f"tasks={row['tasks']:>3}  fds={row['fds']:>3}  "
                    f"payload={row['payload_bytes']:>5}B  rounds={round_number}"
                )
            await asyncio.sleep(1.0 / hz)
    finally:
        try:
            await service.aclose()
        except Exception:  # noqa: BLE001
            pass

    first, last = samples[0], samples[-1]
    return {
        "minutes": minutes,
        "rounds": round_number,
        "samples": len(samples),
        "first": first,
        "last": last,
        "growth": {
            "rss_mb": round(last["rss_mb"] - first["rss_mb"], 1),
            "tasks": last["tasks"] - first["tasks"],
            "fds": last["fds"] - first["fds"],
            "payload_bytes": last["payload_bytes"] - first["payload_bytes"],
        },
        "errors": errors[:20],
        "error_count": len(errors),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Soak the console and measure what grows.")
    parser.add_argument("--minutes", type=float, default=5.0)
    parser.add_argument("--hz", type=float, default=4.0, help="rounds per second")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("-o", "--output", default=None)
    args = parser.parse_args()

    print(f"浸泡 {args.minutes:g} 分钟，每秒 {args.hz:g} 轮…")
    result = asyncio.run(soak(args.minutes, hz=args.hz, quiet=args.json))

    if args.json:
        text = json.dumps(result, ensure_ascii=False, indent=2)
    else:
        g = result["growth"]
        text = "\n".join([
            "",
            f"  轮次 {result['rounds']}（{result['samples']} 个采样）",
            f"  RSS   {result['first']['rss_mb']:>7.1f} -> {result['last']['rss_mb']:>7.1f} MB"
            f"  ({g['rss_mb']:+.1f})",
            f"  任务  {result['first']['tasks']:>7} -> {result['last']['tasks']:>7}"
            f"  ({g['tasks']:+d})",
            f"  句柄  {result['first']['fds']:>7} -> {result['last']['fds']:>7}"
            f"  ({g['fds']:+d})",
            f"  载荷  {result['first']['payload_bytes']:>7} -> {result['last']['payload_bytes']:>7} B"
            f"  ({g['payload_bytes']:+d})",
            f"  错误  {result['error_count']}",
        ])
        for error in result["errors"]:
            text += f"\n    {error}"
        text += "\n\n  判读：任务数和句柄数应当**完全持平**。"
        text += "\n  RSS 是**峰值**（只增不减），所以它涨了就是真的涨过。\n"

    print(text)
    if args.output:
        Path(args.output).write_text(
            json.dumps(result, ensure_ascii=False, indent=2) if args.json else text
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
