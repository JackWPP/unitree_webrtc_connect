"""WebRTC backend internals, exercised without a robot.

The driver is replaced by stand-ins, so the tests pin the agent's own
handling of the driver's quirks: JSON-string payloads in the cache, a
subscribe that silently did not register, and a connect that gets cancelled
(or interrupted) before the handshake completes.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from unitree_robot_agent.backends.webrtc_backend import WebRTCBackend

from .conftest import make_config


class FakePubSub:
    """Mimics the driver's pub/sub, including its print-and-return when the
    channel is not open: ``subscribe`` only registers while open."""

    def __init__(self, *, open: bool = True) -> None:
        self.open = open
        self.subscriptions: dict = {}
        self.published: list = []

    def subscribe(self, topic, callback) -> None:
        if self.open:
            self.subscriptions[topic] = callback

    def publish_without_callback(self, topic, data=None, msg_type=None) -> None:
        self.published.append((topic, data, msg_type))


class FakeDatachannel:
    def __init__(self, pub_sub=None) -> None:
        self.pub_sub = pub_sub or FakePubSub()
        self.data_channel_opened = True


class FakeConn:
    isConnected = True

    def __init__(self, pub_sub=None) -> None:
        self.datachannel = FakeDatachannel(pub_sub)


def backend_for(tmp_path) -> WebRTCBackend:
    return WebRTCBackend(
        make_config(robot="Go2", backend="webrtc", connection="ap", state_dir=tmp_path)
    )


def test_json_string_payloads_are_decoded(tmp_path):
    """On the real robot `rt/multiplestate` arrives as a JSON *string* inside
    the envelope; the tools need the object, not the string."""
    backend = backend_for(tmp_path)
    backend.conn = FakeConn()
    backend._cache["rt/multiplestate"] = {
        "msg": {"type": "x", "topic": "rt/multiplestate", "data": '{"volume": 5}'},
        "at": time.time(),
    }
    assert backend._get_cached("rt/multiplestate", max_age=None) == {"volume": 5}

    # An object payload still passes straight through …
    backend._cache["rt/obj"] = {
        "msg": {"type": "x", "topic": "rt/obj", "data": {"volume": 7}},
        "at": time.time(),
    }
    assert backend._get_cached("rt/obj", max_age=None)["volume"] == 7

    # … and an undecodable one falls back to the raw envelope.
    backend._cache["rt/bad"] = {
        "msg": {"type": "x", "topic": "rt/bad", "data": "not json"},
        "at": time.time(),
    }
    assert backend._get_cached("rt/bad", max_age=None)["topic"] == "rt/bad"


def test_a_topic_is_only_latched_when_the_driver_registered_it(tmp_path):
    backend = backend_for(tmp_path)
    pub_sub = FakePubSub(open=False)
    backend.conn = FakeConn(pub_sub)

    backend._subscribe("rt/x")
    assert "rt/x" not in pub_sub.subscriptions
    assert backend._subscribed == set(), "a failed subscribe must stay retryable"

    pub_sub.open = True
    backend._subscribe("rt/x")
    assert pub_sub.subscriptions["rt/x"]
    assert backend._subscribed == {"rt/x"}


def test_publish_reports_whether_the_frame_reached_the_transport(tmp_path):
    backend = backend_for(tmp_path)
    assert backend.publish("rt/wirelesscontroller", {"ly": 0.0}) is False

    pub_sub = FakePubSub()
    backend.conn = FakeConn(pub_sub)
    assert backend.publish("rt/wirelesscontroller", {"ly": 0.0}) is True

    backend.conn.datachannel.data_channel_opened = False
    assert backend.publish("rt/wirelesscontroller", {"ly": 0.5}) is False
    assert pub_sub.published == [("rt/wirelesscontroller", {"ly": 0.0}, None)]


async def test_a_cancelled_connect_releases_the_client_slot(tmp_path, monkeypatch):
    """The robot accepts one WebRTC client. A connect that is cancelled
    (server shutdown, tool timeout) used to leave the driver's half-built
    client attached, locking the owner out of the official app."""

    class HangingConnection:
        isConnected = False

        def __init__(self, *args, **kwargs) -> None:
            self.datachannel = FakeDatachannel()
            self.disconnects = 0
            self.started: asyncio.Event | None = None

        async def connect(self) -> None:
            self.started = asyncio.Event()
            self.started.set()
            await asyncio.Event().wait()  # never completes

        async def disconnect(self) -> None:
            self.disconnects += 1

    conn = HangingConnection()
    import unitree_webrtc_connect

    monkeypatch.setattr(unitree_webrtc_connect, "UnitreeWebRTCConnection", lambda *a, **k: conn)

    backend = backend_for(tmp_path)
    task = asyncio.create_task(backend.connect())
    for _ in range(200):
        if conn.started is not None:
            break
        await asyncio.sleep(0.01)
    assert conn.started is not None, "connect never reached the driver"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert conn.disconnects == 1, "the cancelled client must be released"
    assert backend.conn is None
