"""运行司机履约与权益保障项目的离线端到端验收。

场景覆盖：跨夜双驾任务派单与履约记录、事件重放、结算与最低保障、
扣款证据、账期关账、申诉托管、支付不阻断、规则调整后复算已关账账期，
以及两类投诉对应的保护（关账后不可追扣、冻结区段不可补录）。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from transport_coordination.clock import ManualClock
from transport_coordination.errors import ConflictError

from .service import DriverRightsService
from .storage import DriverRightsDatabase

TERMS = {
    "rates": {"driving_per_hour_cents": 6000, "loading_per_hour_cents": 4000,
              "waiting_per_hour_cents": 3000, "free_waiting_minutes": 30},
    "subsidies": {"night_per_hour_cents": 2000, "night_start_hour": 22,
                  "night_end_hour": 6, "cross_night_flat_cents": 8000},
    "minimums": {"per_trip_cents": 30000},
    "appeal_window_hours": 168,
}


def _setup(service: DriverRightsService) -> None:
    service.register_organization(request_id="org-c", actor_id="bootstrap",
                                  organization_id="org-carrier", name="顺达运输")
    service.register_actor(request_id="a-admin", actor_id="bootstrap", new_actor_id="admin1",
                           display_name="系统管理员", role="admin", organization_id="org-carrier")
    service.register_organization(request_id="org-r", actor_id="admin1",
                                  organization_id="org-regulator", name="交通监管局")
    service.register_actor(request_id="a-carrier", actor_id="admin1", new_actor_id="carrier1",
                           display_name="承运调度", role="carrier", organization_id="org-carrier")
    service.register_actor(request_id="a-reg", actor_id="admin1", new_actor_id="reg1",
                           display_name="监管专员", role="regulator",
                           organization_id="org-regulator")
    service.register_actor(request_id="a-d1", actor_id="admin1", new_actor_id="drv1",
                           display_name="司机甲", role="driver", organization_id="org-carrier")
    service.register_actor(request_id="a-d2", actor_id="admin1", new_actor_id="drv2",
                           display_name="司机乙", role="driver", organization_id="org-carrier")
    service.register_driver(request_id="d-1", actor_id="carrier1", driver_id="drv1",
                            license_no="A3201", name="司机甲")
    service.register_driver(request_id="d-2", actor_id="carrier1", driver_id="drv2",
                            license_no="A3202", name="司机乙")
    service.publish_regulation(request_id="reg-1", actor_id="reg1",
                               regulation_id="hos-default", rules={})
    service.publish_contract(request_id="c-1", actor_id="carrier1", driver_id="drv1",
                             effective_from="2026-09-01T00:00:00Z", terms=TERMS)
    service.publish_contract(request_id="c-2", actor_id="carrier1", driver_id="drv2",
                             effective_from="2026-09-01T00:00:00Z", terms=TERMS)


def run() -> dict[str, object]:
    """执行完整业务链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = DriverRightsDatabase(Path(directory) / "acceptance.sqlite3")
        clock = ManualClock(datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc))
        service = DriverRightsService(database, clock)
        _setup(service)
        service.create_task(request_id="t-1", actor_id="carrier1", task_id="task-1",
                            origin="上海", destination="杭州",
                            planned_start="2026-09-30T12:00:00Z",
                            planned_end="2026-10-01T00:00:00Z",
                            base_freight_cents=200000)
        service.assign_task(request_id="as-1", actor_id="carrier1", task_id="task-1",
                            driver_id="drv1")
        service.accept_task(request_id="ac-1", actor_id="drv1", task_id="task-1")
        clock.set(datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc))
        service.record_event(request_id="ev-1", actor_id="drv1", task_id="task-1",
                             event_type="segment_start", kind="driving")
        replay = service.record_event(request_id="ev-1", actor_id="drv1", task_id="task-1",
                                      event_type="segment_start", kind="driving")
        clock.set(datetime(2026, 9, 30, 14, 0, tzinfo=timezone.utc))
        service.record_event(request_id="ev-2", actor_id="drv1", task_id="task-1",
                             event_type="segment_start", kind="loading")
        clock.set(datetime(2026, 9, 30, 15, 0, tzinfo=timezone.utc))
        service.record_event(request_id="ev-3", actor_id="drv1", task_id="task-1",
                             event_type="segment_start", kind="driving")
        clock.set(datetime(2026, 9, 30, 18, 0, tzinfo=timezone.utc))
        service.record_event(request_id="ev-4", actor_id="drv1", task_id="task-1",
                             event_type="segment_start", kind="waiting")
        clock.set(datetime(2026, 9, 30, 19, 0, tzinfo=timezone.utc))
        service.record_event(request_id="ev-5", actor_id="drv1", task_id="task-1",
                             event_type="segment_start", kind="driving")
        clock.set(datetime(2026, 9, 30, 20, 0, tzinfo=timezone.utc))
        service.record_event(request_id="ev-6", actor_id="carrier1", task_id="task-1",
                             event_type="handover", driver_id="drv1", to_driver_id="drv2")
        clock.set(datetime(2026, 9, 30, 23, 0, tzinfo=timezone.utc))
        service.record_event(request_id="ev-7", actor_id="drv2", task_id="task-1",
                             event_type="segment_end")
        service.complete_task(request_id="fin-1", actor_id="carrier1", task_id="task-1")
        service.settle_task(request_id="st-1", actor_id="carrier1", task_id="task-1")
        evidence = service.submit_evidence(request_id="evd-1", actor_id="carrier1",
                                           task_id="task-1", kind="late_arrival",
                                           detail={"minutes": 45, "pod": "POD-77"})
        deduction = service.propose_deduction(request_id="dd-1", actor_id="carrier1",
                                              task_id="task-1", driver_id="drv1",
                                              amount_cents=20000, reason="迟到 45 分钟",
                                              evidence_id=evidence.resource_id)
        service.close_period(request_id="cl-1", actor_id="carrier1",
                             organization_id="org-carrier", month="2026-10")
        late_deduction_blocked = False
        try:
            service.propose_deduction(request_id="dd-2", actor_id="carrier1", task_id="task-1",
                                      driver_id="drv1", amount_cents=5000, reason="月末补扣",
                                      evidence_id=evidence.resource_id)
        except ConflictError:
            late_deduction_blocked = True
        frozen_backfill_blocked = False
        try:
            service.record_event(request_id="ev-bf", actor_id="carrier1", task_id="task-1",
                                 event_type="segment_start", driver_id="drv1", kind="rest",
                                 occurred_at="2026-09-30T13:00:00Z",
                                 ended_at="2026-09-30T13:30:00Z")
        except ConflictError:
            frozen_backfill_blocked = True
        statement_id = "org-carrier:2026-10:drv1"
        before_appeal = service.statement_detail("carrier1", statement_id)
        appeal = service.file_appeal(request_id="ap-1", actor_id="drv1",
                                     deduction_id=deduction.resource_id, reason="堵车有报备记录")
        after_appeal = service.statement_detail("carrier1", statement_id)
        service.mark_statement_paid(request_id="pay-1", actor_id="carrier1",
                                    statement_id=statement_id)
        service.mark_statement_paid(request_id="pay-2", actor_id="carrier1",
                                    statement_id="org-carrier:2026-10:drv2")
        service.resolve_appeal(request_id="rs-1", actor_id="reg1",
                               appeal_id=appeal.resource_id, outcome="upheld",
                               note="堵车报备属实，扣款退回")
        after_resolve = service.statement_detail("carrier1", statement_id)
        service.publish_contract(request_id="c-1v2", actor_id="carrier1", driver_id="drv1",
                                 effective_from="2026-10-01T00:00:00Z",
                                 terms={**TERMS, "minimums": {"per_trip_cents": 300000}})
        service.recompute_period(request_id="rc-1", actor_id="reg1",
                                 organization_id="org-carrier", month="2026-10")
        reports = service.list_recompute_reports("reg1")
        recompute = reports[0]["result"]
        drv1_report = next(s for s in recompute["statements"] if s["driver_id"] == "drv1")
        valid, event_count = service.verify_audit()
        detail = service.task_detail("reg1", "task-1")
        result = {
            "status": "ok",
            "audit_valid": valid,
            "audit_events": event_count,
            "event_replayed": replay.replayed,
            "segments": len(detail["segments"]),
            "frozen_segments": sum(1 for s in detail["segments"] if s["frozen"]),
            "drv1_gross_cents": before_appeal["gross_cents"],
            "deduction_cents": before_appeal["deduction_cents"],
            "escrow_during_appeal_cents": after_appeal["escrow_cents"],
            "payable_undisputed_cents": after_appeal["payable_cents"],
            "returned_after_uphold_cents": after_resolve["returned_cents"],
            "outstanding_after_uphold_cents": after_resolve["outstanding_cents"],
            "late_deduction_blocked": late_deduction_blocked,
            "frozen_backfill_blocked": frozen_backfill_blocked,
            "recompute_period_status": recompute["period_status"],
            "recompute_delta_cents": drv1_report["delta_cents"],
            "statement_unchanged_after_recompute":
                service.statement_detail("carrier1", statement_id)["gross_cents"]
                == before_appeal["gross_cents"],
        }
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    ok = (result["status"] == "ok" and result["audit_valid"]
          and result["late_deduction_blocked"] and result["frozen_backfill_blocked"]
          and result["statement_unchanged_after_recompute"])
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
