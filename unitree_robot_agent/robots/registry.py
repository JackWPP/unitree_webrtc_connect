"""Robot profile registry."""

from __future__ import annotations

from typing import Dict, List

from ..errors import FeatureUnavailable
from .base import RobotProfile


def _builtin() -> Dict[str, type]:
    from .go2 import Go2Profile
    from .r1 import R1Profile

    return {"R1": R1Profile, "Go2": Go2Profile}


def available_robots() -> List[str]:
    return sorted(_builtin())


def get_profile(name: str, *, chassis: str = "unknown") -> RobotProfile:
    """Return the profile for ``name`` ("R1" / "Go2")."""
    key = (name or "").strip()
    for known, cls in _builtin().items():
        if key.lower() == known.lower():
            return cls(chassis=chassis)
    if key.lower() == "g1":
        raise FeatureUnavailable(
            "The G1 profile is not implemented yet. The architecture supports "
            "it — add unitree_robot_agent/robots/g1.py and register it — but "
            "the G1 FSM table and its 801-vs-811 differences have not been "
            "verified against a robot.",
            detail={"robot": "G1"},
        )
    raise FeatureUnavailable(
        f"Unknown robot {name!r}. Supported: {', '.join(available_robots())}.",
        detail={"robot": name, "supported": available_robots()},
    )
