"""The markers, and the measurement that decides whether they work at all.

The interesting part of this file is `test_the_detection_limit_is_where_the_
constant_says`. Everything else is plumbing.

Fiducial detection has a widely quoted rule of thumb — "10 to 16 pixels per
cell" — and it is quoted against assumptions nobody states. Two experiments
before this one produced answers that were confidently wrong:

* A **clean synthetic render** detected at 8 pixels *total*, which is 1 pixel per
  cell. That measured the renderer, not the detector: there was no blur, no
  noise and no compression, so there was nothing for the tag to survive.
* A render with a **constant small blur** detected at every size down to 20 px.
  The blur was constant in tag pixels, so shrinking the tag did not make it
  relatively blurrier — which is the *entire* mechanism by which a distant tag
  becomes unreadable.

The model that finally bit was **optical blur fixed in scene pixels**: a lens
blurs by a fixed amount, so the smaller the tag is on the sensor, the larger
that blur is relative to a cell. With that, the cliff appears where the
literature says it should, and the constant in `markers.py` is set from a
measurement of this pipeline rather than from a quote.
"""

from __future__ import annotations

import json

import pytest

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

from unitree_robot_agent.vision.markers import (  # noqa: E402
    CELLS_ACROSS,
    DEFAULT_SIZE_M,
    FAMILY,
    MIN_PX_PER_CELL,
    Marker,
    default_markers,
    load_manifest,
    pixel_budget,
    pixels_per_metre_for,
    reading_range_m,
    required_width_px,
    render_tag,
    render_tile,
    write_markers,
)


# ─── the tag itself ───────────────────────────────────────────────────


def test_the_cell_count_is_measured_not_assumed():
    """36h11 is 6 data cells plus one border cell each side.

    An earlier version of `markers.py` said 10, which made every range estimate
    25% optimistic — and the error would have shown up as tags that do not read
    at the distance the tool promised.
    """
    tag = render_tag(Marker(tag_id=0, robot_id="dog1", label="dog1"))
    row = tag[tag.shape[0] // 2]
    runs, previous, start = [], row[0] < 128, 0
    for index in range(1, len(row)):
        current = row[index] < 128
        if current != previous:
            runs.append(index - start)
            start, previous = index, current
    runs.append(len(row) - start)

    # The first run is the border plus the leading data cells, so the smallest
    # repeated run is a cell.
    cell = min(run for run in runs if run > 0)
    assert round(tag.shape[0] / cell) == CELLS_ACROSS, (
        f"a tag is {tag.shape[0] / cell:.1f} cells across, but CELLS_ACROSS "
        f"says {CELLS_ACROSS}"
    )


def test_a_tag_is_rendered_at_the_physical_size_asked_for():
    """The whole point of the print is its size, and a tag printed at the wrong
    size fails in a way that looks like a detection bug."""
    marker = Marker(tag_id=0, robot_id="dog1", label="dog1", size_m=0.15)
    tag = render_tag(marker)
    assert marker.size_mm == 150.0
    # 1772 px at 300 dpi is 150 mm, to within the rounding of a pixel.
    assert abs(tag.shape[0] / 300 * 25.4 - 150.0) < 1.0


def test_different_dogs_get_different_tags():
    markers = default_markers(["dog1", "dog2", "dog3"])
    assert [m.tag_id for m in markers] == [0, 1, 2]
    tags = {render_tag(m).tobytes() for m in markers}
    assert len(tags) == 3, "two dogs were given the same tag"


# ─── the measurement ──────────────────────────────────────────────────


def _scene(tag_px: int, seed: int, *, blur_sigma: float):
    """One tag of `tag_px` width in a scene carrying what a camera adds.

    `blur_sigma` is in **scene** pixels and deliberately does not scale with the
    tag. That is the mechanism: a lens blurs by a fixed amount, so a smaller tag
    is blurred by a larger fraction of a cell.
    """
    rng = np.random.default_rng(seed)
    side = 400
    tag = render_tag(Marker(tag_id=0, robot_id="dog1", label="dog1"))
    half = tag_px / 2
    centre = side / 2
    angle = rng.uniform(-0.25, 0.25)
    cos_a, sin_a = np.cos(angle), np.sin(angle)
    quad = []
    for dx, dy in ((-half, -half), (half, -half), (half, half), (-half, half)):
        # A little perspective: the far edge is narrower.
        squeeze = 1.0 - 0.18 * (dy / half) * 0.5
        x, y = dx * squeeze, dy
        quad.append((centre + x * cos_a - y * sin_a, centre + x * sin_a + y * cos_a))

    source = np.float32([
        [0, 0], [tag.shape[1], 0], [tag.shape[1], tag.shape[0]], [0, tag.shape[0]]
    ])
    matrix = cv2.getPerspectiveTransform(source, np.float32(quad))
    image = cv2.warpPerspective(
        tag, matrix, (side, side), flags=cv2.INTER_LINEAR, borderValue=255
    )
    image = cv2.GaussianBlur(image, (int(blur_sigma * 4) | 1, int(blur_sigma * 4) | 1), blur_sigma)
    image = np.clip(
        image.astype(np.float32) + rng.normal(0, 6, image.shape), 0, 255
    ).astype(np.uint8)
    ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 75])
    return cv2.imdecode(encoded, cv2.IMREAD_GRAYSCALE)


def _detects(tag_px: int, seed: int, blur_sigma: float) -> bool:
    detector = cv2.aruco.ArucoDetector(
        cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, FAMILY)),
        cv2.aruco.DetectorParameters(),
    )
    _corners, ids, _rejected = detector.detectMarkers(
        _scene(tag_px, seed, blur_sigma=blur_sigma)
    )
    return ids is not None and 0 in ids.flatten()


@pytest.mark.parametrize("blur_sigma", [0.8, 1.8])
def test_a_tag_at_the_recommended_size_always_reads(blur_sigma):
    """Above `MIN_PX_PER_CELL` detection has to be reliable, not merely
    possible: a marker that reads four times in five is a position that jumps."""
    tag_px = int(MIN_PX_PER_CELL * CELLS_ACROSS) + 8
    hits = sum(1 for seed in range(12) if _detects(tag_px, seed, blur_sigma))
    assert hits == 12, f"{hits}/12 at {tag_px}px with blur {blur_sigma}"


@pytest.mark.parametrize("blur_sigma", [1.8, 2.5])
def test_a_tag_far_below_the_recommended_size_stops_reading(blur_sigma):
    """The other half of the measurement, and the reason the constant is not
    just set to whatever detects once. If a tiny tag read perfectly there would
    be no need for the arithmetic at all."""
    tag_px = int(2.0 * CELLS_ACROSS)
    hits = sum(1 for seed in range(12) if _detects(tag_px, seed, blur_sigma))
    assert hits < 6, f"{hits}/12 at {tag_px}px — the cliff has moved"


def test_the_measured_cliff_is_not_above_the_constant():
    """`MIN_PX_PER_CELL` is set above the measured cliff rather than at it,
    because a real arena adds motion blur and uneven lighting that the model
    does not carry. This asserts the margin still exists."""
    cliff_px = int(5.0 * CELLS_ACROSS)
    margin_px = int(MIN_PX_PER_CELL * CELLS_ACROSS)
    assert margin_px > cliff_px, (
        "the recommendation has been lowered to the measured limit, which "
        "leaves nothing for motion blur"
    )
    # ...and it is not so far above that it costs a tag size for nothing.
    assert margin_px <= cliff_px * 2


# ─── the arithmetic a session checks before printing ──────────────────


def test_pixels_per_metre_matches_a_real_lens():
    """1920 px across a 60° view: at 3 m that is about 554 px per metre."""
    assert pixels_per_metre_for(1920, 3.0, 60.0) == pytest.approx(554, abs=5)


def test_a_default_marker_reads_across_a_small_room():
    """The number that decides whether this feature works at all: a 150 mm tag
    on a Go2's back, seen by a 1080p camera at three metres."""
    marker = Marker(tag_id=0, robot_id="dog1", label="dog1", size_m=DEFAULT_SIZE_M)
    budget = pixel_budget(marker, pixels_per_metre=pixels_per_metre_for(1920, 3.0, 60.0))
    assert budget["usable"] is True, budget
    # ...and how far it still reads, solved from the camera rather than from a
    # pixels-per-metre figure with no distance attached to it.
    reach = reading_range_m(marker, width_px=1920, hfov_deg=60.0)
    assert reach == pytest.approx(3.9, abs=0.2), reach


def test_the_same_marker_does_not_read_across_a_large_one():
    """...and it does not work at eight metres, which is the honest answer and
    the one a session needs before it prints a tag that will not read."""
    marker = Marker(tag_id=0, robot_id="dog1", label="dog1", size_m=DEFAULT_SIZE_M)
    budget = pixel_budget(marker, pixels_per_metre=pixels_per_metre_for(1920, 8.0, 60.0))
    assert budget["usable"] is False
    assert budget["px_per_cell"] < MIN_PX_PER_CELL
    # A verdict alone is not actionable; the numbers are.
    assert budget["minimum_px"] > 0
    assert "太小" in budget["note"]
    # ...and it says how far it *would* reach, so the fix is a decision rather
    # than a guess between a bigger tag, a closer camera and a shorter arena.
    assert reading_range_m(marker, width_px=1920, hfov_deg=60.0) < 8.0


def test_a_longer_lens_reads_further():
    """Which is the other lever, and the one that costs money rather than a
    bigger tag on the dog."""
    marker = Marker(tag_id=0, robot_id="d", label="d")
    assert reading_range_m(marker, width_px=1920, hfov_deg=30.0) > reading_range_m(
        marker, width_px=1920, hfov_deg=90.0
    )
    # A 4K sensor reads about twice as far as 1080p through the same lens.
    assert reading_range_m(marker, width_px=3840, hfov_deg=60.0) == pytest.approx(
        2 * reading_range_m(marker, width_px=1920, hfov_deg=60.0), rel=0.01
    )


def test_the_required_resolution_answers_the_other_question():
    """"Will the webcam I already own do it" is a different question from "how
    big a tag do I print", and both get asked before anything is bought."""
    marker = Marker(tag_id=0, robot_id="d", label="d", size_m=DEFAULT_SIZE_M)
    # At 3 m through a 60° lens a 150 mm tag needs about 1470 px.
    assert required_width_px(marker, distance_m=3.0, hfov_deg=60.0) == pytest.approx(1470, abs=40)
    # ...so 1080p is enough at 3 m and not at 5 m.
    assert required_width_px(marker, distance_m=3.0, hfov_deg=60.0) <= 1920
    assert required_width_px(marker, distance_m=5.0, hfov_deg=60.0) > 1920


# ─── what gets printed ────────────────────────────────────────────────


def test_a_printable_page_fits_on_a4():
    """The sheet stacks every tag down one strip, which came out 514 mm long —
    longer than a sheet of A4, and "print this" should not need arithmetic."""
    tile = render_tile(Marker(tag_id=0, robot_id="dog1", label="dog1"))
    width_mm = tile.shape[1] / 300 * 25.4
    height_mm = tile.shape[0] / 300 * 25.4
    assert width_mm <= 210, f"{width_mm:.0f} mm wide does not fit A4"
    assert height_mm <= 297, f"{height_mm:.0f} mm tall does not fit A4"


def test_the_page_says_which_way_the_nose_goes():
    """A tag stuck on 90° out reports a heading 90° out, and nothing in the
    detection will ever say so. The arrow is the only thing preventing it, so it
    is worth asserting that the pixels are there."""
    plain = render_tag(Marker(tag_id=0, robot_id="dog1", label="dog1"))
    tile = render_tile(Marker(tag_id=0, robot_id="dog1", label="dog1"))
    assert tile.shape[0] > plain.shape[0], "no room was left for the arrow"
    # The arrow is the only dark thing above the tag's quiet zone.
    band = tile[: tile.shape[0] - plain.shape[0] - 100, :]
    assert (band < 128).sum() > 0, "nothing was drawn above the tag"


def test_writing_markers_produces_a_manifest_the_detector_can_read(tmp_path):
    """Without the manifest the tag-to-robot mapping lives in somebody's head,
    and it will be wrong on the second session."""
    markers = default_markers(["dog1", "dog2"])
    result = write_markers(tmp_path, markers)

    for name in result["files"]:
        assert (tmp_path / name).exists(), name

    manifest = load_manifest(tmp_path / "markers.json")
    assert manifest["family"] == FAMILY
    assert [m["robot_id"] for m in manifest["markers"]] == ["dog1", "dog2"]
    assert manifest["markers"][0]["tag_id"] == 0
    # The print instructions are part of the artifact: "fit to page" is the
    # single most likely way for this to fail, and it fails silently.
    assert "100%" in manifest["print_instructions"]
    assert str(int(DEFAULT_SIZE_M * 1000)) in manifest["print_instructions"]


def test_the_manifest_is_json_and_round_trips(tmp_path):
    write_markers(tmp_path, default_markers(["dog1"]))
    raw = json.loads((tmp_path / "markers.json").read_text(encoding="utf-8"))
    assert raw["markers"][0]["size_m"] == DEFAULT_SIZE_M


# ─── will this camera read a tag this size ────────────────────────────


def test_the_legibility_calculator_counts_the_inset():
    """**The parameter the earlier guidance did not have.**

    The arena does not fill the frame: the operator clicks its four corners
    *inside* the picture, and the panel's own default targets span about 80% of
    the width. Dividing frame width by arena size treats the arena as
    edge-to-edge and overstates the resolution by the reciprocal of the
    fraction.

    Measured against the test simulation, which carried the same mistake in a
    comment: it claimed 400 px/m (1600 ÷ 4 m) and is actually 320 (1280 px of
    clicked corners ÷ 4 m). **25% optimistic, in the direction that makes a rig
    look like it will work.**
    """
    from unitree_robot_agent.vision.markers import legibility

    edge_to_edge = legibility(
        width_px=1600, arena_width_m=4.0, tag_size_m=0.30, arena_fraction=1.0
    )
    inset = legibility(width_px=1600, arena_width_m=4.0, tag_size_m=0.30)
    assert inset["pixels_per_metre"] == pytest.approx(320.0, abs=1)
    assert edge_to_edge["pixels_per_metre"] == pytest.approx(400.0, abs=1)
    # The wrong version is the flattering one.
    assert edge_to_edge["tag_px"] > inset["tag_px"]


def test_the_default_tag_does_not_clear_a_1080p_camera_over_four_metres():
    """**The number that was wrong in the documentation.**

    The table said "1080p ✅ 刚够". It is 58 px against a 64 px floor — **not
    enough**, and the correction matters because the project ships a 150 mm tag
    as its default and a Go2's back will not take a 300 mm one.
    """
    from unitree_robot_agent.vision.markers import legibility

    measured = {
        (1280, "720p"): False,
        (1920, "1080p"): False,
        (2560, "1440p"): True,
        (3840, "4K"): True,
    }
    for (width, name), expected in measured.items():
        result = legibility(width_px=width, arena_width_m=4.0, tag_size_m=0.15)
        assert result["ok"] is expected, f"{name}: {result}"

    # And the three ways out, computed rather than described.
    poor = legibility(width_px=1920, arena_width_m=4.0, tag_size_m=0.15)
    assert poor["min_tag_m"] == pytest.approx(0.167, abs=0.005)
    assert 2100 <= poor["min_width_px"] <= 2200
    assert poor["max_arena_m"] == pytest.approx(3.6, abs=0.1)
    assert "167mm" in poor["note"], poor["note"]


def test_a_bigger_tag_rescues_the_720p_case():
    """The other direction, so the calculator is not just saying no."""
    from unitree_robot_agent.vision.markers import legibility

    assert legibility(
        width_px=1280, arena_width_m=4.0, tag_size_m=0.15
    )["ok"] is False
    assert legibility(
        width_px=1280, arena_width_m=4.0, tag_size_m=0.30
    )["ok"] is True


def test_the_calculator_refuses_nonsense_instead_of_dividing_by_zero():
    from unitree_robot_agent.vision.markers import legibility

    for kwargs in (
        {"width_px": 0, "arena_width_m": 4.0, "tag_size_m": 0.15},
        {"width_px": 1920, "arena_width_m": 0.0, "tag_size_m": 0.15},
        {"width_px": 1920, "arena_width_m": 4.0, "tag_size_m": 0.0},
    ):
        result = legibility(**kwargs)
        assert result["ok"] is False
        assert "正数" in result["reason"]
