"""Robustness: what the console looks like after running for a long time.

Every other console test checks a single interaction. These check the ones
that only matter in aggregate — leaked per-robot entries, tasks that outlive
the service, a lock left held by a cancelled viewer, a counter that only ever
goes up. None of it is visible in a five-minute session and all of it is
visible in a five-hour one.

Written as an audit of my own work rather than against a bug report, so some
of these passed first time. The point is that they now fail loudly if a future
change reintroduces the shape.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from unitree_robot_agent.errors import UnknownRobot
from unitree_fleet_web.service import ConsoleService


@pytest.fixture
def service(tmp_path):
    from unitree_robot_agent.fleet import FleetManager, Roster
    from unitree_fleet_web import ConsoleConfig

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
    fleet = FleetManager(roster, backend="mock", audit_to_stderr=False)
    return ConsoleService(fleet, config=ConsoleConfig(deadman_s=0.2))


# ─── per-robot state must not grow with junk ids ──────────────────────


async def test_an_unknown_robot_id_leaves_no_trace(service):
    """A route that forwarded an arbitrary id would otherwise grow a lock (and
    a video-health entry) per request, forever."""
    before = len(service._frame_locks)  # noqa: SLF001

    for _ in range(20):
        with pytest.raises(UnknownRobot):
            await service.frame("ghost")

    assert len(service._frame_locks) == before, "a lock was created for a robot that does not exist"
    assert "ghost" not in service._video  # noqa: SLF001


async def test_repeated_use_does_not_grow_the_per_robot_maps(service):
    await service.connect("dog1")
    for _ in range(30):
        await service.status()
        await service.teleop("dog1", ly=0.5)
        await service.frame("dog1")
        service.video_status("dog1")
        service.video_status("ghost")

    assert set(service._telemetry) <= set(service.fleet.ids)  # noqa: SLF001
    assert set(service._video) <= set(service.fleet.ids)  # noqa: SLF001
    assert set(service._frame_locks) <= set(service.fleet.ids)  # noqa: SLF001


# ─── tasks must not multiply or outlive the service ───────────────────


async def test_starting_twice_does_not_duplicate_the_background_tasks(service):
    await service.start()
    first = (service._deadman, service._lease, service._telemetry_task)  # noqa: SLF001
    await service.start()
    second = (service._deadman, service._lease, service._telemetry_task)  # noqa: SLF001

    assert first == second, "start() spawned a second set of loops"
    await service.aclose()


async def test_aclose_cancels_every_background_task(service):
    await service.start()
    tasks = [t for t in (service._deadman, service._lease, service._telemetry_task) if t]  # noqa: SLF001
    assert len(tasks) == 3

    await service.aclose()

    assert all(task.cancelled() or task.done() for task in tasks)
    assert service._deadman is None and service._lease is None  # noqa: SLF001
    assert service._telemetry_task is None  # noqa: SLF001


async def test_aclose_does_not_leave_an_auto_connect_running(service):
    """A connect started by a keystroke must not outlive the console.

    It is the one background task with a reason to keep running while the loop
    does other work, so nothing else stops it — it needs cancelling explicitly,
    or it wakes up seconds later against a session that has been closed.

    Asserted on the loop's task list rather than on a task handle, because the
    point is that *nothing* is left, not that one particular object is done.
    """
    started = asyncio.Event()

    async def slow_connect():
        started.set()
        await asyncio.sleep(30)

    service.fleet.get("dog1").session.ensure_connected = slow_connect  # type: ignore[assignment]
    await service.teleop("dog1", ly=1.0)
    await asyncio.wait_for(started.wait(), timeout=2)

    assert service._connect_tasks, "the auto-connect should be tracked"  # noqa: SLF001

    await service.aclose()
    await asyncio.sleep(0.05)

    assert service._connect_tasks == {}  # noqa: SLF001
    leaked = [
        task for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and not task.done()
        and "autoconnect" in (task.get_name() or "")
    ]
    assert not leaked, f"auto-connect outlived the console: {leaked}"


async def test_a_finished_auto_connect_stops_being_tracked(service):
    """The map is a set of *in-flight* connects, not a history of every robot
    ever driven."""
    await service.teleop("dog1", ly=1.0)

    for _ in range(40):
        await asyncio.sleep(0.02)
        if not service._connect_tasks:  # noqa: SLF001
            break

    assert service._connect_tasks == {}  # noqa: SLF001
    assert service._connecting == set()  # noqa: SLF001


# ─── a cancelled viewer must not keep the frame lock ──────────────────


async def test_a_viewer_that_disconnects_releases_the_frame_lock(service):
    """A leaked lock would wedge the stream for every later viewer."""
    await service.connect("dog1")

    stream = service.mjpeg("dog1")
    first = await asyncio.wait_for(stream.__anext__(), timeout=5)
    assert first.startswith(b"--unitree-fleet-frame")

    await stream.aclose()

    # The lock must be free, so a fresh viewer gets a frame promptly.
    assert not service._frame_locks["dog1"].locked()  # noqa: SLF001
    later = await asyncio.wait_for(service.frame("dog1"), timeout=5)
    assert later is not None


async def test_two_viewers_serialise_rather_than_corrupt(service):
    """Each robot's snapshot path shares one frame waiter, so concurrent
    callers must be serialised — a race there would hand one viewer another's
    frame, or two the same one."""
    await service.connect("dog1")

    frames = await asyncio.gather(*(service.frame("dog1") for _ in range(4)))
    assert all(f is not None and f[:2] == b"\xff\xd8" for f in frames)


# ─── the transport counters must be able to go down ───────────────────


def test_a_rejected_websocket_never_becomes_a_watch(tmp_path):
    """Auth runs before the handler, so a refused socket must not be counted.

    If it were, an unauthenticated client could hold the robots' lease open —
    and each refused connection would leak a watcher that never disconnects.
    """
    from starlette.testclient import TestClient

    from tests.test_console_app import TOKEN, make_app

    with TestClient(make_app(tmp_path, token=TOKEN)) as client:
        service = getattr(client.app, "_console_service")
        assert service._clients == 0  # noqa: SLF001

        with pytest.raises(Exception):
            with client.websocket_connect("/ws"):
                pass

        assert service._clients == 0, "a rejected socket incremented the watch count"  # noqa: SLF001


def test_the_watch_decrement_is_symmetric(service):
    """If the decrement were skipped on an error path, one dropped socket
    would pin the robots' lease until the process restarted."""
    service.client_connected()
    assert service.watching is True

    service.client_disconnected()
    service._last_watch_at -= 3600  # noqa: SLF001

    assert service.watching is False
    # And it cannot go negative, which would corrupt every later count.
    service.client_disconnected()
    assert service._clients == 0  # noqa: SLF001


# ─── the fleet must survive a robot that fails everything ─────────────


async def test_an_exploding_robot_does_not_take_the_console_with_it(service):
    """Every call on dog1 raises; the console must still answer for dog2."""
    async def boom(*args, **kwargs):
        raise RuntimeError("transport exploded")

    runtime = service.fleet.get("dog1")
    runtime.session.ensure_connected = boom  # type: ignore[assignment]
    runtime.session.joystick = boom  # type: ignore[assignment]
    runtime.session.safe_stop = boom  # type: ignore[assignment]

    with contextlib.suppress(Exception):
        await service.frame("dog1")
    with contextlib.suppress(Exception):
        await service.teleop("dog1", ly=1.0)

    # dog2 is unaffected, and the console still produces a status payload.
    payload = await service.status()
    assert {r["robot_id"] for r in payload["robots"]} == {"dog1", "dog2"}
    assert payload["video"]["dog2"]["state"] == "offline"
    await service.aclose()


async def test_teleop_after_a_failed_connect_is_not_a_crash(service):
    async def refuse():
        from unitree_robot_agent.errors import RobotBusy

        raise RobotBusy("someone else holds the slot")

    service.fleet.get("dog1").session.ensure_connected = refuse  # type: ignore[assignment]

    # The frame is dropped, but the call returns rather than raising into the
    # 20 Hz loop.
    result = await service.teleop("dog1", ly=1.0)
    assert result["delivered"] is False


# ─── the e-stop latch must outlive a reconnect ────────────────────────


async def test_the_estop_latch_survives_a_disconnect(service):
    """Otherwise the console's single safety guarantee would be undone by a
    dropped link, which is exactly when you least want it undone."""
    await service.connect("dog1")
    await service.estop(reason="test")
    assert service.state.estop.engaged is True

    await service.disconnect("dog1")
    await service.connect("dog1")

    from unitree_fleet_web.state import MotionBlocked

    with pytest.raises(MotionBlocked):
        await service.teleop("dog1", ly=1.0)


async def test_the_estop_latch_survives_a_console_reload(service):
    """The latch lives in the service, not the page, so closing the browser
    does not silently release it."""
    await service.estop(reason="operator")
    # A fresh status read (what a reloaded page does) must still show it.
    payload = await service.status()
    assert payload["state"]["estop"]["engaged"] is True


# ─── handing the link back ────────────────────────────────────────────


async def _run_lease(service, seconds: float):
    """Run the lease loop for a bounded time, then stop it."""
    await service.start()
    try:
        await asyncio.sleep(seconds)
    finally:
        for task in (service._lease, service._deadman, service._telemetry_task):  # noqa: SLF001
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task


async def test_a_closed_console_hands_the_link_back_promptly(service):
    """"Take over safely" is the objective's word, and waiting out the 45 s
    idle timer is not prompt: the owner stands next to the dog wondering
    whether the app is broken."""
    service.config.unwatched_release_s = 0.5
    # Idle release itself is disabled here so the *unwatched* path is what is
    # being measured, not the session's own timer.
    for rid in service.fleet.ids:
        service.fleet.get(rid).config.idle_disconnect_s = 0.0
    await service.connect("dog1")
    assert service.fleet.get("dog1").session.is_connected is True

    await _run_lease(service, 2.0)

    assert service.fleet.get("dog1").session.is_connected is False
    assert "dog1" not in service._telemetry  # noqa: SLF001


async def test_a_watched_console_keeps_the_link(service):
    """The other half: the release must not fire while somebody is looking."""
    service.config.unwatched_release_s = 0.5
    for rid in service.fleet.ids:
        service.fleet.get(rid).config.idle_disconnect_s = 0.0
    await service.connect("dog1")

    service.client_connected()
    await _run_lease(service, 2.0)

    assert service.fleet.get("dog1").session.is_connected is True


async def test_the_unwatched_release_can_be_disabled(service):
    """`0` means "fall back to the session's own idle timer"."""
    service.config.unwatched_release_s = 0
    for rid in service.fleet.ids:
        service.fleet.get(rid).config.idle_disconnect_s = 0.0
    await service.connect("dog1")

    await _run_lease(service, 2.0)

    assert service.fleet.get("dog1").session.is_connected is True


async def test_handing_back_stops_the_robot_first(service):
    """The worst possible hand-back: releasing the link and leaving the dog
    walking, with the app unable to connect and stop it."""
    service.config.unwatched_release_s = 0.3
    for rid in service.fleet.ids:
        service.fleet.get(rid).config.idle_disconnect_s = 0.0
    await service.connect("dog1")
    await service.teleop("dog1", ly=1.0)
    assert service.state.driven_robots() == {"dog1"}

    stopped: list = []
    session = service.fleet.get("dog1").session
    original = session.disconnect

    async def watching_disconnect(*args, **kwargs):
        stopped.append(kwargs.get("stop_first"))
        return await original(*args, **kwargs)

    session.disconnect = watching_disconnect  # type: ignore[assignment]
    await _run_lease(service, 1.5)

    assert stopped == [True], "the hand-back did not ask for a stop first"
    assert session.is_connected is False


async def test_a_refresh_does_not_drop_the_link(service):
    """The reason the grace period exists at all: a page reload, a blip, or a
    background tab that stops polling must not cost the operator their link."""
    service.config.unwatched_release_s = 3.0
    for rid in service.fleet.ids:
        service.fleet.get(rid).config.idle_disconnect_s = 0.0
    await service.connect("dog1")

    service.client_connected()
    await _run_lease(service, 1.0)   # watched
    service.client_disconnected()    # the reload
    await _run_lease(service, 1.5)   # still inside the grace period

    assert service.fleet.get("dog1").session.is_connected is True


# ─── the telemetry budget ─────────────────────────────────────────────


def test_the_telemetry_timeout_clears_the_collector_own_budget():
    """A regression guard with a specific bug behind it.

    `collect_lowstate` waits per topic and silently omits what did not arrive,
    so its worst case is the sum of its own timeouts. The console wrapped it in
    a 2 s ceiling — shorter than either model's sum — so on a real Go2 whose
    state topic never publishes, every refresh timed out and the panels stayed
    empty. The mock answers every topic instantly, so nothing caught it.

    Against an exported constant now, not a scan of the collector's source: the
    source-scanning version silently under-counted as soon as a read sat in a
    loop, and the state read does exactly that.
    """
    from unitree_robot_agent.core.telemetry import WORST_CASE_S
    from unitree_fleet_web.service import TELEMETRY_TIMEOUT_S

    assert WORST_CASE_S > 0
    assert TELEMETRY_TIMEOUT_S > WORST_CASE_S, (
        f"telemetry ceiling {TELEMETRY_TIMEOUT_S}s does not clear "
        f"collect_lowstate's declared worst case of {WORST_CASE_S}s — every "
        f"refresh would time out on a robot with a silent topic"
    )


def test_the_declared_budget_matches_the_topics_actually_read():
    """The constant is only trustworthy while it describes the code. A Go2
    tries one state topic per entry in `state_candidates`, so the two have to
    agree or the ceiling is computed from fiction."""
    from unitree_robot_agent.core.telemetry import GO2_STATE_TOPICS
    from unitree_robot_agent.robots.go2 import Go2Profile

    assert len(Go2Profile().state_candidates) <= GO2_STATE_TOPICS, (
        "the Go2 tries more state topics than the read budget accounts for"
    )


async def test_a_telemetry_timeout_is_reported_not_swallowed(service):
    """An empty panel and a robot that reports nothing look identical, and
    they want different reactions. The reason has to reach the page."""
    await service.connect("dog1")

    async def hang(*args, **kwargs):
        raise asyncio.TimeoutError

    import unitree_fleet_web.service as service_module

    original = service_module.collect_lowstate
    service_module.collect_lowstate = hang
    try:
        await service.refresh_telemetry("dog1")
        health = service.telemetry_health("dog1")
    finally:
        service_module.collect_lowstate = original

    assert health["state"] == "failed"
    assert "timed out" in health["error"]
    assert health["has_data"] is False


async def test_a_partial_read_is_degraded_not_failed(service):
    """A robot reporting battery but not its gait is still useful — the panel
    should show what it has and say the rest is missing."""
    await service.connect("dog1")
    await service.refresh_telemetry("dog1")

    service._telemetry_error["dog1"] = "sport state unavailable"  # noqa: SLF001
    health = service.telemetry_health("dog1")

    assert health["state"] == "degraded"
    assert health["has_data"] is True


async def test_a_good_refresh_clears_a_previous_error(service):
    await service.connect("dog1")
    service._telemetry_error["dog1"] = "stale reason"  # noqa: SLF001

    await service.refresh_telemetry("dog1")

    assert service.telemetry_health("dog1")["state"] == "ok"
    assert service.telemetry_health("dog1")["error"] == ""
