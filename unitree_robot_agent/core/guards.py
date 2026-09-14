"""Hard-coded safety rules.

These are deliberately *not* configurable from the model side. The model
can supply ``confirm=true``; it cannot turn the rule off. Everything here
is deterministic and unit-testable without a robot.

Rules implemented:

1. **Confirmation** — movement, posture changes and any state-machine
   action require ``confirm=true`` on the call itself. Missing → refuse.
2. **Dangerous actions** — the state machine actions that can make the
   robot fall (dances, getting up, lying down, zero-torque) are gated
   behind both confirmation and ``allow_dangerous``.
3. **Duration clamp** — every movement has a hard ceiling. A model asking
   for 600 s gets ``max_move_duration_s`` and a note, never a robot that
   walks away for ten minutes.
4. **Local transition pre-flight** — see
   :meth:`RobotProfile.validate_transition`.
5. **Rate limiting** — a minimum interval between commands, so a model in
   a loop cannot flood the single data channel.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from ..errors import ConfirmRequired, GuardRejected
from ..robots.base import RobotProfile, TransitionCheck


class Guard:
    """Single place where the hard rules live."""

    def __init__(self, config, audit, profile: RobotProfile) -> None:
        self.config = config
        self.audit = audit
        self.profile = profile
        self._rate_lock = asyncio.Lock()
        self._last_command_at = 0.0

    # ── 0. control disabled entirely ─────────────────────────────────

    def require_control_enabled(self, action: str) -> None:
        """Read-only mode: only reads and the soft stop are allowed."""
        if self.config.safe_mode:
            raise GuardRejected(
                f"'{action}' is blocked: the server is running in read-only "
                f"safe mode (UNITREE_AGENT_SAFE_MODE=true). Reads and the "
                f"soft stop still work.",
                detail={"action": action, "safe_mode": True},
            )
        if not self.config.allow_dangerous:
            raise GuardRejected(
                f"'{action}' is blocked: dangerous control is disabled "
                f"(UNITREE_AGENT_ALLOW_DANGEROUS=false).",
                detail={"action": action, "allow_dangerous": False},
            )

    def require_motion_enabled(self, action: str) -> None:
        """Movement/posture: blocked in safe mode only (not by allow_dangerous)."""
        if self.config.safe_mode:
            raise GuardRejected(
                f"'{action}' is blocked: the server is running in read-only "
                f"safe mode (UNITREE_AGENT_SAFE_MODE=true).",
                detail={"action": action, "safe_mode": True},
            )

    # ── 1. confirmation ──────────────────────────────────────────────

    def require_confirm(self, confirm: bool, action: str, *, detail: Any = None) -> None:
        if not confirm:
            raise ConfirmRequired(
                f"'{action}' moves the robot and needs explicit confirmation. "
                f"Re-issue the call with confirm=true once the area is clear.",
                detail=detail,
            )

    # ── 2. dangerous actions ─────────────────────────────────────────

    def require_dangerous_allowed(self, action: str, *, dangerous: bool) -> None:
        if dangerous and not self.config.allow_dangerous:
            raise GuardRejected(
                f"'{action}' is a dangerous action and dangerous control is "
                f"disabled (UNITREE_AGENT_ALLOW_DANGEROUS=false).",
                detail={"action": action},
            )

    # ── 3. duration clamp ────────────────────────────────────────────

    def clamp_duration(self, requested: Optional[float], *, default: float = 1.0) -> tuple:
        """Return ``(duration, note)`` with the hard ceiling applied."""
        if requested is None:
            duration = min(default, self.config.max_move_duration_s)
            return duration, ""
        try:
            duration = float(requested)
        except (TypeError, ValueError):
            raise GuardRejected(
                f"duration_s must be a number, got {requested!r}.",
                detail={"requested": requested},
            )
        if duration <= 0:
            raise GuardRejected(
                "duration_s must be > 0. Use robot_stop to stop instead.",
                detail={"requested": requested},
            )
        if duration > self.config.max_move_duration_s:
            return (
                self.config.max_move_duration_s,
                f"duration_s was clamped from {duration:g}s to the "
                f"{self.config.max_move_duration_s:g}s hard limit.",
            )
        return duration, ""

    def clamp_speed(self, value: Optional[float], *, axis: str, limit: float) -> tuple:
        if value is None:
            return 0.0, ""
        try:
            speed = float(value)
        except (TypeError, ValueError):
            raise GuardRejected(
                f"{axis} must be a number, got {value!r}.", detail={"axis": axis}
            )
        if abs(speed) > limit:
            return (
                max(-limit, min(limit, speed)),
                f"{axis} was clamped from {speed:g} to {limit:g}.",
            )
        return speed, ""

    # ── 4. local transition pre-flight ───────────────────────────────

    def check_transition(
        self, current_id: Optional[int], target_id: int, *, action: str
    ) -> TransitionCheck:
        check = self.profile.validate_transition(current_id, target_id)
        if not check.allowed:
            self.audit.record(
                action,
                phase="guard",
                ok=False,
                error="transition_refused_locally",
                detail={"current": current_id, "target": target_id, "reason": check.reason},
                robot=self.profile.name,
            )
        return check

    # ── 5. rate limiting ─────────────────────────────────────────────

    async def throttle(self) -> float:
        """Sleep so consecutive commands respect ``min_command_interval_s``."""
        interval = self.config.min_command_interval_s
        if interval <= 0:
            return 0.0
        async with self._rate_lock:
            now = time.monotonic()
            wait = self._last_command_at + interval - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._last_command_at = now
            return max(0.0, wait)

    # ── convenience ──────────────────────────────────────────────────

    def note(self, message: str) -> list:
        return [message] if message else []
