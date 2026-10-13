"""Vision: get a picture from (or without) the robot — and then use it.

Sources, in the order ``auto`` prefers them: the robot's WebRTC video track,
the firmware still-photo api, the on-robot RTSP server, then local files and
local cameras for offline development.

Only the first is verified on hardware today — the R1 Air available for
testing has no camera at all, so the other robot-side paths are implemented
defensively and labelled as unverified until someone runs them on a robot
that has one.
"""

from .base import FrameResult, SnapshotRecord, SourceStatus, decode_inline_image, sniff_image_format
from .capture import VisionService
from .encode import (
    DEFAULT_MAX_WIDTH,
    av_available,
    encode_buffer,
    encode_frame,
    encode_image_file,
    make_test_frame,
    probe_dimensions,
)
from .markers import (
    DEFAULT_SIZE_M,
    FAMILY,
    MIN_PX_PER_CELL,
    Marker,
    default_markers,
    load_manifest,
    pixel_budget,
    pixels_per_metre_for,
    reading_range_m,
    required_width_px,
    render_placement_sheet,
    render_tag,
    render_tile,
    write_markers,
)
from .sources import (
    BytesSource,
    DeviceCameraSource,
    FileSource,
    FrameSource,
    PhotoApiSource,
    RtspSource,
    WebRTCSource,
)

__all__ = [
    "BytesSource",
    "DEFAULT_MAX_WIDTH",
    "DEFAULT_SIZE_M",
    "FAMILY",
    "MIN_PX_PER_CELL",
    "Marker",
    "DeviceCameraSource",
    "FileSource",
    "FrameResult",
    "FrameSource",
    "PhotoApiSource",
    "RtspSource",
    "SnapshotRecord",
    "SourceStatus",
    "VisionService",
    "WebRTCSource",
    "av_available",
    "default_markers",
    "decode_inline_image",
    "encode_buffer",
    "encode_frame",
    "encode_image_file",
    "load_manifest",
    "make_test_frame",
    "pixel_budget",
    "pixels_per_metre_for",
    "probe_dimensions",
    "reading_range_m",
    "required_width_px",
    "render_placement_sheet",
    "render_tag",
    "render_tile",
    "sniff_image_format",
    "write_markers",
]
