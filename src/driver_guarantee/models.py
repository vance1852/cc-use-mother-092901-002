"""司机履约与权益保障服务的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# ---------------------------------------------------------------------------
# 主体与目录
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Principal:
    """通过 API 访问的主体：司机、承运人、监管人员、管理员。"""

    principal_id: str
    role: str
    driver_id: str | None
    carrier_id: str | None
    display_name: str
    active: bool


@dataclass(frozen=True)
class Driver:
    """货运司机。"""

    driver_id: str
    display_name: str
    license_no: str
    organization_id: str
    active: bool


@dataclass(frozen=True)
class ContractVersion:
    """合同版本：把结算规则的来源固定下来。"""

    contract_id: str
    version: int
    carrier_id: str
    title: str
    body: dict[str, Any]
    effective_from: str
    created_at: str


@dataclass(frozen=True)
class RuleSet:
    """可版本化的最低结算与工时规则集合。"""

    ruleset_id: str
    version: int
    title: str
    limits: dict[str, int]
    settlement: dict[str, Any]
    appeal: dict[str, Any]
    effective_from: str
    created_at: str


# ---------------------------------------------------------------------------
# 任务与工时
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Trip:
    """一趟运输任务，携带合同与规则快照。"""

    trip_id: str
    carrier_id: str
    driver_id: str | None
    co_driver_id: str | None
    origin: str
    destination: str
    planned_pickup_at: str
    status: str
    contract_id: str
    contract_version: int
    ruleset_id: str
    ruleset_version: int
    rule_snapshot: dict[str, Any]
    estimated_driving_min: int
    estimated_work_min: int
    freight_amount: int
    completed_at: str | None
    forced: bool
    created_at: str


@dataclass(frozen=True)
class Segment:
    """由日志事件派生出的不可变工时区段。"""

    segment_id: str
    driver_id: str
    trip_id: str
    kind: str
    start_at: str
    end_at: str
    duration_min: int
    source_event_id: str
    frozen: bool


@dataclass(frozen=True)
class LedgerFreeze:
    """司机台账的冻结点。"""

    driver_id: str
    frozen_through: str
    reason: str
    created_at: str


@dataclass(frozen=True)
class AvailabilityView:
    """派单前原子校验的结果视图。"""

    driver_id: str
    at: str
    eligible: bool
    reasons: list[str]
    window_start_at: str | None
    window_end_at: str | None
    continuous_driving_min: int
    driving_last_24h_min: int
    work_last_24h_min: int
    continuous_work_min: int
    rests_last_24h: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# 结算、证据、申诉
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LedgerPeriod:
    """结算账期（可关账，关账后不可变，只能复算）。"""

    period_id: str
    carrier_id: str
    period_start: str
    period_end: str
    status: str
    ruleset_id: str
    ruleset_version: int
    closed_at: str | None


@dataclass(frozen=True)
class SettlementLine:
    """一条结算明细：运费、补贴或扣款。"""

    line_id: str
    period_id: str
    trip_id: str
    driver_id: str
    kind: str            # freight / subsidy / deduction
    category: str
    amount: int          # 分为单位，扣款为负
    evidence_id: str | None
    status: str          # payable / held / released / void
    anchor: str          # 幂等锚点，保证同事件重放不重复计费
    rule_ref: str
    memo: str
    created_at: str


@dataclass(frozen=True)
class DeductionEvidence:
    """扣款依据：异常记录及其到达时间。"""

    evidence_id: str
    carrier_id: str
    trip_id: str
    kind: str
    payload: dict[str, Any]
    occurred_at: str
    received_at: str
    created_by: str
    created_at: str


@dataclass(frozen=True)
class Appeal:
    """司机申诉及其时限状态。"""

    appeal_id: str
    trip_id: str
    driver_id: str
    carrier_id: str
    line_id: str | None
    reason: str
    status: str          # open / upheld / rejected / withdrawn
    evidence_id: str | None
    amount_held: int
    deadline_at: str
    created_at: str
    resolved_at: str | None
    resolution_note: str


@dataclass(frozen=True)
class EscrowView:
    """争议金额托管视图。"""

    appeal_id: str
    amount: int
    status: str
    released_to_driver: int
    returned_carrier: int


@dataclass(frozen=True)
class Recomputation:
    """规则调整后对已关账账期的复算对比。"""

    recomputation_id: str
    period_id: str
    ruleset_id: str
    ruleset_version: int
    delta: int
    original_total: int
    recomputed_total: int
    lines: list[dict[str, Any]]
    created_at: str
