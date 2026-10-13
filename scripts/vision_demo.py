#!/usr/bin/env python
"""See the two dogs, or see what the pipeline would see without them.

    python scripts/vision_demo.py --markers            # make the printable sheet
    python scripts/vision_demo.py --plan               # the calibration diagram
    python scripts/vision_demo.py --simulate out.png   # a synthetic arena
    python scripts/vision_demo.py --frame shot.jpg     # read a real photo

The `--simulate` mode is the one that matters between sessions: it draws two
dogs at known places, runs the detector over the result, and prints what it
found beside what it drew. If that ever disagrees the pipeline is broken, and it
is broken somewhere a screenshot shows and a log does not.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

#: The arena the demo calibrates: 4 m square, camera looking square-on.
DEMO_CORNERS_IMAGE = [(80.0, 80.0), (720.0, 80.0), (720.0, 720.0), (80.0, 720.0)]
DEMO_CORNERS_WORLD = [(0.0, 4.0), (4.0, 4.0), (4.0, 0.0), (0.0, 0.0)]


def _demo_calibration(tag_height_m: float = 0.35, camera_height_m: float = 2.5):
    from unitree_robot_agent.vision import Calibration

    return Calibration(
        image_points=DEMO_CORNERS_IMAGE,
        world_points=DEMO_CORNERS_WORLD,
        tag_height_m=tag_height_m,
        camera_height_m=camera_height_m,
    )


def simulate(out_path: Path, *, dog1=(1.0, 2.0, 0.0), dog2=(3.0, 2.0, 180.0)):
    """Draw two dogs where they are said to be, then read them back."""
    import cv2
    import numpy as np

    from unitree_robot_agent.vision.detect import locate, separation
    from unitree_robot_agent.vision.markers import default_markers, render_tag

    calibration = _demo_calibration()
    markers = default_markers(["dog1", "dog2"])
    frame = np.full((800, 800), 255, dtype=np.uint8)

    for marker, (x, y, heading) in zip(markers, (dog1, dog2)):
        tag = render_tag(marker)
        size = 0.30  # 48 px at 160 px/m — above the measured detection limit
        import math

        theta = math.radians(heading)
        nose = (math.cos(theta), math.sin(theta))
        right = (math.sin(theta), -math.cos(theta))
        half = size / 2

        def corner(forward, side):
            return (x + nose[0] * forward + right[0] * side,
                    y + nose[1] * forward + right[1] * side)

        world = [corner(half, -half), corner(half, half), corner(-half, half), corner(-half, -half)]
        inverse = np.linalg.inv(calibration.matrix)
        projected = []
        for wx, wy in world:
            v = inverse @ np.array([wx, wy, 1.0])
            projected.append((v[0] / v[2], v[1] / v[2]))
        source = np.float32([[0, 0], [tag.shape[1], 0],
                             [tag.shape[1], tag.shape[0]], [0, tag.shape[0]]])
        m = cv2.getPerspectiveTransform(source, np.float32(projected))
        drawn = cv2.warpPerspective(tag, m, (800, 800), flags=cv2.INTER_LINEAR,
                                    borderValue=255)
        frame = np.minimum(frame, drawn)

    manifest = {
        "markers": [
            {"tag_id": m.tag_id, "robot_id": m.robot_id, "size_m": m.size_m}
            for m in markers
        ]
    }
    result = locate(frame, calibration, manifest)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_path), frame)

    drawn = {"dog1": dog1, "dog2": dog2}
    lines = [f"图像写到 {out_path}", ""]
    lines.append(f"{'狗':6} {'画在':>18} {'读回':>18} {'误差':>8}")
    for robot_id, pose in sorted(result.poses.items()):
        tx, ty, th = drawn[robot_id]
        error = ((pose.x - tx) ** 2 + (pose.y - ty) ** 2) ** 0.5
        lines.append(
            f"{robot_id:6} ({tx:5.2f},{ty:5.2f},{th:6.1f}°) "
            f"({pose.x:5.2f},{pose.y:5.2f},{pose.heading_deg:6.1f}°) {error:7.3f}m"
        )
    for robot_id in result.missing:
        lines.append(f"{robot_id:6} 没检出来")
    gap = separation(result)
    if gap:
        lines += ["", f"间距 {gap['distance_m']} m   朝向差 {gap['yaw_spread_deg']}°"]
    lines += ["", f"地面标定的最坏视差 {calibration.residual_m:.2f} m"
                  f"（距离放大 ×{calibration.scale_factor:.3f}）"]
    return "\n".join(lines), result


def read_frame(path: Path, calibration, manifest):
    import cv2

    from unitree_robot_agent.vision.detect import locate, separation

    frame = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if frame is None:
        return f"读不了 {path}", None
    result = locate(frame, calibration, manifest)
    lines = [f"{path}: {frame.shape[1]}x{frame.shape[0]}", ""]
    for robot_id, pose in sorted(result.poses.items()):
        lines.append(
            f"  {robot_id}: ({pose.x:.2f}, {pose.y:.2f}) 朝 {pose.heading_deg:.1f}°"
            f"  标记 {pose.tag_px:.0f}px"
        )
    if result.unknown_tags:
        lines.append(f"  未登记的标记: {result.unknown_tags}")
    if result.missing:
        lines.append(f"  没看到: {result.missing}")
    gap = separation(result)
    if gap:
        lines.append(f"  间距 {gap['distance_m']} m  朝向差 {gap['yaw_spread_deg']}°")
    return "\n".join(lines), result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--markers", metavar="DIR", nargs="?", const="/tmp/dog-markers",
                        help="write the printable marker sheet")
    parser.add_argument("--plan", metavar="OUT", nargs="?", const="/tmp/arena-plan.png",
                        help="write the calibration diagram")
    parser.add_argument("--simulate", metavar="OUT", help="a synthetic arena, read back")
    parser.add_argument("--frame", metavar="IMAGE", help="read a real photo")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.markers:
        from unitree_robot_agent.vision.markers import default_markers, write_markers

        result = write_markers(Path(args.markers), default_markers(["dog1", "dog2"]))
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json
              else f"写到 {args.markers}\n  打印：{result['manifest']['print_instructions']}")
        return 0

    if args.plan:
        import cv2

        from unitree_robot_agent.vision.detect import render_calibration_targets

        plan = render_calibration_targets(DEMO_CORNERS_WORLD)
        cv2.imwrite(args.plan, plan)
        print(f"标定图写到 {args.plan} —— 四个点要按编号顺序量，顺序错了会得到镜像的场地")
        return 0

    if args.simulate:
        text, result = simulate(Path(args.simulate))
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2)
              if args.json else text)
        return 0

    if args.frame:
        text, _ = read_frame(Path(args.frame), _demo_calibration(),
                             {"markers": []})
        print(text)
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
