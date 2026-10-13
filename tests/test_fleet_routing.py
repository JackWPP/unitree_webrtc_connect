"""Every tool acts on the robot it was **called with**, not the default one.

**The bug this file exists for**, measured before it was fixed:

    call_tool("dog2", "robot_walk", {"meters": 0.15})
      -> {"ok": true, "robot_id": "dog2", "reason": "reached"}
    dog1 position: (0.149, 0.0)      <- it walked
    dog2 position: (0.0, 0.0)        <- it did not

The cause: `ToolContext` exposes per-call delegates (`config`, `profile`,
`session`, `audit`, `backend`) that follow the `robot_id` binding, and tool
bodies reached for `ctx.runtime.motion` instead — `ctx.runtime` is the **default
robot's** runtime, captured at registration and never rebound. The response is
built by `ok()`, which uses `self.config.robot_id` and therefore *does* follow
the binding. So the call **reported the robot the operator clicked while acting
on a different one**.

`ctx.session` was the only runtime attribute that followed the binding, which is
why the joystick path, the state reads and every `go2_*` tool were correct and
these four families were not: motion, faults, auth and vision.

Nothing in 900 passing tests could see it, because the two-dog tests drive a
simulated arena that intercepts `call_tool` at a level above this.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def fleet(tmp_path):
    import tempfile

    from unitree_robot_agent.fleet import FleetManager, Roster

    roster = Roster.from_mapping(
        {
            "state_dir": tempfile.mkdtemp(dir=tmp_path),
            # dog1 is the default. The bug is invisible when you address dog1.
            "default": "dog1",
            "robots": [
                {"id": "dog1", "model": "Go2", "ip": "192.168.31.245", "sn": "A"},
                {"id": "dog2", "model": "Go2", "ip": "192.168.31.246", "sn": "B"},
            ],
        }
    )
    manager = FleetManager(roster, backend="mock", audit_to_stderr=False)
    yield manager


@pytest.fixture
def console(fleet):
    from unitree_fleet_web import ConsoleConfig, ConsoleService

    return ConsoleService(fleet, config=ConsoleConfig(deadman_s=30.0))


async def _standing(console, robot_id):
    await console.connect(robot_id)
    await console.call_tool(robot_id, "robot_stand", {"confirm": True})


async def _positions(fleet):
    return {
        robot_id: await fleet.get(robot_id).motion.read_position()
        for robot_id in fleet.ids
    }


async def test_walking_dog2_walks_dog2(console, fleet):
    """The measurement, as a test."""
    await _standing(console, "dog1")
    await _standing(console, "dog2")

    result = await console.call_tool("dog2", "robot_walk", {"meters": 0.15, "confirm": True})

    assert result.get("ok") is True, result
    assert result.get("robot_id") == "dog2"
    positions = await _positions(fleet)
    assert positions["dog2"][0] > 0.05, f"dog2 did not move: {positions}"
    assert positions["dog1"][0] < 1e-6, f"dog1 moved instead: {positions}"


async def test_walking_dog1_walks_dog1(console, fleet):
    """The mirror. **Addressing the default robot worked all along**, which is
    exactly why the bug survived: every single-robot test was green."""
    await _standing(console, "dog1")
    await _standing(console, "dog2")

    await console.call_tool("dog1", "robot_walk", {"meters": 0.15, "confirm": True})

    positions = await _positions(fleet)
    assert positions["dog1"][0] > 0.05
    assert positions["dog2"][0] < 1e-6


async def test_turning_dog2_turns_dog2(console, fleet):
    """The same defect, a different tool. `robot_turn` reads the yaw from
    `ctx.runtime.motion` too, so it turned the default dog and reported the
    other — and a wrong turn is harder to notice than a wrong walk."""
    import math

    await _standing(console, "dog1")
    await _standing(console, "dog2")

    await console.call_tool("dog2", "robot_turn", {"degrees": 40, "confirm": True})

    yaw1 = await fleet.get("dog1").motion.read_yaw()
    yaw2 = await fleet.get("dog2").motion.read_yaw()
    assert abs(math.degrees(yaw2 or 0.0)) > 15, f"dog2 did not turn: {math.degrees(yaw2 or 0)}°"
    assert abs(math.degrees(yaw1 or 0.0)) < 5, f"dog1 turned instead: {math.degrees(yaw1 or 0)}°"


async def test_every_runtime_delegate_follows_the_binding():
    """Structural, so a *new* tool cannot reintroduce it.

    `ToolContext` has to expose a per-call delegate for every runtime attribute
    a tool body might reach for. `ctx.runtime` stays as the default robot and is
    correct for the handful of tools that genuinely mean "the configured one".
    """
    from unitree_robot_agent.tools.context import ToolContext

    for attribute in ("motion", "faults", "auth", "vision", "session", "config", "profile"):
        assert hasattr(ToolContext, attribute), (
            f"ToolContext has no {attribute} delegate, so a tool body reaching "
            f"for ctx.runtime.{attribute} would act on the default robot"
        )


def test_no_tool_reaches_for_a_runtime_attribute_directly():
    """The grep that would have caught it.

    `ctx.runtime.<thing>` is the default robot. Any use of it in a tool body is
    either a deliberate single-robot reference or this bug, and there is no way
    to tell them apart by reading one line — so the default is that there are
    none, and a deliberate one has to say why.
    """
    import re

    tools = Path(__file__).resolve().parent.parent / "unitree_robot_agent" / "tools"
    offenders = []
    for path in sorted(tools.glob("*.py")):
        source = path.read_text()
        for number, line in enumerate(source.splitlines(), 1):
            if re.search(r"ctx\.runtime\.(motion|faults|auth|vision)", line):
                # A comment is fine; code is not.
                if line.strip().startswith("#"):
                    continue
                offenders.append(f"{path.name}:{number}: {line.strip()}")

    assert not offenders, (
        "these reach for the default robot's runtime instead of the bound one:\n"
        + "\n".join(offenders)
    )
