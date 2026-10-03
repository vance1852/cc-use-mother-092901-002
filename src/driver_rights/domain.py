"""定义司机履约与权益保障项目的领域常量与规则文本。"""

from __future__ import annotations

from typing import Any

from transport_coordination.errors import ValidationError

# 履约区段类别：驾驶、装卸、等待、休息。
SEGMENT_KINDS = frozenset({"driving", "loading", "waiting", "rest"})

# 计入执勤（非休息）的区段类别，用于工时与夜间补贴计算。
DUTY_KINDS = frozenset({"driving", "loading", "waiting"})

# 任务状态机。
TASK_STATUSES = frozenset({"offered", "assigned", "in_progress", "completed", "cancelled"})

# 事件类型：区段开始/结束、双驾换班、途中救援。
EVENT_TYPES = frozenset({"segment_start", "segment_end", "handover", "rescue"})

# 结算单状态机：打开（可累加）→ 关账（冻结）→ 已支付。
STATEMENT_STATUSES = frozenset({"open", "closed", "paid"})

# 扣款状态机：提议 → 已入账 → 托管中 → 释放给承运人/司机；提议或入账阶段可撤销。
DEDUCTION_STATUSES = frozenset({
    "proposed", "applied", "escrowed", "released_to_carrier", "released_to_driver", "cancelled",
})

# 申诉状态机。
APPEAL_STATUSES = frozenset({"filed", "upheld", "rejected"})

# 结算明细行的组成部分。
LINE_COMPONENTS = frozenset({
    "base_freight", "driving", "loading", "waiting",
    "night_subsidy", "cross_night_subsidy", "minimum_topup", "deduction",
})

# 合同条款默认值：费率单位为分/小时，金额为分。
DEFAULT_TERMS: dict[str, Any] = {
    "currency": "CNY",
    "rates": {
        "driving_per_hour_cents": 0,
        "loading_per_hour_cents": 0,
        "waiting_per_hour_cents": 0,
        "free_waiting_minutes": 0,
    },
    "subsidies": {
        "night_per_hour_cents": 0,
        "night_start_hour": 22,
        "night_end_hour": 6,
        "cross_night_flat_cents": 0,
    },
    "minimums": {
        "per_trip_cents": 0,
    },
    "appeal_window_hours": 168,
}

# 工时监管规则默认值：连续驾驶 4 小时需至少 20 分钟休息，
# 每日驾驶不超过 8 小时、执勤不超过 12 小时，班次间至少休息 8 小时。
DEFAULT_REGULATION: dict[str, Any] = {
    "max_continuous_driving_minutes": 240,
    "min_break_after_continuous_driving_minutes": 20,
    "max_daily_driving_minutes": 480,
    "max_daily_duty_minutes": 720,
    "min_rest_between_shifts_minutes": 480,
}


def _non_negative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(f"{field} 必须是非负整数")
    return value


def _merge_section(default: dict[str, Any], provided: Any, section: str,
                   integer_fields: frozenset[str]) -> dict[str, Any]:
    if provided is None:
        return dict(default)
    if not isinstance(provided, dict):
        raise ValidationError(f"{section} 必须是对象")
    unknown = set(provided) - set(default)
    if unknown:
        raise ValidationError(f"{section} 包含未知字段: {sorted(unknown)[0]}")
    merged = dict(default)
    merged.update(provided)
    for field in integer_fields:
        merged[field] = _non_negative_int(merged[field], f"{section}.{field}")
    return merged


def normalize_terms(terms: Any) -> dict[str, Any]:
    """校验并补全合同条款，保证最低结算、补贴与申诉时限字段齐全。"""

    if not isinstance(terms, dict):
        raise ValidationError("terms 必须是对象")
    unknown = set(terms) - set(DEFAULT_TERMS)
    if unknown:
        raise ValidationError(f"terms 包含未知字段: {sorted(unknown)[0]}")
    currency = terms.get("currency", DEFAULT_TERMS["currency"])
    if not isinstance(currency, str) or not currency.strip():
        raise ValidationError("terms.currency 必须是非空字符串")
    normalized = {
        "currency": currency.strip(),
        "rates": _merge_section(
            DEFAULT_TERMS["rates"], terms.get("rates"), "terms.rates",
            frozenset({"driving_per_hour_cents", "loading_per_hour_cents",
                       "waiting_per_hour_cents", "free_waiting_minutes"})),
        "subsidies": _merge_section(
            DEFAULT_TERMS["subsidies"], terms.get("subsidies"), "terms.subsidies",
            frozenset({"night_per_hour_cents", "night_start_hour",
                       "night_end_hour", "cross_night_flat_cents"})),
        "minimums": _merge_section(
            DEFAULT_TERMS["minimums"], terms.get("minimums"), "terms.minimums",
            frozenset({"per_trip_cents"})),
        "appeal_window_hours": _non_negative_int(
            terms.get("appeal_window_hours", DEFAULT_TERMS["appeal_window_hours"]),
            "terms.appeal_window_hours"),
    }
    for field in ("night_start_hour", "night_end_hour"):
        if normalized["subsidies"][field] > 23:
            raise ValidationError(f"terms.subsidies.{field} 必须在 0 到 23 之间")
    if normalized["appeal_window_hours"] == 0:
        raise ValidationError("terms.appeal_window_hours 必须大于 0")
    return normalized


def normalize_rules(rules: Any) -> dict[str, Any]:
    """校验并补全工时监管规则。"""

    if not isinstance(rules, dict):
        raise ValidationError("rules 必须是对象")
    unknown = set(rules) - set(DEFAULT_REGULATION)
    if unknown:
        raise ValidationError(f"rules 包含未知字段: {sorted(unknown)[0]}")
    merged = dict(DEFAULT_REGULATION)
    merged.update(rules)
    for field in DEFAULT_REGULATION:
        merged[field] = _non_negative_int(merged[field], f"rules.{field}")
    for field in ("max_continuous_driving_minutes", "max_daily_driving_minutes",
                  "max_daily_duty_minutes", "min_rest_between_shifts_minutes"):
        if merged[field] == 0:
            raise ValidationError(f"rules.{field} 必须大于 0")
    return merged
