"""Robot profile abstraction — one class per robot family.

A profile owns everything that is *semantic* and robot-specific:

* which capabilities exist (drives which MCP tools get registered),
* the state table and its normalization rules,
* the local white/black list used to refuse obviously-illegal transitions
  before they reach the robot,
* the action catalog (dance / gesture / sport command names) exposed to
  the model instead of raw ids,
* the stop plans used by the guard layer.

Adding G1 later means adding one profile class and registering it — no
change anywhere else.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, List, Mapping, Optional, Tuple

# ─── capability names ─────────────────────────────────────────────────
# Kept as plain strings: they end up in tool documentation and in the
# `robot_capabilities` output, so readability beats enum ceremony.

CAP_STATUS = "status"                     # connection / battery / state read
CAP_LOWSTATE = "lowstate"                 # motors + IMUs + board temperature
CAP_ERRORS = "errors"                     # active robot error list
CAP_SNAPSHOT = "snapshot"                 # camera still frame
CAP_FSM_ACTIONS = "fsm_actions"           # humanoid FSM state machine (R1/G1)
CAP_ARM_GESTURES = "arm_gestures"         # humanoid upper-limb gestures
CAP_VELOCITY = "velocity"                 # direct velocity command (R1 7105)
CAP_JOYSTICK = "joystick"                 # rt/wirelesscontroller teleop
CAP_SPORT_COMMANDS = "sport_commands"     # Go2 sport api_ids
CAP_MOTION_SWITCHER = "motion_switcher"   # Go2 normal / ai / mcf mode
CAP_VUI = "vui"                           # Go2 LED / volume / brightness
CAP_OBSTACLE_AVOID = "obstacle_avoid"     # Go2 avoidance service
CAP_LIDAR = "lidar"                       # Go2 LiDAR point cloud


@dataclass(frozen=True)
class StateInfo:
    """One entry of a robot's state/mode table."""

    fsm_id: int
    name: str
    #: Coarse semantic bucket used for grouping and for "is this motion?".
    semantic: str
    #: False for states the firmware stops reporting while active. Reading a
    #: stale `fsm_id` in those states is expected, not a bug.
    reports_state: bool = True
    #: True when entering the state can move/drop the robot.
    dangerous: bool = False
    #: True when the robot only accepts this state from Lock (4).
    requires_lock: bool = False
    description: str = ""
    aliases: Tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "id": self.fsm_id,
            "name": self.name,
            "semantic": self.semantic,
            "dangerous": self.dangerous,
            "requires_lock": self.requires_lock,
            "reports_state": self.reports_state,
            "description": self.description,
        }


@dataclass(frozen=True)
class ActionInfo:
    """A named action the model may ask for."""

    name: str
    #: "fsm"   → humanoid state machine (SET_FSM_ID)
    #: "arm"   → humanoid gesture (SET_ARM_TASK)
    #: "sport" → Go2 sport api_id
    kind: str
    value: int
    dangerous: bool = False
    requires_lock: bool = False
    description: str = ""
    aliases: Tuple[str, ...] = ()
    #: Extra payload the command needs, or None when it takes none.
    #:
    #: Most sport commands are bare — `Scrape`, `FrontJump` and `FrontPounce`
    #: are all sent with no parameter in the library's own examples. A few are
    #: not: the examples pass `{"data": True}` for `Pose`, `FrontFlip`,
    #: `LeftFlip` and `BackFlip`. Sending nothing to one of those is not an
    #: error the firmware reports, it is simply *nothing happening*, which is
    #: the worst possible failure mode on a robot and one nobody can debug from
    #: a web page.
    parameter: Optional[Dict[str, Any]] = None
    #: Topics the command is sent to; filled by the profile.
    topic_key: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "id": self.value,
            "dangerous": self.dangerous,
            "requires_lock": self.requires_lock,
            "description": self.description,
        }


@dataclass(frozen=True)
class StopPlan:
    """How to bring this robot to rest."""

    label: str
    topic_key: Optional[str] = None
    api_id: Optional[int] = None
    parameter: Any = None
    #: Also publish a zeroed joystick frame (harmless, and needed when the
    #: robot was being driven through rt/wirelesscontroller).
    zero_joystick: bool = True
    #: True when this plan makes the robot go limp / lie down.
    drops_robot: bool = False


@dataclass
class TransitionCheck:
    """Result of the local pre-flight check."""

    allowed: bool = True
    reason: str = ""
    hint: str = ""
    #: True when we refuse although the robot might have accepted it.
    confident: bool = False

    def to_dict(self) -> dict:
        out = {"allowed": self.allowed}
        if self.reason:
            out["reason"] = self.reason
        if self.hint:
            out["hint"] = self.hint
        return out


_EMPTY: Mapping[str, Any] = {}


class RobotProfile(ABC):
    """Base class for robot-family profiles."""

    name: str = "unknown"
    device_type: str = "unknown"
    #: Chassis variant ("unknown" unless the operator declared one).
    chassis: str = "unknown"

    def __init__(self, *, chassis: str = "unknown") -> None:
        self.chassis = (chassis or "unknown").lower()

    #: Topics that should be subscribed once per connection to keep a local
    #: view of the robot. First one is the primary state topic.
    report_topics: Tuple[str, ...] = ()
    #: The state topic this model is known to publish.
    state_topic: Optional[str] = None
    #: Every topic worth trying for the state, best first. Firmware differs on
    #: which one it answers — `rt/lf/sportmodestate` and `rt/sportmodestate`
    #: are both in the wild for the *same* robot family, and at least one real
    #: Go2 publishes only the `lf` variant. Kept in step with
    #: `telemetry.GO2_STATE_TOPICS` so the read budget matches the attempt
    #: count.
    state_topics: Tuple[str, ...] = ()

    # ── capability surface ───────────────────────────────────────────

    @abstractmethod
    def capabilities(self) -> frozenset:
        """Capability names (see ``CAP_*``) available on this robot."""

    def has(self, capability: str) -> bool:
        return capability in self.capabilities()

    # ── state table ──────────────────────────────────────────────────

    @abstractmethod
    def states(self) -> Mapping[int, StateInfo]:
        """fsm_id → StateInfo. Ordered as presented to the model."""

    # ── action catalog ───────────────────────────────────────────────

    @abstractmethod
    def actions(self) -> Mapping[str, ActionInfo]:
        """lower-cased action name → ActionInfo."""

    def resolve_action(self, name: str) -> Optional[ActionInfo]:
        target = (name or "").strip().lower()
        if not target:
            return None
        catalog = self.actions()
        if target in catalog:
            return catalog[target]
        for action in catalog.values():
            if target in (alias.lower() for alias in action.aliases):
                return action
            if target == action.name.lower().replace("_", "").replace(" ", ""):
                return action
        return None

    def list_actions(self, *, dangerous: Optional[bool] = None) -> List[dict]:
        rows = []
        for action in self.actions().values():
            if dangerous is not None and action.dangerous != dangerous:
                continue
            rows.append(action.to_dict())
        return sorted(rows, key=lambda r: (r["dangerous"], r["name"]))

    def list_states(self) -> List[dict]:
        return [info.to_dict() for info in self.states().values()]

    # ── reporting / normalization ────────────────────────────────────

    def reported_state(self, topic: str, payload: dict) -> Optional[int]:
        """Extract the robot-reported state id from a topic payload.

        Returns ``None`` when the payload carries no state (a different
        topic, or a partial message)."""
        return None

    def reported_posture(self, payload: dict) -> Optional[str]:
        """Optional human-readable posture from a state payload."""
        return None

    @property
    def state_candidates(self) -> Tuple[str, ...]:
        """Topics to try for the state, in order, without duplicates."""
        ordered = self.state_topics or ((self.state_topic,) if self.state_topic else ())
        seen = []
        for topic in ordered:
            if topic and topic not in seen:
                seen.append(topic)
        return tuple(seen)

    def classify(self, fsm_id: Optional[int]) -> dict:
        """Normalize a state id into a model-friendly dict."""
        if fsm_id is None:
            return {
                "fsm_id": None,
                "state": "unknown",
                "semantic": "unknown",
                "reporting": "unknown",
                "dangerous": False,
                "note": "No state has been reported yet (or the robot does not report in this state).",
            }
        info = self.states().get(int(fsm_id))
        if info is None:
            return {
                "fsm_id": int(fsm_id),
                "state": "unknown",
                "semantic": "unknown",
                "reporting": "unknown",
                "dangerous": False,
                "note": f"fsm_id={fsm_id} is not in this profile's table; firmware may be newer.",
            }
        return {
            "fsm_id": info.fsm_id,
            "state": info.name,
            "semantic": info.semantic,
            "reporting": "reported" if info.reports_state else "frozen",
            "dangerous": info.dangerous,
            "note": info.description,
        }

    def normalize(self, requested_id: int, reported_id: Optional[int]) -> dict:
        """Explain a requested-vs-reported state pair.

        The key insight this encodes: on an R1 Air, asking for ``Run`` (811)
        gets you ``Loco20Dof`` (830). That is a *successful* request, and
        reporting it as a mismatch is what makes an LLM retry forever.
        """
        requested = self.classify(requested_id)
        reported = self.classify(reported_id)
        result = {
            "requested": requested,
            "reported": reported,
            "redirected": False,
            "confirmed": False,
            "same_semantic_group": False,
        }
        if reported_id is None:
            result["note"] = (
                "The robot's reported state is not available (its state "
                "reporting freezes in several modes). Treat the command as "
                "dispatched, not confirmed."
            )
            return result

        if reported["state"] == requested["state"]:
            result["confirmed"] = True
            return result

        if requested["semantic"] == reported["semantic"] and requested["semantic"] != "unknown":
            result["confirmed"] = True
            result["same_semantic_group"] = True
            result["redirected"] = True
            result["note"] = (
                f"The robot is in {reported['state']} ({reported_id}) instead of "
                f"{requested['state']} ({requested_id}) — same semantic group "
                f"'{requested['semantic']}'. This is a normal firmware "
                f"redirect (e.g. R1 Air maps Run 811 to Loco20Dof 830), not a failure."
            )
            return result

        result["note"] = (
            "Reported state differs from the requested one and is in a "
            f"different group ({requested['semantic']} vs {reported['semantic']}). "
            "If the request returned code 0 the robot accepted it; the "
            "reported value may simply be stale."
        )
        return result

    # ── local white list ─────────────────────────────────────────────

    def validate_transition(self, current_id: Optional[int], target_id: int) -> TransitionCheck:
        """Refuse locally only what we are sure the robot will refuse.

        A false rejection is worse than a wasted round trip: the robot
        answers 1001/1003 with the authoritative reason. So this method
        rejects only two cases it can be certain about:

        1. the target id is not in the profile's table at all, and
        2. the target needs Lock and the *currently reported* state is a
           known motion state that cannot reach it.
        """
        target = self.states().get(int(target_id))
        if target is None:
            return TransitionCheck(
                allowed=False,
                confident=True,
                reason=f"fsm_id={target_id} does not exist on {self.name}.",
                hint="Use robot_list_actions to see the states this chassis exposes.",
            )

        current = self.states().get(int(current_id)) if current_id is not None else None
        if current is None or not target.requires_lock or not current.reports_state:
            return TransitionCheck(allowed=True)

        if current.fsm_id in self.lock_reachable_from():
            return TransitionCheck(allowed=True)

        return TransitionCheck(
            allowed=False,
            confident=True,
            reason=(
                f"'{target.name}' is only reachable from Lock (4), and the "
                f"robot is currently in '{current.name}' ({current.fsm_id})."
            ),
            hint="Call robot_switch_state with state='Lock' (or use the "
                 "`robot_stand` tool) first, wait for it to settle, then retry.",
        )

    def lock_reachable_from(self) -> frozenset:
        """State ids from which a Lock-gated motion is accepted."""
        return frozenset({4, 1, 0})

    # ── stop plans ───────────────────────────────────────────────────

    @abstractmethod
    def soft_stop(self) -> StopPlan:
        """Stop moving without changing posture (safe on a standing robot)."""

    @abstractmethod
    def emergency_stop(self) -> StopPlan:
        """Go limp / damp. May drop a standing robot — explicit use only."""

    # ── helpers ──────────────────────────────────────────────────────

    def state_name(self, fsm_id: Optional[int]) -> str:
        info = self.states().get(int(fsm_id)) if fsm_id is not None else None
        return info.name if info else "unknown"

    def describe(self) -> dict:
        return {
            "robot": self.name,
            "device_type": self.device_type,
            "chassis": self.chassis,
            "capabilities": sorted(self.capabilities()),
            "state_topics": list(self.state_candidates),
            "report_topics": list(self.report_topics),
        }
