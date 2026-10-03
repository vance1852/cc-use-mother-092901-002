import unittest

from driver_rights.domain import normalize_terms
from driver_rights.hours import parse_instant
from driver_rights.models import Segment
from driver_rights.settlement import compute_lines, lines_total
from transport_coordination.errors import ValidationError

TERMS = normalize_terms({
    "rates": {"driving_per_hour_cents": 6000, "loading_per_hour_cents": 4000,
              "waiting_per_hour_cents": 3000, "free_waiting_minutes": 30},
    "subsidies": {"night_per_hour_cents": 2000, "night_start_hour": 22,
                  "night_end_hour": 6, "cross_night_flat_cents": 8000},
    "minimums": {"per_trip_cents": 30000},
    "appeal_window_hours": 168,
})


def seg(kind: str, start: str, end: str) -> Segment:
    return Segment(segment_id=f"s-{kind}-{start}", task_id="t1", driver_id="d1", kind=kind,
                   started_at=parse_instant(start), ended_at=parse_instant(end),
                   source="live", event_id=f"e-{kind}-{start}", frozen=False)


class SettlementTest(unittest.TestCase):
    def lines_by_component(self, lines):
        return {line.component: line for line in lines}

    def test_full_trip_computation(self):
        # 北京时间 20:00 至次日 04:00：驾驶 6 小时、装卸 1 小时、等待 1 小时。
        segments = [
            seg("driving", "2026-09-30T12:00:00Z", "2026-09-30T14:00:00Z"),
            seg("loading", "2026-09-30T14:00:00Z", "2026-09-30T15:00:00Z"),
            seg("driving", "2026-09-30T15:00:00Z", "2026-09-30T18:00:00Z"),
            seg("waiting", "2026-09-30T18:00:00Z", "2026-09-30T19:00:00Z"),
            seg("driving", "2026-09-30T19:00:00Z", "2026-09-30T20:00:00Z"),
        ]
        lines = compute_lines(task_id="t1", driver_id="d1", base_freight_cents=200000,
                              segments=segments, terms=TERMS,
                              timezone_name="Asia/Shanghai", rule_version="d1@1")
        by = self.lines_by_component(lines)
        self.assertEqual(200000, by["base_freight"].amount_cents)
        self.assertEqual(36000, by["driving"].amount_cents)
        self.assertEqual(4000, by["loading"].amount_cents)
        # 等待 60 分钟，免计 30 分钟，计费 30 分钟。
        self.assertEqual(1500, by["waiting"].amount_cents)
        self.assertEqual(30, by["waiting"].quantity_minutes)
        # 夜间 22:00-06:00：执勤覆盖 22:00-04:00 共 360 分钟。
        self.assertEqual(12000, by["night_subsidy"].amount_cents)
        # 跨一个当地午夜。
        self.assertEqual(8000, by["cross_night_subsidy"].amount_cents)
        self.assertNotIn("minimum_topup", by)
        self.assertEqual(261500, lines_total(lines))

    def test_minimum_topup_reaches_per_trip_floor(self):
        segments = [seg("driving", "2026-09-30T01:00:00Z", "2026-09-30T01:30:00Z")]
        lines = compute_lines(task_id="t1", driver_id="d1", base_freight_cents=1000,
                              segments=segments, terms=TERMS,
                              timezone_name="Asia/Shanghai", rule_version="d1@1")
        by = self.lines_by_component(lines)
        earned = 1000 + 3000
        self.assertEqual(30000 - earned, by["minimum_topup"].amount_cents)
        self.assertEqual(30000, lines_total(lines))

    def test_rest_segments_do_not_count_as_duty(self):
        segments = [
            seg("driving", "2026-09-30T13:00:00Z", "2026-09-30T15:00:00Z"),
            seg("rest", "2026-09-30T15:00:00Z", "2026-09-30T17:00:00Z"),
        ]
        lines = compute_lines(task_id="t1", driver_id="d1", base_freight_cents=0,
                              segments=segments, terms=TERMS,
                              timezone_name="Asia/Shanghai", rule_version="d1@1")
        by = self.lines_by_component(lines)
        # 夜间只计算 22:00-23:00 驾驶的一小时，休息时段不计。
        self.assertEqual(60, by["night_subsidy"].quantity_minutes)
        self.assertEqual(0, by["cross_night_subsidy"].quantity_minutes)

    def test_same_inputs_produce_identical_lines(self):
        segments = [seg("driving", "2026-09-30T01:00:00Z", "2026-09-30T03:00:00Z")]
        first = compute_lines(task_id="t1", driver_id="d1", base_freight_cents=5000,
                              segments=segments, terms=TERMS,
                              timezone_name="Asia/Shanghai", rule_version="d1@1")
        second = compute_lines(task_id="t1", driver_id="d1", base_freight_cents=5000,
                               segments=segments, terms=TERMS,
                               timezone_name="Asia/Shanghai", rule_version="d1@1")
        self.assertEqual(first, second)

    def test_terms_validation_rejects_unknown_fields(self):
        with self.assertRaises(ValidationError):
            normalize_terms({"rates": {"unknown_field": 1}})
        with self.assertRaises(ValidationError):
            normalize_terms({"appeal_window_hours": 0})
        with self.assertRaises(ValidationError):
            normalize_terms({"minimums": {"per_trip_cents": -1}})


if __name__ == "__main__":
    unittest.main()
