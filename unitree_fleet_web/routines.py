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


#: How many dogs must succeed for a step to count as done.
#:
#: `all` is the default because "make them both do it" is the whole point of a
#: routine. `any` is for a step that only needs one dog to have done something.
#: `best_effort` never fails — for a flourish that is nice if it works.
#:
#: The reason this exists at all: the branch this idea came from gated on
#: `sum(results) > 0`, so a two-dog routine silently degraded to a one-dog
#: routine with no signal anywhere. A policy makes "one dog was offline" a
#: stated outcome instead of a rounding error.
POLICIES = ("all", "any", "best_effort")


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
    #: See POLICIES.
    policy: str = "all"

    def satisfied(self, ok_count: int, total: int) -> bool:
        if self.policy == "best_effort":
            return True
        if self.policy == "any":
            return ok_count > 0
        return total > 0 and ok_count == total

    def as_dict(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "tool": self.tool,
            "args": dict(self.args),
            "seconds": self.seconds,
            "dangerous": self.dangerous,
            "policy": self.policy,
        }


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
            "steps": [step.as_dict() for step in self.steps],
            "seconds": round(self.seconds, 1),
            "needs_dangerous": self.needs_dangerous,
            "builtin": self.id in {routine.id for routine in ROUTINES},
        }


def validate(document: Dict[str, Any], *, known_tools: Any = None) -> Routine:
    """Build a routine from an operator-authored document.

    Validation is the whole reason a routine is a document and not a Python
    list. The branch this idea came from dispatched actions by
    ``getattr(module, f"{action}_single")`` behind a bare ``hasattr`` — any
    public method reachable by name from an HTTP body. Checking every step's
    tool against the console allowlist closes that and, incidentally, is what
    lets the editor offer a palette instead of a text box.
    """
    if not isinstance(document, dict):
        raise ValueError("routine must be an object")

    routine_id = str(document.get("id") or "").strip()
    if not routine_id:
        raise ValueError("routine needs an id")
    if not routine_id.replace("_", "").replace("-", "").isalnum():
        raise ValueError("routine id may only contain letters, digits, - and _")

    label = str(document.get("label") or routine_id).strip()[:48]
    raw_steps = document.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("routine needs at least one step")
    if len(raw_steps) > 40:
        raise ValueError("a routine may have at most 40 steps")

    steps = []
    for index, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            raise ValueError(f"step {index + 1} must be an object")
        tool = str(raw.get("tool") or "").strip()
        if not tool:
            raise ValueError(f"step {index + 1} needs a tool")
        if known_tools is not None and tool not in known_tools:
            raise ValueError(
                f"step {index + 1}: {tool!r} is not available from the console"
            )
        policy = str(raw.get("policy") or "all")
        if policy not in POLICIES:
            raise ValueError(f"step {index + 1}: unknown policy {policy!r}")
        try:
            seconds = float(raw.get("seconds", 3.0))
        except (TypeError, ValueError):
            raise ValueError(f"step {index + 1}: seconds must be a number")
        # `raw.get("args") or {}` would treat `[]`, `0` and `""` as "not
        # provided" and silently substitute an empty object, so a malformed
        # document validates. Check for None explicitly.
        args = raw.get("args")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise ValueError(f"step {index + 1}: args must be an object")
        steps.append(
            Step(
                label=str(raw.get("label") or tool)[:48],
                tool=tool,
                args=dict(args),
                # Clamped, not rejected: a 0 s step makes a routine that fires
                # everything at once, and an hour-long one is a typo.
                seconds=max(0.2, min(seconds, 60.0)),
                dangerous=bool(raw.get("dangerous", False)),
                policy=policy,
            )
        )

    return Routine(
        id=routine_id,
        label=label,
        title=str(document.get("title") or "")[:200],
        steps=tuple(steps),
        loop=bool(document.get("loop", False)),
    )


def _stand() -> Step:
    return Step("站好", "robot_stand", {"confirm": True}, 2.0)


def _action(name: str, label: str, seconds: float, *, dangerous: bool = False) -> Step:
    return Step(label, "robot_play_action", {"name": name, "confirm": True}, seconds, dangerous)


#: The routines offered. Deliberately short and gentle: a routine is the one
#: control that keeps sending commands after the operator has taken their hand
#: off the keyboard, so the bar for including one is that it is obviously safe
#: to watch happen.
#:
#: **Any routine that moves starts by standing.** The firmware refuses
#: `robot_move` from a lying robot with code 3201 — "robot is not standing;
#: BalanceStand first" — which the mock reproduces faithfully. The first
#: version of 走正方形 went straight to a forward step and failed on both dogs;
#: the timeline showed it as a red card, which is the only reason it was
#: noticed before hardware. There is a test that runs every built-in routine
#: end to end against the mock and asserts no step fails.
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
        title="站好 → (前进 0.4m → 右转 90°) ×4，回到原点。两条狗各走各的正方形。",
        steps=(
            _stand(),
            *(
                step
                for _ in range(4)
                for step in (
                    # **A measured leg, not a timed one.** `robot_move` takes a
                    # duration, so how far a dog goes in 0.6 s depends on the
                    # floor, the battery and the gait — and a square walked four
                    # times compounds it. `robot_walk` stops when it has measured
                    # the distance.
                    #
                    # It falls back to a timed move, and says so, on a robot
                    # that publishes no odometry — which the Go2 used for
                    # testing does not. So this is not worse there and better
                    # everywhere else.
                    Step("前进", "robot_walk",
                         {"meters": 0.4, "confirm": True}, 1.6),
                    Step("右转 90°", "robot_turn", {"degrees": -90, "confirm": True}, 6.0),
                )
            ),
        ),
    ),
    Routine(
        id="formation",
        label="列队前进",
        title="站好 → (一起前进 0.3m → 一起停下) ×3。用于确认同驱时序。",
        steps=(
            _stand(),
            *(
                step
                for _ in range(3)
                for step in (
                    # The same distance for both dogs, rather than the same
                    # duration: two timed moves of one second do not travel the
                    # same distance on two floors, and keeping them in line is
                    # the entire point of this routine.
                    Step("一起前进", "robot_walk",
                         {"meters": 0.3, "confirm": True}, 2.0),
                    Step("停下", "robot_stop", {}, 1.0),
                )
            ),
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
