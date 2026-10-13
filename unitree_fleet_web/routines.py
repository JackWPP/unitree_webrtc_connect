"""Routines: an ordered sequence of steps the whole fleet performs together.

The idea comes from the `wpp` branch's `MovementPattern` state machine, which
had exactly this shape — a list of timed steps driven at the fleet — and it was
the right shape. What follows is that idea rebuilt against everything the
console has learned since, because the original had four problems that only
matter on real hardware:

1. **It could not be aborted.** `stop_movement()` cancelled the task and sent
   `StopMove`, which is fine for a square walk but not for a routine holding a
   real robot mid-dance. Here a stop is a first-class state the runner checks
   between and *during* steps, and the console's latch aborts it too.
2. **Failures were a boolean.** `sum(results.values()) > 0` means "at least one
   dog did it", so one dog refusing and one succeeding looked like success.
   Every step reports per-dog, by name.
3. **Durations were guessed in an if-chain** (`_get_action_duration` returns
   3.0 / 4.0 / 6.0 by looking at the action name). They are declared on the
   step here, and shown to the operator *before* starting, because a routine is
   a commitment of time as well as motion.
4. **It bypassed the safety layer entirely** — raw commands, no `confirm`, no
   safe mode, no audit. Steps here run through the same guarded tools the
   buttons do, so all of that applies unchanged.

One more thing the original got right by accident and is deliberate here: a
routine is a *sequence*, so it needs a notion of "the previous step finished".
It uses declared durations rather than waiting for the robot, because none of
these actions reports completion — and pretending otherwise would be worse than
a timing table that is honest about being one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple


@dataclass(frozen=True)
class Step:
    """One thing the whole fleet does, then a pause."""

    label: str
    tool: str
    args: Dict[str, Any] = field(default_factory=dict)
    #: Seconds to wait after issuing it. Declared rather than derived: no tool
    #: reports when an action has finished, so this is a timing table and it
    #: says so instead of pretending to be a measurement.
    seconds: float = 3.0
    #: True when the step needs ``UNITREE_AGENT_ALLOW_DANGEROUS``. Surfaced so
    #: the operator is told before starting rather than by a mid-routine
    #: refusal.
    dangerous: bool = False


@dataclass(frozen=True)
class Routine:
    id: str
    label: str
    title: str
    steps: Tuple[Step, ...]
    #: Repeat until stopped, or run once and finish.
    loop: bool = False

    @property
    def seconds(self) -> float:
        return sum(step.seconds for step in self.steps)

    @property
    def needs_dangerous(self) -> bool:
        return any(step.dangerous for step in self.steps)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "title": self.title,
            "loop": self.loop,
            "steps": [
                {
                    "label": step.label,
                    "tool": step.tool,
                    "seconds": step.seconds,
                    "dangerous": step.dangerous,
                }
                for step in self.steps
            ],
            "seconds": round(self.seconds, 1),
            "needs_dangerous": self.needs_dangerous,
        }


def _stand() -> Step:
    return Step("站好", "robot_stand", {"confirm": True}, 2.0)


def _action(name: str, label: str, seconds: float, *, dangerous: bool = False) -> Step:
    return Step(label, "robot_play_action", {"name": name, "confirm": True}, seconds, dangerous)


#: The routines offered. Deliberately short and gentle: a routine is the one
#: control that keeps sending commands after the operator has taken their hand
#: off the keyboard, so the bar for including one is that it is obviously safe
#: to watch happen.
ROUTINES: Tuple[Routine, ...] = (
    Routine(
        id="greet",
        label="一起打招呼",
        title="站好 → 打招呼 → 伸懒腰。最温和的一条，适合确认两只狗都在听。",
        steps=(
            _stand(),
            _action("hello", "打招呼", 3.0),
            _action("stretch", "伸懒腰", 4.0),
        ),
    ),
    Routine(
        id="dance",
        label="一起跳舞",
        title="跳舞 → 比心 → 扭屁股 → 满足姿态，循环。需要危险动作许可。",
        loop=True,
        steps=(
            _stand(),
            _action("dance_1", "跳舞 1", 8.0, dangerous=True),
            _action("dance_2", "跳舞 2", 8.0, dangerous=True),
            _action("finger_heart", "比心", 3.0),
            _action("wiggle_hips", "扭屁股", 3.0),
            _action("content", "满足姿态", 3.0),
        ),
    ),
    Routine(
        id="square",
        label="走正方形",
        title="前进 0.6s → 右转 90°，重复四次回到原点。两条狗各走各的正方形。",
        steps=tuple(
            step
            for _ in range(4)
            for step in (
                Step("前进", "robot_move",
                     {"vx": 0.25, "duration_s": 0.6, "confirm": True}, 1.6),
                Step("右转 90°", "robot_turn", {"degrees": -90, "confirm": True}, 6.0),
            )
        ),
    ),
    Routine(
        id="formation",
        label="列队前进",
        title="两只狗一起前进 1 秒、一起停下，重复三次。用于确认同驱时序。",
        steps=tuple(
            step
            for _ in range(3)
            for step in (
                Step("一起前进", "robot_move",
                     {"vx": 0.25, "duration_s": 1.0, "confirm": True}, 2.0),
                Step("停下", "robot_stop", {}, 1.0),
            )
        ),
    ),
)


def routine_by_id(routine_id: str) -> Routine:
    for routine in ROUTINES:
        if routine.id == routine_id:
            return routine
    raise KeyError(routine_id)


def catalogue() -> List[Dict[str, Any]]:
    return [routine.as_dict() for routine in ROUTINES]
