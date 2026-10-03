"""根据合同条款与履约区段纯函数式地计算结算明细。"""

from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo

from .domain import DUTY_KINDS
from .hours import (duty_intervals, merge_intervals, midnights_crossed,
                    minutes_between, night_overlap_minutes)
from .models import Segment, SettlementLine


def _per_hour(amount_per_hour: int, minutes: int) -> int:
    """按分钟折算小时费率，向下取整保证可复算。"""

    return amount_per_hour * minutes // 60


def compute_lines(*, task_id: str, driver_id: str, base_freight_cents: int,
                  segments: list[Segment], terms: dict[str, Any],
                  timezone_name: str, rule_version: str) -> list[SettlementLine]:
    """计算一名司机在一趟任务上的全部收入明细行。

    只统计已结束的区段；同一批区段与同一版条款重复计算结果完全一致，
    因此事件重放或重复结算不会产生新的金额。
    """

    tz = ZoneInfo(timezone_name)
    ended = [segment for segment in segments if segment.ended_at is not None]
    totals = {kind: 0 for kind in ("driving", "loading", "waiting")}
    for segment in ended:
        if segment.kind in totals:
            totals[segment.kind] += minutes_between(segment.started_at, segment.ended_at)

    rates = terms["rates"]
    subsidies = terms["subsidies"]
    minimums = terms["minimums"]

    duty = merge_intervals(duty_intervals(ended))
    night_minutes = night_overlap_minutes(
        duty, tz, subsidies["night_start_hour"], subsidies["night_end_hour"])
    crossed = midnights_crossed(duty, tz)
    billable_waiting = max(0, totals["waiting"] - rates["free_waiting_minutes"])

    lines: list[SettlementLine] = []

    def add(component: str, quantity: int, amount: int, detail: dict[str, Any]) -> None:
        lines.append(SettlementLine(
            task_id=task_id, component=component, ref_id="",
            quantity_minutes=quantity, amount_cents=amount,
            rule_version=rule_version, detail={"driver_id": driver_id, **detail}))

    add("base_freight", 0, base_freight_cents, {"note": "每趟基础运费"})
    add("driving", totals["driving"],
        _per_hour(rates["driving_per_hour_cents"], totals["driving"]),
        {"rate_per_hour_cents": rates["driving_per_hour_cents"]})
    add("loading", totals["loading"],
        _per_hour(rates["loading_per_hour_cents"], totals["loading"]),
        {"rate_per_hour_cents": rates["loading_per_hour_cents"]})
    add("waiting", billable_waiting,
        _per_hour(rates["waiting_per_hour_cents"], billable_waiting),
        {"rate_per_hour_cents": rates["waiting_per_hour_cents"],
         "free_waiting_minutes": rates["free_waiting_minutes"],
         "recorded_waiting_minutes": totals["waiting"]})
    add("night_subsidy", night_minutes,
        _per_hour(subsidies["night_per_hour_cents"], night_minutes),
        {"rate_per_hour_cents": subsidies["night_per_hour_cents"],
         "night_window": f"{subsidies['night_start_hour']:02d}:00-"
                         f"{subsidies['night_end_hour']:02d}:00"})
    add("cross_night_subsidy", crossed,
        subsidies["cross_night_flat_cents"] * crossed,
        {"flat_per_night_cents": subsidies["cross_night_flat_cents"], "nights": crossed})

    earned = sum(line.amount_cents for line in lines)
    minimum = minimums["per_trip_cents"]
    if earned < minimum:
        add("minimum_topup", 0, minimum - earned,
            {"per_trip_minimum_cents": minimum, "earned_before_topup_cents": earned})
    return lines


def lines_total(lines: list[SettlementLine]) -> int:
    """汇总明细行金额。"""

    return sum(line.amount_cents for line in lines)
