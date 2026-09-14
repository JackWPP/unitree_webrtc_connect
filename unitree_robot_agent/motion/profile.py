"""Velocity profiles for smooth, time-accurate movement.

The first implementation ramped the joystick at a fixed 0.15 per tick. That
is fine for a 10-second walk and wrong for everything else: at 20 Hz the
ramp takes ~0.35 s, so a 0.3 s move **never reaches the requested speed** —
the robot eases in and immediately eases out, and the distance travelled
bears no relation to the number that was asked for.

A trapezoid (or triangle, when the window is short) fixes that:

* the requested speed is reached in ``peak / accel`` seconds,
* that ramp is capped at half the window, so short moves get a symmetric
  triangle that actually finishes inside the time budget,
* the profile ends at exactly zero, so no separate "ramp down" phase is
  needed and the robot never keeps walking after the window closes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, List, Tuple

#: Default acceleration in "joystick units per second" (1.0 = full speed).
DEFAULT_ACCEL = 1.5


@dataclass(frozen=True)
class TrapezoidProfile:
    """A symmetric velocity profile from 0 → ``peak`` → 0 over ``duration``."""

    peak: float
    duration: float
    accel: float = DEFAULT_ACCEL
    ramp: float = 0.0

    def __post_init__(self) -> None:
        peak = abs(float(self.peak))
        duration = max(0.0, float(self.duration))
        accel = max(0.001, float(self.accel))
        if peak <= 0 or duration <= 0:
            object.__setattr__(self, "ramp", 0.0)
            object.__setattr__(self, "peak", 0.0)
            return
        ramp = min(duration / 2.0, peak / accel)
        object.__setattr__(self, "peak", peak)
        object.__setattr__(self, "duration", duration)
        object.__setattr__(self, "ramp", ramp)

    @property
    def shape(self) -> str:
        """``"triangle"`` when the window is too short to reach the peak.

        The ramp is capped at half the window, so hitting that cap means
        there is no cruise phase left — the move is a symmetric ramp up and
        straight back down, which is exactly what a short step should be.
        """
        if self.peak <= 0 or self.duration <= 0:
            return "none"
        return "triangle" if self.ramp >= self.duration / 2.0 - 1e-9 else "trapezoid"

    @property
    def triangle(self) -> bool:
        return self.shape == "triangle"

    def value_at(self, t: float) -> float:
        """Speed multiplier at time ``t`` seconds into the move."""
        if self.peak <= 0 or self.duration <= 0:
            return 0.0
        if t <= 0 or t >= self.duration:
            return 0.0
        if t < self.ramp:
            return self.peak * (t / self.ramp)
        if t > self.duration - self.ramp:
            return self.peak * ((self.duration - t) / self.ramp)
        return self.peak

    def steps(self, dt: float) -> List[Tuple[float, float]]:
        """``(elapsed, value)`` pairs for a fixed ``dt`` tick, ending at 0."""
        if dt <= 0:
            return []
        out: List[Tuple[float, float]] = []
        count = max(1, int(round(self.duration / dt)))
        for index in range(count + 1):
            elapsed = index * dt
            out.append((elapsed, self.value_at(elapsed)))
        if out and out[-1][1] != 0.0:
            out.append((self.duration, 0.0))
        return out

    def travel(self, dt: float = 0.001) -> float:
        """Integral of the profile — the normalised distance this move covers."""
        if self.peak <= 0 or self.duration <= 0:
            return 0.0
        cruise = max(0.0, self.duration - 2 * self.ramp)
        return self.peak * (cruise + self.ramp)

    def to_dict(self) -> dict:
        return {
            "peak": round(self.peak, 4),
            "duration_s": round(self.duration, 3),
            "ramp_s": round(self.ramp, 3),
            "shape": self.shape,
            "travel_normalised": round(self.travel(), 4),
        }


def iter_profile(profile: TrapezoidProfile, dt: float) -> Iterator[Tuple[float, float]]:
    yield from profile.steps(dt)
