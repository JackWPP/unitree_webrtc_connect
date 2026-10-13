#!/usr/bin/env python3
"""Which topics does the WebRTC link actually deliver?

`constants.py` lists about fifty `RTC_TOPIC` names. That is a *menu of names*,
not a promise: whether the firmware publishes a given topic over the data
channel is a separate question, and the answer differs by model and by firmware
version. Two examples already measured on real hardware:

* `rt/lf/lowstate` and `rt/multiplestate` arrive promptly.
* `rt/sportmodestate` was silent for eight seconds on a Go2 whose firmware
  requires `data2=3` — while the sibling project's notes suggest
  `rt/lf/sportmodestate` is the one that answers on that generation.

Building a feature on a topic nobody checked is how you get a panel that is
empty on hardware and full under the mock. This turns the question into one
command.

**Read-only.** It subscribes and reads; it never publishes a command, sends no
SDP of its own, and takes no motor action. Safe to run while someone else is
driving the robot.

    python scripts/probe_webrtc_topics.py --ip 192.168.31.188

Run it once per robot generation and write the result down. Every feature that
depends on a topic should cite that run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

#: Topics worth asking about, grouped by what they would unlock. Anything not
#: here is either already known to work or not useful to a console.
CANDIDATES = (
    # ── already confirmed on hardware; here so a regression is visible ──
    ("rt/lf/lowstate", "battery, IMU, motor states, foot force", "confirmed"),
    ("rt/multiplestate", "brightness, volume, obstacle-avoid switch", "confirmed"),
    # ── the state topics; which one answers depends on the firmware ──
    ("rt/lf/sportmodestate", "fsm_id (humanoid), mode, velocity", "state"),
    ("rt/sportmodestate", "gait_type, body height (quadruped)", "state"),
    # ── what a map would need ──
    ("rt/utlidar/robot_pose", "absolute pose — would give a live map", "pose"),
    ("rt/utlidar/lidar_state", "LiDAR on/off and health", "lidar"),
    ("rt/utlidar/voxel_map_compressed", "the point cloud", "lidar"),
    ("rt/mapping/grid_map", "2D occupancy grid", "map"),
    # ── everything else that might be free ──
    ("rt/servicestate", "service modes", "misc"),
    ("rt/uwbstate", "UWB ranging", "misc"),
    ("rt/gas_sensor", "gas sensor", "misc"),
    ("rt/audiohub/player/state", "what the speaker is playing", "audio"),
    ("rt/selftest", "self-test result", "misc"),
)


async def probe(
    ip: str, serial: str, key: str, timeout: float, robot: str, backend: str
) -> int:
    from unitree_robot_agent.config import AgentConfig
    from unitree_robot_agent.runtime import create_runtime

    config = AgentConfig(
        robot=robot,
        connection="sta-l",
        ip=ip,
        serial_number=serial,
        aes_128_key=key,
        backend=backend or "auto",
        state_dir=Path("/tmp/webrtc-topic-probe"),
        audit_enabled=False,
    )
    runtime = create_runtime(config)

    print(f"connecting to {ip} …")
    try:
        await runtime.session.ensure_connected()
    except Exception as exc:  # noqa: BLE001 - the failure is the report
        print(f"  CONNECT FAILED: {type(exc).__name__}: {exc}")
        return 2
    print("  connected\n")

    rows = []
    try:
        for topic, purpose, group in CANDIDATES:
            started = time.monotonic()
            try:
                payload = await runtime.session.read_topic(topic, timeout=timeout)
            except Exception as exc:  # noqa: BLE001
                rows.append((topic, group, "error", f"{type(exc).__name__}", 0.0, purpose, None))
                continue
            elapsed = time.monotonic() - started

            if payload is None:
                rows.append((topic, group, "silent", "", elapsed, purpose, None))
            else:
                keys = sorted(payload.keys())[:8] if isinstance(payload, dict) else []
                rows.append((topic, group, "OK", "", elapsed, purpose, keys))
    finally:
        await runtime.aclose()

    width = max(len(topic) for topic, *_ in rows)
    print(f"{'topic'.ljust(width)}  {'result':7} {'wait':>6}  what it would unlock")
    print(f"{'-' * width}  {'-' * 7} {'-' * 6}  {'-' * 40}")
    for topic, group, status, note, elapsed, purpose, keys in rows:
        mark = {"OK": "OK", "silent": "silent", "error": "error"}[status]
        print(f"{topic.ljust(width)}  {mark:7} {elapsed:5.1f}s  {purpose}")
        if keys:
            print(f"{' ' * width}            keys: {', '.join(keys)}")
        if note:
            print(f"{' ' * width}            {note}")

    working = [row for row in rows if row[2] == "OK"]
    print()
    print(f"{len(working)}/{len(rows)} topics delivered.")
    print("Silent is not an error: the firmware simply does not publish it over")
    print("this link. Do not build a feature on a silent row without another way")
    print("to get the data.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ip", required=True, help="robot IP")
    parser.add_argument("--sn", default="", help="serial number (required by V3 firmware)")
    parser.add_argument("--key", default="", help="per-device AES key for data2=3")
    parser.add_argument("--robot", default="Go2", help="Go2 | R1")
    parser.add_argument("--timeout", type=float, default=3.0, help="seconds to wait per topic")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--backend", default="",
        help="auto | webrtc | mock. `mock` exercises the probe itself with no robot",
    )
    args = parser.parse_args()
    return asyncio.run(
        probe(args.ip, args.sn, args.key, args.timeout, args.robot, args.backend)
    )


if __name__ == "__main__":
    raise SystemExit(main())
