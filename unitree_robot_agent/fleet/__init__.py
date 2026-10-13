"""Fleet layer: identity, roster and per-device credentials for N > 1 robots.

Why this exists
---------------
The agent core was written for exactly one robot per process, and its
identifying field is the *model family* (``"Go2"``). That is enough for one
robot and cannot express two: everything from the audit trail to the AES key
cache to the snapshot archive keys off "Go2" and therefore conflates them.

This package adds the missing layer without touching the per-robot core it
sits on — a robot's declared identity (:mod:`~unitree_robot_agent.fleet.spec`),
the set of robots in play (:mod:`~unitree_robot_agent.fleet.roster`), and
per-device credentials (:mod:`~unitree_robot_agent.fleet.keys`).

The per-robot machinery that already works — ``RobotSession``, ``Guard``,
``MotionController``, the mock backend — is reused unchanged and simply
instantiated once per roster entry.
"""

from typing import TYPE_CHECKING, Any

from .interference import (
    InterferenceReport,
    Timing,
    measure_connect_impact,
)
from .keys import KeyEntry, KeyStore
from .preflight import PreflightResult, probe_robot, run_preflight, summarise
from .roster import (
    DEFAULT_STATE_DIR as ROSTER_DEFAULT_STATE_DIR,
    ROSTER_ENV_VAR,
    Roster,
    load_roster,
)
from .spec import RobotSpec, normalise_key

if TYPE_CHECKING:  # pragma: no cover - import cycle, see __getattr__
    from .manager import FleetManager, RobotOutcome

_LAZY = ("FleetManager", "RobotOutcome")


def __getattr__(name: str) -> Any:
    """Resolve :class:`FleetManager` on first use (PEP 562).

    ``config.py`` imports ``fleet.keys`` to share one key-cache
    implementation, and importing any submodule runs this file. If the
    manager were imported eagerly here, ``manager`` → ``config`` would close
    the cycle and the package would fail to import at all.

    Keeping it lazy preserves both things we want: `config` (the core) has no
    dependency on the fleet layer, and ``from unitree_robot_agent.fleet
    import FleetManager`` still works.
    """
    if name in _LAZY:
        from . import manager

        return getattr(manager, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "FleetManager",
    "InterferenceReport",
    "KeyEntry",
    "KeyStore",
    "Timing",
    "measure_connect_impact",
    "PreflightResult",
    "probe_robot",
    "run_preflight",
    "summarise",
    "ROSTER_ENV_VAR",
    "ROSTER_DEFAULT_STATE_DIR",
    "RobotOutcome",
    "RobotSpec",
    "Roster",
    "load_roster",
    "normalise_key",
]
