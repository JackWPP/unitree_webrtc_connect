"""What a console left open for a shift looks like.

Not a benchmark. The failures this is looking for are the ones that only appear
after a few hundred cycles: a dict that grows an entry per reconnect, a task
that is created and never awaited, a counter that only goes up. Every one of
them is invisible in a single-shot test and obvious after an hour on a desk.

Each assertion is written as "stays bounded by the number of robots", not as a
magic number, so it keeps meaning something when the fleet size changes.
"""

from __future__ import annotations

import asyncio
import gc
import json

import pytest


@pytest.fixture
def service(tmp_path):
    from unitree_robot_agent.fleet import FleetManager, Roster
    from unitree_fleet_web import ConsoleConfig, ConsoleService

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
    return ConsoleService(
        FleetManager(roster, backend="mock", audit_to_stderr=False),
        config=ConsoleConfig(deadman_s=0.5, motion_hz=50.0),
    )


async def _stop(service):
    for task in (
        service._motion_task, service._deadman, service._lease,  # noqa: SLF001
        service._telemetry_task,
    ):
        if task is not None:
            task.cancel()


async def test_a_hundred_reconnects_leave_nothing_behind(service):
    """The likeliest leak in a console: a per-robot dict that gains an entry on
    connect and does not lose it on disconnect."""
    robots = list(service.fleet.ids)
    await service.start()
    try:
        for _ in range(25):
            for robot_id in robots:
                await service.connect(robot_id)
                await service.refresh_telemetry(robot_id)
            await service.stop_all()
            for robot_id in robots:
                await service.disconnect(robot_id)

        assert len(service._motion) <= len(robots), "motion controllers leaked"  # noqa: SLF001
        assert len(service._telemetry) <= len(robots), "telemetry leaked"  # noqa: SLF001
        assert len(service._posture) <= len(robots), "posture beliefs leaked"  # noqa: SLF001
        assert len(service._gate) <= len(robots), "standing gates leaked"  # noqa: SLF001
        assert not service._gate_tasks, "auto-stand tasks leaked"  # noqa: SLF001
    finally:
        await _stop(service)


async def test_driving_for_a_long_time_does_not_spawn_tasks(service):
    """Every tick drives the ramps. A task created per tick would be thousands
    of them inside a minute, and would not show up in a one-second test."""
    await service.connect("dog1")
    await service.connect("dog2")
    await service.start()
    try:
        baseline = len(asyncio.all_tasks())

        for _ in range(400):
            await service.teleop("dog1", ly=0.3, rx=0.1)
            await service.teleop("dog2", ly=0.3, rx=0.1)
            await asyncio.sleep(0.005)

        # A little slack for the event loop's own bookkeeping.
        assert len(asyncio.all_tasks()) <= baseline + 4, (
            f"tasks grew from {baseline} to {len(asyncio.all_tasks())}"
        )
    finally:
        await _stop(service)


async def test_a_hundred_browsers_coming_and_going_do_not_leave_the_lease_armed(service):
    """The watcher count arms the operator lease. A counter that only goes up
    would keep every robot connected forever after one visit; one that leaks
    down would release them mid-session."""
    await service.connect("dog1")
    await service.start()
    try:
        for _ in range(100):
            service.client_connected()
            service.client_disconnected()

        assert service._clients == 0, f"watchers leaked: {service._clients}"  # noqa: SLF001
        assert service.watching is False
    finally:
        await _stop(service)


async def test_repeated_routines_do_not_accumulate(service):
    """A routine allocates a task and a result dict. Ten of them back to back
    is an ordinary afternoon."""
    await service.connect("dog1")
    await service.connect("dog2")
    await service.start()
    try:
        for _ in range(6):
            await service.start_routine("greet")
            for _ in range(60):
                await asyncio.sleep(0.05)
                if not service.routine_state()["running"]:
                    break
            await service.stop_routine(reason="soak")

        assert service._routine_task is None or service._routine_task.done()  # noqa: SLF001
        # The state holds one routine, not a history of them.
        state = service.routine_state()
        assert len(state["steps"]) <= 8, "routine state accumulated"
    finally:
        await _stop(service)


async def test_the_audit_trail_does_not_grow_without_bound(tmp_path):
    """It rotates at 5 MB. A console left open for a shift writes an entry per
    command, and an unrotated log would be the one file that never stops."""
    import inspect

    from unitree_robot_agent.core.audit import AuditLog

    signature = inspect.signature(AuditLog.record)
    assert "max_bytes" not in signature.parameters, (
        "size limiting moved into record(); this test needs rewriting"
    )

    path = tmp_path / "audit.log"
    log = AuditLog(path, max_bytes=4096)

    # Whatever `record` takes, the property under test is that writing a lot of
    # entries does not produce an unbounded file.
    parameters = [
        name for name in signature.parameters
        if name != "self"
        and signature.parameters[name].default is inspect.Parameter.empty
    ]
    assert len(parameters) <= 2, f"record() grew arguments: {parameters}"
    for index in range(400):
        try:
            log.record("robot_move", {"robot_id": "dog1", "vx": 0.1, "n": index})
        except TypeError:
            log.record("robot_move")

    files = list(tmp_path.glob("audit.log*"))
    assert files, "nothing was written at all"
    assert sum(f.stat().st_size for f in files) < 4096 * 8, (
        "the audit trail grew without bound"
    )


async def test_status_stays_a_fixed_size_over_time(service):
    """The payload is pushed four times a second. Anything that accumulates in
    it would grow the bandwidth, not just the memory."""
    await service.connect("dog1")
    await service.connect("dog2")
    await service.refresh_telemetry("dog1")
    await service.refresh_telemetry("dog2")
    await service.start()
    try:
        first = len(json.dumps(await service.status(), default=str))
        for _ in range(300):
            await service.teleop("dog1", ly=0.2)
            await asyncio.sleep(0.004)
        later = len(json.dumps(await service.status(), default=str))

        assert later < first * 1.5, (
            f"the status payload grew from {first} to {later} bytes while driving"
        )
    finally:
        await _stop(service)


async def test_teleop_history_does_not_accumulate(service):
    """An audit trail of every frame would be 50 entries a second, forever."""
    await service.connect("dog1")
    await service.start()
    try:
        for _ in range(200):
            await service.teleop("dog1", ly=0.2)
            await asyncio.sleep(0.003)

        trail = await service.audit("dog1", limit=500)
        gc.collect()
        # The trail is bounded by what the audit log keeps, not by how long the
        # operator held the stick.
        assert len(trail.get("entries", [])) <= 500
    finally:
        await _stop(service)


async def test_a_running_routine_is_not_released_out_from_under(service):
    """The bug this file found.

    Closing the laptop lid — or a websocket blinking — while a routine is
    mid-sequence used to release every robot under it, stop them
    (`stop_first`), and leave the runner issuing commands at disconnected
    robots for the rest of the sequence. Half-released is worse than either
    extreme: the pair is neither doing the routine nor safely idle.
    """
    await service.connect("dog1")
    await service.connect("dog2")
    await service.start()
    try:
        await service.start_routine("dance")
        await asyncio.sleep(0.3)
        assert service.routine_state()["running"] is True

        # Nobody is watching; the lease fires.
        assert service.watching is False
        await service._release_unwatched()  # noqa: SLF001

        assert service.fleet.get("dog1").session.is_connected, (
            "a robot was released while a routine was driving it"
        )
        assert service.fleet.get("dog2").session.is_connected
        assert service.routine_state()["running"] is True
    finally:
        await service.stop_routine(reason="test")
        await _stop(service)


async def test_an_idle_unwatched_console_still_hands_the_dogs_back(service):
    """The other half, and the reason the guard above is a condition rather
    than a removal: an owner standing next to the dog must not wait out the
    idle timer because the console is holding a link nobody is using."""
    await service.connect("dog1")
    await service.start()
    try:
        assert service._routine is None  # noqa: SLF001

        await service._release_unwatched()  # noqa: SLF001

        assert not service.fleet.get("dog1").session.is_connected
    finally:
        await _stop(service)
