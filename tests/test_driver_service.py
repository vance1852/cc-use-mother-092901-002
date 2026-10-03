import unittest
from datetime import datetime, timezone

from driver_rights.service import DriverRightsService
from driver_rights.storage import DriverRightsDatabase
from transport_coordination.clock import ManualClock
from transport_coordination.errors import (ConflictError, NotFoundError,
                                           PermissionDenied, ValidationError)

TERMS = {
    "rates": {"driving_per_hour_cents": 6000, "loading_per_hour_cents": 4000,
              "waiting_per_hour_cents": 3000, "free_waiting_minutes": 30},
    "subsidies": {"night_per_hour_cents": 2000, "night_start_hour": 22,
                  "night_end_hour": 6, "cross_night_flat_cents": 8000},
    "minimums": {"per_trip_cents": 30000},
    "appeal_window_hours": 168,
}


class World:
    """搭建一个包含承运企业、监管机构与两名司机的测试世界。"""

    def __init__(self, start: str = "2026-09-30T23:00:00Z"):
        self.database = DriverRightsDatabase()
        self.clock = ManualClock(datetime(2026, 9, 30, 23, 0, tzinfo=timezone.utc))
        self.service = DriverRightsService(self.database, self.clock)
        s = self.service
        s.register_organization(request_id="org-c", actor_id="bootstrap",
                                organization_id="org-c", name="承运企业")
        s.register_actor(request_id="a-admin", actor_id="bootstrap", new_actor_id="admin1",
                         display_name="管理员", role="admin", organization_id="org-c")
        s.register_organization(request_id="org-r", actor_id="admin1",
                                organization_id="org-r", name="监管机构")
        s.register_actor(request_id="a-carrier", actor_id="admin1", new_actor_id="carrier1",
                         display_name="调度", role="carrier", organization_id="org-c")
        s.register_actor(request_id="a-reg", actor_id="admin1", new_actor_id="reg1",
                         display_name="监管员", role="regulator", organization_id="org-r")
        for i in (1, 2):
            s.register_actor(request_id=f"a-d{i}", actor_id="admin1", new_actor_id=f"drv{i}",
                             display_name=f"司机{i}", role="driver", organization_id="org-c")
            s.register_driver(request_id=f"d-{i}", actor_id="carrier1", driver_id=f"drv{i}",
                              license_no=f"L{i:04d}", name=f"司机{i}")
            s.publish_contract(request_id=f"c-{i}", actor_id="carrier1", driver_id=f"drv{i}",
                               effective_from="2026-09-01T00:00:00Z", terms=TERMS)
        s.publish_regulation(request_id="reg-1", actor_id="reg1",
                             regulation_id="hos-default", rules={})

    def make_task(self, task_id: str = "task-1", start: str = "2026-09-30T00:00:00Z",
                  end: str = "2026-09-30T08:00:00Z", freight: int = 200000,
                  driver: str = "drv1") -> None:
        self.service.create_task(request_id=f"t-{task_id}", actor_id="carrier1",
                                 task_id=task_id, origin="甲地", destination="乙地",
                                 planned_start=start, planned_end=end,
                                 base_freight_cents=freight)
        self.service.assign_task(request_id=f"as-{task_id}", actor_id="carrier1",
                                 task_id=task_id, driver_id=driver)
        self.service.accept_task(request_id=f"ac-{task_id}", actor_id=driver, task_id=task_id)

    def backfill(self, event_id: str, task_id: str, driver: str, kind: str,
                 start: str, end: str) -> None:
        self.service.record_event(request_id=event_id, actor_id="carrier1", task_id=task_id,
                                  event_type="segment_start", driver_id=driver, kind=kind,
                                  occurred_at=start, ended_at=end)

    def close(self):
        self.database.close()


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.world = World()

    def tearDown(self):
        self.world.close()

    def test_assign_rejects_when_daily_hours_insufficient(self):
        w = self.world
        w.make_task("task-a", start="2026-09-29T23:00:00Z", end="2026-09-30T07:00:00Z")
        w.backfill("ev-a1", "task-a", "drv1", "driving",
                   "2026-09-30T00:00:00Z", "2026-09-30T07:00:00Z")
        w.service.complete_task(request_id="fin-a", actor_id="carrier1", task_id="task-a",
                                occurred_at="2026-09-30T07:30:00Z")
        w.service.create_task(request_id="t-b", actor_id="carrier1", task_id="task-b",
                              origin="甲地", destination="乙地",
                              planned_start="2026-09-30T08:00:00Z",
                              planned_end="2026-09-30T10:00:00Z", base_freight_cents=1000)
        with self.assertRaises(ValidationError) as ctx:
            w.service.assign_task(request_id="as-b", actor_id="carrier1",
                                  task_id="task-b", driver_id="drv1")
        self.assertIn("工时不足", str(ctx.exception))

    def test_assign_rejects_when_rest_window_missing(self):
        w = self.world
        w.make_task("task-a", start="2026-09-30T00:00:00Z", end="2026-09-30T04:00:00Z")
        w.service.create_task(request_id="t-b", actor_id="carrier1", task_id="task-b",
                              origin="甲地", destination="乙地",
                              planned_start="2026-09-30T08:00:00Z",
                              planned_end="2026-09-30T10:00:00Z", base_freight_cents=1000)
        with self.assertRaises(ValidationError) as ctx:
            w.service.assign_task(request_id="as-b", actor_id="carrier1",
                                  task_id="task-b", driver_id="drv1")
        self.assertIn("休息窗口", str(ctx.exception))

    def test_assign_rejects_overlapping_task(self):
        w = self.world
        w.make_task("task-a", start="2026-09-30T00:00:00Z", end="2026-09-30T08:00:00Z")
        w.service.create_task(request_id="t-b", actor_id="carrier1", task_id="task-b",
                              origin="甲地", destination="乙地",
                              planned_start="2026-09-30T02:00:00Z",
                              planned_end="2026-09-30T03:00:00Z", base_freight_cents=1000)
        with self.assertRaises(ConflictError):
            w.service.assign_task(request_id="as-b", actor_id="carrier1",
                                  task_id="task-b", driver_id="drv1")

    def test_failed_assign_is_atomic(self):
        w = self.world
        w.make_task("task-a", start="2026-09-30T00:00:00Z", end="2026-09-30T04:00:00Z")
        w.service.create_task(request_id="t-b", actor_id="carrier1", task_id="task-b",
                              origin="甲地", destination="乙地",
                              planned_start="2026-09-30T08:00:00Z",
                              planned_end="2026-09-30T10:00:00Z", base_freight_cents=1000)
        with self.assertRaises(ValidationError):
            w.service.assign_task(request_id="as-b", actor_id="carrier1",
                                  task_id="task-b", driver_id="drv1")
        detail = w.service.task_detail("carrier1", "task-b")
        self.assertEqual("offered", detail["status"])
        self.assertEqual([], detail["assignments"])
        # 失败的请求没有写入回执，同一 request_id 重放会重新执行校验并再次失败。
        with self.assertRaises(ValidationError):
            w.service.assign_task(request_id="as-b", actor_id="carrier1",
                                  task_id="task-b", driver_id="drv1")

    def test_driver_sees_available_tasks(self):
        w = self.world
        w.service.create_task(request_id="t-a", actor_id="carrier1", task_id="task-a",
                              origin="甲地", destination="乙地",
                              planned_start="2026-10-01T00:00:00Z",
                              planned_end="2026-10-01T04:00:00Z", base_freight_cents=1000)
        w.service.assign_task(request_id="as-a", actor_id="carrier1",
                              task_id="task-a", driver_id="drv1")
        w.service.create_task(request_id="t-b", actor_id="carrier1", task_id="task-b",
                              origin="甲地", destination="乙地",
                              planned_start="2026-10-05T00:00:00Z",
                              planned_end="2026-10-05T04:00:00Z", base_freight_cents=1000)
        items = w.service.available_tasks("drv1")
        by_id = {item["task_id"]: item for item in items}
        self.assertEqual("pending", by_id["task-a"]["assignment_status"])
        self.assertIsNone(by_id["task-b"]["assignment_status"])
        self.assertEqual({"task-a", "task-b"}, set(by_id))


class EventTest(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.world.make_task("task-1", start="2026-09-30T00:00:00Z",
                             end="2026-09-30T08:00:00Z")

    def tearDown(self):
        self.world.close()

    def test_event_replay_does_not_duplicate_segments(self):
        w = self.world
        first = w.service.record_event(request_id="ev-1", actor_id="drv1", task_id="task-1",
                                       event_type="segment_start", kind="driving",
                                       occurred_at="2026-09-30T01:00:00Z",
                                       ended_at="2026-09-30T02:00:00Z")
        replay = w.service.record_event(request_id="ev-1", actor_id="drv1", task_id="task-1",
                                        event_type="segment_start", kind="driving",
                                        occurred_at="2026-09-30T01:00:00Z",
                                        ended_at="2026-09-30T02:00:00Z")
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        detail = w.service.task_detail("carrier1", "task-1")
        self.assertEqual(1, len(detail["segments"]))

    def test_same_request_id_with_different_payload_conflicts(self):
        w = self.world
        w.service.record_event(request_id="ev-1", actor_id="drv1", task_id="task-1",
                               event_type="segment_start", kind="driving",
                               occurred_at="2026-09-30T01:00:00Z",
                               ended_at="2026-09-30T02:00:00Z")
        with self.assertRaises(ConflictError):
            w.service.record_event(request_id="ev-1", actor_id="drv1", task_id="task-1",
                                   event_type="segment_start", kind="loading",
                                   occurred_at="2026-09-30T01:00:00Z",
                                   ended_at="2026-09-30T02:00:00Z")

    def test_backfill_fills_gap_but_not_overlap(self):
        w = self.world
        w.backfill("ev-1", "task-1", "drv1", "driving",
                   "2026-09-30T00:00:00Z", "2026-09-30T01:00:00Z")
        w.backfill("ev-2", "task-1", "drv1", "driving",
                   "2026-09-30T03:00:00Z", "2026-09-30T04:00:00Z")
        w.backfill("ev-3", "task-1", "drv1", "rest",
                   "2026-09-30T01:30:00Z", "2026-09-30T02:30:00Z")
        with self.assertRaises(ConflictError):
            w.backfill("ev-4", "task-1", "drv1", "waiting",
                       "2026-09-30T02:00:00Z", "2026-09-30T03:30:00Z")
        detail = w.service.task_detail("carrier1", "task-1")
        self.assertEqual(3, len(detail["segments"]))
        self.assertEqual("backfill", detail["segments"][0]["source"])

    def test_live_flow_auto_closes_previous_segment(self):
        w = self.world
        w.clock.set(datetime(2026, 9, 30, 1, 0, tzinfo=timezone.utc))
        w.service.record_event(request_id="ev-1", actor_id="drv1", task_id="task-1",
                               event_type="segment_start", kind="driving")
        w.clock.set(datetime(2026, 9, 30, 3, 0, tzinfo=timezone.utc))
        result = w.service.record_event(request_id="ev-2", actor_id="drv1", task_id="task-1",
                                        event_type="segment_start", kind="loading")
        self.assertFalse(result.replayed)
        detail = w.service.task_detail("carrier1", "task-1")
        self.assertEqual(2, len(detail["segments"]))
        self.assertIsNotNone(detail["segments"][0]["ended_at"])
        self.assertIsNone(detail["segments"][1]["ended_at"])

    def test_handover_switches_driver_atomically(self):
        w = self.world
        w.clock.set(datetime(2026, 9, 30, 1, 0, tzinfo=timezone.utc))
        w.service.record_event(request_id="ev-1", actor_id="drv1", task_id="task-1",
                               event_type="segment_start", kind="driving")
        w.clock.set(datetime(2026, 9, 30, 5, 0, tzinfo=timezone.utc))
        w.service.record_event(request_id="ev-2", actor_id="carrier1", task_id="task-1",
                               event_type="handover", driver_id="drv1", to_driver_id="drv2")
        detail = w.service.task_detail("carrier1", "task-1")
        segments = {(s["driver_id"], s["kind"]): s for s in detail["segments"]}
        self.assertEqual("2026-09-30T05:00:00Z",
                         segments[("drv1", "driving")]["ended_at"])
        self.assertIsNone(segments[("drv2", "driving")]["ended_at"])
        assignments = {a["driver_id"]: a for a in detail["assignments"]}
        self.assertEqual("ended", assignments["drv1"]["status"])
        self.assertEqual("handover", assignments["drv1"]["end_reason"])
        self.assertEqual("active", assignments["drv2"]["status"])

    def test_rescue_records_reason(self):
        w = self.world
        w.clock.set(datetime(2026, 9, 30, 1, 0, tzinfo=timezone.utc))
        w.service.record_event(request_id="ev-1", actor_id="drv1", task_id="task-1",
                               event_type="segment_start", kind="driving")
        w.clock.set(datetime(2026, 9, 30, 2, 0, tzinfo=timezone.utc))
        w.service.record_event(request_id="ev-2", actor_id="carrier1", task_id="task-1",
                               event_type="rescue", driver_id="drv1", to_driver_id="drv2",
                               reason="车辆故障")
        detail = w.service.task_detail("carrier1", "task-1")
        assignments = {a["driver_id"]: a for a in detail["assignments"]}
        self.assertEqual("rescue", assignments["drv1"]["end_reason"])

    def test_cancel_ends_open_segments_and_keeps_task_settleable(self):
        w = self.world
        w.backfill("ev-1", "task-1", "drv1", "driving",
                   "2026-09-30T01:00:00Z", "2026-09-30T02:00:00Z")
        w.clock.set(datetime(2026, 9, 30, 3, 0, tzinfo=timezone.utc))
        w.service.record_event(request_id="ev-2", actor_id="drv1", task_id="task-1",
                               event_type="segment_start", kind="driving")
        w.clock.set(datetime(2026, 9, 30, 4, 0, tzinfo=timezone.utc))
        w.service.cancel_task(request_id="cx-1", actor_id="carrier1", task_id="task-1",
                              reason="货主取消")
        detail = w.service.task_detail("carrier1", "task-1")
        self.assertEqual("cancelled", detail["status"])
        self.assertTrue(all(s["ended_at"] for s in detail["segments"]))
        w.service.settle_task(request_id="st-1", actor_id="carrier1", task_id="task-1")
        statement = w.service.statement_detail("carrier1", "org-c:2026-09:drv1")
        self.assertGreater(statement["gross_cents"], 0)

    def test_driver_cannot_record_for_other_driver(self):
        w = self.world
        with self.assertRaises(PermissionDenied):
            w.service.record_event(request_id="ev-x", actor_id="drv2", task_id="task-1",
                                   event_type="segment_start", driver_id="drv1", kind="driving",
                                   occurred_at="2026-09-30T01:00:00Z",
                                   ended_at="2026-09-30T02:00:00Z")


class SettlementTest(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.world.make_task("task-1", start="2026-09-30T12:00:00Z",
                             end="2026-10-01T00:00:00Z")

    def tearDown(self):
        self.world.close()

    def _run_trip(self):
        w = self.world
        w.backfill("ev-1", "task-1", "drv1", "driving",
                   "2026-09-30T12:00:00Z", "2026-09-30T14:00:00Z")
        w.backfill("ev-2", "task-1", "drv1", "loading",
                   "2026-09-30T14:00:00Z", "2026-09-30T15:00:00Z")
        w.backfill("ev-3", "task-1", "drv1", "driving",
                   "2026-09-30T15:00:00Z", "2026-09-30T18:00:00Z")
        w.backfill("ev-4", "task-1", "drv1", "waiting",
                   "2026-09-30T18:00:00Z", "2026-09-30T19:00:00Z")
        w.backfill("ev-5", "task-1", "drv1", "driving",
                   "2026-09-30T19:00:00Z", "2026-09-30T20:00:00Z")
        w.service.complete_task(request_id="fin-1", actor_id="carrier1", task_id="task-1",
                                occurred_at="2026-09-30T20:30:00Z")

    def test_settlement_lines_freeze_segments(self):
        w = self.world
        self._run_trip()
        w.service.settle_task(request_id="st-1", actor_id="carrier1", task_id="task-1")
        statement = w.service.statement_detail("carrier1", "org-c:2026-10:drv1")
        self.assertEqual(261500, statement["gross_cents"])
        components = {line["component"]: line["amount_cents"] for line in statement["lines"]}
        self.assertEqual(200000, components["base_freight"])
        self.assertEqual(36000, components["driving"])
        self.assertEqual(12000, components["night_subsidy"])
        self.assertEqual(8000, components["cross_night_subsidy"])
        detail = w.service.task_detail("carrier1", "task-1")
        self.assertTrue(all(s["frozen"] for s in detail["segments"]))

    def test_settlement_is_idempotent_by_business_key(self):
        w = self.world
        self._run_trip()
        w.service.settle_task(request_id="st-1", actor_id="carrier1", task_id="task-1")
        replay = w.service.settle_task(request_id="st-1", actor_id="carrier1", task_id="task-1")
        self.assertTrue(replay.replayed)
        w.service.settle_task(request_id="st-2", actor_id="carrier1", task_id="task-1")
        statement = w.service.statement_detail("carrier1", "org-c:2026-10:drv1")
        self.assertEqual(261500, statement["gross_cents"])
        earning = [l for l in statement["lines"] if l["component"] != "deduction"]
        self.assertEqual(6, len(earning))

    def test_backfill_after_freeze_is_rejected(self):
        w = self.world
        self._run_trip()
        w.service.settle_task(request_id="st-1", actor_id="carrier1", task_id="task-1")
        with self.assertRaises(ConflictError) as ctx:
            w.backfill("ev-bf", "task-1", "drv1", "rest",
                       "2026-09-30T15:30:00Z", "2026-09-30T15:40:00Z")
        self.assertIn("冻结", str(ctx.exception))

    def test_minimum_topup_applies(self):
        w = self.world
        w.service.create_task(request_id="t-2", actor_id="carrier1", task_id="task-2",
                              origin="甲地", destination="乙地",
                              planned_start="2026-10-02T01:00:00Z",
                              planned_end="2026-10-02T02:00:00Z", base_freight_cents=1000)
        w.service.assign_task(request_id="as-2", actor_id="carrier1",
                              task_id="task-2", driver_id="drv1")
        w.service.accept_task(request_id="ac-2", actor_id="drv1", task_id="task-2")
        w.clock.set(datetime(2026, 10, 2, 2, 30, tzinfo=timezone.utc))
        w.backfill("ev-t2", "task-2", "drv1", "driving",
                   "2026-10-02T01:00:00Z", "2026-10-02T01:30:00Z")
        w.service.complete_task(request_id="fin-2", actor_id="carrier1", task_id="task-2",
                                occurred_at="2026-10-02T02:00:00Z")
        w.service.settle_task(request_id="st-2", actor_id="carrier1", task_id="task-2")
        statement = w.service.statement_detail("carrier1", "org-c:2026-10:drv1")
        components = {line["component"]: line["amount_cents"] for line in statement["lines"]}
        self.assertEqual(30000, components.get("minimum_topup", 0) + 1000 + 3000)


class DeductionAppealTest(unittest.TestCase):
    def setUp(self):
        self.world = World()
        w = self.world
        w.make_task("task-1", start="2026-09-30T00:00:00Z", end="2026-09-30T08:00:00Z")
        w.backfill("ev-1", "task-1", "drv1", "driving",
                   "2026-09-30T01:00:00Z", "2026-09-30T05:00:00Z")
        w.service.complete_task(request_id="fin-1", actor_id="carrier1", task_id="task-1",
                                occurred_at="2026-09-30T08:00:00Z")
        w.service.settle_task(request_id="st-1", actor_id="carrier1", task_id="task-1")
        w.service.submit_evidence(request_id="evd-1", actor_id="carrier1", task_id="task-1",
                                  kind="late_arrival", detail={"minutes": 45})
        self.evidence_id = w.service.database.connection.execute(
            "SELECT evidence_id FROM dr_evidence").fetchone()["evidence_id"]

    def tearDown(self):
        self.world.close()

    def _deduct(self, request_id: str, amount: int) -> str:
        receipt = self.world.service.propose_deduction(
            request_id=request_id, actor_id="carrier1", task_id="task-1", driver_id="drv1",
            amount_cents=amount, reason="迟到", evidence_id=self.evidence_id)
        return receipt.resource_id

    def test_deduction_requires_evidence_of_same_task(self):
        w = self.world
        with self.assertRaises(ValidationError):
            w.service.propose_deduction(request_id="dd-x", actor_id="carrier1", task_id="task-1",
                                        driver_id="drv1", amount_cents=100, reason="无证据",
                                        evidence_id="not-exist")

    def test_deduction_applies_to_open_statement(self):
        w = self.world
        deduction_id = self._deduct("dd-1", 20000)
        statement = w.service.statement_detail("carrier1", "org-c:2026-09:drv1")
        self.assertEqual(20000, statement["deduction_cents"])
        self.assertEqual(statement["gross_cents"] - 20000, statement["payable_cents"])
        deduction_lines = [l for l in statement["lines"] if l["component"] == "deduction"]
        self.assertEqual(-20000, deduction_lines[0]["amount_cents"])
        self.assertEqual("late_arrival", statement["deductions"][0]["evidence"]["kind"])
        self.assertEqual(deduction_id, statement["deductions"][0]["deduction_id"])

    def test_late_deduction_after_period_close_is_rejected(self):
        w = self.world
        w.service.close_period(request_id="cl-1", actor_id="carrier1",
                               organization_id="org-c", month="2026-09")
        with self.assertRaises(ConflictError) as ctx:
            self._deduct("dd-late", 5000)
        self.assertIn("关账", str(ctx.exception))

    def test_appeal_escrows_disputed_amount_only(self):
        w = self.world
        first = self._deduct("dd-1", 20000)
        self._deduct("dd-2", 30000)
        w.service.close_period(request_id="cl-1", actor_id="carrier1",
                               organization_id="org-c", month="2026-09")
        w.service.file_appeal(request_id="ap-1", actor_id="drv1",
                              deduction_id=first, reason="堵车有报备")
        statement = w.service.statement_detail("carrier1", "org-c:2026-09:drv1")
        gross = statement["gross_cents"]
        # 争议金额 20000 进入托管，无争议扣款 30000 继续生效。
        self.assertEqual(20000, statement["escrow_cents"])
        self.assertEqual(30000, statement["deduction_cents"])
        self.assertEqual(gross - 30000 - 20000, statement["payable_cents"])
        w.service.mark_statement_paid(request_id="pay-1", actor_id="carrier1",
                                      statement_id="org-c:2026-09:drv1")
        paid = w.service.statement_detail("carrier1", "org-c:2026-09:drv1")
        # 支付不被申诉阻断，托管金额留在账上。
        self.assertEqual(gross - 30000 - 20000, paid["paid_cents"])
        self.assertEqual(20000, paid["escrow_cents"])

    def test_resolve_upheld_returns_escrow_to_driver(self):
        w = self.world
        deduction_id = self._deduct("dd-1", 20000)
        w.service.close_period(request_id="cl-1", actor_id="carrier1",
                               organization_id="org-c", month="2026-09")
        appeal = w.service.file_appeal(request_id="ap-1", actor_id="drv1",
                                       deduction_id=deduction_id, reason="堵车有报备")
        w.service.mark_statement_paid(request_id="pay-1", actor_id="carrier1",
                                      statement_id="org-c:2026-09:drv1")
        w.service.resolve_appeal(request_id="rs-1", actor_id="reg1",
                                 appeal_id=appeal.resource_id, outcome="upheld",
                                 note="报备属实")
        statement = w.service.statement_detail("carrier1", "org-c:2026-09:drv1")
        self.assertEqual(0, statement["escrow_cents"])
        self.assertEqual(0, statement["deduction_cents"])
        self.assertEqual(20000, statement["returned_cents"])
        self.assertEqual(20000, statement["outstanding_cents"])
        directions = [e["direction"] for e in statement["escrow_entries"]]
        self.assertEqual(["hold", "release_to_driver"], directions)

    def test_resolve_rejected_releases_escrow_to_carrier(self):
        w = self.world
        deduction_id = self._deduct("dd-1", 20000)
        w.service.close_period(request_id="cl-1", actor_id="carrier1",
                               organization_id="org-c", month="2026-09")
        appeal = w.service.file_appeal(request_id="ap-1", actor_id="drv1",
                                       deduction_id=deduction_id, reason="不认可")
        w.service.resolve_appeal(request_id="rs-1", actor_id="reg1",
                                 appeal_id=appeal.resource_id, outcome="rejected",
                                 note="证据有效")
        statement = w.service.statement_detail("carrier1", "org-c:2026-09:drv1")
        self.assertEqual(0, statement["escrow_cents"])
        self.assertEqual(20000, statement["deduction_cents"])
        appeals = w.service.list_appeals("reg1")
        self.assertEqual("rejected", appeals[0]["status"])

    def test_appeal_window_uses_assignment_contract_snapshot(self):
        w = self.world
        w.service.publish_contract(request_id="c-1v2", actor_id="carrier1", driver_id="drv1",
                                   effective_from="2026-09-01T00:00:00Z",
                                   terms={**TERMS, "appeal_window_hours": 1})
        # 新合同版本只影响之后派单；task-1 的派单快照仍是 168 小时窗口，
        # 因此关账 2 小时后申诉依然被允许。
        deduction_id = self._deduct("dd-1", 20000)
        w.service.close_period(request_id="cl-1", actor_id="carrier1",
                               organization_id="org-c", month="2026-09")
        w.clock.advance(hours=2)
        appeal = w.service.file_appeal(request_id="ap-1", actor_id="drv1",
                                       deduction_id=deduction_id, reason="测试")
        self.assertFalse(appeal.replayed)

    def test_appeal_deadline_uses_task_contract_snapshot(self):
        w = self.world
        # 重新建一个使用 1 小时申诉时限合同的任务。
        w.service.publish_contract(request_id="c-1v2", actor_id="carrier1", driver_id="drv1",
                                   effective_from="2026-09-01T00:00:00Z",
                                   terms={**TERMS, "appeal_window_hours": 1})
        w.service.create_task(request_id="t-2", actor_id="carrier1", task_id="task-2",
                              origin="甲地", destination="乙地",
                              planned_start="2026-10-02T01:00:00Z",
                              planned_end="2026-10-02T03:00:00Z", base_freight_cents=1000)
        w.service.assign_task(request_id="as-2", actor_id="carrier1",
                              task_id="task-2", driver_id="drv1")
        w.service.accept_task(request_id="ac-2", actor_id="drv1", task_id="task-2")
        w.clock.set(datetime(2026, 10, 2, 3, 0, tzinfo=timezone.utc))
        w.backfill("ev-t2", "task-2", "drv1", "driving",
                   "2026-10-02T01:00:00Z", "2026-10-02T02:00:00Z")
        w.service.complete_task(request_id="fin-2", actor_id="carrier1", task_id="task-2",
                                occurred_at="2026-10-02T03:00:00Z")
        w.service.settle_task(request_id="st-2", actor_id="carrier1", task_id="task-2")
        w.service.submit_evidence(request_id="evd-2", actor_id="carrier1", task_id="task-2",
                                  kind="late_arrival", detail={"minutes": 10})
        evidence_id = w.service.database.connection.execute(
            "SELECT evidence_id FROM dr_evidence WHERE task_id='task-2'").fetchone()["evidence_id"]
        deduction = w.service.propose_deduction(
            request_id="dd-2", actor_id="carrier1", task_id="task-2", driver_id="drv1",
            amount_cents=1000, reason="迟到", evidence_id=evidence_id)
        w.service.close_period(request_id="cl-2", actor_id="carrier1",
                               organization_id="org-c", month="2026-10")
        w.clock.advance(hours=2)
        with self.assertRaises(ConflictError) as ctx:
            w.service.file_appeal(request_id="ap-2", actor_id="drv1",
                                  deduction_id=deduction.resource_id, reason="超时申诉")
        self.assertIn("申诉时限", str(ctx.exception))

    def test_deduction_cap_cannot_exceed_task_earnings(self):
        w = self.world
        statement = w.service.statement_detail("carrier1", "org-c:2026-09:drv1")
        with self.assertRaises(ValidationError):
            self._deduct("dd-huge", statement["gross_cents"] + 1)

    def test_cancel_deduction_returns_amount(self):
        w = self.world
        deduction_id = self._deduct("dd-1", 20000)
        w.service.cancel_deduction(request_id="cd-1", actor_id="carrier1",
                                   deduction_id=deduction_id)
        statement = w.service.statement_detail("carrier1", "org-c:2026-09:drv1")
        self.assertEqual(0, statement["deduction_cents"])
        self.assertEqual(statement["gross_cents"], statement["payable_cents"])


class PeriodRecomputeTest(unittest.TestCase):
    def setUp(self):
        self.world = World()
        w = self.world
        w.make_task("task-1", start="2026-09-30T12:00:00Z", end="2026-09-30T20:00:00Z")
        w.backfill("ev-1", "task-1", "drv1", "driving",
                   "2026-09-30T12:00:00Z", "2026-09-30T16:00:00Z")
        w.service.complete_task(request_id="fin-1", actor_id="carrier1", task_id="task-1",
                                occurred_at="2026-09-30T20:00:00Z")

    def tearDown(self):
        self.world.close()

    def test_close_blocks_when_tasks_unsettled(self):
        w = self.world
        with self.assertRaises(ConflictError) as ctx:
            w.service.close_period(request_id="cl-1", actor_id="carrier1",
                                   organization_id="org-c", month="2026-10")
        self.assertIn("task-1", str(ctx.exception))
        w.service.settle_task(request_id="st-1", actor_id="carrier1", task_id="task-1")
        receipt = w.service.close_period(request_id="cl-1", actor_id="carrier1",
                                         organization_id="org-c", month="2026-10")
        self.assertFalse(receipt.replayed)

    def test_settle_after_close_is_rejected(self):
        w = self.world
        w.service.settle_task(request_id="st-1", actor_id="carrier1", task_id="task-1")
        w.service.close_period(request_id="cl-1", actor_id="carrier1",
                               organization_id="org-c", month="2026-10")
        with self.assertRaises(ConflictError):
            w.service.settle_task(request_id="st-2", actor_id="carrier1", task_id="task-1")
        replay = w.service.settle_task(request_id="st-1", actor_id="carrier1", task_id="task-1")
        self.assertTrue(replay.replayed)

    def test_recompute_closed_period_with_new_rules(self):
        w = self.world
        w.service.settle_task(request_id="st-1", actor_id="carrier1", task_id="task-1")
        w.service.close_period(request_id="cl-1", actor_id="carrier1",
                               organization_id="org-c", month="2026-10")
        before = w.service.statement_detail("carrier1", "org-c:2026-10:drv1")
        w.service.publish_contract(request_id="c-1v2", actor_id="carrier1", driver_id="drv1",
                                   effective_from="2026-10-01T00:00:00Z",
                                   terms={**TERMS, "minimums": {"per_trip_cents": 500000}})
        receipt = w.service.recompute_period(request_id="rc-1", actor_id="reg1",
                                             organization_id="org-c", month="2026-10")
        self.assertFalse(receipt.replayed)
        reports = w.service.list_recompute_reports("reg1")
        self.assertEqual(1, len(reports))
        result = reports[0]["result"]
        self.assertEqual("closed", result["period_status"])
        entry = result["statements"][0]
        # 旧规则：基础 200000 + 驾驶 4 小时 24000 + 夜间 2 小时 4000 = 228000。
        self.assertEqual(228000, entry["old_gross_cents"])
        # 新规则最低保障 500000，复算差额 272000。
        self.assertEqual(500000, entry["new_gross_cents"])
        self.assertEqual(272000, entry["delta_cents"])
        after = w.service.statement_detail("carrier1", "org-c:2026-10:drv1")
        self.assertEqual(before["gross_cents"], after["gross_cents"])
        self.assertEqual(before["payable_cents"], after["payable_cents"])

    def test_recompute_with_explicit_contract_version(self):
        w = self.world
        w.service.settle_task(request_id="st-1", actor_id="carrier1", task_id="task-1")
        w.service.close_period(request_id="cl-1", actor_id="carrier1",
                               organization_id="org-c", month="2026-10")
        w.service.publish_contract(request_id="c-1v2", actor_id="carrier1", driver_id="drv1",
                                   effective_from="2026-10-01T00:00:00Z",
                                   terms={**TERMS, "minimums": {"per_trip_cents": 500000}})
        w.service.recompute_period(request_id="rc-1", actor_id="carrier1",
                                   organization_id="org-c", month="2026-10",
                                   contract_version=1)
        reports = w.service.list_recompute_reports("carrier1")
        entry = reports[0]["result"]["statements"][0]
        self.assertEqual(0, entry["delta_cents"])
        self.assertEqual("drv1@1", entry["tasks"][0]["new_rule_version"])


class ViolationTest(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.world.make_task("task-1", start="2026-09-30T00:00:00Z",
                             end="2026-09-30T08:00:00Z")

    def tearDown(self):
        self.world.close()

    def test_continuous_driving_violation_attributes_responsibility(self):
        w = self.world
        w.backfill("ev-1", "task-1", "drv1", "driving",
                   "2026-09-30T00:00:00Z", "2026-09-30T05:00:00Z")
        violations = w.service.list_violations("reg1")
        self.assertEqual(1, len(violations))
        violation = violations[0]
        self.assertEqual("max_continuous_driving", violation["rule"])
        self.assertEqual(300, violation["detail"]["measured_minutes"])
        self.assertEqual(240, violation["detail"]["limit_minutes"])
        self.assertEqual("carrier1", violation["responsible_actor_id"])
        self.assertEqual("org-c", violation["organization_id"])

    def test_daily_limits_create_violations_once(self):
        w = self.world
        w.backfill("ev-1", "task-1", "drv1", "driving",
                   "2026-09-30T00:00:00Z", "2026-09-30T06:00:00Z")
        w.backfill("ev-2", "task-1", "drv1", "loading",
                   "2026-09-30T06:00:00Z", "2026-09-30T07:00:00Z")
        w.backfill("ev-3", "task-1", "drv1", "driving",
                   "2026-09-30T07:00:00Z", "2026-09-30T13:00:00Z")
        violations = w.service.list_violations("reg1")
        rules = sorted(v["rule"] for v in violations)
        self.assertIn("max_daily_driving", rules)
        self.assertIn("max_daily_duty", rules)
        self.assertIn("max_continuous_driving", rules)
        # 重复检测同一窗口不产生重复记录。
        count = len(violations)
        w.backfill("ev-4", "task-1", "drv1", "rest",
                   "2026-09-30T13:00:00Z", "2026-09-30T14:00:00Z")
        self.assertEqual(count, len(w.service.list_violations("reg1")))

    def test_violation_views_are_scoped(self):
        w = self.world
        w.backfill("ev-1", "task-1", "drv1", "driving",
                   "2026-09-30T00:00:00Z", "2026-09-30T05:00:00Z")
        self.assertEqual(1, len(w.service.list_violations("reg1")))
        self.assertEqual(1, len(w.service.list_violations("carrier1")))
        self.assertEqual(1, len(w.service.list_violations("drv1")))
        self.assertEqual(0, len(w.service.list_violations("drv2")))


class PermissionTest(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.world.make_task("task-1", start="2026-09-30T00:00:00Z",
                             end="2026-09-30T08:00:00Z")
        self.world.backfill("ev-1", "task-1", "drv1", "driving",
                            "2026-09-30T01:00:00Z", "2026-09-30T03:00:00Z")
        self.world.service.complete_task(request_id="fin-1", actor_id="carrier1",
                                         task_id="task-1",
                                         occurred_at="2026-09-30T08:00:00Z")
        self.world.service.settle_task(request_id="st-1", actor_id="carrier1", task_id="task-1")

    def tearDown(self):
        self.world.close()

    def test_role_write_guards(self):
        w = self.world
        with self.assertRaises(PermissionDenied):
            w.service.create_task(request_id="t-x", actor_id="drv1", task_id="task-x",
                                  origin="甲", destination="乙",
                                  planned_start="2026-10-01T00:00:00Z",
                                  planned_end="2026-10-01T01:00:00Z", base_freight_cents=1)
        with self.assertRaises(PermissionDenied):
            w.service.publish_regulation(request_id="r-x", actor_id="carrier1",
                                         regulation_id="hos-x", rules={})
        with self.assertRaises(PermissionDenied):
            w.service.record_event(request_id="ev-x", actor_id="reg1", task_id="task-1",
                                   event_type="segment_start", driver_id="drv1", kind="rest",
                                   occurred_at="2026-09-30T04:00:00Z",
                                   ended_at="2026-09-30T05:00:00Z")

    def test_statement_views_are_scoped(self):
        w = self.world
        own = w.service.list_statements("drv1")
        self.assertEqual(1, len(own))
        self.assertEqual("drv1", own[0]["driver_id"])
        self.assertEqual([], w.service.list_statements("drv2"))
        self.assertEqual(1, len(w.service.list_statements("carrier1")))
        self.assertEqual(1, len(w.service.list_statements("reg1")))
        with self.assertRaises(PermissionDenied):
            w.service.statement_detail("drv2", "org-c:2026-09:drv1")
        detail = w.service.statement_detail("reg1", "org-c:2026-09:drv1")
        self.assertEqual("org-c:2026-09:drv1", detail["statement_id"])

    def test_cross_org_write_is_rejected(self):
        w = self.world
        w.service.register_organization(request_id="org-c2", actor_id="admin1",
                                        organization_id="org-c2", name="另一企业")
        w.service.register_actor(request_id="a-c2", actor_id="admin1", new_actor_id="carrier2",
                                 display_name="另一调度", role="carrier", organization_id="org-c2")
        with self.assertRaises(PermissionDenied):
            w.service.assign_task(request_id="as-x", actor_id="carrier2",
                                  task_id="task-1", driver_id="drv1")
        with self.assertRaises(PermissionDenied):
            w.service.task_detail("carrier2", "task-1")

    def test_register_driver_requires_driver_actor(self):
        w = self.world
        with self.assertRaises(ValidationError):
            w.service.register_driver(request_id="d-x", actor_id="carrier1",
                                      driver_id="carrier1", license_no="X1", name="错")


if __name__ == "__main__":
    unittest.main()
