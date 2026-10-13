"""Console-level operator state, and the rules that read it.

Everything here is pure: no I/O, no asyncio, no knowledge of the fleet. That
makes the two decisions that actually matter — *may this robot move?* and
*which robot is the operator currently driving?* — testable without a robot,
a network, or an event loop.

Two rules are worth stating outright because both are borrowed from failures
observed elsewhere:

**E-stop is a latch, not a command.** Sending Damp once is not an emergency
stop; the operator needs the console to *keep refusing motion* until they
deliberately release it. A stop that a subsequent joystick frame can undo is
not a stop.

**Changing the driven robot must zero the previous one.** This is the exact
bug that the browser UI in `unitree_ui` guards against by zeroing shared
state when the input source changes (`app.ts:1173-1177`). Without it, the dog
you just switched away from keeps travelling on its last non-zero velocity,
because the robot holds the last joystick frame it received until told
otherwise.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Iterable, List, Optional, Set

from unitree_robot_agent.errors import AgentError


class MotionBlocked(AgentError):
    """Motion was refused by console policy (an engaged e-stop)."""

    code = "motion_blocked"
    hint = (
        "The emergency stop is engaged and latched. Release it explicitly "
        "before sending movement again."
    )


class InputMode(str, Enum):
    """How one operator's input maps onto two robots."""

    #: Input drives whichever robot is focused.
    FOCUS = "focus"
    #: Input drives every connected robot identically.
    BOTH = "both"
    #: Key groups map to robots: WASD drives one, IJKL the other.
    SPLIT = "split"
    #: A named source (gamepad, keyboard) is bound to a specific robot.
    PER_SOURCE = "per_source"


class EStopScope(str, Enum):
    ALL = "all"
    ROBOT = "robot"


@dataclass
class EStop:
    """A latched emergency stop."""

    engaged: bool = False
    scope: EStopScope = EStopScope.ALL
    #: Empty for a fleet-wide latch.
    robot_id: str = ""
    reason: str = ""
    engaged_at: float = 0.0

    def covers(self, robot_id: str) -> bool:
        if not self.engaged:
            return False
        if self.scope is EStopScope.ALL:
            return True
        return self.robot_id == robot_id

    def engage(self, *, scope: EStopScope, robot_id: str, reason: str) -> None:
        # A fleet-wide latch must not be silently narrowed by a later
        # per-robot press, or releasing one dog would release everything.
        if self.engaged and self.scope is EStopScope.ALL:
            return
        self.engaged = True
        self.scope = scope
        self.robot_id = robot_id
        self.reason = reason
        self.engaged_at = time.time()

    def release(self, *, scope: EStopScope, robot_id: str) -> bool:
        """Release a latch. Returns True if anything actually changed."""
        if not self.engaged:
            return False
        if scope is EStopScope.ROBOT and self.scope is EStopScope.ALL:
            # Fine-grained release cannot clear a fleet latch; that would be
            # a one-click way to un-safety the whole fleet.
            return False
        if scope is EStopScope.ROBOT and self.robot_id != robot_id:
            return False
        self.engaged = False
        self.scope = EStopScope.ALL
        self.robot_id = ""
        self.reason = ""
        self.engaged_at = 0.0
        return True

    def as_dict(self) -> Dict[str, object]:
        return {
            "engaged": self.engaged,
            "scope": self.scope.value,
            "robot_id": self.robot_id,
            "reason": self.reason,
            "engaged_at": self.engaged_at or None,
        }


@dataclass
class ConsoleState:
    """Mutable operator state for one console process."""

    estop: EStop = field(default_factory=EStop)
    input_mode: InputMode = InputMode.FOCUS
    focused: str = ""
    #: Which robots the operator's sticks are currently commanding. A *set*,
    #: because ``BOTH`` mode drives two at once; tracking only one would leave
    #: the other travelling on its last frame after a mode change. Tracked so
    #: a switched-away robot can be zeroed, and so the dead-man timeout knows
    #: what to stop.
    driving: Set[str] = field(default_factory=set)
    driving_since: float = 0.0
    last_teleop_at: float = 0.0
    #: Per-robot video switches, so two streams do not both have to be live.
    video_enabled: Dict[str, bool] = field(default_factory=dict)
    #: source name -> robot_id, used by ``PER_SOURCE``.
    source_bindings: Dict[str, str] = field(default_factory=dict)

    # ── safety ───────────────────────────────────────────────────────

    def motion_blocked_reason(self, robot_id: str) -> Optional[str]:
        """Why *robot_id* may not move right now, or ``None``."""
        if self.estop.covers(robot_id):
            scope = "the whole fleet" if self.estop.scope is EStopScope.ALL else robot_id
            detail = f": {self.estop.reason}" if self.estop.reason else ""
            return f"emergency stop is engaged for {scope}{detail}"
        return None

    def require_motion_allowed(self, robot_id: str) -> None:
        reason = self.motion_blocked_reason(robot_id)
        if reason is not None:
            raise MotionBlocked(f"Refusing to move {robot_id}: {reason}.")

    # ── drive ownership ──────────────────────────────────────────────

    def begin_driving(
        self, robot_ids: Iterable[str], *, now: Optional[float] = None
    ) -> Set[str]:
        """Claim the sticks for *robot_ids*; return the robots to zero first.

        The returned set is every robot that was being driven and is not any
        more. It still holds a non-zero velocity, so the caller must zero it.
        """
        now = time.monotonic() if now is None else now
        wanted = {r for r in robot_ids if r}
        stale = self.driving - wanted
        if wanted != self.driving:
            self.driving = wanted
            self.driving_since = now
        self.last_teleop_at = now
        return stale

    def stop_driving(self) -> Set[str]:
        """Give up the sticks; return everyone who needs zeroing."""
        previous = set(self.driving)
        self.driving = set()
        self.driving_since = 0.0
        self.last_teleop_at = 0.0
        return previous

    def deadman_expired(self, *, now: float, timeout_s: float) -> Set[str]:
        """Who must be zeroed because the operator stopped sending frames."""
        if not self.driving or not self.last_teleop_at:
            return set()
        if now - self.last_teleop_at < timeout_s:
            return set()
        return set(self.driving)

    # ── input arbitration ────────────────────────────────────────────

    def targets_for(
        self, *, source: str = "", robot_ids: Iterable[str] = ()
    ) -> List[str]:
        """Which robots an input frame from *source* should drive.

        Mirrors the four modes the console offers. ``SPLIT`` and
        ``PER_SOURCE`` need a binding; an unbound source drives nothing,
        which is the safe default (better a dead key than an unintended dog).
        """
        ids = list(robot_ids)
        if not ids:
            return []

        if self.input_mode is InputMode.BOTH:
            return ids
        if self.input_mode is InputMode.SPLIT:
            bound = self.source_bindings.get(source)
            return [bound] if bound in ids else []
        if self.input_mode is InputMode.PER_SOURCE:
            bound = self.source_bindings.get(source)
            return [bound] if bound in ids else []
        # FOCUS
        if self.focused in ids:
            return [self.focused]
        return []

    # ── view ─────────────────────────────────────────────────────────

    def as_dict(self, *, now: Optional[float] = None) -> Dict[str, object]:
        now = time.monotonic() if now is None else now
        return {
            "estop": self.estop.as_dict(),
            "input_mode": self.input_mode.value,
            "focused": self.focused,
            "driving": sorted(self.driving),
            "driving_for_s": round(now - self.driving_since, 2) if self.driving else 0.0,
            "last_teleop_age_s": (
                round(now - self.last_teleop_at, 2) if self.last_teleop_at else None
            ),
            "video_enabled": dict(self.video_enabled),
            "source_bindings": dict(self.source_bindings),
        }
