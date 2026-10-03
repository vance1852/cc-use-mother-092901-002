"""司机履约与权益保障服务的离线端到端验收。

覆盖投诉对应的两类核心风险：
1. 未满足休息要求时继续派单 —— 原子校验拒绝，强制派单留痕归责；
2. 月末用晚到异常记录追扣已结算运费 —— 逾期证据禁扣、申诉期冻结扣款、
   争议金额单独托管、关账后可按新规则复算。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from transport_coordination.errors import ConflictError

from .clock import ManualClock
from .service import GuaranteeService
from .storage import Database


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        clock = ManualClock(datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc))
        s = GuaranteeService(database, clock)

        # 建档
        s.bootstrap_admin(request_id="a0", principal_id="admin", display_name="管理员")
        s.register_carrier(request_id="a1", actor_id="admin", carrier_id="c1", name="示范承运人")
        s.register_driver(request_id="a2", actor_id="admin", driver_id="d1", display_name="张师傅",
                          license_no="LIC-001", organization_id="c1")
        s.register_driver(request_id="a3", actor_id="admin", driver_id="d2", display_name="李师傅",
                          license_no="LIC-002", organization_id="c1")
        s.register_principal(request_id="a4", actor_id="admin", principal_id="pd1", role="driver",
                             display_name="张师傅", driver_id="d1")
        s.register_principal(request_id="a5", actor_id="admin", principal_id="pc1", role="carrier",
                             display_name="调度员", carrier_id="c1")
        s.register_principal(request_id="a6", actor_id="admin", principal_id="pr1", role="regulator",
                             display_name="监管员")

        # 合同版本 + 规则版本 v1
        s.create_contract_version(request_id="a7", actor_id="pc1", contract_id="k1",
                                  carrier_id="c1", title="干线运输合同",
                                  body={"min_settlement": True})
        s.create_ruleset(request_id="a8", actor_id="admin", ruleset_id="rs", title="v1")

        # 任务 t1（预计驾驶 240 分钟）
        s.create_trip(request_id="a9", actor_id="pc1", trip_id="t1", carrier_id="c1",
                      origin="上海", destination="武汉",
                      planned_pickup_at="2026-10-01T00:00:00Z", contract_id="k1",
                      estimated_driving_min=240, freight_amount=30000)
        s.dispatch_trip(request_id="a10", actor_id="pc1", trip_id="t1", driver_id="d1",
                        at="2026-10-01T00:00:00Z")

        # 跨夜双驾：d1 驾驶 4 小时（含 40 分钟救援，救援不打断连续驾驶累计）
        s.log_interval(request_id="a11", actor_id="pd1", driver_id="d1", kind="driving",
                       start_at="2026-10-01T00:00:00Z", end_at="2026-10-01T02:00:00Z",
                       trip_id="t1")
        s.log_interval(request_id="a12", actor_id="pd1", driver_id="d1", kind="rescue",
                       start_at="2026-10-01T02:00:00Z", end_at="2026-10-01T02:40:00Z",
                       trip_id="t1")
        s.log_interval(request_id="a13", actor_id="pd1", driver_id="d1", kind="driving",
                       start_at="2026-10-01T02:40:00Z", end_at="2026-10-01T04:40:00Z",
                       trip_id="t1")

        # 同一事件重放不得重复计费
        replay = s.log_interval(request_id="a12", actor_id="pd1", driver_id="d1", kind="rescue",
                                start_at="2026-10-01T02:00:00Z", end_at="2026-10-01T02:40:00Z",
                                trip_id="t1")

        # 未满足休息要求时，新任务派单必须被原子拒绝
        s.create_trip(request_id="a14", actor_id="pc1", trip_id="t2", carrier_id="c1",
                      origin="武汉", destination="重庆",
                      planned_pickup_at="2026-10-01T04:40:00Z", contract_id="k1",
                      estimated_driving_min=60, freight_amount=10000)
        rejected = False
        try:
            s.dispatch_trip(request_id="a15", actor_id="pc1", trip_id="t2", driver_id="d1",
                            at="2026-10-01T04:40:00Z")
        except ConflictError:
            rejected = True

        # 承运人强制派单（双驾 d2 同车），留痕归责
        forced = s.dispatch_trip(request_id="a16", actor_id="pc1", trip_id="t2", driver_id="d1",
                                 co_driver_id="d2", at="2026-10-01T04:40:00Z", force=True)

        # 双驾换班只能影响未冻结区段
        shift = s.shift_change(request_id="a17", actor_id="pc1", trip_id="t2",
                               relieved_driver_id="d1", relieving_driver_id="d2",
                               at="2026-10-01T04:40:00Z")
        s.complete_trip(request_id="a18", actor_id="pc1", trip_id="t2",
                        completed_at="2026-10-01T07:00:00Z")
        s.log_interval(request_id="a19", actor_id="pd1", driver_id="d1", kind="waiting",
                       start_at="2026-10-01T04:40:00Z", end_at="2026-10-01T05:40:00Z",
                       trip_id="t1")
        s.complete_trip(request_id="a20", actor_id="pc1", trip_id="t1",
                        completed_at="2026-10-01T05:40:00Z")

        # 监管超时责任：t2 为强制派单
        responsibility = s.overtime_responsibility(actor_id="pr1",
                                                   at="2026-10-01T04:40:00Z")
        forced_attributed = any(r["forced_dispatch"] and
                                r["responsibility"] == "carrier_forced_dispatch"
                                for r in responsibility)

        # 账期结算：最低运费 + 等待/救援补贴
        s.create_period(request_id="a21", actor_id="pc1", period_id="oct1", carrier_id="c1",
                        period_start="2026-10-01T00:00:00Z",
                        period_end="2026-10-02T00:00:00Z")
        settled = s.settle_period(request_id="a22", actor_id="pc1", period_id="oct1")
        anchors = {line["anchor"] for line in
                   s.list_settlement_lines(actor_id="pd1", period_id="oct1")["items"]}
        rescue_lines = [a for a in anchors if "rescue" in a]

        # 按时到达的证据可扣款
        ev = s.register_evidence(request_id="a23", actor_id="pc1", trip_id="t1",
                                 kind="late_arrival", payload={"minutes": 12},
                                 occurred_at="2026-10-01T05:00:00Z",
                                 received_at="2026-10-02T00:00:00Z")
        before = s.list_settlement_lines(actor_id="pd1", period_id="oct1")
        deduction = s.add_deduction(request_id="a24", actor_id="pc1", period_id="oct1",
                                    trip_id="t1", driver_id="d1", amount=2000,
                                    category="late", memo="晚到扣款",
                                    evidence_id=ev["response"]["evidence_id"])

        # 月末才到（逾期 3 天）的异常记录不得追扣
        late_ev = s.register_evidence(request_id="a25", actor_id="pc1", trip_id="t1",
                                      kind="retroactive", payload={"case": "月末补录"},
                                      occurred_at="2026-10-01T05:30:00Z",
                                      received_at="2026-10-09T00:00:00Z")
        late_blocked = False
        try:
            s.add_deduction(request_id="a26", actor_id="pc1", period_id="oct1",
                            trip_id="t1", driver_id="d1", amount=9000,
                            category="retro", memo="月末追扣",
                            evidence_id=late_ev["response"]["evidence_id"])
        except ConflictError:
            late_blocked = True

        # 司机申诉：争议金额托管，无争议收入照常
        clock.set(datetime(2026, 10, 3, 0, 0, tzinfo=timezone.utc))
        uncontested_before = s.list_settlement_lines(
            actor_id="pd1", period_id="oct1")["uncontested_income"]
        appeal = s.file_appeal(request_id="a27", actor_id="pd1", trip_id="t1",
                               reason="晚到因救援等待", line_id=deduction["response"]["line_id"])
        during = s.list_settlement_lines(actor_id="pd1", period_id="oct1")

        # 申诉期间不得继续扣款
        more_blocked = False
        try:
            s.add_deduction(request_id="a28", actor_id="pc1", period_id="oct1",
                            trip_id="t1", driver_id="d1", amount=1000,
                            category="late", memo="继续扣款",
                            evidence_id=ev["response"]["evidence_id"])
        except ConflictError:
            more_blocked = True

        # 监管裁决成立：托管金额退还司机
        resolution = s.resolve_appeal(request_id="a29", actor_id="pr1",
                                      appeal_id=appeal["response"]["appeal_id"],
                                      decision="upheld", note="救援导致，非司机责任")

        # 关账后原账期不可变；新规则复算过去账期
        s.close_period(request_id="a30", actor_id="pc1", period_id="oct1")
        s.create_ruleset(request_id="a31", actor_id="admin", ruleset_id="rs", title="v2",
                         settlement={"min_freight_amount": 35000, "wait_subsidy_per_min": 8},
                         effective_from="2026-10-10T00:00:00Z")
        recomp = s.recompute_closed_period(request_id="a32", actor_id="pr1", period_id="oct1",
                                           ruleset_id="rs", ruleset_version=2)

        # 台账冻结后，补录只能落在未冻结区段
        s.freeze_ledger(request_id="a33", actor_id="pc1", driver_id="d1",
                        frozen_through="2026-10-01T12:00:00Z", reason="10月1日台账核对")
        frozen_blocked = False
        try:
            s.log_interval(request_id="a34", actor_id="pd1", driver_id="d1", kind="backfill",
                           payload={"target_kind": "rest"},
                           start_at="2026-10-01T10:00:00Z",
                           end_at="2026-10-01T11:00:00Z")
        except ConflictError:
            frozen_blocked = True

        audit_valid, audit_events = s.verify_audit()
        result = {
            "status": "ok",
            "dispatch_rejected_when_unrested": rejected,
            "forced_dispatch_attributed_to_carrier": forced_attributed,
            "rescue_replayed": replay["replayed"],
            "rescue_lines_count": len(rescue_lines),
            "settled_lines": settled["response"]["lines_added"],
            "late_evidence_blocked": late_blocked,
            "uncontested_income_preserved": during["uncontested_income"] == uncontested_before,
            "escrow_held": during["escrow_held_total"] == 2000,
            "deductions_blocked_during_appeal": more_blocked,
            "appeal_released_to_driver": resolution["response"]["released_to_driver"] == 2000,
            "recompute_delta": recomp["response"]["delta"],
            "frozen_backfill_blocked": frozen_blocked,
            "shift_recorded": shift["resource_type"] == "shift_change",
            "forced_flag": forced["response"]["forced"],
            "audit_valid": audit_valid,
            "audit_events": audit_events,
        }
        database.close()
        return result


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    expected = {k: v for k, v in result.items() if k != "recompute_delta"}
    return 0 if result["status"] == "ok" and all(
        v is True or k in ("status", "rescue_lines_count", "settled_lines",
                           "audit_events", "recompute_delta")
        for k, v in expected.items()) and result["recompute_delta"] > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
