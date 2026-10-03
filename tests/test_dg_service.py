"""司机履约与权益保障服务的集成测试。"""

import unittest
from datetime import datetime, timezone

from transport_coordination.errors import ConflictError, PermissionDenied

from driver_guarantee.clock import ManualClock
from driver_guarantee.service import GuaranteeService
from driver_guarantee.storage import Database


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.clock = ManualClock(datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc))
        self.s = GuaranteeService(self.db, self.clock)
        self.s.bootstrap_admin(request_id="b0", principal_id="admin", display_name="管理员")
        self.s.register_carrier(request_id="b1", actor_id="admin", carrier_id="c1", name="承运人一")
        self.s.register_carrier(request_id="b2", actor_id="admin", carrier_id="c2", name="承运人二")
        for rid, did, name, lic in (("b3", "d1", "张", "L1"), ("b4", "d2", "李", "L2"),
                                    ("b5", "d3", "王", "L3")):
            self.s.register_driver(request_id=rid, actor_id="admin", driver_id=did,
                                   display_name=name, license_no=lic, organization_id="c1")
        self.s.register_principal(request_id="b6", actor_id="admin", principal_id="pd1",
                                  role="driver", display_name="张", driver_id="d1")
        self.s.register_principal(request_id="b7", actor_id="admin", principal_id="pd2",
                                  role="driver", display_name="李", driver_id="d2")
        self.s.register_principal(request_id="b8", actor_id="admin", principal_id="pc1",
                                  role="carrier", display_name="调度", carrier_id="c1")
        self.s.register_principal(request_id="b9", actor_id="admin", principal_id="pr1",
                                  role="regulator", display_name="监管")
        self.s.create_contract_version(request_id="b10", actor_id="pc1", contract_id="k1",
                                       carrier_id="c1", title="合同", body={"v": 1})
        self.s.create_ruleset(request_id="b11", actor_id="admin", ruleset_id="rs", title="v1")

    def tearDown(self):
        self.db.close()

    def trip(self, rid, tid, *, at="2026-10-01T00:00:00Z", drive=120, work=None,
             freight=30000, carrier="c1"):
        return self.s.create_trip(request_id=rid, actor_id="pc1", trip_id=tid,
                                  carrier_id=carrier, origin="A", destination="B",
                                  planned_pickup_at=at, contract_id="k1",
                                  estimated_driving_min=drive,
                                  estimated_work_min=work, freight_amount=freight)


class DispatchTest(ServiceCase):
    def test_dispatch_persists_rule_snapshot_on_trip(self):
        self.trip("t", "t1")
        self.s.dispatch_trip(request_id="d", actor_id="pc1", trip_id="t1",
                             driver_id="d1", at="2026-10-01T00:00:00Z")
        trip = self.s.get_trip(actor_id="pr1", trip_id="t1")
        self.assertEqual("rs", trip["ruleset_id"])
        self.assertEqual(1, trip["ruleset_version"])
        self.assertEqual(240, trip["rule_snapshot"]["limits"]["max_continuous_driving_min"])

    def test_dispatch_rejected_after_continuous_limit(self):
        self.trip("t1", "t1", drive=240)
        self.s.dispatch_trip(request_id="d1", actor_id="pc1", trip_id="t1",
                             driver_id="d1", at="2026-10-01T00:00:00Z")
        self.s.log_interval(request_id="w1", actor_id="pd1", driver_id="d1", kind="driving",
                            start_at="2026-10-01T00:00:00Z", end_at="2026-10-01T04:00:00Z",
                            trip_id="t1")
        self.trip("t2", "t2", at="2026-10-01T04:00:00Z", drive=10)
        with self.assertRaises(ConflictError):
            self.s.dispatch_trip(request_id="d2", actor_id="pc1", trip_id="t2",
                                 driver_id="d1", at="2026-10-01T04:00:00Z")
        # 被拒任务仍未派单
        self.assertIsNone(self.s.get_trip(actor_id="pr1", trip_id="t2")["driver_id"])

    def test_rejected_dispatch_replay_raises_again(self):
        self.trip("t2", "t2", drive=10)
        self.s.log_interval(request_id="w1", actor_id="pd1", driver_id="d1", kind="driving",
                            start_at="2026-09-30T20:00:00Z", end_at="2026-10-01T00:00:00Z")
        for _ in range(2):
            with self.assertRaises(ConflictError):
                self.s.dispatch_trip(request_id="replay", actor_id="pc1", trip_id="t2",
                                     driver_id="d1", at="2026-10-01T00:00:00Z")

    def test_double_crew_checks_both_drivers(self):
        # d2 已连续驾驶 4 小时
        self.s.log_interval(request_id="w2", actor_id="pd2", driver_id="d2", kind="driving",
                            start_at="2026-09-30T20:00:00Z", end_at="2026-10-01T00:00:00Z")
        self.trip("t", "t1", drive=60)
        with self.assertRaises(ConflictError):
            self.s.dispatch_trip(request_id="dd", actor_id="pc1", trip_id="t1",
                                 driver_id="d1", co_driver_id="d2",
                                 at="2026-10-01T00:00:00Z")

    def test_forced_dispatch_is_flagged_and_attributed_to_carrier(self):
        self.s.log_interval(request_id="w1", actor_id="pd1", driver_id="d1", kind="driving",
                            start_at="2026-09-30T20:00:00Z", end_at="2026-10-01T00:00:00Z")
        self.trip("t1", "t1", drive=30)
        r = self.s.dispatch_trip(request_id="f", actor_id="pc1", trip_id="t1",
                                 driver_id="d1", at="2026-10-01T00:00:00Z", force=True)
        self.assertTrue(r["response"]["forced"])
        findings = self.s.overtime_responsibility(actor_id="pr1",
                                                  at="2026-10-01T00:00:00Z")
        self.assertEqual("carrier_forced_dispatch", findings[0]["responsibility"])

    def test_codriver_preset_at_creation_is_checked_at_dispatch(self):
        # d2 已连续驾驶 4 小时；建任务时预置副驾，派单不重复传 co_driver_id
        self.s.log_interval(request_id="w2", actor_id="pd2", driver_id="d2", kind="driving",
                            start_at="2026-09-30T20:00:00Z", end_at="2026-10-01T00:00:00Z")
        self.s.create_trip(request_id="t", actor_id="pc1", trip_id="t1", carrier_id="c1",
                           origin="A", destination="B",
                           planned_pickup_at="2026-10-01T00:00:00Z", contract_id="k1",
                           estimated_driving_min=60, co_driver_id="d2")
        with self.assertRaises(ConflictError):
            self.s.dispatch_trip(request_id="dd", actor_id="pc1", trip_id="t1",
                                 driver_id="d1", at="2026-10-01T00:00:00Z")

    def test_forced_dispatch_remains_attributed_after_trip_completes(self):
        self.s.log_interval(request_id="w1", actor_id="pd1", driver_id="d1", kind="driving",
                            start_at="2026-09-30T20:00:00Z", end_at="2026-10-01T00:00:00Z")
        self.trip("t1", "t1", drive=30)
        self.s.dispatch_trip(request_id="f", actor_id="pc1", trip_id="t1",
                             driver_id="d1", at="2026-10-01T00:00:00Z", force=True)
        self.s.complete_trip(request_id="fin", actor_id="pc1", trip_id="t1",
                             completed_at="2026-10-01T01:00:00Z")
        findings = self.s.overtime_responsibility(actor_id="pr1",
                                                  at="2026-10-02T00:00:00Z")
        self.assertTrue(findings)
        self.assertEqual("carrier_forced_dispatch", findings[0]["responsibility"])

    def test_regulator_cannot_force_dispatch(self):
        self.trip("t1", "t1")
        with self.assertRaises(PermissionDenied):
            self.s.dispatch_trip(request_id="x", actor_id="pr1", trip_id="t1",
                                 driver_id="d1", at="2026-10-01T00:00:00Z", force=True)

    def test_rest_opens_following_dispatch(self):
        self.s.log_interval(request_id="w1", actor_id="pd1", driver_id="d1", kind="driving",
                            start_at="2026-09-30T20:00:00Z", end_at="2026-10-01T00:00:00Z")
        self.s.log_interval(request_id="w2", actor_id="pd1", driver_id="d1", kind="rest",
                            start_at="2026-10-01T00:00:00Z", end_at="2026-10-01T00:30:00Z")
        self.trip("t1", "t1", at="2026-10-01T00:30:00Z", drive=60)
        r = self.s.dispatch_trip(request_id="ok", actor_id="pc1", trip_id="t1",
                                 driver_id="d1", at="2026-10-01T00:30:00Z")
        self.assertFalse(r["response"].get("forced"))


class LedgerTest(ServiceCase):
    def test_overlapping_intervals_rejected(self):
        self.s.log_interval(request_id="w1", actor_id="pd1", driver_id="d1", kind="driving",
                            start_at="2026-10-01T00:00:00Z", end_at="2026-10-01T02:00:00Z")
        with self.assertRaises(ConflictError):
            self.s.log_interval(request_id="w2", actor_id="pd1", driver_id="d1", kind="rest",
                                start_at="2026-10-01T01:00:00Z", end_at="2026-10-01T01:30:00Z")

    def test_freeze_blocks_backfill_but_allows_unfrozen_period(self):
        self.s.freeze_ledger(request_id="fz", actor_id="pc1", driver_id="d1",
                             frozen_through="2026-10-02T00:00:00Z", reason="核对")
        with self.assertRaises(ConflictError):
            self.s.log_interval(request_id="bf1", actor_id="pd1", driver_id="d1",
                                kind="backfill", payload={"target_kind": "rest"},
                                start_at="2026-10-01T10:00:00Z",
                                end_at="2026-10-01T11:00:00Z")
        self.s.log_interval(request_id="bf2", actor_id="pd1", driver_id="d1", kind="rest",
                            start_at="2026-10-03T10:00:00Z", end_at="2026-10-03T11:00:00Z")

    def test_shift_change_only_touches_unfrozen_segments(self):
        self.trip("t1", "t1", drive=120)
        self.s.dispatch_trip(request_id="dp", actor_id="pc1", trip_id="t1",
                             driver_id="d1", co_driver_id="d2", at="2026-10-01T00:00:00Z")
        self.s.log_interval(request_id="w1", actor_id="pd1", driver_id="d1", kind="driving",
                            start_at="2026-10-01T00:00:00Z", end_at="2026-10-01T02:00:00Z",
                            trip_id="t1")
        self.s.freeze_ledger(request_id="fz", actor_id="pc1", driver_id="d1",
                             frozen_through="2026-10-01T03:00:00Z", reason="核对")
        with self.assertRaises(ConflictError):
            self.s.shift_change(request_id="sh", actor_id="pc1", trip_id="t1",
                                relieved_driver_id="d1", relieving_driver_id="d2",
                                at="2026-10-01T02:30:00Z")

    def test_cancellation_closes_open_segments_and_keeps_subsidies(self):
        self.trip("t1", "t1")
        self.s.dispatch_trip(request_id="dp", actor_id="pc1", trip_id="t1",
                             driver_id="d1", at="2026-10-01T00:00:00Z")
        started = self.s.start_work(request_id="st", actor_id="pd1", driver_id="d1",
                                    kind="waiting", start_at="2026-10-01T00:00:00Z",
                                    trip_id="t1")
        self.clock.advance(hours=1)
        self.s.cancel_trip(request_id="cx", actor_id="pc1", trip_id="t1",
                           at="2026-10-01T01:00:00Z", reason="货源取消")
        segs = self.s.list_segments(actor_id="pd1", driver_id="d1")
        self.assertEqual(1, len(segs))
        self.assertEqual("waiting", segs[0]["kind"])

    def test_shift_change_ends_relieved_and_starts_reliever(self):
        self.trip("t1", "t1", drive=120)
        self.s.dispatch_trip(request_id="dp", actor_id="pc1", trip_id="t1",
                             driver_id="d1", co_driver_id="d2", at="2026-10-01T00:00:00Z")
        self.s.log_interval(request_id="w1", actor_id="pd1", driver_id="d1", kind="driving",
                            start_at="2026-10-01T00:00:00Z", end_at="2026-10-01T02:00:00Z",
                            trip_id="t1")
        self.s.shift_change(request_id="sh", actor_id="pc1", trip_id="t1",
                            relieved_driver_id="d1", relieving_driver_id="d2",
                            at="2026-10-01T02:00:00Z")
        self.s.complete_trip(request_id="fin", actor_id="pc1", trip_id="t1",
                             completed_at="2026-10-01T04:00:00Z")
        d2 = [g for g in self.s.list_segments(actor_id="pr1", driver_id="d2")
              if g["kind"] == "driving"]
        self.assertEqual(1, len(d2))
        self.assertEqual(120, d2[0]["duration_min"])


class SettlementTest(ServiceCase):
    def _settled(self):
        self.trip("t1", "t1", drive=120, freight=15000)
        self.s.dispatch_trip(request_id="dp", actor_id="pc1", trip_id="t1",
                             driver_id="d1", at="2026-10-01T00:00:00Z")
        self.s.log_interval(request_id="w1", actor_id="pd1", driver_id="d1", kind="waiting",
                            start_at="2026-10-01T00:00:00Z", end_at="2026-10-01T01:00:00Z",
                            trip_id="t1")
        self.s.complete_trip(request_id="fin", actor_id="pc1", trip_id="t1",
                             completed_at="2026-10-01T03:00:00Z")
        self.s.create_period(request_id="p", actor_id="pc1", period_id="oct", carrier_id="c1",
                             period_start="2026-10-01T00:00:00Z",
                             period_end="2026-10-02T00:00:00Z")
        self.s.settle_period(request_id="st", actor_id="pc1", period_id="oct")

    def test_minimum_freight_and_waiting_subsidy(self):
        self._settled()
        lines = self.s.list_settlement_lines(actor_id="pd1", period_id="oct")["items"]
        kinds = {(l["kind"], l["category"]): l["amount"] for l in lines}
        self.assertEqual(20000, kinds[("freight", "freight")])      # 约定 150 < 保底 200
        self.assertEqual(30 * 5, kinds[("subsidy", "waiting")])     # 免费 30 分钟

    def test_replaying_rescue_does_not_double_bill(self):
        self.trip("t1", "t1", drive=120)
        self.s.dispatch_trip(request_id="dp", actor_id="pc1", trip_id="t1",
                             driver_id="d1", at="2026-10-01T00:00:00Z")
        kwargs = dict(actor_id="pd1", driver_id="d1", kind="rescue",
                      start_at="2026-10-01T00:00:00Z", end_at="2026-10-01T00:30:00Z",
                      trip_id="t1")
        self.s.log_interval(request_id="rs", **kwargs)
        replay = self.s.log_interval(request_id="rs", **kwargs)
        self.assertTrue(replay["replayed"])
        self.s.complete_trip(request_id="fin", actor_id="pc1", trip_id="t1",
                             completed_at="2026-10-01T02:00:00Z")
        self.s.create_period(request_id="p", actor_id="pc1", period_id="oct", carrier_id="c1",
                             period_start="2026-10-01T00:00:00Z",
                             period_end="2026-10-02T00:00:00Z")
        self.s.settle_period(request_id="st", actor_id="pc1", period_id="oct")
        rescues = [l for l in self.s.list_settlement_lines(actor_id="pd1")["items"]
                   if l["category"] == "rescue"]
        self.assertEqual(1, len(rescues))

    def test_late_evidence_cannot_deduct(self):
        self._settled()
        ev = self.s.register_evidence(request_id="e", actor_id="pc1", trip_id="t1",
                                      kind="damage", payload={"x": 1},
                                      occurred_at="2026-10-01T02:00:00Z",
                                      received_at="2026-10-09T00:00:00Z")
        with self.assertRaises(ConflictError):
            self.s.add_deduction(request_id="dd", actor_id="pc1", period_id="oct",
                                 trip_id="t1", driver_id="d1", amount=1000,
                                 category="damage", memo="月末追扣",
                                 evidence_id=ev["response"]["evidence_id"])

    def test_appeal_holds_only_disputed_amount(self):
        self._settled()
        income_before = self.s.list_settlement_lines(
            actor_id="pd1", period_id="oct")["uncontested_income"]
        ev = self.s.register_evidence(request_id="e", actor_id="pc1", trip_id="t1",
                                      kind="late", payload={"x": 1},
                                      occurred_at="2026-10-01T02:00:00Z",
                                      received_at="2026-10-02T00:00:00Z")
        ded = self.s.add_deduction(request_id="dd", actor_id="pc1", period_id="oct",
                                   trip_id="t1", driver_id="d1", amount=2000,
                                   category="late", memo="扣款",
                                   evidence_id=ev["response"]["evidence_id"])
        self.clock.set(datetime(2026, 10, 3, tzinfo=timezone.utc))
        self.s.file_appeal(request_id="ap", actor_id="pd1", trip_id="t1",
                           reason="申诉", line_id=ded["response"]["line_id"])
        view = self.s.list_settlement_lines(actor_id="pd1", period_id="oct")
        self.assertEqual(income_before, view["uncontested_income"])
        self.assertEqual(2000, view["escrow_held_total"])
        # 申诉期间继续扣款被阻断
        with self.assertRaises(ConflictError):
            self.s.add_deduction(request_id="dd2", actor_id="pc1", period_id="oct",
                                 trip_id="t1", driver_id="d1", amount=500,
                                 category="late", memo="再扣",
                                 evidence_id=ev["response"]["evidence_id"])

    def test_rejected_appeal_returns_escrow_to_carrier(self):
        self._settled()
        ev = self.s.register_evidence(request_id="e", actor_id="pc1", trip_id="t1",
                                      kind="late", payload={"x": 1},
                                      occurred_at="2026-10-01T02:00:00Z",
                                      received_at="2026-10-02T00:00:00Z")
        ded = self.s.add_deduction(request_id="dd", actor_id="pc1", period_id="oct",
                                   trip_id="t1", driver_id="d1", amount=2000,
                                   category="late", memo="扣款",
                                   evidence_id=ev["response"]["evidence_id"])
        self.clock.set(datetime(2026, 10, 3, tzinfo=timezone.utc))
        ap = self.s.file_appeal(request_id="ap", actor_id="pd1", trip_id="t1",
                                reason="申诉", line_id=ded["response"]["line_id"])
        r = self.s.resolve_appeal(request_id="rv", actor_id="pr1",
                                  appeal_id=ap["response"]["appeal_id"],
                                  decision="rejected", note="证据充分")
        self.assertEqual(2000, r["response"]["returned_carrier"])

    def test_duplicate_deduction_on_same_evidence_rejected(self):
        self._settled()
        ev = self.s.register_evidence(request_id="e", actor_id="pc1", trip_id="t1",
                                      kind="late", payload={"x": 1},
                                      occurred_at="2026-10-01T02:00:00Z",
                                      received_at="2026-10-02T00:00:00Z")
        kwargs = dict(period_id="oct", trip_id="t1", driver_id="d1", amount=1000,
                      category="late", memo="扣款", evidence_id=ev["response"]["evidence_id"])
        self.s.add_deduction(request_id="dd1", actor_id="pc1", **kwargs)
        with self.assertRaises(ConflictError):
            self.s.add_deduction(request_id="dd2", actor_id="pc1", **kwargs)

    def test_appeal_deadline_enforced(self):
        self._settled()
        ev = self.s.register_evidence(request_id="e", actor_id="pc1", trip_id="t1",
                                      kind="late", payload={"x": 1},
                                      occurred_at="2026-10-01T02:00:00Z",
                                      received_at="2026-10-02T00:00:00Z")
        ded = self.s.add_deduction(request_id="dd", actor_id="pc1", period_id="oct",
                                   trip_id="t1", driver_id="d1", amount=2000,
                                   category="late", memo="扣款",
                                   evidence_id=ev["response"]["evidence_id"])
        self.clock.set(datetime(2026, 10, 20, tzinfo=timezone.utc))
        with self.assertRaises(ConflictError):
            self.s.file_appeal(request_id="late", actor_id="pd1", trip_id="t1",
                               reason="逾期申诉", line_id=ded["response"]["line_id"])

    def test_closed_period_is_immutable_and_recomputable(self):
        self._settled()
        self.s.close_period(request_id="cl", actor_id="pc1", period_id="oct")
        with self.assertRaises(ConflictError):
            self.s.settle_period(request_id="again", actor_id="pc1", period_id="oct")
        self.s.create_ruleset(request_id="rs2", actor_id="admin", ruleset_id="rs",
                              title="v2", settlement={"min_freight_amount": 30000},
                              effective_from="2026-10-10T00:00:00Z")
        r = self.s.recompute_closed_period(request_id="rc", actor_id="pr1",
                                           period_id="oct", ruleset_id="rs",
                                           ruleset_version=2)
        self.assertEqual(10000, r["response"]["delta"])
        # 原始明细未被改写
        self.assertEqual(20000, [l for l in self.s.list_settlement_lines(
            actor_id="pr1", period_id="oct")["items"]
            if l["kind"] == "freight"][0]["amount"])

    def test_upheld_void_deduction_stays_void_after_recompute(self):
        self._settled()
        ev = self.s.register_evidence(request_id="e", actor_id="pc1", trip_id="t1",
                                      kind="late", payload={"x": 1},
                                      occurred_at="2026-10-01T02:00:00Z",
                                      received_at="2026-10-02T00:00:00Z")
        ded = self.s.add_deduction(request_id="dd", actor_id="pc1", period_id="oct",
                                   trip_id="t1", driver_id="d1", amount=2000,
                                   category="late", memo="扣款",
                                   evidence_id=ev["response"]["evidence_id"])
        self.clock.set(datetime(2026, 10, 3, tzinfo=timezone.utc))
        ap = self.s.file_appeal(request_id="ap", actor_id="pd1", trip_id="t1",
                                reason="申诉", line_id=ded["response"]["line_id"])
        self.s.resolve_appeal(request_id="rv", actor_id="pr1",
                              appeal_id=ap["response"]["appeal_id"],
                              decision="upheld", note="成立")
        self.s.close_period(request_id="cl", actor_id="pc1", period_id="oct")
        self.s.create_ruleset(request_id="rs2", actor_id="admin", ruleset_id="rs",
                              title="v2", settlement={"min_freight_amount": 21000},
                              effective_from="2026-10-10T00:00:00Z")
        r = self.s.recompute_closed_period(request_id="rc", actor_id="pr1",
                                           period_id="oct", ruleset_id="rs",
                                           ruleset_version=2)
        ded_line = [l for l in r["response"]["lines"] if l["kind"] == "deduction"][0]
        self.assertEqual("void", ded_line["status"])
        self.assertEqual(0, ded_line["amount"])


class PermissionTest(ServiceCase):
    def test_driver_cannot_dispatch(self):
        self.trip("t1", "t1")
        with self.assertRaises(PermissionDenied):
            self.s.dispatch_trip(request_id="x", actor_id="pd1", trip_id="t1",
                                 driver_id="d1", at="2026-10-01T00:00:00Z")

    def test_driver_sees_only_own_settlement(self):
        self.trip("t1", "t1", drive=10)
        self.s.dispatch_trip(request_id="dp", actor_id="pc1", trip_id="t1",
                             driver_id="d2", at="2026-10-01T00:00:00Z")
        self.s.complete_trip(request_id="fin", actor_id="pc1", trip_id="t1",
                             completed_at="2026-10-01T01:00:00Z")
        self.s.create_period(request_id="p", actor_id="pc1", period_id="oct",
                             carrier_id="c1", period_start="2026-10-01T00:00:00Z",
                             period_end="2026-10-02T00:00:00Z")
        self.s.settle_period(request_id="st", actor_id="pc1", period_id="oct")
        items = self.s.list_settlement_lines(actor_id="pd1", period_id="oct")["items"]
        self.assertEqual([], items)
        items = self.s.list_settlement_lines(actor_id="pd2", period_id="oct")["items"]
        self.assertEqual(1, len(items))

    def test_carrier_scoped_to_own_organization(self):
        with self.assertRaises(PermissionDenied):
            self.s.create_trip(request_id="x", actor_id="pc1", trip_id="zz",
                               carrier_id="c2", origin="A", destination="B",
                               planned_pickup_at="2026-10-01T00:00:00Z",
                               contract_id="k1")

    def test_driver_available_list_shows_live_evaluation(self):
        self.trip("t1", "t1", drive=10)
        items = self.s.list_available_trips(actor_id="pd1",
                                            at="2026-10-01T00:00:00Z")
        self.assertEqual("t1", items[0]["trip_id"])
        self.assertTrue(items[0]["availability"]["eligible"])


if __name__ == "__main__":
    unittest.main()
