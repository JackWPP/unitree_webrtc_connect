"""Shared types for the vision layer."""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field
from typing import Optional

#: Magic numbers so callers never have to trust a filename.
_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_GIF_MAGIC = (b"GIF87a", b"GIF89a")
_WEBP_MAGIC = b"RIFF"


def sniff_image_format(payload: bytes) -> Optional[str]:
    """Best-effort container detection for a byte string."""
    if not payload or len(payload) < 12:
        return None
    if payload.startswith(_JPEG_MAGIC):
        return "jpeg"
    if payload.startswith(_PNG_MAGIC):
        return "png"
    if payload.startswith(_GIF_MAGIC):
        return "gif"
    if payload.startswith(_WEBP_MAGIC) and payload[8:12] == b"WEBP":
        return "webp"
    return None


def looks_like_base64_image(text: str) -> bool:
    """True when a string plausibly carries an inline image."""
    if not text or not isinstance(text, str):
        return False
    sample = text.strip()
    if sample.startswith("data:image/"):
        return True
    if len(sample) < 256:  # too short to be an image payload
        return False
    head = sample[:64]
    # JPEG/PNG in base64 always start with these prefixes.
    return head.startswith(("/9j/", "iVBOR", "R0lGOD"))


def decode_inline_image(text: str) -> Optional[bytes]:
    """Decode ``data:image/...;base64,XXX`` or a bare base64 image string."""
    if not looks_like_base64_image(text):
        return None
    payload = text.strip()
    if payload.startswith("data:"):
        _, _, payload = payload.partition(",")
    try:
        raw = base64.b64decode(payload, validate=False)
    except Exception:
        return None
    return raw if sniff_image_format(raw) else None


@dataclass
class FrameResult:
    """One captured frame, however it was obtained."""

    ok: bool = False
    image: Optional[bytes] = None
    format: str = ""
    source: str = ""
    width: int = 0
    height: int = 0
    latency_ms: float = 0.0
    saved_path: Optional[str] = None
    error: str = ""
    note: str = ""

    @property
    def size_bytes(self) -> int:
        return len(self.image or b"")

    def to_dict(self, *, include_image: bool = False) -> dict:
        out = {
            "ok": self.ok,
            "source": self.source,
            "format": self.format,
            "width": self.width,
            "height": self.height,
            "size_bytes": self.size_bytes,
            "latency_ms": round(self.latency_ms, 1),
        }
        if self.saved_path:
            out["saved_path"] = self.saved_path
        if self.error:
            out["error"] = self.error
        if self.note:
            out["note"] = self.note
        if include_image and self.image:
            out["image_base64"] = base64.b64encode(self.image).decode("ascii")
        return out


@dataclass
class SourceStatus:
    """Result of probing one possible frame source."""

    name: str
    available: bool
    detail: str = ""
    latency_ms: float = 0.0
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        out = {"source": self.name, "available": self.available}
        if self.detail:
            out["detail"] = self.detail
        if self.latency_ms:
            out["latency_ms"] = round(self.latency_ms, 1)
        if self.extra:
            out.update(self.extra)
        return out


@dataclass
class SnapshotRecord:
    """A frame written to the snapshot store."""

    path: str
    ts: float = field(default_factory=time.time)
    source: str = ""
    width: int = 0
    height: int = 0
    size_bytes: int = 0
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.ts)),
            "source": self.source,
            "width": self.width,
            "height": self.height,
            "size_bytes": self.size_bytes,
            "note": self.note,
        }
