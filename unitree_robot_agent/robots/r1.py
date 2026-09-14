"""Unitree R1 (Air / Pro / Edu) profile.

Two facts drive almost every design decision in this file:

* **Run is 811, not G1's 801** — and on an *Air* chassis the firmware
  redirects 811 to ``Loco20Dof`` (830). Requesting 811 and reading back 830
  is a success, so the two are put in the same semantic group and the
  redirect is reported as ``redirected: true, confirmed: true``.
* **Eight states stop publishing ``sportmodestate``** (5, 6, 7, 800, 813,
  814 and the G1-only 804 / 805). The read-back freezes at the last value,
  which is *not* a failure and must not make a tool wait for a change.
"""

from __future__ import annotations

from typing import Mapping, Optional

from unitree_webrtc_connect.constants import (
    ARM_ACTION,
    LOCO_API,
    RTC_TOPIC,
    R1_FSM,
    LOCO_API_SERVED_R1,
)

from .base import (
    CAP_ARM_GESTURES,
    CAP_ERRORS,
    CAP_FSM_ACTIONS,
    CAP_JOYSTICK,
    CAP_LOWSTATE,
    CAP_SNAPSHOT,
    CAP_STATUS,
    CAP_VELOCITY,
    ActionInfo,
    RobotProfile,
    StateInfo,
    StopPlan,
    TransitionCheck,
)

#: fsm_id → (semantic, dangerous, reports_state, requires_lock, description)
_TABLE = {
    0: ("zero_torque", True, True, False,
        "Motors go free. The robot drops if it is standing. Only reachable from Damping."),
    1: ("damping", False, True, False,
        "Soft stop. Accepted from EVERY state — the universal escape hatch."),
    4: ("stance", False, True, False,
        "Standing stance ('Lock' in the app). The hub: most motions must be entered from here."),
    5: ("hold", False, False, True, "Hold the current pose."),
    6: ("transition", False, False, True, "MoveTo (internal transition state)."),
    7: ("sit", False, False, True, "Sit down."),
    601: ("dance", True, True, True, "Dance 1 (dances_air: only 602/604/607 on the Air chassis)."),
    602: ("dance", True, True, True, "Dance 2 (Air)."),
    603: ("dance", True, True, True, "Dance 3 (not in dances_air)."),
    604: ("dance", True, True, True, "Twist / niuniuwu (Air)."),
    607: ("martial", True, True, True, "Kung Fu / gongfu (Air)."),
    608: ("martial", True, True, True, "Jeet Kune Do / jiequandao (not in dances_air)."),
    701: ("recovery", True, True, True,
          "Stand up (Qishen). Recovers from face-up or face-down; needs clear space. Air uses a dedicated ONNX policy."),
    702: ("recovery", True, True, True, "Lie down (Tangxia) — the robot ends on the ground."),
    800: ("locomotion", False, False, True, "Motion super-state. Stops state reporting."),
    811: ("locomotion", False, True, True,
          "Run / AmpMotion22Dof. ON AIR CHASSIS THE FIRMWARE REDIRECTS THIS TO 830 (Loco20Dof)."),
    812: ("locomotion", False, True, True, "AMP motion, 24-dof."),
    813: ("locomotion", False, False, True, "Walk, straight knee. Stops state reporting."),
    814: ("locomotion", False, False, True, "Walk, 24-dof. Stops state reporting."),
    815: ("locomotion", False, True, True, "AMP locomotion."),
    816: ("locomotion", False, True, True, "Locomotion + arm SDK."),
    830: ("locomotion", False, True, True,
          "20-dof locomotion. This is where the Air chassis lands after asking for Run (811). "
          "NOTE: its speed handler is fixed at vx 1.0 — SET_VELOCITY cannot re-scale it."),
    831: ("locomotion", False, True, True, "Locomotion + arm SDK (20-dof)."),
}

#: Actions that may be worth exposing. Damping / Lock are omitted: they have
#: dedicated tools (robot_stop / robot_stand) with their own guard rails.
_ACTIONS = [
    ("run", 811, False, True, "Start walking/locomotion. Air redirects to Loco20Dof (830)."),
    ("walk", 814, False, True, "Walk, 24-dof."),
    ("amp_locomotion", 815, False, True, "AMP locomotion."),
    ("loco_20dof", 830, False, True, "20-dof locomotion (fixed vx 1.0 — not speed-scalable)."),
    ("loco_arm_sdk", 831, False, True, "Locomotion with arm SDK."),
    ("arm_sdk_loco", 816, False, True, "Locomotion with arm SDK."),
    ("motion", 800, False, True, "Motion super-state (stops state reporting)."),
    ("amp_motion", 812, False, True, "AMP motion, 24-dof."),
    ("walk_straight_knee", 813, False, True, "Walk with straight knees (stops state reporting)."),
    ("keep", 5, False, True, "Hold the current pose (stops state reporting)."),
    ("move_to", 6, False, True, "MoveTo (stops state reporting)."),
    ("sit_down", 7, False, True, "Sit down."),
    ("dance_1", 601, True, True, "Dance 1."),
    ("dance_2", 602, True, True, "Dance 2."),
    ("dance_3", 603, True, True, "Dance 3."),
    ("twist", 604, True, True, "Twist / niuniuwu."),
    ("kung_fu", 607, True, True, "Kung Fu."),
    ("jeet_kune_do", 608, True, True, "Jeet Kune Do."),
    ("stand_up", 701, True, True, "Get up off the ground (face-up or face-down)."),
    ("lie_down", 702, True, True, "Lie down — the robot ends on the ground."),
    ("zero_torque", 0, True, False, "Cut motor power. Only from Damping. The robot drops."),
]

#: States the Air chassis firmware does not serve — dances_air holds only
#: 602/604/607. Applied only when chassis == "air"; unverified on hardware
#: (see references/verification-checklist.md), hence opt-in.
_AIR_UNSUPPORTED = frozenset({601, 603, 608})

_ACTION_ALIASES = {
    "run": ("locomotion", "amp_motion_22dof", "walk_start"),
    "walk": ("walk_24",),
    "twist": ("niuniuwu", "niu_niu_wu"),
    "kung_fu": ("gongfu", "kungfu"),
    "jeet_kune_do": ("jiequandao", "jeetkune", "jkd"),
    "stand_up": ("qishen", "get_up", "standup"),
    "lie_down": ("tangxia", "liedown"),
    "zero_torque": ("zero", "free_motors"),
    "dance_1": ("dance1", "dances_1"),
    "dance_2": ("dance2", "dances_2"),
    "dance_3": ("dance3", "dances_3"),
    "loco_20dof": ("loco20dof", "locomotion_20dof"),
    "sit_down": ("sit", "sitdown"),
}

#: Gestures R1 actually ships. G1 additionally has ArmHeart (20) and
#: RightHeart (21); requesting those on R1 is refused by the robot.
_GESTURES = [
    ("release", "Release", "Cancel the current gesture and bring the arms back to neutral."),
    ("handshake", "Handshake", "Offer a hand to shake."),
    ("high_five", "HighFive", "High five."),
    ("hug", "Hug", "Hug."),
    ("high_wave", "HighWave", "Wave with the arm raised."),
    ("clap", "Clap", "Clap."),
    ("face_wave", "FaceWave", "Wave in front of the face."),
    ("left_kiss", "LeftKiss", "Blow a kiss with the left hand."),
    ("x_ray", "XRay", "Ultraman ray pose."),
    ("hands_up", "HandsUp", "Both hands up."),
    ("right_hand_up", "RightHandUp", "Right hand up."),
    ("reject", "Reject", "Refusal gesture."),
    ("forward_push", "ForwardPush", "Push forward."),
]

_GESTURE_ALIASES = {
    "high_five": ("highfive", "high-5"),
    "high_wave": ("highwave",),
    "face_wave": ("facewave",),
    "left_kiss": ("leftkiss", "kiss"),
    "right_hand_up": ("righthandup",),
    "hands_up": ("handsup",),
    "forward_push": ("forwardpush", "push"),
    "x_ray": ("xray",),
}


class R1Profile(RobotProfile):
    """Semantic model of an R1."""

    name = "R1"
    device_type = "R1"
    state_topic = RTC_TOPIC["LF_SPORT_MOD_STATE"]
    report_topics = (RTC_TOPIC["LF_SPORT_MOD_STATE"],)

    #: Telemetry topics the lowstate tools subscribe to on demand.
    telemetry_topics = (
        RTC_TOPIC["LOW_STATE"],
        RTC_TOPIC["BMS_STATE"],
        RTC_TOPIC["SECONDARY_IMU"],
        RTC_TOPIC["MAIN_BOARD_STATE"],
    )

    def capabilities(self) -> frozenset:
        return frozenset(
            {
                CAP_STATUS,
                CAP_LOWSTATE,
                CAP_ERRORS,
                CAP_SNAPSHOT,
                CAP_FSM_ACTIONS,
                CAP_ARM_GESTURES,
                CAP_VELOCITY,
                CAP_JOYSTICK,
            }
        )

    # ── states ───────────────────────────────────────────────────────

    def states(self) -> Mapping[int, StateInfo]:
        # NOTE: R1_FSM is name -> fsm_id.
        out = {}
        for name, fsm_id in R1_FSM.items():
            entry = _TABLE.get(fsm_id)
            if entry is None:
                out[fsm_id] = StateInfo(fsm_id=fsm_id, name=name, semantic="unknown")
                continue
            semantic, dangerous, reports, needs_lock, description = entry
            out[fsm_id] = StateInfo(
                fsm_id=fsm_id,
                name=name,
                semantic=semantic,
                dangerous=dangerous,
                reports_state=reports,
                requires_lock=needs_lock,
                description=description,
            )
        return dict(sorted(out.items(), key=lambda kv: kv[0]))

    # ── actions ──────────────────────────────────────────────────────

    def actions(self) -> Mapping[str, ActionInfo]:
        out = {}
        skip_air = self.chassis == "air"
        for name, fsm_id, dangerous, needs_lock, description in _ACTIONS:
            if skip_air and fsm_id in _AIR_UNSUPPORTED:
                continue
            out[name] = ActionInfo(
                name=name,
                kind="fsm",
                value=fsm_id,
                dangerous=dangerous,
                requires_lock=needs_lock,
                description=description,
                aliases=_ACTION_ALIASES.get(name, ()),
                topic_key=RTC_TOPIC["SPORT_MOD"],
            )
        for name, constant, description in _GESTURES:
            if constant not in ARM_ACTION:
                continue
            out[name] = ActionInfo(
                name=name,
                kind="arm",
                value=ARM_ACTION[constant],
                dangerous=False,
                requires_lock=False,
                description=description,
                aliases=_GESTURE_ALIASES.get(name, ()),
                topic_key=RTC_TOPIC["ARM_REQUEST"],
            )
        return out

    def list_actions(self, *, dangerous=None):
        rows = super().list_actions(dangerous=dangerous)
        for row in rows:
            action = self.actions()[row["name"]]
            row["kind"] = action.kind
        return rows

    # ── reporting ────────────────────────────────────────────────────

    def reported_state(self, topic: str, payload: dict) -> Optional[int]:
        if topic != self.state_topic or not isinstance(payload, dict):
            return None
        value = payload.get("fsm_id")
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def reported_posture(self, payload: dict) -> Optional[str]:
        if not isinstance(payload, dict):
            return None
        state = self.classify(self.reported_state(self.state_topic, payload))
        return state.get("state")

    # ── local rules ──────────────────────────────────────────────────

    def lock_reachable_from(self) -> frozenset:
        # Damping is the only state that accepts an arbitrary motion; Lock
        # obviously accepts motions too.
        return frozenset({1, 4})

    def validate_transition(self, current_id: Optional[int], target_id: int) -> TransitionCheck:
        # ZeroTorque has its own rule: motors-free is only accepted from
        # Damping, because going limp while standing drops the robot.
        if int(target_id) == R1_FSM["ZeroTorque"]:
            if current_id is None:
                return TransitionCheck(allowed=True)
            if int(current_id) != R1_FSM["Damping"]:
                return TransitionCheck(
                    allowed=False,
                    confident=True,
                    reason=(
                        "ZeroTorque (0) is only reachable from Damping (1). The robot is "
                        f"in '{self.state_name(current_id)}' ({current_id})."
                    ),
                    hint="Call robot_stop first (Damping), then request zero_torque.",
                )
            return TransitionCheck(allowed=True)
        if int(target_id) == R1_FSM["Damping"]:
            return TransitionCheck(allowed=True)
        return super().validate_transition(current_id, target_id)

    # ── stop plans ───────────────────────────────────────────────────

    def soft_stop(self) -> StopPlan:
        return StopPlan(
            label="R1 SET_VELOCITY [0,0,0] (posture preserved)",
            topic_key=RTC_TOPIC["SPORT_MOD"],
            api_id=LOCO_API["SET_VELOCITY"],
            parameter={"velocity": [0.0, 0.0, 0.0], "duration": 1.0},
            zero_joystick=True,
            drops_robot=False,
        )

    def emergency_stop(self) -> StopPlan:
        return StopPlan(
            label="R1 SET_FSM_ID Damping(1) — may drop the robot",
            topic_key=RTC_TOPIC["SPORT_MOD"],
            api_id=LOCO_API["SET_FSM_ID"],
            parameter={"data": R1_FSM["Damping"]},
            zero_joystick=True,
            drops_robot=True,
        )

    # ── extras ───────────────────────────────────────────────────────

    def describe(self) -> dict:
        info = super().describe()
        info.update(
            {
                "fsm_api": LOCO_API["SET_FSM_ID"],
                "velocity_api": LOCO_API["SET_VELOCITY"],
                "arm_api": LOCO_API["SET_ARM_TASK"],
                "served_apis": list(LOCO_API_SERVED_R1),
                "notes": [
                    "Run (811) is redirected to Loco20Dof (830) on an Air chassis — a success, not a mismatch.",
                    "States 5/6/7/800/813/814 freeze state reporting; read-back is stale there.",
                    "Damping (1) is accepted from every state.",
                    "Loco20Dof (830) ignores speed scaling (fixed vx 1.0).",
                ],
            }
        )
        return info
