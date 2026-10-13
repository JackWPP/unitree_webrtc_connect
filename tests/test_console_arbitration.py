"""Two writers, one topic: who owns the sticks when both want them.

`robot_move`, `robot_turn` and `robot_walk` publish wireless-controller frames
directly. So does the console's own motion loop, fifty times a second, from the
ramped teleop target. **Both write the same topic and nothing arbitrated.**

Measured before the fix: an operator holding W while `robot_walk` ran produced
**297 frames from the tool with 3 from the loop spliced between them** — a robot
being told to walk and to stop alternately, and in the general case told to walk
*and* to obey a stick, at 50 Hz.

The policy now: **a tool holding a motion window owns the sticks**, and the loop
stands aside until it is done. The window already existed (`MotionController`
takes one for every closed-loop move); the loop simply was not consulting it.

This is the second half of the audit that produced `docs/wiring_audit.md`. That
one looked for wires that were not connected; this looks for wires that are
connected to each other and should not be.
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
            "robots": [{"id": "dog1", "model": "Go2", "ip": "192.168.31.245", "sn": "A"}],
        }
    )
    return ConsoleService(
        FleetManager(roster, backend="mock", audit_to_stderr=False),
        # A long dead-man so it cannot be what stops the frames, and a fast
        # motion loop so the interleaving is unambiguous.
        config=ConsoleConfig(deadman_s=30.0, motion_hz=100.0),
    )


def _spy(service, seen):
    """Record every frame **the motion loop** publishes, against the window.

    Spying on `session.joystick` records both writers and cannot tell them
    apart — the first version of this test did that and then asserted that the
    loop had published outside the window, which it legitimately had: the
    operator was holding W while the tool was still doing its guard checks and
    had not claimed the sticks yet.

    `service._frame` is the loop's own path, so a call to it *while the window
    is open* is unambiguously the thing being tested.
    """
    original = service._frame  # noqa: SLF001

    async def spy(robot_id, **kwargs):
        seen.append((
            round(float(kwargs.get("ly") or 0.0), 4),
            service.fleet.get(robot_id).motion.window_active,
        ))
        return await original(robot_id, **kwargs)

    service._frame = spy  # type: ignore[assignment]


async def test_the_loop_stands_aside_while_a_tool_drives(service):
    """Both writers are live: the operator is holding W and a walk is running."""
    await service.connect("dog1")
    await service.start()
    try:
        runtime = service.fleet.get("dog1")
        # A plausible posture, or the standing gate would hold the drive.
        await service.call_tool("dog1", "robot_stand", {"confirm": True})

        seen: list = []
        _spy(service, seen)

        # The operator holds W. The loop ramps towards it and publishes.
        await service.teleop("dog1", ly=0.5)
        for _ in range(40):
            await asyncio.sleep(0.02)
            if seen:
                break
        assert seen, "the loop never published, so there is nothing to arbitrate"

        seen.clear()
        await service.call_tool("dog1", "robot_walk", {"meters": 0.2, "confirm": True})
        await asyncio.sleep(0.05)

        # **The assertion that matters.** Any frame from the loop while the
        # window is open is the loop overwriting the tool's command, and the
        # tool is the one that knows what it is doing.
        during_window = [ly for ly, window in seen if window]
        assert not during_window, (
            f"the motion loop published {during_window} while a tool held the "
            f"window — two writers on one topic with different intents"
        )
    finally:
        for task in (service._motion_task, service._deadman, service._lease):  # noqa: SLF001
            if task is not None:
                task.cancel()


async def test_the_loop_resumes_after_the_tool_is_done(service):
    """Standing aside is not stopping. The operator's stick still works once the
    tool has let go, or a routine would leave the console unresponsive."""
    await service.connect("dog1")
    await service.start()
    try:
        await service.call_tool("dog1", "robot_stand", {"confirm": True})

        seen: list = []
        _spy(service, seen)

        await service.call_tool("dog1", "robot_walk", {"meters": 0.2, "confirm": True})
        assert not service.fleet.get("dog1").motion.window_active

        seen.clear()
        await service.teleop("dog1", ly=0.4)
        for _ in range(60):
            await asyncio.sleep(0.02)
            if any(ly for ly, _window in seen):
                break

        assert any(ly > 0 for ly, _window in seen), (
            "the loop did not resume after the tool finished"
        )
        # ...and it resumed *because* the window closed, not despite it.
        assert all(not window for _ly, window in seen)
    finally:
        for task in (service._motion_task, service._deadman, service._lease):  # noqa: SLF001
            if task is not None:
                task.cancel()


async def test_the_window_is_the_signal_and_it_is_a_property(service):
    """`motion.window_active` is what the loop consults. Asserted because the
    first probe written for this called it as a method and got back a bool that
    is not callable — a mistake in a test, and one that would be silent in
    production if the check were `if runtime.motion.window_active():`."""
    await service.connect("dog1")
    runtime = service.fleet.get("dog1")
    assert isinstance(runtime.motion.window_active, bool)
    assert runtime.motion.window_active is False
