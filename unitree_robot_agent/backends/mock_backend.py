"""Offline robot simulator.

Reproduces the *awkward* parts of the real robots on purpose, because those
are the parts the guard layer and the tool surface have to cope with:

* R1 Air redirects ``Run`` (811) to ``Loco20Dof`` (830) — a request that
  succeeds while the read-back disagrees.
* Several R1 states stop publishing ``sportmodestate`` entirely, so the
  read-back **freezes** at the last reported value.
* Transitions that are not on the robot's white list answer ``1001``; ids
  the firmware doesn't build answer ``1003``.
* Some api_ids exist as constants but have no handler, so the request is
  answered by *silence* — the backend reports ``timed_out`` rather than a
  refusal, which is exactly the distinction the session layer needs.
* A second WebRTC client is refused (``RobotBusyError``).

If a tool works against this backend, it will behave sanely against the
real one. It powers ``unitree-robot-mcp selftest`` and the test suite.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import struct
import time
import zlib
from typing import Any, Dict, Optional

from ..errors import RobotBusy
from .base import ApiResponse

# Topic keys (literal strings, so the mock does not need the upstream
# package to be importable in order to run offline tests).
T_SPORT_MOD = "rt/api/sport/request"
T_ARM_REQUEST = "rt/api/arm/request"
T_MOTION_SWITCHER = "rt/api/motion_switcher/request"
T_VUI = "rt/api/vui/request"
T_OBSTACLES = "rt/api/obstacles_avoid/request"
T_WIRELESS = "rt/wirelesscontroller"
T_LOWSTATE = "rt/lf/lowstate"
T_BMS = "rt/lf/bmsstate"
T_SECONDARY_IMU = "rt/lf/secondary_imu"
T_MAINBOARD = "rt/lf/mainboardstate"
T_R1_STATE = "rt/lf/sportmodestate"
T_GO2_STATE = "rt/sportmodestate"
T_MULTIPLE_STATE = "rt/multiplestate"
T_ULIDAR_SWITCH = "rt/utlidar/switch"
T_ULIDAR_MAP = "rt/utlidar/voxel_map_compressed"
T_VIDEOHUB = "rt/api/videohub/request"

#: The api_id the mock pretends serves still photos. Upstream documents no
#: value for this topic, so the number is arbitrary — what matters is that
#: only one id answers, which is what makes the probe meaningful.
PHOTO_API_ID = 1001

# R1 firmware facts (mirrors constants.py; duplicated so the mock stays
# self-contained for offline use).
R1_DAMPING = 1
R1_ZERO_TORQUE = 0
R1_LOCK = 4
R1_RUN = 811
R1_LOCO20 = 830
R1_FROZEN_REPORT = frozenset({5, 6, 7, 800, 804, 805, 813, 814})
R1_SERVED_API = frozenset({7001, 7101, 7105, 7108})

#: Minimal 1×1 PNG, built in code so no binary blob lives in the source.
def _tiny_png() -> bytes:
    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw = b"\x00\x2f\x6f\xc8"  # filter byte + one RGB pixel
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


class MockBackend:
    """In-process simulation of a Unitree robot."""

    name = "mock"

    def __init__(
        self,
        robot_name: str = "R1",
        *,
        air: bool = True,
        latency: float = 0.01,
        speed_limit: float = 1.0,
        camera: bool = True,
        strict_air: bool = False,
    ) -> None:
        self.robot_name = robot_name
        self.air = air
        self.latency = max(0.0, latency)
        self.speed_limit = speed_limit
        #: Simulates the model difference we hit on hardware: the R1 Air we
        #: tested has no camera at all (no WebRTC track, no RTSP mount, no
        #: still-photo reply). Set False to exercise that path.
        self.has_camera = camera
        #: Air firmware serves fewer dances than the shared table lists.
        self.strict_air = strict_air

        self._connected = False
        self._busy = False
        self._closed = False
        self._cache: Dict[str, dict] = {}
        self._subscribed: set = set()

        # R1 state machine
        self.fsm_id = R1_DAMPING
        self.reported_fsm_id: Optional[int] = None
        self.velocity = [0.0, 0.0, 0.0]
        self.moving_until = 0.0

        # Integrate yaw from the commanded omega so the closed-loop turn can
        # be developed and tested with no robot. Yaw grows counter-clockwise
        # (positive omega), matching the real IMU convention.
        self.yaw = 0.0
        self._last_integration = time.time()

        # Go2 state
        self.go2_posture = "damping"
        self.motion_mode = "normal"
        self.volume = 5
        self.brightness = 5
        self.led_color = "white"
        self.avoid_enabled = False
        self.lidar_on = False

        # diagnostics
        self.calls: list = []
        self._errors: list = []

    def error_events(self) -> list:
        if self._errors:
            return list(self._errors)
        # A representative (already-cleared) event so the tool has something
        # to render offline. Real robots usually report nothing at all.
        return [
            {
                "type": "rm_error",
                "data": [int(time.time()), 300, 1],
                "at": time.time(),
                "mock": True,
            }
        ]

    # ── fault injection ──────────────────────────────────────────────

    def set_busy(self, busy: bool) -> None:
        """Simulate another WebRTC client holding the robot."""
        self._busy = busy

    # ── lifecycle ────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        if self._closed:
            raise RuntimeError("mock backend closed")
        await asyncio.sleep(self.latency)
        if self._busy:
            raise RobotBusy("mock: robot already has a WebRTC client")
        self._connected = True
        self.reported_fsm_id = self.fsm_id
        self._seed_states()

    async def disconnect(self) -> None:
        self._connected = False
        self._subscribed.clear()

    async def aclose(self) -> None:
        self._closed = True
        self._connected = False

    def _seed_states(self) -> None:
        now = time.time()
        self._cache[T_R1_STATE] = self._r1_state_payload()
        self._cache[T_GO2_STATE] = {
            "mode": 0,
            "gait_type": "trot",
            "body_height": 0.3,
            "foot_raise_height": 0.05,
            "progress": 1.0,
            "velocity": list(self.velocity),
            "position": [0.0, 0.0, 0.0],
        }
        self._cache[T_MULTIPLE_STATE] = self._multi_state_payload()
        self._cache[T_LOWSTATE] = self._lowstate_payload()
        self._cache[T_BMS] = self._bms_payload()
        self._cache[T_SECONDARY_IMU] = {
            "rpy": [0.0, 0.0, 0.0],
            "temperature": 34,
        }
        self._cache[T_MAINBOARD] = {"temperature": [33, 34, 35]}
        self._cache[T_WIRELESS] = {"lx": 0.0, "ly": 0.0, "rx": 0.0, "ry": 0.0, "keys": 0}
        del now

    # ── request handling ─────────────────────────────────────────────

    async def api_request(
        self,
        topic: str,
        api_id: int,
        parameter: Any = None,
        *,
        timeout: Optional[float] = None,
    ) -> ApiResponse:
        if not self._connected:
            return ApiResponse(code=-1, timed_out=True, message="mock backend not connected")

        self.calls.append({"t": time.time(), "topic": topic, "api_id": api_id, "parameter": parameter})

        # Model "no reply within the window" the same way the real link does:
        # a timeout is silence, NOT a refusal. Tests rely on this being
        # distinguishable, and so does the tool layer's messaging.
        if timeout is not None and self.latency > timeout:
            await asyncio.sleep(timeout)
            return ApiResponse(
                code=-1, timed_out=True, message=f"mock: no reply within {timeout:g}s"
            )
        await asyncio.sleep(self.latency)

        params = self._as_dict(parameter)

        if topic == T_SPORT_MOD:
            if self.robot_name == "Go2":
                return self._go2_sport(api_id, params)
            return self._r1_sport(api_id, params)
        if topic == T_ARM_REQUEST:
            return self._r1_arm(api_id, params)
        if topic == T_MOTION_SWITCHER:
            return self._go2_motion_switcher(api_id, params)
        if topic == T_VUI:
            return self._go2_vui(api_id, params)
        if topic == T_OBSTACLES:
            return self._go2_avoid(api_id, params)
        if topic == T_VIDEOHUB:
            return self._videohub(api_id, params)

        return ApiResponse(code=0, data="", message="mock: accepted")

    def _videohub(self, api_id: int, params: dict) -> ApiResponse:
        """Still-photo request. Mirrors what a camera-equipped robot should do:
        one api_id serves an inline base64 JPEG, the rest answer 127."""
        if not self.has_camera:
            return ApiResponse(code=127, message="mock: no camera on this model")
        if api_id != PHOTO_API_ID:
            return ApiResponse(code=127, message=f"mock: api_id {api_id} has no handler")
        frame = self._jpeg_frame(320, 240)
        if not frame:
            return ApiResponse(code=-1, timed_out=True, message="mock: encoder unavailable")
        encoded = base64.b64encode(frame).decode("ascii")
        return self._ok(json.dumps({"image": f"data:image/jpeg;base64,{encoded}"}))

    def publish(self, topic: str, data: Any = None, msg_type: Optional[str] = None) -> bool:
        if not self._connected:
            return False
        if topic == T_WIRELESS and isinstance(data, dict):
            # Mimic the on-robot mapping [ly, -lx, -rx] -> [vx, vy, omega],
            # scaled by the robot's own limit.
            self.velocity = [
                float(data.get("ly", 0.0)) * self.speed_limit,
                -float(data.get("lx", 0.0)) * self.speed_limit,
                -float(data.get("rx", 0.0)) * self.speed_limit,
            ]
            self._cache[T_WIRELESS] = dict(data)
            if any(abs(v) > 0.001 for v in self.velocity):
                self.moving_until = time.time() + 0.5
        elif topic == T_ULIDAR_SWITCH:
            self.lidar_on = str(data).strip().strip('"').lower() == "on"
        # Fire-and-forget: anything else is accepted silently.
        return True

    async def latest(
        self,
        topic: str,
        *,
        subscribe: bool = True,
        timeout: Optional[float] = None,
        max_age: Optional[float] = None,
    ) -> Optional[dict]:
        if subscribe:
            self._subscribed.add(topic)
        if self.robot_name == "R1" and topic == T_R1_STATE:
            self._cache[topic] = self._r1_state_payload()
        if topic == T_LOWSTATE:
            # Regenerate on read so the integrated yaw is always current —
            # that is what makes the closed-loop turn testable offline.
            self._cache[topic] = self._lowstate_payload()
        if topic == T_ULIDAR_MAP:
            if not self.lidar_on:
                return None
            return self._lidar_payload()
        if self.latency and timeout:
            await asyncio.sleep(min(self.latency, timeout))
        return self._cache.get(topic)

    async def snapshot_frame(self, *, timeout: float = 5.0) -> Optional[bytes]:
        await asyncio.sleep(self.latency)
        if not self.has_camera:
            # Matches the R1 Air: the video channel opens but no track ever
            # arrives, so the caller sees a timeout rather than an error.
            await asyncio.sleep(min(timeout, 1.0))
            return None
        return self._jpeg_frame()

    @staticmethod
    def _jpeg_frame(width: int = 640, height: int = 480) -> Optional[bytes]:
        """A real JPEG, produced through the same encoder the robot uses.

        Returning a PNG here would hide encoder bugs — and indeed the first
        version of the encoder was broken and only showed up once this path
        was exercised.
        """
        try:
            from ..vision.encode import encode_frame, make_test_frame

            payload, _, _ = encode_frame(make_test_frame(width, height, shade=0x55))
            return payload
        except Exception:
            return _tiny_png()

    def extra_status(self) -> dict:
        out = {
            "mock": True,
            "mock_air": self.air,
            "simulated_velocity": list(self.velocity),
        }
        if self.robot_name == "Go2":
            out.update(
                {
                    "motion_mode": self.motion_mode,
                    "volume": self.volume,
                    "brightness": self.brightness,
                    "led_color": self.led_color,
                    "obstacle_avoid": self.avoid_enabled,
                    "lidar_on": self.lidar_on,
                    "posture": self.go2_posture,
                }
            )
        return out

    # ── R1 simulation ────────────────────────────────────────────────

    def _r1_sport(self, api_id: int, params: dict) -> ApiResponse:
        if api_id not in R1_SERVED_API:
            # Firmware has the constant but no handler: no reply at all.
            return ApiResponse(
                code=-1,
                timed_out=True,
                message=f"mock: api_id {api_id} has no handler on R1 (no reply)",
            )

        if api_id == 7001:  # GET_FSM_ID
            return self._ok(json.dumps({"fsm_id": self.fsm_id}))

        if api_id == 7105:  # SET_VELOCITY
            if self.fsm_id == R1_LOCO20:
                # Verified on a real R1 Air: Loco20Dof (830) has no speed
                # handler and answers 127 to SET_VELOCITY. The tool layer
                # falls back to the joystick path on this code.
                return ApiResponse(
                    code=127,
                    message="mock: no speed handler in Loco20Dof (830)",
                )
            velocity = params.get("velocity")
            if not isinstance(velocity, (list, tuple)) or len(velocity) != 3:
                return ApiResponse(code=1001, message="mock: velocity must be [vx, vy, omega]")
            duration = float(params.get("duration", 1.0) or 1.0)
            self.velocity = [float(v) for v in velocity]
            self.moving_until = time.time() + duration
            return self._ok("")

        if api_id == 7108:  # SET_MOTION — parameter is an array
            return self._ok("")

        # api_id == 7101 SET_FSM_ID
        target = params.get("data")
        if not isinstance(target, int):
            return ApiResponse(code=1001, message="mock: SET_FSM_ID needs {'data': <int>}")

        known = _R1_KNOWN_STATES
        if target not in known:
            return ApiResponse(
                code=1003,
                message=f"mock: fsm_id {target} does not exist on this firmware",
            )

        current = self.fsm_id
        if target == current:
            return self._ok("")

        if self.strict_air and target in (601, 603, 608):
            return ApiResponse(code=1001, message="mock: not in dances_air")

        if target == R1_DAMPING:
            self._enter_fsm(target)
            return self._ok("")

        if target == R1_ZERO_TORQUE and current != R1_DAMPING:
            return ApiResponse(
                code=1001,
                message="mock: ZeroTorque is only reachable from Damping",
            )

        if current not in (R1_DAMPING, R1_LOCK):
            return ApiResponse(
                code=1001,
                message=f"mock: state {current} refuses the transition",
            )

        actual = target
        if target == R1_RUN and self.air:
            actual = R1_LOCO20  # firmware redirect on the Air chassis
        self._enter_fsm(actual)
        return self._ok("")

    def _enter_fsm(self, fsm_id: int) -> None:
        self.fsm_id = fsm_id
        if fsm_id not in R1_FROZEN_REPORT:
            self.reported_fsm_id = fsm_id
        # In frozen states the robot keeps publishing the last known value,
        # so `reported_fsm_id` intentionally stays put.
        self._cache[T_R1_STATE] = self._r1_state_payload()

    def _r1_state_payload(self) -> dict:
        reported = self.reported_fsm_id if self.reported_fsm_id is not None else self.fsm_id
        return {
            "fsm_id": reported,
            "mode": self.fsm_id,
            "progress": 1.0,
            "velocity": list(self.velocity),
            "imu_state": {"rpy": [0.0, 0.0, 0.0], "temperature": 36},
            "body_height": 0.7,
        }

    def _r1_arm(self, api_id: int, params: dict) -> ApiResponse:
        if api_id != 7106:
            return ApiResponse(code=-1, timed_out=True, message="mock: no handler")
        action = params.get("data")
        if not isinstance(action, int) or action not in _ARM_ACTION_IDS:
            return ApiResponse(code=1001, message=f"mock: unknown arm action {action!r}")
        return self._ok("")

    # ── Go2 simulation ───────────────────────────────────────────────

    def _go2_sport(self, api_id: int, params: dict) -> ApiResponse:
        if api_id == 1001:  # Damp
            self.go2_posture = "damping"
            self.velocity = [0.0, 0.0, 0.0]
        elif api_id == 1002:  # BalanceStand
            self.go2_posture = "standing"
        elif api_id == 1003:  # StopMove
            self.velocity = [0.0, 0.0, 0.0]
        elif api_id == 1004:  # StandUp
            self.go2_posture = "standing"
        elif api_id == 1005:  # StandDown
            self.go2_posture = "lying"
        elif api_id == 1006:  # RecoveryStand
            self.go2_posture = "standing"
        elif api_id == 1009:  # Sit
            self.go2_posture = "sitting"
        elif api_id == 1008:  # Move
            self.velocity = [
                float(params.get("x", 0.0) or 0.0),
                float(params.get("y", 0.0) or 0.0),
                float(params.get("z", 0.0) or 0.0),
            ]
            duration = abs(float(params.get("duration", 1.0) or 1.0))
            self.moving_until = time.time() + max(0.2, duration)
            if self.go2_posture not in ("standing", "moving"):
                return ApiResponse(
                    code=3201,
                    message="mock: robot is not standing; BalanceStand first",
                )
            self.go2_posture = "moving"
        elif api_id == 1034:  # GetState
            return self._ok(
                json.dumps(
                    {
                        "state": self.go2_posture,
                        "bodyHeight": 0.3,
                        "speedLevel": 1,
                        "gait": "trot",
                        "continuousGait": False,
                        "economicGait": False,
                    }
                )
            )
        return self._ok("")

    def _go2_motion_switcher(self, api_id: int, params: dict) -> ApiResponse:
        if api_id == 1001:
            return self._ok(json.dumps({"name": self.motion_mode}))
        if api_id == 1002:
            name = str(params.get("name", "")).lower()
            if name not in ("normal", "ai", "mcf", "advanced"):
                return ApiResponse(code=3001, message=f"mock: unknown motion mode {name!r}")
            self.motion_mode = name
            if name == "normal":
                self.go2_posture = "standing"
            return self._ok("")
        return ApiResponse(code=3001, message="mock: unsupported motion switcher api_id")

    def _go2_vui(self, api_id: int, params: dict) -> ApiResponse:
        if api_id == 1003:
            self.volume = max(0, min(10, int(params.get("volume", self.volume))))
            self._cache[T_MULTIPLE_STATE] = self._multi_state_payload()
            return self._ok("")
        if api_id == 1004:
            return self._ok(json.dumps({"volume": self.volume}))
        if api_id == 1005:
            self.brightness = max(0, min(10, int(params.get("brightness", self.brightness))))
            self._cache[T_MULTIPLE_STATE] = self._multi_state_payload()
            return self._ok("")
        if api_id == 1006:
            return self._ok(json.dumps({"brightness": self.brightness}))
        if api_id == 1007:
            self.led_color = str(params.get("color", self.led_color))
            return self._ok("")
        return ApiResponse(code=1001, message="mock: unsupported VUI api_id")

    def _go2_avoid(self, api_id: int, params: dict) -> ApiResponse:
        if api_id == 1001:
            self.avoid_enabled = bool(params.get("enable", False))
            self._cache[T_MULTIPLE_STATE] = self._multi_state_payload()
            return self._ok("")
        if api_id == 1002:
            return self._ok(json.dumps({"enable": self.avoid_enabled}))
        return ApiResponse(code=1001, message="mock: unsupported obstacle-avoid api_id")

    # ── synthetic telemetry ──────────────────────────────────────────

    def _integrate_yaw(self) -> None:
        """Advance yaw from the commanded omega.

        Lazy integration (done on read rather than on a timer) keeps the mock
        free of background tasks while still giving the closed-loop turn a
        realistic response to the sticks and to SET_VELOCITY."""
        now = time.time()
        dt = now - self._last_integration
        self._last_integration = now
        if dt <= 0:
            return
        if now <= self.moving_until:
            self.yaw += float(self.velocity[2]) * min(dt, 0.5)
        self.yaw = (self.yaw + math.pi) % (2 * math.pi) - math.pi

    def _lowstate_payload(self) -> dict:
        self._integrate_yaw()
        if self.robot_name == "Go2":
            return {
                "imu_state": {"rpy": [0.01, -0.02, round(self.yaw, 5)], "temperature": 41},
                "motor_state": [
                    {"q": 0.1 * i, "dq": 0.0, "tau_est": 0.0, "temperature": 38 + i, "lost": 0}
                    for i in range(12)
                ],
                "bms_state": {"soc": 87, "current": -1200, "cycle": 42, "bq_ntc": 30, "mcu_ntc": 32},
                "foot_force": [12, 13, 11, 14],
                "temperature_ntc1": 39,
                "power_v": 25.4,
            }
        return {
            "motor_state": [
                {
                    "q": 0.05 * (i % 6),
                    "dq": 0.0,
                    "tau_est": 0.3,
                    "temperature": [34 + (i % 5), 40 + (i % 6)],
                    "lost": 0,
                }
                for i in range(29)
            ],
            "imu_state": {"rpy": [0.0, 0.0, round(self.yaw, 5)], "temperature": 36},
            "power_v": 24.8,
            "temperature_ntc1": 35,
        }

    def _bms_payload(self) -> dict:
        return {
            "soc": 87,
            "current": -1200,
            "cycle": 42,
            "bmsvoltage": [24800, 24760, 0],
            "pack_voltage": 24800,
            "bat_voltage": 24760,
            "cell_vol": [4130, 4128, 4132, 4129] + [0] * 36,
            "temperature": [30, 0, 29, 0],
        }

    def _multi_state_payload(self) -> dict:
        # The real topic carries this as a JSON *string*; the mock mirrors
        # that shape so the tools must handle it (and they do).
        return {
            "bodyHeight": 0.3,
            "brightness": self.brightness,
            "footRaiseHeight": 0.05,
            "obstaclesAvoidSwitch": self.avoid_enabled,
            "speedLevel": 1,
            "uwbSwitch": False,
            "volume": self.volume,
        }

    def _lidar_payload(self) -> dict:
        return {
            "stamp": time.time(),
            "frame_id": "utlidar_lidar",
            "resolution": 0.05,
            "src_size": 0,
            "origin": [0.0, 0.0, 0.0],
            "width": 0,
            "data": {"point_count": 0, "points": [], "mock": True},
        }

    # ── helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _as_dict(parameter: Any) -> dict:
        if parameter is None:
            return {}
        if isinstance(parameter, dict):
            return parameter
        if isinstance(parameter, str):
            try:
                parsed = json.loads(parameter)
            except ValueError:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}

    @staticmethod
    def _ok(data: Any) -> ApiResponse:
        return ApiResponse(code=0, data=data)


# Tables used by the simulator. Kept here (not imported from the upstream
# package) so the mock can run in an environment where only the agent
# package is installed.
_R1_KNOWN_STATES = frozenset(
    {0, 1, 4, 5, 6, 7, 601, 602, 603, 604, 607, 608, 701, 702, 800, 811,
     812, 813, 814, 815, 816, 830, 831}
)
_ARM_ACTION_IDS = frozenset({99, 12, 15, 17, 18, 19, 22, 23, 24, 25, 26, 27, 36})
