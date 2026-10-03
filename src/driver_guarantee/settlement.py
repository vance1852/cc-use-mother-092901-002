"""最低结算规则引擎（纯函数，初次结算与账期复算共用同一套算法）。

金额单位为分（整数）。复算时用新规则重新计算运费与补贴，
扣款行则依据证据到达时限重新校验；同一锚点的收入只出现一次，
保证事件重放不会重复计费。
"""

from __future__ import annotations

from typing import Any

from .domain import (
    LINE_DEDUCTION,
    LINE_FREIGHT,
    LINE_HELD,
    LINE_SUBSIDY,
    LINE_VOID,
    TRIP_CANCELLED,
    TRIP_COMPLETED,
)
from .worktime import add_minutes, parse_ts


DEFAULT_SETTLEMENT: dict[str, Any] = {
    "min_freight_amount": 20000,          # 每趟最低运费 200.00 元
    "free_wait_minutes": 30,              # 30 分钟内等待不计补贴
    "wait_subsidy_per_min": 5,            # 超出部分每分钟 0.05 元
    "loading_subsidy_per_min": 4,         # 装卸每分钟 0.04 元
    "rescue_allowance": 5000,             # 每次途中救援补贴 50.00 元
    "berth_allowance_per_event": 3000,    # 每次跨夜卧铺值守 30.00 元
    "max_deduction_delay_days": 3,        # 异常证据超过 trip 完成后 3 天不得扣款
}

DEFAULT_APPEAL = {
    "appeal_window_days": 7,             # 结算通知后 7 天申诉期
}


def merge_settlement(values: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(DEFAULT_SETTLEMENT)
    if values:
        merged.update(values)
    return merged


def merge_appeal(values: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(DEFAULT_APPEAL)
    if values:
        merged.update(values)
    return merged


def _anchor(*parts: Any) -> str:
    return ":".join(str(p) for p in parts)


def _minutes(seg: dict[str, Any]) -> int:
    return int(round((parse_ts(seg["end_at"]) - parse_ts(seg["start_at"])).total_seconds() / 60))


def compute_trip_earnings(trip: dict[str, Any], segments: list[dict[str, Any]],
                          rules: dict[str, Any], driver_id: str | None = None,
                          include_freight: bool = True) -> list[dict[str, Any]]:
    """计算单趟任务中某名司机的运费与补贴行。

    已完成任务按约定运费与最低运费孰高结算（仅主驾，双驾不重复发运费）；
    被取消的任务不结运费，但取消前实际发生的等待、装卸、救援仍按规则计补贴。
    """

    rules = merge_settlement(rules)
    trip_id = trip["trip_id"]
    driver_id = driver_id or trip.get("driver_id")
    if not driver_id:
        return []
    mine = [g for g in segments if g.get("trip_id") == trip_id and g.get("driver_id") == driver_id]
    lines: list[dict[str, Any]] = []

    if include_freight and trip["status"] == TRIP_COMPLETED:
        agreed = int(trip.get("freight_amount") or 0)
        freight = max(agreed, int(rules["min_freight_amount"]))
        lines.append({
            "kind": LINE_FREIGHT, "category": "freight", "amount": freight,
            "anchor": _anchor("freight", trip_id, driver_id),
            "rule_ref": "settlement.min_freight_amount",
            "memo": f"任务 {trip_id} 运费（约定 {agreed}，保底 {rules['min_freight_amount']}）",
        })

    # 等待补贴：扣除免费等待时长
    waiting = sum(_minutes(g) for g in mine if g["kind"] == "waiting")
    billable_wait = max(0, waiting - int(rules["free_wait_minutes"]))
    if billable_wait:
        lines.append({
            "kind": LINE_SUBSIDY, "category": "waiting",
            "amount": billable_wait * int(rules["wait_subsidy_per_min"]),
            "anchor": _anchor("subsidy", "waiting", trip_id, driver_id),
            "rule_ref": "settlement.wait_subsidy_per_min",
            "memo": f"等待 {waiting} 分钟，免计 {rules['free_wait_minutes']} 分钟",
        })

    # 装卸补贴
    loading = sum(_minutes(g) for g in mine if g["kind"] == "loading")
    if loading:
        lines.append({
            "kind": LINE_SUBSIDY, "category": "loading",
            "amount": loading * int(rules["loading_subsidy_per_min"]),
            "anchor": _anchor("subsidy", "loading", trip_id, driver_id),
            "rule_ref": "settlement.loading_subsidy_per_min",
            "memo": f"装卸 {loading} 分钟",
        })

    # 救援补贴：按救援事件计，每事件一次，重放不重复
    seen_events: set[str] = set()
    for seg in mine:
        if seg["kind"] == "rescue" and seg["source_event_id"] not in seen_events:
            seen_events.add(seg["source_event_id"])
            lines.append({
                "kind": LINE_SUBSIDY, "category": "rescue",
                "amount": int(rules["rescue_allowance"]),
                "anchor": _anchor("subsidy", "rescue", trip_id, driver_id, seg["source_event_id"]),
                "rule_ref": "settlement.rescue_allowance",
                "memo": f"途中救援事件 {seg['source_event_id']}",
            })

    # 跨夜卧铺值守补贴
    seen_berth: set[str] = set()
    for seg in mine:
        if seg["kind"] == "berth_rest" and seg["source_event_id"] not in seen_berth:
            seen_berth.add(seg["source_event_id"])
            lines.append({
                "kind": LINE_SUBSIDY, "category": "berth",
                "amount": int(rules["berth_allowance_per_event"]),
                "anchor": _anchor("subsidy", "berth", trip_id, driver_id, seg["source_event_id"]),
                "rule_ref": "settlement.berth_allowance_per_event",
                "memo": f"跨夜卧铺值守 {seg['source_event_id']}",
            })

    return lines


def evidence_is_timely(evidence: dict[str, Any], rules: dict[str, Any]) -> bool:
    """异常证据必须在任务完成后的规定天数内到达，否则不得扣款。"""

    rules = merge_settlement(rules)
    if not evidence.get("trip_completed_at"):
        return False
    deadline = add_minutes(evidence["trip_completed_at"],
                           int(rules["max_deduction_delay_days"]) * 24 * 60)
    return evidence["received_at"] <= deadline


def revalidate_deductions(deductions: list[dict[str, Any]], rules: dict[str, Any]) -> list[dict[str, Any]]:
    """对既有扣款行按证据时限重新校验。

    - 逾期到达的证据对应扣款作废；
    - 已因申诉成立而作废（void）的扣款保持作废——裁决结果不随规则复算复活；
    - 仍在申诉托管（held）中的扣款保持托管，等待裁决。
    """

    out: list[dict[str, Any]] = []
    for line in deductions:
        prior_status = line.get("status", "payable")
        if prior_status == LINE_VOID:
            valid, status = False, LINE_VOID
            memo = line.get("memo") or f"申诉成立，扣款作废（{line['anchor']}）"
        elif prior_status == LINE_HELD:
            valid, status = True, LINE_HELD
            memo = line.get("memo", "")
        else:
            valid = evidence_is_timely(line["evidence"], rules)
            status = LINE_VOID if not valid else prior_status
            memo = line.get("memo", "") if valid else f"证据逾期到达，扣款作废（{line['anchor']}）"
        out.append({
            "kind": LINE_DEDUCTION,
            "category": line.get("category", "penalty"),
            "amount": line["amount"] if valid else 0,
            "status": status,
            "anchor": line["anchor"],
            "rule_ref": "settlement.max_deduction_delay_days",
            "memo": memo,
            "evidence_id": line.get("evidence_id"),
        })
    return out


def recompute_period(*, trips: list[dict[str, Any]], segments: list[dict[str, Any]],
                     deductions: list[dict[str, Any]],
                     rules: dict[str, Any]) -> list[dict[str, Any]]:
    """用给定规则重算整个账期，返回按锚点去重后的全部结算行。

    每趟任务的主驾获得运费与其本人补贴；副驾只获得本人补贴（运费不重复）。
    """

    lines: dict[str, dict[str, Any]] = {}
    for trip in trips:
        if trip["status"] == TRIP_CANCELLED and not trip.get("driver_id"):
            continue
        drivers = {d for d in (trip.get("driver_id"), trip.get("co_driver_id")) if d}
        for index, driver_id in enumerate(sorted(drivers)):
            for line in compute_trip_earnings(trip, segments, rules, driver_id=driver_id,
                                              include_freight=(driver_id == trip.get("driver_id"))):
                lines.setdefault(line["anchor"], line)
    for line in revalidate_deductions(deductions, rules):
        lines.setdefault(line["anchor"], line)
    return [lines[key] for key in sorted(lines)]
