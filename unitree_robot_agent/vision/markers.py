"""Printable fiducial markers, one per dog, for a camera that can see them both.

## Why this shape of solution

Two dogs' odometry does not share an origin — `position` is relative to wherever
each estimator started and `yaw` to whatever heading it happened to be facing.
`sync.py` works around that by measuring *change since a reference the operator
marks*, and the module says plainly what that cannot do: it can tell you how
differently the two have moved, never where they are.

A camera looking at both of them at once does not have that problem. One frame,
one coordinate system, both robots in it. That is the entire reason to do this,
and it is why the marker is on the **back**: a dog cannot see its own back, so
the camera has to be a third thing watching the pair.

## The marker

`DICT_APRILTAG_36h11` — 36 data bits with an 11-bit Hamming distance, which is
the standard robust choice: it tolerates a misread corner and still identifies
itself, and it will not confuse one tag for another at the ranges a dog walks.
The alternative, `16h5`, needs fewer pixels but has a 5-bit distance and starts
inventing tag ids out of blur.

Physical size is the parameter that actually decides whether this works, and it
is **measured rather than quoted**. The experiment (`tests/test_vision_markers.py`
reproduces it) renders a tag at decreasing pixel widths over a scene carrying
what a real camera adds — a perspective tilt, rotation, **optical blur in scene
pixels**, sensor noise, and JPEG:

| optical blur | smallest tag that still detects reliably |
|---|---|
| σ 0.8 px | ~40 px wide (5 px per cell) |
| σ 1.2 px | ~40 px wide |
| σ 1.8 px | ~40 px wide |
| σ 2.5 px | ~56 px wide (7 px per cell) |

The binding constraint is the **blur relative to a cell**, not the cell count on
its own — which is why the widely quoted "10-16 px per cell" is conservative and
why "it detected at 8 px total in a clean render" is worthless. The rules of
thumb are measured against different assumptions; this one is measured against
this pipeline.

`MIN_PX_PER_CELL = 8.0` is set above the measured cliff of 5 px/cell rather than
at it, because a real arena adds motion blur and uneven lighting that the model
does not carry.

Hence `DEFAULT_SIZE_M = 0.15`, which fits on a Go2's back. The generated sheet
states the size, because a tag printed at "fit to page" is a tag at the wrong
size and the failure looks like a detection bug rather than a printer setting.

## Orientation

A planar tag gives its own rotation from the order of its four corners, so the
dog's heading comes for free — but only if the tag is stuck on the right way
round. The sheet prints an arrow and the words 狗头方向 ("nose direction") along
the edge that must point towards the dog's head, because a tag rotated 90° on
the back reports a heading rotated 90°, and nothing in the detection will say so.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

#: 36 data bits, 11-bit minimum Hamming distance. The robust standard.
FAMILY = "DICT_APRILTAG_36h11"

#: Printed width of the tag's black square, in metres.
#:
#: 15 cm fits on a Go2's back and reads at the far edge of a small arena with a
#: 1080p camera. See the module docstring for the arithmetic; the short version
#: is that a tag under about 8 px per cell at the far edge stops being detected
#: and starts being guessed at.
DEFAULT_SIZE_M = 0.15

#: Cells across a 36h11 tag including its border.
#:
#: Measured, not assumed: rendering the tag and counting runs across its middle
#: row gives cells of 1772/8 = 221 px, so 36h11 is 6 data cells plus one border
#: cell each side. An earlier version of this file said 10, which made every
#: range estimate 25% optimistic.
CELLS_ACROSS = 8

#: Pixels per cell below which detection stops being reliable.
#:
#: **Measured.** See the module docstring for the table: with optical blur up to
#: σ 1.8 scene px a tag detects reliably at 5 px per cell, and at σ 2.5 it needs
#: 7. This is set above the cliff rather than at it, because a real arena adds
#: motion blur and uneven lighting that the model does not carry.
MIN_PX_PER_CELL = 8.0

#: Print resolution. 300 dpi is what a normal printer does well and is four
#: times more than a 15 cm tag needs, which leaves room for the sheet to be
#: scaled slightly by a print dialog without hurting detection.
DPI = 300

MARKER_COLOURS = {
    # Named so two printed sheets can be told apart on a table, and so the
    # operator can say "the blue one is dog1" out loud. The colour is *outside*
    # the tag and plays no part in detection — a tinted tag would change its
    # contrast, and contrast is what the detector lives on.
    "dog1": "#1f6feb",
    "dog2": "#d29922",
    "dog3": "#3ecf8e",
    "dog4": "#ef4444",
}


@dataclass
class Marker:
    """One tag, and the things needed to print and place it."""

    tag_id: int
    robot_id: str
    label: str
    size_m: float = DEFAULT_SIZE_M
    colour: str = "#1f6feb"

    @property
    def size_mm(self) -> float:
        return round(self.size_m * 1000, 1)

    @property
    def pixels(self) -> int:
        """Side length of the rendered tag's black square, at `DPI`."""
        return int(round(self.size_m / 0.0254 * DPI))

    def as_dict(self) -> Dict[str, Any]:
        return {
            "tag_id": self.tag_id,
            "robot_id": self.robot_id,
            "label": self.label,
            "size_m": self.size_m,
            "size_mm": self.size_mm,
            "family": FAMILY,
            "colour": self.colour,
            "pixels": self.pixels,
        }


def default_markers(robot_ids: Sequence[str], size_m: float = DEFAULT_SIZE_M) -> List[Marker]:
    """One marker per robot, in the order given.

    Tag id equals index, so dog1 is tag 0 and dog2 is tag 1 — a mapping simple
    enough to hold in your head while crouching behind a robot, which is where
    it will be needed.
    """
    out: List[Marker] = []
    for index, robot_id in enumerate(robot_ids):
        out.append(Marker(
            tag_id=index,
            robot_id=robot_id,
            label=f"{robot_id}",
            size_m=size_m,
            colour=MARKER_COLOURS.get(robot_id, "#8b949e"),
        ))
    return out


def _dictionary():
    import cv2

    return cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, FAMILY))


def render_tag(marker: Marker, *, border_cells: int = 1):
    """The tag as an image array, black square only, no white quiet zone.

    `border_cells` is the tag's own internal border, part of the pattern. The
    white quiet zone the detector also needs is added by the sheet, because it
    is printing margin rather than pattern.

    The keyword is `borderBits` in OpenCV 5 and `borderWidth` in 4.x, and both
    accept the number of cells rather than a width — the rename is not a change
    of meaning.
    """
    import cv2

    generate = cv2.aruco.generateImageMarker
    for keyword in ("borderBits", "borderWidth"):
        try:
            return generate(_dictionary(), marker.tag_id, marker.pixels, **{keyword: border_cells})
        except TypeError:
            continue
    return generate(_dictionary(), marker.tag_id, marker.pixels)


def render_tile(marker: Marker, *, quiet_zone_mm: float = 25.0) -> "Any":
    """One tag, at true size, with its quiet zone, direction arrow and caption.

    Sizing the tile to a single page rather than stacking every tag down one
    long strip is the difference between "print this" and "print this, then
    work out how to fit 514 mm onto 297 mm of A4".
    """
    import cv2
    import numpy as np

    quiet_px = int(round(quiet_zone_mm / 25.4 * DPI))
    tag = render_tag(marker)
    side = tag.shape[0]

    arrow_h = int(side * 0.22)
    caption_h = int(side * 0.16)
    tiled = np.full((side + quiet_px * 2, side + quiet_px * 2), 255, dtype=np.uint8)
    tiled[quiet_px:quiet_px + side, quiet_px:quiet_px + side] = tag

    canvas = np.full(
        (arrow_h + tiled.shape[0] + caption_h, tiled.shape[1]), 255, dtype=np.uint8
    )
    canvas[arrow_h:arrow_h + tiled.shape[0], :] = tiled

    # The arrow, pointing at the tag's top edge, and nothing else. Words beside
    # it overflowed the tile at any size readable from a metre away; the caption
    # below carries the meaning instead.
    cx = canvas.shape[1] // 2
    thickness = max(4, int(side * 0.014))
    cv2.arrowedLine(
        canvas, (cx, int(arrow_h * 0.88)), (cx, int(arrow_h * 0.10)),
        (0, 0, 0), thickness, cv2.LINE_AA, tipLength=0.40,
    )

    # The caption, sized from the tile rather than in absolute points: the tag is
    # ~1800 px across at 300 dpi, so a font scale that reads on a screen is
    # invisible on the print and one that reads on the print is enormous on a
    # screen.
    base = arrow_h + tiled.shape[0]
    scale = side / 1100.0
    cv2.putText(
        canvas, f"tag {marker.tag_id}  -  {marker.robot_id}",
        (quiet_px // 2, base + int(caption_h * 0.42)),
        cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0),
        max(2, thickness // 2), cv2.LINE_AA,
    )
    cv2.putText(
        canvas, f"{marker.size_mm:.0f} mm   {FAMILY}   arrow -> nose",
        (quiet_px // 2, base + int(caption_h * 0.88)),
        cv2.FONT_HERSHEY_SIMPLEX, scale * 0.72, (0, 0, 0),
        max(1, thickness // 3), cv2.LINE_AA,
    )
    return canvas


def render_placement_sheet(
    markers: Sequence[Marker], *, quiet_zone_mm: float = 25.0
) -> "Any":
    """Every tile stacked, for a printer that takes a long sheet or for reading
    on screen. `render_tile` is what gets printed one page at a time."""
    import numpy as np

    tiles = [render_tile(marker, quiet_zone_mm=quiet_zone_mm) for marker in markers]
    if not tiles:
        return np.full((10, 10), 255, dtype=np.uint8)

    gap = int(round(0.012 / 25.4 * DPI))
    width = max(tile.shape[1] for tile in tiles)
    height = sum(tile.shape[0] for tile in tiles) + gap * (len(tiles) + 1)
    sheet = np.full((height, width + gap * 2), 255, dtype=np.uint8)
    y = gap
    for tile in tiles:
        sheet[y:y + tile.shape[0], gap:gap + tile.shape[1]] = tile
        y += tile.shape[0] + gap
    return sheet


def write_markers(
    out_dir: Path,
    markers: Sequence[Marker],
    *,
    quiet_zone_mm: float = 25.0,
) -> Dict[str, Any]:
    """Write one PNG per tag, a combined sheet, and a manifest.

    The manifest is what the detector reads to know which tag is which robot and
    how big the tags physically are. Without it the mapping lives in somebody's
    head, and it will be wrong on the second session.
    """
    import cv2

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    written: List[str] = []
    for marker in markers:
        # Two files per tag, on purpose. The `_print` one is the page: true
        # size, quiet zone, arrow. The bare one is the pattern on its own, which
        # is what a detector test or a different layout wants.
        page = out_dir / f"tag_{marker.tag_id}_{marker.robot_id}_print.png"
        cv2.imwrite(str(page), render_tile(marker, quiet_zone_mm=quiet_zone_mm))
        written.append(page.name)

        bare = out_dir / f"tag_{marker.tag_id}_{marker.robot_id}.png"
        cv2.imwrite(str(bare), render_tag(marker))
        written.append(bare.name)

    sheet_path = out_dir / "markers_sheet.png"
    cv2.imwrite(str(sheet_path), render_placement_sheet(markers, quiet_zone_mm=quiet_zone_mm))
    written.append(sheet_path.name)

    manifest = {
        "family": FAMILY,
        "size_m": markers[0].size_m if markers else DEFAULT_SIZE_M,
        "dpi": DPI,
        "quiet_zone_mm": quiet_zone_mm,
        "cells_across": CELLS_ACROSS,
        "print_instructions": (
            "按 100% 打印，不要「适合页面」。"
            f"黑方块边长必须是 {markers[0].size_mm if markers else 150:.0f}mm —— "
            "打印完用尺子量一下。贴的时候让箭头指向狗头。"
        ),
        "markers": [marker.as_dict() for marker in markers],
    }
    (out_dir / "markers.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"directory": str(out_dir), "files": written, "manifest": manifest}


def load_manifest(path: Path) -> Dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def tag_pixels(marker: Marker, *, pixels_per_metre: float) -> float:
    """How many pixels wide this tag is at the far edge of the arena."""
    return marker.size_m * pixels_per_metre


def pixels_per_cell(marker: Marker, *, pixels_per_metre: float) -> float:
    return tag_pixels(marker, pixels_per_metre=pixels_per_metre) / CELLS_ACROSS


def usable(marker: Marker, *, pixels_per_metre: float) -> bool:
    return pixels_per_cell(marker, pixels_per_metre=pixels_per_metre) >= MIN_PX_PER_CELL


def reading_range_m(marker: Marker, *, width_px: int, hfov_deg: float) -> float:
    """The farthest this marker still reads reliably with this camera.

    Solved from the camera rather than from a pixels-per-metre figure, because
    that figure is meaningless without the distance it was measured at — an
    earlier version of this function took one and returned 1.3 m for a setup
    that demonstrably worked at 3 m.

    Pixels per metre falls as 1/distance, so the answer is simply the distance
    at which the tag's width in pixels equals the minimum:

        size * width / (2 * d * tan(hfov/2)) = MIN_PX_PER_CELL * CELLS_ACROSS
    """
    if width_px <= 0 or marker.size_m <= 0:
        return 0.0
    half_angle = math.tan(math.radians(hfov_deg) / 2.0)
    if half_angle <= 0:
        return 0.0
    needed = MIN_PX_PER_CELL * CELLS_ACROSS
    return marker.size_m * width_px / (2.0 * half_angle * needed)


def required_width_px(marker: Marker, *, distance_m: float, hfov_deg: float) -> int:
    """The camera resolution this marker needs to read at `distance_m`.

    The other direction round, and the one a buying decision needs: "will the
    webcam I already own do it" is a different question from "how big a tag do
    I print".
    """
    if distance_m <= 0 or marker.size_m <= 0:
        return 0
    half_angle = math.tan(math.radians(hfov_deg) / 2.0)
    needed = MIN_PX_PER_CELL * CELLS_ACROSS
    return int(math.ceil(2.0 * distance_m * half_angle * needed / marker.size_m))


def pixels_per_metre_for(width_px: int, distance_m: float, hfov_deg: float) -> float:
    """How many pixels one metre occupies at `distance_m`.

    Enough to answer the only question that matters before printing: is my
    camera, at my arena's far edge, going to read a tag this size?
    """
    if distance_m <= 0 or width_px <= 0:
        return 0.0
    arena_width_m = 2.0 * distance_m * math.tan(math.radians(hfov_deg) / 2.0)
    if arena_width_m <= 0:
        return 0.0
    return width_px / arena_width_m


def pixel_budget(marker: Marker, *, pixels_per_metre: float) -> Dict[str, Any]:
    """The arithmetic, exposed so a session can check it before printing.

    Prints the numbers rather than a verdict alone: when it says "too small" the
    operator needs to know by how much, because the fix might be a bigger tag,
    a closer camera, or a shorter arena, and those cost very different amounts.
    """
    tag_px = tag_pixels(marker, pixels_per_metre=pixels_per_metre)
    per_cell = pixels_per_cell(marker, pixels_per_metre=pixels_per_metre)
    ok = usable(marker, pixels_per_metre=pixels_per_metre)
    needed = MIN_PX_PER_CELL * CELLS_ACROSS
    return {
        "size_m": marker.size_m,
        "tag_px": round(tag_px, 1),
        "px_per_cell": round(per_cell, 1),
        "usable": ok,
        "minimum_px": needed,
        "minimum_px_for_this_size": needed,
        "note": (
            "每个格子 ≥%.0f 像素才稳（实测下限是 5，留了余量）。"
            "低于这个数检测器不是失败，是开始瞎猜 id —— 而猜错 id 会把一只狗的位置"
            "算到另一只身上，比检测不到更糟。" % MIN_PX_PER_CELL
            if ok else
            "太小了：远端只有 %.1f px/格，需要 %.1f。改用更大的标记、更近的相机、"
            "或更小的场地。" % (per_cell, MIN_PX_PER_CELL)
        ),
    }
