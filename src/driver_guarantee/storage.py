"""司机履约与权益保障服务的 SQLite 表结构与事务边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

-- 承运人（运输企业）
CREATE TABLE IF NOT EXISTS carriers (
    carrier_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- API 访问主体：司机、承运人、监管人员、管理员
CREATE TABLE IF NOT EXISTS principals (
    principal_id TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    driver_id TEXT REFERENCES drivers(driver_id),
    carrier_id TEXT REFERENCES carriers(carrier_id),
    display_name TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

-- 货运司机
CREATE TABLE IF NOT EXISTS drivers (
    driver_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    license_no TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    UNIQUE(license_no)
);

-- 合同版本
CREATE TABLE IF NOT EXISTS contract_versions (
    contract_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    carrier_id TEXT NOT NULL REFERENCES carriers(carrier_id),
    title TEXT NOT NULL,
    body_json TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(contract_id, version)
);

-- 规则集合版本（工时上限 + 最低结算 + 申诉时限）
CREATE TABLE IF NOT EXISTS ruleset_versions (
    ruleset_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    title TEXT NOT NULL,
    limits_json TEXT NOT NULL,
    settlement_json TEXT NOT NULL,
    appeal_json TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(ruleset_id, version)
);

-- 运输任务（每趟快照合同与规则；取消的未派任务允许 driver 为空）
CREATE TABLE IF NOT EXISTS trips (
    trip_id TEXT PRIMARY KEY,
    carrier_id TEXT NOT NULL REFERENCES carriers(carrier_id),
    driver_id TEXT REFERENCES drivers(driver_id),
    co_driver_id TEXT REFERENCES drivers(driver_id),
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    planned_pickup_at TEXT NOT NULL,
    status TEXT NOT NULL,
    contract_id TEXT NOT NULL,
    contract_version INTEGER NOT NULL,
    ruleset_id TEXT NOT NULL,
    ruleset_version INTEGER NOT NULL,
    rule_snapshot_json TEXT NOT NULL,
    estimated_driving_min INTEGER NOT NULL DEFAULT 0,
    estimated_work_min INTEGER NOT NULL DEFAULT 0,
    freight_amount INTEGER NOT NULL DEFAULT 0,
    completed_at TEXT,
    forced INTEGER NOT NULL DEFAULT 0 CHECK(forced IN (0,1)),
    created_at TEXT NOT NULL
);

-- 可控时钟记录的原始工时日志事件
CREATE TABLE IF NOT EXISTS work_log_events (
    event_id TEXT PRIMARY KEY,
    driver_id TEXT NOT NULL REFERENCES drivers(driver_id),
    trip_id TEXT REFERENCES trips(trip_id),
    kind TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT,
    recorded_at TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    superseded_by TEXT,
    UNIQUE(driver_id, idempotency_key)
);

-- 由日志事件派生的不可变工时区段
CREATE TABLE IF NOT EXISTS work_segments (
    segment_id TEXT PRIMARY KEY,
    driver_id TEXT NOT NULL REFERENCES drivers(driver_id),
    trip_id TEXT REFERENCES trips(trip_id),
    kind TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    duration_min INTEGER NOT NULL CHECK(duration_min >= 0),
    source_event_id TEXT NOT NULL REFERENCES work_log_events(event_id),
    frozen INTEGER NOT NULL DEFAULT 0 CHECK(frozen IN (0,1)),
    UNIQUE(source_event_id, driver_id, kind, start_at)
);

-- 台账冻结点
CREATE TABLE IF NOT EXISTS ledger_freezes (
    driver_id TEXT NOT NULL REFERENCES drivers(driver_id),
    frozen_through TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(driver_id, frozen_through)
);

-- 结算账期
CREATE TABLE IF NOT EXISTS ledger_periods (
    period_id TEXT PRIMARY KEY,
    carrier_id TEXT NOT NULL REFERENCES carriers(carrier_id),
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    status TEXT NOT NULL,
    ruleset_id TEXT NOT NULL,
    ruleset_version INTEGER NOT NULL,
    closed_at TEXT
);

-- 扣款依据（异常记录 + 到达时间）
CREATE TABLE IF NOT EXISTS deduction_evidence (
    evidence_id TEXT PRIMARY KEY,
    carrier_id TEXT NOT NULL REFERENCES carriers(carrier_id),
    trip_id TEXT NOT NULL REFERENCES trips(trip_id),
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 结算明细行
CREATE TABLE IF NOT EXISTS settlement_lines (
    line_id TEXT PRIMARY KEY,
    period_id TEXT NOT NULL REFERENCES ledger_periods(period_id),
    trip_id TEXT NOT NULL REFERENCES trips(trip_id),
    driver_id TEXT NOT NULL REFERENCES drivers(driver_id),
    kind TEXT NOT NULL,
    category TEXT NOT NULL,
    amount INTEGER NOT NULL,
    evidence_id TEXT REFERENCES deduction_evidence(evidence_id),
    status TEXT NOT NULL,
    anchor TEXT NOT NULL,
    rule_ref TEXT NOT NULL,
    memo TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(period_id, anchor)
);

-- 申诉
CREATE TABLE IF NOT EXISTS appeals (
    appeal_id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL REFERENCES trips(trip_id),
    driver_id TEXT NOT NULL REFERENCES drivers(driver_id),
    carrier_id TEXT NOT NULL REFERENCES carriers(carrier_id),
    line_id TEXT REFERENCES settlement_lines(line_id),
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    evidence_id TEXT REFERENCES deduction_evidence(evidence_id),
    amount_held INTEGER NOT NULL DEFAULT 0,
    deadline_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolution_note TEXT
);

-- 争议金额托管台账
CREATE TABLE IF NOT EXISTS escrow_entries (
    escrow_id TEXT PRIMARY KEY,
    appeal_id TEXT NOT NULL REFERENCES appeals(appeal_id),
    line_id TEXT NOT NULL REFERENCES settlement_lines(line_id),
    amount INTEGER NOT NULL CHECK(amount >= 0),
    status TEXT NOT NULL,
    released_to_driver INTEGER NOT NULL DEFAULT 0,
    returned_carrier INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

-- 已关账账期的复算对比
CREATE TABLE IF NOT EXISTS recomputations (
    recomputation_id TEXT PRIMARY KEY,
    period_id TEXT NOT NULL REFERENCES ledger_periods(period_id),
    ruleset_id TEXT NOT NULL,
    ruleset_version INTEGER NOT NULL,
    original_total INTEGER NOT NULL,
    recomputed_total INTEGER NOT NULL,
    delta INTEGER NOT NULL,
    lines_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 派单留痕（含被拒与强制派单）
CREATE TABLE IF NOT EXISTS dispatch_decisions (
    decision_id TEXT PRIMARY KEY,
    trip_id TEXT REFERENCES trips(trip_id),
    driver_id TEXT NOT NULL,
    co_driver_id TEXT,
    checked_at TEXT NOT NULL,
    eligible INTEGER NOT NULL,
    forced INTEGER NOT NULL,
    reasons_json TEXT NOT NULL
);

-- 幂等请求回执
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 哈希串联审计事件
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
