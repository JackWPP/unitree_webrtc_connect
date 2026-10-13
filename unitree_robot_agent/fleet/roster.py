"""The robot roster: the full set of robots this process may talk to.

The single-robot agent reads exactly one address out of the environment
(``UNITREE_ROBOT_IP``, ``UNITREE_ROBOT_SN``, ``UNITREE_AES_128_KEY`` …). That
cannot express a fleet, and the failure mode of stretching it — several
processes, each with its own env — is precisely what we want to avoid: one
process per robot means N event loops and no shared state to coordinate on.

A roster is a small JSON document (or inline JSON in an env var) naming each
robot. It also derives a **per-robot state directory**, which is the single
highest-leverage line in the whole fleet design: it simultaneously fixes

* the AES key cache (two Go2s no longer share ``keys.json``),
* the cached cloud session (one account's logout cannot wipe another's),
* the snapshot archive (frames are attributable),
* the audit log path (two trails, not one interleaved one).

Cross-references in the state dir are what make same-model robots
indistinguishable; giving each its own directory removes the whole class.

Roster JSON schema::

    {
      "version": 1,
      "state_dir": "~/.unitree-agent",
      "default": "dog1",
      "defaults": {"idle_disconnect_s": 45, "safe_mode": true},
      "robots": [
        {"id": "dog1", "model": "Go2", "connection": "sta-l",
         "ip": "192.168.31.245", "sn": "B42D2000XXXXXXXX",
         "aes_128_key": "<32 hex>", "role": "lead"},
        {"id": "dog2", "model": "Go2", "connection": "sta-l",
         "ip": "192.168.31.246", "role": "wing"}
      ]
    }
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple, Union

from ..errors import FleetConfigError, UnknownRobot
from .spec import RobotSpec, normalise_key

#: Env var holding either a path to a roster file or the JSON itself.
ROSTER_ENV_VAR = "UNITREE_AGENT_ROSTER"

#: Where per-robot state lives unless the roster says otherwise.
DEFAULT_STATE_DIR = "~/.unitree-agent"

RosterSource = Union[str, Path, Mapping[str, Any], None]


def _is_inline_json(text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith("{") or stripped.startswith("[")


def _read_source(source: RosterSource) -> Mapping[str, Any]:
    """Normalise a path / inline JSON / mapping / env var into a mapping."""
    if source is None:
        source = os.environ.get(ROSTER_ENV_VAR) or None
        if source is None:
            raise FleetConfigError(
                f"No roster configured. Point {ROSTER_ENV_VAR} at a JSON file "
                f"(or inline JSON), or run with a single-robot environment.",
                detail={"env_var": ROSTER_ENV_VAR},
            )

    if isinstance(source, Mapping):
        return source

    text = str(source)
    if _is_inline_json(text):
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            raise FleetConfigError(
                f"{ROSTER_ENV_VAR} looks like inline JSON but does not parse: {exc}",
                detail={"error": str(exc)},
            ) from exc
        origin = f"{ROSTER_ENV_VAR} (inline)"
    else:
        path = Path(text).expanduser()
        if not path.is_file():
            raise FleetConfigError(
                f"Roster file not found: {path}",
                detail={"path": str(path)},
            )
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise FleetConfigError(
                f"Could not read roster {path}: {exc}",
                detail={"path": str(path)},
            ) from exc
        origin = str(path)

    if isinstance(parsed, list):
        # Tolerate a bare list of robots — nothing else to configure.
        parsed = {"robots": parsed}
    if not isinstance(parsed, Mapping):
        raise FleetConfigError(
            f"Roster {origin} must be a JSON object (or a bare list of robots), "
            f"got {type(parsed).__name__}.",
            detail={"origin": origin},
        )
    return parsed


def _check_unique(robots: Tuple[RobotSpec, ...]) -> None:
    """Reject rosters that would make two entries fight over one robot slot.

    A Unitree robot serves exactly one WebRTC client. If two specs resolve to
    the same physical robot, one of them will spend its life getting
    ``RobotBusy`` — and the operator will blame the network. Catch it here,
    where the error can still name both entries.
    """
    seen_ids: Dict[str, int] = {}
    seen_sn: Dict[str, str] = {}
    seen_ip: Dict[str, str] = {}

    for index, spec in enumerate(robots):
        if spec.robot_id in seen_ids:
            raise FleetConfigError(
                f"Duplicate robot id {spec.robot_id!r} "
                f"(robots[{seen_ids[spec.robot_id]}] and robots[{index}]). "
                f"Ids are how tools and the console address a robot, so they "
                f"must be unique.",
                detail={"robot_id": spec.robot_id},
            )
        seen_ids[spec.robot_id] = index

        if spec.serial_number:
            if spec.serial_number in seen_sn:
                raise FleetConfigError(
                    f"Robots {seen_sn[spec.serial_number]!r} and "
                    f"{spec.robot_id!r} both claim serial number "
                    f"{spec.serial_number!r}. They are the same robot, and it "
                    f"only accepts one WebRTC client.",
                    detail={"serial_number": spec.serial_number},
                )
            seen_sn[spec.serial_number] = spec.robot_id

        if spec.ip:
            if spec.ip in seen_ip:
                raise FleetConfigError(
                    f"Robots {seen_ip[spec.ip]!r} and {spec.robot_id!r} both "
                    f"claim IP {spec.ip!r}. They are the same robot, and it "
                    f"only accepts one WebRTC client.",
                    detail={"ip": spec.ip},
                )
            seen_ip[spec.ip] = spec.robot_id


@dataclass(frozen=True)
class Roster:
    """An ordered, validated set of robots plus the fleet-wide defaults."""

    robots: Tuple[RobotSpec, ...]
    default_id: str = ""
    state_dir: Path = Path(DEFAULT_STATE_DIR)
    defaults: Mapping[str, Any] = field(default_factory=dict)

    # ── container behaviour ──────────────────────────────────────────

    def __len__(self) -> int:
        return len(self.robots)

    def __iter__(self) -> Iterator[RobotSpec]:
        return iter(self.robots)

    @property
    def ids(self) -> Tuple[str, ...]:
        return tuple(spec.robot_id for spec in self.robots)

    @property
    def is_single(self) -> bool:
        return len(self.robots) == 1

    def get(self, robot_id: str) -> RobotSpec:
        for spec in self.robots:
            if spec.robot_id == robot_id:
                return spec
        raise UnknownRobot(
            f"No robot {robot_id!r} on the roster. Known robots: "
            f"{', '.join(self.ids)}.",
            detail={"robot_id": robot_id, "known": list(self.ids)},
        )

    def resolve(self, robot_id: str = "") -> RobotSpec:
        """Turn an optional ``robot_id`` into a spec.

        Omitted means "the default robot". With exactly one robot on the
        roster that is unambiguous; with several we refuse rather than guess,
        because silently picking a dog for a movement command is how you
        move the wrong one.
        """
        if robot_id:
            return self.get(robot_id)
        if self.default_id:
            return self.get(self.default_id)
        if self.is_single:
            return self.robots[0]
        raise UnknownRobot(
            "More than one robot is configured, so `robot_id` is required. "
            "Known robots: " + ", ".join(self.ids) + ".",
            detail={"known": list(self.ids)},
        )

    # ── per-robot state ──────────────────────────────────────────────

    def state_dir_for(self, robot_id: str) -> Path:
        """Per-robot state directory. See the module docstring for why."""
        return Path(self.state_dir).expanduser() / robot_id

    def options_for(self, spec: RobotSpec) -> Dict[str, Any]:
        """Roster defaults with the spec's own overrides applied on top."""
        merged: Dict[str, Any] = dict(self.defaults)
        merged.update(spec.options)
        return merged

    # ── views ────────────────────────────────────────────────────────

    def redacted(self) -> Dict[str, Any]:
        return {
            "default": self.default_id,
            "state_dir": str(self.state_dir),
            "robots": [spec.redacted() for spec in self.robots],
        }

    # ── construction ─────────────────────────────────────────────────

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "Roster":
        entries = raw.get("robots")
        if entries is None:
            raise FleetConfigError("Roster has no `robots` list.")
        if not isinstance(entries, list):
            raise FleetConfigError(
                f"`robots` must be a list, got {type(entries).__name__}."
            )
        if not entries:
            raise FleetConfigError(
                "Roster has an empty `robots` list. Add at least one robot "
                "(or use the single-robot environment variables instead)."
            )

        specs: List[RobotSpec] = []
        for index, entry in enumerate(entries):
            spec = RobotSpec.from_mapping(entry, index=index)
            # Validate eagerly so a bad key is caught at load time, not at the
            # first handshake — where it would surface as a GCM tag failure
            # that looks like a hardware problem.
            if spec.aes_128_key:
                spec = replace(spec, aes_128_key=normalise_key(spec.aes_128_key))
            specs.append(spec)

        robots = tuple(specs)
        _check_unique(robots)

        state_dir_raw = raw.get("state_dir") or DEFAULT_STATE_DIR
        default_id = str(raw.get("default") or "").strip()
        if default_id and default_id not in {s.robot_id for s in robots}:
            raise FleetConfigError(
                f"`default` names {default_id!r}, which is not on the roster. "
                f"Known robots: {', '.join(s.robot_id for s in robots)}.",
                detail={"default": default_id},
            )

        defaults = raw.get("defaults") or {}
        if not isinstance(defaults, Mapping):
            raise FleetConfigError("`defaults` must be an object.")

        return cls(
            robots=robots,
            default_id=default_id,
            state_dir=Path(str(state_dir_raw)),
            defaults=dict(defaults),
        )

    @classmethod
    def from_source(cls, source: RosterSource = None) -> "Roster":
        return cls.from_mapping(_read_source(source))

    @classmethod
    def single(cls, spec: RobotSpec, *, state_dir: Optional[Path] = None) -> "Roster":
        """A one-robot roster — the bridge for the existing single-robot path."""
        return cls(
            robots=(spec,),
            default_id=spec.robot_id,
            state_dir=Path(state_dir) if state_dir is not None else Path(DEFAULT_STATE_DIR),
            defaults={},
        )


def load_roster(source: RosterSource = None) -> Roster:
    """Load a roster from a path, inline JSON, mapping, or the env var."""
    return Roster.from_source(source)
