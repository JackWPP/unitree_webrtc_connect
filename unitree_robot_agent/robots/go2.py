"""Unitree Go2 (Air / Pro / Edu) profile.

Unlike the humanoids, the Go2 is not driven by an FSM state machine: each
behaviour is its own ``api_id`` on ``rt/api/sport/request`` (``SPORT_CMD``),
and there is no state to read back. That has two consequences:

* ``states()`` is empty, so the base class's local white-list would refuse
  everything — the transition check is overridden to always allow and let
  the robot answer.
* ``Move`` (1008) has **no duration**: the robot keeps walking until it is
  told to stop. The locomotion tool therefore schedules its own
  ``StopMove`` (1003) and the guard's duration clamp is what bounds it.

``LeadFollow`` and ``FreeWalk`` are deliberately absent from the action
catalog: upstream ``SPORT_CMD`` maps both to 1045, so the name cannot be
resolved unambiguously.
"""

from __future__ import annotations

from typing import Mapping, Optional

from unitree_webrtc_connect.constants import RTC_TOPIC, SPORT_CMD

from .base import (
    CAP_ERRORS,
    CAP_JOYSTICK,
    CAP_LIDAR,
    CAP_LOWSTATE,
    CAP_MOTION_SWITCHER,
    CAP_OBSTACLE_AVOID,
    CAP_SNAPSHOT,
    CAP_SPORT_COMMANDS,
    CAP_STATUS,
    CAP_VUI,
    ActionInfo,
    RobotProfile,
    StateInfo,
    StopPlan,
    TransitionCheck,
)

#: Actions that need a payload, and why they are believed to.
#:
#: The library's own examples split cleanly: `Scrape`, `FrontJump`, `FrontPounce`
#: and `Heart` are sent bare, while `Pose`, `FrontFlip`, `LeftFlip` and
#: `BackFlip` are all sent `{"data": True}` —
#: ``examples/go2/data_channel/sportmode_mcf/sportmode_mcf.py:193``,
#: ``:229``, and the commented-out `BackFlip` in the non-MCF example at
#: ``examples/go2/data_channel/sportmode/sportmode.py:125``.
#:
#: **The honest caveat**: that example drives the MCF api_id space, where
#: `BackFlip` is 2043 rather than 1044, so this is strong evidence and not
#: proof for the ids sent here. It is worth acting on anyway because the
#: failure mode is silent — a flip that does nothing looks exactly like a flip
#: that is not supported — and because sending a parameter to a command that
#: ignores it is not dangerous.
#:
#: To settle it on hardware: press 前空翻 with this in place, and if the dog
#: flips, it was needed; if it refuses with `code != 0`, drop the entry and the
#: command goes back to being bare.
_ACTION_PARAMETERS = {
    "pose": {"data": True},
    "front_flip": {"data": True},
    "left_flip": {"data": True},
    "back_flip": {"data": True},
}

#: Curated sport actions. ``(name, SPORT_CMD key, dangerous, description)``
_ACTIONS = [
    ("hello", "Hello", False, "Wave hello (1016)."),
    ("stretch", "Stretch", False, "Stretch (1017)."),
    ("content", "Content", False, "Show a content/idle pose (1020)."),
    ("pose", "Pose", False, "Strike a pose (1028)."),
    ("scrape", "Scrape", False, "Scrape / bow gesture (1029)."),
    ("wiggle_hips", "WiggleHips", False, "Wiggle the hips (1033)."),
    ("finger_heart", "FingerHeart", False, "Finger-heart gesture (1036)."),
    ("sit", "Sit", False, "Sit down (1009)."),
    ("rise_sit", "RiseSit", False, "Stand up from sitting (1010)."),
    ("dance_1", "Dance1", True, "Dance 1 (1022)."),
    ("dance_2", "Dance2", True, "Dance 2 (1023)."),
    ("handstand", "Handstand", True, "Handstand (1301)."),
    ("cross_step", "CrossStep", True, "Cross step (1302)."),
    ("onesided_step", "OnesidedStep", True, "One-sided step (1303)."),
    ("bound", "Bound", True, "Bound / gallop (1304)."),
    ("moon_walk", "MoonWalk", True, "Moon walk (1305)."),
    ("front_jump", "FrontJump", True, "Jump forward (1031)."),
    ("front_pounce", "FrontPounce", True, "Pounce forward (1032)."),
    ("front_flip", "FrontFlip", True, "Front flip (1030). Requires space and a soft floor."),
    ("back_flip", "BackFlip", True, "Back flip (1044). Requires space and a soft floor."),
    ("left_flip", "LeftFlip", True, "Left flip (1042)."),
    ("right_flip", "RightFlip", True, "Right flip (1043)."),
]

_ALIASES = {
    "hello": ("wave", "hi"),
    "stretch": ("stretching",),
    "wiggle_hips": ("wiggle", "hips"),
    "finger_heart": ("heart", "fingerheart"),
    "rise_sit": ("risesit", "stand_from_sit"),
    "dance_1": ("dance1",),
    "dance_2": ("dance2",),
    "moon_walk": ("moonwalk",),
    "front_flip": ("frontflip", "flip"),
    "back_flip": ("backflip",),
    "left_flip": ("leftflip",),
    "right_flip": ("rightflip",),
    "onesided_step": ("onesidedstep",),
    "cross_step": ("crossstep",),
    "front_jump": ("frontjump", "jump"),
    "front_pounce": ("frontpounce", "pounce"),
}

#: Sport commands with a dedicated tool (stand / stop) — not actions.
_DEDICATED = {"Damp", "StopMove", "BalanceStand", "StandUp", "StandDown", "RecoveryStand"}

#: Motion modes accepted by the motion switcher.
MOTION_MODES = ("normal", "ai", "advanced", "mcf")


class Go2Profile(RobotProfile):
    """Semantic model of a Go2."""

    name = "Go2"
    device_type = "Go2"
    state_topic = RTC_TOPIC["SPORT_MOD_STATE"]
    #: The low-frequency topic first. A real Go2 (`B42N6000Q3UC1M8C`, firmware
    #: requiring `data2=3`) answered neither for eight seconds — but only
    #: `rt/sportmodestate` was ever tried before this, and the sibling project
    #: `unitree_harness` prefers `rt/lf/sportmodestate` and falls back the
    #: other way. Trying both costs one extra read slot on a silent topic and
    #: is the difference between a gait readout and none.
    state_topics = (RTC_TOPIC["LF_SPORT_MOD_STATE"], RTC_TOPIC["SPORT_MOD_STATE"])
    report_topics = (
        RTC_TOPIC["LF_SPORT_MOD_STATE"],
        RTC_TOPIC["SPORT_MOD_STATE"],
        RTC_TOPIC["MULTIPLE_STATE"],
    )
    telemetry_topics = (RTC_TOPIC["LOW_STATE"],)

    def capabilities(self) -> frozenset:
        return frozenset(
            {
                CAP_STATUS,
                CAP_LOWSTATE,
                CAP_ERRORS,
                CAP_SNAPSHOT,
                CAP_SPORT_COMMANDS,
                CAP_MOTION_SWITCHER,
                CAP_VUI,
                CAP_OBSTACLE_AVOID,
                CAP_JOYSTICK,
                CAP_LIDAR,
            }
        )

    # ── no FSM ───────────────────────────────────────────────────────

    def states(self) -> Mapping[int, StateInfo]:
        return {}

    def validate_transition(self, current_id: Optional[int], target_id: int) -> TransitionCheck:
        """The Go2 has no state machine — always let the robot decide."""
        return TransitionCheck(allowed=True)

    def reported_state(self, topic: str, payload: dict) -> Optional[int]:
        return None

    def classify(self, fsm_id: Optional[int]) -> dict:
        return {
            "fsm_id": None,
            "state": "n/a",
            "semantic": "n/a",
            "reporting": "n/a",
            "dangerous": False,
            "note": (
                "Go2 has no FSM state machine. Use go2_get_state (or "
                "robot_lowstate) for the reported mode, gait and posture."
            ),
        }

    # ── actions ──────────────────────────────────────────────────────

    def actions(self) -> Mapping[str, ActionInfo]:
        out = {}
        for name, key, dangerous, description in _ACTIONS:
            if key not in SPORT_CMD or key in _DEDICATED:
                continue
            out[name] = ActionInfo(
                name=name,
                kind="sport",
                value=SPORT_CMD[key],
                dangerous=dangerous,
                requires_lock=False,
                description=description,
                aliases=_ALIASES.get(name, ()),
                parameter=_ACTION_PARAMETERS.get(name),
                topic_key=RTC_TOPIC["SPORT_MOD"],
            )
        return out

    def list_actions(self, *, dangerous=None):
        rows = super().list_actions(dangerous=dangerous)
        for row in rows:
            row["kind"] = self.actions()[row["name"]].kind
        return rows

    # ── stop plans ───────────────────────────────────────────────────

    def soft_stop(self) -> StopPlan:
        return StopPlan(
            label="Go2 StopMove (1003) + zero joystick",
            topic_key=RTC_TOPIC["SPORT_MOD"],
            api_id=SPORT_CMD["StopMove"],
            parameter=None,
            zero_joystick=True,
            drops_robot=False,
        )

    def emergency_stop(self) -> StopPlan:
        return StopPlan(
            label="Go2 Damp (1001) — may drop the robot",
            topic_key=RTC_TOPIC["SPORT_MOD"],
            api_id=SPORT_CMD["Damp"],
            parameter=None,
            zero_joystick=True,
            drops_robot=True,
        )

    def stand_command(self) -> tuple:
        return (RTC_TOPIC["SPORT_MOD"], SPORT_CMD["BalanceStand"], None)

    #: Ways back onto the feet, and why they are not the same command.
    #:
    #: `BalanceStand` — what `robot_stand` sends — asks a robot that is already
    #: upright to hold its balance. On a Go2 lying on its side or its back it
    #: does nothing useful; the firmware needs `RecoveryStand` to roll up, and
    #: `StandUp` to rise from a crouch. Both ids sat in `SPORT_CMD` and in
    #: `_DEDICATED` from the start, and `_DEDICATED` is exactly what excludes a
    #: command from the action catalog — so **no tool could send either one**,
    #: and a fallen dog had no way up through the MCP surface at all.
    RECOVER_MODES = {
        "stand_up": SPORT_CMD["StandUp"],
        "recovery_stand": SPORT_CMD["RecoveryStand"],
    }

    def recover_command(self, mode: str) -> tuple:
        """``(topic, api_id, parameter)`` for one recovery mode.

        Raises ``KeyError`` for an unknown mode; the tool turns that into a
        refusal rather than sending a guess to a real robot.
        """
        return (RTC_TOPIC["SPORT_MOD"], self.RECOVER_MODES[mode], None)

    def lie_command(self) -> tuple:
        return (RTC_TOPIC["SPORT_MOD"], SPORT_CMD["StandDown"], None)

    def describe(self) -> dict:
        info = super().describe()
        info.update(
            {
                "sport_api": RTC_TOPIC["SPORT_MOD"],
                "motion_switcher_api": RTC_TOPIC["MOTION_SWITCHER"],
                "motion_modes": list(MOTION_MODES),
                "notes": [
                    "Move (1008) has no duration — the agent schedules StopMove (1003) itself.",
                    "LeadFollow and FreeWalk are omitted: upstream maps both to api_id 1045.",
                    "MCF ids (2000+) require the robot to already be in MCF mode; this profile drives normal ids.",
                ],
            }
        )
        return info
