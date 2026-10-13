"""Chunked file upload: it must actually send, and must not lie about it.

The original `upload_file` called `self.publish(...)` **without await**.
`publish` is a coroutine, so that built a coroutine object and discarded it:
`publish` was never entered, no chunk was ever transmitted, and the method
still returned `"ok"`. A SLAM map "upload" moved zero bytes and reported
success — the worst possible shape for a bug, because nothing downstream has
any reason to doubt it.

These tests fail against that implementation, which is the point.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from unitree_webrtc_connect.msgs.rtc_inner_req import (
    FileUploadError,
    WebRTCDataChannelFileUploader,
    _file_status,
)


class FakeChannel:
    readyState = "open"


class FakePubSub:
    """Stands in for WebRTCDataChannelPubSub.

    Records every publish and replies with a canned acknowledgement, so the
    uploader's own behaviour is what is under test.
    """

    def __init__(self, *, status="ok", delay=0.0, hang_from=None):
        self.status = status
        self.delay = delay
        self.hang_from = hang_from
        self.calls = []
        self.responded = 0

    async def publish(self, topic, data=None, msg_type=None):
        self.calls.append((topic, data, msg_type))
        index = len(self.calls)
        if self.hang_from is not None and index >= self.hang_from:
            await asyncio.sleep(3600)  # never acknowledges
        if self.delay:
            await asyncio.sleep(self.delay)
        self.responded += 1
        return {"info": {"file_status": self.status, "req_uuid": (data or {}).get("req_uuid")}}


def uploader(**kwargs):
    pub_sub = FakePubSub(**kwargs)
    return WebRTCDataChannelFileUploader(FakeChannel(), pub_sub), pub_sub


# ─── the regression ───────────────────────────────────────────────────


async def test_every_chunk_is_actually_published():
    """Regression: nothing was sent at all, yet the call returned "ok"."""
    up, pub_sub = uploader()
    payload = b"x" * 2000

    result = await up.upload_file(payload, "/unitree/test.pcd", chunk_size=256)

    assert result == "ok"
    assert len(pub_sub.calls) > 1, "the payload should span several chunks"
    # One publish per chunk, and every one of them carries chunk data.
    for _topic, data, _msg_type in pub_sub.calls:
        assert data["req_type"] == "push_static_file"
        assert data["file"]["chunk_data"]


async def test_the_number_of_chunks_matches_what_the_payload_needs():
    up, pub_sub = uploader()
    payload = b"x" * 1024

    await up.upload_file(payload, "/unitree/test.pcd", chunk_size=200, ack_timeout=1.0)

    encoded_len = len(pub_sub.calls[0][1]["file"]["chunk_data"])
    assert encoded_len <= 200
    assert pub_sub.calls[-1][1]["file"]["total_chunk_num"] == len(pub_sub.calls)
    assert [c[1]["file"]["chunk_index"] for c in pub_sub.calls] == list(
        range(1, len(pub_sub.calls) + 1)
    )


async def test_each_chunk_uses_a_distinct_request_id():
    """The ack is matched by `req_uuid`; reusing one would let chunk 1's reply
    satisfy chunk 2."""
    up, pub_sub = uploader()
    await up.upload_file(b"x" * 2000, "/unitree/test.pcd", chunk_size=256)

    ids = [c[1]["req_uuid"] for c in pub_sub.calls]
    assert len(set(ids)) == len(ids)


async def test_there_is_no_breather_before_the_first_chunk():
    """`i % 5 == 0` is true at i == 0, which added half a second of pure
    latency to every upload — including single-chunk ones."""
    up, pub_sub = uploader()

    started = time.monotonic()
    await up.upload_file(b"x" * 100, "/unitree/test.pcd", chunk_size=1000)
    elapsed = time.monotonic() - started

    assert len(pub_sub.calls) == 1
    assert elapsed < 0.3, f"a one-chunk upload took {elapsed:.2f}s"


# ─── failures are reported, not swallowed ─────────────────────────────


async def test_a_rejected_chunk_raises():
    up, _ = uploader(status="failed")
    with pytest.raises(FileUploadError, match="was rejected"):
        await up.upload_file(b"x" * 100, "/unitree/test.pcd", chunk_size=64)


async def test_an_unacknowledged_chunk_times_out_and_raises():
    """A partial upload must not be reported as a finished one."""
    up, pub_sub = uploader(hang_from=2)
    with pytest.raises(FileUploadError, match="not acknowledged"):
        await up.upload_file(
            b"x" * 300, "/unitree/test.pcd", chunk_size=100, ack_timeout=0.2
        )
    assert pub_sub.responded == 1  # chunk 1 acked, chunk 2 never did


async def test_a_failure_names_the_file_and_the_chunk():
    up, _ = uploader(status="failed")
    with pytest.raises(FileUploadError) as exc:
        await up.upload_file(b"x" * 300, "/unitree/module/map.pcd", chunk_size=100)
    message = str(exc.value)
    assert "/unitree/module/map.pcd" in message
    assert "1/" in message


async def test_a_reply_without_a_status_is_still_an_acknowledgement():
    """The envelope has not been confirmed against hardware here, so a reply
    we do not recognise must not be treated as a rejection — the guarantee
    that matters is that the robot answered for every chunk."""
    up, pub_sub = uploader()
    pub_sub.status = None
    assert await up.upload_file(b"x" * 100, "/unitree/test.pcd", chunk_size=64) == "ok"


# ─── cancellation and progress ────────────────────────────────────────


async def test_cancel_stops_the_upload():
    up, pub_sub = uploader(delay=0.01)

    async def cancel_soon():
        await asyncio.sleep(0.03)
        up.cancel()

    asyncio.create_task(cancel_soon())
    result = await up.upload_file(b"x" * 20000, "/unitree/test.pcd", chunk_size=64)

    assert result == "cancel"
    assert len(pub_sub.calls) < 100


async def test_progress_is_reported_per_chunk_and_ends_at_100():
    seen = []
    up, pub_sub = uploader()
    await up.upload_file(
        b"x" * 1000,
        "/unitree/test.pcd",
        chunk_size=200,
        progress_callback=seen.append,
    )

    assert len(seen) == len(pub_sub.calls)
    assert seen[-1] == 100
    assert seen == sorted(seen)


# ─── the acknowledgement parser ───────────────────────────────────────


@pytest.mark.parametrize(
    "response,expected",
    [
        ({"file_status": "ok"}, "ok"),
        ({"info": {"file_status": "ok"}}, "ok"),
        ({"data": {"file_status": "ok"}}, "ok"),
        ({"info": {"file_status": "failed"}}, "failed"),
        ({"info": {"something": "else"}}, None),
        ({}, None),
        (None, None),
        ("not a dict", None),
    ],
)
def test_file_status_reads_the_shapes_we_know(response, expected):
    assert _file_status(response) == expected


# ─── the download half ────────────────────────────────────────────────


def _downloader(response):
    class FakePubSub:
        async def publish(self, topic, data=None, msg_type=None):
            return response

    from unitree_webrtc_connect.msgs.rtc_inner_req import WebRTCDataChannelFileDownloader

    return WebRTCDataChannelFileDownloader(FakeChannel(), FakePubSub())


async def test_a_download_returns_the_decoded_bytes():
    import base64 as b64

    payload = b"\x00\x01binary file contents"
    encoded = b64.b64encode(payload).decode()
    up = _downloader({"info": {"file": {"data": encoded}}})

    assert await up.download_file("/unitree/map.pcd") == payload


async def test_a_download_failure_raises_instead_of_returning_a_string():
    """The success value is `bytes`; returning "error" meant a caller that
    skipped the type check would write that word to a file and move on."""
    from unitree_webrtc_connect import FileDownloadError

    up = _downloader({"info": {"file": {}}})
    with pytest.raises(FileDownloadError, match="no data"):
        await up.download_file("/unitree/map.pcd")


async def test_a_download_that_blows_up_raises_with_the_path():
    from unitree_webrtc_connect import FileDownloadError
    from unitree_webrtc_connect.msgs.rtc_inner_req import WebRTCDataChannelFileDownloader

    class Exploding:
        async def publish(self, topic, data=None, msg_type=None):
            raise RuntimeError("link dropped")

    up = WebRTCDataChannelFileDownloader(FakeChannel(), Exploding())
    with pytest.raises(FileDownloadError, match="/unitree/map.pcd"):
        await up.download_file("/unitree/map.pcd")


async def test_a_download_reports_progress_once_complete():
    import base64 as b64

    seen = []
    up = _downloader({"info": {"file": {"data": b64.b64encode(b"x").decode()}}})
    await up.download_file("/unitree/map.pcd", progress_callback=seen.append)
    assert seen == [100]


async def test_a_cancelled_download_says_so():
    """Cancellation happens *during* the request, from another task.

    `download_file` resets the flag on entry, so setting it beforehand is
    wiped — which is correct: the flag means "cancel the transfer in flight".
    """
    import asyncio

    from unitree_webrtc_connect.msgs.rtc_inner_req import WebRTCDataChannelFileDownloader

    class SlowPubSub:
        async def publish(self, topic, data=None, msg_type=None):
            await asyncio.sleep(0.05)
            return {"info": {"file": {"data": ""}}}

    up = WebRTCDataChannelFileDownloader(FakeChannel(), SlowPubSub())
    downloading = asyncio.create_task(up.download_file("/unitree/map.pcd"))
    await asyncio.sleep(0.01)
    up.cancel()
    assert await downloading == "cancel"


# ─── the resolver that stitches the chunks ────────────────────────────


def test_the_resolver_joins_file_chunks_into_one_blob():
    """No coverage existed for the chunk stitching either.

    The downloader only ever sees the *joined* payload, so a resolver that
    dropped the last chunk or appended out of order would corrupt every map
    with nothing to catch it.

    Note the protocol shape, which is easy to get wrong: the robot base64s the
    whole file and then slices that base64 *string* into chunks. It does not
    base64 each chunk independently, so the chunks must be concatenated before
    decoding — which is exactly what the resolver does.
    """
    import base64 as b64

    from unitree_webrtc_connect.msgs.future_resolver import FutureResolver

    payload = bytes(range(256)) * 40
    encoded = b64.b64encode(payload).decode()
    size = len(encoded) // 3 + 1
    chunks = [encoded[i:i + size] for i in range(0, len(encoded), size)]
    assert len(chunks) == 3

    resolver = FutureResolver()
    message = None
    for index, chunk in enumerate(chunks, start=1):
        message = {
            "type": "rtc_inner_req",
            "topic": "",
            "info": {
                "req_uuid": "req_abc",
                "req_type": "request_static_file",
                "file": {
                    "enable_chunking": True,
                    "chunk_index": index,
                    "total_chunk_num": len(chunks),
                    "data": chunk,
                },
            },
        }
        resolver.run_resolve_for_topic(message)

    joined = message["info"]["file"]["data"]
    assert b64.b64decode(joined) == payload
    # The per-request storage is cleaned up.
    assert resolver.chunk_data_storage == {}


def test_the_resolver_rejects_a_zero_chunk_count():
    from unitree_webrtc_connect.msgs.future_resolver import FutureResolver

    resolver = FutureResolver()
    with pytest.raises(ValueError, match="cannot be zero"):
        resolver.run_resolve_for_topic(
            {
                "type": "rtc_inner_req",
                "topic": "",
                "info": {
                    "req_uuid": "x",
                    "req_type": "request_static_file",
                    "file": {"enable_chunking": True, "chunk_index": 1, "total_chunk_num": 0},
                },
            }
        )
