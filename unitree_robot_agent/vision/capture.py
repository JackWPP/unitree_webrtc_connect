"""Vision service: pick a source, grab a frame, keep a small archive.

Why an archive at all? Because the useful question is rarely "what does the
robot see *now*" — it is "what did it look like when I issued that command".
Every capture is therefore written to ``<state_dir>/snapshots/`` together
with an append-only ``index.jsonl``, and ``robot_snapshot_history`` reads it
back. The store is size-capped so a long run cannot fill the disk.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

from .base import FrameResult, SourceStatus
from .encode import DEFAULT_MAX_WIDTH
from .sources import (
    DeviceCameraSource,
    FileSource,
    FrameSource,
    PhotoApiSource,
    RtspSource,
    WebRTCSource,
)

logger = logging.getLogger("unitree_robot_agent.vision")

#: Preference order for ``source="auto"``. Local explicit sources come first
#: because asking for one is a deliberate override.
AUTO_ORDER = ("webrtc", "photo_api", "rtsp", "device_camera", "file")

MAX_STORE_ENTRIES = 500


class VisionService:
    def __init__(self, config, backend, audit=None, session=None) -> None:
        self.config = config
        self.backend = backend
        self.audit = audit
        self.session = session

    async def _ensure_link(self) -> None:
        """Bring the robot link up before remote sources are tried.

        Without this, a snapshot taken before any other tool call would ask
        an unconnected backend for a frame and get silence. Failure is not
        fatal: local sources (file / camera) still have a chance.
        """
        if self.session is None or getattr(self.session, "is_connected", False):
            return
        try:
            await self.session.ensure_connected()
        except Exception as exc:
            logger.debug("vision could not establish the robot link: %s", exc)



    # ── source construction ──────────────────────────────────────────

    def _max_width(self) -> Optional[int]:
        value = getattr(self.config, "snapshot_max_width", DEFAULT_MAX_WIDTH)
        return None if not value or int(value) <= 0 else int(value)

    def build_sources(self) -> Dict[str, FrameSource]:
        cfg = self.config
        width = self._max_width()
        sources: Dict[str, FrameSource] = {
            "webrtc": WebRTCSource(self.backend, max_width=width),
            "photo_api": PhotoApiSource(
                self.backend,
                api_ids=getattr(cfg, "photo_api_ids", None),
                max_width=width,
            ),
            "rtsp": RtspSource(getattr(cfg, "rtsp_url", "") or "", max_width=width),
        }
        camera = getattr(cfg, "camera_device", "") or ""
        if camera:
            sources["device_camera"] = DeviceCameraSource(camera, max_width=width)
        snapshot_file = getattr(cfg, "snapshot_file", "") or ""
        if snapshot_file:
            sources["file"] = FileSource(snapshot_file, max_width=width)
        return sources

    def _select(self, requested: str) -> List[FrameSource]:
        sources = self.build_sources()
        want = (requested or "").strip().lower()
        if not want or want == "auto":
            want = (getattr(self.config, "video_source", "") or "auto").strip().lower()
        if want and want != "auto":
            source = sources.get(want)
            if source is None:
                return []
            return [source]
        return [sources[name] for name in AUTO_ORDER if name in sources]

    # ── capture ──────────────────────────────────────────────────────

    async def capture(
        self,
        source: str = "auto",
        *,
        timeout: float = 6.0,
        save: bool = True,
        max_width: Optional[int] = None,
    ) -> FrameResult:
        candidates = self._select(source)
        if not candidates:
            return FrameResult(
                ok=False,
                source=source or "auto",
                error=f"unknown source {source!r}",
                note=(
                    "Known sources: webrtc, photo_api, rtsp, device_camera, file. "
                    "For offline development set UNITREE_AGENT_SNAPSHOT_FILE (an "
                    "image/video) or UNITREE_AGENT_CAMERA."
                ),
            )

        if any(candidate.remote for candidate in candidates):
            await self._ensure_link()

        attempts: List[str] = []
        for candidate in candidates:
            if max_width is not None:
                candidate.max_width = max_width
            result = await candidate.grab(timeout=timeout)
            if result.ok:
                if save:
                    self._save(result)
                if self.audit is not None:
                    self.audit.record(
                        "robot_snapshot",
                        args={"source": candidate.name},
                        ok=True,
                        detail={
                            "bytes": result.size_bytes,
                            "dimensions": f"{result.width}x{result.height}",
                            "latency_ms": round(result.latency_ms, 1),
                        },
                    )
                return result
            attempts.append(f"{candidate.name}: {result.error or result.note}")
            if source and source.lower() != "auto":
                break

        return FrameResult(
            ok=False,
            source=(source or "auto"),
            error="no source produced a frame",
            note=" | ".join(attempts),
        )

    def _probe_window(self, total: float) -> float:
        """Budget for one remote source: the caller's window, not N x it."""
        return max(1.0, min(float(total), 6.0))

    async def probe(self, *, timeout: float = 6.0, include_missing: bool = True) -> List[SourceStatus]:
        """Try every source so the operator can see which one works."""
        sources = self.build_sources()
        names = [n for n in AUTO_ORDER if n in sources]
        if any(sources[name].remote for name in names):
            await self._ensure_link()
        window = self._probe_window(timeout)
        statuses: List[SourceStatus] = []
        for name in names:
            statuses.append(await sources[name].probe(timeout=window))
        if include_missing:
            for name in AUTO_ORDER:
                if name in sources:
                    continue
                statuses.append(
                    SourceStatus(
                        name=name,
                        available=False,
                        detail=_missing_reason(name),
                    )
                )
        return statuses

    # ── archive ──────────────────────────────────────────────────────

    @property
    def store_dir(self) -> Path:
        base = getattr(self.config, "snapshot_dir", None) or (self.config.state_dir / "snapshots")
        return Path(base)

    @property
    def index_path(self) -> Path:
        return self.store_dir / "index.jsonl"

    def _save(self, result: FrameResult) -> None:
        if not result.image:
            return
        try:
            self.store_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
            suffix = result.format if result.format in ("jpeg", "png", "webp", "gif") else "jpg"
            path = self.store_dir / f"{stamp}_{result.source}.{suffix}"
            counter = 1
            while path.exists():
                path = self.store_dir / f"{stamp}_{result.source}_{counter}.{suffix}"
                counter += 1
            path.write_bytes(result.image)
            result.saved_path = str(path)
            record = {
                "ts": time.time(),
                "path": str(path),
                "source": result.source,
                "width": result.width,
                "height": result.height,
                "size_bytes": result.size_bytes,
                "note": result.note,
            }
            with open(self.index_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._enforce_limits()
        except OSError as exc:
            logger.debug("could not archive snapshot: %s", exc)
            result.saved_path = None

    def history(self, limit: int = 10) -> List[dict]:
        limit = max(1, min(int(limit), 200))
        try:
            with open(self.index_path, "r", encoding="utf-8") as handle:
                lines = handle.readlines()[-limit:]
        except OSError:
            return []
        records = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            record["iso"] = time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.localtime(record.get("ts", 0))
            )
            record["exists"] = os.path.exists(record.get("path", ""))
            records.append(record)
        return records

    def _enforce_limits(self) -> None:
        """Keep the archive bounded: drop the oldest entries past the caps."""
        try:
            with open(self.index_path, "r", encoding="utf-8") as handle:
                lines = [line for line in handle if line.strip()]
            if len(lines) <= MAX_STORE_ENTRIES:
                return
            keep = lines[-MAX_STORE_ENTRIES:]
            drop = lines[:-MAX_STORE_ENTRIES]
            for line in drop:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                try:
                    os.remove(record.get("path", ""))
                except OSError:
                    pass
            tmp = self.index_path.with_suffix(".jsonl.tmp")
            tmp.write_text("".join(keep), encoding="utf-8")
            tmp.replace(self.index_path)
        except OSError:
            pass


def _missing_reason(name: str) -> str:
    return {
        "webrtc": "not available on the configured backend",
        "photo_api": "not available on the configured backend",
        "rtsp": "set UNITREE_AGENT_RTSP_URL to enable",
        "device_camera": "set UNITREE_AGENT_CAMERA=<index> to enable",
        "file": "set UNITREE_AGENT_SNAPSHOT_FILE=<image|video|dir> to enable",
    }.get(name, "not configured")
