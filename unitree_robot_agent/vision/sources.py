"""Frame sources.

Four ways to get a picture, in the order ``auto`` tries them:

1. :class:`WebRTCSource` — the robot's own video track. Works on the Go2 and
   on any R1 that actually has a camera; a single frame is requested without
   keeping the video channel open.
2. :class:`PhotoApiSource` — the firmware's still-photo request
   (``rt/api/videohub/request``). **Unverified**: the only reference to it
   upstream is the topic constant, and the one R1 available for hardware
   testing has no camera at all. Implemented defensively and clearly
   labelled, with a probe that reports the raw status code so a real robot
   can be used to pin the api_id down.
3. :class:`RtspSource` — the on-robot GStreamer RTSP server (port 8551).
   Also unverified: the R1 Air advertises ``ANNOUNCE/RECORD`` (it is a push
   target) and 404s on every mount point, so there was nothing to read. Kept
   because other models/firmwares are known to serve a live view there.
4. :class:`FileSource` / :class:`DeviceCameraSource` — a local image/video or
   a local webcam. These exist so the whole vision pipeline (encoding,
   scaling, history, MCP delivery) can be developed and tested with **no
   robot at all**, which is exactly the constraint this was written under.
"""

from __future__ import annotations

import asyncio
import glob
import logging
import os
import time
from typing import List, Optional, Tuple

from .base import FrameResult, SourceStatus, decode_inline_image, sniff_image_format
from .encode import DEFAULT_MAX_WIDTH, encode_buffer, encode_frame, encode_image_file, probe_dimensions

logger = logging.getLogger("unitree_robot_agent.vision.sources")

IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".bmp", ".webp", ".gif")
VIDEO_SUFFIXES = (".mp4", ".mov", ".mkv", ".avi", ".h264", ".mjpeg")


class FrameSource:
    """Base class. Subclasses override :meth:`grab`."""

    name = "source"
    #: Short description used by the probe tool.
    description = ""
    #: True when this source needs a live robot link.
    remote = False

    def __init__(self, *, max_width: Optional[int] = DEFAULT_MAX_WIDTH) -> None:
        self.max_width = max_width
        #: Seconds the caller allowed for the frame in flight (see grab()).
        self._budget = 6.0

    async def run(self) -> Tuple[Optional[bytes], int, int, str]:
        """Return ``(jpeg, width, height, note)``; ``jpeg`` is None on failure."""
        raise NotImplementedError

    async def grab(self, *, timeout: float = 6.0) -> FrameResult:
        started = time.monotonic()
        result = FrameResult(source=self.name)
        # The hardware layers time themselves too; give them the caller's
        # budget rather than their own hard-coded default.
        self._budget = float(timeout)
        try:
            image, width, height, note = await asyncio.wait_for(self.run(), timeout=timeout)
        except asyncio.TimeoutError:
            result.error = f"no frame within {timeout:g}s"
            result.latency_ms = (time.monotonic() - started) * 1000
            return result
        except Exception as exc:
            result.error = f"{exc.__class__.__name__}: {exc}"
            result.latency_ms = (time.monotonic() - started) * 1000
            return result

        result.latency_ms = (time.monotonic() - started) * 1000
        if not image:
            result.error = result.error or "source produced no data"
            result.note = note
            return result

        result.ok = True
        result.image = image
        result.format = sniff_image_format(image) or "unknown"
        result.width, result.height = (width, height) if width else probe_dimensions(image)
        result.note = note
        return result

    async def probe(self, *, timeout: float = 6.0) -> SourceStatus:
        result = await self.grab(timeout=timeout)
        return SourceStatus(
            name=self.name,
            available=result.ok,
            detail=result.note or result.error,
            latency_ms=result.latency_ms,
            extra={
                "description": self.description,
                "size_bytes": result.size_bytes,
                "dimensions": f"{result.width}x{result.height}" if result.ok else None,
            },
        )


# ─── 1. robot video track ─────────────────────────────────────────────


class WebRTCSource(FrameSource):
    name = "webrtc"
    description = "Robot video track over the existing WebRTC link (no extra port)."
    remote = True

    def __init__(self, backend, **kwargs) -> None:
        super().__init__(**kwargs)
        self.backend = backend

    async def run(self):
        # Deliberately no "is this the mock?" special case: every backend
        # implements snapshot_frame(), and None means no frame — which is
        # exactly what a camera-less robot reports too.
        raw = await self.backend.snapshot_frame(timeout=self._budget)
        if not raw:
            return (
                None,
                0,
                0,
                "no video track arrived — this model may not have a camera, or "
                "the firmware does not expose one over WebRTC (verified on an "
                "R1 Air: no track at all within 20s)",
            )
        image, width, height = encode_buffer(raw, max_width=self.max_width)
        return image, width, height, ""


# ─── 2. firmware still-photo API ──────────────────────────────────────


class PhotoApiSource(FrameSource):
    name = "photo_api"
    description = (
        "Firmware still-photo request on rt/api/videohub/request. UNVERIFIED — "
        "no api_id is documented upstream; set UNITREE_AGENT_PHOTO_API_IDS to "
        "the ids worth trying and use robot_vision_probe to read the codes."
    )
    remote = True

    def __init__(self, backend, api_ids: Optional[List[int]] = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.backend = backend
        self.api_ids = list(api_ids or [1001, 1002, 1003, 1004, 1005, 1006])

    async def run(self):
        if getattr(self.backend, "api_request", None) is None:
            return None, 0, 0, "backend has no api_request"

        topic = "rt/api/videohub/request"
        attempts: List[str] = []
        for api_id in self.api_ids:
            try:
                response = await self.backend.api_request(
                    topic, api_id, None, timeout=min(3.0, self._budget)
                )
            except Exception as exc:
                attempts.append(f"{api_id}:{exc.__class__.__name__}")
                continue

            payload = response.decoded(default=None)
            attempts.append(f"{api_id}:code={response.code}")
            image = None
            if isinstance(payload, str):
                image = decode_inline_image(payload)
            elif isinstance(payload, dict):
                for key in ("image", "data", "photo", "jpeg", "picture"):
                    value = payload.get(key)
                    if isinstance(value, str):
                        image = decode_inline_image(value)
                        if image:
                            break
            if image:
                encoded, width, height = encode_buffer(image, max_width=self.max_width)
                if encoded:
                    return encoded, width, height, f"served by api_id={api_id}"
        return (
            None,
            0,
            0,
            "no still-photo reply carrying an image; tried " + ", ".join(attempts),
        )


# ─── 3. on-robot RTSP ────────────────────────────────────────────────


class RtspSource(FrameSource):
    name = "rtsp"
    description = "On-robot RTSP server (GStreamer, usually :8551). UNVERIFIED on R1 Air."
    remote = True

    DEFAULT_MOUNTS = ("", "/live", "/stream", "/video", "/main", "/ds", "/1")

    def __init__(self, url: str = "", mounts: Optional[List[str]] = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.url = url
        self.mounts = tuple(mounts or self.DEFAULT_MOUNTS)

    def _candidates(self) -> List[str]:
        if self.url:
            return [self.url]
        return []

    async def run(self):
        candidates = self._candidates()
        if not candidates:
            return None, 0, 0, "no RTSP url configured (set UNITREE_AGENT_RTSP_URL)"
        errors = []
        for url in candidates:
            image, width, height, note = await asyncio.to_thread(self._grab_blocking, url)
            if image:
                return image, width, height, note
            errors.append(f"{url}: {note}")
        return None, 0, 0, "; ".join(errors)

    def _grab_blocking(self, url: str):
        try:
            import av
        except ImportError:  # pragma: no cover
            return None, 0, 0, "PyAV not installed"
        budget = min(4.0, self._budget)
        try:
            container = av.open(
                url,
                options={"rtsp_transport": "tcp", "stimeout": str(int(budget * 1_000_000))},
                timeout=budget,
            )
        except Exception as exc:
            return None, 0, 0, f"{exc.__class__.__name__}: {str(exc)[:120]}"
        try:
            for frame in container.decode(video=0):
                image, width, height = encode_frame(frame, max_width=self.max_width)
                return image, width, height, ""
            return None, 0, 0, "stream opened but no video frame was decoded"
        except Exception as exc:
            return None, 0, 0, f"{exc.__class__.__name__}: {str(exc)[:120]}"
        finally:
            try:
                container.close()
            except Exception:
                pass


# ─── 4. local sources (offline development) ──────────────────────────


class FileSource(FrameSource):
    name = "file"
    description = "Local image or video file. For developing the vision pipeline with no robot."
    remote = False

    def __init__(self, path: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.path = path

    def _resolve(self) -> Optional[str]:
        if os.path.isfile(self.path):
            return self.path
        # A directory (or glob) picks the newest matching file, which is what
        # you want when a test harness drops frames into a folder.
        matches: List[str] = []
        if os.path.isdir(self.path):
            for suffix in IMAGE_SUFFIXES + VIDEO_SUFFIXES:
                matches.extend(glob.glob(os.path.join(self.path, f"*{suffix}")))
        elif any(ch in self.path for ch in "*?["):
            matches = glob.glob(self.path)
        if not matches:
            return None
        return max(matches, key=os.path.getmtime)

    async def run(self):
        path = self._resolve()
        if not path:
            return None, 0, 0, f"no file found at {self.path!r}"
        lower = path.lower()
        if lower.endswith(VIDEO_SUFFIXES):
            return await asyncio.to_thread(self._grab_video, path)
        image, width, height = await asyncio.to_thread(
            encode_image_file, path, max_width=self.max_width
        )
        return image, width, height, f"from {os.path.basename(path)}"

    def _grab_video(self, path: str):
        try:
            import av
        except ImportError:  # pragma: no cover
            return None, 0, 0, "PyAV not installed"
        try:
            container = av.open(path)
            for frame in container.decode(video=0):
                image, width, height = encode_frame(frame, max_width=self.max_width)
                return image, width, height, f"first frame of {os.path.basename(path)}"
            return None, 0, 0, "video contained no frames"
        except Exception as exc:
            return None, 0, 0, f"{exc.__class__.__name__}: {str(exc)[:120]}"
        finally:
            try:
                container.close()
            except Exception:
                pass


class DeviceCameraSource(FrameSource):
    name = "device_camera"
    description = "Local camera (macOS avfoundation / Linux v4l2). Offline development only."
    remote = False

    def __init__(self, device: str = "0", **kwargs) -> None:
        super().__init__(**kwargs)
        self.device = str(device)

    def _input_format(self) -> str:
        return "avfoundation" if os.uname().sysname == "Darwin" else "v4l2"

    async def run(self):
        return await asyncio.to_thread(self._grab_blocking)

    def _grab_blocking(self):
        try:
            import av
        except ImportError:  # pragma: no cover
            return None, 0, 0, "PyAV not installed"
        fmt = self._input_format()
        source = f"{self.device}:none" if fmt == "avfoundation" else self.device
        try:
            container = av.open(source, format=fmt, options={"framerate": "30"})
        except Exception as exc:
            return None, 0, 0, f"{exc.__class__.__name__}: {str(exc)[:120]}"
        try:
            for frame in container.decode(video=0):
                image, width, height = encode_frame(frame, max_width=self.max_width)
                return image, width, height, f"local camera {self.device}"
            return None, 0, 0, "camera opened but produced no frame"
        except Exception as exc:
            return None, 0, 0, f"{exc.__class__.__name__}: {str(exc)[:120]}"
        finally:
            try:
                container.close()
            except Exception:
                pass


class BytesSource(FrameSource):
    """Wraps an in-memory image. Used by tests and by the mock backend."""

    name = "bytes"
    description = "In-memory image payload (tests / mock backend)."
    remote = False

    def __init__(self, payload: bytes, **kwargs) -> None:
        super().__init__(**kwargs)
        self.payload = payload

    async def run(self):
        image, width, height = await asyncio.to_thread(
            lambda: encode_buffer(self.payload, max_width=self.max_width)
            if sniff_image_format(self.payload) != "jpeg"
            else (
                self.payload,
                *(probe_dimensions(self.payload) or (0, 0)),
            )
        )
        return image, width, height, "from memory"
