"""How far apart two dogs have got, measured without seeing them.

The problem this exists for is real and shows up the first time two dogs walk
a square together. `robot_turn(-90)` is **closed loop**: each robot turns until
its own gyro says it has turned ninety degrees. Two dogs on the same floor will
not agree — different friction, different IMU calibration, different payload —
so they finish at different moments and stop at different headings. Over four
turns of 走正方形 the error accumulates and the pair ends up visibly out of
formation. Nothing in the console noticed.

## What can honestly be measured

Each robot's odometry has **its own origin**: `position` is relative to wherever
that dog was when its estimator started, and `yaw` is relative to whatever
heading it happened to be facing. Comparing two dogs' raw poses is therefore
meaningless, and a console that drew them on one set of axes would be inventing
a relationship that does not exist.

What *is* meaningful is **how much each has changed since a shared reference
point**. If the operator lines the dogs up facing the same way and presses
对齐, then afterwards "dog 1 has turned +88° and dog 2 has turned +92°" is a
true statement, and the 4° between them is a real divergence — it is exactly
the thing that makes the formation drift.

So a *reference* is recorded and every number here is a delta from it. The
assumption is stated rather than hidden: **the dogs are aligned when the
reference is taken.** If they are not, these numbers are still honest — they
measure how differently the two have moved — but they will not match what a
camera would see.

## What it cannot do

It cannot tell you where the dogs are, only how differently they have moved.
Correcting that needs a shared frame, and there is no confirmed topic that
provides one. Where a corrective action is offered it is therefore deliberately
small and bounded: nudge the outliers, once, and report — never a loop that
chases a target it cannot verify it reached.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

#: Below this the pair is doing the same thing to within the noise of two
#: independent estimators.
ALIGNED_YAW_DEG = 5.0
ALIGNED_DISTANCE_M = 0.15

#: Above this the pair is no longer in formation, whatever the numbers say.
DIVERGED_YAW_DEG = 20.0
DIVERGED_DISTANCE_M = 0.60


def _wrap_degrees(value: float) -> float:
    """Shortest way round. 350° and -10° are 0° apart, not 360°."""
    return (value + 180.0) % 360.0 - 180.0


@dataclass
class Reading:
    """One robot's odometry, as a delta from the reference."""

    x: float = 0.0
    y: float = 0.0
    yaw: float = 0.0
    #: Distance travelled since the reference. Always non-negative, and
    #: comparable between dogs even when their origins are not.
    travelled: float = 0.0
    #: Change in heading since the reference, in degrees.
    turned: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "x": round(self.x, 3),
            "y": round(self.y, 3),
            "yaw": round(self.yaw, 4),
            "travelled": round(self.travelled, 3),
            "turned_deg": round(self.turned, 1),
        }


@dataclass
class Pair:
    """The divergence between two robots."""

    a: str
    b: str
    yaw_spread_deg: float
    distance_spread_m: float
    state: str
    #: Which of the two is ahead, and by how much. Named so a corrective action
    #: has something to act on rather than a bare number.
    leader: str = ""
    laggard: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "a": self.a,
            "b": self.b,
            "yaw_spread_deg": round(self.yaw_spread_deg, 1),
            "distance_spread_m": round(self.distance_spread_m, 3),
            "state": self.state,
            "leader": self.leader,
            "laggard": self.laggard,
            "detail": self.detail,
        }


class SyncMonitor:
    """Tracks each robot's odometry delta from a shared reference point."""

    def __init__(self) -> None:
        #: robot_id -> the odometry triple recorded when the reference was taken.
        self._reference: Dict[str, Tuple[float, float, float]] = {}
        #: Robot ids that were present when the reference was taken. A dog that
        #: joins later has no reference and is reported as such rather than
        #: silently compared against a point it was not there for.
        self._members: List[str] = []

    # ── the reference ────────────────────────────────────────────────

    @property
    def marked(self) -> bool:
        return bool(self._reference)

    @property
    def members(self) -> List[str]:
        return list(self._members)

    def mark(self, poses: Dict[str, Tuple[float, float, float]]) -> Dict[str, Any]:
        """Take the reference. `poses` is robot_id -> (x, y, yaw).

        Robots missing a pose are not recorded, and are named in the return so
        the operator knows the reference does not cover them.
        """
        self._reference = {
            robot_id: (float(x), float(y), float(yaw))
            for robot_id, (x, y, yaw) in poses.items()
        }
        self._members = list(self._reference)
        return {"marked": self._members, "missing": [r for r in poses if r not in self._reference]}

    def clear(self) -> None:
        self._reference = {}
        self._members = []

    # ── reading ──────────────────────────────────────────────────────

    def reading(self, robot_id: str, pose: Optional[Tuple[float, float, float]]) -> Optional[Reading]:
        """One robot's delta from the reference, or None if it has no pose."""
        if pose is None:
            return None
        origin = self._reference.get(robot_id)
        if origin is None:
            return None

        x, y, yaw = (float(pose[0]), float(pose[1]), float(pose[2]))
        dx, dy = x - origin[0], y - origin[1]
        return Reading(
            x=dx,
            y=dy,
            yaw=yaw - origin[2],
            travelled=math.hypot(dx, dy),
            turned=_wrap_degrees(math.degrees(yaw - origin[2])),
        )

    def pairs(self, poses: Dict[str, Optional[Tuple[float, float, float]]]) -> List[Pair]:
        """Every pair of referenced robots that currently has a pose."""
        readings: Dict[str, Reading] = {}
        for robot_id in self._members:
            reading = self.reading(robot_id, poses.get(robot_id))
            if reading is not None:
                readings[robot_id] = reading

        out: List[Pair] = []
        ids = sorted(readings)
        for index, a in enumerate(ids):
            for b in ids[index + 1:]:
                out.append(self._pair(a, readings[a], b, readings[b]))
        return out

    @staticmethod
    def _pair(a: str, reading_a: Reading, b: str, reading_b: Reading) -> Pair:
        yaw_spread = abs(_wrap_degrees(reading_a.turned - reading_b.turned))
        distance_spread = abs(reading_a.travelled - reading_b.travelled)

        if yaw_spread >= DIVERGED_YAW_DEG or distance_spread >= DIVERGED_DISTANCE_M:
            state = "diverged"
        elif yaw_spread >= ALIGNED_YAW_DEG or distance_spread >= ALIGNED_DISTANCE_M:
            state = "drifting"
        else:
            state = "aligned"

        # Who is ahead. For a turn that is whoever has turned further; for a
        # walk it is whoever has gone further. Whichever spread is the larger
        # problem decides, so the answer matches the reason for the flag.
        if yaw_spread >= distance_spread:
            leader, laggard = (
                (a, b) if abs(reading_a.turned) >= abs(reading_b.turned) else (b, a)
            )
        else:
            leader, laggard = (
                (a, b) if reading_a.travelled >= reading_b.travelled else (b, a)
            )

        return Pair(
            a=a, b=b,
            yaw_spread_deg=yaw_spread,
            distance_spread_m=distance_spread,
            state=state,
            leader=leader,
            laggard=laggard,
            detail={"a": reading_a.as_dict(), "b": reading_b.as_dict()},
        )

    def summary(self, poses: Dict[str, Optional[Tuple[float, float, float]]]) -> Dict[str, Any]:
        """The whole picture, for one status push."""
        if not self.marked:
            return {"marked": False, "members": [], "pairs": [], "state": "unmarked"}

        pairs = self.pairs(poses)
        if not pairs:
            # Marked, but nobody is reporting a pose. That is not "aligned" —
            # it is "cannot tell", and saying otherwise would be a guess with a
            # confident colour on it.
            return {
                "marked": True, "members": self.members, "pairs": [],
                "state": "unknown",
            }

        worst = max(pairs, key=lambda pair: (
            pair.state == "diverged",
            pair.yaw_spread_deg / DIVERGED_YAW_DEG,
            pair.distance_spread_m / DIVERGED_DISTANCE_M,
        ))
        return {
            "marked": True,
            "members": self.members,
            "pairs": [pair.as_dict() for pair in pairs],
            "state": worst.state,
            "worst": worst.as_dict(),
        }

    # ── correction ───────────────────────────────────────────────────

    @staticmethod
    def correction(pair: Pair, *, max_degrees: float = 12.0) -> Optional[Dict[str, Any]]:
        """A bounded nudge for the laggard of a turn, or None if none is due.

        Deliberately one shot and capped. Chasing a target this cannot verify
        it reached is how a correction becomes an oscillation, and two robots
        oscillating at each other is worse than two robots slightly out of
        step. The caller is expected to apply at most one of these per step.
        """
        if pair.state == "aligned":
            return None
        gap = pair.yaw_spread_deg
        if gap < ALIGNED_YAW_DEG:
            return None
        detail = pair.detail
        a_turned = detail.get("a", {}).get("turned_deg", 0.0)
        b_turned = detail.get("b", {}).get("turned_deg", 0.0)
        # Turn the laggard the way the leader went, by the gap, capped.
        sign = 1.0 if (a_turned if pair.leader == pair.a else b_turned) >= 0 else -1.0
        return {
            "robot_id": pair.laggard,
            "degrees": round(sign * min(gap, max_degrees), 1),
            "reason": f"{pair.laggard} 比 {pair.leader} 少转 {gap:.1f}°",
        }
