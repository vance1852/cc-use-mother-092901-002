"""最低结算规则引擎的纯函数测试。"""

import unittest

from driver_guarantee.domain import LINE_VOID
from driver_guarantee.settlement import (
    DEFAULT_SETTLEMENT,
    compute_trip_earnings,
    evidence_is_timely,
    recompute_period,
    revalidate_deductions,
)


def seg(kind, start, end, event, driver="d1"):
    return {"driver_id": driver, "trip_id": "t1", "kind": kind,
            "start_at": start, "end_at": end, "source_event_id": event}


def trip(status="completed", driver="d1", co=None, freight=15000):
    return {"trip_id": "t1", "driver_id": driver, "co_driver_id": co,
            "status": status, "freight_amount": freight}


class EarningsTest(unittest.TestCase):
    def test_freight_uses_max_of_agreed_and_minimum(self):
        lines = compute_trip_earnings(trip(freight=15000), [], DEFAULT_SETTLEMENT)
        self.assertEqual(20000, lines[0]["amount"])
        lines = compute_trip_earnings(trip(freight=32000), [], DEFAULT_SETTLEMENT)
        self.assertEqual(32000, lines[0]["amount"])

    def test_free_wait_window_excluded(self):
        segments = [seg("waiting", "2026-10-01T00:00:00Z", "2026-10-01T00:20:00Z", "w1")]
        lines = compute_trip_earnings(trip(), segments, DEFAULT_SETTLEMENT)
        self.assertFalse(any(l["category"] == "waiting" for l in lines))
        segments = [seg("waiting", "2026-10-01T00:00:00Z", "2026-10-01T01:00:00Z", "w1")]
        lines = compute_trip_earnings(trip(), segments, DEFAULT_SETTLEMENT)
        waiting = [l for l in lines if l["category"] == "waiting"][0]
        self.assertEqual(30 * 5, waiting["amount"])

    def test_cancelled_trip_pays_subsidies_but_not_freight(self):
        segments = [seg("waiting", "2026-10-01T00:00:00Z", "2026-10-01T01:30:00Z", "w1")]
        lines = compute_trip_earnings(trip(status="cancelled"), segments, DEFAULT_SETTLEMENT)
        self.assertFalse(any(l["kind"] == "freight" for l in lines))
        self.assertTrue(any(l["category"] == "waiting" for l in lines))

    def test_rescue_subsidy_counts_once_per_event(self):
        segments = [
            seg("rescue", "2026-10-01T00:00:00Z", "2026-10-01T00:30:00Z", "ev1"),
            seg("rescue", "2026-10-01T02:00:00Z", "2026-10-01T02:30:00Z", "ev2"),
        ]
        lines = compute_trip_earnings(trip(), segments, DEFAULT_SETTLEMENT)
        rescues = [l for l in lines if l["category"] == "rescue"]
        self.assertEqual(2, len(rescues))
        anchors = {l["anchor"] for l in rescues}
        self.assertEqual(2, len(anchors))

    def test_codriver_gets_subsidies_without_double_freight(self):
        segments = [seg("loading", "2026-10-01T00:00:00Z", "2026-10-01T00:30:00Z",
                        "e1", driver="d2")]
        t = trip(driver="d1", co="d2")
        lines = recompute_period(trips=[t], segments=segments, deductions=[],
                                 rules=DEFAULT_SETTLEMENT)
        freight = [l for l in lines if l["kind"] == "freight"]
        self.assertEqual(1, len(freight))
        self.assertEqual("freight:t1:d1", freight[0]["anchor"])
        loading = [l for l in lines if l["category"] == "loading"]
        self.assertEqual(1, len(loading))
        self.assertEqual("subsidy:loading:t1:d2", loading[0]["anchor"])


class EvidenceTimingTest(unittest.TestCase):
    def test_timely_evidence(self):
        self.assertTrue(evidence_is_timely(
            {"received_at": "2026-10-03T00:00:00Z",
             "trip_completed_at": "2026-10-01T00:00:00Z"}, DEFAULT_SETTLEMENT))

    def test_late_evidence_rejected(self):
        self.assertFalse(evidence_is_timely(
            {"received_at": "2026-10-09T00:00:00Z",
             "trip_completed_at": "2026-10-01T00:00:00Z"}, DEFAULT_SETTLEMENT))

    def test_revalidate_voids_late_and_preserves_appeal_states(self):
        deductions = [
            {"anchor": "a1", "amount": -1000, "category": "x",
             "status": "payable", "evidence": {"received_at": "2026-10-09T00:00:00Z",
                                               "trip_completed_at": "2026-10-01T00:00:00Z"}},
            {"anchor": "a2", "amount": -2000, "category": "y", "status": "void",
             "evidence": {"received_at": "2026-10-02T00:00:00Z",
                          "trip_completed_at": "2026-10-01T00:00:00Z"}},
            {"anchor": "a3", "amount": -3000, "category": "z", "status": "held",
             "evidence": {"received_at": "2026-10-02T00:00:00Z",
                          "trip_completed_at": "2026-10-01T00:00:00Z"}},
        ]
        out = {l["anchor"]: l for l in revalidate_deductions(deductions, DEFAULT_SETTLEMENT)}
        self.assertEqual(LINE_VOID, out["a1"]["status"])
        self.assertEqual(0, out["a1"]["amount"])
        self.assertEqual(LINE_VOID, out["a2"]["status"])  # 申诉裁决不复活
        self.assertEqual("held", out["a3"]["status"])
        self.assertEqual(-3000, out["a3"]["amount"])


if __name__ == "__main__":
    unittest.main()
