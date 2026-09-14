"""Read-only tools: every one of these is safe to call at any time."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import sys
from typing import Any, Dict, List

from mcp.types import ToolAnnotations

from ..core.telemetry import collect_lowstate
from ..errors import AgentError
from .context import ToolContext, run_tool

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)


def register(server, ctx: ToolContext) -> None:
    @server.tool(
        name="robot_status",
        description=(
            "Connection status, robot model, whether the link is held right now, "
            "the reported posture/state and the safety mode. Call this first when "
            "anything looks wrong."
        ),
        annotations=READ_ONLY,
    )
    async def robot_status() -> Dict[str, Any]:
        async def _impl():
            status = ctx.session.status()
            state = await ctx.session.state_view()
            return ctx.ok(
                connection=status,
                posture=state,
                capabilities=sorted(ctx.profile.capabilities()),
                action_count=len(ctx.profile.actions()),
            )

        return await run_tool(ctx, "robot_status", {}, _impl)

    @server.tool(
        name="robot_get_posture",
        description=(
            "What the robot is doing right now, semantically (standing / damping / "
            "locomotion / dancing ...). Includes whether the value is trustworthy: "
            "several R1 states freeze state reporting, in which case the read-back "
            "is stale by design rather than a failure."
        ),
        annotations=READ_ONLY,
    )
    async def robot_get_posture() -> Dict[str, Any]:
        async def _impl():
            state = await ctx.session.state_view()
            return ctx.ok(posture=state)

        return await run_tool(ctx, "robot_get_posture", {}, _impl)

    @server.tool(
        name="robot_list_actions",
        description=(
            "The actions this chassis actually exposes (dances, gestures, sport "
            "commands), already filtered for the robot family. Names from here are "
            "what robot_play_action and robot_switch_state accept."
        ),
        annotations=READ_ONLY,
    )
    async def robot_list_actions(include_dangerous: bool = True) -> Dict[str, Any]:
        args = {"include_dangerous": include_dangerous}

        async def _impl():
            actions = ctx.profile.list_actions(
                dangerous=None if include_dangerous else False
            )
            states = ctx.profile.list_states()
            payload = ctx.ok(
                actions=actions,
                states=states,
                hint=(
                    "Dangerous actions additionally require "
                    "UNITREE_AGENT_ALLOW_DANGEROUS=true; every moving action "
                    "requires confirm=true on the call."
                ),
            )
            return payload

        return await run_tool(ctx, "robot_list_actions", args, _impl)

    @server.tool(
        name="robot_lowstate",
        description=(
            "Battery, IMU(s), board temperature and (optionally) the full motor "
            "table. Temperatures are already sign-corrected; voltages are in volts."
        ),
        annotations=READ_ONLY,
    )
    async def robot_lowstate(include_motors: bool = True) -> Dict[str, Any]:
        args = {"include_motors": include_motors}

        async def _impl():
            await ctx.session.ensure_connected()
            data = await collect_lowstate(
                ctx.session, ctx.profile, include_motors=include_motors
            )
            return ctx.ok(**data)

        return await run_tool(ctx, "robot_lowstate", args, _impl)

    @server.tool(
        name="robot_errors",
        description=(
            "Currently active robot errors, decoded to human-readable text "
            "(motor overheat, battery, communication ...). Reads the unsolicited "
            "error notifications the firmware pushes on the data channel."
        ),
        annotations=READ_ONLY,
    )
    async def robot_errors(limit: int = 20) -> Dict[str, Any]:
        args = {"limit": limit}

        async def _impl():
            events = []
            getter = getattr(ctx.backend, "error_events", None)
            if callable(getter):
                with contextlib.suppress(Exception):
                    events = getter() or []
            decoded = _decode_errors(events)[-max(1, min(int(limit), 200)):]
            active = [e for e in decoded if e["type"] != "rm_error"]
            return ctx.ok(
                active_count=len(active),
                errors=decoded,
                note=(
                    "An empty list means no error notification has been seen "
                    "since the last connect — it is not a full diagnostic."
                ),
            )

        return await run_tool(ctx, "robot_errors", args, _impl)

    @server.tool(
        name="robot_auth_status",
        description=(
            "How the server authenticates to the Unitree cloud: which region "
            "accepted the account, whether a login is cached, which credential "
            "sources are configured, and where the AES key cache lives. No "
            "secrets are returned. Use this when a connection fails with an "
            "auth-shaped error."
        ),
        annotations=READ_ONLY,
    )
    async def robot_auth_status() -> Dict[str, Any]:
        async def _impl():
            payload = ctx.runtime.auth.describe()
            payload["aes_key_cached"] = bool(ctx.config.load_cached_aes_key())
            payload["devices"] = [
                {"sn": d["sn"], "alias": d.get("alias"), "series": d.get("series"),
                 "model": d.get("model"), "online": d.get("online"), "has_key": d.get("has_key")}
                for d in getattr(ctx.runtime.auth, "_device_cache", [])
            ]
            return ctx.ok(**payload)

        return await run_tool(ctx, "robot_auth_status", {}, _impl)

    @server.tool(
        name="robot_fetch_key",
        description=(
            "Fetch this robot's per-device AES-128 key from the Unitree cloud "
            "and cache it (0600) so connects stop needing credentials. Only "
            "works when account credentials are configured "
            "(UNITREE_AGENT_EMAIL/PASSWORD or UNITREE_AGENT_ACCESS_TOKEN). "
            "Safe to call repeatedly — it is a lookup, not a robot command."
        ),
    )
    async def robot_fetch_key(serial_number: str = "") -> Dict[str, Any]:
        args = {"serial_number": serial_number}

        async def _impl():
            lookup = await ctx.runtime.auth.resolve_aes_key(serial_number or None)
            payload = {
                "found": lookup.ok,
                "sn": lookup.sn,
                "source": lookup.source,
                "region": lookup.region or ctx.config.resolved_region,
                "key_preview": (f"{lookup.key[:4]}…{lookup.key[-2:]}" if lookup.key else None),
                "note": lookup.note or None,
                "cached_at": str(ctx.config.key_cache_path) if lookup.ok else None,
            }
            if not lookup.ok:
                payload["hint"] = (
                    "Check robot_auth_status. If no credentials are configured, "
                    "either export UNITREE_AGENT_EMAIL + UNITREE_AGENT_PASSWORD, "
                    "or paste the key from the Unitree app as "
                    "UNITREE_AES_128_KEY."
                )
            return ctx.ok(**payload)

        return await run_tool(ctx, "robot_fetch_key", args, _impl)

    @server.tool(
        name="robot_discover",
        description=(
            "Scan the local network for robots (multicast). Pass the serial "
            "number: modern firmware — every R1 and G1 >= 1.5.1 — only answers a "
            "query that names its own SN, so an untargeted scan is usually empty."
        ),
        annotations=READ_ONLY,
    )
    async def robot_discover(
        serial_number: str = "",
        device_type: str = "",
        timeout_s: float = 3.0,
    ) -> Dict[str, Any]:
        args = {
            "serial_number": serial_number,
            "device_type": device_type,
            "timeout_s": timeout_s,
        }

        async def _impl():
            if ctx.config.backend == "mock":
                return ctx.ok(devices={}, note="mock backend: discovery is disabled.")
            from ..backends.webrtc_backend import discover

            family = device_type or ctx.profile.device_type
            devices = await discover(
                device_type=family,
                sn=serial_number or None,
                timeout=max(1.0, min(float(timeout_s), 15.0)),
            )
            return ctx.ok(
                devices=devices,
                scanned_family=family,
                note=(
                    "Empty result with an SN supplied means that serial is not "
                    "answering; empty without an SN is expected on V3 firmware."
                ),
            )

        return await run_tool(ctx, "robot_discover", args, _impl)

    @server.tool(
        name="robot_audit_tail",
        description="The last N audited operations (time, tool, arguments, outcome, robot code).",
        annotations=READ_ONLY,
    )
    async def robot_audit_tail(limit: int = 50) -> Dict[str, Any]:
        args = {"limit": limit}

        async def _impl():
            return ctx.ok(
                records=ctx.audit.tail(limit),
                log_path=str(ctx.config.audit_log) if ctx.config.audit_enabled else None,
            )

        return await run_tool(ctx, "robot_audit_tail", args, _impl)

    @server.tool(
        name="robot_doctor",
        description=(
            "Self-diagnosis: configuration (secrets redacted), which optional "
            "features are installed, whether the robot answers, and what to fix. "
            "Works without a robot — that is the point."
        ),
        annotations=READ_ONLY,
    )
    async def robot_doctor() -> Dict[str, Any]:
        async def _impl():
            report: Dict[str, Any] = {
                "config": ctx.config.redacted(),
                "robot_profile": ctx.profile.describe(),
                "environment": _environment_probe(),
                "backend": getattr(ctx.backend, "name", type(ctx.backend).__name__),
                "auth": ctx.runtime.auth.describe(),
            }

            findings: List[str] = []
            try:
                ctx.config.require_target()
            except AgentError as exc:
                findings.append(str(exc))
                report["connection_test"] = {"attempted": False, "reason": exc.code}
                report["findings"] = findings
                report["verdict"] = "not_configured"
                return ctx.ok(**report)

            try:
                await ctx.session.ensure_connected()
                # The network-status probe (STA-L / STA-T / 4G) resolves on a
                # ~1 s timer after the data channel validates, so reading it
                # immediately after connecting comes back empty. Give it a
                # moment rather than reporting a misleading null.
                await asyncio.sleep(1.2)
                state = await ctx.session.state_view()
                report["connection_test"] = {
                    "attempted": True,
                    "ok": True,
                    "state": state,
                    "network_mode": ctx.session.status().get("network_mode"),
                }
                report["verdict"] = "connected"
                await ctx.session.disconnect(reason="doctor")
            except AgentError as exc:
                report["connection_test"] = {
                    "attempted": True,
                    "ok": False,
                    "error": exc.code,
                    "message": str(exc),
                }
                findings.append(exc.hint or str(exc))
                report["verdict"] = "connect_failed"

            if ctx.config.aes_128_key is None and ctx.profile.name in ("R1",):
                findings.append(
                    "No AES-128 key configured. R1 always needs one; fetch it with "
                    "`unitree-fetch-aes-key --device-type R1 --sn <sn> --quiet`."
                )
            if ctx.config.safe_mode:
                findings.append("Read-only safe mode is ON: control tools are blocked.")
            if not ctx.config.allow_dangerous:
                findings.append("Dangerous actions are disabled (UNITREE_AGENT_ALLOW_DANGEROUS=false).")
            report["findings"] = findings
            return ctx.ok(**report)

        return await run_tool(ctx, "robot_doctor", {}, _impl)


# ─── helpers ──────────────────────────────────────────────────────────


def _decode_errors(events: List[dict]) -> List[dict]:
    """Turn raw error notifications into readable rows."""
    try:
        from unitree_webrtc_connect.msgs.error_handler import (
            get_error_code_text,
            get_error_source_text,
            integer_to_hex_string,
        )
    except Exception:  # pragma: no cover - upstream always present
        return list(events)

    rows: List[dict] = []
    for event in events:
        data = event.get("data")
        triples = data if isinstance(data, list) else [data]
        if triples and not isinstance(triples[0], (list, tuple)):
            triples = [triples]
        for triple in triples:
            if not isinstance(triple, (list, tuple)) or len(triple) < 3:
                continue
            ts, source, code = triple[0], triple[1], triple[2]
            try:
                code_hex = integer_to_hex_string(int(code))
                rows.append(
                    {
                        "type": event.get("type"),
                        "timestamp": ts,
                        "source": get_error_source_text(source),
                        "source_id": source,
                        "code": get_error_code_text(source, code_hex),
                        "raw": [ts, source, code],
                    }
                )
            except Exception:
                rows.append({"type": event.get("type"), "raw": list(triple)})
    return rows


def _environment_probe() -> dict:
    def has(module: str) -> bool:
        with contextlib.suppress(Exception):
            return importlib.util.find_spec(module) is not None
        return False

    probe = {
        "python": sys.version.split()[0],
        "optional_dependencies": {
            "mcp": has("mcp"),
            "aiortc": has("aiortc"),
            "numpy": has("numpy"),
            "wasmtime": has("wasmtime"),
            "lz4": has("lz4"),
            "sounddevice": has("sounddevice"),
            "pydub": has("pydub"),
        },
    }
    with contextlib.suppress(Exception):
        import mcp

        probe["mcp_version"] = getattr(mcp, "__version__", "unknown")
        from mcp.server.mcpserver import MCPServer  # noqa: F401

        probe["mcp_server_api"] = "mcp.server.mcpserver.MCPServer"
    with contextlib.suppress(Exception):
        import aiortc

        probe["aiortc_version"] = aiortc.__version__
    with contextlib.suppress(Exception):
        from .. import __version__

        probe["agent_version"] = __version__
    return probe
