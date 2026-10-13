"""Does connecting one robot stall the others?

This is the last structural unknown in the dual-dog design, and it is a real
hazard rather than a theoretical one. ``WebRTCBackend`` offloads the *driver
constructor* to a worker thread and then awaits ``conn.connect()`` directly on
the shared event loop — and inside the driver that path performs blocking I/O:
a UDP discovery wait, and blocking ``requests`` calls for the SDP exchange.
While it runs, nothing else on that loop runs either. A 20 Hz joystick stream
to a robot that is already connected simply stops.

Whether it *actually* stalls, and by how much, depends on the network and the
firmware, so it cannot be settled by reading code. It has to be measured — and
measuring it needs two robots on a desk.

So this is the instrument, ready for that moment. It needs no robot to be
*trusted*: the tests drive it with a deliberately blocking stub and assert it
reports the stall, and with a well-behaved one and assert it reports none.

The measurement itself is deliberately about the **event loop**, not about the
robot. Telemetry freshness would be a proxy with its own explanations — the
robot might just be quiet. A coroutine that times its own sleeps measures the
thing the hazard actually damages: the loop's ability to run anything at all.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, List, Optional

logger = logging.getLogger("unitree_robot_agent.fleet.interference")

#: How often the sampler wakes. 10 ms is fine enough to see a handshake's
#: damage and coarse enough not to dominate the loop itself.
DEFAULT_INTERVAL_S = 0.01

#: A stall at or above this is worth reporting regardless of the baseline:
#: at 20 Hz a joystick frame is due every 50 ms.
STALL_ALERT_MS = 50.0


@dataclass
class Timing:
    """Wakeup lateness over one window."""

    samples: List[float] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.samples)

    @property
    def max_ms(self) -> float:
        return max(self.samples) * 1000 if self.samples else 0.0

    @property
    def mean_ms(self) -> float:
        return (sum(self.samples) / len(self.samples) * 1000) if self.samples else 0.0

    def percentile_ms(self, fraction: float) -> float:
        if not self.samples:
            return 0.0
        ordered = sorted(self.samples)
        index = min(len(ordered) - 1, int(len(ordered) * fraction))
        return ordered[index] * 1000

    def as_dict(self) -> dict:
        return {
            "samples": self.count,
            "max_ms": round(self.max_ms, 1),
            "mean_ms": round(self.mean_ms, 2),
            "p95_ms": round(self.percentile_ms(0.95), 1),
        }


@dataclass
class InterferenceReport:
    """What a connect cost everything else on the loop."""

    baseline: Timing
    during: Timing
    connect_seconds: float = 0.0
    connect_error: str = ""
    #: The threshold used, so a report can be read without guessing.
    alert_ms: float = STALL_ALERT_MS

    @property
    def stalled(self) -> bool:
        return self.during.max_ms >= self.alert_ms

    @property
    def factor(self) -> float:
        """How much worse than the baseline, guarding a near-zero baseline."""
        baseline = max(self.baseline.max_ms, 1.0)
        return self.during.max_ms / baseline

    def as_dict(self) -> dict:
        return {
            "baseline": self.baseline.as_dict(),
            "during": self.during.as_dict(),
            "connect_seconds": round(self.connect_seconds, 2),
            "connect_error": self.connect_error,
            "alert_ms": self.alert_ms,
            "stalled": self.stalled,
            "factor": round(self.factor, 1),
            "verdict": self.verdict,
        }

    @property
    def verdict(self) -> str:
        if self.connect_error:
            return f"the connect failed ({self.connect_error}); nothing to conclude"
        if self.stalled:
            return (
                f"stalled: the loop was blocked for up to "
                f"{self.during.max_ms:.0f} ms while connecting, "
                f"{self.factor:.0f}x the baseline. Anything connected would "
                f"have missed its control frames for that long."
            )
        return (
            f"clean: worst wakeup lateness {self.during.max_ms:.0f} ms against "
            f"a {self.baseline.max_ms:.0f} ms baseline."
        )


async def _sample_until(stop: asyncio.Event, interval: float) -> Timing:
    """Time our own sleeps until told to stop.

    The lateness of each wakeup is the loop's unavailability, measured from
    inside it. Nothing else needs to be instrumented.
    """
    timing = Timing()
    deadline = time.monotonic()
    while not stop.is_set():
        deadline += interval
        delay = deadline - time.monotonic()
        if delay > 0:
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
                break  # stop was set while we waited
            except asyncio.TimeoutError:
                pass
        timing.samples.append(max(0.0, time.monotonic() - deadline))
    return timing


async def measure_connect_impact(
    connect: Callable[[], Awaitable[None]],
    *,
    interval: float = DEFAULT_INTERVAL_S,
    baseline_s: float = 1.0,
    settle_s: float = 0.3,
    max_s: float = 60.0,
    alert_ms: float = STALL_ALERT_MS,
) -> InterferenceReport:
    """Measure what ``connect()`` costs everything else on the loop.

    A baseline window first (nothing else happening), then the connect runs
    concurrently with a second sampling window. Both windows are on the same
    loop, which is the point: if the connect blocks, the sampler's wakeups are
    late by exactly as much.
    """
    baseline_stop = asyncio.Event()
    baseline_task = asyncio.create_task(_sample_until(baseline_stop, interval))
    await asyncio.sleep(baseline_s)
    baseline_stop.set()
    baseline = await baseline_task

    during_stop = asyncio.Event()
    sampler = asyncio.create_task(_sample_until(during_stop, interval))

    report_error = ""
    started = time.monotonic()
    connect_task = asyncio.create_task(connect())
    try:
        await asyncio.wait_for(asyncio.shield(connect_task), timeout=max_s)
    except asyncio.TimeoutError:
        report_error = f"did not finish within {max_s:.0f}s"
        connect_task.cancel()
    except Exception as exc:  # noqa: BLE001 - a failed connect is a result
        report_error = f"{type(exc).__name__}: {exc}"
    elapsed = time.monotonic() - started

    # Sample a little past the connect, so damage done at the very end of the
    # handshake still lands inside the window.
    await asyncio.sleep(settle_s)
    during_stop.set()
    during = await sampler

    report = InterferenceReport(
        baseline=baseline,
        during=during,
        connect_seconds=elapsed,
        connect_error=report_error,
        alert_ms=alert_ms,
    )
    logger.info("connect interference: %s", report.verdict)
    return report


def summarise(reports: List[InterferenceReport]) -> dict:
    """One verdict across several measurements."""
    stalled = [r for r in reports if r.stalled and not r.connect_error]
    failed = [r for r in reports if r.connect_error]
    worst = max((r.during.max_ms for r in reports), default=0.0)
    return {
        "measurements": len(reports),
        "stalled": len(stalled),
        "connect_errors": len(failed),
        "worst_stall_ms": round(worst, 1),
        "clean": not stalled and not failed,
        "reports": [r.as_dict() for r in reports],
    }
