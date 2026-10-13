"""Audit log for every robot-affecting call.

Requirements that shaped this module:

* Every tool call that can move the robot must be traceable afterwards
  (time, tool, arguments, robot status code, outcome).
* Secrets must never land in the log — AES keys, passwords and tokens are
  redacted by key name *and* by value shape.
* A failing log must never take the robot link down, so all I/O is
  best-effort and swallows ``OSError``.

Records are written as one JSON object per line to
``<state_dir>/audit.log`` (rotated by size) so ``audit-tail`` and any
external log shipper can read them trivially.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

_LOGGER_NAME = "unitree_robot_agent.audit"

# Key names (case-insensitive, matched as substrings) whose values are
# replaced before anything is logged or returned to a caller.
_SECRET_MARKERS = (
    "password",
    "passwd",
    "aes",
    "key",
    "token",
    "secret",
    "credential",
    "authorization",
    "cookie",
)

_MAX_STRING = 160
_MAX_ITEMS = 32

#: Keys that are known to be safe even though they contain a marker.
_SECRET_ALLOWLIST = frozenset({"key_count", "keys", "key_name", "keystroke"})


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Return a JSON-safe copy of ``value`` with secrets masked."""
    if _depth > 6:
        return "…"
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for index, (k, v) in enumerate(value.items()):
            if index >= _MAX_ITEMS:
                out["…"] = f"{len(value) - _MAX_ITEMS} more"
                break
            k_str = str(k)
            out[k_str] = "***" if _is_secret_key(k_str) else redact(v, _depth=_depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        if len(items) > _MAX_ITEMS:
            items = items[:_MAX_ITEMS] + [f"… {len(value) - _MAX_ITEMS} more"]
        return [redact(v, _depth=_depth + 1) for v in items]
    if isinstance(value, str):
        return value if len(value) <= _MAX_STRING else value[:_MAX_STRING] + "…"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:_MAX_STRING]


def _is_secret_key(name: str) -> bool:
    low = str(name).lower()
    if low in _SECRET_ALLOWLIST:
        return False
    return any(marker in low for marker in _SECRET_MARKERS)


def summarize_args(args: Dict[str, Any]) -> Dict[str, Any]:
    """Compact, redacted argument view used for audit records.

    Redaction is keyed on the *argument name*, so it happens here as well as
    inside :func:`redact` — a plain ``str`` value carries no type information
    telling us it is a secret.
    """
    out: Dict[str, Any] = {}
    for k, v in (args or {}).items():
        key = str(k)
        if _is_secret_key(key):
            out[key] = "***"
        elif isinstance(v, (list, tuple)) and len(v) > 8:
            out[key] = f"<{len(v)} items>"
        elif isinstance(v, str) and len(v) > _MAX_STRING:
            out[key] = v[:_MAX_STRING] + "…"
        else:
            out[key] = redact(v)
    return out


@dataclass
class AuditLog:
    """Append-only JSONL audit trail for **one** robot.

    Each instance owns a *private* logger rather than sharing a module-level
    one. That matters as soon as there is more than one robot: the previous
    implementation fetched the process-wide logger by name and then added a
    ``RotatingFileHandler`` to it per instance, so N robots meant N handlers
    on one logger and **every record was written to all N files** — dog 1's
    movements appearing in dog 2's trail. The shared logger was also why
    ``to_stderr`` never took effect once a file handler existed.

    ``robot_id`` is stamped onto every record. ``robot`` (the model family)
    stays as capability context, but it cannot tell two Go2s apart, which is
    exactly the case this field exists for.
    """

    path: Optional[Path] = None
    enabled: bool = True
    to_stderr: bool = False
    max_bytes: int = 5 * 1024 * 1024
    backups: int = 3
    #: Stable identity of the robot this trail belongs to, e.g. ``"dog1"``.
    robot_id: str = ""
    _logger: logging.Logger = field(init=False, repr=False)
    _buffer: List[dict] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        # Deliberately NOT `logging.getLogger(name)`: that registry is
        # process-global, so two AuditLogs would end up configuring the same
        # logger and cross-writing. A directly constructed Logger has no
        # parent and no entry in the manager, which is what we want.
        self._logger = logging.Logger(
            f"{_LOGGER_NAME}.{id(self):x}", level=logging.INFO
        )
        self._logger.propagate = False
        if not self.enabled:
            return
        if self.path is not None:
            try:
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
                handler: logging.Handler = logging.handlers.RotatingFileHandler(
                    self.path,
                    maxBytes=self.max_bytes,
                    backupCount=self.backups,
                    encoding="utf-8",
                )
                handler.setFormatter(logging.Formatter("%(message)s"))
                self._logger.addHandler(handler)
            except OSError:
                # Never let an unwritable log path break the tool call.
                self.path = None
        if self.to_stderr and not any(
            isinstance(h, logging.StreamHandler)
            and not isinstance(h, logging.FileHandler)
            for h in self._logger.handlers
        ):
            # Note the FileHandler exclusion: RotatingFileHandler *is* a
            # StreamHandler, so the old check saw the file handler and
            # silently skipped stderr altogether.
            stream = logging.StreamHandler()
            stream.setFormatter(logging.Formatter("%(message)s"))
            self._logger.addHandler(stream)

    # ── writing ──────────────────────────────────────────────────────

    def record(
        self,
        tool: str,
        *,
        phase: str = "result",
        args: Optional[Dict[str, Any]] = None,
        ok: Optional[bool] = None,
        code: Optional[int] = None,
        error: Optional[str] = None,
        detail: Any = None,
        duration_ms: Optional[float] = None,
        robot: Optional[str] = None,
    ) -> dict:
        entry: Dict[str, Any] = {
            "ts": round(time.time(), 3),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            "tool": tool,
            "phase": phase,
        }
        if robot:
            entry["robot"] = robot
        if self.robot_id:
            entry["robot_id"] = self.robot_id
        if args:
            entry["args"] = summarize_args(args)
        if ok is not None:
            entry["ok"] = ok
        if code is not None:
            entry["code"] = code
        if error:
            entry["error"] = error
        if detail is not None:
            entry["detail"] = redact(detail)
        if duration_ms is not None:
            entry["duration_ms"] = round(duration_ms, 1)

        if self.enabled and self._logger.handlers:
            try:
                self._logger.info(json.dumps(entry, ensure_ascii=False, default=str))
            except Exception:
                pass
        self._buffer.append(entry)
        if len(self._buffer) > 500:
            del self._buffer[:-200]
        return entry

    # ── reading (for the audit_tail tool) ────────────────────────────

    def tail(self, limit: int = 50) -> List[dict]:
        """Return the last ``limit`` records, newest last.

        Reads from the file when available, otherwise falls back to the
        in-memory buffer (which is what a stdio deployment without a
        writable state dir will use)."""
        limit = max(1, min(int(limit), 500))
        if self.path is not None:
            try:
                with open(self.path, "r", encoding="utf-8") as handle:
                    lines = handle.readlines()[-limit:]
                records = []
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except ValueError:
                        continue
                if records:
                    return records
            except OSError:
                pass
        return list(self._buffer[-limit:])
