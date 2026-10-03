"""提供工时与夜间窗口的纯函数计算，全部基于带时区的 UTC 时间。"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

from .domain import DUTY_KINDS
from .models import Segment


def parse_instant(value: str, field: str = "occurred_at") -> datetime:
    """把 ISO 8601 文本解析为带时区的 UTC 时间。"""

    from transport_coordination.errors import ValidationError

    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} 必须是 ISO 8601 时间文本")
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{field} 不是有效的 ISO 8601 时间") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{field} 必须包含时区")
    return parsed.astimezone(timezone.utc)


def format_instant(value: datetime) -> str:
    """把时间序列化为项目统一的 UTC 文本。"""

    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def minutes_between(start: datetime, end: datetime) -> int:
    """计算两个时间之间的整分钟数。"""

    return max(0, int((end - start).total_seconds() // 60))


def merge_intervals(intervals: Iterable[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """合并相互重叠或首尾相接的时间区间。"""

    ordered = sorted((start, end) for start, end in intervals if start < end)
    merged: list[list[datetime]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def overlap_minutes(a_start: datetime, a_end: datetime,
                    b_start: datetime, b_end: datetime) -> int:
    """计算两个区间的重叠分钟数。"""

    start = max(a_start, b_start)
    end = min(a_end, b_end)
    if end <= start:
        return 0
    return minutes_between(start, end)


def local_day_bounds(day: datetime, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """返回指定时间所在当地自然日的 UTC 起止。"""

    local = day.astimezone(tz)
    start_local = datetime.combine(local.date(), time.min, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def each_local_day(start: datetime, end: datetime, tz: ZoneInfo) -> list[tuple[datetime, datetime]]:
    """列出区间覆盖的每一个当地自然日的 UTC 起止。"""

    days: list[tuple[datetime, datetime]] = []
    cursor, _ = local_day_bounds(start, tz)
    while cursor < end:
        # 自然日边界按当地日历推进，而不是简单加 24 小时。
        local_next = datetime.combine(
            cursor.astimezone(tz).date() + timedelta(days=1), time.min, tzinfo=tz)
        day_end = local_next.astimezone(timezone.utc)
        days.append((cursor, day_end))
        cursor = day_end
    return days


def daily_usage_minutes(segments: Iterable[Segment], day_start: datetime,
                        day_end: datetime) -> dict[str, int]:
    """统计区间内各类区段与当地自然日重叠的分钟数。"""

    usage = {kind: 0 for kind in ("driving", "loading", "waiting", "rest")}
    for segment in segments:
        if segment.ended_at is None:
            continue
        usage[segment.kind] = usage.get(segment.kind, 0) + overlap_minutes(
            segment.started_at, segment.ended_at, day_start, day_end)
    usage["duty"] = sum(usage[kind] for kind in DUTY_KINDS if kind in usage)
    return usage


def continuous_driving_minutes(segments: Iterable[Segment], min_break_minutes: int) -> int:
    """计算以最新驾驶区段结尾的连续驾驶链总分钟数。

    两次驾驶之间的间隔不足 min_break_minutes 时不视为有效休息，链条继续累计。
    """

    intervals = merge_intervals(
        (segment.started_at, segment.ended_at)
        for segment in segments
        if segment.kind == "driving" and segment.ended_at is not None
    )
    if not intervals:
        return 0
    chain: list[tuple[datetime, datetime]] = [intervals[-1]]
    for previous in reversed(intervals[:-1]):
        following = chain[0]
        gap = minutes_between(previous[1], following[0])
        if gap >= min_break_minutes:
            break
        chain.insert(0, previous)
    return sum(minutes_between(start, end) for start, end in chain)


def night_overlap_minutes(intervals: Iterable[tuple[datetime, datetime]], tz: ZoneInfo,
                          night_start_hour: int, night_end_hour: int) -> int:
    """计算区间落在当地夜间窗口（如 22:00 至次日 06:00）内的分钟数。"""

    total = 0
    for start, end in merge_intervals(intervals):
        for day_start, day_end in each_local_day(start, end, tz):
            local_date = day_start.astimezone(tz).date()
            windows = []
            if night_end_hour > 0:
                morning = datetime.combine(local_date, time(night_end_hour), tzinfo=tz)
                windows.append((day_start, morning.astimezone(timezone.utc)))
            if night_start_hour < 24:
                evening = datetime.combine(local_date, time(night_start_hour), tzinfo=tz)
                windows.append((evening.astimezone(timezone.utc), day_end))
            for window_start, window_end in windows:
                total += overlap_minutes(start, end, window_start, window_end)
    return total


def midnights_crossed(intervals: Iterable[tuple[datetime, datetime]], tz: ZoneInfo) -> int:
    """统计区间内部覆盖的当地午夜次数，用于跨夜轮班补贴。"""

    count = 0
    for start, end in merge_intervals(intervals):
        local = start.astimezone(tz)
        next_midnight = datetime.combine(
            local.date() + timedelta(days=1), time.min, tzinfo=tz).astimezone(timezone.utc)
        while next_midnight < end:
            if start < next_midnight:
                count += 1
            next_midnight += timedelta(days=1)
    return count


def duty_intervals(segments: Iterable[Segment]) -> list[tuple[datetime, datetime]]:
    """提取执勤（非休息）区段的时间区间。"""

    return [(segment.started_at, segment.ended_at)
            for segment in segments
            if segment.kind in DUTY_KINDS and segment.ended_at is not None]
