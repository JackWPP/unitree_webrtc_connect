"""Reading the markers back: where the dogs are, in one coordinate system.

The test that matters is `test_a_tag_placed_at_a_known_spot_is_found_there`.
Everything before it is arithmetic, and everything after it is about the ways a
real setup goes wrong.

It is a **round trip**: put a tag at a known place in arena metres, project it
into an image through the inverse of the calibration, detect it, and check the
position and heading come back. No camera, no robot, and it exercises the same
code a live frame does.
"""

from __future__ import annotations

import math

import pytest

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

from unitree_robot_agent.vision.detect import (  # noqa: E402
    Calibration,
    DogPose,
    FrameResult,
    detect_markers,
    locate,
    separation,
)
from unitree_robot_agent.vision.markers import (  # noqa: E402
    Marker,
    default_markers,
    render_tag,
)

#: A 4 m square arena, viewed from a camera looking square-on at the middle.
#: The image is 800x800 and the arena is inset, so the projection is a plain
#: scale and the expected numbers can be worked out by hand.
ARENA = Calibration(
    image_points=[(80.0, 80.0), (720.0, 80.0), (720.0, 720.0), (80.0, 720.0)],
    world_points=[(0.0, 4.0), (4.0, 4.0), (4.0, 0.0), (0.0, 0.0)],
)
# Image y grows downward and world y grows upward, so the image's top edge is
# the arena's far edge. Getting this round the wrong way is the mistake the
# calibration-target diagram exists to prevent.


def _manifest(robot_ids=("dog1", "dog2")):
    return {
        "family": "DICT_APRILTAG_36h11",
        "size_m": 0.15,
        "markers": [
            {"tag_id": m.tag_id, "robot_id": m.robot_id, "size_m": m.size_m}
            for m in default_markers(list(robot_ids))
        ],
    }


def _place(frame, marker: Marker, calibration: Calibration, x: float, y: float,
           heading_deg: float, *, size_m: float = 0.30):
    """Draw `marker` into `frame` as if it lay on the arena floor at (x, y).

    The tag is placed at 0.30 m across in arena metres rather than the printed
    0.15 m, because the arena is 4 m wide across 640 px — 160 px per metre — and
    a 0.15 m tag would be 24 px, below the detection limit this project measured.
    The size on the floor is not what is under test here; the *position* is.
    """
    tag = render_tag(marker)
    half = size_m / 2.0
    theta = math.radians(heading_deg)
    # The nose direction in arena metres, and the tag's right-hand side. In a
    # y-up world, "right of the nose" is the nose rotated by -90°.
    nose = (math.cos(theta), math.sin(theta))
    right = (math.sin(theta), -math.cos(theta))

    def world_corner(forward: float, side: float):
        return (
            x + nose[0] * forward + right[0] * side,
            y + nose[1] * forward + right[1] * side,
        )

    # The tag image's own corners, in its own order: top-left, top-right,
    # bottom-right, bottom-left. On the printed sheet the arrow points at the top
    # edge, so "top" is the nose — which makes this assignment the same statement
    # as the arrow, rather than a second convention that could disagree with it.
    ordered_world = [
        world_corner(half, -half),   # top-left    = forward, left
        world_corner(half, half),    # top-right   = forward, right
        world_corner(-half, half),   # bottom-right= back, right
        world_corner(-half, -half),  # bottom-left = back, left
    ]

    # World -> image is the inverse of the calibration.
    inverse = np.linalg.inv(calibration.matrix)
    projected = []
    for wx, wy in ordered_world:
        vector = inverse @ np.array([wx, wy, 1.0])
        projected.append((vector[0] / vector[2], vector[1] / vector[2]))

    source = np.float32(
        [[0, 0], [tag.shape[1], 0], [tag.shape[1], tag.shape[0]], [0, tag.shape[0]]]
    )
    matrix = cv2.getPerspectiveTransform(source, np.float32(projected))
    return cv2.warpPerspective(
        tag, matrix, (frame.shape[1], frame.shape[0]),
        flags=cv2.INTER_LINEAR, borderValue=255,
    )


def _scene(placements, *, size: int = 800):
    """A white floor with the given tags drawn on it."""
    frame = np.full((size, size), 255, dtype=np.uint8)
    for marker, x, y, heading in placements:
        drawn = _place(np.full((size, size), 255, dtype=np.uint8), marker,
                       ARENA, x, y, heading)
        frame = np.minimum(frame, drawn)
    return frame


# ─── the arithmetic ───────────────────────────────────────────────────


def test_a_calibration_maps_the_four_points_to_themselves():
    for image, world in zip(ARENA.image_points, ARENA.world_points):
        assert ARENA.to_world(image) == pytest.approx(world, abs=1e-6)


def test_the_middle_of_the_image_is_the_middle_of_the_arena():
    assert ARENA.to_world((400.0, 400.0)) == pytest.approx((2.0, 2.0), abs=1e-6)


def test_a_calibration_needs_exactly_four_points():
    """Three points is not a homography, and four is the number that has to be
    measured on the floor. Failing loudly beats a matrix built from a subset."""
    with pytest.raises(ValueError, match="exactly four"):
        Calibration(image_points=[(0, 0), (1, 0), (1, 1)], world_points=[(0, 0), (1, 0), (1, 1)])


def test_flipping_the_point_order_is_a_different_calibration():
    """Which is the whole reason the printed diagram numbers its points: four
    correct points in the wrong order produce a plausible mirrored arena."""
    mirrored = Calibration(
        image_points=list(reversed(ARENA.image_points)),
        world_points=list(ARENA.world_points),
    )
    # A non-centre point, because the centre of a square is a fixed point under
    # a mirror and would have agreed with the correct calibration.
    assert mirrored.to_world((200.0, 200.0)) != pytest.approx(
        ARENA.to_world((200.0, 200.0)), abs=0.05
    )


# ─── the round trip ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "x,y,heading",
    [
        (2.0, 2.0, 0.0),
        (1.0, 3.0, 90.0),
        (3.0, 1.0, -90.0),
        (2.5, 2.5, 180.0),
        (1.5, 1.5, 45.0),
    ],
)
def test_a_tag_placed_at_a_known_spot_is_found_there(x, y, heading):
    """The round trip: project it in, detect it out, compare.

    The tolerance is 6 cm on position and 4° on heading. At 160 px per metre
    that is about 10 px of position error, which is what sub-pixel corner
    refinement on a 48 px tag actually delivers — not a number chosen to make
    the test pass.
    """
    markers = default_markers(["dog1"])
    frame = _scene([(markers[0], x, y, heading)])

    result = locate(frame, ARENA, _manifest(["dog1"]))

    assert "dog1" in result.poses, "the tag was not found at all"
    pose = result.poses["dog1"]
    assert pose.x == pytest.approx(x, abs=0.06), f"x came back as {pose.x}"
    assert pose.y == pytest.approx(y, abs=0.06), f"y came back as {pose.y}"
    # The heading is the tag's own "up" edge, which the printed arrow points at
    # the dog's nose.
    delta = (pose.heading_deg - heading + 180.0) % 360.0 - 180.0
    assert abs(delta) < 4.0, f"heading {pose.heading_deg} vs {heading}"


def test_two_dogs_are_found_in_one_frame_with_one_coordinate_system():
    """The entire reason this feature exists: odometry cannot put the two dogs
    in the same frame, and one camera cannot help but."""
    markers = default_markers(["dog1", "dog2"])
    frame = _scene([
        (markers[0], 1.0, 2.0, 0.0),
        (markers[1], 3.0, 2.0, 180.0),
    ])

    result = locate(frame, ARENA, _manifest(["dog1", "dog2"]))

    assert set(result.poses) == {"dog1", "dog2"}
    assert result.missing == []
    gap = separation(result)
    assert gap["distance_m"] == pytest.approx(2.0, abs=0.1)
    # Facing each other: their headings differ by 180°.
    assert gap["yaw_spread_deg"] == pytest.approx(180.0, abs=5.0)
    # ...and the bearing from one to the other is along +x.
    assert gap["bearing_deg_from_a_to_b"] == pytest.approx(0.0, abs=5.0)


def test_the_separation_is_measured_not_inferred():
    """Two dogs 1 m apart read as 1 m, at several places in the arena. If this
    drifted with position, the calibration would be wrong in a way that only
    shows up at the edges."""
    markers = default_markers(["dog1", "dog2"])
    for x in (1.0, 2.0, 3.0):
        frame = _scene([
            (markers[0], x - 0.5, 2.0, 0.0),
            (markers[1], x + 0.5, 2.0, 0.0),
        ])
        gap = separation(locate(frame, ARENA, _manifest(["dog1", "dog2"])))
        assert gap["distance_m"] == pytest.approx(1.0, abs=0.08), f"at x={x}"


# ─── the ways a real setup goes wrong ─────────────────────────────────


def test_a_missing_dog_is_named_rather_than_omitted():
    """A dog that is not in frame is not a dog at (0, 0). The distinction is
    the one the whole project keeps making, and here it is the difference
    between "out of shot" and "at the origin"."""
    markers = default_markers(["dog1", "dog2"])
    frame = _scene([(markers[0], 2.0, 2.0, 0.0)])

    result = locate(frame, ARENA, _manifest(["dog1", "dog2"]))

    assert set(result.poses) == {"dog1"}
    assert result.missing == ["dog2"]


def test_an_empty_frame_reports_every_dog_missing():
    result = locate(np.full((800, 800), 255, dtype=np.uint8), ARENA,
                    _manifest(["dog1", "dog2"]))
    assert result.poses == {}
    assert result.missing == ["dog1", "dog2"]
    assert separation(result) is None


def test_an_unexpected_tag_is_reported_not_ignored():
    """A tag the manifest does not describe is usually another session's marker
    left on the floor, and dropping it silently hides a real setup mistake."""
    markers = default_markers(["dog1", "dog2"])
    frame = _scene([(markers[1], 2.0, 2.0, 0.0)])  # tag 1 only

    result = locate(frame, ARENA, _manifest(["dog1"]))  # manifest knows tag 0

    assert result.unknown_tags == [1]
    assert result.poses == {}


def test_one_dog_alone_gives_no_separation():
    markers = default_markers(["dog1"])
    frame = _scene([(markers[0], 2.0, 2.0, 0.0)])
    assert separation(locate(frame, ARENA, _manifest(["dog1"]))) is None


def test_a_tag_seen_edge_on_is_not_reported_as_a_position():
    """A tag at a grazing angle gives corners that are almost collinear, and a
    homography through a degenerate quadrilateral produces a number that looks
    like a position and is not one."""
    marker = default_markers(["dog1"])[0]
    tag = render_tag(marker)
    frame = np.full((800, 800), 255, dtype=np.uint8)
    # A sliver: 200 px wide, 4 px tall.
    squashed = cv2.resize(tag, (200, 4), interpolation=cv2.INTER_AREA)
    frame[400:404, 300:500] = squashed

    result = locate(frame, ARENA, _manifest(["dog1"]))
    # Either it is not detected at all, or whatever comes back is a real
    # position in the arena — never an infinity or a NaN.
    for pose in result.poses.values():
        assert math.isfinite(pose.x) and math.isfinite(pose.y)
        assert -1.0 <= pose.x <= 6.0
        assert -1.0 <= pose.y <= 6.0


# ─── the parallax the floor calibration cannot avoid ──────────────────


def test_a_floor_calibration_reports_its_own_error():
    """A tag 0.35 m up seen by a camera 2.5 m up has its line of sight cross the
    floor further out than the dog is. The calibration says so rather than
    implying a precision it does not have."""
    floor = Calibration(
        image_points=ARENA.image_points, world_points=ARENA.world_points,
        tag_height_m=0.35, camera_height_m=2.5,
    )
    assert floor.residual_m > 0.15, (
        "a 4 m arena with a 2.5 m camera and tags 0.35 m up has a parallax "
        "larger than half a dog — reporting it as adequate would be a lie"
    )
    assert floor.adequate is False
    # A metre between two dogs reads as 1.16 m.
    assert floor.scale_factor == pytest.approx(1.163, abs=0.01)


def test_calibrating_at_tag_height_has_no_error_at_all():
    """The fix, and the reason the calibration targets should go on boxes the
    height of a dog's back rather than on the floor."""
    at_height = Calibration(
        image_points=ARENA.image_points, world_points=ARENA.world_points,
        tag_height_m=0.0,
    )
    assert at_height.residual_m == 0.0
    assert at_height.scale_factor == 1.0
    assert at_height.adequate is True


def test_a_higher_camera_is_better_but_not_enough():
    """Worth knowing before somebody buys a taller tripod expecting it to fix
    the problem. It reduces the error and does not remove it."""
    errors = [
        Calibration(image_points=ARENA.image_points, world_points=ARENA.world_points,
                    tag_height_m=0.35, camera_height_m=h).residual_m
        for h in (1.5, 2.5, 4.0, 8.0)
    ]
    assert errors == sorted(errors, reverse=True), "a higher camera did not help"
    assert errors[-1] > 0, "a higher camera did not remove the error, and should not claim to"


def test_the_scale_factor_is_a_ratio_not_a_guess():
    """It falls out of the geometry: the line of sight to a tag h up crosses the
    floor at H/(H-h) times its lateral distance."""
    calibration = Calibration(
        image_points=ARENA.image_points, world_points=ARENA.world_points,
        tag_height_m=0.5, camera_height_m=2.0,
    )
    assert calibration.scale_factor == pytest.approx(2.0 / 1.5)


def test_a_calibration_round_trips_through_json():
    original = Calibration(
        image_points=ARENA.image_points, world_points=ARENA.world_points,
        tag_height_m=0.35, camera_height_m=2.5,
    )
    restored = Calibration.from_dict(original.as_dict())
    assert restored.to_world((400.0, 400.0)) == pytest.approx(
        original.to_world((400.0, 400.0))
    )
    assert restored.tag_height_m == 0.35


# ─── an attempted guard that was wrong, and why ───────────────────────
#
# A reversed click order produces a valid homography that mirrors the arena:
# the same camera, the same arena, only the order reversed, and every distance
# comes out identical while every direction is wrong. That is real and it is
# dangerous — the formation steers by direction.
#
# A guard was written for it using the sign of the transform's determinant. It
# was **inverted**: the image's y axis points down and the arena's +y points
# away from the camera, so the *correct* calibration is orientation-reversing.
# Measured against the order the page generates (`defaultWorldPoints`), it
# reported the correct order as mirrored and would have refused every
# legitimate calibration.
#
# These tests pin the convention so the mistake cannot be made twice.


def test_the_convention_the_page_actually_generates():
    """`defaultWorldPoints(4, 4)` returns `[[0,4],[4,4],[4,0],[0,0]]` —
    "anticlockwise from the far-left corner, with arena +y away from the
    camera" — and the panel says to click 左上 → 右上 → 右下 → 左下.

    That pairing is correct, and it projects the image's top-right corner to the
    arena's far-right corner.
    """
    from unitree_robot_agent.vision.detect import Calibration

    image = [(80, 80), (720, 80), (720, 720), (80, 720)]
    world = [(0, 4), (4, 4), (4, 0), (0, 0)]
    calibration = Calibration(image, world)

    # The four by four check: image corners land on the arena corners they name.
    assert calibration.to_world((80, 80)) == pytest.approx((0.0, 4.0), abs=0.05)
    assert calibration.to_world((720, 80)) == pytest.approx((4.0, 4.0), abs=0.05)
    assert calibration.to_world((720, 720)) == pytest.approx((4.0, 0.0), abs=0.05)
    assert calibration.to_world((80, 720)) == pytest.approx((0.0, 0.0), abs=0.05)


def test_the_correct_calibration_is_orientation_reversing():
    """**The fact that made the guard wrong.**

    Image y points down, arena +y points away from the camera, so the correct
    map flips orientation and its determinant is negative. A guard that refuses
    negative determinants refuses everything.
    """
    from unitree_robot_agent.vision.detect import Calibration

    calibration = Calibration(
        [(80, 80), (720, 80), (720, 720), (80, 720)],
        [(0, 4), (4, 4), (4, 0), (0, 0)],
    )
    assert calibration.mirrored is True, (
        "if this is ever False, the axis conventions have changed and the "
        "reasoning in `Calibration.mirrored` needs revisiting"
    )


def test_a_reversed_click_order_produces_identical_distances():
    """Why the failure is dangerous rather than obvious: **the numbers are the
    right size.** An operator comparing separations would see nothing wrong."""
    import math

    from unitree_robot_agent.vision.detect import Calibration

    world = [(0, 4), (4, 4), (4, 0), (0, 0)]
    correct = Calibration([(80, 80), (720, 80), (720, 720), (80, 720)], world)
    reversed_order = Calibration(
        [(80, 80), (80, 720), (720, 720), (720, 80)], world
    )

    def separation(calibration):
        return math.dist(
            calibration.to_world((300, 400)), calibration.to_world((500, 400))
        )

    assert separation(correct) == pytest.approx(separation(reversed_order), abs=0.05)
    # ...and the places are different, which is the half nothing reports.
    assert correct.to_world((720, 80)) != pytest.approx(
        reversed_order.to_world((720, 80)), abs=0.5
    )


# ─── the parallax bound ───────────────────────────────────────────────


def test_the_parallax_matches_the_geometry_scale_factor_derives():
    """**Two formulas for one quantity, in one class, disagreeing.**

    `parallax_m` returned `L·h/H`. The line of sight from a camera at `H`
    through a tag at `h` crosses the floor at `H/(H-h)` times the tag's lateral
    distance, so the error is `L·h/(H-h)` — which is `parallax_m` times
    `scale_factor`, the quantity this same class derives ten lines further down.

    The existing tests asserted `> 0.15` and the ratio separately, so they passed
    with either formula. This one pins the number.
    """
    from unitree_robot_agent.vision.detect import Calibration

    corners = [(0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0)]
    calibration = Calibration(
        [(0, 0), (100, 0), (100, 100), (0, 100)],
        corners,
        tag_height_m=0.35,
        camera_height_m=2.5,
    )

    h, H, lateral = 0.35, 2.5, 1.5

    # The bound is the *worst* parallax over the calibrated area, and the centre
    # is the origin when the camera position was not given, so use the farthest
    # corner from there.
    expected = h * lateral / (H - h)
    assert calibration.parallax_m((0.0, 0.0)) == pytest.approx(
        h * 2.8284 / (H - h), abs=0.01
    )

    # The relationship that makes the old form wrong rather than merely
    # different: parallax × scale_factor is the old answer.
    assert calibration.scale_factor == pytest.approx(H / (H - h), abs=0.001)
    wrong = calibration.parallax_m((0.0, 0.0)) / calibration.scale_factor
    assert wrong != pytest.approx(calibration.parallax_m((0.0, 0.0)), abs=0.001), (
        "if these are equal the scale factor is 1 and the test proves nothing"
    )
    del expected


def test_the_residual_is_a_bound_and_not_an_optimistic_estimate():
    """`residual_m` is documented as an upper bound and decides `adequate`. **A
    bound that is 16% low is not a bound** — it answers the "is a floor
    calibration good enough" question in the flattering direction."""
    from unitree_robot_agent.vision.detect import Calibration

    corners = [(0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0)]
    floor = Calibration(
        [(0, 0), (100, 0), (100, 100), (0, 100)],
        corners,
        tag_height_m=0.35,      # calibrated on the floor, tags on the dogs
        camera_height_m=2.5,
    )
    at_height = Calibration(
        [(0, 0), (100, 0), (100, 100), (0, 100)],
        corners,
        tag_height_m=0.0,       # calibrated on boxes at tag height
        camera_height_m=2.5,
    )

    assert at_height.residual_m == 0.0
    assert at_height.adequate is True
    # The floor case is a real error worth warning about.
    assert floor.residual_m > 0.3, floor.residual_m
    assert floor.scale_factor == pytest.approx(1.163, abs=0.01)
    # And the two are consistent, which is the check the old formula failed.
    farthest = max(
        floor.parallax_m(point) for point in floor.world_points
    )
    assert floor.residual_m == pytest.approx(farthest, abs=1e-9)


# ─── a tag too small to trust its id ──────────────────────────────────


def test_a_big_tag_is_legible_and_a_small_one_is_not():
    """**`tag_px` was computed, documented as "the honest measure of how much to
    trust the numbers beside it", put on the wire — and compared to nothing.**

    Below `MIN_PX_PER_CELL × CELLS_ACROSS` the detector does not fail, it starts
    misreading ids, and two tags four bits apart in `DICT_APRILTAG_36h11` decode
    to each other under blur. The position is real; the *identity* is not.
    """
    from unitree_robot_agent.vision.detect import DogPose
    from unitree_robot_agent.vision.markers import CELLS_ACROSS, MIN_PX_PER_CELL

    floor = MIN_PX_PER_CELL * CELLS_ACROSS
    assert floor == 64.0, f"the floor moved to {floor}; the reasoning below assumes 64"

    def pose(tag_px):
        return DogPose(robot_id="dog1", tag_id=0, x=0.0, y=0.0,
                       heading_deg=0.0, tag_px=tag_px)

    assert pose(200.0).legible is True
    assert pose(floor).legible is True
    assert pose(floor - 0.1).legible is False
    # The measured regime the review reached: `locate` reports a pose for a
    # 40 × 40 px tag, and 40 px is where the detector starts guessing.
    assert pose(40.0).legible is False
    assert pose(0.0).legible is False, "a pose with no measurement is not legible"


def test_legibility_travels_with_the_pose():
    """It has to reach the server, or the flag is a private opinion."""
    from unitree_robot_agent.vision.detect import DogPose, FrameResult

    frame = FrameResult(poses={
        "dog1": DogPose("dog1", 0, 1.0, 2.0, 90.0, tag_px=180.0),
        "dog2": DogPose("dog2", 1, 3.0, 2.0, 90.0, tag_px=38.0),
    })
    payload = frame.as_dict()
    assert payload["poses"]["dog1"]["legible"] is True
    assert payload["poses"]["dog2"]["legible"] is False
