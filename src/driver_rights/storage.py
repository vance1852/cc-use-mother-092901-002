"""在基础服务的 SQLite 边界上扩展司机履约项目的表结构。"""

from __future__ import annotations

from pathlib import Path

from transport_coordination.storage import Database

SCHEMA = """
CREATE TABLE IF NOT EXISTS dr_drivers (
    driver_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    license_no TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dr_regulations (
    regulation_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    rules_json TEXT NOT NULL,
    rules_hash TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (regulation_id, version)
);
CREATE TABLE IF NOT EXISTS dr_contracts (
    contract_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    driver_id TEXT NOT NULL REFERENCES dr_drivers(driver_id),
    organization_id TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    terms_json TEXT NOT NULL,
    terms_hash TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (contract_id, version)
);
CREATE TABLE IF NOT EXISTS dr_tasks (
    task_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    regulation_id TEXT NOT NULL,
    regulation_version INTEGER NOT NULL,
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    planned_start TEXT NOT NULL,
    planned_end TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    base_freight_cents INTEGER NOT NULL CHECK(base_freight_cents >= 0),
    status TEXT NOT NULL CHECK(status IN ('offered','assigned','in_progress','completed','cancelled')),
    settled INTEGER NOT NULL DEFAULT 0 CHECK(settled IN (0, 1)),
    completed_at TEXT,
    cancel_reason TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dr_assignments (
    assignment_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES dr_tasks(task_id),
    driver_id TEXT NOT NULL REFERENCES dr_drivers(driver_id),
    role TEXT NOT NULL CHECK(role IN ('primary','relief')),
    contract_id TEXT NOT NULL,
    contract_version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','active','ended')),
    assigned_by TEXT NOT NULL,
    assigned_at TEXT NOT NULL,
    ended_at TEXT,
    end_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_dr_assignments_driver ON dr_assignments(driver_id, status);
CREATE TABLE IF NOT EXISTS dr_events (
    event_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES dr_tasks(task_id),
    actor_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dr_segments (
    segment_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES dr_tasks(task_id),
    driver_id TEXT NOT NULL REFERENCES dr_drivers(driver_id),
    kind TEXT NOT NULL CHECK(kind IN ('driving','loading','waiting','rest')),
    started_at TEXT NOT NULL,
    ended_at TEXT,
    source TEXT NOT NULL CHECK(source IN ('live','backfill')),
    event_id TEXT NOT NULL UNIQUE,
    frozen INTEGER NOT NULL DEFAULT 0 CHECK(frozen IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dr_segments_driver ON dr_segments(driver_id, started_at);
CREATE INDEX IF NOT EXISTS idx_dr_segments_task ON dr_segments(task_id);
CREATE TABLE IF NOT EXISTS dr_violations (
    violation_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES dr_tasks(task_id),
    driver_id TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    rule TEXT NOT NULL,
    window_key TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    responsible_actor_id TEXT,
    detected_at TEXT NOT NULL,
    UNIQUE(task_id, driver_id, rule, window_key)
);
CREATE TABLE IF NOT EXISTS dr_periods (
    period_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    month TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','closed')),
    closed_at TEXT,
    closed_by TEXT,
    UNIQUE(organization_id, month)
);
CREATE TABLE IF NOT EXISTS dr_statements (
    statement_id TEXT PRIMARY KEY,
    period_id TEXT NOT NULL REFERENCES dr_periods(period_id),
    driver_id TEXT NOT NULL REFERENCES dr_drivers(driver_id),
    organization_id TEXT NOT NULL,
    gross_cents INTEGER NOT NULL DEFAULT 0,
    deduction_cents INTEGER NOT NULL DEFAULT 0,
    escrow_cents INTEGER NOT NULL DEFAULT 0,
    returned_cents INTEGER NOT NULL DEFAULT 0,
    payable_cents INTEGER NOT NULL DEFAULT 0,
    paid_cents INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK(status IN ('open','closed','paid')),
    created_at TEXT NOT NULL,
    closed_at TEXT,
    paid_at TEXT,
    UNIQUE(period_id, driver_id)
);
CREATE TABLE IF NOT EXISTS dr_statement_lines (
    line_id TEXT PRIMARY KEY,
    statement_id TEXT NOT NULL REFERENCES dr_statements(statement_id),
    task_id TEXT NOT NULL REFERENCES dr_tasks(task_id),
    component TEXT NOT NULL,
    ref_id TEXT NOT NULL DEFAULT '',
    quantity_minutes INTEGER NOT NULL DEFAULT 0,
    amount_cents INTEGER NOT NULL,
    rule_version TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(statement_id, task_id, component, ref_id)
);
CREATE TABLE IF NOT EXISTS dr_evidence (
    evidence_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES dr_tasks(task_id),
    kind TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    submitted_by TEXT NOT NULL,
    submitted_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dr_deductions (
    deduction_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES dr_tasks(task_id),
    driver_id TEXT NOT NULL REFERENCES dr_drivers(driver_id),
    organization_id TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
    reason TEXT NOT NULL,
    evidence_id TEXT NOT NULL REFERENCES dr_evidence(evidence_id),
    status TEXT NOT NULL CHECK(status IN ('proposed','applied','escrowed',
                                         'released_to_carrier','released_to_driver','cancelled')),
    statement_id TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dr_escrow_entries (
    entry_id TEXT PRIMARY KEY,
    deduction_id TEXT NOT NULL REFERENCES dr_deductions(deduction_id),
    appeal_id TEXT,
    direction TEXT NOT NULL CHECK(direction IN ('hold','release_to_carrier','release_to_driver')),
    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dr_appeals (
    appeal_id TEXT PRIMARY KEY,
    deduction_id TEXT NOT NULL UNIQUE REFERENCES dr_deductions(deduction_id),
    driver_id TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('filed','upheld','rejected')),
    deadline TEXT,
    filed_at TEXT NOT NULL,
    resolved_by TEXT,
    resolved_at TEXT,
    resolution_note TEXT
);
CREATE TABLE IF NOT EXISTS dr_recompute_reports (
    report_id TEXT PRIMARY KEY,
    period_id TEXT NOT NULL REFERENCES dr_periods(period_id),
    organization_id TEXT NOT NULL,
    contract_version INTEGER,
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class DriverRightsDatabase(Database):
    """在基础库之上追加司机履约项目表结构的数据库。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        super().__init__(path)
        self.connection.executescript(SCHEMA)
