"""A browser console for driving two Unitree robots at once.

Design notes that are easy to lose later:

* **One event loop.** The ASGI app and the WebRTC connections share a loop,
  so there is no thread bridge anywhere in the request path. See
  :mod:`unitree_fleet_web.app`.
* **Rules live server-side.** The e-stop latch and the "zero the previous
  robot when the target changes" rule are enforced in
  :mod:`unitree_fleet_web.service`, not in JavaScript, so a second client or
  a stale tab cannot bypass them.
* **Loopback by default, token required to leave it.** A console that can
  drive two robots must not appear on the LAN by accident.
* **Nothing here talks to a robot directly.** It drives
  :class:`~unitree_robot_agent.fleet.manager.FleetManager`, which is the same
  core the MCP server uses.
"""

from .app import create_app
from .config import ConsoleConfig
from .service import ConsoleService
from .state import ConsoleState, EStop, EStopScope, InputMode, MotionBlocked

__all__ = [
    "ConsoleConfig",
    "ConsoleService",
    "ConsoleState",
    "EStop",
    "EStopScope",
    "InputMode",
    "MotionBlocked",
    "create_app",
]
