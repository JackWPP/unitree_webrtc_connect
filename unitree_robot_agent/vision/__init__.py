"""Vision: get a picture from (or without) the robot.

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
    "decode_inline_image",
    "encode_buffer",
    "encode_frame",
    "encode_image_file",
    "make_test_frame",
    "probe_dimensions",
    "sniff_image_format",
]
