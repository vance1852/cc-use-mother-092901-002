import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from driver_rights.hours import (continuous_driving_minutes, daily_usage_minutes,
                                 each_local_day, merge_intervals, midnights_crossed,
                                 night_overlap_minutes, parse_instant)
from driver_rights.models import Segment
from transport_coordination.errors import ValidationError

TZ = ZoneInfo("Asia/Shanghai")


def seg(kind: str, start: str, end: str) -> Segment:
    return Segment(segment_id=f"s-{start}-{end}", task_id="t", driver_id="d", kind=kind,
                   started_at=parse_instant(start), ended_at=parse_instant(end),
                   source="live", event_id=f"e-{start}", frozen=False)


class HoursTest(unittest.TestCase):
    def test_parse_instant_requires_timezone(self):
        with self.assertRaises(ValidationError):
            parse_instant("2026-09-30 10:00:00")
        self.assertEqual("2026-09-30T02:00:00Z",
                         parse_instant("2026-09-30T10:00:00+08:00").isoformat().replace(
                             "+00:00", "Z"))

    def test_merge_intervals_joins_touching_ranges(self):
        merged = merge_intervals([
            (parse_instant("2026-09-30T00:00:00Z"), parse_instant("2026-09-30T01:00:00Z")),
            (parse_instant("2026-09-30T01:00:00Z"), parse_instant("2026-09-30T02:00:00Z")),
            (parse_instant("2026-09-30T05:00:00Z"), parse_instant("2026-09-30T06:00:00Z")),
        ])
        self.assertEqual(2, len(merged))

    def test_each_local_day_splits_at_local_midnight(self):
        days = each_local_day(parse_instant("2026-09-30T15:00:00Z"),
                              parse_instant("2026-10-01T17:00:00Z"), TZ)
        self.assertEqual(3, len(days))
        self.assertEqual("2026-09-30T16:00:00Z", days[1][0].isoformat().replace("+00:00", "Z"))

    def test_night_overlap_counts_local_night_window(self):
        # 北京时间 20:00 至次日 04:00 的执勤，落在 22:00-06:00 夜间窗口内为 6 小时。
        minutes = night_overlap_minutes(
            [(parse_instant("2026-09-30T12:00:00Z"), parse_instant("2026-09-30T20:00:00Z"))],
            TZ, 22, 6)
        self.assertEqual(360, minutes)

    def test_night_overlap_zero_outside_window(self):
        minutes = night_overlap_minutes(
            [(parse_instant("2026-09-30T01:00:00Z"), parse_instant("2026-09-30T05:00:00Z"))],
            TZ, 22, 6)
        self.assertEqual(0, minutes)

    def test_midnights_crossed_counts_local_midnights(self):
        crossed = midnights_crossed(
            [(parse_instant("2026-09-30T12:00:00Z"), parse_instant("2026-09-30T20:00:00Z"))], TZ)
        self.assertEqual(1, crossed)
        not_crossed = midnights_crossed(
            [(parse_instant("2026-09-30T01:00:00Z"), parse_instant("2026-09-30T05:00:00Z"))], TZ)
        self.assertEqual(0, not_crossed)

    def test_continuous_driving_chain_ignores_short_breaks(self):
        segments = [
            seg("driving", "2026-09-30T00:00:00Z", "2026-09-30T03:00:00Z"),
            seg("rest", "2026-09-30T03:00:00Z", "2026-09-30T03:10:00Z"),
            seg("driving", "2026-09-30T03:10:00Z", "2026-09-30T05:00:00Z"),
        ]
        # 链条累计驾驶 180 + 110 分钟，10 分钟的短休息不打断链条也不计入驾驶。
        self.assertEqual(290, continuous_driving_minutes(segments, 20))

    def test_continuous_driving_resets_after_full_break(self):
        segments = [
            seg("driving", "2026-09-30T00:00:00Z", "2026-09-30T03:00:00Z"),
            seg("driving", "2026-09-30T04:00:00Z", "2026-09-30T05:00:00Z"),
        ]
        self.assertEqual(60, continuous_driving_minutes(segments, 20))

    def test_daily_usage_uses_local_day(self):
        segments = [
            seg("driving", "2026-09-29T15:00:00Z", "2026-09-29T17:00:00Z"),
            seg("loading", "2026-09-29T17:00:00Z", "2026-09-29T18:00:00Z"),
        ]
        days = each_local_day(parse_instant("2026-09-29T16:00:00Z"),
                              parse_instant("2026-09-29T16:30:00Z"), TZ)
        usage = daily_usage_minutes(segments, days[0][0], days[0][1])
        # 北京时间 9 月 30 日 00:00（UTC 16:00）之后：驾驶 60 分钟、装卸 60 分钟。
        self.assertEqual(60, usage["driving"])
        self.assertEqual(60, usage["loading"])
        self.assertEqual(120, usage["duty"])


if __name__ == "__main__":
    unittest.main()
