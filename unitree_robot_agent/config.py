"""Configuration for the agent layer.

Everything is environment-driven so the MCP server can be launched by a
client (CodeBuddy / Claude / Cursor …) with nothing but an ``env`` block.
A ``.env`` file is also honoured — looked up next to the current working
directory or at ``$UNITREE_AGENT_ENV_FILE`` — parsed by hand so the agent
package keeps a zero-dependency core.

Precedence: explicit constructor values > process env > .env file > defaults.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

DEFAULT_STATE_DIR = "~/.unitree-agent"

VALID_ROBOTS = ("R1", "Go2", "G1")
VALID_CONNECTION_METHODS = ("ap", "sta-l", "sta-t")
VALID_BACKENDS = ("auto", "webrtc", "mock")
VALID_STOP_MODES = ("soft", "damp", "none")
VALID_CHASSIS = ("air", "pro", "edu", "unknown")
VALID_REGIONS = ("global", "cn")
#: `auto` walks the region candidates and remembers the one that accepts the
#: account — an account registered in China answers "email not register" on
#: the global endpoint, which is easy to misread as a wrong password.
AUTO_REGION = "auto"

# Connection method aliases accepted on input.
_CONNECTION_ALIASES = {
    "ap": "ap",
    "localap": "ap",
    "local-ap": "ap",
    "sta-l": "sta-l",
    "stal": "sta-l",
    "localsta": "sta-l",
    "sta-t": "sta-t",
    "stat": "sta-t",
    "remote": "sta-t",
}


def _env(*names: str) -> Optional[str]:
    for name in names:
        raw = os.environ.get(name)
        if raw is not None and raw.strip() != "":
            return raw.strip()
    return None


def _env_bool(*names: str, default: bool) -> bool:
    raw = _env(*names)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes", "on", "y")


def _env_float(*names: str, default: float) -> float:
    raw = _env(*names)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int_list(*names: str) -> List[int]:
    """Comma-separated int list, e.g. ``1001,1002,1007``."""
    raw = _env(*names)
    if not raw:
        return [1001, 1002, 1003, 1004, 1005, 1006]
    values: List[int] = []
    for chunk in raw.replace(" ", "").split(","):
        if not chunk:
            continue
        try:
            values.append(int(chunk, 0))
        except ValueError:
            continue
    return values or [1001, 1002, 1003, 1004, 1005, 1006]


def load_dotenv(path: Optional[Path] = None, *, override: bool = False) -> Optional[Path]:
    """Minimal .env loader. Returns the path actually read, if any.

    Supports ``KEY=VALUE`` lines, ``#`` comments, blank lines, optional
    ``export`` prefixes and single/double quoted values. Deliberately does
    not support interpolation — nothing here needs it.
    """
    candidates = []
    if path is not None:
        candidates.append(Path(path))
    else:
        explicit = _env("UNITREE_AGENT_ENV_FILE")
        if explicit:
            candidates.append(Path(explicit).expanduser())
        candidates.append(Path.cwd() / ".env")
        candidates.append(Path(DEFAULT_STATE_DIR).expanduser() / ".env")

    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            for line in candidate.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[len("export "):]
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                if not key:
                    continue
                if override or key not in os.environ:
                    os.environ[key] = value
        except OSError:
            continue
        return candidate
    return None


@dataclass
class AgentConfig:
    """Resolved runtime configuration."""

    # ── robot identity ───────────────────────────────────────────────
    robot: str = "R1"
    chassis: str = "unknown"        # air | pro | edu | unknown
    connection: str = "sta-l"
    ip: Optional[str] = None
    serial_number: Optional[str] = None
    aes_128_key: Optional[str] = None
    device_type: Optional[str] = None  # defaults to `robot`
    region: str = "auto"               # auto | global | cn
    region_auto_fallback: bool = True  # try the other endpoint when "not register"
    username: Optional[str] = None
    password: Optional[str] = None
    access_token: Optional[str] = None  # skips the password login entirely

    #: Filled in by :class:`CloudAuth` once the working endpoint is known.
    resolved_region: Optional[str] = None

    # ── lifecycle / timing ───────────────────────────────────────────
    idle_disconnect_s: float = 45.0
    connect_timeout_s: float = 30.0
    request_timeout_s: float = 10.0
    state_read_timeout_s: float = 2.0
    min_command_interval_s: float = 0.1
    max_move_duration_s: float = 10.0

    # ── safety ───────────────────────────────────────────────────────
    safe_mode: bool = False          # read-only: only read tools + soft stop
    allow_dangerous: bool = True     # gate the dangerous action/state catalog
    stop_on_disconnect: str = "soft"  # soft | damp | none

    # ── backend / plumbing ───────────────────────────────────────────
    backend: str = "auto"            # auto | webrtc | mock
    state_dir: Path = field(default_factory=lambda: Path(DEFAULT_STATE_DIR).expanduser())
    audit_enabled: bool = True
    audit_log: Optional[Path] = None
    audit_to_stderr: bool = False
    log_level: str = "WARNING"

    # ── motion ───────────────────────────────────────────────────────
    #: Joystick-units per second squared used by the trapezoid profile.
    motion_accel: float = 1.5
    max_turn_speed: float = 0.4        # peak |rx| during a closed-loop turn
    turn_tolerance_deg: float = 4.0    # stop when the yaw error is this small
    max_turn_seconds: float = 20.0     # watchdog for a single turn

    # ── vision ───────────────────────────────────────────────────────
    #: auto | webrtc | photo_api | rtsp | device_camera | file
    video_source: str = "auto"
    rtsp_url: str = ""
    #: api_ids worth trying on `rt/api/videohub/request` (unverified upstream)
    photo_api_ids: List[int] = field(default_factory=lambda: [1001, 1002, 1003, 1004, 1005, 1006])
    camera_device: str = ""
    snapshot_file: str = ""
    snapshot_dir: Optional[Path] = None
    snapshot_max_width: int = 1280

    # ── server transport ─────────────────────────────────────────────
    transport: str = "stdio"         # stdio | streamable-http
    host: str = "127.0.0.1"
    port: int = 8765

    # ── filled in by __post_init__ ───────────────────────────────────
    env_file: Optional[Path] = None

    def __post_init__(self) -> None:
        self.robot = _normalise_robot(self.robot)
        self.connection = _normalise_connection(self.connection)
        self.region = (self.region or AUTO_REGION).lower().strip()
        if self.region not in (AUTO_REGION,) + VALID_REGIONS:
            self.region = AUTO_REGION
        if self.device_type is None:
            self.device_type = self.robot
        else:
            self.device_type = _normalise_robot(self.device_type)
        self.backend = self.backend.lower().strip()
        if self.backend not in VALID_BACKENDS:
            self.backend = "auto"
        if self.stop_on_disconnect not in VALID_STOP_MODES:
            self.stop_on_disconnect = "soft"
        self.chassis = (self.chassis or "unknown").lower().strip()
        if self.chassis not in VALID_CHASSIS:
            self.chassis = "unknown"
        self.state_dir = Path(self.state_dir).expanduser()
        if self.audit_log is None:
            self.audit_log = self.state_dir / "audit.log"
        else:
            self.audit_log = Path(self.audit_log).expanduser()
        self.idle_disconnect_s = max(0.0, float(self.idle_disconnect_s))
        self.max_move_duration_s = max(0.05, float(self.max_move_duration_s))
        self.min_command_interval_s = max(0.0, float(self.min_command_interval_s))
        if self.snapshot_dir is not None:
            self.snapshot_dir = Path(self.snapshot_dir).expanduser()
        self.video_source = (self.video_source or "auto").lower().strip()

    # ── derived helpers ─────────────────────────────────────────────

    @property
    def key_cache_path(self) -> Path:
        return self.state_dir / "keys.json"

    @property
    def is_remote(self) -> bool:
        return self.connection == "sta-t"

    @property
    def connection_region(self) -> str:
        """Region for the STA-T driver call — never ``auto``.

        ``CloudAuth`` fills in :attr:`resolved_region` once it has proved
        which endpoint accepts the account; before that we fall back to the
        configured literal (or global) so a remote connect still has a value.
        """
        if self.resolved_region in VALID_REGIONS:
            return self.resolved_region  # type: ignore[return-value]
        if self.region in VALID_REGIONS:
            return self.region
        return "global"

    @property
    def needs_aes_key(self) -> bool:
        """R1 always needs the per-device key; Go2 only on >= 1.1.15.

        We cannot know the Go2 firmware before connecting, so we prefer to
        have a key and let the handshake decide. R1/G1 (data2=3 always) are
        hard requirements."""
        return self.robot in ("R1", "G1") or bool(self.aes_128_key)

    def target_label(self) -> str:
        """Stable, log-safe identifier for the configured robot."""
        return self.serial_number or self.ip or "unconfigured"

    def require_target(self) -> None:
        from .errors import RobotNotConfigured

        if self.connection == "ap":
            return
        if self.connection == "sta-l" and not (self.ip or self.serial_number):
            raise RobotNotConfigured(
                "STA-L needs an IP or a serial number "
                "(UNITREE_ROBOT_IP / UNITREE_ROBOT_SN)."
            )
        if self.connection == "sta-t":
            missing = [
                name
                for name, value in (
                    ("UNITREE_ROBOT_SN", self.serial_number),
                    ("UNITREE_AGENT_EMAIL", self.username),
                    ("UNITREE_AGENT_PASSWORD", self.password),
                )
                if not value
            ]
            if missing:
                raise RobotNotConfigured(
                    "Remote (STA-T) needs " + ", ".join(missing) + "."
                )

    # ── key cache ────────────────────────────────────────────────────

    def _cache_key(self) -> str:
        return f"{self.device_type}:{self.target_label()}"

    def load_cached_aes_key(self) -> Optional[str]:
        """Return a cached key for this device.

        Lookup order: exact ``device_type:sn_or_ip`` → the only entry for this
        device family (so a key fetched once still works on a later run that
        only sets the IP, or neither) → miss. The family fallback is what
        makes "fetch the key once, then forget about credentials" work.
        """
        try:
            payload = json.loads(self.key_cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None

        entry = payload.get(self._cache_key())
        if isinstance(entry, str) and entry:
            return entry

        prefix = f"{self.device_type}:"
        family = [
            value
            for key, value in payload.items()
            if isinstance(key, str) and key.startswith(prefix) and isinstance(value, str) and value
        ]
        if len(family) == 1:
            return family[0]
        return None

    def store_cached_aes_key(self, key: str) -> None:
        """Persist a key for this device. Best-effort; never raises."""
        if not key:
            return
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            try:
                payload: Dict[str, str] = json.loads(
                    self.key_cache_path.read_text(encoding="utf-8")
                )
                if not isinstance(payload, dict):
                    payload = {}
            except (OSError, ValueError):
                payload = {}
            payload[self._cache_key()] = key
            tmp = self.key_cache_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
            os.chmod(tmp, 0o600)
            tmp.replace(self.key_cache_path)
        except OSError:
            pass

    def resolve_aes_key(self, *, persist: bool = True) -> Optional[str]:
        """Return the AES key from (in order) config, env, cache, cloud.

        The cloud lookup only runs when an account is configured; it is the
        same ``device/bind/list`` call the bundled CLI performs."""
        if self.aes_128_key:
            return self.aes_128_key
        cached = self.load_cached_aes_key()
        if cached:
            self.aes_128_key = cached
            return cached
        if not (self.username and self.password and self.serial_number):
            return None
        try:
            from unitree_webrtc_connect import fetch_aes_key
        except Exception:
            return None
        try:
            key = fetch_aes_key(
                self.username,
                self.password,
                sn=self.serial_number,
                region=self.region,
                device_type=self.device_type,
            )
        except Exception:
            return None
        if key and persist:
            self.store_cached_aes_key(key)
        self.aes_128_key = key
        return key

    def redacted(self) -> dict:
        """Config view safe for logs and ``doctor`` output."""
        return {
            "robot": self.robot,
            "device_type": self.device_type,
            "chassis": self.chassis,
            "connection": self.connection,
            "ip": self.ip,
            "serial_number": self.serial_number,
            "aes_128_key": _mask(self.aes_128_key),
            "region": self.region,
            "region_resolved": self.resolved_region,
            "region_auto_fallback": self.region_auto_fallback,
            "username": _mask(self.username, keep=3),
            "password": "***" if self.password else None,
            "access_token": _mask(self.access_token, keep=3),
            "idle_disconnect_s": self.idle_disconnect_s,
            "request_timeout_s": self.request_timeout_s,
            "min_command_interval_s": self.min_command_interval_s,
            "max_move_duration_s": self.max_move_duration_s,
            "safe_mode": self.safe_mode,
            "allow_dangerous": self.allow_dangerous,
            "stop_on_disconnect": self.stop_on_disconnect,
            "backend": self.backend,
            "state_dir": str(self.state_dir),
            "audit_log": str(self.audit_log) if self.audit_log else None,
            "transport": self.transport,
            "vision": {
                "video_source": self.video_source,
                "rtsp_url": self.rtsp_url or None,
                "photo_api_ids": self.photo_api_ids,
                "camera_device": self.camera_device or None,
                "snapshot_file": self.snapshot_file or None,
                "snapshot_dir": str(self.snapshot_dir or (self.state_dir / "snapshots")),
                "snapshot_max_width": self.snapshot_max_width,
            },
        }


def _mask(value: Optional[str], keep: int = 4) -> Optional[str]:
    if not value:
        return None
    if len(value) <= keep:
        return "***"
    return f"{value[:keep]}…***"


def _normalise_robot(value: str) -> str:
    for candidate in VALID_ROBOTS:
        if value and value.lower() == candidate.lower():
            return candidate
    return "R1"


def _normalise_connection(value: str) -> str:
    key = (value or "").lower().strip()
    return _CONNECTION_ALIASES.get(key, "sta-l")


def config_from_env(**overrides) -> AgentConfig:
    """Build an :class:`AgentConfig` from the environment (+ optional .env).

    ``overrides`` take precedence and are passed straight to the dataclass,
    which is handy in tests and in the CLI where ``--flag`` should win."""
    load_dotenv()

    audit_log_raw = (_env("UNITREE_AGENT_AUDIT_LOG") or "default").lower()

    values = dict(
        robot=_env("UNITREE_AGENT_ROBOT", "UNITREE_ROBOT_TYPE") or "R1",
        chassis=(_env("UNITREE_AGENT_CHASSIS") or "unknown").lower(),
        connection=_env("UNITREE_AGENT_CONNECTION") or "sta-l",
        ip=_env("UNITREE_ROBOT_IP", "UNITREE_AGENT_IP", "ROBOT_IP"),
        serial_number=_env("UNITREE_ROBOT_SN", "UNITREE_AGENT_SN", "UNITREE_SN"),
        aes_128_key=_env("UNITREE_AES_128_KEY", "UNITREE_AGENT_AES_128_KEY"),
        device_type=_env("UNITREE_AGENT_DEVICE_TYPE"),
        region=_env("UNITREE_AGENT_REGION", "UNITREE_REGION") or AUTO_REGION,
        region_auto_fallback=_env_bool("UNITREE_AGENT_REGION_AUTO_FALLBACK", default=True),
        username=_env("UNITREE_AGENT_EMAIL", "UNITREE_EMAIL"),
        password=_env("UNITREE_AGENT_PASSWORD", "UNITREE_PASSWORD"),
        access_token=_env("UNITREE_AGENT_ACCESS_TOKEN", "UNITREE_ACCESS_TOKEN"),
        idle_disconnect_s=_env_float(
            "UNITREE_AGENT_IDLE_DISCONNECT_S", "UNITREE_AGENT_IDLE_TIMEOUT_S", default=45.0
        ),
        connect_timeout_s=_env_float("UNITREE_AGENT_CONNECT_TIMEOUT_S", default=30.0),
        request_timeout_s=_env_float("UNITREE_AGENT_REQUEST_TIMEOUT_S", default=10.0),
        state_read_timeout_s=_env_float("UNITREE_AGENT_STATE_READ_TIMEOUT_S", default=2.0),
        min_command_interval_s=_env_float("UNITREE_AGENT_MIN_COMMAND_INTERVAL_S", default=0.1),
        max_move_duration_s=_env_float("UNITREE_AGENT_MAX_MOVE_S", default=10.0),
        safe_mode=_env_bool("UNITREE_AGENT_SAFE_MODE", default=False),
        allow_dangerous=_env_bool("UNITREE_AGENT_ALLOW_DANGEROUS", "UNITREE_AGENT_ALLOW_CONTROL", default=True),
        stop_on_disconnect=(_env("UNITREE_AGENT_STOP_ON_DISCONNECT") or "soft").lower(),
        backend=(_env("UNITREE_AGENT_BACKEND") or "auto").lower(),
        state_dir=Path(_env("UNITREE_AGENT_STATE_DIR") or DEFAULT_STATE_DIR).expanduser(),
        audit_enabled=audit_log_raw != "off",
        audit_log=(
            None
            if audit_log_raw in ("", "default", "off")
            else Path(audit_log_raw).expanduser()
        ),
        audit_to_stderr=_env_bool("UNITREE_AGENT_AUDIT_TO_STDERR", default=False),
        motion_accel=_env_float("UNITREE_AGENT_MOTION_ACCEL", default=1.5),
        max_turn_speed=_env_float("UNITREE_AGENT_MAX_TURN_SPEED", default=0.4),
        turn_tolerance_deg=_env_float("UNITREE_AGENT_TURN_TOLERANCE_DEG", default=4.0),
        max_turn_seconds=_env_float("UNITREE_AGENT_MAX_TURN_SECONDS", default=20.0),
        video_source=_env("UNITREE_AGENT_VIDEO_SOURCE") or "auto",
        rtsp_url=_env("UNITREE_AGENT_RTSP_URL") or "",
        photo_api_ids=_env_int_list("UNITREE_AGENT_PHOTO_API_IDS"),
        camera_device=_env("UNITREE_AGENT_CAMERA") or "",
        snapshot_file=_env("UNITREE_AGENT_SNAPSHOT_FILE") or "",
        snapshot_dir=(
            Path(_env("UNITREE_AGENT_SNAPSHOT_DIR")).expanduser()
            if _env("UNITREE_AGENT_SNAPSHOT_DIR")
            else None
        ),
        snapshot_max_width=int(_env_float("UNITREE_AGENT_SNAPSHOT_MAX_WIDTH", default=1280)),
        log_level=(_env("UNITREE_AGENT_LOG_LEVEL") or "WARNING").upper(),
        transport=(_env("UNITREE_AGENT_TRANSPORT") or "stdio").lower(),
        host=_env("UNITREE_AGENT_HOST") or "127.0.0.1",
        port=int(_env_float("UNITREE_AGENT_PORT", default=8765)),
    )
    values.update({k: v for k, v in overrides.items() if v is not None})
    return AgentConfig(**values)
