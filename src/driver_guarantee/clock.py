"""本项目使用的可控时钟。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from transport_coordination.clock import Clock, FixedClock, SystemClock  # noqa: F401


class ManualClock:
    """可由测试或离线验收显式推进的时钟，用于跨夜轮班场景。"""

    def __init__(self, start: datetime | None = None) -> None:
        if start is None:
            start = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
        if start.tzinfo is None:
            raise ValueError("起始时间必须包含时区")
        self._now = start.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self._now

    def advance(self, *, minutes: int = 0, hours: int = 0, days: int = 0) -> datetime:
        self._now += timedelta(minutes=minutes, hours=hours, days=days)
        return self._now

    def set(self, value: datetime) -> None:
        if value.tzinfo is None:
            raise ValueError("时间必须包含时区")
        self._now = value.astimezone(timezone.utc)
