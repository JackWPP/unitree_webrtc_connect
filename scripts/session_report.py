#!/usr/bin/env python
"""One command that gathers everything a hardware session needs to report back.

The runbook asks for six things after a session, they come from four different
tools, and the operator is standing next to a robot that wants to go back in
its box. Asking for six commands means getting four, or getting them with a
typo in one, or getting them an hour later from memory. So this is one command
whose output can be pasted whole.

What it does, in the order that matters:

1. **Environment** — git revision, Python, and the versions of the libraries
   that decide whether a link works at all.
2. **Preflight** — read-only; starts the signalling handshake but sends no SDP,
   so the robot's single WebRTC client slot is not claimed.
3. **Topic probe** — which topics this firmware actually publishes. This is the
   decisive measurement: it decides which features can exist.
4. **Live sample** — connects, reads one telemetry snapshot, and reports what
   arrived. Skipped with `--no-connect`.

**It never prints the AES key.** The roster's own `redacted()` is used rather
than a hand-rolled filter, because a hand-rolled filter is the one that leaks —
and a report that gets pasted into a chat is exactly where a leaked key ends up.

Usage:

    python scripts/session_report.py                    # everything
    python scripts/session_report.py --no-connect       # read-only, no slot
    python scripts/session_report.py --json             # machine-readable
    python scripts/session_report.py -o report.md       # to a file
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _git(*args: str) -> str:
    try:
        out = subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=10, check=False
        )
        return out.stdout.strip() or out.stderr.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def environment() -> Dict[str, Any]:
    """Versions and revisions, because a link that works on one machine and not
    another is usually this list rather than the robot."""
    import importlib.metadata as md

    packages = {}
    for name in ("aiortc", "aioice", "mcp", "starlette", "uvicorn", "httpx", "av"):
        try:
            packages[name] = md.version(name)
        except md.PackageNotFoundError:
            packages[name] = "absent"

    return {
        "git_revision": _git("rev-parse", "--short", "HEAD"),
        "git_branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "git_dirty": bool(_git("status", "--porcelain")),
        "git_subject": _git("log", "-1", "--format=%s"),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "packages": packages,
    }


async def preflight(roster) -> Dict[str, Any]:
    from unitree_robot_agent.fleet import run_preflight, summarise

    results = await run_preflight(roster)
    return {
        "summary": summarise(results),
        "rows": [r.as_dict() if hasattr(r, "as_dict") else vars(r) for r in results],
    }


async def probe(roster, timeout: float, backend: str = "auto") -> Dict[str, Any]:
    """The decisive measurement.

    Imported from the standalone probe script rather than reimplemented: two
    copies of a topic list is how two tools end up disagreeing about what the
    firmware publishes, and the disagreement would be discovered at the worst
    possible moment.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from probe_webrtc_topics import probe_topics  # type: ignore

    out: Dict[str, Any] = {}
    for spec in roster.robots:
        try:
            out[spec.robot_id] = await probe_topics(
                spec.ip, serial=spec.serial_number or "", key=spec.aes_128_key or "",
                robot=getattr(spec, "model", "Go2") or "Go2", timeout=timeout,
                backend=backend, quiet=True,
            )
        except Exception as exc:  # noqa: BLE001 - a report must not die here
            out[spec.robot_id] = {"error": f"{type(exc).__name__}: {exc}", "rows": []}
    return out


async def live_sample(roster, backend: str = "auto") -> Dict[str, Any]:
    """Connect once and report what actually arrived.

    This is the part the topic probe cannot tell you: the probe asks whether a
    topic publishes at all, this asks whether the console's own read path works
    on this robot.
    """
    from unitree_robot_agent.fleet import FleetManager
    from unitree_fleet_web import ConsoleConfig, ConsoleService

    fleet = FleetManager(roster, backend=backend, audit_to_stderr=False)
    service = ConsoleService(fleet, config=ConsoleConfig(deadman_s=2.0))
    out: Dict[str, Any] = {}
    try:
        for spec in roster.robots:
            robot_id = spec.robot_id
            entry: Dict[str, Any] = {}
            try:
                await service.connect(robot_id)
                entry["connected"] = True
            except Exception as exc:  # noqa: BLE001
                out[robot_id] = {
                    "connected": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                continue

            try:
                await service.refresh_telemetry(robot_id)
            except Exception as exc:  # noqa: BLE001
                entry["telemetry_error"] = f"{type(exc).__name__}: {exc}"

            body = service.body(robot_id)
            sources = service.sources(robot_id)
            entry["body"] = body
            entry["sources"] = {
                item["id"]: item["status"] for item in sources["read"]
            }
            entry["pose"] = service.where(robot_id)
            entry["posture_belief"] = service.posture(robot_id).as_dict()
            entry["video"] = service.video_status(robot_id)
            out[robot_id] = entry

        try:
            marked = service.mark_sync()
            out["_sync"] = {"marked": marked.get("marked", []), "skipped": marked.get("skipped", [])}
        except Exception as exc:  # noqa: BLE001 - no pose is a finding, not a failure
            out["_sync"] = {"could_not_mark": f"{type(exc).__name__}: {exc}"}
    finally:
        try:
            await service.aclose()
        except Exception:  # noqa: BLE001
            pass
    return out


# ─── rendering ────────────────────────────────────────────────────────


def render(report: Dict[str, Any]) -> str:
    lines: List[str] = ["# 真机会话报告", ""]

    env = report.get("environment", {})
    lines += [
        f"- 代码版本：`{env.get('git_revision')}`"
        f"（{env.get('git_branch')}{'，有未提交改动' if env.get('git_dirty') else ''}）",
        f"- 提交：{env.get('git_subject')}",
        f"- Python {env.get('python')} · {env.get('platform')}",
        "- 依赖：" + " · ".join(f"{k} {v}" for k, v in (env.get("packages") or {}).items()),
        "",
    ]

    lines += ["## 机队（密钥已隐去）", "", "```json",
              json.dumps(report.get("roster", {}), ensure_ascii=False, indent=2), "```", ""]

    pf = report.get("preflight") or {}
    lines += ["## 只读体检", ""]
    summary = pf.get("summary") or {}
    if summary:
        lines.append(
            f"**{summary.get('count', 0)} 只** · 就绪 {summary.get('ready') or '无'} · "
            f"受阻 {summary.get('blocked') or '无'}"
        )
    else:
        lines.append("（未运行）")
    for row in pf.get("rows") or []:
        status = "OK " if row.get("ready") else "FAIL"
        lines.append(
            f"- **[{status}] {row.get('robot_id')}** {row.get('model','')} "
            f"{row.get('target','')} — {row.get('verdict') or row.get('reason') or ''}"
        )
        if row.get("detail"):
            lines.append(f"  - {row['detail']}")
    lines.append("")

    lines += ["## 话题探针 —— 决定后面能做什么", ""]
    probe_report = report.get("probe") or {}
    # A failure at the top of the probe leaves a bare string here rather than a
    # per-robot mapping, and a report that crashes while printing a failure is
    # worse than no report.
    if not isinstance(probe_report, dict) or "error" in probe_report:
        lines += [f"探针整体失败：`{probe_report}`", ""]
        probe_report = {}
    for robot_id, result in probe_report.items():
        if not isinstance(result, dict) or "error" in result:
            detail = result.get("error") if isinstance(result, dict) else result
            lines += [f"**{robot_id}**: {detail}", ""]
            continue
        delivered = result.get("delivered")
        total = result.get("total")
        lines.append(f"**{robot_id}**：{delivered}/{total} 有数据")
        lines.append("")
        lines.append("| topic | 结果 | 会解锁什么 |")
        lines.append("|---|---|---|")
        for row in result.get("rows") or []:
            mark = "**有数据**" if row.get("ok") else "静默"
            lines.append(f"| `{row.get('topic')}` | {mark} | {row.get('unlocks','')} |")
        lines.append("")
    if not report.get("probe"):
        lines += ["（未运行）", ""]

    sample = report.get("live_sample")
    lines += ["## 连上之后实际收到了什么", ""]
    if isinstance(sample, dict) and "error" in sample and "connected" not in str(sample):
        lines += [f"采样失败：`{sample}`", ""]
        sample = None
    if not sample:
        lines += ["（未运行 —— 用了 `--no-connect`，没有占用狗的 WebRTC 槽位）", ""]
    else:
        for robot_id, entry in sample.items():
            if robot_id.startswith("_"):
                continue
            lines.append(f"### {robot_id}")
            if not entry.get("connected"):
                lines += [f"- **连不上**：{entry.get('error')}", ""]
                continue
            body = entry.get("body") or {}
            lines.append(f"- 数据来源：`{entry.get('sources')}`")
            lines.append(f"- 位姿：`{entry.get('pose')}`")
            lines.append(f"- 姿态信念：`{entry.get('posture_belief')}`")
            lines.append(f"- 视频：`{entry.get('video')}`")
            if body:
                lines.append(
                    f"- 电量：`{(body.get('battery') or {}).get('soc_percent')}%`"
                    f" · 步态 `{body.get('gait')}`"
                    f" · 足端 `{body.get('feet')}`"
                    f" · 姿态 `{(body.get('attitude') or {}).get('pitch_deg')}°`"
                )
            else:
                lines.append("- **本体数据是空的** —— 见上面的数据来源清单")
            lines.append("")
        if "_sync" in sample:
            lines += [f"同步基准：`{sample['_sync']}`", ""]

    lines += [
        "---",
        "",
        "**这份报告不含 AES 密钥**（用的是 roster 自己的 `redacted()`）。",
        "可以直接整份贴回来。",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Gather everything a hardware session needs to report back."
    )
    parser.add_argument("--roster", default=None, help="roster JSON file or inline JSON")
    parser.add_argument("--timeout", type=float, default=6.0, help="seconds per topic")
    parser.add_argument("--no-connect", action="store_true",
                        help="skip the live sample; claims no WebRTC slot")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--backend", default="auto", choices=("auto", "webrtc", "mock"),
                        help="'mock' exercises this report with no robot attached")
    parser.add_argument("-o", "--output", default=None, help="write to this file")
    args = parser.parse_args()

    import os

    from unitree_robot_agent.errors import AgentError
    from unitree_robot_agent.fleet import Roster

    raw = args.roster or os.environ.get("UNITREE_AGENT_ROSTER") or ""
    if not raw:
        print(
            "没有配置 roster。用 --roster 或设置 UNITREE_AGENT_ROSTER。\n"
            "格式见 docs/REAL_DOG_SESSION.md 第 2 节。",
            file=sys.stderr,
        )
        return 2
    try:
        from unitree_robot_agent.fleet import load_roster

        roster = load_roster(raw)
    except Exception as exc:  # noqa: BLE001 - the message is the point
        print(f"roster 读不了：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    report: Dict[str, Any] = {
        "environment": environment(),
        # `redacted()` rather than a filter written here: a hand-rolled filter is
        # the one that leaks, and a pasted report is where a key would end up.
        "roster": roster.redacted(),
    }

    try:
        report["preflight"] = asyncio.run(preflight(roster))
    except Exception as exc:  # noqa: BLE001
        report["preflight"] = {"error": f"{type(exc).__name__}: {exc}"}

    try:
        report["probe"] = asyncio.run(probe(roster, args.timeout, args.backend))
    except Exception as exc:  # noqa: BLE001
        report["probe"] = {"error": f"{type(exc).__name__}: {exc}"}

    if not args.no_connect:
        try:
            report["live_sample"] = asyncio.run(live_sample(roster, args.backend))
        except Exception as exc:  # noqa: BLE001
            report["live_sample"] = {"error": f"{type(exc).__name__}: {exc}"}

    text = (
        json.dumps(report, ensure_ascii=False, indent=2, default=str)
        if args.json else render(report)
    )
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
        print(f"写到 {args.output}（{len(text)} 字节）")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
