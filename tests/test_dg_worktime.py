"""工时规则引擎的纯函数测试。"""

import unittest

from driver_guarantee.worktime import (
    DEFAULT_LIMITS,
    continuous_driving,
    evaluate_availability,
    merge_limits,
    totals_in_window,
)


def seg(kind, start, end, event="e"):
    return {"driver_id": "d1", "trip_id": "t1", "kind": kind,
            "start_at": start, "end_at": end, "source_event_id": event}


class WorktimeTest(unittest.TestCase):
    def test_empty_ledger_is_available(self):
        r = evaluate_availability([], at="2026-10-01T00:00:00Z", trip_driving_min=120)
        self.assertTrue(r["eligible"])
        self.assertEqual([], r["reasons"])
        self.assertEqual("2026-10-01T02:00:00Z", r["window_start_at"])
        self.assertEqual("2026-10-01T02:20:00Z", r["window_end_at"])

    def test_continuous_driving_accumulates_without_qualifying_rest(self):
        segments = [
            seg("driving", "2026-10-01T00:00:00Z", "2026-10-01T01:30:00Z", "e1"),
            seg("loading", "2026-10-01T01:30:00Z", "2026-10-01T02:00:00Z", "e2"),
            seg("driving", "2026-10-01T02:00:00Z", "2026-10-01T03:30:00Z", "e3"),
        ]
        # 装卸不打断连续驾驶
        total = continuous_driving(segments, "2026-10-01T03:30:00Z", 20)
        self.assertEqual(180, total)

    def test_short_rest_does_not_reset_continuous_driving(self):
        segments = [
            seg("driving", "2026-10-01T00:00:00Z", "2026-10-01T03:00:00Z", "e1"),
            seg("rest", "2026-10-01T03:00:00Z", "2026-10-01T03:10:00Z", "e2"),
            seg("driving", "2026-10-01T03:10:00Z", "2026-10-01T03:40:00Z", "e3"),
        ]
        total = continuous_driving(segments, "2026-10-01T03:40:00Z", 20)
        self.assertEqual(210, total)

    def test_qualifying_rest_resets_continuous_driving(self):
        segments = [
            seg("driving", "2026-10-01T00:00:00Z", "2026-10-01T03:50:00Z", "e1"),
            seg("rest", "2026-10-01T03:50:00Z", "2026-10-01T04:20:00Z", "e2"),
        ]
        total = continuous_driving(segments, "2026-10-01T04:20:00Z", 20)
        self.assertEqual(0, total)

    def test_berth_rest_counts_as_rest(self):
        segments = [
            seg("driving", "2026-10-01T00:00:00Z", "2026-10-01T03:50:00Z", "e1"),
            seg("berth_rest", "2026-10-01T03:50:00Z", "2026-10-01T04:20:00Z", "e2"),
        ]
        total = continuous_driving(segments, "2026-10-01T04:20:00Z", 20)
        self.assertEqual(0, total)

    def test_dispatch_blocked_when_continuous_limit_reached(self):
        segments = [seg("driving", "2026-10-01T00:00:00Z", "2026-10-01T04:00:00Z")]
        r = evaluate_availability(segments, at="2026-10-01T04:00:00Z", trip_driving_min=60)
        self.assertFalse(r["eligible"])
        self.assertIn("continuous_driving_limit_reached", r["reasons"])

    def test_dispatch_blocked_when_remaining_capacity_insufficient(self):
        # 已连续驾驶 220 分钟，本趟 60 分钟会在途中突破 240 上限
        segments = [seg("driving", "2026-10-01T00:00:00Z", "2026-10-01T03:40:00Z")]
        r = evaluate_availability(segments, at="2026-10-01T03:40:00Z", trip_driving_min=60)
        self.assertFalse(r["eligible"])
        self.assertIn("continuous_driving_capacity_would_exceed", r["reasons"])

    def test_dispatch_blocked_when_24h_driving_would_exceed(self):
        segments = [seg("driving", "2026-10-01T00:00:00Z", "2026-10-01T07:30:00Z", "e1"),
                    seg("rest", "2026-10-01T07:30:00Z", "2026-10-01T08:00:00Z", "e2")]
        r = evaluate_availability(segments, at="2026-10-01T08:00:00Z", trip_driving_min=60)
        self.assertFalse(r["eligible"])
        self.assertIn("driving_24h_limit_would_exceed", r["reasons"])

    def test_24h_window_excludes_old_segments(self):
        segments = [seg("driving", "2026-09-29T08:00:00Z", "2026-09-29T16:00:00Z")]
        total = totals_in_window(segments, "2026-10-01T00:00:00Z", 24 * 60,
                                 frozenset({"driving"}))
        self.assertEqual(0, total)

    def test_partial_overlap_counts_only_overlap(self):
        segments = [seg("driving", "2026-09-30T23:00:00Z", "2026-10-01T01:00:00Z")]
        total = totals_in_window(segments, "2026-10-01T01:00:00Z", 24 * 60,
                                 frozenset({"driving"}))
        self.assertEqual(120, total)

    def test_custom_limits_take_effect(self):
        limits = merge_limits({"max_continuous_driving_min": 120})
        self.assertEqual(120, limits["max_continuous_driving_min"])
        self.assertEqual(480, limits["max_driving_24h_min"])
        segments = [seg("driving", "2026-10-01T00:00:00Z", "2026-10-01T02:05:00Z")]
        r = evaluate_availability(segments, at="2026-10-01T02:05:00Z", limits=limits)
        self.assertFalse(r["eligible"])


if __name__ == "__main__":
    unittest.main()
