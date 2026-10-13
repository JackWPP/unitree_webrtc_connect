"""Turning "they are 1.8 m apart and should be 1.0 m" into commands.

The arena map measures a relationship: two positions, two headings, one
coordinate system. This turns a *desired* relationship into what each dog has to
do about it, and it is the last step of the thing the operator asked for —
"知道互相的相对位置就不需要再手动调了".

## Why a loop and not a calculation

`robot_move`'s `vx`/`vy` are **normalised to the robot's own limit**, not metres
per second, and nobody has measured what a Go2's limit actually is. So a move
cannot be ordered as "travel 0.42 m": it is ordered as "travel forward at 30% for
some seconds", and how far that goes depends on a constant this code does not
know.

That would be fatal for an open-loop plan and is merely slow for a closed one.
The camera measures again after every step, so a wrong speed constant costs
iterations rather than accuracy — the loop converges on what it can see instead
of trusting what it assumed. `NOMINAL_SPEED_MPS` is therefore a starting guess
with an honest name, and `speed_mps` is a parameter so a session can measure it.

## What it refuses to do

Every correction is bounded, and the loop has a budget of steps, a tolerance,
and a requirement that the measurement is fresh. A formation loop that cannot
tell it has arrived is a formation loop that walks two robots into a wall while
chasing a number — so it stops when it stops making progress, and says so.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: What a Go2's normalised 1.0 is worth in metres per second. **A guess, and
#: named as one.**
#:
#: `robot_move`'s `vx` is a fraction of the robot's own maximum, so converting a
#: distance into a command needs to know what that maximum is, and nobody has
#: measured it. The loop re-measures after every step, so being wrong costs
#: iterations rather than accuracy — but a session that measures it once and
#: passes it in will converge in half the moves.
NOMINAL_MAX_SPEED_MPS = 3.0

#: The speed corrections are actually ordered at. Deliberately well under the
#: robot's maximum: this is a formation shuffle in a room, not a sprint, and a
#: slow move is one that can be stopped and seen.
NOMINAL_SPEED_MPS = 1.2

#: The largest single correction. Two reasons: a robot told to travel a metre in
#: one order will do it at whatever speed it likes, and a step small enough to
#: see the result of is a step that can be stopped.
MAX_STEP_M = 0.35

#: How close is close enough, in metres and degrees.
DEFAULT_TOLERANCE_M = 0.10
DEFAULT_TOLERANCE_DEG = 8.0

#: Moves before the loop gives up. Bounded because a loop chasing a target it
#: cannot reach does not fail, it oscillates.
#:
#: **What that budget is worth, measured rather than assumed:** a step is capped
#: at `MAX_STEP_M` and a normalised command travels somewhat less than that in
#: practice, so six steps close roughly **a metre**. This is a correction for two
#: dogs that are nearly in formation, not a way to march them across a room —
#: and a gap beyond the budget reports `budget` rather than quietly shuffling
#: until it runs out of tries.
DEFAULT_MAX_STEPS = 6


def wrap_degrees(value: float) -> float:
    return (value + 180.0) % 360.0 - 180.0


#: Which dog moves. "b" holds A still, "both" splits the correction.
MOVERS = ("b", "both")


def parse_formation(raw: Dict[str, Any]) -> "Formation":
    """A formation from an HTTP body, checked.

    **A dataclass does not type-check.** `Formation(distance_m="far")` is
    accepted without complaint and blows up somewhere inside the loop, several
    seconds later, with an error that says nothing about the form the operator
    filled in. Everything is coerced and range-checked here instead, so the
    refusal comes back while they are still looking at the field.
    """
    values: Dict[str, Any] = {}

    for key, low, high in (
        ("distance_m", 0.0, 50.0),
        ("bearing_deg", -360.0, 360.0),
        ("heading_spread_deg", -360.0, 360.0),
    ):
        if key not in raw or raw[key] is None:
            continue
        try:
            value = float(raw[key])
        except (TypeError, ValueError):
            raise ValueError(f"{key} 要一个数字，收到 {raw[key]!r}") from None
        if not math.isfinite(value):
            raise ValueError(f"{key} 不是有限数")
        if not low <= value <= high:
            raise ValueError(f"{key} 要在 {low} 到 {high} 之间，收到 {value}")
        values[key] = value

    mover = raw.get("mover")
    if mover is not None:
        if mover not in MOVERS:
            raise ValueError(f"mover 只能是 {MOVERS} 之一，收到 {mover!r}")
        values["mover"] = mover

    return Formation(**values)


@dataclass
class Formation:
    """What the two dogs should look like from above."""

    #: How far apart, in metres.
    distance_m: float = 1.0
    #: Where B should stand relative to A, in arena degrees (0 = +x).
    bearing_deg: float = 0.0
    #: How far apart their headings should be. 0 means parallel, 180 means
    #: facing each other.
    heading_spread_deg: float = 0.0
    #: Which dog moves. "b" holds A still, "both" splits the correction.
    mover: str = "both"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "distance_m": self.distance_m,
            "bearing_deg": self.bearing_deg,
            "heading_spread_deg": self.heading_spread_deg,
            "mover": self.mover,
        }


@dataclass
class DogMove:
    """One dog's share of a correction, in its own body frame."""

    robot_id: str
    #: Metres forward along the dog's nose.
    forward_m: float
    #: Metres to the dog's **left**. Positive is left, matching `robot_move`'s
    #: own `vy` — getting this sign wrong sends the two dogs apart instead of
    #: together, which is at least immediately visible.
    lateral_m: float
    #: Degrees to turn, positive anticlockwise.
    turn_deg: float
    #: Where it is now, for the record.
    from_x: float
    from_y: float
    from_heading_deg: float

    @property
    def distance_m(self) -> float:
        return math.hypot(self.forward_m, self.lateral_m)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "robot_id": self.robot_id,
            "forward_m": round(self.forward_m, 3),
            "lateral_m": round(self.lateral_m, 3),
            "turn_deg": round(self.turn_deg, 1),
            "distance_m": round(self.distance_m, 3),
            "from": [round(self.from_x, 3), round(self.from_y, 3),
                     round(self.from_heading_deg, 1)],
        }

    def move_args(
        self,
        *,
        speed_mps: float = NOMINAL_SPEED_MPS,
        max_speed_mps: float = NOMINAL_MAX_SPEED_MPS,
        limit: float = 0.6,
    ) -> Dict[str, Any]:
        """`robot_move` arguments for this share.

        Two conversions, and the first version of this got one of them
        backwards: it scaled the **speed** by the inverse of the duration, so a
        shorter move was ordered *faster* — a dog asked to shuffle 20 cm would
        have been told to do it at full tilt.

        The right way round:
          * **duration** = distance / the speed corrections travel at.
          * **vx/vy** = that speed as a fraction of the robot's maximum, which
            is what `robot_move` actually takes.

        `max_speed_mps` is the guess. The loop re-measures, so a wrong value
        makes the correction overshoot or undershoot by a constant factor and
        the next step absorbs it — but `limit` caps how wrong it can look, and
        the duration is capped so a ridiculous distance cannot turn into an
        order to sprint.
        """
        distance = self.distance_m
        if distance <= 1e-3:
            return {}

        travel_speed = max(0.05, speed_mps)
        duration = max(0.2, min(2.5, distance / travel_speed))
        # Fraction of the robot's own maximum, along each body axis.
        fraction = min(limit, travel_speed / max(0.1, max_speed_mps))
        # The axes carry the direction; scaling by the share of the distance
        # each one covers keeps the resultant pointing where the gap is.
        unit_forward = self.forward_m / distance
        unit_lateral = self.lateral_m / distance
        return {
            "vx": round(unit_forward * fraction, 3),
            "vy": round(unit_lateral * fraction, 3),
            "omega": 0.0,
            "duration_s": round(duration, 2),
            "confirm": True,
        }


@dataclass
class Plan:
    """What to do, and why — or why not."""

    moves: List[DogMove] = field(default_factory=list)
    converged: bool = False
    distance_m: float = 0.0
    distance_error_m: float = 0.0
    yaw_error_deg: float = 0.0
    notes: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "moves": [move.as_dict() for move in self.moves],
            "converged": self.converged,
            "distance_m": round(self.distance_m, 3),
            "distance_error_m": round(self.distance_error_m, 3),
            "yaw_error_deg": round(self.yaw_error_deg, 1),
            "notes": self.notes,
        }


def _body_frame(
    error_x: float, error_y: float, heading_deg: float
) -> Tuple[float, float]:
    """A displacement in arena metres, expressed in a dog's own frame.

    Heading is the nose direction (0 = +x, anticlockwise), so the dog's forward
    axis is `(cos h, sin h)` and its left is that turned 90° anticlockwise,
    `(-sin h, cos h)`. The projection onto those two is what `robot_move` wants.
    """
    radians = math.radians(heading_deg)
    forward = (math.cos(radians), math.sin(radians))
    left = (-math.sin(radians), math.cos(radians))
    return (
        error_x * forward[0] + error_y * forward[1],
        error_x * left[0] + error_y * left[1],
    )


def plan(
    poses: Dict[str, Dict[str, Any]],
    target: Formation,
    *,
    tolerance_m: float = DEFAULT_TOLERANCE_M,
    tolerance_deg: float = DEFAULT_TOLERANCE_DEG,
    max_step_m: float = MAX_STEP_M,
    order: Optional[Sequence[str]] = None,
) -> Plan:
    """Work out what each dog has to do to reach `target`.

    `poses` is the arena map's `poses` mapping — `{robot_id: {x, y,
    heading_deg}}` — so this takes exactly what the camera produced and there is
    no second representation to disagree with it.
    """
    names = list(order) if order else sorted(poses)
    if len(names) < 2:
        return Plan(notes=["需要两只狗都在画面里才能编队。"])

    a_id, b_id = names[0], names[1]
    a, b = poses[a_id], poses[b_id]

    # Where B should be: `distance` from A, along `bearing` from A.
    radians = math.radians(target.bearing_deg)
    want_x = a["x"] + target.distance_m * math.cos(radians)
    want_y = a["y"] + target.distance_m * math.sin(radians)

    # A's heading is the reference the spread is measured against, so the target
    # is "B faces where A faces, plus the spread". Measuring it against arena
    # north instead would make the formation rotate every time A turned.
    want_b_heading = a["heading_deg"] + target.heading_spread_deg

    error_x, error_y = want_x - b["x"], want_y - b["y"]
    distance_error = math.hypot(error_x, error_y)
    yaw_error = wrap_degrees(want_b_heading - b["heading_deg"])

    # **A component already within tolerance must not be commanded.**
    # `converged` uses these same tolerances, so a plan that accepted an 8°
    # spread and then ordered a 5° turn was contradicting itself — and spending
    # a second of the operator's time on something it had just decided was fine.
    if abs(yaw_error) <= tolerance_deg:
        yaw_error = 0.0
    if distance_error <= tolerance_m:
        error_x = error_y = 0.0
        distance_error = 0.0

    result = Plan(distance_m=math.hypot(b["x"] - a["x"], b["y"] - a["y"]),
                  distance_error_m=distance_error, yaw_error_deg=yaw_error)

    if not distance_error and not yaw_error:
        result.converged = True
        result.notes.append("已经到位。")
        return result

    shares = (
        [(b_id, 1.0)] if target.mover == "b"
        else [(a_id, -0.5), (b_id, 0.5)]
    )

    for robot_id, share in shares:
        pose = poses[robot_id]
        if robot_id == a_id:
            # A moving towards B means A moving *against* the error vector.
            this_x, this_y = error_x * share, error_y * share
        else:
            this_x, this_y = error_x * share, error_y * share

        span = math.hypot(this_x, this_y)
        if span > max_step_m:
            # Capped. A robot told to travel a metre in one order does it at
            # whatever speed it likes, and a step small enough to see the result
            # of is a step that can be stopped.
            this_x *= max_step_m / span
            this_y *= max_step_m / span

        forward, lateral = _body_frame(this_x, this_y, pose["heading_deg"])
        # The turn goes to B, whichever way the move is split: turning both dogs
        # to fix one spread is two chances to overshoot.
        # The turn goes to B, whichever way the move is split: turning both
        # dogs to fix one spread is two chances to overshoot.
        turn = max(-30.0, min(30.0, yaw_error)) if robot_id == b_id else 0.0

        result.moves.append(DogMove(
            robot_id=robot_id,
            forward_m=forward,
            lateral_m=lateral,
            turn_deg=turn,
            from_x=pose["x"], from_y=pose["y"],
            from_heading_deg=pose["heading_deg"],
        ))

    result.notes.append(
        f"间距差 {distance_error:.2f} m（每步最多 {max_step_m} m），"
        f"朝向差 {yaw_error:.1f}°"
    )
    return result


def converged(
    poses: Dict[str, Dict[str, Any]],
    target: Formation,
    *,
    tolerance_m: float = DEFAULT_TOLERANCE_M,
    tolerance_deg: float = DEFAULT_TOLERANCE_DEG,
) -> Dict[str, Any]:
    """Just the measurement against the target, for a loop or a display."""
    names = sorted(poses)
    if len(names) < 2:
        return {"ready": False, "reason": "只有一只狗在画面里"}
    a, b = poses[names[0]], poses[names[1]]

    distance = math.hypot(b["x"] - a["x"], b["y"] - a["y"])
    bearing = math.degrees(math.atan2(b["y"] - a["y"], b["x"] - a["x"]))
    spread = abs(wrap_degrees(b["heading_deg"] - a["heading_deg"]))

    distance_error = distance - target.distance_m
    # The spread is measured against A's heading, not against arena north:
    # "parallel to each other" must not become "both facing east".
    yaw_error = wrap_degrees(
        (a["heading_deg"] + target.heading_spread_deg) - b["heading_deg"]
    )
    return {
        "ready": abs(distance_error) <= tolerance_m and abs(yaw_error) <= tolerance_deg,
        "a": names[0],
        "b": names[1],
        "distance_m": round(distance, 3),
        "bearing_deg": round(bearing, 1),
        "heading_spread_deg": round(spread, 1),
        "distance_error_m": round(distance_error, 3),
        "yaw_error_deg": round(yaw_error, 1),
    }
