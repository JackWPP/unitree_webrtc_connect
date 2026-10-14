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
It uses declared durations rather than waiting for the robot — and pretending
otherwise would be worse than a timing table that is honest about being one.

**But the premise of that sentence is wrong for the three primitives that matter,
and that is worth more than the sentence.** It said "none of these actions
reports completion". Measured, twice and independently:

    robot_walk   → motion.forward_m()  closed loop on odometry, awaits until done
    robot_turn   → motion.turn()       closed loop on the IMU,  awaits until done
    robot_move   → _go2_move()         sleeps out its own duration

All three **block until the motion is over**, so `seconds` is stacked *on top of*
an already-finished call. Timed on the mock: `robot_walk(0.4 m)` returns after
**1.59 s** but the shipped square waits 2.0 s after it; `robot_turn(-90°)`
returns after **4.73 s** and the square waits 6.0 s. Of 走正方形's declared 34 s,
**32 s is dead standing-still time**, about 16 s of it pure padding per lap.

It is not only waste. The padding is a **hand-typed integer with no source** —
真机上是 4.73 s 还是 5.1 s 取决于地面 — so the routine is slower than it says,
and the "9步/34s" on the button is an under-promise.

The other class is genuinely fire-and-forget: `robot_play_action` returns in
**0.01 s** and the dance keeps going, so there `seconds` is the *only* timing
there is. **One field, two meanings.** Distinguishing them is the change worth
making: a `BLOCKING_TOOLS` table, `seconds=None` meaning "this step waits for
itself", and a total that says what it can actually compute.

See `docs/visual_editing_research.md` for the editor question this sits inside.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class Param:
    """One knob on a routine, declared rather than hardcoded.

    ``走正方形`` walked a fixed 0.4 m leg because the number was a literal in
    this file. The operator who wants a bigger square had no way to say so, and
    the one who wants a smaller one — indoors, with two dogs in a room — had no
    way either.

    Declared here rather than in the page for the same reason the step
    durations are: **a routine is a commitment of time and motion, and the
    operator should see both before pressing it.** The page renders these; it
    does not invent them.
    """

    key: str
    label: str
    default: float
    minimum: float
    maximum: float
    step: float = 0.1
    unit: str = ""

    def clamp(self, value: Any) -> float:
        """Coerce and bound one value, refusing rather than guessing.

        Raises ``ValueError`` naming the field, because a dataclass does not
        type-check: `Formation(distance_m="far")` was accepted without complaint
        in this project once and blew up inside a loop seconds later, with an
        error that said nothing about the form the operator had filled in.
        """
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{self.label}（{self.key}）要一个数字，收到 {value!r}") from None
        if number != number or number in (float("inf"), float("-inf")):
            raise ValueError(f"{self.label}（{self.key}）不是有限数")
        if number < self.minimum or number > self.maximum:
            raise ValueError(
                f"{self.label}（{self.key}）要在 {self.minimum:g} 到 "
                f"{self.maximum:g}{self.unit} 之间，收到 {number:g}"
            )
        return number

    def as_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "default": self.default,
            "min": self.minimum,
            "max": self.maximum,
            "step": self.step,
            "unit": self.unit,
        }


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
    #: Knobs the operator can turn. Empty for a routine that is what it is.
    params: Tuple[Param, ...] = ()
    #: ``params -> steps``. Required whenever ``params`` is non-empty.
    #:
    #: ``steps`` stays populated with the **default** build, so the catalogue,
    #: the tests and every existing caller keep working unchanged — the
    #: parameterised version is the same routine with different numbers, not a
    #: different kind of object.
    build: Optional[Callable[[Dict[str, float]], Tuple[Step, ...]]] = None

    def with_params(self, raw: Optional[Dict[str, Any]] = None) -> "Routine":
        """This routine with the operator's values applied.

        Unknown keys are refused rather than ignored: a page sending
        ``leg_m`` to a routine that takes ``side_m`` has a typo, and silently
        using the default would look like the setting had no effect — which is
        exactly how a UI bug survives a session.
        """
        if not self.params:
            if raw:
                raise ValueError(
                    f"「{self.label}」不接受参数，但收到了 {sorted(raw)}"
                )
            return self
        if self.build is None:  # pragma: no cover - a construction mistake
            raise ValueError(f"「{self.label}」声明了参数却没有构建函数")

        supplied = dict(raw or {})
        known = {param.key for param in self.params}
        unknown = sorted(set(supplied) - known)
        if unknown:
            raise ValueError(
                f"「{self.label}」不认识参数 {unknown}；可用的有 {sorted(known)}"
            )

        values: Dict[str, float] = {}
        for param in self.params:
            values[param.key] = (
                param.default if param.key not in supplied
                else param.clamp(supplied[param.key])
            )

        steps = self.build(values)
        if not steps:
            raise ValueError(f"「{self.label}」用这些参数构建出了空的序列")
        return Routine(
            id=self.id, label=self.label, title=self.title, steps=steps,
            loop=self.loop, params=self.params, build=self.build,
        )

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
            # Empty for most routines, and the page only draws the ⚙ when it is
            # not. Sending `[]` rather than omitting the key keeps the contract
            # one shape.
            "params": [param.as_dict() for param in self.params],
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


#: Seconds allowed for a walk, for the *step's* declared wait.
#:
#: `robot_walk` closes its loop and stops when it has measured the distance, so
#: this is a **ceiling** on the wait rather than a duration — except on a robot
#: that publishes no odometry, where it falls back to a timed move and then it
#: really is the duration.
#:
#: Which is why the rule is "generous", not "accurate": **a wait that is too
#: short fires the next step while the dog is still walking**, and the failure
#: that produces is a turn taken mid-stride. Too long only makes the routine
#: slower, which is visible and harmless.
#:
#: The two shipped routines were 4.0 s/m (0.4 m → 1.6 s) and 6.7 s/m (0.3 m →
#: 2.0 s) for the same primitive, which is not a rule at all — it is two numbers
#: somebody typed. Rather than reverse-engineer a formula that reproduces both,
#: this is a floor plus a rate, and the floor is the generous one.
_WALK_SECONDS_PER_M = 4.0
_WALK_SECONDS_FLOOR = 2.0


def _walk_seconds(metres: float) -> float:
    return max(_WALK_SECONDS_FLOOR, abs(metres) * _WALK_SECONDS_PER_M)


# ── the two routines with knobs ───────────────────────────────────────


#: ``走正方形``: how long a side, and how many laps.
#:
#: The numbers were literals — 0.4 m and four legs — which meant the operator
#: who wanted a bigger square had no way to say so, and the one indoors with two
#: dogs in a room had no way to ask for a smaller one.
SQUARE_PARAMS: Tuple[Param, ...] = (
    Param("leg_m", "边长", 0.4, 0.2, 2.0, step=0.1, unit=" m"),
    Param("laps", "圈数", 1, 1, 4, step=1, unit=" 圈"),
)


def _build_square(values: Dict[str, float]) -> Tuple[Step, ...]:
    leg = values["leg_m"]
    laps = int(round(values["laps"]))
    seconds = _walk_seconds(leg)
    return (
        # **Standing first is not optional.** The firmware refuses `robot_move`
        # from a lying robot with code 3201, and the first version of this
        # routine went straight to a forward step and failed on both dogs.
        _stand(),
        *(
            step
            # **Four sides per lap.** `laps` counts trips round the square, and
            # a square has four sides — the first version of this used one leg
            # per lap, which quietly turned the shipped nine-step routine into a
            # three-step one. The default has to reproduce what is on the button
            # today, not merely something plausible.
            for _ in range(laps * 4)
            for step in (
                # **A measured leg, not a timed one.** `robot_move` takes a
                # duration, so how far a dog goes in 0.6 s depends on the floor,
                # the battery and the gait — and a square walked four times
                # compounds it. `robot_walk` stops when it has measured the
                # distance.
                #
                # It falls back to a timed move, and says so, on a robot that
                # publishes no odometry — which the Go2 used for testing does
                # not. So this is not worse there and better everywhere else.
                Step("前进", "robot_walk",
                     {"meters": leg, "confirm": True}, seconds),
                Step("右转 90°", "robot_turn", {"degrees": -90, "confirm": True}, 6.0),
            )
        ),
    )


#: ``列队前进``: how far each leg is, how many legs, and how long to pause.
FORMATION_PARAMS: Tuple[Param, ...] = (
    Param("step_m", "每步距离", 0.3, 0.1, 1.0, step=0.1, unit=" m"),
    Param("legs", "步数", 3, 1, 10, step=1, unit=" 步"),
    Param("pause_s", "组间停顿", 1.0, 0.5, 3.0, step=0.5, unit=" s"),
)


def _build_formation(values: Dict[str, float]) -> Tuple[Step, ...]:
    distance = values["step_m"]
    legs = int(round(values["legs"]))
    pause = values["pause_s"]
    seconds = _walk_seconds(distance)
    return (
        _stand(),
        *(
            step
            for _ in range(legs)
            for step in (
                # The same distance for both dogs, rather than the same
                # duration: two timed moves of one second do not travel the same
                # distance on two floors, and keeping them in line is the entire
                # point of this routine.
                Step("一起前进", "robot_walk",
                     {"meters": distance, "confirm": True}, seconds),
                Step("停下", "robot_stop", {}, pause),
            )
        ),
    )


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
        title="站好 → (前进 → 右转 90°) × 圈数。两条狗各走各的正方形。",
        params=SQUARE_PARAMS,
        build=_build_square,
        steps=_build_square({p.key: p.default for p in SQUARE_PARAMS}),
    ),
    Routine(
        id="formation",
        label="列队前进",
        title="站好 → (一起前进 → 一起停下) × 步数。用于确认同驱时序。",
        params=FORMATION_PARAMS,
        build=_build_formation,
        steps=_build_formation({p.key: p.default for p in FORMATION_PARAMS}),
    ),
)


def routine_by_id(routine_id: str) -> Routine:
    for routine in ROUTINES:
        if routine.id == routine_id:
            return routine
    raise KeyError(routine_id)


def catalogue() -> List[Dict[str, Any]]:
    return [routine.as_dict() for routine in ROUTINES]
