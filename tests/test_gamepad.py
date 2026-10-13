"""The gamepad → wireless-controller mapping, exercised in node.

`unitree_fleet_web/static/gamepad.js` is written to load both ways: as a plain
browser script and as a CommonJS module. That makes the arithmetic — deadzone
rescaling, axis inversion, the 16-bit button mask — testable, which matters
because getting it wrong does not fail loudly. It makes a different button do
the thing.

Skipped when node is unavailable; the file is still served and syntax-checked
by the console tests.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

GAMEPAD_JS = Path(__file__).resolve().parent.parent / "unitree_fleet_web" / "static" / "gamepad.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed"
)


def run_node(script: str):
    """Evaluate a snippet against the mapping module, returning parsed JSON."""
    harness = (
        f"const g = require({json.dumps(str(GAMEPAD_JS))});\n"
        f"const out = (() => {{ {script} }})();\n"
        f"process.stdout.write(JSON.stringify(out));\n"
    )
    result = subprocess.run(
        ["node", "-e", harness], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# ─── the button mask ──────────────────────────────────────────────────


def test_the_button_order_matches_the_documented_table():
    """Bit order is the part that silently does the wrong thing.

    These names and their order come from the decompiled Unitree app and are
    independently reproduced by `unitree_ui`'s gamepad manager and by this
    repo's own `docs/r1_action_list.md`.
    """
    order = run_node("return g.BUTTON_ORDER;")
    assert order == [
        "R1", "L1", "Start", "Select", "R2", "L2", "F1", "F2",
        "A", "B", "X", "Y", "Up", "Right", "Down", "Left",
    ]


def test_a_single_button_sets_exactly_its_bit():
    """One press, one bit — no phantom neighbours.

    Explicit bit numbers, not `enumerate` over a shortened list: F1/F2 sit at
    bits 6 and 7 and are deliberately skipped, so positional indexing would
    silently test the wrong bits.
    """
    cases = [
        (0, "R1"), (1, "L1"), (2, "Start"), (3, "Select"),
        (4, "R2"), (5, "L2"),
        (8, "A"), (9, "B"), (10, "X"), (11, "Y"),
        (12, "Up"), (13, "Right"), (14, "Down"), (15, "Left"),
    ]
    for bit, name in cases:
        index = run_node(f"return g.BUTTON_INDICES[{bit}];")
        assert index is not None, f"{name} should map to a pad button"
        mask = run_node(
            "const b = new Array(17).fill(null).map(() => ({pressed:false,value:0}));"
            f"b[{index}] = {{pressed:true, value:1}};"
            "return g.keys({buttons: b, axes: [0,0,0,0]});"
        )
        assert mask == 1 << bit, f"{name} should be bit {bit}, got mask {mask}"


def test_nothing_pressed_is_a_zero_mask():
    assert run_node(
        "const b = new Array(17).fill(null).map(() => ({pressed:false,value:0}));"
        "return g.keys({buttons: b, axes: [0,0,0,0]});"
    ) == 0


def test_an_analogue_trigger_counts_as_pressed():
    """Some pads report a trigger through `value` and never set `pressed`."""
    index = run_node("return g.BUTTON_INDICES[4];")  # R2
    mask = run_node(
        "const b = new Array(17).fill(null).map(() => ({pressed:false,value:0}));"
        f"b[{index}] = {{pressed:false, value:0.9}};"
        "return g.keys({buttons: b, axes: [0,0,0,0]});"
    )
    assert mask == 1 << 4


def test_buttons_a_gamepad_cannot_produce_are_never_set():
    """F1/F2 have no standard-mapping equivalent, so pressing everything the
    pad *does* have must leave exactly those two bits clear."""
    mask = run_node(
        "const b = new Array(17).fill(null).map(() => ({pressed:true,value:1}));"
        "return g.keys({buttons: b, axes: [0,0,0,0]});"
    )
    assert mask & (1 << 6) == 0, "F1 must stay clear"
    assert mask & (1 << 7) == 0, "F2 must stay clear"
    # Everything else is pressed.
    assert mask == 0xFFFF & ~(1 << 6) & ~(1 << 7)


def test_a_missing_button_array_is_not_a_crash():
    assert run_node("return g.keys({});") == 0
    assert run_node("return g.keys(null);") == 0


# ─── the axes ─────────────────────────────────────────────────────────


def test_the_two_sticks_map_to_the_expected_axes():
    """Left stick translates, right stick yaws, and the pad's Y axes are
    inverted relative to the robot's."""
    frame = run_node(
        "return g.axes({axes: [0.5, 0.5, -0.5, -0.5]});"
    )
    # Values are deadzone-rescaled, hence 0.42/0.92 rather than 0.5.
    assert frame["lx"] == pytest.approx(0.42 / 0.92)
    assert frame["ly"] == pytest.approx(-0.42 / 0.92)
    assert frame["rx"] == pytest.approx(-0.42 / 0.92)
    assert frame["ry"] == pytest.approx(0.42 / 0.92)
    assert frame["ly"] == -frame["lx"] and frame["ry"] == -frame["rx"]


def test_a_stick_at_rest_gives_zero():
    assert run_node("return g.axes({axes: [0, 0, 0, 0]});") == {
        "lx": 0, "ly": 0, "rx": 0, "ry": 0
    }


def test_the_deadzone_zeroes_small_drift_but_rescales_the_rest():
    """Without the rescale, usable travel starts at the deadzone edge and the
    robot never quite reaches full speed."""
    assert run_node("return g.deadzone(0.03, 0.08);") == 0
    assert run_node("return g.deadzone(-0.03, 0.08);") == 0
    # Just past the edge: near zero, not near the edge.
    assert run_node("return g.deadzone(0.09, 0.08);") < 0.02
    # Full deflection still reaches exactly full deflection.
    assert run_node("return g.deadzone(1.0, 0.08);") == 1
    assert run_node("return g.deadzone(-1.0, 0.08);") == -1


def test_the_rescale_preserves_sign_and_never_exceeds_full_deflection():
    values = run_node(
        "return [-1.5, -1, -0.5, -0.1, 0, 0.1, 0.5, 1, 1.5]"
        ".map(v => g.deadzone(v, 0.1));"
    )
    assert values[0] == -1 and values[-1] == 1
    assert all(-1 <= v <= 1 for v in values)
    assert values[:4] == sorted(values[:4])          # monotonic, negative side
    assert values[4:] == sorted(values[4:])          # monotonic, positive side


def test_a_missing_axis_is_treated_as_centred():
    assert run_node("return g.axes({axes: []});") == {
        "lx": 0, "ly": 0, "rx": 0, "ry": 0
    }
    assert run_node("return g.axes(null);") == {"lx": 0, "ly": 0, "rx": 0, "ry": 0}


# ─── frames and honesty about mapping ─────────────────────────────────


def test_a_idle_frame_is_recognised():
    assert run_node("return g.isIdle({lx:0,ly:0,rx:0,ry:0,keys:0});") is True
    assert run_node("return g.isIdle({lx:0,ly:0.2,rx:0,ry:0,keys:0});") is False
    assert run_node("return g.isIdle({lx:0,ly:0,rx:0,ry:0,keys:4});") is False


def test_a_full_frame_carries_all_five_fields_the_robot_accepts():
    """Dropping `ry` or `keys` would make a gamepad half-work: the sticks
    steer but the body-rotation axis and every button do nothing."""
    frame = run_node(
        "const b = new Array(17).fill(null).map(() => ({pressed:false,value:0}));"
        "b[0] = {pressed:true, value:1};"  # A
        "return g.frameFor({axes: [0.5, 0, 0, 0], buttons: b, mapping: 'standard'});"
    )
    assert set(frame) == {"lx", "ly", "rx", "ry", "keys"}
    assert frame["lx"] == pytest.approx(0.42 / 0.92)
    assert frame["keys"] == 1 << 8  # A is bit 8, and only bit 8


def test_a_non_standard_pad_is_flagged_rather_than_guessed():
    """A pad that is not in `standard` mapping reports arbitrary button
    indices, so the mask is meaningless. Saying so beats driving on a guess."""
    assert run_node("return g.mappingWarning({mapping: 'standard'});") == ""
    assert run_node("return g.mappingWarning({mapping: ''});") != ""
    assert run_node("return g.mappingWarning({});") != ""


def test_connected_pads_skips_empty_slots():
    """`navigator.getGamepads()` returns a fixed-length list with holes."""
    assert run_node("return g.connectedPads([null, {index:1}, null, {index:3}]);") == [1, 3]
    assert run_node("return g.connectedPads([]);") == []
    assert run_node("return g.connectedPads(undefined);") == []
