"""What happens when the browser stops, and nobody is watching the dog.

The `wpp` branch has this bug, and finding it is why this file exists. It
drives with `Move`, which **persists** — the robot keeps applying the last
velocity until it is told otherwise — and:

* its all-dogs path never sends `StopMove` while the single-dog path does
  (`dual_dog_movement.py:336-338` vs `:409-415`),
* there is no key-release stop and no `blur` handler
  (`static/js/app.js:121-125`, `keyboard_controller.py:105-113`),
* and there is no debounce or throttle anywhere.

So holding W and alt-tabbing leaves a dog walking. This console drives with
joystick frames instead, which the firmware treats as momentary — but that is
only a reason to expect it is safe, not evidence that it is. These tests are the
evidence.

The four ways a browser stops sending all look identical from the server: a
closed tab, a sleeping laptop, a dropped Wi-Fi link, a backgrounded tab whose
timers the browser has throttled to 1 Hz. In every one of them the robot keeps
applying the last frame it received.
"""

from __future__ import annotations

import asyncio

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


async def _drive_until_moving(service, robot_id: str = "dog1") -> None:
    """Stand the dog, then hold the stick until the ramp has built up speed.

    The stand is not incidental. A fresh robot's posture is `unknown`, so the
    auto-stand gate holds the first drive of every session for about a second
    while it asks the dog to get up — which is correct behaviour and would
    otherwise make this helper time out and report that nothing was tested.
    """
    await service.call_tool(robot_id, "robot_stand", {"confirm": True})
    for _ in range(60):
        await service.teleop(robot_id, ly=1.0)
        await asyncio.sleep(0.02)
        if service.motion(robot_id).commanded[0] > 0.2:
            return
    raise AssertionError("the ramp never built up, so nothing was tested")


async def test_frames_stopping_zeroes_the_robot(service):
    """The core property. Everything else here is a variation on how the frames
    stop; this is what has to happen once they do."""
    await service.connect("dog1")
    await service.start()
    try:
        await _drive_until_moving(service)
        assert service.motion("dog1").commanded[0] > 0.2

        # The operator alt-tabs. Frames stop. Nothing else is sent.
        await asyncio.sleep(1.2)

        controller = service.motion("dog1")
        assert controller.commanded == (0.0, 0.0, 0.0), (
            "the robot was still being told to move after the frames stopped"
        )
        assert controller.state in ("stopped", "idle"), controller.state
    finally:
        await _stop(service)


async def test_the_watchdog_zeroes_the_frame_the_robot_sees(service):
    """Not just the ramp's internal value — the frame has to go out. A watchdog
    that only clears its own bookkeeping leaves the robot applying the last
    frame it received, which is the whole failure."""
    await service.connect("dog1")
    await service.start()
    sent = []
    original = service._frame  # noqa: SLF001

    async def record(robot_id, **kwargs):
        sent.append((robot_id, kwargs))
        return await original(robot_id, **kwargs)

    service._frame = record  # type: ignore[assignment]
    try:
        await _drive_until_moving(service)
        sent.clear()
        await asyncio.sleep(1.2)

        zeroed = [
            kwargs for robot_id, kwargs in sent
            if robot_id == "dog1"
            and kwargs.get("ly") == 0.0 and kwargs.get("lx") == 0.0
        ]
        assert zeroed, "no zero frame was ever sent to the robot"
    finally:
        await _stop(service)


async def test_a_throttled_background_tab_still_stops(service):
    """Browsers throttle `setInterval` in a background tab to about 1 Hz.

    So the frames do not stop — they arrive once a second, which is longer than
    the dead-man and short enough that the robot never quite gets a full stop
    from the client. The watchdog has to be the thing that ends it, because the
    client cannot be trusted to be running at the rate it promised.
    """
    await service.connect("dog1")
    await service.start()
    try:
        await _drive_until_moving(service)

        # One frame a second, forever, is what a throttled tab sends.
        for _ in range(3):
            await asyncio.sleep(1.0)
            await service.teleop("dog1", ly=1.0)
            # Between those frames the ramp must have been zeroed, so the
            # speed can never build back to what a live tab would hold.
            assert service.motion("dog1").commanded[0] < 0.2, (
                "a 1 Hz trickle of frames kept the robot walking"
            )
    finally:
        await _stop(service)


async def test_a_stop_does_not_need_a_browser_at_all(service):
    """The watchdog runs on the server, so it works with the tab closed, the
    laptop asleep, and no websocket in sight."""
    await service.connect("dog1")
    await service.start()
    try:
        await _drive_until_moving(service)
        assert service.watching is False, "a browser is watching, so this proves nothing"

        await asyncio.sleep(1.2)

        assert service.motion("dog1").commanded == (0.0, 0.0, 0.0)
    finally:
        await _stop(service)


async def test_releasing_the_stick_ramps_down_rather_than_cutting(service):
    """The other half, and the reason the watchdog is not the only mechanism:
    an ordinary release should decelerate. A console that stopped dead every
    time the operator let go would be unusable, and one that relied on the
    watchdog alone would lurch."""
    await service.connect("dog1")
    await service.start()
    try:
        await _drive_until_moving(service)

        release = await service.release_drive()
        assert release is not None
        await asyncio.sleep(0.06)

        commanded = service.motion("dog1").commanded[0]
        assert commanded > 0.0, "the ramp was cut instead of decelerated"
        assert service.motion("dog1").state in ("stopping", "idle", "stopped")
    finally:
        await _stop(service)
