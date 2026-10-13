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


# ─── the general form of the bug ──────────────────────────────────────


def test_every_ctx_attribute_a_tool_reads_is_bound_to_the_call():
    """**The complete version of the grep.**

    The bug was one attribute missing from a list of delegates. The specific
    four are fixed; this checks the *rule*, so the fifth one — whenever somebody
    reaches for `ctx.<something new>` — cannot reintroduce it.

    Every `ctx.<name>` a tool body reads has to be one of:

    * a property that returns `self.active.<name>`,
    * a method on `ToolContext` (they use `self.session` etc. inside),
    * a dataclass field, which is fixed by design,
    * or, for exactly one entry, `runtime` — the default robot, which is correct
      only in single-robot branches.

    Anything else is a name nobody has classified, and that is how this started.
    """
    import dataclasses
    import inspect
    import re
    from pathlib import Path

    from unitree_robot_agent.tools.context import ToolContext

    tools = Path(__file__).resolve().parent.parent / "unitree_robot_agent" / "tools"
    used: set = set()
    for path in sorted(tools.glob("*.py")):
        for line in path.read_text().splitlines():
            code = line.split("#")[0]
            used.update(re.findall(r"\bctx\.([A-Za-z_][A-Za-z0-9_]*)", code))

    source = inspect.getsource(ToolContext)
    follows = set(re.findall(r"def (\w+)\(self\):\s*\n\s*return self\.active\.", source))
    functions = {name for name, _ in inspect.getmembers(ToolContext, inspect.isfunction)}
    fields = {field.name for field in dataclasses.fields(ToolContext)}

    #: Correct by design: the default robot, used only where there is no fleet.
    #: Asserted separately below rather than waved through.
    allowed_fixed = {"runtime", "fleet"}

    unexplained = sorted(
        name for name in used
        if name not in follows and name not in functions
        and name not in fields and name not in allowed_fixed
    )
    assert not unexplained, (
        f"tools read ctx.{unexplained}, which ToolContext does not classify. "
        f"If it comes from the runtime it must be a property returning "
        f"self.active.<name>, or it will silently mean the default robot."
    )


def test_the_only_unbound_attribute_is_the_default_robot_itself():
    """`ctx.runtime` is allowed to be the default — that is what it is for — but
    it may only be used where there is no fleet to be wrong about."""
    import re
    from pathlib import Path

    tools = Path(__file__).resolve().parent.parent / "unitree_robot_agent" / "tools"
    guards = {"fleet.py": 'if ctx.fleet is None:'}

    offenders = []
    for path in sorted(tools.glob("*.py")):
        source = path.read_text()
        for match in re.finditer(r"ctx\.runtime\b", source):
            line_start = source.rfind("\n", 0, match.start()) + 1
            if source[line_start:match.start()].strip().startswith("#"):
                continue
            # Look back a few lines for the single-robot guard.
            window = source[max(0, match.start() - 400):match.start()]
            if guards.get(path.name, "\0") not in window:
                number = source[:match.start()].count("\n") + 1
                offenders.append(f"{path.name}:{number}")

    assert not offenders, (
        f"ctx.runtime used outside a single-robot branch at {offenders} — "
        f"inside a fleet that is the default robot, not the bound one"
    )


# ─── the other families, behaviourally ────────────────────────────────


async def test_reading_faults_reads_the_right_robot(console, fleet):
    """`ctx.runtime.faults` had the same defect, and this checks what it can.

    **It is not a guard for that bug, and saying so matters.** `robot_id` in the
    payload comes from `ctx.ok()`, which follows the binding, so it is correct
    even when the tool read the wrong robot — verified by running this against
    the pre-fix code, where it passed. The faults themselves are identical on
    two mock robots, so there is nothing here to tell them apart.

    `test_auth_status_is_read_per_robot` is the one that catches it, because the
    key cache path carries the robot id. This test stays as the smoke check that
    the family is reachable and answers at all.
    """
    await _standing(console, "dog1")
    await _standing(console, "dog2")

    for robot_id in ("dog1", "dog2"):
        result = await console.call_tool(robot_id, "robot_errors", {})
        assert result.get("robot_id") == robot_id, result
        assert result.get("ok") is not False or "error" in result


async def test_auth_status_is_read_per_robot(fleet):
    """The key question: the two dogs are **different firmware generations**, so
    reading the wrong one's auth state is not a cosmetic error — it is the
    difference between "this dog needs a key" and "this dog does not".

    Driven through the **bridge** rather than the console, because
    `robot_auth_status` is not in the console's allowlist. The bridge is the
    layer the bug lived in: it calls the tool function with a `robot_id`, and
    the function decides which robot that means.
    """
    from unitree_robot_agent.bridge import ToolBridge
    from unitree_robot_agent.server import build_server

    bundle = build_server(fleet.resolve("dog1"), fleet=fleet, fleet_from_env=False)
    bridge = ToolBridge(bundle)

    for robot_id in ("dog1", "dog2"):
        result = await bridge.call("robot_auth_status", {"robot_id": robot_id})
        assert isinstance(result, dict), result
        assert result.get("robot_id") == robot_id, (
            f"asked for {robot_id}, the tool answered about "
            f"{result.get('robot_id')!r}"
        )
        # **`robot_id` alone proves nothing here** — it comes from `ctx.ok()`,
        # which follows the binding, so it is right even when the tool read the
        # wrong robot. The key cache path is per-robot and does not:
        #
        #     dog1 -> .../dog1/keys.json
        #     dog2 -> .../dog2/keys.json
        #
        # so a cross-wired read shows up as the wrong directory. Without this
        # assertion the test passed against the broken code.
        assert f"/{robot_id}/" in str(result.get("key_cache") or ""), (
            f"asked about {robot_id} and got a key cache at "
            f"{result.get('key_cache')!r}"
        )


async def test_the_fleet_is_actually_two_robots(fleet):
    """A guard on the guard: if the fixture ever became single-robot, every test
    in this file would pass for the wrong reason."""
    assert len(fleet.ids) >= 2, fleet.ids
    assert fleet.resolve("dog1") is not fleet.resolve("dog2")
