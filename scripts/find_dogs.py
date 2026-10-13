#!/usr/bin/env python
"""Find the robots on this network and print a roster you can paste.

    python scripts/find_dogs.py
    python scripts/find_dogs.py --json
    python scripts/find_dogs.py -o /tmp/dogs.json

Three sources, tried at once because they are independent:

* **multicast** — the library's own discovery, and the only one that gives a
  serial-to-address mapping directly. Measured to never answer on the network
  this was built against, so it is not relied on.
* **a sweep of the local /24** for the signaling ports — what actually found
  the dog there.
* **the account's device list** — serial numbers and aliases without touching a
  robot. Needs `--email`/`--password` or `$UNITREE_EMAIL`/`$UNITREE_PASSWORD`,
  because the cloud session is per-process and there is no token file.

When multicast is silent, the serial numbers and the addresses are joined by
**trying each device's key against each address**: `data2 === 3` firmware
encrypts its handshake with the key for its own serial number, so the key that
decrypts identifies the robot. That is a measurement, not a guess.

Nothing here claims a WebRTC client slot, so a robot in use by the official app
is still found.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def render(result: dict) -> str:
    lines = ["# 找到的机器狗", ""]
    lines.append(
        f"本机 {result.get('local_ip') or '?'} · 网段 {result.get('subnet') or '?'}"
    )
    lines.append("")

    found = result.get("found") or []
    if not found:
        lines += ["**一只都没找到。**", ""]
    else:
        lines += ["| IP | 序列号 | 别名 | 怎么认出来的 |", "|---|---|---|---|"]
        why = {"multicast": "多播应答", "key": "密钥试配", "": "只知道有信令端口"}
        for item in found:
            lines.append(
                f"| `{item['ip']}` | {item.get('sn') or '**未知**'} "
                f"| {item.get('alias') or ''} | {why.get(item.get('matched_by', ''), '')} |"
            )
        lines.append("")

    account = result.get("account") or []
    if account:
        lines += ["## 账号下的设备", "", "| 序列号 | 别名 | 在线 | 有密钥 |", "|---|---|---|---|"]
        for device in account:
            online = {True: "在线", False: "离线", None: "未知"}.get(device.get("online"))
            lines.append(
                f"| `{device.get('sn')}` | {device.get('alias') or ''} | {online} "
                f"| {'是' if device.get('has_key') else '否'} |"
            )
        lines.append("")

    roster = result.get("roster") or {}
    if roster.get("robots"):
        lines += ["## 可以直接用的 roster", "", "```json",
                  json.dumps(roster, ensure_ascii=False, indent=2), "```", ""]
    else:
        lines += [
            "## 还没有能用的 roster",
            "",
            "需要有序列号才能取到密钥。没有认出来的地址，序列号只能从",
            "狗的标签、官方 App 或账号设备列表里拿 —— 加 `--email/--password` 试试最后一条。",
            "",
        ]

    if result.get("address_hint"):
        lines += [f"> {result['address_hint']}", ""]

    lines += ["## 每个来源做了什么", ""]
    for note in result.get("notes") or []:
        lines.append(f"- {note}")
    lines += [
        "",
        "---",
        "",
        "**没有占用任何狗的 WebRTC 槽位** —— 扫描是 TCP 连接，握手在发 SDP 之前就停了。",
    ]
    return "\n".join(lines)


async def run(args) -> dict:
    from unitree_robot_agent.fleet.discovery import discover

    return await discover(
        device_type=args.device_type,
        model=args.model,
        subnet=args.subnet or "",
        timeout=args.timeout,
        sweep_lan=not args.no_sweep,
        use_account=not args.no_account,
        email=args.email or "",
        password=args.password or "",
        region=args.region,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--device-type", default="Go2")
    parser.add_argument("--model", default="Go2")
    parser.add_argument("--subnet", default="", help="default: this machine's /24")
    parser.add_argument("--timeout", type=float, default=2.0, help="multicast listen")
    parser.add_argument("--email", default="", help="for the account device list")
    parser.add_argument("--password", default="")
    parser.add_argument("--region", default="cn")
    parser.add_argument("--no-sweep", action="store_true", help="multicast only")
    parser.add_argument("--no-account", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("-o", "--output", default=None)
    args = parser.parse_args()

    result = asyncio.run(run(args))
    text = json.dumps(result, ensure_ascii=False, indent=2) if args.json else render(result)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
        print(f"写到 {args.output}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
