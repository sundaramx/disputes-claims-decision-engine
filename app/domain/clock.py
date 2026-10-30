"""Injectable time source. Everything in the engine reads time from a Clock instance,
never from datetime.now() — this is what lets the simulator mature 10bd-90d regulatory
clocks in seconds (PLAN.md §15.4)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


class SimClock:
    """Virtual clock. Starts at a fixed instant; the lifecycle driver advances it."""

    def __init__(self, start: datetime | None = None):
        self._now = start or datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs) -> datetime:
        """advance(days=1, hours=3, ...)"""
        self._now += timedelta(**kwargs)
        return self._now

    def set(self, dt: datetime) -> None:
        if dt < self._now:
            raise ValueError("SimClock cannot move backwards")
        self._now = dt
