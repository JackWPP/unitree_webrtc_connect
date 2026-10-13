"""The connect-interference instrument, tested against a known-bad stub.

The measurement exists because the hazard it looks for — a blocking handshake
freezing every other robot's control loop — cannot be settled by reading code
and needs two robots to observe. So the instrument must be trustworthy before
the robots arrive, and the way to establish that is to feed it a connect that
*definitely* blocks and check that it notices, then one that definitely does
not and check that it stays quiet.

Without the first test this would be a report that always says "clean".
"""

from __future__ import annotations

import asyncio
import time

from unitree_robot_agent.fleet.interference import (
    InterferenceReport,
    Timing,
    measure_connect_impact,
    summarise,
)

INTERVAL = 0.005


async def test_it_detects_a_connect_that_blocks_the_loop():
    """The hazard, reproduced: a blocking call inside the handshake."""

    async def blocking_connect():
        time.sleep(0.4)  # exactly what blocking I/O on the loop looks like

    report = await measure_connect_impact(
        blocking_connect, interval=INTERVAL, baseline_s=0.3, settle_s=0.05
    )

    assert report.stalled is True
    assert report.during.max_ms >= 300, report.as_dict()
    assert report.baseline.max_ms < 100, "the baseline should be quiet"
    assert "stalled" in report.verdict
    assert "would have missed its control frames" in report.verdict


async def test_it_stays_quiet_for_a_well_behaved_connect():
    """The other half: a false alarm would be worse than no instrument."""

    async def polite_connect():
        await asyncio.sleep(0.2)

    report = await measure_connect_impact(
        polite_connect, interval=INTERVAL, baseline_s=0.3, settle_s=0.05
    )

    assert report.stalled is False
    assert report.during.max_ms < 50, report.as_dict()
    assert "clean" in report.verdict


async def test_a_slow_but_yielding_connect_is_not_flagged():
    """Takes a long time, blocks nothing. The distinction the whole tool is
    about: a slow handshake is survivable, a blocking one is not."""
    ticks = 0

    async def slow_but_polite():
        nonlocal ticks
        for _ in range(20):
            await asyncio.sleep(0.02)
            ticks += 1

    report = await measure_connect_impact(
        slow_but_polite, interval=INTERVAL, baseline_s=0.2, settle_s=0.05
    )

    assert report.connect_seconds >= 0.4
    assert ticks == 20, "the connect should have yielded the whole way"
    # Relative to the baseline, not to the absolute 50 ms alert. The tool's
    # absolute threshold is right for the field — a 50 ms stall is a 50 ms
    # stall — but as a *test* assertion it makes this flaky: on a loaded
    # machine the idle loop's own wakeups drift past 50 ms, and the test then
    # fails for the environment rather than for the code. What is actually
    # being asserted is "a polite connect did not make the loop worse than it
    # already was".
    assert report.during.max_ms < max(50.0, report.baseline.max_ms * 4), (
        f"a yielding connect still degraded the loop: "
        f"{report.baseline.max_ms:.0f} ms -> {report.during.max_ms:.0f} ms"
    )


async def test_a_partially_blocking_connect_is_measured_not_rounded():
    """Real blocking I/O blocks for a while and then yields; the report has to
    carry the magnitude, not a boolean."""

    async def mixed():
        await asyncio.sleep(0.05)
        time.sleep(0.15)
        await asyncio.sleep(0.05)

    report = await measure_connect_impact(
        mixed, interval=INTERVAL, baseline_s=0.3, settle_s=0.05
    )

    assert 100 <= report.during.max_ms <= 400, report.as_dict()


async def test_a_failed_connect_is_reported_and_not_concluded_from():
    async def broken():
        raise RuntimeError("robot busy")

    report = await measure_connect_impact(
        broken, interval=INTERVAL, baseline_s=0.2, settle_s=0.05
    )

    assert report.connect_error.startswith("RuntimeError")
    assert "nothing to conclude" in report.verdict
    assert report.stalled is False


async def test_a_connect_that_never_finishes_is_bounded():
    async def hangs():
        await asyncio.sleep(3600)

    report = await measure_connect_impact(
        hangs, interval=INTERVAL, baseline_s=0.1, settle_s=0.05, max_s=0.3
    )

    assert "did not finish" in report.connect_error


async def test_the_connect_gets_a_full_window_to_strand_the_sampler():
    """Sampling must continue past the end of the handshake, or damage done at
    the very end of it would land outside the measurement."""
    seen = {"sampled_during_connect": 0}

    async def blocking_then_quiet():
        time.sleep(0.25)

    report = await measure_connect_impact(
        blocking_then_quiet, interval=INTERVAL, baseline_s=0.2, settle_s=0.1
    )
    assert report.during.count > report.baseline.count  # a longer window


# ─── the report shape ─────────────────────────────────────────────────


def test_timing_percentiles_and_dictionary():
    timing = Timing(samples=[0.001, 0.002, 0.003, 0.004, 0.5])
    assert timing.count == 5
    assert timing.max_ms == 500
    assert timing.percentile_ms(0.95) == 500
    assert timing.percentile_ms(0.5) == 3
    assert timing.as_dict()["samples"] == 5


def test_an_empty_timing_window_is_all_zeroes():
    timing = Timing()
    assert timing.max_ms == 0
    assert timing.as_dict() == {"samples": 0, "max_ms": 0.0, "mean_ms": 0.0, "p95_ms": 0.0}


def test_the_factor_guards_a_near_zero_baseline():
    report = InterferenceReport(baseline=Timing([0.0]), during=Timing([0.05]))
    assert report.factor == 50.0


def test_the_summary_separates_stalls_from_failed_connects():
    from unittest.mock import MagicMock

    stalled = MagicMock()
    stalled.stalled = True
    stalled.connect_error = ""
    stalled.during.max_ms = 400.0
    stalled.as_dict.return_value = {"verdict": "stalled"}

    failed = MagicMock()
    failed.stalled = False
    failed.connect_error = "RobotBusy"
    failed.during.max_ms = 10.0
    failed.as_dict.return_value = {"verdict": "failed"}

    summary = summarise([stalled, failed])
    assert summary["measurements"] == 2
    assert summary["stalled"] == 1
    assert summary["connect_errors"] == 1
    assert summary["clean"] is False
    assert summary["worst_stall_ms"] == 400.0


def test_an_empty_summary_is_clean():
    summary = summarise([])
    assert summary["clean"] is True
    assert summary["measurements"] == 0
