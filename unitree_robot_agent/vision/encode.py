"""Image encoding helpers — PyAV only, no OpenCV / Pillow.

``aiortc`` already depends on PyAV, so every install that can talk to a
robot can also encode a frame. Keeping this here (rather than inline in the
WebRTC backend) means the same code path serves the WebRTC track, an RTSP
stream, a local file and a local camera — and can be unit-tested without any
of them.
"""

from __future__ import annotations

import io
import logging
from typing import Optional, Tuple

from .base import sniff_image_format

logger = logging.getLogger("unitree_robot_agent.vision.encode")

DEFAULT_MAX_WIDTH = 1280


def av_available() -> bool:
    try:
        import av  # noqa: F401

        return True
    except Exception:
        return False


def encode_frame(
    frame,
    *,
    fmt: str = "jpeg",
    max_width: Optional[int] = DEFAULT_MAX_WIDTH,
) -> Tuple[Optional[bytes], int, int]:
    """Encode an ``av.VideoFrame``. Returns ``(bytes, width, height)``.

    Downscaling happens *before* encoding: a 1080p JPEG is ~200 KB and MCP
    clients are not always happy about that, while 1280px is plenty to see
    what the robot is looking at.

    No muxer is involved. That is deliberate — the obvious choices both
    misbehave when writing to memory:

    * ``image2`` wants a *filename pattern*, so pointing it at a ``BytesIO``
      yields packets while ``mux()`` writes nothing: an empty buffer and no
      error at all.
    * ``mpjpeg`` does write, but wraps the JPEG in multipart boundaries, so
      the payload no longer starts with the JPEG magic.

    A bare ``CodecContext`` gives exactly the raw JPEG frame, which also
    means ``sniff_image_format`` sees ``\\xff\\xd8`` as it should.
    (All of this was found by exercising the encoder offline: the one robot
    available for hardware testing has no camera, so this path had never
    actually run on hardware.)
    """
    try:
        import av
    except ImportError:  # pragma: no cover - aiortc always brings av
        logger.warning("PyAV is not installed; cannot encode frames")
        return None, 0, 0

    if fmt != "jpeg":
        logger.debug("only JPEG output is supported, got %r", fmt)

    try:
        if max_width and frame.width > max_width:
            height = max(2, int(frame.height * (max_width / frame.width)))
            frame = frame.reformat(width=int(max_width), height=height)
        width, height = frame.width, frame.height

        codec = av.CodecContext.create("mjpeg", "w")
        codec.width = width
        codec.height = height
        codec.pix_fmt = "yuvj420p"
        packets = list(codec.encode(frame))
        try:
            packets += list(codec.encode(None))  # flush the encoder
        except TypeError:  # older PyAV: flush takes no argument
            packets += list(codec.encode())
        payload = b"".join(bytes(packet) for packet in packets)
        if not payload:
            logger.warning("mjpeg encoder produced no data")
            return None, 0, 0
        return payload, width, height
    except Exception as exc:
        logger.warning("frame encoding failed: %s", exc)
        return None, 0, 0


def encode_buffer(payload: bytes, *, max_width: Optional[int] = DEFAULT_MAX_WIDTH):
    """Decode any image/PDF-less container PyAV understands, then re-encode.

    Used for local files and for the still-photo API, so everything the
    vision layer hands out is a well-formed JPEG.
    """
    try:
        import av
    except ImportError:  # pragma: no cover
        return None, 0, 0
    try:
        container = av.open(io.BytesIO(payload))
        for frame in container.decode(video=0):
            return encode_frame(frame, max_width=max_width)
        return None, 0, 0
    except Exception as exc:
        logger.debug("buffer decode failed: %s", exc)
        return None, 0, 0


def encode_image_file(path: str, *, max_width: Optional[int] = DEFAULT_MAX_WIDTH):
    """Read an image file and return a normalised JPEG."""
    try:
        with open(path, "rb") as handle:
            payload = handle.read()
    except OSError as exc:
        logger.debug("cannot read %s: %s", path, exc)
        return None, 0, 0
    # Already a JPEG and no scaling asked for? Ship it untouched.
    if max_width is None and sniff_image_format(payload) == "jpeg":
        return payload, 0, 0
    return encode_buffer(payload, max_width=max_width)


def probe_dimensions(payload: bytes) -> Tuple[int, int]:
    """Best-effort ``(width, height)`` for an encoded image."""
    try:
        import av
    except ImportError:  # pragma: no cover
        return 0, 0
    try:
        container = av.open(io.BytesIO(payload))
        for stream in container.streams.video:
            return int(stream.codec_context.width or 0), int(stream.codec_context.height or 0)
    except Exception:
        pass
    return 0, 0


def make_test_frame(width: int = 640, height: int = 480, shade: int = 0x40):
    """Build a solid-colour ``av.VideoFrame`` with no numpy dependency.

    Exists so the encoding path can be exercised offline (the mock backend
    and the vision tests use it).

    Each plane is filled using **its own** ``buffer_size``: PyAV pads rows to
    an alignment boundary, so ``width * height`` is the wrong number for
    anything whose width is not a multiple of the stride.
    """
    import av

    frame = av.VideoFrame(width, height, "yuv420p")
    for index, plane in enumerate(frame.planes):
        value = shade if index == 0 else 0x80
        plane.update(bytes([value]) * plane.buffer_size)
    return frame
