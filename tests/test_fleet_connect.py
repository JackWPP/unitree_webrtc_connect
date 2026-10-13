"""Connect isolation: one handshake at a time, and no blocking on the loop.

Both properties exist because of the same failure. ``discover_ip_sn`` binds
UDP 10134 with ``SO_REUSEADDR`` and collects every reply without correlating
it to the request, so two concurrent discoveries can hand each robot the
other's address. And the driver's handshake does blocking I/O on the shared
event loop, so connecting one robot freezes every other robot's control loop
for the duration.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from unitree_robot_agent.fleet.connect import (
    ConnectGate,
    exclusive_connect,
    resolve_target,
    reset_default_gate,
)


class FakeConfig:
    """Just the attributes ``resolve_target`` reads."""

    def __init__(self, *, ip=None, serial_number=None, device_type="Go2", robot="Go2"):
        self.ip = ip
        self.serial_number = serial_number
        self.device_type = device_type
        self.robot = robot


# ─── the gate ─────────────────────────────────────────────────────────


async def test_only_one_handshake_runs_at_a_time():
    gate = ConnectGate(stagger_s=0.0)
    order = []

    async def worker(name: str, hold_s: float) -> None:
        async with gate.hold(name):
            order.append(f"{name}:in")
            await asyncio.sleep(hold_s)
            order.append(f"{name}:out")

    await asyncio.gather(worker("a", 0.05), worker("b", 0.01))

    # Interleaving would show up as a:in, b:in, ...
    assert order == ["a:in", "a:out", "b:in", "b:out"]


async def test_the_gate_survives_a_failing_handshake():
    gate = ConnectGate(stagger_s=0.0)

    with pytest.raises(RuntimeError):
        async with gate.hold("a"):
            raise RuntimeError("handshake blew up")

    # The lock must have been released, or the fleet would deadlock.
    async with gate.hold("b"):
        pass
    assert "b" in gate.stats


async def test_consecutive_connects_are_staggered():
    gate = ConnectGate(stagger_s=0.2)

    async with gate.hold("a"):
        pass
    started = time.monotonic()
    async with gate.hold("b"):
        pass
    assert time.monotonic() - started >= 0.15


async def test_handshake_times_are_recorded_per_robot():
    gate = ConnectGate(stagger_s=0.0)
    async with gate.hold("dog1"):
        await asyncio.sleep(0.02)
    assert gate.stats["dog1"] >= 0.02


async def test_the_default_gate_is_shared_process_wide():
    """Two fleets in one process must not each think they are alone."""
    reset_default_gate()
    order = []

    async def worker(name: str) -> None:
        async with exclusive_connect(name):
            order.append(f"{name}:in")
            await asyncio.sleep(0.02)
            order.append(f"{name}:out")

    await asyncio.gather(worker("a"), worker("b"))
    assert order == ["a:in", "a:out", "b:in", "b:out"]


# ─── target resolution ────────────────────────────────────────────────


async def test_a_configured_ip_is_used_without_scanning(monkeypatch):
    called = []

    def scan(**kwargs):
        called.append(kwargs)
        return {}

    monkeypatch.setattr(
        "unitree_webrtc_connect.multicast_scanner.discover_ip_sn", scan
    )
    assert await resolve_target(FakeConfig(ip="192.168.31.245")) == "192.168.31.245"
    assert called == [], "a known IP must not trigger a scan"


async def test_nothing_to_resolve_returns_empty():
    assert await resolve_target(FakeConfig()) == ""


async def test_a_serial_number_is_resolved_to_an_address(monkeypatch):
    def scan(timeout=None, device_type=None, sn=None):
        return {"B42D2000AAAA": "192.168.31.245"}

    monkeypatch.setattr(
        "unitree_webrtc_connect.multicast_scanner.discover_ip_sn", scan
    )
    config = FakeConfig(serial_number="B42D2000AAAA")
    assert await resolve_target(config) == "192.168.31.245"


async def test_a_silent_network_is_a_miss_not_an_error(monkeypatch):
    monkeypatch.setattr(
        "unitree_webrtc_connect.multicast_scanner.discover_ip_sn",
        lambda **kwargs: {},
    )
    assert await resolve_target(FakeConfig(serial_number="NOPE")) == ""


async def test_a_failing_scan_is_a_miss_not_an_error(monkeypatch):
    def scan(**kwargs):
        raise OSError("network is down")

    monkeypatch.setattr(
        "unitree_webrtc_connect.multicast_scanner.discover_ip_sn", scan
    )
    assert await resolve_target(FakeConfig(serial_number="B42")) == ""


async def test_the_scan_does_not_block_the_event_loop(monkeypatch):
    """The whole point: a discovery wait must not freeze other robots.

    A heartbeat task runs alongside the scan. If the scan were awaited
    directly on the loop, the heartbeat could not advance while it ran.
    """
    ticks = 0

    def scan(timeout=None, device_type=None, sn=None):
        time.sleep(0.3)  # blocking, exactly like the real socket wait
        return {"B42D2000AAAA": "192.168.31.245"}

    monkeypatch.setattr(
        "unitree_webrtc_connect.multicast_scanner.discover_ip_sn", scan
    )

    async def heartbeat():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    try:
        resolved = await resolve_target(FakeConfig(serial_number="B42D2000AAAA"))
    finally:
        beat.cancel()

    assert resolved == "192.168.31.245"
    # 0.3s of blocking scan, ticking every 10ms: a loop-blocking scan leaves
    # this at 0 or 1. Off-loop it should be around 30.
    assert ticks >= 10, f"event loop was blocked during discovery ({ticks} ticks)"
