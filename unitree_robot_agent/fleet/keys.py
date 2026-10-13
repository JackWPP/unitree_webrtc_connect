"""Per-device AES-128 keys, one entry per robot, never shared.

Context: since G1 firmware 1.5.1 and Go2 firmware 1.1.15 the LAN signaling
handshake returns ``data2=3``, meaning the embedded RSA public key is wrapped
under a **per-device** AES-128-GCM key. Each robot has its own key and a key
belonging to another robot fails the GCM tag check.

That makes the existing cache dangerous. ``AgentConfig.load_cached_aes_key``
falls back to "the only entry for this device family", which was a reasonable
convenience for one robot and becomes a silent mis-assignment for two::

    cache = {"Go2:192.168.31.245": "<dog1's key>"}

    # dog2 is configured by IP only, no key yet
    load_cached_aes_key()  ->  "<dog1's key>"     # family has exactly 1 entry

Dog 2 is then handed dog 1's key. The handshake fails with a GCM tag error
that reads like a hardware fault, and nothing in the message points at the
cache. With two same-model robots this is not a corner case; it is the
default path the first time one dog has a cached key and the other does not.

So this store keys strictly by device identity:

* ``sn:<serial>`` is authoritative — a serial number identifies one robot
  forever, and it survives DHCP changing the IP.
* ``ip:<addr>`` is a weaker identity, used only for a spec that has no serial
  number. Entries written by IP remember the serial number they were learned
  with, so an IP entry can refuse to serve a spec that names a *different*
  robot.
* There is **no family fallback**. A miss is a miss; the caller fetches the
  key for that device.

The legitimate convenience the old fallback provided — "fetch the key once,
then address the robot by either its SN or its IP" — is preserved a different
way: when a key is stored with both identities known, it is written under
both, cross-referenced. Same device, either address, still one lookup.
"""

from __future__ import annotations

import ipaddress
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

from .spec import RobotSpec, normalise_key

#: Bumped when the on-disk shape changes.
SCHEMA_VERSION = 1


def _looks_like_address(value: str) -> bool:
    """True for an IPv4/IPv6 literal, so a legacy key can be classified."""
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class KeyEntry:
    """One cached key, plus whatever identity we learned it under."""

    key: str
    sn: str = ""
    ip: str = ""

    def to_json(self) -> Dict[str, str]:
        return {"key": self.key, "sn": self.sn, "ip": self.ip}

    @classmethod
    def from_json(cls, raw: Any) -> Optional["KeyEntry"]:
        if isinstance(raw, str):
            # Either the v1 shorthand or a legacy bare value.
            return cls(key=raw) if raw else None
        if isinstance(raw, Mapping):
            key = str(raw.get("key") or "").strip()
            if not key:
                return None
            return cls(
                key=key,
                sn=str(raw.get("sn") or "").strip(),
                ip=str(raw.get("ip") or "").strip(),
            )
        return None


class KeyStore:
    """A JSON-backed, per-device AES-128 key cache.

    Reads are tolerant (a corrupt or absent file is simply a miss). Writes are
    atomic and mode ``0600`` — these are credentials.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser()
        self._entries: Optional[Dict[str, KeyEntry]] = None

    # ── persistence ──────────────────────────────────────────────────

    def _load(self) -> Dict[str, KeyEntry]:
        if self._entries is not None:
            return self._entries

        entries: Dict[str, KeyEntry] = {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._entries = entries
            return entries

        if isinstance(payload, Mapping) and "keys" in payload:
            raw_keys = payload.get("keys") or {}
            if isinstance(raw_keys, Mapping):
                for identity, value in raw_keys.items():
                    entry = KeyEntry.from_json(value)
                    if entry is not None:
                        entries[str(identity)] = entry
        elif isinstance(payload, Mapping):
            # Legacy shape: {"Go2:192.168.31.245": "<key>", ...}. Re-key it by
            # device identity. Note the classifier is deliberately strict: an
            # entry we cannot attribute is dropped rather than guessed at,
            # because guessing is the bug we are fixing.
            for legacy_key, value in payload.items():
                if not isinstance(value, str) or not value:
                    continue
                entry = KeyEntry.from_json(value)
                if entry is None:
                    continue
                _, _, target = str(legacy_key).partition(":")
                if not target:
                    continue
                if _looks_like_address(target):
                    entries[f"ip:{target}"] = KeyEntry(key=entry.key, ip=target)
                else:
                    entries[f"sn:{target}"] = KeyEntry(key=entry.key, sn=target)

        self._entries = entries
        return entries

    def _flush(self) -> None:
        entries = self._entries or {}
        payload = {
            "version": SCHEMA_VERSION,
            "keys": {identity: entry.to_json() for identity, entry in sorted(entries.items())},
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
            os.chmod(tmp, 0o600)
            tmp.replace(self.path)
        except OSError:
            # Never let an unwritable cache break a connection attempt; the
            # caller can still pass the key explicitly or fetch it again.
            pass

    # ── lookup ───────────────────────────────────────────────────────

    def get(self, spec: RobotSpec) -> Optional[str]:
        """Return the cached key for *this device*, or ``None``.

        ``None`` means "no key is known for this robot" — never "here is a
        key from a robot of the same model".
        """
        entries = self._load()

        if spec.serial_number:
            entry = entries.get(f"sn:{spec.serial_number}")
            if entry is not None:
                return entry.key
            # A spec that knows its SN may still be served by an IP entry, but
            # only if that entry was learned from this same robot. Otherwise
            # the address has been recycled (DHCP) and the key is not ours.
            if spec.ip:
                by_ip = entries.get(f"ip:{spec.ip}")
                if by_ip is not None and by_ip.sn in ("", spec.serial_number):
                    return by_ip.key
            return None

        if spec.ip:
            entry = entries.get(f"ip:{spec.ip}")
            if entry is not None:
                return entry.key
        return None

    def put(self, spec: RobotSpec, key: str) -> None:
        """Remember *key* for *spec*, under every identity we know for it."""
        canonical = normalise_key(key)
        if not canonical:
            return
        entries = self._load()
        if spec.serial_number:
            entries[f"sn:{spec.serial_number}"] = KeyEntry(
                key=canonical, sn=spec.serial_number, ip=spec.ip
            )
        if spec.ip:
            entries[f"ip:{spec.ip}"] = KeyEntry(
                key=canonical, sn=spec.serial_number, ip=spec.ip
            )
        self._flush()

    def forget(self, spec: RobotSpec) -> None:
        """Drop this robot's key. Used when the robot rejects it."""
        entries = self._load()
        for identity in self._identities_for(spec):
            entries.pop(identity, None)
        self._flush()

    @staticmethod
    def _identities_for(spec: RobotSpec) -> Iterable[str]:
        if spec.serial_number:
            yield f"sn:{spec.serial_number}"
        if spec.ip:
            yield f"ip:{spec.ip}"

    # ── introspection ────────────────────────────────────────────────

    def known_ids(self) -> list:
        """Identities with a cached key — for ``doctor`` output, never keys."""
        return sorted(self._load().keys())

    def has_key_for(self, spec: RobotSpec) -> bool:
        return self.get(spec) is not None
