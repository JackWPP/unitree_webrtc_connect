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

        # The decisive questions, asked while the link is open rather than
        # reconstructed from the raw sample afterwards.
        try:
            out["_readiness"] = await readiness(service, roster)
        except Exception as exc:  # noqa: BLE001 - a failed check is a finding
            out["_readiness"] = [{
                "id": "readiness",
                "question": "就绪判定本身",
                "verdict": "unknown",
                "evidence": f"{type(exc).__name__}: {exc}",
                "matters": "这是报告自己的问题，不是机器人的。",
            }]

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


# ─── readiness: the decisive questions, answered ──────────────────────
#
# A session report that only lists versions and topics leaves the operator to
# work out what it means. Each of these answers one question that changes what
# the console can do, and says which way it went and what it was based on.
#
# **All read-only.** Nothing here commands the robot; the checks that would
# (`--motion`) are separate and refused without the flag, because the first
# thing an operator does with a strange dog should not be to make it walk.


async def readiness(service, roster, *, attempts: int = 3) -> List[Dict[str, Any]]:
    """What this robot can and cannot do, measured rather than assumed."""
    checks: List[Dict[str, Any]] = []

    def add(check_id, question, verdict, evidence, matters, robot_id=""):
        checks.append({
            "id": check_id,
            "robot_id": robot_id,
            "question": question,
            "verdict": verdict,
            "evidence": evidence,
            "matters": matters,
        })

    for spec in roster.robots:
        robot_id = spec.robot_id
        runtime = service.fleet.get(robot_id)

        # ── odometry: the one that decides what `robot_walk` means ──
        #
        # `robot_move` takes a duration, which is a guess; `robot_walk` closes
        # its loop on `position`. If the robot publishes no position, every
        # measured walk silently becomes the timed guess it was meant to
        # replace — and it says so in `reason`, but only if somebody asks.
        position = None
        for _ in range(attempts):
            try:
                position = await runtime.motion.read_position(timeout=2.0)
            except Exception:  # noqa: BLE001 - absence is the finding
                position = None
            if position is not None:
                break
        add(
            "odometry", "机器人发布里程计（position）吗？",
            "yes" if position is not None else "no",
            f"读到 {position}" if position is not None else
            f"连读 {attempts} 次都没有 position —— 不是「没动」，是「没说」",
            "有 → robot_walk 走的是量出来的距离；没有 → 它会退回按时长猜，"
            "并在 reason 里写明 no_odometry。正方形的每一步都会变成猜的。",
            robot_id=robot_id,
        )

        # ── the gait source the goal asked about ──
        yaw = None
        try:
            yaw = await runtime.motion.read_yaw(timeout=2.0)
        except Exception:  # noqa: BLE001
            yaw = None
        add(
            "imu", "能读到躯干 IMU 的偏航角吗？",
            "yes" if yaw is not None else "no",
            # No literal topic name here: `test_the_probe_is_not_reimplemented`
            # guards against this report growing its own copy of the probe's
            # topic list, and a blunt substring check cannot tell a topic *list*
            # from a topic *mentioned in a sentence*. The guard is worth keeping,
            # so the sentence is the thing that changes.
            f"yaw={yaw:.4f} rad" if yaw is not None else "低层状态topic里没有 imu_state.rpy",
            "有 → robot_turn 是闭环的（转到角度为止）；"
            "没有 → 闭环转向直接拒绝（code=no_imu），只剩按时长的摇杆。",
            robot_id=robot_id,
        )

        # ── fault visibility ──
        # Read from the runtime, not from the console: the console renders the
        # fault badge from the fleet snapshot, and there is no `service.faults`.
        faults = list(runtime.faults.active())
        authoritative = bool(runtime.faults.authoritative)
        add(
            "faults", "机器人发过完整的故障快照吗？",
            "yes" if authoritative else "no",
            f"{len(faults)} 条活动故障，快照{'已' if authoritative else '未'}收全",
            "没有 → 面板显示「尚未收到完整故障快照」而不是「无故障」。"
            "**空列表只有在 authoritative 时才是好消息**，否则它只是「还不知道」。",
            robot_id=robot_id,
        )

        # ── firmware generation, which differs between the two dogs ──
        try:
            auth = runtime.auth.describe()
        except Exception as exc:  # noqa: BLE001
            auth = {"error": f"{type(exc).__name__}: {exc}"}

        # ── video ──
        video = service.video_status(robot_id)
        add(
            "video", "视频流能解出帧吗？",
            "yes" if video.get("ok") else (
                "unknown" if video.get("state") in (None, "starting") else "no"
            ),
            json.dumps(video, ensure_ascii=False)[:200],
            "没有视频 → 场地视图没有输入，编队和标定都用不了。",
            robot_id=robot_id,
        )

        # **What is knowable here is the configuration, not the firmware.** The
        # first version called this `no` when no key was cached, which reads as
        # "this dog does not need one" — a claim about the robot made from a
        # claim about a file. Whether the handshake wants `data2=3` is only
        # knowable by trying it, and `preflight` is what tries it.
        credentials = auth.get("credentials") or {}
        key_present = bool(getattr(spec, "aes_128_key", ""))
        add(
            "firmware", "这只狗的密钥配置（两台狗不是同一代固件）",
            "info",
            f"roster 里有密钥: {key_present} · 缓存的会话: "
            f"{bool(auth.get('cached_session'))} · "
            f"邮箱/密码已配: {bool(credentials.get('email'))}/{bool(credentials.get('password'))}",
            "**真机上需要看 preflight 的握手结果**，不是这里。"
            "固件 ≥1.1.15 需要 data2=3（每台一个 AES 密钥）；"
            "固件更老则 dev.key 留空是对的。**两台狗在这一项上不同。**",
            robot_id=robot_id,
        )

        # ── identity: the routing bug's hardware check ──
        #
        # The console once addressed the default robot for every call while
        # reporting the one that was clicked. Offline that is fixed and tested;
        # on hardware the check is that two dogs report two different identities.
        identity = {
            "robot_id": robot_id,
            "serial": getattr(spec, "serial_number", "") or getattr(spec, "sn", ""),
            "ip": getattr(spec, "ip", ""),
        }
        add(
            "identity", "这只狗的身份（用来确认命令发对了对象）",
            "info",
            json.dumps(identity, ensure_ascii=False),
            "**两台狗要给出不同的身份。** 控制台曾经对每次调用都驱动默认那只、"
            "却报告你点的那只；离线上已修好并有回归测试，真机上靠这一条确认。",
            robot_id=robot_id,
        )

    # ── cross-robot: the check that needs both ──
    if len(roster.robots) > 1:
        # **Identity, not position.** The first version compared positions and
        # reported a failure when both dogs sat at `(0.0, 0.0)` — which is
        # correct: each odometry has its own origin, so two dogs at their own
        # starting points read the same numbers. A check that cries wolf on the
        # normal case is worse than no check, because somebody will chase it.
        #
        # What must differ is which robot the two calls reached, and the serial
        # number is what says so.
        identities = {}
        for spec in roster.robots:
            identities[spec.robot_id] = (
                getattr(spec, "serial_number", "") or getattr(spec, "sn", "")
                or getattr(spec, "ip", "")
            )
        distinct = len(set(identities.values())) == len(identities)
        add(
            "two_robots", "两台狗是两个不同的对象吗？",
            "yes" if distinct else "no",
            json.dumps(identities, ensure_ascii=False),
            "身份相同 → 两次调用可能打到了同一个运行时，**正是 `ctx.runtime` "
            "那个缺陷的形状**。注意：两台狗位置读数相同是**正常的**"
            "（各自的里程计有自己的原点），所以这里比的是身份。",
        )

    return checks


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

    # Readiness first, because it is the part that changes what to do next.
    checks = (report.get("live_sample") or {}).get("_readiness") or []
    if checks:
        icons = {"yes": "✅", "no": "❌", "unknown": "❔", "info": "·"}
        lines += ["## 就绪判定", ""]
        for check in checks:
            icon = icons.get(check.get("verdict"), "·")
            who = check.get("robot_id")
            label = f"`{who}` · " if who else ""
            lines += [
                f"### {icon} {label}{check.get('question')}",
                "",
                f"- **结论：{check.get('verdict')}** — {check.get('evidence')}",
                f"- 影响：{check.get('matters')}",
                "",
            ]
        no = [c for c in checks if c.get("verdict") == "no"]
        if no:
            lines += [
                f"> **{len(no)} 项为「否」**，各有各的后果，见上。"
                "「否」不等于坏了 —— 有的是固件代次，有的是这台狗的配置。",
                "",
            ]

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
