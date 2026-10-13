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

from .keys import KeyEntry, KeyStore
from .roster import (
    DEFAULT_STATE_DIR as ROSTER_DEFAULT_STATE_DIR,
    ROSTER_ENV_VAR,
    Roster,
    load_roster,
)
from .spec import RobotSpec, normalise_key

__all__ = [
    "KeyEntry",
    "KeyStore",
    "ROSTER_ENV_VAR",
    "ROSTER_DEFAULT_STATE_DIR",
    "RobotSpec",
    "Roster",
    "load_roster",
    "normalise_key",
]
