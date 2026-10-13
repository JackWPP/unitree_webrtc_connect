"""Some sport commands need a payload, and sending nothing is silent.

The libraries's own examples split cleanly, and the split lands exactly on our
action table:

    needs {"data": True}     Pose, FrontFlip, LeftFlip, BackFlip
    takes nothing            Scrape, FrontJump, FrontPounce, Heart, ...

`examples/go2/data_channel/sportmode_mcf/sportmode_mcf.py:193` (`FrontFlip`) and
`:229` (`BackFlip`) are live code; the non-MCF example's `BackFlip` at
`examples/go2/data_channel/sportmode/sportmode.py:125` is commented out but
passes the same thing.

**The failure mode is why this is worth a file.** A flip that does nothing looks
exactly like a flip the firmware does not support. There is no error code, no
log line, no refusal — the dog simply stands there. That is the worst kind of
bug on a robot, and it is the kind nobody can debug from a web page.
"""

from __future__ import annotations

import pytest

from unitree_robot_agent.robots.go2 import Go2Profile

#: Taken from the MCF example's dispatch: these are sent `{"data": True}`.
NEEDS_PARAMETER = {"pose", "front_flip", "left_flip", "back_flip"}

#: ...and these are sent bare, in the same example, so sending a payload would
#: be inventing one.
TAKES_NOTHING = {"scrape", "front_jump", "front_pounce", "content", "hello"}


@pytest.fixture
def actions():
    return Go2Profile().actions()


def test_the_parameterised_actions_carry_their_payload(actions):
    for name in NEEDS_PARAMETER:
        assert name in actions, f"{name} fell out of the action table"
        assert actions[name].parameter == {"data": True}, (
            f"{name} is sent bare, and a command that needs a payload does "
            f"nothing at all without one"
        )


def test_the_bare_actions_stay_bare(actions):
    """Sending a payload to a command that ignores it is not dangerous, but it
    is a guess, and the example shows these four taking none."""
    for name in TAKES_NOTHING:
        assert name in actions, f"{name} fell out of the action table"
        assert actions[name].parameter is None, (
            f"{name} gained a payload the library's own example does not send"
        )


def test_the_split_is_exactly_the_example_s_split(actions):
    """If this fails, someone added or removed a parameter without reading the
    example. That is not automatically wrong — but it is a decision, and it
    should be made deliberately."""
    with_payload = {name for name, action in actions.items() if action.parameter}
    assert with_payload == NEEDS_PARAMETER, (
        f"the split moved: {with_payload ^ NEEDS_PARAMETER}"
    )


def test_every_parameter_is_a_json_object(actions):
    """The firmware takes an object. A bare `True` would serialise to something
    the robot has never been sent."""
    for name, action in actions.items():
        if action.parameter is not None:
            assert isinstance(action.parameter, dict), name
            assert action.parameter, f"{name} has an empty parameter"


def test_the_dispatch_actually_sends_it():
    """The field is worthless if the call site drops it — which is exactly what
    it did: `ctx.session.call(topic, api_id, None)` for every action."""
    import inspect

    from unitree_robot_agent.tools import actions as module

    source = inspect.getsource(module)
    assert "action.parameter" in source, (
        "the sport dispatch no longer passes the action's parameter"
    )
    assert "action.value, None)" not in source, (
        "a bare `None` is being sent again"
    )


def test_a_flip_is_still_marked_dangerous(actions):
    """Unrelated to payloads, and worth keeping next to them: a flip needs
    space and a soft floor, and the confirmation gate is what says so."""
    for name in ("front_flip", "back_flip", "left_flip"):
        assert actions[name].dangerous is True, name
