"""The camera watching the arena, and the map it produces.

The camera is a `VideoCapture` on a background thread, keeping only the newest
frame. That sounds like plumbing and is load-bearing in three ways: opening a
webcam takes most of a second so it has to stay open, a decoder faster than its
consumer builds a backlog of frames that look live and are not, and a dead
camera has to be a *state* rather than an exception — the console also drives
robots, and refusing to run because a webcam is unplugged is the wrong trade.

The tests use `ReplayCamera`, which is the same class driven by frames from
memory rather than a mock: everything downstream of the camera is the real code.
"""

from __future__ import annotations

import ast
import math
import time
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
cv2 = pytest.importorskip("cv2")

from unitree_robot_agent.vision.arena import (  # noqa: E402
    STALE_AFTER_S,
    ArenaCamera,
    Frame,
    ReplayCamera,
)
from unitree_robot_agent.vision.markers import default_markers, render_tag  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _test_frame(value: int = 0, size: int = 200):
    return np.full((size, size), value, dtype=np.uint8)


# ─── the camera ───────────────────────────────────────────────────────


def test_a_stopped_camera_says_so_rather_than_looking_empty():
    """The distinction the whole project keeps making: "no frames" and "no
    camera" want different reactions from the operator."""
    camera = ArenaCamera("0")
    assert camera.status()["state"] == "stopped"
    assert camera.latest() is None


def test_a_replaying_camera_serves_frames():
    camera = ReplayCamera([_test_frame(0), _test_frame(128)], fps=50)
    camera.start()
    try:
        for _ in range(40):
            if camera.latest() is not None:
                break
            time.sleep(0.02)
        frame = camera.latest()
        assert frame is not None
        assert frame.image.shape == (200, 200)
        assert camera.status()["state"] == "ok"
    finally:
        camera.close()


def test_closing_releases_and_says_stopped():
    """An open webcam is held against every other program on the machine, so
    closing has to actually close — not wait for a garbage collection."""
    camera = ReplayCamera([_test_frame()], fps=50)
    camera.start()
    time.sleep(0.1)
    camera.close()
    assert camera.status()["state"] == "stopped"
    assert camera.running is False


def test_closing_twice_is_fine():
    camera = ReplayCamera([_test_frame()], fps=50)
    camera.start()
    camera.close()
    camera.close()
    assert camera.status()["state"] == "stopped"


def test_a_camera_that_has_gone_quiet_is_stale_not_ok():
    """A webcam at 15 fps that has not produced a frame in two seconds is not
    slow, it is gone — and a map drawn from a stale frame is worse than no map,
    because it looks live."""
    camera = ArenaCamera("0")
    camera._frame = Frame(image=_test_frame(), at=time.monotonic() - STALE_AFTER_S - 1.0)
    camera._thread = type("T", (), {"is_alive": staticmethod(lambda: True)})()
    assert camera.status()["state"] == "stale"
    assert camera.status()["age_s"] > STALE_AFTER_S


def test_a_camera_that_cannot_open_is_a_state_not_a_crash():
    """An unplugged camera, a busy one, a device index that does not exist —
    all of them leave the console running."""
    camera = ArenaCamera("9999")
    camera.start()
    try:
        for _ in range(60):
            if camera.status()["state"] == "failed":
                break
            time.sleep(0.05)
        status = camera.status()
        assert status["state"] in ("failed", "starting"), status
        if status["state"] == "failed":
            assert status["error"], "a failure has to say what failed"
    finally:
        camera.close()


def test_the_dropped_count_is_tracked():
    """Frames decoded minus frames consumed is the only honest way to know
    whether the consumer is keeping up."""
    camera = ReplayCamera([_test_frame(i) for i in range(3)], fps=200)
    camera.start()
    try:
        time.sleep(0.3)
        status = camera.status()
        assert status["decoded"] > 3, "the camera did not keep running"
        assert status["dropped"] > 0, "a fast producer never dropped a frame"
    finally:
        camera.close()


# ─── through the console ──────────────────────────────────────────────


@pytest.fixture
def arena_service(tmp_path):
    import json

    from unitree_robot_agent.fleet import FleetManager, Roster
    from unitree_robot_agent.vision import Calibration
    from unitree_fleet_web import ConsoleConfig, ConsoleService

    calibration = {
        "image_points": [[80, 80], [720, 80], [720, 720], [80, 720]],
        "world_points": [[0, 4], [4, 4], [4, 0], [0, 0]],
        # Zero height: calibrating at tag height is the case with no parallax.
        "tag_height_m": 0.0,
        "camera_height_m": 0.0,
    }
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps(calibration), encoding="utf-8")

    roster = Roster.from_mapping(
        {
            "state_dir": str(tmp_path / "state"),
            "default": "dog1",
            "robots": [
                {"id": "dog1", "model": "Go2", "ip": "192.168.31.245", "sn": "A"},
                {"id": "dog2", "model": "Go2", "ip": "192.168.31.246", "sn": "B"},
            ],
        }
    )
    service = ConsoleService(
        FleetManager(roster, backend="mock", audit_to_stderr=False),
        config=ConsoleConfig(deadman_s=0.5, arena_calibration=str(path)),
    )
    service._calibration = Calibration.from_dict(calibration)
    return service


def _arena_scene(placements, *, size: int = 800):
    """The arena with markers drawn at known places. Shares its geometry with
    `vision_demo.py` on purpose: a test that draws differently from the tool
    tests the test."""
    import json

    calibration = {
        "image_points": [[80, 80], [720, 80], [720, 720], [80, 720]],
        "world_points": [[0, 4], [4, 4], [4, 0], [0, 0]],
    }
    del json
    frame = np.full((size, size), 255, dtype=np.uint8)
    inverse = np.linalg.inv(
        cv2.getPerspectiveTransform(
            np.float32(calibration["image_points"]), np.float32(calibration["world_points"])
        )
    )
    for marker, x, y, heading in placements:
        tag = render_tag(marker)
        half, size_m = 0.15, 0.30
        theta = math.radians(heading)
        nose = (math.cos(theta), math.sin(theta))
        right = (math.sin(theta), -math.cos(theta))

        def corner(forward, side):
            return (x + nose[0] * forward + right[0] * side,
                    y + nose[1] * forward + right[1] * side)

        world = [corner(half, -half), corner(half, half),
                 corner(-half, half), corner(-half, -half)]
        projected = []
        for wx, wy in world:
            v = inverse @ np.array([wx, wy, 1.0])
            projected.append((v[0] / v[2], v[1] / v[2]))
        source = np.float32([[0, 0], [tag.shape[1], 0],
                             [tag.shape[1], tag.shape[0]], [0, tag.shape[0]]])
        drawn = cv2.warpPerspective(
            tag, cv2.getPerspectiveTransform(source, np.float32(projected)),
            (size, size), borderValue=255,
        )
        frame = np.minimum(frame, drawn)
    return frame


async def test_the_console_says_off_when_no_camera_is_configured(arena_service):
    """Not an empty arena with no dogs in it. One of those is a setup problem
    and the other is a dog that walked off."""
    state = arena_service.arena_state()
    assert state["state"] == "off"
    assert state["poses"] == {}


async def test_the_console_maps_the_arena_from_a_camera(arena_service):
    """The whole feature, end to end: camera to detection to the status push."""
    import asyncio

    markers = default_markers(["dog1", "dog2"])
    frames = [
        _arena_scene([(markers[0], 1.0, 2.0, 0.0), (markers[1], 3.0, 2.0, 180.0)])
    ] * 3
    arena_service._camera = ReplayCamera(frames, fps=30)
    arena_service._camera.start()
    task = asyncio.create_task(arena_service._arena_loop())
    try:
        for _ in range(60):
            await asyncio.sleep(0.05)
            if arena_service.arena_state()["state"] == "ok":
                break

        state = arena_service.arena_state()
        assert state["state"] == "ok", state
        assert set(state["poses"]) == {"dog1", "dog2"}
        assert state["poses"]["dog1"]["x"] == pytest.approx(1.0, abs=0.05)
        assert state["poses"]["dog2"]["x"] == pytest.approx(3.0, abs=0.05)
        assert state["poses"]["dog1"]["heading_deg"] == pytest.approx(0.0, abs=3.0)

        payload = await arena_service.status()
        assert payload["arena"]["state"] == "ok"
        assert set(payload["arena"]["poses"]) == {"dog1", "dog2"}
    finally:
        task.cancel()
        arena_service._camera.close()


async def test_a_missing_calibration_is_named_rather_than_guessed(arena_service):
    """A camera with no calibration gives a bearing and no distance. Drawing a
    map from it would be drawing a picture."""
    arena_service._calibration = None
    arena_service._camera = ReplayCamera([_test_frame()], fps=30)
    arena_service._camera.start()
    try:
        assert arena_service.arena_state()["calibration"] is None
    finally:
        arena_service._camera.close()


def test_starting_without_a_camera_is_not_an_error(arena_service):
    """A console with no camera still drives robots, which is still the point."""
    result = arena_service.start_arena()
    assert result["started"] is False
    assert result["reason"]


def test_a_broken_calibration_file_is_reported_not_raised(tmp_path):
    from unitree_robot_agent.fleet import FleetManager, Roster
    from unitree_fleet_web import ConsoleConfig, ConsoleService

    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    roster = Roster.from_mapping(
        {"state_dir": str(tmp_path), "robots": [
            {"id": "dog1", "model": "Go2", "ip": "192.168.1.1", "sn": "A"}]}
    )
    service = ConsoleService(
        FleetManager(roster, backend="mock", audit_to_stderr=False),
        config=ConsoleConfig(arena_camera="0", arena_calibration=str(bad)),
    )
    result = service.start_arena()
    assert result["started"] is False
    assert "unreadable" in result["reason"]


# ─── the collision this feature could cause ───────────────────────────


def test_cv2_is_never_imported_at_module_level():
    """On macOS OpenCV and PyAV each ship an FFmpeg and fight over registering
    the same Objective-C classes, ending in "may cause spurious casting failures
    and mysterious crashes". A console that drives robots cannot carry that.

    Every `cv2` import in this package is inside a function, which is what keeps
    it out of a session that never uses vision — a plain console start was
    checked and does not load it. Moving one to module level would make every
    session pay for a feature it is not using, and this is the test that says so.
    """
    offenders = []
    for path in sorted((ROOT / "unitree_robot_agent" / "vision").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:  # module level only; anything nested is fine
            if isinstance(node, ast.Import):
                offenders += [
                    f"{path.name}:{node.lineno} import {alias.name}"
                    for alias in node.names if alias.name.split(".")[0] == "cv2"
                ]
            elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("cv2"):
                offenders.append(f"{path.name}:{node.lineno} from {node.module}")
    assert not offenders, (
        "cv2 is imported at module level, so every console session now loads a "
        f"second FFmpeg: {offenders}"
    )


# ─── the map on the page ──────────────────────────────────────────────


def test_the_arena_payload_carries_bounds_and_separation(arena_service):
    """The map is drawn to the calibration's own extent rather than a fixed
    one: an arena the drawing disagrees with is a map that lies about
    distances."""
    import asyncio

    markers = default_markers(["dog1", "dog2"])
    frames = [
        _arena_scene([(markers[0], 1.0, 2.0, 0.0), (markers[1], 3.0, 2.0, 90.0)])
    ] * 3
    arena_service._camera = ReplayCamera(frames, fps=30)
    arena_service._camera.start()
    loop = asyncio.new_event_loop()
    task = loop.create_task(arena_service._arena_loop())
    try:
        for _ in range(60):
            loop.run_until_complete(asyncio.sleep(0.05))
            if arena_service.arena_state()["state"] == "ok":
                break
        state = loop.run_until_complete(arena_service.status())["arena"]
        assert state["calibration"]["bounds"] == [0.0, 0.0, 4.0, 4.0]
        gap = state["separation"]
        assert gap["distance_m"] == pytest.approx(2.0, abs=0.1)
        assert gap["yaw_spread_deg"] == pytest.approx(90.0, abs=5.0)
        assert gap["bearing_deg_from_a_to_b"] == pytest.approx(0.0, abs=5.0)
    finally:
        task.cancel()
        loop.run_until_complete(asyncio.sleep(0))
        loop.close()
        arena_service._camera.close()


def test_the_console_js_draws_the_map(open_client):
    source = open_client.get("/static/console.js").text
    for marker in ("renderArena", "ARENA_STATES", "bearing_deg", "scale_factor"):
        assert marker in source, f"console.js lost {marker}"
    # The map must be built from the calibration's bounds, not a hardcoded size.
    assert "calibration.bounds" in source or "calibration && calibration.bounds" in source


def test_the_page_says_which_reason_the_map_is_blank(open_client):
    """An empty field and two dogs out of shot look identical, and they want
    different reactions."""
    html = open_client.get("/").text
    for marker in ("arena-panel", "arena-map", "arena-state", "arena-legend"):
        assert marker in html, f"the page lost {marker}"
    source = open_client.get("/static/console.js").text
    for state in ("camera_failed", "camera_stale", "no_markers", "starting"):
        assert state in source, f"the map does not handle {state}"
