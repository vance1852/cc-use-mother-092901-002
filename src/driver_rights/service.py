"""司机履约与权益保障项目的领域服务。

在基础服务的身份、幂等、事务与审计边界上实现：
- 按可控时钟记录驾驶、装卸、等待、休息与跨夜轮班区段；
- 合同版本、最低结算规则、补贴、扣款证据与申诉时限关联到每趟任务；
- 派单前原子校验剩余工时与后续休息窗口；
- 双驾换班、任务取消、途中救援与补录事件只影响尚未冻结的区段；
- 同一事件重放不得重复计费，争议金额单独托管而不阻断无争议收入；
- 规则调整后可以复算已经关账的账期。
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from transport_coordination.audit import append_event, canonical_json, digest
from transport_coordination.clock import Clock
from transport_coordination.errors import (ConflictError, NotFoundError,
                                           PermissionDenied, ValidationError)
from transport_coordination.models import Actor, WriteReceipt
from transport_coordination.service import DomainService

from .domain import (DEFAULT_REGULATION, DUTY_KINDS, EVENT_TYPES, SEGMENT_KINDS,
                     normalize_rules, normalize_terms)
from .hours import (continuous_driving_minutes, daily_usage_minutes,
                    each_local_day, format_instant, minutes_between,
                    overlap_minutes, parse_instant)
from .models import Segment
from .settlement import compute_lines, lines_total
from .storage import DriverRightsDatabase

MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
BACKFLOW_TOLERANCE_SECONDS = 60


class DriverRightsService(DomainService):
    """协调司机履约、工时、结算、扣款、申诉与复算规则。"""

    def __init__(self, database: DriverRightsDatabase, clock: Clock | None = None) -> None:
        super().__init__(database, clock)

    # ------------------------------------------------------------------
    # 基础读取与校验
    # ------------------------------------------------------------------

    def _parse(self, value: str, field: str):
        return parse_instant(value, field)

    def _month(self, value: str) -> str:
        value = str(value).strip()
        if not MONTH.fullmatch(value):
            raise ValidationError("month 必须是 YYYY-MM 格式")
        return value

    def _cents(self, value: Any, field: str, allow_zero: bool = False) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(f"{field} 必须是整数（单位：分）")
        if value < 0 or (value == 0 and not allow_zero):
            raise ValidationError(f"{field} 必须{'为非负' if allow_zero else '为正'}整数")
        return value

    def _driver_row(self, connection, driver_id: str):
        row = connection.execute(
            "SELECT * FROM dr_drivers WHERE driver_id=?", (driver_id,)).fetchone()
        if row is None:
            raise NotFoundError("司机不存在")
        return row

    def _task_row(self, connection, task_id: str):
        row = connection.execute(
            "SELECT * FROM dr_tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise NotFoundError("任务不存在")
        return row

    def _require_carrier_org(self, actor: Actor, organization_id: str) -> None:
        if actor.role == "admin":
            return
        if actor.role != "carrier" or actor.organization_id != organization_id:
            raise PermissionDenied("只能操作本运输企业的数据")

    def _latest_version(self, connection, table: str, key_column: str, key: str) -> int | None:
        row = connection.execute(
            f"SELECT MAX(version) AS version FROM {table} WHERE {key_column}=?", (key,),
        ).fetchone()
        return row["version"] if row and row["version"] is not None else None

    def _terms_for(self, connection, contract_id: str, version: int) -> dict[str, Any]:
        row = connection.execute(
            "SELECT terms_json FROM dr_contracts WHERE contract_id=? AND version=?",
            (contract_id, version)).fetchone()
        if row is None:
            raise NotFoundError("合同版本不存在")
        return json.loads(row["terms_json"])

    def _rules_for(self, connection, regulation_id: str, version: int) -> dict[str, Any]:
        row = connection.execute(
            "SELECT rules_json FROM dr_regulations WHERE regulation_id=? AND version=?",
            (regulation_id, version)).fetchone()
        if row is None:
            raise NotFoundError("工时监管规则版本不存在")
        return json.loads(row["rules_json"])

    def _segments_of(self, connection, task_id: str | None = None,
                     driver_id: str | None = None) -> list[Segment]:
        query = "SELECT * FROM dr_segments WHERE 1=1"
        parameters: list[Any] = []
        if task_id is not None:
            query += " AND task_id=?"
            parameters.append(task_id)
        if driver_id is not None:
            query += " AND driver_id=?"
            parameters.append(driver_id)
        query += " ORDER BY started_at, segment_id"
        segments = []
        for row in connection.execute(query, parameters):
            segments.append(Segment(
                segment_id=row["segment_id"], task_id=row["task_id"],
                driver_id=row["driver_id"], kind=row["kind"],
                started_at=parse_instant(row["started_at"], "started_at"),
                ended_at=parse_instant(row["ended_at"], "ended_at") if row["ended_at"] else None,
                source=row["source"], event_id=row["event_id"],
                frozen=bool(row["frozen"])))
        return segments

    def _open_segment(self, connection, driver_id: str):
        return connection.execute(
            "SELECT * FROM dr_segments WHERE driver_id=? AND ended_at IS NULL"
            " ORDER BY started_at DESC LIMIT 1", (driver_id,)).fetchone()

    def _active_assignment(self, connection, task_id: str, driver_id: str):
        return connection.execute(
            "SELECT * FROM dr_assignments WHERE task_id=? AND driver_id=? AND status='active'"
            " ORDER BY assigned_at DESC LIMIT 1", (task_id, driver_id)).fetchone()

    def _latest_assignment(self, connection, task_id: str, driver_id: str):
        return connection.execute(
            "SELECT * FROM dr_assignments WHERE task_id=? AND driver_id=?"
            " ORDER BY assigned_at DESC LIMIT 1", (task_id, driver_id)).fetchone()

    def _task_month(self, task) -> str:
        finished = task["completed_at"]
        if not finished:
            raise ValidationError("任务尚未结束，无法确定账期")
        tz = ZoneInfo(task["timezone_name"])
        local = parse_instant(finished, "completed_at").astimezone(tz)
        return f"{local.year:04d}-{local.month:02d}"

    # ------------------------------------------------------------------
    # 建档：司机档案、工时监管规则、合同版本
    # ------------------------------------------------------------------

    def register_driver(self, *, request_id: str, actor_id: str, driver_id: str,
                        license_no: str, name: str,
                        organization_id: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "driver_id": driver_id, "license_no": license_no,
                   "name": name, "organization_id": organization_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "carrier")
            driver_id = self._identifier(driver_id, "driver_id")
            license_no = self._text(license_no, "license_no", 60)
            name = self._text(name, "name")
            identity = connection.execute(
                "SELECT * FROM actors WHERE actor_id=?", (driver_id,)).fetchone()
            if identity is None or identity["role"] != "driver":
                raise ValidationError("driver_id 必须是角色为 driver 的操作者")
            org = organization_id or actor.organization_id
            if actor.role == "carrier" and org != actor.organization_id:
                raise PermissionDenied("只能为本企业登记司机")
            if identity["organization_id"] != org:
                raise ValidationError("司机操作者不属于目标企业")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO dr_drivers(driver_id,organization_id,license_no,name,active,created_at)"
                        " VALUES(?,?,?,?,1,?)",
                        (driver_id, org, license_no, name, self._now()))
                except Exception as exc:
                    raise ConflictError("司机档案或驾驶证号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="driver.registered",
                             resource_type="driver", resource_id=driver_id,
                             detail={"organization_id": org, "license_no": license_no},
                             occurred_at=self._now())
                return "driver", driver_id, {"driver_id": driver_id, "organization_id": org}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_driver", payload=payload, create=create)

    def publish_regulation(self, *, request_id: str, actor_id: str, regulation_id: str,
                           rules: dict[str, Any]) -> WriteReceipt:
        payload = {"actor_id": actor_id, "regulation_id": regulation_id, "rules": rules}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "regulator")
            regulation_id = self._identifier(regulation_id, "regulation_id")
            normalized = normalize_rules(rules)

            def create() -> tuple[str, str, dict[str, Any]]:
                version = (self._latest_version(connection, "dr_regulations",
                                                "regulation_id", regulation_id) or 0) + 1
                connection.execute(
                    "INSERT INTO dr_regulations(regulation_id,version,rules_json,rules_hash,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (regulation_id, version, canonical_json(normalized),
                     digest(normalized), actor_id, self._now()))
                append_event(connection, actor_id=actor_id, action="regulation.published",
                             resource_type="regulation", resource_id=regulation_id,
                             detail={"version": version, "rules": normalized},
                             occurred_at=self._now())
                return "regulation", regulation_id, {"regulation_id": regulation_id,
                                                     "version": version}

            return self._idempotent(connection, request_id=request_id,
                                    action="publish_regulation", payload=payload, create=create)

    def publish_contract(self, *, request_id: str, actor_id: str, driver_id: str,
                         effective_from: str, terms: dict[str, Any]) -> WriteReceipt:
        payload = {"actor_id": actor_id, "driver_id": driver_id,
                   "effective_from": effective_from, "terms": terms}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "carrier")
            driver_id = self._identifier(driver_id, "driver_id")
            driver = self._driver_row(connection, driver_id)
            self._require_carrier_org(actor, driver["organization_id"])
            effective = self._parse(effective_from, "effective_from")
            normalized = normalize_terms(terms)

            def create() -> tuple[str, str, dict[str, Any]]:
                version = (self._latest_version(connection, "dr_contracts",
                                                "contract_id", driver_id) or 0) + 1
                connection.execute(
                    "INSERT INTO dr_contracts(contract_id,version,driver_id,organization_id,"
                    "effective_from,terms_json,terms_hash,created_by,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (driver_id, version, driver_id, driver["organization_id"],
                     format_instant(effective), canonical_json(normalized),
                     digest(normalized), actor_id, self._now()))
                append_event(connection, actor_id=actor_id, action="contract.published",
                             resource_type="contract", resource_id=driver_id,
                             detail={"version": version, "effective_from": format_instant(effective),
                                     "terms": normalized}, occurred_at=self._now())
                return "contract", driver_id, {"contract_id": driver_id, "version": version}

            return self._idempotent(connection, request_id=request_id,
                                    action="publish_contract", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 派单：任务、原子工时校验、司机确认
    # ------------------------------------------------------------------

    def create_task(self, *, request_id: str, actor_id: str, task_id: str,
                    origin: str, destination: str, planned_start: str, planned_end: str,
                    base_freight_cents: int, timezone_name: str = "Asia/Shanghai",
                    regulation_id: str = "hos-default",
                    organization_id: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "task_id": task_id, "origin": origin,
                   "destination": destination, "planned_start": planned_start,
                   "planned_end": planned_end, "base_freight_cents": base_freight_cents,
                   "timezone_name": timezone_name, "regulation_id": regulation_id,
                   "organization_id": organization_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "carrier")
            task_id = self._identifier(task_id, "task_id")
            origin = self._text(origin, "origin")
            destination = self._text(destination, "destination")
            timezone_name = self._text(timezone_name, "timezone_name", 80)
            try:
                ZoneInfo(timezone_name)
            except Exception as exc:
                raise ValidationError("timezone_name 不是有效的时区") from exc
            start = self._parse(planned_start, "planned_start")
            end = self._parse(planned_end, "planned_end")
            if end <= start:
                raise ValidationError("planned_end 必须晚于 planned_start")
            freight = self._cents(base_freight_cents, "base_freight_cents", allow_zero=True)
            regulation_id = self._identifier(regulation_id, "regulation_id")
            org = organization_id or actor.organization_id
            self._require_carrier_org(actor, org)
            regulation_version = self._latest_version(connection, "dr_regulations",
                                                      "regulation_id", regulation_id)
            if regulation_version is None:
                raise NotFoundError("工时监管规则不存在，请先由监管人员发布")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO dr_tasks(task_id,organization_id,regulation_id,"
                        "regulation_version,origin,destination,planned_start,planned_end,"
                        "timezone_name,base_freight_cents,status,settled,created_by,created_at)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,'offered',0,?,?)",
                        (task_id, org, regulation_id, regulation_version, origin, destination,
                         format_instant(start), format_instant(end), timezone_name,
                         freight, actor_id, self._now()))
                except Exception as exc:
                    raise ConflictError("任务编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="task.created",
                             resource_type="task", resource_id=task_id,
                             detail={"organization_id": org, "regulation_id": regulation_id,
                                     "regulation_version": regulation_version,
                                     "planned_start": format_instant(start),
                                     "planned_end": format_instant(end)},
                             occurred_at=self._now())
                return "task", task_id, {"task_id": task_id,
                                         "regulation_version": regulation_version}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_task", payload=payload, create=create)

    def _assert_dispatchable(self, connection, *, driver_id: str, task,
                             window_start, window_end) -> None:
        """在同一事务内原子校验剩余工时与后续休息窗口。"""

        rules = self._rules_for(connection, task["regulation_id"], task["regulation_version"])
        tz = ZoneInfo(task["timezone_name"])
        segments = self._segments_of(connection, driver_id=driver_id)
        for day_start, day_end in each_local_day(window_start, window_end, tz):
            usage = daily_usage_minutes(segments, day_start, day_end)
            planned = overlap_minutes(window_start, window_end, day_start, day_end)
            if usage["driving"] + planned > rules["max_daily_driving_minutes"]:
                raise ValidationError("剩余驾驶工时不足，禁止派单")
            if usage["duty"] + planned > rules["max_daily_duty_minutes"]:
                raise ValidationError("剩余执勤工时不足，禁止派单")
        rest = rules["min_rest_between_shifts_minutes"]
        rows = connection.execute(
            "SELECT t.planned_start, t.planned_end, t.task_id FROM dr_assignments a"
            " JOIN dr_tasks t ON t.task_id=a.task_id"
            " WHERE a.driver_id=? AND a.status IN ('pending','active') AND a.task_id!=?",
            (driver_id, task["task_id"])).fetchall()
        for row in rows:
            other_start = parse_instant(row["planned_start"], "planned_start")
            other_end = parse_instant(row["planned_end"], "planned_end")
            if other_start < window_end and window_start < other_end:
                raise ConflictError("与司机已有任务时间冲突")
            rest_delta = timedelta(minutes=rest)
            if other_start < window_end + rest_delta and window_start < other_end + rest_delta:
                raise ValidationError("派单后无法满足班次间的最低休息窗口")

    def assign_task(self, *, request_id: str, actor_id: str, task_id: str,
                    driver_id: str, role: str = "primary") -> WriteReceipt:
        payload = {"actor_id": actor_id, "task_id": task_id,
                   "driver_id": driver_id, "role": role}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "carrier")
            task_id = self._identifier(task_id, "task_id")
            driver_id = self._identifier(driver_id, "driver_id")
            if role not in ("primary", "relief"):
                raise ValidationError("role 必须是 primary 或 relief")

            def create() -> tuple[str, str, dict[str, Any]]:
                task = self._task_row(connection, task_id)
                self._require_carrier_org(actor, task["organization_id"])
                if task["status"] not in ("offered", "assigned"):
                    raise ConflictError("只有待派或已派任务可以继续派单")
                driver = self._driver_row(connection, driver_id)
                if not driver["active"]:
                    raise ValidationError("司机已停用")
                if driver["organization_id"] != task["organization_id"]:
                    raise PermissionDenied("司机不属于承运该任务的企业")
                existing = connection.execute(
                    "SELECT 1 FROM dr_assignments WHERE task_id=? AND driver_id=?"
                    " AND status IN ('pending','active')", (task_id, driver_id)).fetchone()
                if existing:
                    raise ConflictError("司机已被派到该任务")
                start = parse_instant(task["planned_start"], "planned_start")
                end = parse_instant(task["planned_end"], "planned_end")
                self._assert_dispatchable(connection, driver_id=driver_id, task=task,
                                          window_start=start, window_end=end)
                contract_version = self._latest_version(connection, "dr_contracts",
                                                        "contract_id", driver_id)
                if contract_version is None:
                    raise NotFoundError("司机没有已发布的合同版本")
                assignment_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO dr_assignments(assignment_id,task_id,driver_id,role,"
                    "contract_id,contract_version,status,assigned_by,assigned_at)"
                    " VALUES(?,?,?,?,?,?,'pending',?,?)",
                    (assignment_id, task_id, driver_id, role, driver_id,
                     contract_version, actor_id, self._now()))
                connection.execute("UPDATE dr_tasks SET status='assigned' WHERE task_id=?",
                                   (task_id,))
                append_event(connection, actor_id=actor_id, action="task.assigned",
                             resource_type="task", resource_id=task_id,
                             detail={"driver_id": driver_id, "assignment_id": assignment_id,
                                     "contract_version": contract_version, "role": role},
                             occurred_at=self._now())
                return "assignment", assignment_id, {
                    "assignment_id": assignment_id, "task_id": task_id,
                    "driver_id": driver_id, "contract_version": contract_version}

            return self._idempotent(connection, request_id=request_id,
                                    action="assign_task", payload=payload, create=create)

    def accept_task(self, *, request_id: str, actor_id: str, task_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "task_id": task_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "driver")
            task_id = self._identifier(task_id, "task_id")

            def create() -> tuple[str, str, dict[str, Any]]:
                task = self._task_row(connection, task_id)
                if task["status"] not in ("offered", "assigned"):
                    raise ConflictError("任务当前状态不能接单")
                row = connection.execute(
                    "SELECT * FROM dr_assignments WHERE task_id=? AND driver_id=?"
                    " AND status='pending' ORDER BY assigned_at DESC LIMIT 1",
                    (task_id, actor.actor_id)).fetchone()
                if row is None:
                    raise NotFoundError("没有待确认的派单")
                connection.execute(
                    "UPDATE dr_assignments SET status='active' WHERE assignment_id=?",
                    (row["assignment_id"],))
                append_event(connection, actor_id=actor_id, action="task.accepted",
                             resource_type="task", resource_id=task_id,
                             detail={"driver_id": actor.actor_id,
                                     "assignment_id": row["assignment_id"]},
                             occurred_at=self._now())
                return "assignment", row["assignment_id"], {
                    "assignment_id": row["assignment_id"], "task_id": task_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="accept_task", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 履约事件：区段、双驾换班、途中救援、任务取消与完成
    # ------------------------------------------------------------------

    def _frozen_guard(self, connection, task_id: str, start, end) -> None:
        """补录与变更只允许影响尚未冻结的区段。"""

        row = connection.execute(
            "SELECT 1 FROM dr_segments WHERE task_id=? AND frozen=1 AND ended_at IS NOT NULL"
            " AND started_at<? AND ended_at>? LIMIT 1",
            (task_id, format_instant(end), format_instant(start))).fetchone()
        if row:
            raise ConflictError("相关区段已冻结结算，补录或变更须通过申诉与复算流程")

    def _overlap_guard(self, connection, driver_id: str, start, end, now_text: str) -> None:
        row = connection.execute(
            "SELECT 1 FROM dr_segments WHERE driver_id=? AND started_at<?"
            " AND COALESCE(ended_at, ?)>? LIMIT 1",
            (driver_id, format_instant(end), now_text, format_instant(start))).fetchone()
        if row:
            raise ConflictError("与司机既有履约区段时间重叠")

    def _insert_segment(self, connection, *, task_id: str, driver_id: str, kind: str,
                        started_at, ended_at, source: str, event_id: str) -> str:
        segment_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO dr_segments(segment_id,task_id,driver_id,kind,started_at,ended_at,"
            "source,event_id,frozen,created_at) VALUES(?,?,?,?,?,?,?,?,0,?)",
            (segment_id, task_id, driver_id, kind, format_instant(started_at),
             format_instant(ended_at) if ended_at else None, source, event_id, self._now()))
        return segment_id

    def _end_open_segment(self, connection, driver_id: str, ended_at) -> str | None:
        row = self._open_segment(connection, driver_id)
        if row is None:
            return None
        if ended_at < parse_instant(row["started_at"], "started_at"):
            raise ValidationError("结束时间早于进行中区段的开始时间")
        connection.execute("UPDATE dr_segments SET ended_at=? WHERE segment_id=?",
                           (format_instant(ended_at), row["segment_id"]))
        return row["segment_id"]

    def _detect_violations(self, connection, *, task, driver_id: str,
                           just_ended_day) -> list[dict[str, Any]]:
        """按任务快照的工时规则检测超时责任。"""

        rules = self._rules_for(connection, task["regulation_id"], task["regulation_version"])
        tz = ZoneInfo(task["timezone_name"])
        segments = self._segments_of(connection, driver_id=driver_id)
        assignment = self._latest_assignment(connection, task["task_id"], driver_id)
        responsible = assignment["assigned_by"] if assignment else None
        findings: list[tuple[str, str, dict[str, Any]]] = []
        chain = continuous_driving_minutes(
            segments, rules["min_break_after_continuous_driving_minutes"])
        if chain > rules["max_continuous_driving_minutes"]:
            driving = [s for s in segments if s.kind == "driving" and s.ended_at]
            window_key = f"chain:{driving[-1].segment_id}" if driving else "chain:unknown"
            findings.append(("max_continuous_driving", window_key, {
                "measured_minutes": chain,
                "limit_minutes": rules["max_continuous_driving_minutes"]}))
        day_start, day_end = None, None
        for start, end in each_local_day(just_ended_day, just_ended_day + timedelta(minutes=1), tz):
            day_start, day_end = start, end
        if day_start is not None:
            usage = daily_usage_minutes(segments, day_start, day_end)
            day_key = f"day:{day_start.astimezone(tz).date().isoformat()}"
            if usage["driving"] > rules["max_daily_driving_minutes"]:
                findings.append(("max_daily_driving", day_key, {
                    "measured_minutes": usage["driving"],
                    "limit_minutes": rules["max_daily_driving_minutes"]}))
            if usage["duty"] > rules["max_daily_duty_minutes"]:
                findings.append(("max_daily_duty", day_key, {
                    "measured_minutes": usage["duty"],
                    "limit_minutes": rules["max_daily_duty_minutes"]}))
        created = []
        for rule, window_key, detail in findings:
            violation_id = uuid.uuid4().hex
            cursor = connection.execute(
                "INSERT OR IGNORE INTO dr_violations(violation_id,task_id,driver_id,"
                "organization_id,rule,window_key,detail_json,responsible_actor_id,detected_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (violation_id, task["task_id"], driver_id, task["organization_id"],
                 rule, window_key, canonical_json(detail), responsible, self._now()))
            if cursor.rowcount:
                append_event(connection, actor_id="system", action="violation.detected",
                             resource_type="violation", resource_id=violation_id,
                             detail={"task_id": task["task_id"], "driver_id": driver_id,
                                     "rule": rule, **detail},
                             occurred_at=self._now())
                created.append({"violation_id": violation_id, "rule": rule, **detail})
        return created

    def record_event(self, *, request_id: str, actor_id: str, task_id: str,
                     event_type: str, occurred_at: str | None = None,
                     driver_id: str | None = None, kind: str | None = None,
                     ended_at: str | None = None, to_driver_id: str | None = None,
                     reason: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "task_id": task_id, "event_type": event_type,
                   "occurred_at": occurred_at, "driver_id": driver_id, "kind": kind,
                   "ended_at": ended_at, "to_driver_id": to_driver_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "carrier", "driver")
            task_id = self._identifier(task_id, "task_id")
            if event_type not in EVENT_TYPES:
                raise ValidationError("event_type 不在允许范围内")
            now = self.clock.now()
            occurred = self._parse(occurred_at, "occurred_at") if occurred_at else now
            if occurred > now + timedelta(seconds=BACKFLOW_TOLERANCE_SECONDS):
                raise ValidationError("occurred_at 不能晚于当前时间")
            source = "backfill" if occurred < now - timedelta(
                seconds=BACKFLOW_TOLERANCE_SECONDS) else "live"

            def create() -> tuple[str, str, dict[str, Any]]:
                task = self._task_row(connection, task_id)
                if actor.role == "carrier":
                    self._require_carrier_org(actor, task["organization_id"])
                status = task["status"]
                if status == "offered":
                    raise ConflictError("任务尚未派单，不能记录履约事件")
                if status in ("completed", "cancelled"):
                    # 已结束但未结算的任务允许补录（区段尚未冻结）；
                    # 已结算的任务区段全部冻结，只能走申诉与复算流程。
                    if task["settled"]:
                        raise ConflictError("相关区段已冻结结算，补录或变更须通过申诉与复算流程")
                    if event_type in ("handover", "rescue"):
                        raise ConflictError("任务已结束，不能换班或救援")
                    finished = parse_instant(task["completed_at"], "completed_at")
                    if occurred > finished:
                        raise ValidationError("补录时间不能晚于任务结束时间")
                if event_type in ("segment_start", "segment_end"):
                    result = self._apply_segment_event(
                        connection, actor=actor, task=task, event_id=request_id,
                        occurred=occurred, source=source, event_type=event_type,
                        driver_id=driver_id, kind=kind, ended_at=ended_at, now=now)
                else:
                    result = self._apply_handover(
                        connection, actor=actor, task=task, event_id=request_id,
                        occurred=occurred, source=source, event_type=event_type,
                        from_driver_id=driver_id, to_driver_id=to_driver_id,
                        reason=reason, now=now)
                connection.execute(
                    "INSERT INTO dr_events(event_id,task_id,actor_id,event_type,payload_json,"
                    "occurred_at,recorded_at) VALUES(?,?,?,?,?,?,?)",
                    (request_id, task_id, actor.actor_id, event_type, canonical_json(payload),
                     format_instant(occurred), self._now()))
                append_event(connection, actor_id=actor_id, action="event.recorded",
                             resource_type="event", resource_id=request_id,
                             detail={"task_id": task_id, "event_type": event_type,
                                     "source": source, **result},
                             occurred_at=self._now())
                return "event", request_id, {"event_id": request_id, **result}

            return self._idempotent(connection, request_id=request_id,
                                    action="record_event", payload=payload, create=create)

    def _apply_segment_event(self, connection, *, actor: Actor, task, event_id: str,
                             occurred, source: str, event_type: str,
                             driver_id: str | None, kind: str | None,
                             ended_at: str | None, now) -> dict[str, Any]:
        target = driver_id or (actor.actor_id if actor.role == "driver" else None)
        if not target:
            raise ValidationError("driver_id 不能为空")
        target = self._identifier(target, "driver_id")
        if actor.role == "driver" and target != actor.actor_id:
            raise PermissionDenied("司机只能记录本人的履约事件")
        driver = self._driver_row(connection, target)
        if driver["organization_id"] != task["organization_id"]:
            raise PermissionDenied("司机不属于承运该任务的企业")
        if self._active_assignment(connection, task["task_id"], target) is None:
            raise ConflictError("司机在该任务上没有已确认的派单")
        result: dict[str, Any] = {"driver_id": target}
        if event_type == "segment_start":
            if kind not in SEGMENT_KINDS:
                raise ValidationError("kind 必须是 driving/loading/waiting/rest")
            closed_at = self._parse(ended_at, "ended_at") if ended_at else None
            if closed_at is not None:
                if closed_at <= occurred:
                    raise ValidationError("ended_at 必须晚于 occurred_at")
                if closed_at > now:
                    raise ValidationError("ended_at 不能晚于当前时间")
                self._frozen_guard(connection, task["task_id"], occurred, closed_at)
                self._overlap_guard(connection, target, occurred, closed_at, self._now())
                segment_id = self._insert_segment(
                    connection, task_id=task["task_id"], driver_id=target, kind=kind,
                    started_at=occurred, ended_at=closed_at, source="backfill",
                    event_id=event_id)
                result.update({"segment_id": segment_id, "closed_segment": True})
            else:
                if source == "backfill":
                    raise ValidationError("补录区段必须同时提供 ended_at")
                self._frozen_guard(connection, task["task_id"], occurred, now)
                open_row = self._open_segment(connection, target)
                if open_row is not None:
                    if occurred <= parse_instant(open_row["started_at"], "started_at"):
                        raise ValidationError("新区段开始时间必须晚于进行中区段的开始时间")
                    connection.execute("UPDATE dr_segments SET ended_at=? WHERE segment_id=?",
                                       (format_instant(occurred), open_row["segment_id"]))
                    result["auto_closed_segment_id"] = open_row["segment_id"]
                self._overlap_guard(connection, target, occurred, now, self._now())
                segment_id = self._insert_segment(
                    connection, task_id=task["task_id"], driver_id=target, kind=kind,
                    started_at=occurred, ended_at=None, source=source, event_id=event_id)
                result.update({"segment_id": segment_id, "closed_segment": False})
            connection.execute(
                "UPDATE dr_tasks SET status='in_progress' WHERE task_id=? AND status='assigned'",
                (task["task_id"],))
        else:
            open_row = self._open_segment(connection, target)
            if open_row is None or open_row["task_id"] != task["task_id"]:
                raise NotFoundError("该司机在此任务上没有进行中的区段")
            if occurred < parse_instant(open_row["started_at"], "started_at"):
                raise ValidationError("结束时间早于区段开始时间")
            self._frozen_guard(connection, task["task_id"],
                               parse_instant(open_row["started_at"], "started_at"), occurred)
            connection.execute("UPDATE dr_segments SET ended_at=? WHERE segment_id=?",
                               (format_instant(occurred), open_row["segment_id"]))
            result.update({"segment_id": open_row["segment_id"], "ended": True})
        ended_kind = kind if event_type == "segment_start" else open_row["kind"]
        if ended_kind in DUTY_KINDS or event_type == "segment_end":
            violations = self._detect_violations(
                connection, task=task, driver_id=target, just_ended_day=occurred)
            if violations:
                result["violations"] = violations
        return result

    def _apply_handover(self, connection, *, actor: Actor, task, event_id: str,
                        occurred, source: str, event_type: str,
                        from_driver_id: str | None, to_driver_id: str | None,
                        reason: str | None, now) -> dict[str, Any]:
        if not to_driver_id:
            raise ValidationError("to_driver_id 不能为空")
        from_driver = from_driver_id or (actor.actor_id if actor.role == "driver" else None)
        if not from_driver:
            raise ValidationError("driver_id（交班司机）不能为空")
        from_driver = self._identifier(from_driver, "driver_id")
        to_driver = self._identifier(to_driver_id, "to_driver_id")
        if from_driver == to_driver:
            raise ValidationError("接班司机不能与交班司机相同")
        if actor.role == "driver" and actor.actor_id != from_driver:
            raise PermissionDenied("司机只能为本人发起换班")
        assignment = self._active_assignment(connection, task["task_id"], from_driver)
        if assignment is None:
            raise ConflictError("交班司机在该任务上没有已确认的派单")
        relief = self._driver_row(connection, to_driver)
        if not relief["active"]:
            raise ValidationError("接班司机已停用")
        if relief["organization_id"] != task["organization_id"]:
            raise PermissionDenied("接班司机不属于承运该任务的企业")
        self._frozen_guard(connection, task["task_id"], occurred, now)
        planned_end = parse_instant(task["planned_end"], "planned_end")
        self._assert_dispatchable(connection, driver_id=to_driver, task=task,
                                  window_start=occurred, window_end=max(planned_end, occurred))
        open_row = self._open_segment(connection, from_driver)
        ended_segment = None
        if open_row is not None and open_row["task_id"] == task["task_id"]:
            if occurred < parse_instant(open_row["started_at"], "started_at"):
                raise ValidationError("换班时间早于进行中区段的开始时间")
            connection.execute("UPDATE dr_segments SET ended_at=? WHERE segment_id=?",
                               (format_instant(occurred), open_row["segment_id"]))
            ended_segment = open_row["segment_id"]
        connection.execute(
            "UPDATE dr_assignments SET status='ended', ended_at=?, end_reason=?"
            " WHERE assignment_id=?",
            (self._now(), event_type, assignment["assignment_id"]))
        contract_version = self._latest_version(connection, "dr_contracts",
                                                "contract_id", to_driver)
        if contract_version is None:
            raise NotFoundError("接班司机没有已发布的合同版本")
        new_assignment = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO dr_assignments(assignment_id,task_id,driver_id,role,contract_id,"
            "contract_version,status,assigned_by,assigned_at)"
            " VALUES(?,?,?,'relief',?,?,'active',?,?)",
            (new_assignment, task["task_id"], to_driver, to_driver,
             contract_version, actor.actor_id, self._now()))
        segment_id = self._insert_segment(
            connection, task_id=task["task_id"], driver_id=to_driver, kind="driving",
            started_at=occurred, ended_at=None, source=source, event_id=event_id)
        result: dict[str, Any] = {
            "from_driver_id": from_driver, "to_driver_id": to_driver,
            "ended_segment_id": ended_segment, "segment_id": segment_id,
            "assignment_id": new_assignment, "event_type": event_type}
        if event_type == "rescue":
            result["rescue_reason"] = reason or ""
        violations = self._detect_violations(
            connection, task=task, driver_id=from_driver, just_ended_day=occurred)
        if violations:
            result["violations"] = violations
        return result

    def cancel_task(self, *, request_id: str, actor_id: str, task_id: str,
                    reason: str, occurred_at: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "task_id": task_id,
                   "reason": reason, "occurred_at": occurred_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "carrier")
            task_id = self._identifier(task_id, "task_id")
            reason = self._text(reason, "reason")
            now = self.clock.now()
            occurred = self._parse(occurred_at, "occurred_at") if occurred_at else now
            if occurred > now:
                raise ValidationError("occurred_at 不能晚于当前时间")

            def create() -> tuple[str, str, dict[str, Any]]:
                task = self._task_row(connection, task_id)
                self._require_carrier_org(actor, task["organization_id"])
                if task["status"] in ("completed", "cancelled"):
                    raise ConflictError("任务已经结束")
                self._frozen_guard(connection, task_id, occurred, now)
                ended = self._finish_task(connection, task=task, finished_at=occurred,
                                          end_reason="cancel")
                connection.execute(
                    "UPDATE dr_tasks SET status='cancelled', cancel_reason=?, completed_at=?"
                    " WHERE task_id=?", (reason, format_instant(occurred), task_id))
                append_event(connection, actor_id=actor_id, action="task.cancelled",
                             resource_type="task", resource_id=task_id,
                             detail={"reason": reason, "ended_segments": ended},
                             occurred_at=self._now())
                return "task", task_id, {"task_id": task_id, "status": "cancelled",
                                         "ended_segments": ended}

            return self._idempotent(connection, request_id=request_id,
                                    action="cancel_task", payload=payload, create=create)

    def complete_task(self, *, request_id: str, actor_id: str, task_id: str,
                      occurred_at: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "task_id": task_id, "occurred_at": occurred_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "carrier", "driver")
            task_id = self._identifier(task_id, "task_id")
            now = self.clock.now()
            occurred = self._parse(occurred_at, "occurred_at") if occurred_at else now
            if occurred > now:
                raise ValidationError("occurred_at 不能晚于当前时间")

            def create() -> tuple[str, str, dict[str, Any]]:
                task = self._task_row(connection, task_id)
                if actor.role == "carrier":
                    self._require_carrier_org(actor, task["organization_id"])
                elif self._active_assignment(connection, task_id, actor.actor_id) is None:
                    raise PermissionDenied("司机只能完成本人承运的任务")
                if task["status"] not in ("assigned", "in_progress"):
                    raise ConflictError("任务当前状态不能完成")
                self._frozen_guard(connection, task_id, occurred, now)
                ended = self._finish_task(connection, task=task, finished_at=occurred,
                                          end_reason="complete")
                connection.execute(
                    "UPDATE dr_tasks SET status='completed', completed_at=? WHERE task_id=?",
                    (format_instant(occurred), task_id))
                append_event(connection, actor_id=actor_id, action="task.completed",
                             resource_type="task", resource_id=task_id,
                             detail={"ended_segments": ended}, occurred_at=self._now())
                return "task", task_id, {"task_id": task_id, "status": "completed",
                                         "ended_segments": ended}

            return self._idempotent(connection, request_id=request_id,
                                    action="complete_task", payload=payload, create=create)

    def _finish_task(self, connection, *, task, finished_at, end_reason: str) -> list[str]:
        """结束任务全部未冻结的开放区段与派单；已冻结区段保持原样。"""

        ended: list[str] = []
        rows = connection.execute(
            "SELECT * FROM dr_segments WHERE task_id=? AND ended_at IS NULL",
            (task["task_id"],)).fetchall()
        for row in rows:
            if finished_at < parse_instant(row["started_at"], "started_at"):
                raise ValidationError("结束时间早于进行中区段的开始时间")
            connection.execute("UPDATE dr_segments SET ended_at=? WHERE segment_id=?",
                               (format_instant(finished_at), row["segment_id"]))
            ended.append(row["segment_id"])
        connection.execute(
            "UPDATE dr_assignments SET status='ended', ended_at=?, end_reason=?"
            " WHERE task_id=? AND status IN ('pending','active')",
            (self._now(), end_reason, task["task_id"]))
        return ended

    # ------------------------------------------------------------------
    # 扣款证据、结算、账期与复算
    # ------------------------------------------------------------------

    def submit_evidence(self, *, request_id: str, actor_id: str, task_id: str,
                        kind: str, detail: dict[str, Any]) -> WriteReceipt:
        payload = {"actor_id": actor_id, "task_id": task_id, "kind": kind, "detail": detail}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "carrier")
            task_id = self._identifier(task_id, "task_id")
            kind = self._text(kind, "kind", 60)
            if not isinstance(detail, dict) or not detail:
                raise ValidationError("detail 必须是非空对象")

            def create() -> tuple[str, str, dict[str, Any]]:
                task = self._task_row(connection, task_id)
                self._require_carrier_org(actor, task["organization_id"])
                evidence_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO dr_evidence(evidence_id,task_id,kind,detail_json,"
                    "submitted_by,submitted_at) VALUES(?,?,?,?,?,?)",
                    (evidence_id, task_id, kind, canonical_json(detail), actor_id, self._now()))
                append_event(connection, actor_id=actor_id, action="evidence.submitted",
                             resource_type="evidence", resource_id=evidence_id,
                             detail={"task_id": task_id, "kind": kind},
                             occurred_at=self._now())
                return "evidence", evidence_id, {"evidence_id": evidence_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="submit_evidence", payload=payload, create=create)

    def propose_deduction(self, *, request_id: str, actor_id: str, task_id: str,
                          driver_id: str, amount_cents: int, reason: str,
                          evidence_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "task_id": task_id, "driver_id": driver_id,
                   "amount_cents": amount_cents, "reason": reason, "evidence_id": evidence_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "carrier")
            task_id = self._identifier(task_id, "task_id")
            driver_id = self._identifier(driver_id, "driver_id")
            amount = self._cents(amount_cents, "amount_cents")
            reason = self._text(reason, "reason")
            evidence_id = self._identifier(evidence_id, "evidence_id")

            def create() -> tuple[str, str, dict[str, Any]]:
                task = self._task_row(connection, task_id)
                self._require_carrier_org(actor, task["organization_id"])
                if task["status"] not in ("completed", "cancelled"):
                    raise ConflictError("任务结束后才能登记扣款")
                evidence = connection.execute(
                    "SELECT * FROM dr_evidence WHERE evidence_id=?", (evidence_id,)).fetchone()
                if evidence is None or evidence["task_id"] != task_id:
                    raise ValidationError("扣款证据必须属于同一任务")
                if self._latest_assignment(connection, task_id, driver_id) is None:
                    raise ValidationError("司机未参与该任务")
                deduction_id = uuid.uuid4().hex
                status = "proposed"
                statement_id = None
                if task["settled"]:
                    statement = self._statement_of(connection, task, driver_id)
                    if statement is None:
                        raise ValidationError("该司机在此任务没有结算单")
                    if statement["status"] != "open":
                        raise ConflictError("账期已关账，已结算金额不可追扣，"
                                            "请通过申诉与复算流程处理")
                    statement_id = statement["statement_id"]
                    status = "applied"
                connection.execute(
                    "INSERT INTO dr_deductions(deduction_id,task_id,driver_id,organization_id,"
                    "amount_cents,reason,evidence_id,status,statement_id,created_by,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (deduction_id, task_id, driver_id, task["organization_id"], amount,
                     reason, evidence_id, status, statement_id, actor_id, self._now()))
                if statement_id:
                    self._apply_deduction(connection, deduction_id=deduction_id)
                append_event(connection, actor_id=actor_id, action="deduction.proposed",
                             resource_type="deduction", resource_id=deduction_id,
                             detail={"task_id": task_id, "driver_id": driver_id,
                                     "amount_cents": amount, "status": status,
                                     "evidence_id": evidence_id},
                             occurred_at=self._now())
                return "deduction", deduction_id, {"deduction_id": deduction_id,
                                                   "status": status}

            return self._idempotent(connection, request_id=request_id,
                                    action="propose_deduction", payload=payload, create=create)

    def _statement_of(self, connection, task, driver_id: str):
        month = self._task_month(task)
        period_id = f"{task['organization_id']}:{month}"
        return connection.execute(
            "SELECT * FROM dr_statements WHERE period_id=? AND driver_id=?",
            (period_id, driver_id)).fetchone()

    def _apply_deduction(self, connection, *, deduction_id: str) -> None:
        deduction = connection.execute(
            "SELECT * FROM dr_deductions WHERE deduction_id=?", (deduction_id,)).fetchone()
        statement = connection.execute(
            "SELECT * FROM dr_statements WHERE statement_id=?",
            (deduction["statement_id"],)).fetchone()
        if statement["status"] != "open":
            raise ConflictError("账期已关账，已结算金额不可追扣")
        earned_row = connection.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS total FROM dr_statement_lines"
            " WHERE statement_id=? AND task_id=? AND component!='deduction'",
            (statement["statement_id"], deduction["task_id"])).fetchone()
        deducted_row = connection.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS total FROM dr_deductions"
            " WHERE statement_id=? AND task_id=? AND driver_id=?"
            " AND status IN ('applied','escrowed','released_to_carrier')",
            (statement["statement_id"], deduction["task_id"],
             deduction["driver_id"])).fetchone()
        if deducted_row["total"] + deduction["amount_cents"] > earned_row["total"]:
            raise ValidationError("扣款总额不能超过该任务的结算金额")
        connection.execute(
            "UPDATE dr_deductions SET status='applied' WHERE deduction_id=?", (deduction_id,))
        connection.execute(
            "INSERT OR IGNORE INTO dr_statement_lines(line_id,statement_id,task_id,component,"
            "ref_id,quantity_minutes,amount_cents,rule_version,detail_json,created_at)"
            " VALUES(?,?,?,'deduction',?,0,?,?,?,?)",
            (uuid.uuid4().hex, statement["statement_id"], deduction["task_id"],
             deduction["deduction_id"], -deduction["amount_cents"], "evidence",
             canonical_json({"reason": deduction["reason"],
                             "evidence_id": deduction["evidence_id"],
                             "driver_id": deduction["driver_id"]}), self._now()))
        self._refresh_statement(connection, statement["statement_id"])

    def cancel_deduction(self, *, request_id: str, actor_id: str,
                         deduction_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "deduction_id": deduction_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "carrier")
            deduction_id = self._identifier(deduction_id, "deduction_id")

            def create() -> tuple[str, str, dict[str, Any]]:
                deduction = connection.execute(
                    "SELECT * FROM dr_deductions WHERE deduction_id=?",
                    (deduction_id,)).fetchone()
                if deduction is None:
                    raise NotFoundError("扣款不存在")
                self._require_carrier_org(actor, deduction["organization_id"])
                if deduction["status"] == "proposed":
                    connection.execute(
                        "UPDATE dr_deductions SET status='cancelled' WHERE deduction_id=?",
                        (deduction_id,))
                elif deduction["status"] == "applied":
                    statement = connection.execute(
                        "SELECT * FROM dr_statements WHERE statement_id=?",
                        (deduction["statement_id"],)).fetchone()
                    if statement["status"] != "open":
                        raise ConflictError("账期已关账，扣款状态不可再变更")
                    connection.execute(
                        "DELETE FROM dr_statement_lines WHERE statement_id=? AND component='deduction'"
                        " AND ref_id=?", (statement["statement_id"], deduction_id))
                    connection.execute(
                        "UPDATE dr_deductions SET status='cancelled' WHERE deduction_id=?",
                        (deduction_id,))
                    self._refresh_statement(connection, statement["statement_id"])
                else:
                    raise ConflictError("当前状态的扣款不能撤销")
                append_event(connection, actor_id=actor_id, action="deduction.cancelled",
                             resource_type="deduction", resource_id=deduction_id,
                             detail={}, occurred_at=self._now())
                return "deduction", deduction_id, {"deduction_id": deduction_id,
                                                   "status": "cancelled"}

            return self._idempotent(connection, request_id=request_id,
                                    action="cancel_deduction", payload=payload, create=create)

    def _ensure_statement(self, connection, *, organization_id: str, month: str,
                          driver_id: str):
        period_id = f"{organization_id}:{month}"
        period = connection.execute(
            "SELECT * FROM dr_periods WHERE period_id=?", (period_id,)).fetchone()
        if period is None:
            connection.execute(
                "INSERT INTO dr_periods(period_id,organization_id,month,status)"
                " VALUES(?,?,?,'open')", (period_id, organization_id, month))
        elif period["status"] == "closed":
            raise ConflictError("账期已关账，不能再写入新的结算")
        statement_id = f"{period_id}:{driver_id}"
        statement = connection.execute(
            "SELECT * FROM dr_statements WHERE statement_id=?", (statement_id,)).fetchone()
        if statement is None:
            connection.execute(
                "INSERT INTO dr_statements(statement_id,period_id,driver_id,organization_id,"
                "status,created_at) VALUES(?,?,?,?,'open',?)",
                (statement_id, period_id, driver_id, organization_id, self._now()))
            statement = connection.execute(
                "SELECT * FROM dr_statements WHERE statement_id=?", (statement_id,)).fetchone()
        if statement["status"] != "open":
            raise ConflictError("结算单已关账，不能再写入")
        return statement

    def _refresh_statement(self, connection, statement_id: str) -> None:
        row = connection.execute(
            "SELECT COALESCE(SUM(CASE WHEN status IN ('applied','released_to_carrier')"
            " THEN amount_cents ELSE 0 END),0) AS deducted,"
            " COALESCE(SUM(CASE WHEN status='escrowed' THEN amount_cents ELSE 0 END),0) AS escrow,"
            " COALESCE(SUM(CASE WHEN status='released_to_driver' THEN amount_cents ELSE 0 END),0)"
            " AS returned FROM dr_deductions WHERE statement_id=?",
            (statement_id,)).fetchone()
        statement = connection.execute(
            "SELECT gross_cents FROM dr_statements WHERE statement_id=?",
            (statement_id,)).fetchone()
        # 应付 = 总收入 - 生效扣款 - 托管中金额；释放给司机的扣款不再扣减，
        # 自然回到应付中，不需要再加回。
        payable = statement["gross_cents"] - row["deducted"] - row["escrow"]
        connection.execute(
            "UPDATE dr_statements SET deduction_cents=?, escrow_cents=?, returned_cents=?,"
            " payable_cents=? WHERE statement_id=?",
            (row["deducted"], row["escrow"], row["returned"], payable, statement_id))

    def settle_task(self, *, request_id: str, actor_id: str, task_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "task_id": task_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "carrier")
            task_id = self._identifier(task_id, "task_id")

            def create() -> tuple[str, str, dict[str, Any]]:
                task = self._task_row(connection, task_id)
                self._require_carrier_org(actor, task["organization_id"])
                if task["status"] not in ("completed", "cancelled"):
                    raise ConflictError("任务结束后才能结算")
                month = self._task_month(task)
                statements: list[dict[str, Any]] = []
                drivers = [row["driver_id"] for row in connection.execute(
                    "SELECT DISTINCT driver_id FROM dr_segments WHERE task_id=?", (task_id,))]
                for driver_id in sorted(drivers):
                    assignment = self._latest_assignment(connection, task_id, driver_id)
                    if assignment is None:
                        raise ValidationError("存在没有派单记录的履约区段")
                    statement = self._ensure_statement(
                        connection, organization_id=task["organization_id"],
                        month=month, driver_id=driver_id)
                    existing = connection.execute(
                        "SELECT 1 FROM dr_statement_lines WHERE statement_id=? AND task_id=?"
                        " LIMIT 1", (statement["statement_id"], task_id)).fetchone()
                    if existing:
                        continue
                    terms = self._terms_for(connection, assignment["contract_id"],
                                            assignment["contract_version"])
                    rule_version = f"{assignment['contract_id']}@{assignment['contract_version']}"
                    segments = self._segments_of(connection, task_id=task_id,
                                                 driver_id=driver_id)
                    lines = compute_lines(
                        task_id=task_id, driver_id=driver_id,
                        base_freight_cents=task["base_freight_cents"], segments=segments,
                        terms=terms, timezone_name=task["timezone_name"],
                        rule_version=rule_version)
                    for line in lines:
                        connection.execute(
                            "INSERT INTO dr_statement_lines(line_id,statement_id,task_id,"
                            "component,ref_id,quantity_minutes,amount_cents,rule_version,"
                            "detail_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (uuid.uuid4().hex, statement["statement_id"], task_id,
                             line.component, line.ref_id, line.quantity_minutes,
                             line.amount_cents, line.rule_version,
                             canonical_json(line.detail), self._now()))
                    connection.execute(
                        "UPDATE dr_statements SET gross_cents=gross_cents+? WHERE statement_id=?",
                        (lines_total(lines), statement["statement_id"]))
                    pending = connection.execute(
                        "SELECT deduction_id FROM dr_deductions WHERE task_id=? AND driver_id=?"
                        " AND status='proposed' ORDER BY created_at",
                        (task_id, driver_id)).fetchall()
                    for row in pending:
                        connection.execute(
                            "UPDATE dr_deductions SET statement_id=? WHERE deduction_id=?",
                            (statement["statement_id"], row["deduction_id"]))
                        self._apply_deduction(connection, deduction_id=row["deduction_id"])
                    self._refresh_statement(connection, statement["statement_id"])
                    statements.append({"statement_id": statement["statement_id"],
                                       "driver_id": driver_id})
                connection.execute(
                    "UPDATE dr_segments SET frozen=1 WHERE task_id=? AND ended_at IS NOT NULL",
                    (task_id,))
                connection.execute("UPDATE dr_tasks SET settled=1 WHERE task_id=?", (task_id,))
                append_event(connection, actor_id=actor_id, action="settlement.run",
                             resource_type="task", resource_id=task_id,
                             detail={"month": month, "statements": statements},
                             occurred_at=self._now())
                return "settlement", task_id, {"task_id": task_id, "month": month,
                                               "statements": statements}

            return self._idempotent(connection, request_id=request_id,
                                    action="settle_task", payload=payload, create=create)

    def close_period(self, *, request_id: str, actor_id: str, organization_id: str,
                     month: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "organization_id": organization_id, "month": month}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "carrier")
            organization_id = self._identifier(organization_id, "organization_id")
            month = self._month(month)
            self._require_carrier_org(actor, organization_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                period_id = f"{organization_id}:{month}"
                period = connection.execute(
                    "SELECT * FROM dr_periods WHERE period_id=?", (period_id,)).fetchone()
                if period is None:
                    connection.execute(
                        "INSERT INTO dr_periods(period_id,organization_id,month,status)"
                        " VALUES(?,?,?,'open')", (period_id, organization_id, month))
                    period = connection.execute(
                        "SELECT * FROM dr_periods WHERE period_id=?", (period_id,)).fetchone()
                if period["status"] == "closed":
                    raise ConflictError("账期已经关账")
                pending = connection.execute(
                    "SELECT task_id FROM dr_tasks WHERE organization_id=? AND settled=0"
                    " AND status IN ('completed','cancelled')",
                    (organization_id,)).fetchall()
                blocking = [row["task_id"] for row in pending
                            if self._task_month(self._task_row(connection, row["task_id"])) == month]
                if blocking:
                    raise ConflictError(f"存在未结算的已结束任务: {blocking[0]}")
                cursor = connection.execute(
                    "UPDATE dr_statements SET status='closed', closed_at=?"
                    " WHERE period_id=? AND status='open'", (self._now(), period_id))
                connection.execute(
                    "UPDATE dr_periods SET status='closed', closed_at=?, closed_by=?"
                    " WHERE period_id=?", (self._now(), actor_id, period_id))
                append_event(connection, actor_id=actor_id, action="period.closed",
                             resource_type="period", resource_id=period_id,
                             detail={"statements_closed": cursor.rowcount},
                             occurred_at=self._now())
                return "period", period_id, {"period_id": period_id,
                                             "statements_closed": cursor.rowcount}

            return self._idempotent(connection, request_id=request_id,
                                    action="close_period", payload=payload, create=create)

    def recompute_period(self, *, request_id: str, actor_id: str, organization_id: str,
                         month: str, contract_version: int | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "organization_id": organization_id,
                   "month": month, "contract_version": contract_version}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "carrier", "regulator")
            organization_id = self._identifier(organization_id, "organization_id")
            month = self._month(month)
            if actor.role == "carrier":
                self._require_carrier_org(actor, organization_id)
            if contract_version is not None:
                if isinstance(contract_version, bool) or not isinstance(contract_version, int) \
                        or contract_version < 1:
                    raise ValidationError("contract_version 必须是正整数")

            def create() -> tuple[str, str, dict[str, Any]]:
                period_id = f"{organization_id}:{month}"
                period = connection.execute(
                    "SELECT * FROM dr_periods WHERE period_id=?", (period_id,)).fetchone()
                if period is None:
                    raise NotFoundError("账期不存在")
                report_statements = []
                statements = connection.execute(
                    "SELECT * FROM dr_statements WHERE period_id=? ORDER BY driver_id",
                    (period_id,)).fetchall()
                for statement in statements:
                    tasks = connection.execute(
                        "SELECT DISTINCT task_id FROM dr_statement_lines WHERE statement_id=?",
                        (statement["statement_id"],)).fetchall()
                    task_entries = []
                    old_gross = 0
                    new_gross = 0
                    for task_row in tasks:
                        task = self._task_row(connection, task_row["task_id"])
                        assignment = self._latest_assignment(
                            connection, task["task_id"], statement["driver_id"])
                        version = contract_version or self._latest_version(
                            connection, "dr_contracts", "contract_id",
                            assignment["contract_id"])
                        terms = self._terms_for(connection, assignment["contract_id"], version)
                        segments = self._segments_of(
                            connection, task_id=task["task_id"],
                            driver_id=statement["driver_id"])
                        lines = compute_lines(
                            task_id=task["task_id"], driver_id=statement["driver_id"],
                            base_freight_cents=task["base_freight_cents"], segments=segments,
                            terms=terms, timezone_name=task["timezone_name"],
                            rule_version=f"{assignment['contract_id']}@{version}")
                        old_row = connection.execute(
                            "SELECT COALESCE(SUM(amount_cents),0) AS total,"
                            " MAX(rule_version) AS rule_version FROM dr_statement_lines"
                            " WHERE statement_id=? AND task_id=? AND component!='deduction'",
                            (statement["statement_id"], task["task_id"])).fetchone()
                        old_cents = old_row["total"]
                        new_cents = lines_total(lines)
                        old_gross += old_cents
                        new_gross += new_cents
                        task_entries.append({
                            "task_id": task["task_id"], "old_cents": old_cents,
                            "new_cents": new_cents, "delta_cents": new_cents - old_cents,
                            "old_rule_version": old_row["rule_version"],
                            "new_rule_version": f"{assignment['contract_id']}@{version}"})
                    report_statements.append({
                        "statement_id": statement["statement_id"],
                        "driver_id": statement["driver_id"],
                        "statement_status": statement["status"],
                        "old_gross_cents": old_gross, "new_gross_cents": new_gross,
                        "delta_cents": new_gross - old_gross, "tasks": task_entries})
                result = {"period_id": period_id, "period_status": period["status"],
                          "contract_version": contract_version,
                          "statements": report_statements}
                report_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO dr_recompute_reports(report_id,period_id,organization_id,"
                    "contract_version,result_json,created_by,created_at)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (report_id, period_id, organization_id, contract_version,
                     canonical_json(result), actor_id, self._now()))
                append_event(connection, actor_id=actor_id, action="period.recomputed",
                             resource_type="period", resource_id=period_id,
                             detail={"report_id": report_id,
                                     "contract_version": contract_version,
                                     "statements": len(report_statements)},
                             occurred_at=self._now())
                return "recompute_report", report_id, {"report_id": report_id,
                                                       "period_id": period_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="recompute_period", payload=payload, create=create)

    def mark_statement_paid(self, *, request_id: str, actor_id: str,
                            statement_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "statement_id": statement_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "carrier")

            def create() -> tuple[str, str, dict[str, Any]]:
                statement = connection.execute(
                    "SELECT * FROM dr_statements WHERE statement_id=?",
                    (statement_id,)).fetchone()
                if statement is None:
                    raise NotFoundError("结算单不存在")
                self._require_carrier_org(actor, statement["organization_id"])
                if statement["status"] != "closed":
                    raise ConflictError("只有已关账的结算单可以登记支付")
                connection.execute(
                    "UPDATE dr_statements SET status='paid', paid_cents=payable_cents,"
                    " paid_at=? WHERE statement_id=?", (self._now(), statement_id))
                append_event(connection, actor_id=actor_id, action="statement.paid",
                             resource_type="statement", resource_id=statement_id,
                             detail={"paid_cents": statement["payable_cents"],
                                     "escrow_cents": statement["escrow_cents"]},
                             occurred_at=self._now())
                return "statement", statement_id, {
                    "statement_id": statement_id, "paid_cents": statement["payable_cents"],
                    "escrow_cents": statement["escrow_cents"]}

            return self._idempotent(connection, request_id=request_id,
                                    action="mark_statement_paid", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 申诉与争议托管
    # ------------------------------------------------------------------

    def file_appeal(self, *, request_id: str, actor_id: str, deduction_id: str,
                    reason: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "deduction_id": deduction_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "driver")
            deduction_id = self._identifier(deduction_id, "deduction_id")
            reason = self._text(reason, "reason")

            def create() -> tuple[str, str, dict[str, Any]]:
                deduction = connection.execute(
                    "SELECT * FROM dr_deductions WHERE deduction_id=?",
                    (deduction_id,)).fetchone()
                if deduction is None:
                    raise NotFoundError("扣款不存在")
                if deduction["driver_id"] != actor.actor_id:
                    raise PermissionDenied("只能对本人的扣款发起申诉")
                if deduction["status"] != "applied":
                    raise ConflictError("只有已入账的扣款可以申诉")
                statement = connection.execute(
                    "SELECT * FROM dr_statements WHERE statement_id=?",
                    (deduction["statement_id"],)).fetchone()
                deadline = None
                if statement["closed_at"]:
                    task = self._task_row(connection, deduction["task_id"])
                    assignment = self._latest_assignment(
                        connection, task["task_id"], deduction["driver_id"])
                    terms = self._terms_for(connection, assignment["contract_id"],
                                            assignment["contract_version"])
                    deadline = parse_instant(statement["closed_at"], "closed_at") + timedelta(
                        hours=terms["appeal_window_hours"])
                    if self.clock.now() > deadline:
                        raise ConflictError("已超过合同约定的申诉时限")
                appeal_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO dr_appeals(appeal_id,deduction_id,driver_id,organization_id,"
                    "reason,status,deadline,filed_at) VALUES(?,?,?,?,?,'filed',?,?)",
                    (appeal_id, deduction_id, deduction["driver_id"],
                     deduction["organization_id"], reason,
                     format_instant(deadline) if deadline else None, self._now()))
                connection.execute(
                    "UPDATE dr_deductions SET status='escrowed' WHERE deduction_id=?",
                    (deduction_id,))
                connection.execute(
                    "INSERT INTO dr_escrow_entries(entry_id,deduction_id,appeal_id,direction,"
                    "amount_cents,created_at) VALUES(?,?,?,'hold',?,?)",
                    (uuid.uuid4().hex, deduction_id, appeal_id,
                     deduction["amount_cents"], self._now()))
                self._refresh_statement(connection, statement["statement_id"])
                append_event(connection, actor_id=actor_id, action="appeal.filed",
                             resource_type="appeal", resource_id=appeal_id,
                             detail={"deduction_id": deduction_id,
                                     "amount_cents": deduction["amount_cents"],
                                     "deadline": format_instant(deadline) if deadline else None},
                             occurred_at=self._now())
                return "appeal", appeal_id, {
                    "appeal_id": appeal_id,
                    "deadline": format_instant(deadline) if deadline else None}

            return self._idempotent(connection, request_id=request_id,
                                    action="file_appeal", payload=payload, create=create)

    def resolve_appeal(self, *, request_id: str, actor_id: str, appeal_id: str,
                       outcome: str, note: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "appeal_id": appeal_id,
                   "outcome": outcome, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "regulator")
            appeal_id = self._identifier(appeal_id, "appeal_id")
            if outcome not in ("upheld", "rejected"):
                raise ValidationError("outcome 必须是 upheld 或 rejected")
            note = self._text(note, "note")

            def create() -> tuple[str, str, dict[str, Any]]:
                appeal = connection.execute(
                    "SELECT * FROM dr_appeals WHERE appeal_id=?", (appeal_id,)).fetchone()
                if appeal is None:
                    raise NotFoundError("申诉不存在")
                if appeal["status"] != "filed":
                    raise ConflictError("申诉已经处理完毕")
                deduction = connection.execute(
                    "SELECT * FROM dr_deductions WHERE deduction_id=?",
                    (appeal["deduction_id"],)).fetchone()
                new_status = "released_to_driver" if outcome == "upheld" else "released_to_carrier"
                connection.execute(
                    "UPDATE dr_deductions SET status=? WHERE deduction_id=?",
                    (new_status, deduction["deduction_id"]))
                connection.execute(
                    "INSERT INTO dr_escrow_entries(entry_id,deduction_id,appeal_id,direction,"
                    "amount_cents,created_at) VALUES(?,?,?,?,?,?)",
                    (uuid.uuid4().hex, deduction["deduction_id"], appeal_id,
                     f"release_to_{'driver' if outcome == 'upheld' else 'carrier'}",
                     deduction["amount_cents"], self._now()))
                connection.execute(
                    "UPDATE dr_appeals SET status=?, resolved_by=?, resolved_at=?,"
                    " resolution_note=? WHERE appeal_id=?",
                    (outcome, actor_id, self._now(), note, appeal_id))
                self._refresh_statement(connection, deduction["statement_id"])
                append_event(connection, actor_id=actor_id, action="appeal.resolved",
                             resource_type="appeal", resource_id=appeal_id,
                             detail={"outcome": outcome,
                                     "deduction_id": deduction["deduction_id"],
                                     "amount_cents": deduction["amount_cents"]},
                             occurred_at=self._now())
                return "appeal", appeal_id, {"appeal_id": appeal_id, "status": outcome}

            return self._idempotent(connection, request_id=request_id,
                                    action="resolve_appeal", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 分角色视图
    # ------------------------------------------------------------------

    def available_tasks(self, actor_id: str) -> list[dict[str, Any]]:
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "driver")
            rows = connection.execute(
                "SELECT t.*, a.status AS assignment_status, a.assignment_id FROM dr_tasks t"
                " LEFT JOIN dr_assignments a ON a.task_id=t.task_id AND a.driver_id=?"
                " AND a.status IN ('pending','active')"
                " WHERE t.organization_id=? AND t.status IN ('offered','assigned','in_progress')"
                " ORDER BY t.planned_start", (actor.actor_id, actor.organization_id)).fetchall()
            return [self._task_view(row) for row in rows]

    def _task_view(self, row) -> dict[str, Any]:
        keys = row.keys()
        view = {"task_id": row["task_id"], "organization_id": row["organization_id"],
                "origin": row["origin"], "destination": row["destination"],
                "planned_start": row["planned_start"], "planned_end": row["planned_end"],
                "timezone_name": row["timezone_name"],
                "base_freight_cents": row["base_freight_cents"], "status": row["status"],
                "settled": bool(row["settled"])}
        if "assignment_status" in keys:
            view["assignment_status"] = row["assignment_status"]
            view["assignment_id"] = row["assignment_id"]
        return view

    def task_detail(self, actor_id: str, task_id: str) -> dict[str, Any]:
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            task = self._task_row(connection, task_id)
            self._check_task_scope(actor, task)
            assignments = connection.execute(
                "SELECT * FROM dr_assignments WHERE task_id=? ORDER BY assigned_at",
                (task_id,)).fetchall()
            segments = self._segments_of(connection, task_id=task_id)
            return {**self._task_view(task),
                    "assignments": [dict(row) for row in assignments],
                    "segments": [{"segment_id": s.segment_id, "driver_id": s.driver_id,
                                  "kind": s.kind, "started_at": format_instant(s.started_at),
                                  "ended_at": format_instant(s.ended_at) if s.ended_at else None,
                                  "source": s.source, "frozen": s.frozen}
                                 for s in segments]}

    def _check_task_scope(self, actor: Actor, task) -> None:
        if actor.role in ("admin", "regulator"):
            return
        if actor.role == "carrier":
            if actor.organization_id != task["organization_id"]:
                raise PermissionDenied("只能查看本企业的任务")
            return
        if actor.role == "driver":
            row = self.database.connection.execute(
                "SELECT 1 FROM dr_assignments WHERE task_id=? AND driver_id=?",
                (task["task_id"], actor.actor_id)).fetchone()
            if row is None:
                raise PermissionDenied("只能查看本人参与的任务")
            return
        raise PermissionDenied("当前角色不能查看任务")

    def list_statements(self, actor_id: str, driver_id: str | None = None,
                        organization_id: str | None = None,
                        month: str | None = None) -> list[dict[str, Any]]:
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            query = ("SELECT s.*, p.month FROM dr_statements s"
                     " JOIN dr_periods p ON p.period_id=s.period_id WHERE 1=1")
            parameters: list[Any] = []
            if actor.role == "driver":
                query += " AND s.driver_id=?"
                parameters.append(actor.actor_id)
            elif actor.role == "carrier":
                query += " AND s.organization_id=?"
                parameters.append(actor.organization_id)
            elif actor.role not in ("admin", "regulator"):
                raise PermissionDenied("当前角色不能查看结算单")
            if driver_id:
                query += " AND s.driver_id=?"
                parameters.append(driver_id)
            if organization_id:
                query += " AND s.organization_id=?"
                parameters.append(organization_id)
            if month:
                query += " AND p.month=?"
                parameters.append(self._month(month))
            query += " ORDER BY s.statement_id"
            return [self._statement_view(connection, row)
                    for row in connection.execute(query, parameters)]

    def _statement_view(self, connection, row) -> dict[str, Any]:
        view = {key: row[key] for key in row.keys()}
        view["outstanding_cents"] = row["payable_cents"] - row["paid_cents"]
        return view

    def statement_detail(self, actor_id: str, statement_id: str) -> dict[str, Any]:
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            row = connection.execute(
                "SELECT s.*, p.month FROM dr_statements s"
                " JOIN dr_periods p ON p.period_id=s.period_id WHERE s.statement_id=?",
                (statement_id,)).fetchone()
            if row is None:
                raise NotFoundError("结算单不存在")
            if actor.role == "driver" and row["driver_id"] != actor.actor_id:
                raise PermissionDenied("只能查看本人的结算单")
            if actor.role == "carrier" and row["organization_id"] != actor.organization_id:
                raise PermissionDenied("只能查看本企业的结算单")
            if actor.role not in ("driver", "carrier", "regulator", "admin"):
                raise PermissionDenied("当前角色不能查看结算单")
            lines = connection.execute(
                "SELECT component,task_id,ref_id,quantity_minutes,amount_cents,rule_version,"
                "detail_json FROM dr_statement_lines WHERE statement_id=?"
                " ORDER BY created_at, rowid", (statement_id,)).fetchall()
            deductions = connection.execute(
                "SELECT * FROM dr_deductions WHERE statement_id=? ORDER BY created_at",
                (statement_id,)).fetchall()
            deduction_views = []
            for deduction in deductions:
                evidence = connection.execute(
                    "SELECT kind,detail_json FROM dr_evidence WHERE evidence_id=?",
                    (deduction["evidence_id"],)).fetchone()
                appeal = connection.execute(
                    "SELECT appeal_id,status,deadline,filed_at,resolved_at FROM dr_appeals"
                    " WHERE deduction_id=?", (deduction["deduction_id"],)).fetchone()
                deduction_views.append({
                    "deduction_id": deduction["deduction_id"],
                    "task_id": deduction["task_id"],
                    "amount_cents": deduction["amount_cents"],
                    "reason": deduction["reason"], "status": deduction["status"],
                    "evidence": {"evidence_id": deduction["evidence_id"],
                                 "kind": evidence["kind"],
                                 "detail": json.loads(evidence["detail_json"])}
                    if evidence else None,
                    "appeal": dict(appeal) if appeal else None})
            escrow = connection.execute(
                "SELECT e.entry_id,e.deduction_id,e.appeal_id,e.direction,e.amount_cents,"
                "e.created_at FROM dr_escrow_entries e JOIN dr_deductions d"
                " ON d.deduction_id=e.deduction_id WHERE d.statement_id=?"
                " ORDER BY e.created_at, e.rowid", (statement_id,)).fetchall()
            return {**self._statement_view(connection, row),
                    "lines": [{**{k: line[k] for k in line.keys() if k != "detail_json"},
                               "detail": json.loads(line["detail_json"])} for line in lines],
                    "deductions": deduction_views,
                    "escrow_entries": [dict(entry) for entry in escrow]}

    def list_violations(self, actor_id: str,
                        organization_id: str | None = None) -> list[dict[str, Any]]:
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            query = "SELECT * FROM dr_violations WHERE 1=1"
            parameters: list[Any] = []
            if actor.role == "driver":
                query += " AND driver_id=?"
                parameters.append(actor.actor_id)
            elif actor.role == "carrier":
                query += " AND organization_id=?"
                parameters.append(actor.organization_id)
            elif actor.role not in ("admin", "regulator"):
                raise PermissionDenied("当前角色不能查看超时责任")
            if organization_id:
                query += " AND organization_id=?"
                parameters.append(organization_id)
            query += " ORDER BY detected_at, violation_id"
            return [{"violation_id": row["violation_id"], "task_id": row["task_id"],
                     "driver_id": row["driver_id"], "organization_id": row["organization_id"],
                     "rule": row["rule"], "detail": json.loads(row["detail_json"]),
                     "responsible_actor_id": row["responsible_actor_id"],
                     "detected_at": row["detected_at"]}
                    for row in connection.execute(query, parameters)]

    def list_appeals(self, actor_id: str, organization_id: str | None = None,
                     driver_id: str | None = None) -> list[dict[str, Any]]:
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            query = "SELECT * FROM dr_appeals WHERE 1=1"
            parameters: list[Any] = []
            if actor.role == "driver":
                query += " AND driver_id=?"
                parameters.append(actor.actor_id)
            elif actor.role == "carrier":
                query += " AND organization_id=?"
                parameters.append(actor.organization_id)
            elif actor.role not in ("admin", "regulator"):
                raise PermissionDenied("当前角色不能查看申诉")
            if organization_id:
                query += " AND organization_id=?"
                parameters.append(organization_id)
            if driver_id:
                query += " AND driver_id=?"
                parameters.append(driver_id)
            query += " ORDER BY filed_at, appeal_id"
            return [dict(row) for row in connection.execute(query, parameters)]

    def list_recompute_reports(self, actor_id: str,
                               organization_id: str | None = None) -> list[dict[str, Any]]:
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            query = "SELECT * FROM dr_recompute_reports WHERE 1=1"
            parameters: list[Any] = []
            if actor.role == "carrier":
                query += " AND organization_id=?"
                parameters.append(actor.organization_id)
            elif actor.role not in ("admin", "regulator"):
                raise PermissionDenied("当前角色不能查看复算报告")
            if organization_id:
                query += " AND organization_id=?"
                parameters.append(organization_id)
            query += " ORDER BY created_at, report_id"
            return [{"report_id": row["report_id"], "period_id": row["period_id"],
                     "organization_id": row["organization_id"],
                     "contract_version": row["contract_version"],
                     "result": json.loads(row["result_json"]),
                     "created_by": row["created_by"], "created_at": row["created_at"]}
                    for row in connection.execute(query, parameters)]
