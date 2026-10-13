"""What is wrong with the robot *right now*, and what to call it.

Two deficiencies this fixes, both borrowed from the browser implementation in
``unitree_ui``, which is the closest thing to a specification of the wire
behaviour:

**Labels.** The driver ships 33 message strings covering sources 100–600. It
has no entry for a G1's chassis (800), power distribution (900) or emergency
stop (1000), and — more usefully for a two-robot console — no notion that
sources 301–399 and 3000–3999 are *per-motor*: they are the same bit catalog
as source 300, reported for one specific joint. Without that synthesis a
fleet operator sees "300-1000000" instead of "Motor 7: MOS almost overheat".

**Reconciliation.** The backend appends raw notifications to a bounded ring
and the error tool derived "active" by filtering out ``rm_error``. That cannot
answer the question that matters, because the three message types are not
peers:

* ``errors`` is an **authoritative snapshot** — it replaces the active set.
* ``add_error`` / ``rm_error`` are **deltas** — one entry each.

Treating them as one stream means a fault cleared in a snapshot you missed
stays visible forever, and a reconnect (which re-sends the snapshot) looks
like a fresh batch of faults.

The store also keeps a **revision** so the console can tell when something
actually changed instead of diffing on every poll.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger("unitree_robot_agent.faults")

#: Human names for the error sources. 100–600 come from the driver's catalog;
#: 800/900/1000 are G1-only and are absent from it entirely.
SOURCE_LABELS: Dict[int, str] = {
    100: "System",
    200: "Fan",
    300: "Motor",
    400: "IMU",
    500: "Battery",
    600: "Board",
    800: "Chassis",
    900: "Power distribution",
    1000: "Emergency stop",
}

#: Message types that carry faults, and how each one is interpreted.
SNAPSHOT_TYPES = ("errors",)
DELTA_ADD_TYPES = ("add_error",)
DELTA_REMOVE_TYPES = ("rm_error",)
FAULT_TYPES = SNAPSHOT_TYPES + DELTA_ADD_TYPES + DELTA_REMOVE_TYPES


def motor_index(source: int) -> Optional[int]:
    """The joint a per-motor source refers to, or ``None``.

    Two ranges, both meaning "source 300's bit catalog, for motor N":
    ``301–399`` maps directly (301 -> motor 1), and ``3000–3999`` is the
    wider form (3007 -> motor 7).
    """
    if 301 <= source <= 399:
        return source - 300
    if 3000 <= source <= 3999:
        return source % 100
    return None


def source_label(source: int) -> str:
    """The *category* an error source belongs to, without the joint number.

    A per-motor source reports the same bit catalogue as source 300 for one
    specific joint, so 301 and 3007 are both "Motor". The joint itself is
    carried separately (see :func:`motor_index` and ``Fault.motor``) rather
    than baked into this string, so a caller that renders both does not end up
    with "Motor 1 #1".
    """
    known = SOURCE_LABELS.get(source)
    if known:
        return known
    if motor_index(source) is not None:
        return SOURCE_LABELS[300]
    return f"Source {source}"


def describe_source(source: int) -> str:
    """The category plus the joint, for display: ``"Motor 7"``."""
    motor = motor_index(source)
    if motor is None:
        return source_label(source)
    return f"{source_label(source)} {motor}"


def code_label(source: int, code: int) -> str:
    """The message string for one (source, code) pair.

    Reuses the driver's catalog keyed by *unpadded uppercase hex*, which is
    the format the library's own lookup uses. Per-motor sources that are not
    in the catalog fall back to source 300's entries, because that is what
    they are.
    """
    from unitree_webrtc_connect.constants import app_error_messages

    hex_code = hex(int(code))[2:].upper()
    for candidate in (source, 300 if motor_index(source) is not None else None):
        if candidate is None:
            continue
        text = app_error_messages.get(f"app_error_code_{candidate}_{hex_code}")
        if text:
            return text
    # Unlike the library's own fallback, which prints a bare "<source>-<code>"
    # number, this keeps the label short: the source and joint are carried as
    # their own fields (`source_display`, `motor`), so repeating them here
    # would make a UI that shows all three read "Motor 1  Motor 1: code 0x1".
    return f"code 0x{hex_code}"


@dataclass(frozen=True)
class Fault:
    """One active fault."""

    source: int
    code: int
    label: str
    source_label: str
    #: 1-based joint number for per-motor sources.
    motor: Optional[int] = None
    #: When the robot first reported it, preserved across snapshots so
    #: "appeared at" does not jitter on every refresh.
    first_seen: float = 0.0
    #: When we last heard about it.
    last_seen: float = 0.0

    @property
    def key(self) -> str:
        return f"{self.source}:{self.code}"

    @property
    def code_hex(self) -> str:
        return f"0x{hex(int(self.code))[2:].upper()}"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "source": self.source,
            "source_label": self.source_label,
            # Category + joint in one string, for callers that want a single
            # readable field rather than assembling it themselves.
            "source_display": describe_source(self.source),
            "code": self.code,
            "code_hex": self.code_hex,
            "motor": self.motor,
            "label": self.label,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
        }


@dataclass
class FaultDelta:
    """What changed in one :meth:`FaultStore.apply`."""

    added: List[Fault] = field(default_factory=list)
    removed: List[Fault] = field(default_factory=list)
    #: True when this came from a full snapshot rather than a delta. The
    #: console uses it to avoid announcing every pre-existing fault as new
    #: after a reconnect.
    from_snapshot: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed)


def _normalise(entries: Any) -> List[Tuple[int, int]]:
    """Coerce the wire payloads into ``[(source, code), ...]``.

    ``errors`` is a list of ``[ts, src, code]``; ``add_error`` / ``rm_error``
    arrive as a *single* triple on some firmwares and a list on others — the
    library normalises the same way, and the existing ``handle_error`` fix
    upstream (831ff0e) is about exactly this shape difference.

    The robot's own ``ts`` is deliberately discarded: it comes from the
    robot's clock, which has no NTP guarantee, so it is unreliable for
    ordering or for "how long has this been wrong". The store timestamps
    faults from our own clock at the moment the notification arrives, and
    ``Fault.first_seen`` therefore means "when *we* first saw it".
    """
    if not entries:
        return []
    if isinstance(entries, (list, tuple)) and entries and not isinstance(
        entries[0], (list, tuple)
    ):
        entries = [entries]

    out: List[Tuple[int, int]] = []
    for item in entries:
        if not isinstance(item, (list, tuple)) or len(item) < 3:
            continue
        try:
            source = int(item[1])
            code = int(item[2])
        except (TypeError, ValueError):
            continue
        out.append((source, code))
    return out


class FaultStore:
    """The active fault set for one robot, reconciled from the wire."""

    def __init__(self) -> None:
        self._active: Dict[str, Fault] = {}
        self._revision = 0
        self._last_change_at = 0.0
        #: Set once a snapshot has been seen, so callers can distinguish
        #: "no faults" from "we have not heard from the robot yet".
        self._seen_snapshot = False

    # ── reading ──────────────────────────────────────────────────────

    @property
    def revision(self) -> int:
        """Bumped on every change; stable while nothing changes."""
        return self._revision

    @property
    def last_change_at(self) -> float:
        return self._last_change_at

    @property
    def authoritative(self) -> bool:
        """True once the robot has sent a full snapshot this session."""
        return self._seen_snapshot

    def active(self) -> List[Fault]:
        return sorted(self._active.values(), key=lambda f: (f.source, f.code))

    def count(self) -> int:
        return len(self._active)

    def clear(self, *, reason: str = "") -> None:
        """Drop everything — used on disconnect, where the set is unknown."""
        if self._active:
            logger.info("clearing %d fault(s): %s", len(self._active), reason or "reset")
            self._active.clear()
            self._touch()
        self._seen_snapshot = False

    # ── writing ──────────────────────────────────────────────────────

    def apply(self, msg_type: str, entries: Any, *, at: Optional[float] = None) -> FaultDelta:
        """Reconcile one notification. Returns what changed."""
        now = time.time() if at is None else at
        parsed = _normalise(entries)
        if msg_type in SNAPSHOT_TYPES:
            return self._apply_snapshot(parsed, now)
        if msg_type in DELTA_ADD_TYPES:
            return FaultDelta(added=self._insert(parsed, now))
        if msg_type in DELTA_REMOVE_TYPES:
            return FaultDelta(removed=self._remove(parsed))
        return FaultDelta()

    def feed_event(self, event: Dict[str, Any]) -> FaultDelta:
        """Reconcile one record from the backend's error ring."""
        if not isinstance(event, dict):
            return FaultDelta()
        msg_type = str(event.get("type") or "")
        if msg_type not in FAULT_TYPES:
            return FaultDelta()
        return self.apply(msg_type, event.get("data"), at=event.get("at"))

    def feed_events(self, events: Iterable[Dict[str, Any]]) -> FaultDelta:
        """Reconcile a whole ring, oldest first.

        Order matters: a snapshot followed by deltas must end in the delta
        state, so the caller must pass the ring in the order the robot sent
        it.
        """
        combined = FaultDelta()
        for event in events or []:
            delta = self.feed_event(event)
            combined.added.extend(delta.added)
            combined.removed.extend(delta.removed)
            combined.from_snapshot = combined.from_snapshot or delta.from_snapshot
        return combined

    # ── internals ────────────────────────────────────────────────────

    def _make(self, source: int, code: int, now: float) -> Fault:
        return Fault(
            source=source,
            code=code,
            label=code_label(source, code),
            source_label=source_label(source),
            motor=motor_index(source),
            first_seen=now,
            last_seen=now,
        )

    def _apply_snapshot(self, parsed: Sequence[Tuple[int, int]], now: float) -> FaultDelta:
        """The snapshot REPLACES the active set.

        Faults that survive keep their original ``first_seen``: without that,
        a console refreshing the list every second would report every fault as
        having just appeared.
        """
        self._seen_snapshot = True
        wanted = {(source, code) for source, code in parsed}

        added: List[Fault] = []
        for source, code in sorted(wanted):
            key = f"{source}:{code}"
            existing = self._active.get(key)
            if existing is None:
                fault = self._make(source, code, now)
                self._active[key] = fault
                added.append(fault)
            else:
                # Keep first_seen, refresh last_seen.
                self._active[key] = Fault(
                    source=existing.source,
                    code=existing.code,
                    label=existing.label,
                    source_label=existing.source_label,
                    motor=existing.motor,
                    first_seen=existing.first_seen,
                    last_seen=now,
                )

        removed = [
            fault
            for key, fault in list(self._active.items())
            if (fault.source, fault.code) not in wanted and self._active.pop(key, None)
        ]

        if added or removed:
            self._touch()
        return FaultDelta(added=added, removed=removed, from_snapshot=True)

    def _insert(self, parsed: Sequence[Tuple[int, int]], now: float) -> List[Fault]:
        """Idempotent: re-adding an active fault is not a change."""
        added: List[Fault] = []
        for source, code in parsed:
            key = f"{source}:{code}"
            if key in self._active:
                continue
            fault = self._make(source, code, now)
            self._active[key] = fault
            added.append(fault)
        if added:
            self._touch()
        return added

    def _remove(self, parsed: Sequence[Tuple[int, int]]) -> List[Fault]:
        removed: List[Fault] = []
        for source, code in parsed:
            fault = self._active.pop(f"{source}:{code}", None)
            if fault is not None:
                removed.append(fault)
        if removed:
            self._touch()
        return removed

    def _touch(self) -> None:
        self._revision += 1
        self._last_change_at = time.time()
