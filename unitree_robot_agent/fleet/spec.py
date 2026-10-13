"""One robot's identity, as declared by the operator.

The single-robot agent identifies a robot by its *model family* — ``"Go2"``
is all that ends up in the audit trail and in tool results. That is exactly
enough for one robot and useless for two: two Go2s are indistinguishable
everywhere it matters (audit, key cache, snapshot archive, tool routing).

So the fleet layer introduces ``robot_id``: an operator-chosen, stable name
(``"dog1"``, ``"scout"``) that is the *address* of a robot in every
interface. The model family stays what it always was — a capability selector
— and stops doubling as identity.

A spec is immutable on purpose. ``AgentConfig`` is mutated at runtime
(``resolve_aes_key`` writes ``self.aes_128_key``, region auto-detection
writes ``self.resolved_region``), which is fine for one robot and actively
dangerous when one config object is shared across a fleet. The spec is the
declared input; the config is the mutable per-robot state derived from it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional

from ..errors import FleetConfigError

#: Model families the driver can actually talk to.
VALID_MODELS = ("Go2", "G1", "R1")

#: Connection methods, matching ``AgentConfig``.
VALID_CONNECTIONS = ("ap", "sta-l", "sta-t")

VALID_REGIONS = ("global", "cn", "auto")

#: Chassis is a capability hint, not a separate protocol.
VALID_CHASSIS = ("air", "pro", "edu", "unknown")

#: Connection-method spellings an operator might reasonably type.
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


def _clean(value: Any) -> str:
    return str(value).strip() if value is not None else ""


@dataclass(frozen=True)
class RobotSpec:
    """The declared identity and address of one robot."""

    robot_id: str
    model: str = "Go2"
    connection: str = "sta-l"
    ip: str = ""
    serial_number: str = ""
    aes_128_key: str = ""
    device_type: str = ""
    region: str = "global"
    chassis: str = "unknown"
    #: Free-form operator label, e.g. ``"lead"`` / ``"wing"``. The fleet layer
    #: never branches on it; it exists so the console and the audit trail can
    #: say something more useful than "dog2".
    role: str = ""
    #: Extra per-robot overrides applied on top of the roster defaults.
    options: Mapping[str, Any] = field(default_factory=dict)

    # ── derived ──────────────────────────────────────────────────────

    @property
    def target(self) -> str:
        """The most stable address we have: serial number, else IP.

        Log-safe. Never use this as identity — it changes when DHCP does.
        """
        return self.serial_number or self.ip

    @property
    def resolved_device_type(self) -> str:
        """``device_type`` is the cloud/multicast family; it defaults to the model."""
        return self.device_type or self.model

    @property
    def has_key(self) -> bool:
        return bool(self.aes_128_key)

    @property
    def is_remote(self) -> bool:
        return self.connection == "sta-t"

    def redacted(self) -> Dict[str, Any]:
        """Spec view safe for logs, tool results and audit records."""
        return {
            "robot_id": self.robot_id,
            "model": self.model,
            "connection": self.connection,
            "target": self.target,
            "has_key": self.has_key,
            "region": self.region,
            "role": self.role,
        }

    # ── construction ─────────────────────────────────────────────────

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, Any], *, index: int = 0
    ) -> "RobotSpec":
        """Build and validate one spec from parsed JSON/env.

        ``index`` only feeds error messages, so a broken roster can say
        "robots[1]" rather than "some robot".
        """
        where = f"robots[{index}]"
        if not isinstance(raw, Mapping):
            raise FleetConfigError(
                f"{where} must be an object, got {type(raw).__name__}.",
                detail={"index": index},
            )

        robot_id = _clean(raw.get("id") or raw.get("robot_id"))
        if not robot_id:
            raise FleetConfigError(
                f"{where} has no `id`. Every robot needs a stable name; it is "
                f"how tools and the console address it.",
                detail={"index": index},
            )

        model = _clean(raw.get("model") or raw.get("robot")) or "Go2"
        # Accept the family in either case; the driver wants exact casing.
        model = {m.lower(): m for m in VALID_MODELS}.get(model.lower(), "")
        if not model:
            raise FleetConfigError(
                f"{where} (`{robot_id}`) has model {raw.get('model')!r}; "
                f"expected one of {', '.join(VALID_MODELS)}.",
                detail={"robot_id": robot_id},
            )

        connection = _clean(raw.get("connection")) or "sta-l"
        connection = _CONNECTION_ALIASES.get(connection.lower().replace("_", "-"), "")
        if not connection:
            raise FleetConfigError(
                f"{where} (`{robot_id}`) has connection "
                f"{raw.get('connection')!r}; expected one of "
                f"{', '.join(VALID_CONNECTIONS)}.",
                detail={"robot_id": robot_id},
            )

        ip = _clean(raw.get("ip"))
        serial_number = _clean(raw.get("sn") or raw.get("serial_number"))

        # `ap` finds the robot itself at 192.168.12.1, `sta-t` goes through the
        # cloud by serial number, and `sta-l` needs one of the two.
        if connection == "sta-l" and not (ip or serial_number):
            raise FleetConfigError(
                f"{where} (`{robot_id}`) is STA-L but has neither `ip` nor "
                f"`sn`. Provide one — `sn` alone works over multicast "
                f"discovery.",
                detail={"robot_id": robot_id},
            )
        if connection == "sta-t" and not serial_number:
            raise FleetConfigError(
                f"{where} (`{robot_id}`) is STA-T (remote) but has no `sn`. "
                f"The cloud flow addresses robots by serial number.",
                detail={"robot_id": robot_id},
            )

        region = _clean(raw.get("region")) or "global"
        if region.lower() not in VALID_REGIONS:
            raise FleetConfigError(
                f"{where} (`{robot_id}`) has region {region!r}; expected one "
                f"of {', '.join(VALID_REGIONS)}.",
                detail={"robot_id": robot_id},
            )

        chassis = _clean(raw.get("chassis")) or "unknown"
        chassis = chassis.lower()
        if chassis not in VALID_CHASSIS:
            chassis = "unknown"

        options = raw.get("options") or {}
        if not isinstance(options, Mapping):
            raise FleetConfigError(
                f"{where} (`{robot_id}`) has `options` that is not an object.",
                detail={"robot_id": robot_id},
            )

        return cls(
            robot_id=robot_id,
            model=model,
            connection=connection,
            ip=ip,
            serial_number=serial_number,
            aes_128_key=_clean(raw.get("aes_128_key") or raw.get("key")),
            device_type=_clean(raw.get("device_type")),
            region=region.lower(),
            chassis=chassis,
            role=_clean(raw.get("role")),
            options=dict(options),
        )


def normalise_key(key: str) -> str:
    """Canonicalise a 32-hex-char AES-128 key, or raise.

    Keys are compared and cached by this form, so ``"AB CD"`` and ``"abcd"``
    must not end up as two different cache entries for one robot.
    """
    cleaned = "".join(ch for ch in _clean(key) if ch not in " :-_")
    if not cleaned:
        return ""
    if len(cleaned) != 32 or any(ch not in "0123456789abcdefABCDEF" for ch in cleaned):
        raise FleetConfigError(
            "An AES-128 key must be 32 hex characters (16 bytes). Register "
            "one, or fetch it with `unitree-fetch-aes-key --sn <sn> --quiet`.",
            detail={"length": len(cleaned)},
        )
    return cleaned.lower()
