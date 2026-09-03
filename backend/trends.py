"""Fleet-level history, for the trend chart.

The dashboard could accumulate this itself from the frames it receives, but then every
fresh page load would start with an empty chart and fill in over the next few minutes.
An operator opening the dashboard during an incident needs to see what the last quarter
of an hour looked like, not wait for it.

Kept deliberately small and separate from any per-robot history: this is a handful of
fleet-wide numbers once a second, so a bounded in-memory deque is the whole
implementation. At the default hour of retention it holds 3600 rows of six numbers.
"""

from __future__ import annotations

import time
from collections import deque


class TrendBuffer:
    """A ring buffer of fleet summaries, sampled on a fixed interval."""

    def __init__(self, retention_minutes: int = 60, interval_seconds: float = 1.0) -> None:
        self.interval = max(0.2, interval_seconds)
        self.points: deque[tuple[int, int, int, int, int, float]] = deque(
            maxlen=max(60, int(retention_minutes * 60 / self.interval))
        )
        self._last_sample = 0.0

    def sample(self, summary: dict, now: float | None = None) -> bool:
        """Record one point if the interval has elapsed. Returns True if it did."""
        now = time.time() if now is None else now
        if now - self._last_sample < self.interval:
            return False
        self._last_sample = now
        self.points.append((
            int(now * 1000),
            summary["total"],
            summary["working"],
            summary["attention"],
            summary["stale"],
            summary["avg_battery"],
        ))
        return True

    def series(self, since_ms: int | None = None) -> dict:
        """History in uPlot's columnar layout: one array per series, not one object per
        point. uPlot consumes exactly this shape, so the browser does no reshaping, and
        columns of numbers compress far better than repeated key names."""
        rows = self.points
        if since_ms is not None:
            rows = [r for r in rows if r[0] >= since_ms]

        if not rows:
            return {"t": [], "total": [], "working": [], "attention": [],
                    "stale": [], "avg_battery": [], "working_pct": []}

        t, total, working, attention, stale, battery = (list(c) for c in zip(*rows))
        return {
            # uPlot's x axis is in seconds, not milliseconds.
            "t": [ms / 1000.0 for ms in t],
            "total": total,
            "working": working,
            "attention": attention,
            "stale": stale,
            "avg_battery": battery,
            "working_pct": [
                round(100.0 * w / n, 1) if n else 0.0 for w, n in zip(working, total)
            ],
        }

    def stats(self) -> dict:
        return {
            "points": len(self.points),
            "capacity": self.points.maxlen,
            "interval_seconds": self.interval,
        }
