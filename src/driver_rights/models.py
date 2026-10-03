"""定义司机履约项目在计算边界使用的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Segment:
    """表示一段已记录的履约时间（驾驶、装卸、等待或休息）。"""

    segment_id: str
    task_id: str
    driver_id: str
    kind: str
    started_at: datetime
    ended_at: datetime | None
    source: str
    event_id: str
    frozen: bool


@dataclass(frozen=True)
class SettlementLine:
    """表示一趟任务结算明细中的一行。"""

    task_id: str
    component: str
    ref_id: str
    quantity_minutes: int
    amount_cents: int
    rule_version: str
    detail: dict[str, Any]
