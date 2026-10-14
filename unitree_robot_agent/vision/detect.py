"""Where the dogs are, from one camera watching both.

`markers.py` makes the thing you print. This reads it.

## The problem, restated because it decides the design

Two robots' odometry has no shared origin, so `sync.py` can measure how
*differently* they have moved and never where they are. One camera looking at
both has one frame and one coordinate system, and that is the whole reason to
put a tag on a dog's back.

## The floor is not where the tag is

A tag on a dog's back sits roughly 0.35 m above the floor. A homography
calibrated on the floor therefore reports the point where the *line of sight to
the tag* crosses the floor — not the floor point under the dog. The offset is
parallax, and its size is worth stating rather than hiding:

    error ≈ height × (lateral distance from the camera axis) / camera height

A camera 2.5 m up, a dog 1 m off-axis, a tag 0.35 m up: about **0.14 m** of
error. That is small next to "are the two dogs in formation" and large next to
"is it exactly on the line", and it is why `Calibration` carries the camera
height and reports the residual rather than implying a precision it does not
have.

Two ways to deal with it, and both are supported:

* **Calibrate on a plane at tag height.** Put the calibration targets on boxes
  the height of a dog's back and the parallax disappears by construction.
* **Calibrate on the floor** and set `tag_height_m` so the residual is
  estimated and reported alongside the position.

## What is not attempted

No lens calibration, no `solvePnP`, no 6-DOF pose. The question is "where are
the two dogs on the floor and which way are they facing", and a homography from
four known floor points answers exactly that with no intrinsics at all. A full
camera model would be more precise and would also be more to go wrong — and the
residual above would still be there, because it comes from the tag's height
rather than from the lens.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

Point = Tuple[float, float]


@dataclass
class Calibration:
    """Four known floor points, image pixels to metres.

    Order matters and is the same on both sides: the points are given in the
    order they appear round the arena, so a mapping that is rotated or mirrored
    is a mistake that shows up immediately rather than a plausible-looking
    coordinate system that is quietly wrong.
    """

    image_points: Sequence[Point]
    world_points: Sequence[Point]
    #: How high the tags sit above the floor the points were measured on. Zero
    #: when the calibration targets were themselves at tag height.
    tag_height_m: float = 0.0
    #: Camera height above the floor, for the parallax estimate. Zero disables
    #: the estimate rather than guessing at it.
    camera_height_m: float = 0.0

    def __post_init__(self) -> None:
        if len(self.image_points) != 4 or len(self.world_points) != 4:
            raise ValueError(
                "a homography needs exactly four points on each side; "
                f"got {len(self.image_points)} image and {len(self.world_points)} world"
            )
        self._matrix = None

    @property
    def matrix(self):
        import cv2
        import numpy as np

        if self._matrix is None:
            self._matrix = cv2.getPerspectiveTransform(
                np.float32(self.image_points), np.float32(self.world_points)
            )
        return self._matrix

    @property
    def mirrored(self) -> bool:
        """Whether the transform reverses orientation. **Not an error signal.**

        Kept because it is the honest record of an attempted guard, and because
        the next person to think of it should find out here rather than by
        breaking every calibration.

        A reversed click order does produce a valid homography that mirrors the
        arena — measured, same camera, same arena, only the order reversed:

            image point (1100, 100)   normal -> (4.0, 0.0)
                                      reversed -> (-0.0, 4.0)
            distance between two dogs  both -> 1.60 m

        The distances are identical and the directions are wrong, which is
        genuinely dangerous: the formation steers by direction.

        **But the determinant is the wrong way to detect it.** The image's y
        axis points down and the arena's +y points *away from the camera*, so
        the correct calibration is orientation-**reversing** and its determinant
        is negative. Measured against the order the page itself generates
        (`defaultWorldPoints`, "anticlockwise from the far-left corner"):

            correct order   (80,80),(720,80),(720,720),(80,720)
                          + (0,4),(4,4),(4,0),(0,0)
                          -> projects (720,80) to (4.0, 4.0), which is right
                          -> and this property says True

        It would have refused every legitimate calibration. Worse, refusing is
        the failure mode that looks like diligence.

        There is no independent check available: the world corners are
        *generated* from the arena size, not measured, so nothing in the data
        says which way round is right. The mitigation is the one that already
        exists — the panel prescribes the click order and the printed sheet
        shows it.
        """
        matrix = self.matrix
        return float(matrix[0][0]) * float(matrix[1][1]) - float(
            matrix[0][1]
        ) * float(matrix[1][0]) < 0

    def to_world(self, point: Point) -> Point:
        """Image pixels to metres on the calibrated plane."""
        import numpy as np

        matrix = self.matrix
        vector = np.array([point[0], point[1], 1.0], dtype=np.float64)
        result = matrix @ vector
        if abs(result[2]) < 1e-9:
            raise ValueError(f"{point} projects to infinity")
        return (float(result[0] / result[2]), float(result[1] / result[2]))

    #: Where the camera is, in arena metres. Optional, and used only to turn the
    #: parallax bound into a real number.
    camera_world_xy: Optional[Point] = None

    @property
    def _centre(self) -> Point:
        xs = [x for x, _y in self.world_points]
        ys = [y for _x, y in self.world_points]
        return (sum(xs) / len(xs), sum(ys) / len(ys))

    def parallax_m(self, world_point: Point) -> Optional[float]:
        """How far the reported point is from the floor point under the dog.

        The line of sight from a camera at height `H` through a tag at height
        `h` crosses the floor at `H / (H - h)` times the tag's true lateral
        distance `L`, so the error is

            L · H/(H-h) - L  =  L · h / (H - h)

        **The formula here used to be `L · h / H`** — `H` where `H - h` belongs —
        which under-reports by exactly `scale_factor`, the very quantity this
        class derives ten lines further down. For the worked case in
        `scale_factor` (h = 0.35, H = 2.5) that is 16% optimistic, and it fed
        `residual_m`, which is documented as an **upper bound** and decides
        `adequate`. A bound that is 16% low is not a bound.

        Reported rather than corrected, deliberately: correcting it needs the
        direction from the camera to the point, and a correction applied from
        the wrong geometry is worse than a stated error.

        The lateral distance is measured from the camera when the calibration
        says where the camera was, and from the **centre of the calibrated
        area** otherwise. That assumption is stated rather than hidden, and it
        is the one a fixed camera in a room usually satisfies.
        """
        if self.tag_height_m <= 0 or self.camera_height_m <= 0:
            return 0.0
        if self.camera_height_m <= self.tag_height_m:
            # The camera is at or below tag height, so the geometry this is the
            # small-angle form of does not exist — there is no parallax to
            # bound, only a configuration that cannot happen.
            #
            # **`None`, not `inf`.** The first version returned infinity, which
            # flowed into `residual_m` and `as_dict` and out through a
            # `JSONResponse` — whose `json.dumps(..., allow_nan=False)` raises.
            # The endpoint returned 500 and the whole arena panel was
            # unreachable. Exactly the class of bug this change was part of
            # fixing, introduced by the fix.
            return None
        origin = self.camera_world_xy or self._centre
        lateral = math.dist(world_point, origin)
        return self.tag_height_m * lateral / (self.camera_height_m - self.tag_height_m)

    @property
    def residual_m(self) -> Optional[float]:
        """The **worst** parallax over the calibrated area — an upper bound.

        Zero when the calibration was taken on a plane at tag height, which is
        the case with no error to report.

        An upper bound rather than a typical value, because the number is used
        to decide whether a floor calibration is good enough, and a typical
        value would answer that question optimistically. For a camera 2.5 m up
        over a 4 m arena with the tags 0.35 m up, it is about **0.40 m** —
        larger than a dog, which is the honest reason to put the calibration
        targets on boxes instead.
        """
        if self.tag_height_m <= 0 or self.camera_height_m <= 0:
            return 0.0
        values = [self.parallax_m(point) for point in self.world_points]
        if any(value is None for value in values):
            # "I cannot compute it" is not "it is fine". A bound that does not
            # exist is not a small bound.
            return None
        return max(values, default=0.0)

    @property
    def scale_factor(self) -> float:
        """How much a floor calibration inflates every distance.

        The parallax is not noise — it is a **radial expansion about the point
        under the camera**. A tag 0.35 m up, seen by a camera 2.5 m up, has its
        line of sight cross the floor at `H / (H - h)` times its true lateral
        distance: a factor of **1.16**, so a metre between two dogs reads as
        1.16 m.

        That is the number worth knowing, and it is mostly benign: both dogs are
        inflated by the same factor, so a formation that is the right shape
        reads as the right shape at the wrong size. What it breaks is comparing
        a camera measurement against an odometry one, or against a distance
        somebody measured with a tape.

        1.0 when nothing was calibrated wrong — a floor calibration from a very
        high camera, or a calibration taken at tag height.
        """
        if self.tag_height_m <= 0 or self.camera_height_m <= 0:
            return 1.0
        if self.camera_height_m <= self.tag_height_m:
            return float("inf")
        return self.camera_height_m / (self.camera_height_m - self.tag_height_m)

    @property
    def adequate(self) -> bool:
        """Whether the parallax is small enough to call the positions positions.

        The threshold is 15 cm, roughly half a dog's width: below that the two
        dogs' relative positions are still meaningful for formation, above it
        the numbers describe the floor under where the tags *appear* to be.
        """
        bound = self.residual_m
        # A calibration whose bound cannot be computed is not one to act on.
        return bound is not None and bound <= 0.15

    def as_dict(self) -> Dict[str, Any]:
        return {
            "image_points": [list(p) for p in self.image_points],
            "world_points": [list(p) for p in self.world_points],
            "tag_height_m": self.tag_height_m,
            "camera_height_m": self.camera_height_m,
            # `None` when the geometry does not exist, which serialises as
            # `null`. It used to be `inf`, which does not serialise at all.
            "residual_m": (
                round(self.residual_m, 3) if self.residual_m is not None else None
            ),
            "adequate": self.adequate,
            "scale_factor": (
                round(self.scale_factor, 3)
                if self.scale_factor != float("inf") else None
            ),
            "camera_world_xy": list(self.camera_world_xy) if self.camera_world_xy else None,
            # **`mirrored` is deliberately not here.** It is inverted with
            # respect to the convention the page generates (see the property),
            # so writing it into the calibration file and the calibrate response
            # would ship a flag that means the opposite of its name to whoever
            # reads it next. It stays as a documented negative result, not as
            # data.
        }

    @staticmethod
    def from_dict(raw: Dict[str, Any]) -> "Calibration":
        return Calibration(
            image_points=[tuple(p) for p in raw["image_points"]],
            world_points=[tuple(p) for p in raw["world_points"]],
            tag_height_m=float(raw.get("tag_height_m", 0.0)),
            camera_height_m=float(raw.get("camera_height_m", 0.0)),
            camera_world_xy=(
                tuple(raw["camera_world_xy"]) if raw.get("camera_world_xy") else None
            ),
        )


@dataclass
class DogPose:
    """One dog, in the arena's own metres."""

    robot_id: str
    tag_id: int
    x: float
    y: float
    #: Degrees, 0 = +x, counter-clockwise. The tag's own "up" edge is the nose.
    heading_deg: float
    #: How many pixels across the tag was in this frame — the honest measure of
    #: how much to trust the numbers beside it.
    tag_px: float = 0.0
    #: Estimated parallax error from the tag's height, in metres. `None` when
    #: the calibration cannot compute one — see `Calibration.parallax_m`.
    parallax_m: Optional[float] = 0.0

    @property
    def position(self) -> Point:
        return (self.x, self.y)

    @property
    def legible(self) -> bool:
        """Whether the tag was big enough in this frame to trust its **id**.

        **This is what `tag_px` is for, and nothing checked it.** The value was
        computed, documented as "the honest measure of how much to trust the
        numbers beside it", put on the wire — and compared to nothing. A tag
        detected at 40 px was treated exactly like one at 200 px.

        That matters more than a position being slightly off, because the
        failure below the floor is not a refusal, it is a **misread id**: two
        tags four bits apart in `DICT_APRILTAG_36h11` decode to each other under
        blur, and a misread id puts one dog's position on the other. The
        formation then steers each dog by the wrong error, from data that looks
        entirely plausible. `markers.py` says the same thing about the same
        number: "低于这个数检测器不是失败，是开始瞎猜 id".

        The floor is the module's own — `MIN_PX_PER_CELL × CELLS_ACROSS`, 64 px
        for a 36h11 tag at 8 cells — and it is deliberately above the measured
        detection cliff (σ≤1.8 px, about 40 px), because the cliff is where
        *detection* fails and this is about *identity*.
        """
        from .markers import CELLS_ACROSS, MIN_PX_PER_CELL

        floor = MIN_PX_PER_CELL * CELLS_ACROSS
        # A pose with no measurement at all is not legible; one from a synthetic
        # scene that did not fill in the field should not be silently trusted
        # either.
        return self.tag_px >= floor

    def as_dict(self) -> Dict[str, Any]:
        return {
            "robot_id": self.robot_id,
            "tag_id": self.tag_id,
            "x": round(self.x, 3),
            "y": round(self.y, 3),
            "heading_deg": round(self.heading_deg, 1),
            "tag_px": round(self.tag_px, 1),
            "legible": self.legible,
            "parallax_m": (
                round(self.parallax_m, 3) if self.parallax_m is not None else None
            ),
        }


@dataclass
class FrameResult:
    """What one frame contained."""

    poses: Dict[str, DogPose] = field(default_factory=dict)
    #: Tag ids seen that the manifest does not describe. Reported rather than
    #: dropped: an unexpected tag is usually a second session's marker left on
    #: the floor, and silently ignoring it hides a real setup mistake.
    unknown_tags: List[int] = field(default_factory=list)
    #: Robot ids the manifest expects that this frame did not contain.
    missing: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "poses": {rid: pose.as_dict() for rid, pose in self.poses.items()},
            "unknown_tags": self.unknown_tags,
            "missing": self.missing,
        }


def _detector():
    """OpenCV's AprilTag detector, configured for small tags in a big frame."""
    import cv2

    from .markers import FAMILY
    import numpy as np

    dictionary = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, FAMILY))
    parameters = cv2.aruco.DetectorParameters()
    # Sub-pixel corner refinement: at 40-80 px a tag's corners land between
    # pixels, and the corner positions are what the position is computed from.
    parameters.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    parameters.adaptiveThreshWinSizeMin = 5
    parameters.adaptiveThreshWinSizeMax = 35
    parameters.adaptiveThreshWinSizeStep = 6
    # Small tags at a distance have few cells, so a stricter minimum perimeter
    # rejects them before they are ever decoded.
    parameters.minMarkerPerimeterRate = 0.01
    parameters.maxMarkerPerimeterRate = 4.0
    parameters.polygonalApproxAccuracyRate = 0.05
    return cv2.aruco.ArucoDetector(dictionary, parameters)


def detect_markers(frame) -> Tuple[List[Any], Optional[Any]]:
    """Raw corner sets and ids. Separated so a caller can log a frame it could
    not read without going through the whole pipeline."""
    corners, ids, _rejected = _detector().detectMarkers(frame)
    return corners, ids


def _heading_deg(corners, calibration: Calibration) -> float:
    """The direction the dog's nose points, in degrees, 0 = +x.

    **The convention, because it is not self-evident.** The tag's own frame has
    `u` along the nose and `v` to the nose's right, so its four corners are
    forward-left, forward-right, back-right, back-left. OpenCV returns them in
    the printed tag's own order, so the vector from the back-left corner to the
    forward-left one **is** the nose direction, and taking its angle in arena
    metres gives the heading directly.

    The arrow printed on the sheet is what ties that to the physical dog: it
    marks which edge of the sticker goes towards the head. A tag stuck on 90°
    out reports a heading 90° out, and nothing here can tell.

    Both ends are mapped through the homography and the angle is taken in
    **world** metres rather than in pixels. Taking it in pixels would be wrong
    whenever the camera is not square-on: a tag pointing north would read as
    pointing north-east at the edge of a wide frame.
    """
    points = corners.reshape(4, 2)
    bottom_left = calibration.to_world((float(points[3][0]), float(points[3][1])))
    top_left = calibration.to_world((float(points[0][0]), float(points[0][1])))
    dx, dy = top_left[0] - bottom_left[0], top_left[1] - bottom_left[1]
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return 0.0
    return math.degrees(math.atan2(dy, dx))


def locate(
    frame,
    calibration: Calibration,
    manifest: Dict[str, Any],
) -> FrameResult:
    """Every dog the manifest knows about, in arena metres, from one frame.

    The **centre** of the tag is the reference point. An earlier version used
    the bottom-left corner on the grounds that the corner nearest the dog's body
    has the least parallax from the tag's height — which is true, and worth
    7.5 cm on a 15 cm tag, against the cost of reporting a position nobody
    expects: "where is the dog" means the middle of the dog, not the corner of a
    sticker.
    """
    corners, ids = detect_markers(frame)
    result = FrameResult()

    by_tag = {
        int(marker["tag_id"]): marker for marker in manifest.get("markers", [])
    }
    expected = {marker["robot_id"] for marker in manifest.get("markers", [])}

    if ids is None:
        result.missing = sorted(expected)
        return result

    seen: set = set()
    for corner_set, tag_id in zip(corners, ids.flatten()):
        tag_id = int(tag_id)
        marker = by_tag.get(tag_id)
        if marker is None:
            result.unknown_tags.append(tag_id)
            continue

        points = corner_set.reshape(4, 2)
        world_corners = [
            calibration.to_world((float(point[0]), float(point[1]))) for point in points
        ]
        anchor = (
            sum(point[0] for point in world_corners) / 4.0,
            sum(point[1] for point in world_corners) / 4.0,
        )
        span = float(
            max(
                math.dist(points[0], points[1]),
                math.dist(points[1], points[2]),
            )
        )
        result.poses[marker["robot_id"]] = DogPose(
            robot_id=marker["robot_id"],
            tag_id=tag_id,
            x=anchor[0],
            y=anchor[1],
            heading_deg=_heading_deg(corner_set, calibration),
            tag_px=span,
            parallax_m=calibration.parallax_m(anchor),
        )
        seen.add(marker["robot_id"])

    result.unknown_tags.sort()
    result.missing = sorted(expected - seen)
    return result


def separation(result: FrameResult) -> Optional[Dict[str, Any]]:
    """How far apart the dogs are, and how differently they are facing.

    The number this whole feature exists to produce, and the one odometry
    cannot: **in one frame, in one coordinate system**, rather than as a
    difference between two independent estimates.
    """
    ids = sorted(result.poses)
    if len(ids) < 2:
        return None
    a, b = result.poses[ids[0]], result.poses[ids[1]]
    distance = math.dist(a.position, b.position)
    yaw = abs((a.heading_deg - b.heading_deg + 180.0) % 360.0 - 180.0)
    return {
        "a": a.robot_id,
        "b": b.robot_id,
        "distance_m": round(distance, 3),
        "yaw_spread_deg": round(yaw, 1),
        # A formation command needs a bearing, not just a distance: "close the
        # gap" is ambiguous without knowing which way to close it.
        "bearing_deg_from_a_to_b": round(
            math.degrees(math.atan2(b.y - a.y, b.x - a.x)), 1
        ),
    }


# ─── calibration targets ──────────────────────────────────────────────


def render_calibration_targets(
    points: Sequence[Point], *, size_m: float = 0.06, canvas_px: int = 1200
):
    """A printable plan of where to put the four calibration markers.

    A diagram rather than four separate tags, because the thing that goes wrong
    with a homography is the *order*: four correct points given in the wrong
    order produce a matrix that looks fine and reports a mirrored arena.
    """
    import cv2
    import numpy as np

    image = np.full((canvas_px, canvas_px, 3), 255, dtype=np.uint8)
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    pad = canvas_px * 0.12

    def to_px(point: Point) -> Tuple[int, int]:
        span_x = max(max_x - min_x, 1e-6)
        span_y = max(max_y - min_y, 1e-6)
        scale = min((canvas_px - 2 * pad) / span_x, (canvas_px - 2 * pad) / span_y)
        return (
            int(pad + (point[0] - min_x) * scale),
            int(canvas_px - pad - (point[1] - min_y) * scale),  # y up on the plan
        )

    for index, point in enumerate(points):
        px = to_px(point)
        cv2.circle(image, px, 12, (0, 0, 0), 2, cv2.LINE_AA)
        cv2.circle(image, px, 4, (0, 0, 0), -1, cv2.LINE_AA)
        cv2.putText(
            image, f"{index}: ({point[0]:.2f}, {point[1]:.2f})",
            (px[0] + 18, px[1] - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
            (0, 0, 0), 2, cv2.LINE_AA,
        )
    return image
