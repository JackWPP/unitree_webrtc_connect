"""Vision: encoding, sources, capture ordering and the snapshot archive.

Everything here runs offline. The robot-side sources are exercised through
the mock backend (which can pretend to have, or not have, a camera) — that
second case matters because it is what the real R1 Air does.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time

import pytest

from unitree_robot_agent.cli import _invoke
from unitree_robot_agent.runtime import create_runtime
from unitree_robot_agent.server import build_server
from unitree_robot_agent.vision import VisionService
from unitree_robot_agent.vision import capture as capture_module
from unitree_robot_agent.vision.base import decode_inline_image, sniff_image_format
from unitree_robot_agent.vision.encode import (
    encode_buffer,
    encode_frame,
    encode_image_file,
    make_test_frame,
    probe_dimensions,
)
from unitree_robot_agent.vision.sources import (
    FileSource,
    FrameSource,
    PhotoApiSource,
    RtspSource,
    WebRTCSource,
)

from .conftest import make_config


def runtime_for(**overrides):
    return create_runtime(make_config(**overrides))


# ─── encoding (the path that had a real bug) ──────────────────────────


def test_encoded_frame_is_a_bare_jpeg():
    """Regression: the first implementation returned an *empty* buffer with no
    error at all, because `image2` is a file muxer and `mpjpeg` adds multipart
    boundaries. The payload must start with the JPEG magic."""
    payload, width, height = encode_frame(make_test_frame(320, 240))
    assert payload is not None
    assert payload[:3] == b"\xff\xd8\xff"
    assert sniff_image_format(payload) == "jpeg"
    assert (width, height) == (320, 240)
    assert len(payload) > 300


def test_encoding_downscales_before_encoding():
    payload, width, height = encode_frame(make_test_frame(1920, 1080), max_width=1280)
    assert (width, height) == (1280, 720)
    assert probe_dimensions(payload) == (1280, 720)


def test_round_trip_reencode():
    original, _, _ = encode_frame(make_test_frame(800, 600))
    again, width, height = encode_buffer(original, max_width=400)
    assert (width, height) == (400, 300)
    assert sniff_image_format(again) == "jpeg"


def test_format_sniffing_and_inline_decoding():
    payload, _, _ = encode_frame(make_test_frame(64, 64))
    assert sniff_image_format(payload) == "jpeg"
    assert sniff_image_format(b"\x89PNG\r\n\x1a\n" + b"0" * 32) == "png"
    assert sniff_image_format(b"nope") is None

    inline = "data:image/jpeg;base64," + base64.b64encode(payload).decode()
    decoded = decode_inline_image(inline)
    assert decoded is not None and sniff_image_format(decoded) == "jpeg"
    assert decode_inline_image("not an image") is None
    assert decode_inline_image("data:image/jpeg;base64," + base64.b64encode(b"xx").decode()) is None


def test_encode_image_file(tmp_path):
    source = tmp_path / "frame.jpg"
    payload, _, _ = encode_frame(make_test_frame(200, 100))
    source.write_bytes(payload)
    out, width, height = encode_image_file(str(source), max_width=100)
    assert out is not None and (width, height) == (100, 50)
    missing, _, _ = encode_image_file(str(tmp_path / "nope.jpg"))
    assert missing is None


# ─── sources ──────────────────────────────────────────────────────────


class SlowSource(FrameSource):
    name = "slow"

    async def run(self):
        await asyncio.sleep(5)
        return b"never", 0, 0, ""


class BrokenSource(FrameSource):
    name = "broken"

    async def run(self):
        raise RuntimeError("boom")


async def test_source_timeout_and_exception_are_reported_not_raised():
    timed = await SlowSource().grab(timeout=0.05)
    assert timed.ok is False and "within" in timed.error

    crashed = await BrokenSource().grab(timeout=1.0)
    assert crashed.ok is False and "RuntimeError" in crashed.error


async def test_webrtc_source_uses_the_backend_frame():
    runtime = runtime_for(robot="R1")
    result = await WebRTCSource(runtime.backend).grab(timeout=3.0)
    assert result.ok is True
    assert result.format == "jpeg"
    await runtime.aclose()


async def test_webrtc_source_explains_a_camera_less_robot():
    runtime = runtime_for(robot="R1")
    runtime.backend.has_camera = False
    result = await WebRTCSource(runtime.backend).grab(timeout=2.0)
    assert result.ok is False
    assert "no video track" in result.note
    await runtime.aclose()


async def test_photo_api_source_finds_the_serving_api_id():
    runtime = runtime_for(robot="R1")
    # Sources assume a live link; VisionService establishes it before trying
    # remote sources, so a direct call has to do the same.
    await runtime.session.ensure_connected()
    source = PhotoApiSource(runtime.backend, api_ids=[1005, 1001])
    result = await source.grab(timeout=5.0)
    assert result.ok is True
    assert "api_id=1001" in result.note
    await runtime.aclose()


async def test_photo_api_source_reports_every_code_it_tried():
    runtime = runtime_for(robot="R1")
    await runtime.session.ensure_connected()
    runtime.backend.has_camera = False
    result = await PhotoApiSource(runtime.backend, api_ids=[1001, 1002]).grab(timeout=5.0)
    assert result.ok is False
    assert "1001:code=127" in result.note and "1002:code=127" in result.note
    await runtime.aclose()


async def test_rtsp_without_a_url_says_so():
    result = await RtspSource().grab(timeout=1.0)
    assert result.ok is False
    assert "UNITREE_AGENT_RTSP_URL" in result.note


async def test_file_source_reads_an_image(tmp_path):
    path = tmp_path / "shot.jpg"
    payload, _, _ = encode_frame(make_test_frame(300, 200))
    path.write_bytes(payload)
    result = await FileSource(str(path)).grab(timeout=2.0)
    assert result.ok is True and (result.width, result.height) == (300, 200)


async def test_file_source_reads_a_video(tmp_path):
    """The video branch had to be exercised too — it decodes the first frame."""
    import av

    path = tmp_path / "clip.mp4"
    container = av.open(str(path), mode="w")
    stream = container.add_stream("mpeg4", rate=5)
    stream.width, stream.height = 160, 120
    for _ in range(5):
        for packet in stream.encode(make_test_frame(160, 120)):
            container.mux(packet)
    for packet in stream.encode(None):
        container.mux(packet)
    container.close()

    result = await FileSource(str(path)).grab(timeout=5.0)
    assert result.ok is True
    assert (result.width, result.height) == (160, 120)


async def test_file_source_picks_the_newest_in_a_directory(tmp_path):
    old = tmp_path / "old.jpg"
    new = tmp_path / "new.jpg"
    old.write_bytes(encode_frame(make_test_frame(100, 100))[0])
    time.sleep(0.02)
    new.write_bytes(encode_frame(make_test_frame(200, 200))[0])
    result = await FileSource(str(tmp_path)).grab(timeout=2.0)
    assert result.ok is True
    assert (result.width, result.height) == (200, 200)


async def test_file_source_missing_path_is_actionable(tmp_path):
    result = await FileSource(str(tmp_path / "nope")).grab(timeout=1.0)
    assert result.ok is False and "no file found" in result.note


# ─── service: ordering, archive, history ──────────────────────────────


async def test_auto_prefers_webrtc_then_photo_api(tmp_path):
    runtime = runtime_for(robot="R1", state_dir=tmp_path)
    result = await runtime.vision.capture("auto", timeout=4.0)
    assert result.ok is True
    assert result.source == "webrtc"
    await runtime.aclose()


async def test_configured_video_source_is_used_by_default(tmp_path):
    """UNITREE_AGENT_VIDEO_SOURCE used to be read by nobody: an operator who
    pinned "file" still got the WebRTC track."""
    shot = tmp_path / "shot.jpg"
    shot.write_bytes(encode_frame(make_test_frame(120, 80))[0])

    runtime = runtime_for(video_source="file", snapshot_file=str(shot), state_dir=tmp_path)
    result = await runtime.vision.capture(timeout=3.0, save=False)
    assert result.ok is True
    assert result.source == "file"

    # An explicit argument still overrides the configured default.
    explicit = await runtime.vision.capture("webrtc", timeout=3.0, save=False)
    assert explicit.source == "webrtc"
    await runtime.aclose()


async def test_probe_honours_its_timeout(tmp_path, monkeypatch):
    """The probe window used to be a hard-coded 6 s per source, so
    `timeout_s` on robot_vision_probe did nothing."""
    runtime = runtime_for(robot="R1", state_dir=tmp_path)
    monkeypatch.setattr(capture_module, "AUTO_ORDER", ("slow",))
    runtime.vision.build_sources = lambda: {"slow": SlowSource()}

    started = time.monotonic()
    statuses = await runtime.vision.probe(timeout=0.2)
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, elapsed  # not the source's own 5 s, not the old 6 s
    assert [s.name for s in statuses] == ["slow"]
    assert statuses[0].available is False
    assert "within" in statuses[0].detail  # it timed out on the budget given
    # The budget is the caller's, floored at 1 s (so a tiny window cannot
    # fail every source instantly) and capped at the 6 s default.
    assert runtime.vision._probe_window(0.2) == 1.0
    assert runtime.vision._probe_window(3.0) == 3.0
    assert runtime.vision._probe_window(99.0) == 6.0
    await runtime.aclose()


async def test_capture_archives_and_history_reads_back(tmp_path):
    runtime = runtime_for(robot="R1", state_dir=tmp_path)
    result = await runtime.vision.capture("auto", timeout=4.0, save=True)
    assert result.saved_path is not None

    from pathlib import Path

    assert Path(result.saved_path).is_file()

    records = runtime.vision.history(5)
    assert len(records) == 1
    record = records[0]
    assert record["source"] == "webrtc"
    assert record["width"] == result.width
    assert record["exists"] is True
    await runtime.aclose()


async def test_capture_can_skip_the_archive(tmp_path):
    runtime = runtime_for(robot="R1", state_dir=tmp_path)
    result = await runtime.vision.capture("auto", timeout=4.0, save=False)
    assert result.ok is True
    assert result.saved_path is None
    assert runtime.vision.history(5) == []
    await runtime.aclose()


async def test_unknown_source_name_is_explained(tmp_path):
    runtime = runtime_for(robot="R1", state_dir=tmp_path)
    result = await runtime.vision.capture("telepathy", timeout=1.0)
    assert result.ok is False
    assert "telepathy" in result.error
    assert "webrtc" in result.note
    await runtime.aclose()


async def test_probe_reports_every_source_including_unconfigured(tmp_path):
    runtime = runtime_for(robot="R1", state_dir=tmp_path)
    statuses = await runtime.vision.probe(timeout=3.0)
    by_name = {s.name: s for s in statuses}
    assert set(by_name) >= {"webrtc", "photo_api", "rtsp", "device_camera", "file"}
    assert by_name["webrtc"].available is True
    assert by_name["rtsp"].available is False
    assert "UNITREE_AGENT_RTSP_URL" in by_name["rtsp"].detail
    await runtime.aclose()


async def test_history_is_empty_before_anything_is_captured(tmp_path):
    runtime = runtime_for(robot="R1", state_dir=tmp_path)
    assert runtime.vision.history(5) == []
    await runtime.aclose()


# ─── through the tool layer ───────────────────────────────────────────


def bundle_for(tmp_path, **overrides):
    return build_server(create_runtime(make_config(state_dir=tmp_path, **overrides)))


async def test_snapshot_tool_returns_image_content(tmp_path):
    bundle = bundle_for(tmp_path)
    result = await _invoke(bundle, "robot_snapshot", {"timeout_s": 5.0})
    assert type(result).__name__ == "Image"
    assert result.data[:3] == b"\xff\xd8\xff"
    await bundle.context.runtime.aclose()


async def test_snapshot_tool_reports_a_camera_less_robot_clearly(tmp_path):
    bundle = bundle_for(tmp_path)
    bundle.context.backend.has_camera = False
    result = await _invoke(bundle, "robot_snapshot", {"timeout_s": 3.0})
    assert result["ok"] is False
    assert result["error"] == "no_frame"
    assert "robot_vision_probe" in result["hint"]
    await bundle.context.runtime.aclose()


async def test_vision_probe_tool_lists_sources(tmp_path):
    bundle = bundle_for(tmp_path)
    result = await _invoke(bundle, "robot_vision_probe", {"timeout_s": 3.0})
    assert result["ok"] is True
    assert "webrtc" in result["working"]
    assert isinstance(result["sources"], list)
    await bundle.context.runtime.aclose()


async def test_snapshot_history_tool_exposes_paths(tmp_path):
    bundle = bundle_for(tmp_path)
    await _invoke(bundle, "robot_snapshot", {"timeout_s": 5.0})
    result = await _invoke(bundle, "robot_snapshot_history", {"limit": 5})
    assert result["count"] == 1
    assert result["records"][0]["path"].endswith((".jpg", ".jpeg"))
    await bundle.context.runtime.aclose()


async def test_snapshot_tool_honours_max_width(tmp_path):
    bundle = bundle_for(tmp_path)
    result = await _invoke(bundle, "robot_snapshot", {"timeout_s": 5.0, "max_width": 320})
    assert type(result).__name__ == "Image"
    assert probe_dimensions(result.data)[0] == 320
    await bundle.context.runtime.aclose()
