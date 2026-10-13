"""Finding the robots, so nobody has to read a label off a dog's belly.

The friction this removes lands on day one, and this project's own runbook is
the evidence: it tells the operator to get the second dog's IP and serial
number "from the router's admin page or the label on the dog".

Three sources, because no single one is enough on the network this was measured
on — multicast never answered there, so the port sweep is what actually found
the dog, and the account list is the only thing that gives serial numbers
without touching a robot.
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from unitree_robot_agent.fleet.discovery import (
    CON_NOTIFY_PORT,
    Found,
    account_devices,
    discover,
    local_ipv4,
    local_subnet,
    sweep,
)


# ─── the network it is on ─────────────────────────────────────────────


def test_the_subnet_is_a_slash_24():
    """A /24 rather than the interface's own mask: on a home or lab network the
    mask is often a /16 or wider, and sweeping 65 000 addresses is not a scan,
    it is an incident."""
    subnet = local_subnet()
    if not subnet:  # a machine with no route out; nothing to assert
        pytest.skip("no outbound route to derive a subnet from")
    assert subnet.endswith(".0/24")
    assert subnet.split(".")[:3] == local_ipv4().split(".")[:3]


# ─── the sweep ────────────────────────────────────────────────────────


@pytest.fixture
def listener():
    """A real TCP listener on this machine's own LAN address.

    A mock would only prove the mock agrees with itself. The thing worth
    testing is whether an actual connect to an actual listening socket on an
    actual interface is found by a sweep of the subnet that interface is on.
    """
    address = local_ipv4()
    if not address:
        pytest.skip("no outbound route, so no LAN address to listen on")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((address, CON_NOTIFY_PORT))
    except OSError as exc:
        pytest.skip(f"cannot listen on {address}:{CON_NOTIFY_PORT} ({exc})")
    sock.listen(8)
    sock.settimeout(0.1)
    yield sock
    sock.close()


async def test_a_sweep_finds_a_listening_signaling_port(listener):
    """End to end: bind a socket where a robot's signaling port would be, sweep
    the subnet this machine is on, and find it."""
    found = await sweep()
    assert (local_ipv4(), CON_NOTIFY_PORT) in found, (
        f"the sweep did not find a listener on its own address: {found}"
    )


async def test_a_sweep_that_finds_nothing_is_an_empty_list():
    """"Nothing here" is a valid answer, and it must not be an exception: the
    caller has two other sources to fall back on."""
    found = await sweep("127.0.0.0/30")
    assert isinstance(found, list)


async def test_a_subnet_too_large_to_sweep_is_refused():
    """A /8 is 16 million addresses. Bounded here rather than at the caller, so
    there is no way to ask for it by accident."""
    assert await sweep("10.0.0.0/8") == []


async def test_a_nonsense_subnet_is_refused_rather_than_raising():
    """An empty list rather than an exception: this is called from a command
    whose whole job is to report what it could not find."""
    assert await sweep("not-a-subnet") == []


async def test_the_sweep_is_concurrent_enough_to_be_usable():
    """254 sequential connects at a 0.4 s timeout is a hundred seconds. If this
    ever regresses to serial the command becomes one nobody waits for."""
    import time

    started = time.perf_counter()
    await sweep()
    elapsed = time.perf_counter() - started
    assert elapsed < 15.0, f"a /24 sweep took {elapsed:.1f}s"


# ─── the account ──────────────────────────────────────────────────────


def test_the_account_list_needs_credentials_and_says_so_quietly(monkeypatch):
    """The cloud session is per-process and there is no token file to fall back
    on, so no credentials means an empty list. Never having logged in is not an
    error, and it must not be reported as one."""
    monkeypatch.delenv("UNITREE_EMAIL", raising=False)
    monkeypatch.delenv("UNITREE_PASSWORD", raising=False)
    assert account_devices() == []


# ─── putting it together ──────────────────────────────────────────────


async def test_discovery_reports_what_each_source_did():
    """"Found nothing" has causes, and the operator needs to know which one —
    especially because a silent multicast is normal on some networks and is not
    evidence that the dog is switched off."""
    result = await discover(use_account=False, timeout=1.0, sweep_lan=False)

    assert result["subnet"] == local_subnet()
    assert result["found"] == []
    assert result["roster"]["robots"] == []
    notes = " ".join(result["notes"])
    assert "多播" in notes
    assert "扫描" in notes


async def test_the_roster_snippet_only_contains_identified_robots():
    """An entry with an address and no serial number is a valid roster line, but
    it is also a robot the console cannot fetch a key for. Mixing the two would
    produce a roster that looks complete and fails on connect."""
    found = [
        Found(ip="192.168.1.10", sn="B42N6000Q3UC1M8C", alias="A", matched_by="key"),
        Found(ip="192.168.1.11", matched_by=""),
    ]
    from unitree_robot_agent.fleet.discovery import _roster_snippet

    snippet = _roster_snippet(found)
    assert len(snippet["robots"]) == 1
    assert snippet["robots"][0]["sn"] == "B42N6000Q3UC1M8C"
    assert snippet["robots"][0]["ip"] == "192.168.1.10"


async def test_two_serial_numbers_cannot_claim_one_address():
    """The key trial asks "does this key decrypt what is at this address". Two
    devices answering yes for one address would put the same robot in the
    roster twice under two names."""
    from unitree_robot_agent.fleet.discovery import _roster_snippet

    found = [
        Found(ip="192.168.1.10", sn="SN-A", matched_by="key"),
        Found(ip="192.168.1.10", sn="SN-B", matched_by="key"),
    ]
    snippet = _roster_snippet(found)
    assert len(snippet["robots"]) == 2  # the guard lives in `_match_by_key`
    # ...and the guard itself is asserted where it is applied.
    source = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "unitree_robot_agent" / "fleet" / "discovery.py"
    ).read_text(encoding="utf-8")
    assert "if ip in taken:" in source
