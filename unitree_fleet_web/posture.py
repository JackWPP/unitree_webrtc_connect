"""What the console believes the robot's posture is, and why it can know.

The firmware refuses to walk a robot that is not standing:

    code 3201 — "robot is not standing; BalanceStand first"

Every operator hits this the first time they press W at a robot that is lying
down, and nothing happens. `unitree_harness` solves it with an `auto_stand`
gate in its mover — but it has a DDS state subscription to tell it the posture,
and this console does not: the Go2's state topic is silent on at least one real
firmware.

It does not need one. **The console knows what it told the robot to do.** Every
posture change goes through this console, so a belief updated from its own
commands is more reliable here than a topic read — it is available on the first
tick after connecting, it costs nothing, and it cannot go stale in a way that
matters, because the only thing that changes posture is a command.

The honest limit, stated rather than hidden: if the official app changes the
posture, or the robot falls over, this belief is wrong. So it is used for one
thing only — **deciding whether to send a stand before a drive** — which is a
no-op on a robot that is already standing. It never blocks an action and it is
never shown as a measurement.

Where it *is* right, and where the alternative is worse: a page that has just
connected, a robot the operator just told to lie down, a robot that was damped
by the emergency stop a second ago. In all three the belief is correct and the
firmware would have refused the drive.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

#: Postures the console tracks by name. Deliberately the same words the tool
#: names use, so `note()` reads as the command it came from.
STANDING = "standing"
MOVING = "moving"
LYING = "lying"
SITTING = "sitting"
DAMPING = "damping"
UNKNOWN = "unknown"

#: Postures from which the firmware will accept a velocity command.
WALKABLE = frozenset({STANDING, MOVING})

#: `DAMPING` is tracked but never reached through the console: the only tool
#: that damps is `robot_emergency_stop`, which is deliberately not on the
#: console allowlist — the console has its own latching e-stop instead, and the
#: latch refuses a drive before the standing gate ever sees it. It is kept as a
#: name so a belief fed from elsewhere has somewhere to land.

#: Which tool leaves the robot in which posture. Keyed by the tool the console
#: actually calls, not by the wire command, because that is the layer that
#: knows.
_TOOL_POSTURE = {
    "robot_stand": STANDING,
    "go2_recover": STANDING,
    "robot_lie_down": LYING,
    "robot_emergency_stop": DAMPING,
    "robot_stop": None,          # stops motion, does not change posture
}

#: Actions that change posture without being a posture tool: `sit` and
#: `rise_sit` arrive through `robot_play_action` by name.
_ACTION_POSTURE = {
    "sit": SITTING,
    "rise_sit": STANDING,
    "stand_up": STANDING,
    "recovery_stand": STANDING,
}


class PostureBelief:
    """Per-robot posture, tracked from the commands this console sent."""

    def __init__(self, robot_id: str = "") -> None:
        self.robot_id = robot_id
        #: `unknown` rather than `lying`: a robot the console has just met
        #: might be doing anything, and guessing "lying" would make every
        #: first drive send a stand the operator did not ask for.
        self.posture = UNKNOWN
        #: How we came to believe it, for the tooltip. "asked" means the
        #: console sent the command; nothing here claims to have observed it.
        self.source = ""

    # ── learning ─────────────────────────────────────────────────────

    def note(self, tool: str, args: Optional[Dict[str, Any]] = None) -> None:
        """Update from a tool the console just ran successfully."""
        args = args or {}
        posture = _TOOL_POSTURE.get(tool)
        if tool == "robot_move":
            # A bounded move only leaves it standing if it was standing; if the
            # belief says otherwise the move is about to be refused, and the
            # standing gate will deal with it.
            if self.walkable:
                self._set(MOVING, "asked")
            return
        if tool == "robot_play_action":
            name = str(args.get("name") or "").strip().lower()
            posture = _ACTION_POSTURE.get(name)
            if posture is None:
                return
        if posture is None:
            return
        self._set(posture, "asked")

    def _set(self, posture: str, source: str) -> None:
        self.posture = posture
        self.source = source

    def forget(self) -> None:
        """A fresh link: whatever it was doing, we no longer know."""
        self._set(UNKNOWN, "")

    # ── reading ──────────────────────────────────────────────────────

    @property
    def walkable(self) -> bool:
        return self.posture in WALKABLE

    @property
    def known(self) -> bool:
        return self.posture != UNKNOWN

    def as_dict(self) -> Dict[str, Any]:
        return {
            "posture": self.posture,
            "walkable": self.walkable,
            "known": self.known,
            "source": self.source,
        }
