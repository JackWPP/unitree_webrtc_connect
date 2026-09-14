"""Unitree cloud authentication: region detection, sessions, AES keys.

Hard-won lessons from the first real-robot session (2026-09-13) are baked
into this module:

1. **The region is not guessable.** An account registered in China answers
   ``login/email`` on ``global`` with ``code=1000 "email not register"`` —
   which reads like a wrong password but is actually a wrong endpoint.
   So ``region="auto"`` walks the candidates and remembers the winner.
2. **Logging in on every connect is wasteful and fragile.** The cloud
   returns an access token plus a refresh token; both are cached per
   account here (0600) so a later run can skip the password entirely and
   only re-login when the refresh token is also dead.
3. **The AES key must be fetched once per device**, not per connect. The
   key is stable across re-pairings, so it is cached separately in
   :meth:`AgentConfig.store_cached_aes_key`.

Everything blocking (curl_cffi) runs in a worker thread so the MCP server's
event loop is never stalled by a login.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..errors import AgentError, UnitreeCloudAuthError

logger = logging.getLogger("unitree_robot_agent.auth")

REGIONS = ("global", "cn")
AUTO_REGION = "auto"

#: Substrings that mean "this account does not exist on this endpoint"
#: rather than "your password is wrong".
_WRONG_REGION_MARKERS = (
    "not register",
    "not exist",
    "no such user",
    "unregistered",
)


@dataclass
class CloudSession:
    """A cached login for one account."""

    region: str = ""
    email: str = ""
    access_token: str = ""
    refresh_token: str = ""
    obtained_at: float = 0.0
    source: str = ""  # "env" | "login" | "cache" | "refresh"

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(payload: Dict[str, Any]) -> "CloudSession":
        known = {f for f in CloudSession.__dataclass_fields__}
        return CloudSession(**{k: v for k, v in (payload or {}).items() if k in known})

    @property
    def age_s(self) -> float:
        return max(0.0, time.time() - self.obtained_at) if self.obtained_at else 0.0


@dataclass
class KeyLookup:
    """Outcome of an AES-key lookup, so callers can explain what happened."""

    key: Optional[str] = None
    sn: str = ""
    source: str = ""      # "config" | "cache" | "cloud"
    region: str = ""
    devices: List[dict] = field(default_factory=list)
    note: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.key)


class CloudAuth:
    """Region-aware, session-caching wrapper around ``UnitreeCloud``."""

    def __init__(self, config, audit=None) -> None:
        self.config = config
        self.audit = audit
        self.session: Optional[CloudSession] = None
        self._client = None
        self._lock = asyncio.Lock()
        self._device_cache: List[dict] = []

    # ── session persistence ──────────────────────────────────────────

    @property
    def cache_path(self) -> Path:
        return self.config.state_dir / "session.json"

    def _account_key(self, session: Optional[CloudSession] = None) -> str:
        """Stable per-account key.

        Prefers the email carried *by the session* over the configured
        username: a later run that no longer exports UNITREE_AGENT_EMAIL must
        still write (and find) the same entry, otherwise it would silently
        create a second one under a different key.
        """
        email = (session.email if session else "") or self.config.username or ""
        return email.strip().lower() or "default"

    def load_session(self) -> Optional[CloudSession]:
        """Return the cached login for this account.

        The lookup has to survive the common case where credentials are
        supplied once and then removed from the environment: in that run
        there is no username to key on, so fall back to the only cached
        entry (or the one whose stored email matches, when there are
        several accounts on this machine).
        """
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict) or not payload:
            return None

        candidates = {
            key: value for key, value in payload.items() if isinstance(value, dict)
        }
        if not candidates:
            return None

        entry = candidates.get(self._account_key())
        if entry is None and self.config.username:
            wanted = self.config.username.strip().lower()
            for value in candidates.values():
                if str(value.get("email", "")).strip().lower() == wanted:
                    entry = value
                    break
        if entry is None and len(candidates) == 1:
            # Single account on this machine: no need to guess further.
            entry = next(iter(candidates.values()))
        if not isinstance(entry, dict):
            return None

        session = CloudSession.from_dict(entry)
        return session if session.access_token else None

    def store_session(self, session: CloudSession) -> None:
        """Persist the login (0600). Best-effort — never raises."""
        try:
            self.config.state_dir.mkdir(parents=True, exist_ok=True)
            try:
                payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    payload = {}
            except (OSError, ValueError):
                payload = {}
            payload[self._account_key(session)] = session.to_dict()
            tmp = self.cache_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
            os.chmod(tmp, 0o600)
            tmp.replace(self.cache_path)
        except OSError as exc:
            logger.debug("could not persist session: %s", exc)

    def clear_session(self) -> None:
        self.session = None
        self._client = None
        try:
            if self.cache_path.exists():
                self.cache_path.unlink()
        except OSError:
            pass

    # ── region handling ──────────────────────────────────────────────

    def _region_candidates(self) -> List[str]:
        configured = (self.config.region or AUTO_REGION).lower()
        if configured in ("", AUTO_REGION):
            return list(REGIONS)
        if configured not in REGIONS:
            return list(REGIONS)
        first = [configured]
        if not self.config.region_auto_fallback:
            return first
        return first + [r for r in REGIONS if r != configured]

    def _note_region(self, region: str) -> None:
        """Record the working region so later connects and STA-T use it."""
        with contextlib.suppress(Exception):
            self.config.resolved_region = region

    # ── client construction ──────────────────────────────────────────

    async def client(self):
        """Return a logged-in ``UnitreeCloud``, reusing whatever works."""
        if self._client is not None:
            return self._client
        async with self._lock:
            if self._client is not None:
                return self._client
            self._client = await asyncio.to_thread(self._connect_blocking)
        return self._client

    # ── blocking internals (run in a worker thread) ──────────────────

    def _connect_blocking(self):
        from unitree_webrtc_connect import UnitreeCloud, UnitreeCloudError

        cfg = self.config

        # 1. Explicit access token — no login, no password.
        if cfg.access_token:
            for region in self._region_candidates():
                client = UnitreeCloud(
                    region=region,
                    device_type=cfg.device_type,
                    access_token=cfg.access_token,
                )
                if self._probe(client, UnitreeCloudError):
                    self.session = CloudSession(
                        region=region, email=cfg.username or "", access_token=cfg.access_token,
                        source="env", obtained_at=time.time(),
                    )
                    self._note_region(region)
                    self._audit("auth", ok=True, source="env", region=region)
                    return client
            raise UnitreeCloudAuthError(
                "The supplied access token was rejected on both regions.",
                hint="Tokens expire. Log in again, or fetch a fresh one from the app.",
            )

        # 2. Cached session: access token first, then refresh.
        cached = self.load_session()
        if cached and cached.access_token:
            region = cached.region or self._region_candidates()[0]
            client = UnitreeCloud(
                region=region,
                device_type=cfg.device_type,
                access_token=cached.access_token,
                refresh_token=cached.refresh_token,
            )
            if self._probe(client, UnitreeCloudError):
                cached.source = "cache"
                self.session = cached
                self._note_region(region)
                self._audit("auth", ok=True, source="cache", region=region)
                return client

            if self._refresh(client, UnitreeCloudError):
                cached.source = "refresh"
                cached.obtained_at = time.time()
                cached.access_token = client.access_token
                cached.refresh_token = client.refresh_token or cached.refresh_token
                self.session = cached
                self.store_session(cached)
                self._note_region(region)
                self._audit("auth", ok=True, source="refresh", region=region)
                return client

            logger.info("cached Unitree session is dead; falling back to login")
            self._audit("auth", ok=False, source="cache", error="session_expired")

        # 3. Email + password login, walking the region candidates.
        if not (cfg.username and cfg.password):
            expired = self.load_session() is not None
            raise UnitreeCloudAuthError(
                "Cached Unitree session is no longer valid and no password was "
                "supplied to log in again."
                if expired
                else "No usable Unitree credentials.",
                hint=(
                    "Re-supply UNITREE_AGENT_EMAIL + UNITREE_AGENT_PASSWORD once "
                    "(the new session is cached), or paste the robot's AES key as "
                    "UNITREE_AES_128_KEY to skip the cloud entirely."
                ),
            )

        errors: List[str] = []
        for region in self._region_candidates():
            client = UnitreeCloud(region=region, device_type=cfg.device_type)
            try:
                client.login_email(cfg.username, cfg.password)
            except UnitreeCloudError as exc:
                errors.append(f"{region}: {exc}")
                if self._looks_like_wrong_region(str(exc)):
                    self._audit("auth", ok=False, region=region, error="wrong_region")
                    continue
                # Wrong password on the right endpoint — stop trying regions.
                self._audit("auth", ok=False, region=region, error=str(exc)[:120])
                raise UnitreeCloudAuthError(
                    f"Login failed: {exc}",
                    hint=(
                        "Check the email/password. If the account is registered in "
                        "China, set UNITREE_AGENT_REGION=cn; elsewhere it is global."
                    ),
                ) from exc

            session = CloudSession(
                region=region,
                email=cfg.username,
                access_token=client.access_token,
                refresh_token=client.refresh_token,
                source="login",
                obtained_at=time.time(),
            )
            self.session = session
            self.store_session(session)
            self._note_region(region)
            self._audit("auth", ok=True, source="login", region=region)
            return client

        raise UnitreeCloudAuthError(
            "Login failed on every region. " + " | ".join(errors),
            hint=(
                "If the account was registered in China, set "
                "UNITREE_AGENT_REGION=cn; otherwise use global."
            ),
        )

    @staticmethod
    def _probe(client, error_cls) -> bool:
        """True when the token actually works (a lightweight authed call)."""
        try:
            client.list_devices()
            return True
        except error_cls as exc:
            logger.debug("token probe failed: %s", exc)
            return False
        except Exception as exc:  # network blips should not look like auth failure
            logger.debug("token probe raised: %s", exc)
            return False

    @staticmethod
    def _refresh(client, error_cls) -> bool:
        refresh = getattr(client, "_refresh", None)
        if not callable(refresh):
            return False
        try:
            return bool(refresh())
        except error_cls as exc:
            logger.debug("refresh failed: %s", exc)
            return False
        except Exception as exc:
            logger.debug("refresh raised: %s", exc)
            return False

    @staticmethod
    def _looks_like_wrong_region(message: str) -> bool:
        low = (message or "").lower()
        return any(marker in low for marker in _WRONG_REGION_MARKERS)

    def _audit(self, tool: str, **detail) -> None:
        if self.audit is not None:
            self.audit.record(tool, phase="auth", detail=detail or None)

    # ── public API used by the backend / tools ───────────────────────

    async def list_devices(self) -> List[dict]:
        """Bound devices as plain dicts (masked-safe: they do carry keys)."""
        client = await self.client()
        devices = await asyncio.to_thread(client.list_devices)
        rows = [
            {
                "sn": d.sn,
                "alias": d.alias,
                "series": d.series,
                "model": d.model,
                "online": d.online,
                "has_key": bool(d.key),
                "key": d.key,
            }
            for d in devices
        ]
        self._device_cache = rows
        return rows

    async def resolve_aes_key(self, sn: Optional[str] = None) -> KeyLookup:
        """Find the per-device AES-128 key for ``sn`` (or the configured one)."""
        target = (sn or self.config.serial_number or "").strip()

        if self.config.aes_128_key:
            return KeyLookup(key=self.config.aes_128_key, sn=target, source="config")

        cached = self.config.load_cached_aes_key()
        if cached:
            return KeyLookup(key=cached, sn=target, source="cache")

        # A cached session counts as "configured": the whole point of caching
        # is that later runs no longer need the email/password in the env.
        if not (
            self.config.username
            or self.config.access_token
            or self.load_session() is not None
        ):
            return KeyLookup(
                note="No account configured, so the cloud lookup was skipped.",
            )

        try:
            devices = await self.list_devices()
        except AgentError as exc:
            return KeyLookup(note=str(exc))
        except Exception as exc:  # pragma: no cover - network
            return KeyLookup(note=f"{exc.__class__.__name__}: {exc}")

        if not target:
            humanoids = [d for d in devices if (d.get("series") or "").upper() in ("R1", "G1")]
            if len(devices) == 1:
                target = devices[0]["sn"]
            elif len(humanoids) == 1:
                target = humanoids[0]["sn"]

        match = next((d for d in devices if d["sn"] == target), None)
        if match is None:
            return KeyLookup(
                devices=_masked(devices),
                region=(self.session.region if self.session else ""),
                note=(
                    f"No bound device matched SN {target!r}. "
                    + (
                        f"Bound: {', '.join(d['sn'] for d in devices)}"
                        if devices
                        else "The account has no bound devices."
                    )
                ),
            )
        if not match.get("key"):
            return KeyLookup(
                sn=match["sn"],
                devices=_masked(devices),
                region=(self.session.region if self.session else ""),
                note=(
                    "The device is bound but the cloud returned an empty dev.key. "
                    "R1 always needs one; for G1/Go2 it exists only on firmware "
                    "that uses data2=3 (G1 >= 1.5.1 / Go2 >= 1.1.15)."
                ),
            )

        key = match["key"]
        self.config.aes_128_key = key
        self.config.serial_number = self.config.serial_number or match["sn"]
        self.config.store_cached_aes_key(key)
        self._audit(
            "aes_key_lookup",
            ok=True,
            sn=match["sn"],
            region=(self.session.region if self.session else ""),
        )
        return KeyLookup(
            key=key,
            sn=match["sn"],
            source="cloud",
            region=(self.session.region if self.session else ""),
            devices=_masked(devices),
        )

    def describe(self) -> dict:
        """Auth state for ``doctor`` — never includes secrets."""
        return {
            "region_configured": self.config.region,
            "region_resolved": getattr(self.config, "resolved_region", None),
            "auto_fallback": self.config.region_auto_fallback,
            "credentials": {
                "email": bool(self.config.username),
                "password": bool(self.config.password),
                "access_token": bool(self.config.access_token),
                "aes_key": bool(self.config.aes_128_key),
            },
            "cached_session": bool(self.session) or self.load_session() is not None,
            "session_source": (self.session.source if self.session else None),
            "session_age_s": round(self.session.age_s, 1) if self.session else None,
            "session_cache": str(self.cache_path),
            "key_cache": str(self.config.key_cache_path),
        }


def _masked(devices: List[dict]) -> List[dict]:
    out = []
    for d in devices:
        row = dict(d)
        if row.get("key"):
            row["key"] = row["key"][:4] + "****" + row["key"][-2:]
        out.append(row)
    return out
