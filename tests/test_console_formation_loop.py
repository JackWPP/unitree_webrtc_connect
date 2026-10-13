"""The closed loop, against an arena that answers back.

The unit tests in `test_console_formation.py` check geometry against pictures
that can be worked out by hand. This file checks the *loop*: plan, command,
look again, decide — against a simulated arena that renders frames from the
dogs' poses and **moves them when `robot_move` is called**.

Without that, the loop's most important property is untestable: that a wrong
speed constant costs iterations rather than accuracy. A static replay camera
would let the loop "converge" on a picture that never changed.
"""

from __future__ import annotations

import asyncio
import math
import time
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
cv2 = pytest.importorskip("cv2")

from unitree_robot_agent.fleet import FleetManager, Roster  # noqa: E402
from unitree_robot_agent.vision.markers import default_markers, render_tag  # noqa: E402
from unitree_fleet_web import ConsoleConfig, ConsoleService  # noqa: E402
from unitree_fleet_web.formation import Formation  # noqa: E402

CALIBRATION = {
    "image_points": [[80, 80], [720, 80], [720, 720], [80, 720]],
    "world_points": [[0, 4], [4, 4], [4, 0], [0, 0]],
    "tag_height_m": 0.0,
    "camera_height_m": 0.0,
}

MARKERS = default_markers(["dog1", "dog2"])


def render(poses, *, size: int = 800):
    """The arena as the camera would see it, from the dogs' poses."""
    frame = np.full((size, size), 255, dtype=np.uint8)
    inverse = np.linalg.inv(cv2.getPerspectiveTransform(
        np.float32(CALIBRATION["image_points"]),
        np.float32(CALIBRATION["world_points"]),
    ))
    for marker, (x, y, heading) in zip(MARKERS, poses):
        tag = render_tag(marker)
        tag_size, half = 0.30, 0.15
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


class SimulatedArena:
    """A camera over dogs that actually move when they are told to.

    It models the thing the loop has to survive: `robot_move` takes a
    **normalised** speed, so how far a step travels depends on a constant. This
    one deliberately picks a constant different from the loop's guess, so the
    test proves the loop converges *despite* being wrong about it — which is the
    entire reason the loop exists.
    """

    def __init__(self, poses, *, max_speed_mps: float = 1.9):
        self.poses = [list(pose) for pose in poses]
        self.max_speed_mps = max_speed_mps
        self.commands = []
        self._frame = None
        # A realistic monotonic clock, not 0.0. The detection loop skips a frame
        # whose timestamp equals the last one it processed, and it starts at
        # 0.0 — so a double starting there presents a frame the loop treats as
        # already seen and never processes at all.
        self._at = time.monotonic()

    # ── the camera ──

    def latest(self):
        from unitree_robot_agent.vision.arena import Frame

        self._frame = Frame(image=render(self.poses), at=self._at)
        return self._frame

    @property
    def running(self):
        return True

    def status(self):
        return {"source": "simulated", "state": "ok", "error": "", "age_s": 0.0,
                "size": [800, 800], "decoded": 1, "dropped": 0}

    def close(self):
        pass

    async def aclose(self):
        pass

    def tick(self, dt: float = 0.05):
        """A new frame. The loop waits for this, so the test drives it."""
        self._at += dt

    # ── the robot ──

    def apply_move(self, robot_id: str, args: dict) -> None:
        """Exactly what the robot would do with a normalised command."""
        self.commands.append((robot_id, dict(args)))
        index = [m.robot_id for m in MARKERS].index(robot_id)
        duration = float(args.get("duration_s") or 0.0)
        # The robot's own maximum, which is *not* what the loop assumed.
        vx = float(args.get("vx") or 0.0) * self.max_speed_mps * duration
        vy = float(args.get("vy") or 0.0) * self.max_speed_mps * duration
        x, y, heading = self.poses[index]
        theta = math.radians(heading)
        nose = (math.cos(theta), math.sin(theta))
        left = (-math.sin(theta), math.cos(theta))
        self.poses[index] = [
            x + nose[0] * vx + left[0] * vy,
            y + nose[1] * vx + left[1] * vy,
            heading,
        ]
        self.tick()

    def apply_walk(self, robot_id: str, meters: float) -> None:
        """`robot_walk`: a measured distance straight along the nose.

        Exact here, because the simulation *is* the odometry — which is the
        point of the primitive. On a real robot it is the robot's own dead
        reckoning and carries its drift; in here there is none, so a test that
        converges in fewer steps than before is showing what the measured walk
        buys rather than a flattering simulation.
        """
        self.commands.append((robot_id, {"walk_m": meters}))
        index = [m.robot_id for m in MARKERS].index(robot_id)
        x, y, heading = self.poses[index]
        theta = math.radians(heading)
        self.poses[index] = [
            x + math.cos(theta) * meters,
            y + math.sin(theta) * meters,
            heading,
        ]
        self.tick()

    def apply_turn(self, robot_id: str, degrees: float) -> None:
        index = [m.robot_id for m in MARKERS].index(robot_id)
        self.poses[index][2] += degrees
        self.tick()


@pytest.fixture
def loop_service(tmp_path):
    import json

    from unitree_robot_agent.fleet import FleetManager, Roster
    from unitree_fleet_web import ConsoleConfig, ConsoleService

    path = tmp_path / "calibration.json"
    path.write_text(json.dumps(CALIBRATION), encoding="utf-8")
    roster = Roster.from_mapping(
        {"state_dir": str(tmp_path / "state"), "default": "dog1", "robots": [
            {"id": "dog1", "model": "Go2", "ip": "192.168.31.245", "sn": "A"},
            {"id": "dog2", "model": "Go2", "ip": "192.168.31.246", "sn": "B"}]}
    )
    config = ConsoleConfig(host="127.0.0.1", deadman_s=0.5,
                           arena_camera="sim", arena_calibration=str(path))
    service = ConsoleService(FleetManager(roster, backend="mock", audit_to_stderr=False),
                             config=config)
    service.start_arena()
    return service


def wire(service, arena):
    """Point the service at the simulated arena, commands and all.

    **Both halves matter.** Setting `_camera` makes the service *see* the
    simulated dogs; routing `robot_move` and `robot_turn` into
    `SimulatedArena.apply_*` makes it *move* them. Wiring only the camera gives
    a loop that commands a real robot nobody is watching and measures a picture
    that never changes — which is exactly the failure mode the first version of
    these tests had, and it reported `no_progress` rather than anything that
    pointed at the test.

    **Every tool the loop can use has to be routed here.** Switching the
    formation to prefer `robot_walk` reproduced the whole thing one level up:
    the loop commanded a measured walk, the simulation had no handler for it,
    the command went to the mock backend, and the dogs did not move.
    """
    service._camera = arena
    original = service.call_tool

    async def routed(robot_id, tool, arguments=None):
        arguments = dict(arguments or {})
        if tool == "robot_move":
            arena.apply_move(robot_id, arguments)
            return {"ok": True}
        if tool == "robot_walk":
            arena.apply_walk(robot_id, float(arguments.get("meters") or 0.0))
            return {"ok": True, "reason": "reached"}
        if tool == "robot_turn":
            arena.apply_turn(robot_id, float(arguments.get("degrees") or 0.0))
            return {"ok": True}
        return await original(robot_id, tool, arguments)

    service.call_tool = routed  # type: ignore[assignment]
    return service


async def _settled(service, arena, *, seconds: float = 4.0):
    """Start the detector and wait for the first real measurement."""
    import asyncio as _asyncio

    wire(service, arena)
    detector = _asyncio.create_task(service._arena_loop())
    for _ in range(int(seconds / 0.05)):
        await _asyncio.sleep(0.05)
        if service.arena_state()["state"] == "ok":
            return detector
    detector.cancel()
    raise AssertionError("the simulated arena never produced a measurement")


async def test_the_loop_closes_the_gap(loop_service):
    """1.8 m apart, told to be 1.0 — a misalignment to correct, not a march
    across the room.

    The gap is chosen to be inside the default budget on purpose. A step travels
    appreciably less than `MAX_STEP_M` once the normalised speed is worked
    through, so six steps close about a metre; a test asking for more would be
    testing the budget rather than the loop.
    """
    arena = SimulatedArena([(1.0, 2.0, 0.0), (2.8, 2.0, 0.0)])
    detector = await _settled(loop_service, arena)
    try:
        result = await loop_service.align_formation(
            Formation(distance_m=1.0, bearing_deg=0.0, mover="b"),
            max_steps=6, settle_s=0.05,
        )
    finally:
        detector.cancel()
        with pytest.raises(asyncio.CancelledError):
            await detector

    assert result["outcome"] == "converged", result
    assert result["steps"], "it converged without moving anything"

    # Every step measured again and the error shrank each time, which is what
    # "closed loop" has to mean — and what an open-loop plan cannot do.
    errors = [abs(step["measured"]["distance_error_m"]) for step in result["steps"]]
    assert errors == sorted(errors, reverse=True), errors
    assert math.dist(arena.poses[0][:2], arena.poses[1][:2]) == pytest.approx(
        1.0, abs=0.12
    )


async def test_it_converges_despite_a_wrong_speed_constant(loop_service):
    """**The reason the loop exists.**

    The simulated robot's maximum is 1.9 m/s and the planner assumes 3.0, so
    every step lands short of where it was aimed. An open-loop plan would stop
    there, confidently in the wrong place; the loop measures again and takes
    another step.
    """
    # A 0.6 m gap, which takes more than one step because the planner's speed
    # constant is wrong by a third.
    arena = SimulatedArena([(1.4, 2.0, 0.0), (3.0, 2.0, 0.0)], max_speed_mps=1.9)
    detector = await _settled(loop_service, arena)
    try:
        result = await loop_service.align_formation(
            Formation(distance_m=1.0, bearing_deg=0.0, mover="b"),
            max_steps=8, settle_s=0.05,
        )
    finally:
        detector.cancel()
        with pytest.raises(asyncio.CancelledError):
            await detector

    assert result["outcome"] == "converged", result
    assert len(result["steps"]) > 1, "one shot was enough, so nothing was proven"
    assert result["outcome"] == "converged", result

    final = arena.poses
    assert math.dist(final[0][:2], final[1][:2]) == pytest.approx(1.0, abs=0.15)


async def test_an_already_correct_formation_is_left_alone(loop_service):
    """Pressing the button twice must not shuffle two robots that are already
    where they should be."""
    arena = SimulatedArena([(1.0, 2.0, 0.0), (2.0, 2.0, 0.0)])
    detector = await _settled(loop_service, arena)
    try:
        result = await loop_service.align_formation(
            Formation(distance_m=1.0, bearing_deg=0.0), settle_s=0.05,
        )
    finally:
        detector.cancel()
        with pytest.raises(asyncio.CancelledError):
            await detector

    assert result["outcome"] == "converged"
    assert result["steps"] == []
    assert arena.commands == []


async def test_it_stops_when_it_stops_making_progress(loop_service):
    """A loop chasing a target it cannot reach does not fail, it oscillates —
    and two robots oscillating at each other in a room is worse than two robots
    standing slightly wrong."""
    arena = SimulatedArena([(1.0, 2.0, 0.0), (4.0, 2.0, 0.0)])

    loop_service.state.estop.release  # noqa: B018 - no latch in this test
    detector = await _settled(loop_service, arena)

    # Commands do nothing at all: the robot is held, or a wheel is off the
    # ground, or something else is stopping it. **Both** movement tools have to
    # be neutralised — the loop picks between them, and disabling only the one
    # it used to prefer would leave this testing the other.
    def ignore(*_args, **_kwargs):
        arena.tick()

    arena.apply_move = ignore  # type: ignore[assignment]
    arena.apply_walk = ignore  # type: ignore[assignment]
    try:
        result = await loop_service.align_formation(
            Formation(distance_m=1.0, bearing_deg=0.0, mover="b"),
            max_steps=6, settle_s=0.02,
        )
    finally:
        detector.cancel()
        with pytest.raises(asyncio.CancelledError):
            await detector

    assert result["outcome"] == "no_progress", result
    assert "没有变小" in result["detail"]
    assert len(result["steps"]) <= 2, "it kept going after learning nothing"


async def test_it_refuses_without_a_measurement(loop_service):
    """Driving on from a stale position is driving blind."""
    loop_service._camera = None
    result = await loop_service.align_formation(Formation(), settle_s=0.02)
    assert result["outcome"] == "no_measurement"
    assert result["steps"] == []


async def test_the_estop_stops_it(loop_service):
    """A latched stop has to stop this too, and be checked every step rather
    than once at the start."""
    arena = SimulatedArena([(1.0, 2.0, 0.0), (4.0, 2.0, 0.0)])
    wire(loop_service, arena)
    from unitree_fleet_web.state import EStopScope

    loop_service.state.estop.engage(
        scope=EStopScope.ALL, robot_id="", reason="test"
    )
    result = await loop_service.align_formation(Formation(mover="b"), settle_s=0.02)
    assert result["outcome"] == "estopped"


async def test_every_step_is_recorded(loop_service):
    """So the operator can see what it did and why it stopped, rather than
    being told "not converged"."""
    arena = SimulatedArena([(0.5, 2.0, 0.0), (3.5, 2.0, 0.0)])
    detector = await _settled(loop_service, arena)
    try:
        result = await loop_service.align_formation(
            Formation(distance_m=1.0, mover="b"), max_steps=3, settle_s=0.02,
        )
    finally:
        detector.cancel()
        with pytest.raises(asyncio.CancelledError):
            await detector

    assert result["steps"]
    step = result["steps"][0]
    assert step["moves"], "a step with no commands is not a step"
    assert "distance_error_m" in step["measured"]
    assert step["measured"]["distance_m"] > 0


# ─── through the console ──────────────────────────────────────────────


@pytest.fixture
def form_client(tmp_path):
    """A console with the arena camera configured, for the formation routes."""
    import json as _json

    from starlette.testclient import TestClient

    from unitree_fleet_web.app import create_app
    from unitree_fleet_web.service import ConsoleService

    path = tmp_path / "calibration.json"
    path.write_text(_json.dumps(CALIBRATION), encoding="utf-8")
    roster = Roster.from_mapping(
        {"state_dir": str(tmp_path / "state"), "default": "dog1", "robots": [
            {"id": "dog1", "model": "Go2", "ip": "192.168.31.245", "sn": "A"},
            {"id": "dog2", "model": "Go2", "ip": "192.168.31.246", "sn": "B"}]}
    )
    config = ConsoleConfig(host="127.0.0.1", deadman_s=0.5,
                           arena_camera="sim", arena_calibration=str(path))
    service = ConsoleService(FleetManager(roster, backend="mock", audit_to_stderr=False),
                             config=config)
    service.start_arena()
    app = create_app(service, config)
    app._console_service = service
    with TestClient(app) as client:
        client.app = app
        yield client


def test_starting_a_formation_reports_progress_in_the_payload(form_client):
    """A run is seconds long, so it cannot be a request the browser waits on —
    and the operator has to be able to watch it and stop it."""
    service = form_client.app._console_service
    arena = SimulatedArena([(1.0, 2.0, 0.0), (2.8, 2.0, 0.0)])
    wire(service, arena)
    service._arena = {
        "state": "ok",
        "poses": {
            "dog1": {"x": 1.0, "y": 2.0, "heading_deg": 0.0},
            "dog2": {"x": 2.8, "y": 2.0, "heading_deg": 0.0},
        },
    }

    started = form_client.post("/api/fleet/formation", json={
        "distance_m": 1.0, "bearing_deg": 0.0, "mover": "b",
    })
    assert started.status_code == 200, started.text
    assert started.json()["state"] == "running"

    # The status push carries it, so the page can show progress without polling
    # a second endpoint.
    status = form_client.get("/api/fleet/status").json()
    assert "formation" in status
    assert status["formation"]["target"]["distance_m"] == 1.0


def test_a_second_formation_cannot_start_on_top_of_the_first(form_client):
    """Two alignment loops driving the same pair is two loops fighting."""
    service = form_client.app._console_service
    import asyncio

    async def slow(*_args, **_kwargs):
        await asyncio.sleep(5)
        return {"outcome": "converged", "detail": "", "converged": True, "steps": []}

    service.align_formation = slow  # type: ignore[assignment]
    service._formation = {"state": "running"}
    service._formation_task = None  # the guard checks the task, so make one

    async def run():
        loop = asyncio.get_running_loop()
        service._formation_task = loop.create_task(asyncio.sleep(5))
        try:
            service.start_formation({"distance_m": 1.0})
            assert False, "it allowed a second run"
        except Exception as exc:  # noqa: BLE001
            assert getattr(exc, "code", "") == "formation_running"
        finally:
            service._formation_task.cancel()

    # TestClient runs the app on its own loop; drive this one directly.
    asyncio.run(run())


def test_stopping_a_formation_stops_the_dogs(form_client):
    service = form_client.app._console_service
    response = form_client.post("/api/fleet/formation/stop", json={})
    assert response.status_code == 200
    assert response.json()["state"] in ("stopped", "idle")


def test_bad_formation_parameters_are_refused_with_a_type(form_client):
    response = form_client.post("/api/fleet/formation", json={"distance_m": "far"})
    assert response.status_code == 400
    assert response.json()["code"] == "bad_formation"
    # The refusal has to say which field, or the operator is left guessing
    # which of five inputs was wrong.
    payload = response.json()
    said = str(payload.get("error") or payload.get("message") or payload.get("hint") or "")
    assert "数字" in said, payload


def test_every_parameter_is_range_checked(form_client):
    """A dataclass does not type-check, and `Formation(distance_m="far")` is
    accepted without complaint — then blows up inside the loop seconds later
    with an error that says nothing about the form the operator filled in."""
    for body, why in (
        ({"distance_m": "far"}, "a string"),
        ({"distance_m": -1.0}, "a negative distance"),
        ({"bearing_deg": 1e6}, "an absurd bearing"),
        ({"mover": "dog3"}, "a dog that is not in the pair"),
    ):
        response = form_client.post("/api/fleet/formation", json=body)
        assert response.status_code == 400, f"{why} was accepted"
        assert response.json()["code"] == "bad_formation"


def test_infinities_and_nans_are_refused_at_the_parser():
    """They cannot arrive over HTTP — JSON has no infinity — so this is the
    layer that can be asked. Defensive rather than reachable, and worth having
    because `Formation(distance_m=float("inf"))` is constructible in Python and
    would make every later comparison false rather than raising."""
    from unitree_fleet_web.formation import parse_formation

    for value in (float("inf"), float("-inf"), float("nan")):
        with pytest.raises(ValueError, match="有限"):
            parse_formation({"distance_m": value})


def test_the_page_carries_the_formation_controls(form_client):
    html = form_client.get("/").text
    for marker in ("form-distance", "form-bearing", "form-spread", "form-mover",
                   "form-start", "form-stop", "form-steps"):
        assert marker in html, f"the page lost {marker}"
    source = form_client.get("/static/console.js").text
    for marker in ("renderFormation", "FORMATION_STATES", "startFormation"):
        assert marker in source, f"console.js lost {marker}"


async def test_stop_all_stops_a_running_formation(loop_service):
    """**The button an operator reaches for.**

    `stop_all` cancelled `_routine_task`, and its own comment explains why that
    matters — *"leaving it running would mean the fleet stops and then the
    sequence immediately issues its next step"* — and then never touched
    `_formation_task`. The formation loop only checks the e-stop latch, so it
    planned again on its next iteration and called `robot_walk`.

    Reproduced before the fix: one command before STOP ALL, **two three seconds
    later**, with `formation["state"]` still `running`.

    The tests that existed used the formation's own stop endpoint (which does
    cancel the task) and the e-stop (which works, because the latch makes
    `call_tool` refuse the motion-gated tools). **STOP ALL was the one stop that
    left the loop running**, and it is the one an operator reaches for.

    Commands are observed by wrapping `call_tool` rather than by driving the
    simulated arena: the question is whether the loop *issued* anything, which
    is one level above what the dogs did with it.
    """
    # Two dogs far enough apart that the formation needs several steps, so the
    # loop is still running when STOP ALL arrives.
    arena = SimulatedArena([(1.0, 2.0, 0.0), (4.0, 2.0, 0.0)])
    # `_settled` starts the detector loop as well as wiring the arena — without
    # it the formation waits forever for a measurement that never arrives, and
    # the test would "pass" by never reaching the thing it is testing.
    detector = await _settled(loop_service, arena)
    await loop_service.connect("dog1")
    await loop_service.connect("dog2")

    try:
        loop_service.start_formation(
            {"distance_m": 1.0, "bearing_deg": 0.0, "mover": "b"}
        )
        # Wait until the loop has actually commanded something, so the test is
        # about a *running* formation rather than a starting one.
        for _ in range(120):
            await asyncio.sleep(0.05)
            if arena.commands:
                break
        assert arena.commands, "the formation never issued a command"

        before = len(arena.commands)
        await loop_service.stop_all(reason="test_stop_all")

        # Three seconds is several loop iterations — long enough that a loop
        # which was not cancelled would have commanded again. Measured before
        # the fix: one more command, and state still `running`.
        await asyncio.sleep(3.0)

        assert len(arena.commands) == before, (
            f"the formation issued {len(arena.commands) - before} more command(s) "
            f"after STOP ALL: {arena.commands[before:]}"
        )
        assert loop_service.formation_state().get("state") != "running", (
            "STOP ALL left the formation reporting "
            f"{loop_service.formation_state().get('state')!r}"
        )
    finally:
        detector.cancel()
        with pytest.raises(asyncio.CancelledError):
            await detector
        for task in (loop_service._motion_task, loop_service._deadman,
                     loop_service._lease):  # noqa: SLF001
            if task is not None:
                task.cancel()
