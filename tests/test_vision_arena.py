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
    # The camera still opens, because the panel needs a frame to recalibrate
    # against — refusing would leave a corrupt file unfixable from the page.
    assert result["started"] is True
    assert result["calibrated"] is False
    assert "读不了" in result["reason"]


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


# ─── calibrating from the page ────────────────────────────────────────


@pytest.fixture
def open_client(tmp_path):
    """A console with an arena camera configured, for the calibration routes."""
    import json as _json

    from starlette.testclient import TestClient

    from unitree_fleet_web.app import create_app

    # A *valid* starting calibration, because the fixture is a console that is
    # already set up. An empty file would make the console warn on every start,
    # which is correct behaviour and the wrong thing to build the fixture on.
    path = tmp_path / "calibration.json"
    path.write_text(_json.dumps({
        "image_points": [[80, 80], [720, 80], [720, 720], [80, 720]],
        "world_points": [[0, 4], [4, 4], [4, 0], [0, 0]],
        "tag_height_m": 0.0, "camera_height_m": 0.0,
    }), encoding="utf-8")

    from unitree_robot_agent.fleet import FleetManager, Roster
    from unitree_fleet_web import ConsoleConfig, ConsoleService

    roster = Roster.from_mapping(
        {"state_dir": str(tmp_path / "state"), "default": "dog1", "robots": [
            {"id": "dog1", "model": "Go2", "ip": "192.168.31.245", "sn": "A"},
            {"id": "dog2", "model": "Go2", "ip": "192.168.31.246", "sn": "B"}]}
    )
    config = ConsoleConfig(
        host="127.0.0.1", deadman_s=0.2,
        arena_camera="0", arena_calibration=str(path),
    )
    service = ConsoleService(FleetManager(roster, backend="mock", audit_to_stderr=False),
                             config=config)
    app = create_app(service, config)
    app._console_service = service
    with TestClient(app) as client:
        client.app = app
        yield client


def _a_frame():
    markers = default_markers(["dog1"])
    return _arena_scene([(markers[0], 2.0, 2.0, 0.0)])


def test_a_calibration_can_be_saved_from_the_page(open_client):
    """The last place that needed a hand-written JSON file."""
    service = open_client.app._console_service
    service._camera = ReplayCamera([_a_frame()] * 3, fps=30)
    service._camera.start()
    try:
        for _ in range(40):
            if service._camera.latest() is not None:
                break
            time.sleep(0.02)

        response = open_client.post("/api/arena/calibrate", json={
            "image_points": [[80, 80], [720, 80], [720, 720], [80, 720]],
            "world_points": [[0, 4], [4, 4], [4, 0], [0, 0]],
            "tag_height_m": 0.0, "camera_height_m": 0.0,
        })
        assert response.status_code == 200, response.text
        assert response.json()["saved"] is True
        assert response.json()["adequate"] is True

        # ...and it is on disk, so the next session does not have to redo it.
        import json as _json

        saved = _json.loads(Path(service.config.arena_calibration).read_text())
        assert saved["image_points"][0] == [80, 80]

        # ...and it is live, so the operator sees the map without a restart:
        # they have just measured something and want to know whether it worked.
        assert service._calibration.to_world((400.0, 400.0)) == pytest.approx(
            (2.0, 2.0), abs=0.01
        )
    finally:
        service._camera.close()


def test_the_frame_size_is_reported_beside_the_frame(open_client):
    """Without it the page records clicks in display coordinates and the
    calibration is wrong by the ratio between the two — a mistake that produces
    a map which looks plausible and is off by a factor."""
    service = open_client.app._console_service
    service._camera = ReplayCamera([_a_frame()] * 3, fps=30)
    service._camera.start()
    try:
        for _ in range(40):
            if service._camera.latest() is not None:
                break
            time.sleep(0.02)
        spec = open_client.get("/api/arena/calibration").json()
        assert spec["frame_size"] == [800, 800]
    finally:
        service._camera.close()


def test_the_frame_route_serves_a_jpeg(open_client):
    service = open_client.app._console_service
    service._camera = ReplayCamera([_a_frame()] * 3, fps=30)
    service._camera.start()
    try:
        for _ in range(40):
            if service._camera.latest() is not None:
                break
            time.sleep(0.02)
        response = open_client.get("/api/arena/frame")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/jpeg"
        # The JPEG magic, not a multipart wrapper: a console that pasted a
        # boundary into an <img> would show nothing and say nothing.
        assert response.content[:2] == b"\xff\xd8"
    finally:
        service._camera.close()


def test_a_degenerate_calibration_is_refused(open_client):
    """Three points in a line still produce a matrix. It just maps everything
    to nonsense, which is worse than failing."""
    response = open_client.post("/api/arena/calibrate", json={
        "image_points": [[100, 100], [200, 100], [300, 100], [100, 100]],
        "world_points": [[0, 0], [1, 0], [0, 1], [1, 1]],
    })
    assert response.status_code == 400
    assert response.json()["code"] == "bad_calibration"


def test_an_incomplete_calibration_is_refused_with_a_reason(open_client):
    response = open_client.post("/api/arena/calibrate", json={
        "image_points": [[0, 0], [1, 0], [1, 1]],
        "world_points": [[0, 0], [1, 0], [1, 1]],
    })
    assert response.status_code == 400
    assert response.json()["code"] == "bad_calibration"


def test_no_frame_yet_is_a_conflict_not_a_server_error(open_client):
    """A camera that has not produced a frame is a state the page can explain,
    not an exception."""
    response = open_client.get("/api/arena/frame")
    assert response.status_code == 409
    assert response.json()["available"] is False


def test_the_page_carries_the_calibration_controls(open_client):
    html = open_client.get("/").text
    for marker in ("calib-start", "calib-save", "calib-image", "calib-overlay",
                   "calib-width", "calib-tag-height"):
        assert marker in html, f"the page lost {marker}"


def test_the_console_js_converts_clicks_back_to_frame_pixels(open_client):
    """The conversion the whole flow turns on, asserted in the source because
    the failure is silent: a calibration recorded in display coordinates looks
    entirely plausible and is off by a factor."""
    source = open_client.get("/static/console.js").text
    assert "calibration.size[0]" in source
    assert "getBoundingClientRect" in source
    assert "renderCalibrationOverlay" in source
    # The order is named, not inferred: four correct points in the wrong order
    # produce a mirrored arena.
    assert "CALIB_ORDER" in source


def test_a_missing_calibration_is_a_starting_state_not_a_refusal(tmp_path):
    """The chicken-and-egg this flow would otherwise have.

    The calibration file is written by the console's own panel, so its absence
    is the state every arena starts in — refusing to open the camera because it
    is missing would mean the panel could never run to create it.
    """
    import json as _json

    from unitree_robot_agent.fleet import FleetManager, Roster
    from unitree_fleet_web import ConsoleConfig, ConsoleService

    path = tmp_path / "not-yet.json"
    assert not path.exists()
    roster = Roster.from_mapping(
        {"state_dir": str(tmp_path / "state"), "robots": [
            {"id": "dog1", "model": "Go2", "ip": "192.168.1.1", "sn": "A"}]}
    )
    service = ConsoleService(
        FleetManager(roster, backend="mock", audit_to_stderr=False),
        config=ConsoleConfig(arena_camera="0", arena_calibration=str(path)),
    )

    result = service.start_arena()

    assert result["started"] is True, "the camera must open so the page has a frame"
    assert result["calibrated"] is False
    assert "标定" in result["reason"]
    service._camera.close()

    # ...and saving one from the page fills the gap.
    saved = service.calibrate_arena({
        "image_points": [[80, 80], [720, 80], [720, 720], [80, 720]],
        "world_points": [[0, 4], [4, 4], [4, 0], [0, 0]],
    })
    assert saved["saved"] is True
    assert _json.loads(path.read_text())["world_points"][1] == [4, 4]


def test_a_corrupt_calibration_is_reported_not_ignored(tmp_path):
    """A file that exists and cannot be read is a different thing: it is a
    calibration somebody made, and silently ignoring it leaves them wondering
    why the map is blank."""
    from unitree_robot_agent.fleet import FleetManager, Roster
    from unitree_fleet_web import ConsoleConfig, ConsoleService

    path = tmp_path / "broken.json"
    path.write_text('{"image_points": [[0,0],[1,0],[1,1]], "world_points": []}', encoding="utf-8")
    roster = Roster.from_mapping(
        {"state_dir": str(tmp_path / "state"), "robots": [
            {"id": "dog1", "model": "Go2", "ip": "192.168.1.1", "sn": "A"}]}
    )
    service = ConsoleService(
        FleetManager(roster, backend="mock", audit_to_stderr=False),
        config=ConsoleConfig(arena_camera="0", arena_calibration=str(path)),
    )
    service.start_arena()
    try:
        state = service.arena_state()
        assert state["calibration"] is None
        assert "读不了" in state["detail"]
    finally:
        if service._camera is not None:
            service._camera.close()


def test_without_a_calibration_path_there_is_nowhere_to_save(tmp_path):
    """The one case worth refusing: the panel could not fix it either."""
    from unitree_robot_agent.fleet import FleetManager, Roster
    from unitree_fleet_web import ConsoleConfig, ConsoleService

    roster = Roster.from_mapping(
        {"state_dir": str(tmp_path), "robots": [
            {"id": "dog1", "model": "Go2", "ip": "192.168.1.1", "sn": "A"}]}
    )
    service = ConsoleService(
        FleetManager(roster, backend="mock", audit_to_stderr=False),
        config=ConsoleConfig(arena_camera="0", arena_calibration=""),
    )
    result = service.start_arena()
    assert result["started"] is False
    assert "路径" in result["reason"]


# ─── which tag belongs to which robot ─────────────────────────────────


def _console(tmp_path, robots, *, manifest=""):
    from unitree_robot_agent.fleet import FleetManager, Roster
    from unitree_fleet_web import ConsoleConfig, ConsoleService

    roster = Roster.from_mapping(
        {"state_dir": str(tmp_path / "state"), "default": robots[0]["id"],
         "robots": robots}
    )
    return ConsoleService(
        FleetManager(roster, backend="mock", audit_to_stderr=False),
        config=ConsoleConfig(deadman_s=1.0, marker_manifest=manifest),
    )


def test_the_manifest_is_read_when_it_is_configured(tmp_path):
    """**The printed sheet owns the mapping, not the roster.**

    Markers are printed once and stuck on the dogs; the roster is a JSON file
    somebody reorders. `load_manifest` existed, was exported, and had **no call
    site anywhere** — the console re-derived the map positionally instead.
    """
    import json

    manifest = tmp_path / "markers.json"
    manifest.write_text(json.dumps({
        "family": "DICT_APRILTAG_36h11",
        "size_m": 0.15,
        "markers": [
            {"tag_id": 0, "robot_id": "dog1", "size_m": 0.15},
            {"tag_id": 1, "robot_id": "dog2", "size_m": 0.15},
        ],
    }), encoding="utf-8")

    service = _console(
        tmp_path,
        [{"id": "dog1", "model": "Go2", "ip": "192.168.31.245", "sn": "A"},
         {"id": "dog2", "model": "Go2", "ip": "192.168.31.246", "sn": "B"}],
        manifest=str(manifest),
    )
    result = service._marker_manifest()  # noqa: SLF001
    assert result["source"] == "manifest", result
    assert {m["tag_id"]: m["robot_id"] for m in result["markers"]} == {0: "dog1", 1: "dog2"}


def test_the_manifest_beats_the_roster_order(tmp_path):
    """**The bug, as a test.** The sheet was printed with dog1 as tag 0, and the
    console's roster lists dog2 first. The positional mapping swaps the two dogs
    and nothing looks wrong: positions, headings and the separation are all
    plausible, and the formation then steers each dog by the other's error.
    """
    import json

    manifest = tmp_path / "markers.json"
    manifest.write_text(json.dumps({
        "family": "DICT_APRILTAG_36h11",
        "markers": [
            {"tag_id": 0, "robot_id": "dog1", "size_m": 0.15},
            {"tag_id": 1, "robot_id": "dog2", "size_m": 0.15},
        ],
    }), encoding="utf-8")

    # dog2 first — the order that made the positional mapping wrong.
    service = _console(
        tmp_path,
        [{"id": "dog2", "model": "Go2", "ip": "192.168.31.246", "sn": "B"},
         {"id": "dog1", "model": "Go2", "ip": "192.168.31.245", "sn": "A"}],
        manifest=str(manifest),
    )
    mapping = {
        m["tag_id"]: m["robot_id"]
        for m in service._marker_manifest()["markers"]  # noqa: SLF001
    }
    assert mapping == {0: "dog1", 1: "dog2"}, (
        f"the roster order overrode the printed sheet: {mapping}"
    )


def test_without_a_manifest_the_fallback_says_which_mapping_it_used(tmp_path):
    """It has to do *something*, so it derives one — and says so, because a
    silent derivation is how a wrong mapping survives a whole session."""
    service = _console(
        tmp_path,
        [{"id": "dog1", "model": "Go2", "ip": "192.168.31.245", "sn": "A"},
         {"id": "dog2", "model": "Go2", "ip": "192.168.31.246", "sn": "B"}],
    )
    result = service._marker_manifest()  # noqa: SLF001
    assert result["source"] == "roster"
    assert "按名单顺序" in result["note"]
    assert "marker-manifest" in result["note"], (
        "the fallback does not say how to remove the guess"
    )


def test_a_manifest_for_a_different_pair_is_reported(tmp_path):
    """A sheet printed for another pair is not a subtle failure — it means the
    tags in the arena belong to robots this console does not have."""
    import json

    manifest = tmp_path / "markers.json"
    manifest.write_text(json.dumps({
        "family": "DICT_APRILTAG_36h11",
        "markers": [
            {"tag_id": 0, "robot_id": "dog1", "size_m": 0.15},
            {"tag_id": 1, "robot_id": "dog9", "size_m": 0.15},
        ],
    }), encoding="utf-8")

    service = _console(
        tmp_path,
        [{"id": "dog1", "model": "Go2", "ip": "192.168.31.245", "sn": "A"},
         {"id": "dog2", "model": "Go2", "ip": "192.168.31.246", "sn": "B"}],
        manifest=str(manifest),
    )
    result = service._marker_manifest()  # noqa: SLF001
    assert "dog9" in result["note"]
    assert "dog2" in result["note"]


def test_a_broken_manifest_falls_back_rather_than_crashing(tmp_path):
    """The console has to come up. A bad path is a finding, not a traceback."""
    service = _console(
        tmp_path,
        [{"id": "dog1", "model": "Go2", "ip": "192.168.31.245", "sn": "A"},
         {"id": "dog2", "model": "Go2", "ip": "192.168.31.246", "sn": "B"}],
        manifest=str(tmp_path / "nope.json"),
    )
    result = service._marker_manifest()  # noqa: SLF001
    assert result["source"] == "roster"
    assert "读不了" in result["note"]


# ─── a camera below the tags ──────────────────────────────────────────


def _calibration_client(tmp_path, **config):
    import json as _json

    from starlette.testclient import TestClient

    from unitree_robot_agent.fleet import FleetManager, Roster
    from unitree_fleet_web import ConsoleConfig, ConsoleService
    from unitree_fleet_web.app import create_app

    path = tmp_path / "calib.json"
    path.write_text(_json.dumps({
        "image_points": [[80, 80], [720, 80], [720, 720], [80, 720]],
        "world_points": [[0, 4], [4, 4], [4, 0], [0, 0]],
    }), encoding="utf-8")
    roster = Roster.from_mapping({
        "state_dir": str(tmp_path / "state"),
        "robots": [{"id": "dog1", "model": "Go2", "ip": "192.168.31.245", "sn": "A"}],
    })
    cfg = ConsoleConfig(host="127.0.0.1", deadman_s=0.5,
                        arena_calibration=str(path), **config)
    app = create_app(
        ConsoleService(FleetManager(roster, backend="mock", audit_to_stderr=False),
                       config=cfg), cfg,
    )
    return TestClient(app), path


def test_a_camera_below_the_tags_is_refused_not_saved(tmp_path):
    """**Two free number fields with nothing cross-checking them.**

    With the camera lower than the tags the parallax geometry does not exist:
    `scale_factor` is infinite and `residual_m` has no value. That state used to
    reach the API, where `round(inf)` and `round(None)` raised and **every
    endpoint touching the calibration returned 500** — including the
    four-a-second status push, whose body then carried `Infinity`. The page's
    `JSON.parse` failed inside a `try` and **the live console froze silently**,
    which is the worst way for it to fail.

    Refusing at the door is better than papering over it at the call sites: a
    calibration nobody can compute is not one to save.
    """
    client, path = _calibration_client(tmp_path)
    before = path.read_text(encoding="utf-8")
    with client:
        response = client.post("/api/arena/calibrate", json={
            "image_points": [[80, 80], [720, 80], [720, 720], [80, 720]],
            "world_points": [[0, 4], [4, 4], [4, 0], [0, 0]],
            "tag_height_m": 0.35,
            "camera_height_m": 0.30,
        })

    assert response.status_code == 400, response.text
    assert response.json()["code"] == "bad_calibration"
    # It has to say which of the two numbers is wrong, because the page offers
    # them as two unlabelled boxes.
    assert "相机" in response.json()["error"]
    assert "填反" in response.json()["hint"]
    # And it must not have been written.
    assert path.read_text(encoding="utf-8") == before, "the bad calibration was saved"


def test_the_normal_heights_are_accepted_and_every_endpoint_answers(tmp_path):
    """The control. A guard that refuses a sane calibration is not a guard."""
    client, _path = _calibration_client(tmp_path)
    with client:
        response = client.post("/api/arena/calibrate", json={
            "image_points": [[80, 80], [720, 80], [720, 720], [80, 720]],
            "world_points": [[0, 4], [4, 4], [4, 0], [0, 0]],
            "tag_height_m": 0.35,
            "camera_height_m": 2.5,
        })
        assert response.status_code == 200, response.text
        assert response.json()["residual_m"] > 0.3

        # The endpoints that 500'd on the impossible geometry.
        for path in ("/api/fleet/status", "/api/arena/calibration"):
            assert client.get(path).status_code == 200, path
        # The status body must be parseable by a browser, which is what froze.
        import json as _json

        _json.loads(client.get("/api/fleet/status").text)


def test_equal_heights_are_also_refused(tmp_path):
    """`camera == tag` is the same missing geometry, and it is the boundary the
    first version would have let through with an infinite scale factor."""
    client, _path = _calibration_client(tmp_path)
    with client:
        response = client.post("/api/arena/calibrate", json={
            "image_points": [[80, 80], [720, 80], [720, 720], [80, 720]],
            "world_points": [[0, 4], [4, 4], [4, 0], [0, 0]],
            "tag_height_m": 2.5,
            "camera_height_m": 2.5,
        })
    assert response.status_code == 400, response.text


def test_the_arena_state_says_which_mapping_is_in_force(tmp_path):
    """**A warning nobody reads is not a warning.**

    `_marker_manifest()` was produced, used by the detector, and exposed on
    `/api/fleet` — and no page code read it. So a console deriving tag→robot
    from roster order, and therefore attributing every pose to the other dog,
    looked exactly like one reading the printed sheet.
    """
    import json as _json

    service = _console(
        tmp_path,
        [{"id": "dog1", "model": "Go2", "ip": "192.168.31.245", "sn": "A"},
         {"id": "dog2", "model": "Go2", "ip": "192.168.31.246", "sn": "B"}],
    )
    state = service.arena_state()
    assert state["marker_source"] == "roster"
    assert "按名单顺序" in state["marker_note"]

    manifest = tmp_path / "markers.json"
    manifest.write_text(_json.dumps({
        "family": "DICT_APRILTAG_36h11",
        "markers": [
            {"tag_id": 0, "robot_id": "dog1", "size_m": 0.15},
            {"tag_id": 1, "robot_id": "dog2", "size_m": 0.15},
        ],
    }), encoding="utf-8")

    configured = _console(
        tmp_path,
        [{"id": "dog1", "model": "Go2", "ip": "192.168.31.245", "sn": "A"},
         {"id": "dog2", "model": "Go2", "ip": "192.168.31.246", "sn": "B"}],
        manifest=str(manifest),
    )
    state = configured.arena_state()
    assert state["marker_source"] == "manifest"
    # No complaint when the sheet and the roster agree.
    assert not state["marker_note"]


def test_the_page_shows_the_mapping_warning(open_client):
    """The map is where an operator looks, so the map is where it says so."""
    source = open_client.get("/static/console.js").text
    assert "arena.marker_source" in source
    assert "按名单顺序" in source
    assert "arena.marker_note" in source
