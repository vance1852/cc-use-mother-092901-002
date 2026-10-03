"""司机履约与权益保障领域服务。

协调可控时钟工时台账、派单原子校验、合同/规则版本化、结算与扣款证据、
申诉托管与账期复算；所有写操作具备请求幂等、SQLite 事务和哈希审计。
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Callable

from transport_coordination.audit import append_event, canonical_json, digest, verify_chain
from transport_coordination.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from . import domain as D
from .clock import Clock, SystemClock
from .models import AvailabilityView, Principal, RuleSet
from .settlement import (
    compute_trip_earnings,
    evidence_is_timely,
    merge_appeal,
    merge_settlement,
    recompute_period,
)
from .storage import Database
from .worktime import add_minutes, evaluate_availability, format_ts, parse_ts
from .worktime import merge_limits


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
INTERVAL_KINDS = frozenset({"driving", "loading", "waiting", "rest", "berth_rest", "rescue"})


class GuaranteeService:
    """履约与权益保障的全部用例。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _id(self, value: str, field: str) -> str:
        value = str(value or "").strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: str, field: str, limit: int = 300) -> str:
        value = str(value or "").strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 字符")
        return value

    def _amount(self, value: Any, field: str) -> int:
        try:
            amount = int(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{field} 必须是以分为单位的整数") from exc
        if amount < 0:
            raise ValidationError(f"{field} 不能为负")
        return amount

    def _ts(self, value: str | None, field: str, default_now: bool = False) -> str:
        if value is None and default_now:
            return self._now()
        if not value:
            raise ValidationError(f"{field} 不能为空")
        try:
            return format_ts(parse_ts(str(value)))
        except (ValueError, TypeError) as exc:
            raise ValidationError(f"{field} 不是合法的带时区时间") from exc

    def _principal(self, conn, principal_id: str) -> Principal:
        row = conn.execute("SELECT * FROM principals WHERE principal_id=?", (principal_id,)).fetchone()
        if row is None:
            raise NotFoundError("访问主体不存在")
        principal = Principal(row["principal_id"], row["role"], row["driver_id"],
                              row["carrier_id"], row["display_name"], bool(row["active"]))
        if not principal.active:
            raise PermissionDenied("访问主体已停用")
        return principal

    def _require(self, principal: Principal, *roles: str) -> None:
        if principal.role not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _require_carrier_scope(self, principal: Principal, carrier_id: str) -> None:
        if principal.role == D.ROLE_ADMIN or principal.role == D.ROLE_REGULATOR:
            return
        if principal.role != D.ROLE_CARRIER or principal.carrier_id != carrier_id:
            raise PermissionDenied("不能访问其他承运人的数据")

    def _driver_carrier(self, conn, driver_id: str) -> str:
        row = conn.execute("SELECT organization_id FROM drivers WHERE driver_id=?", (driver_id,)).fetchone()
        if row is None:
            raise NotFoundError("司机不存在")
        return row["organization_id"]

    def _require_driver_access(self, principal: Principal, conn, driver_id: str) -> None:
        if principal.role in (D.ROLE_ADMIN, D.ROLE_REGULATOR):
            return
        if principal.role == D.ROLE_DRIVER:
            if principal.driver_id != driver_id:
                raise PermissionDenied("司机只能操作本人台账")
            return
        if principal.role == D.ROLE_CARRIER:
            if self._driver_carrier(conn, driver_id) != principal.carrier_id:
                raise PermissionDenied("承运人只能操作本企业司机")
            return
        raise PermissionDenied("无权操作司机台账")

    def _early_replay(self, conn, *, request_id: str, action: str,
                      payload: dict[str, Any]) -> dict[str, Any] | None:
        """事务内最先执行的幂等检查：重放直接返回原回执，不再跑后续业务校验。"""

        try:
            request_id = self._id(request_id, "request_id")
        except ValidationError:
            return None
        row = conn.execute("SELECT * FROM request_receipts WHERE request_id=?",
                           (request_id,)).fetchone()
        if not row:
            return None
        if row["action"] != action or row["payload_hash"] != digest(payload):
            raise ConflictError("request_id 已被不同内容使用")
        return {"request_id": request_id, "resource_type": row["resource_type"],
                "resource_id": row["resource_id"], "replayed": True,
                "response": json.loads(row["response_json"])}

    def _idempotent(self, conn, *, request_id: str, action: str, payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]]]):
        request_id = self._id(request_id, "request_id")
        payload_hash = digest(payload)
        row = conn.execute("SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return {"request_id": request_id, "resource_type": row["resource_type"],
                    "resource_id": row["resource_id"], "replayed": True,
                    "response": json.loads(row["response_json"])}
        resource_type, resource_id, response = create()
        conn.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
            "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return {"request_id": request_id, "resource_type": resource_type,
                "resource_id": resource_id, "replayed": False, "response": response}

    def _audit(self, conn, *, actor_id: str, action: str, resource_type: str,
               resource_id: str, detail: dict[str, Any]) -> None:
        append_event(conn, actor_id=actor_id, action=action, resource_type=resource_type,
                     resource_id=resource_id, detail=detail, occurred_at=self._now())

    def _latest_freeze(self, conn, driver_id: str) -> str | None:
        row = conn.execute(
            "SELECT frozen_through FROM ledger_freezes WHERE driver_id=? "
            "ORDER BY frozen_through DESC LIMIT 1", (driver_id,)
        ).fetchone()
        return row["frozen_through"] if row else None

    def _assert_unfrozen(self, conn, driver_id: str, at: str) -> None:
        boundary = self._latest_freeze(conn, driver_id)
        if boundary is not None and at <= boundary:
            raise ConflictError(f"台账在 {boundary} 及之前的区段已冻结，只能变更未冻结区段")

    def _assert_no_overlap(self, conn, driver_id: str, start_at: str, end_at: str | None) -> None:
        """同一司机同一时段只能有一条工时记录（区间半开，首尾相接允许）。"""

        if end_at is not None:
            row = conn.execute(
                "SELECT 1 FROM work_segments WHERE driver_id=? AND start_at<? AND end_at>? LIMIT 1",
                (driver_id, end_at, start_at)).fetchone()
            if row:
                raise ConflictError("新区间与既有工时区段重叠")
            row = conn.execute(
                "SELECT 1 FROM work_log_events WHERE driver_id=? AND end_at IS NULL "
                "AND superseded_by IS NULL AND start_at<? LIMIT 1",
                (driver_id, end_at)).fetchone()
            if row:
                raise ConflictError("存在尚未关闭的工时事件，不能登记与之重叠的新区间")
        else:
            row = conn.execute(
                "SELECT 1 FROM work_log_events WHERE driver_id=? AND end_at IS NULL "
                "AND superseded_by IS NULL LIMIT 1", (driver_id,)).fetchone()
            if row:
                raise ConflictError("该司机已有未关闭的工时事件")
            row = conn.execute(
                "SELECT 1 FROM work_segments WHERE driver_id=? AND end_at>? LIMIT 1",
                (driver_id, start_at)).fetchone()
            if row:
                raise ConflictError("开始时间落在既有工时区段之内或之前")

    def _load_ruleset(self, conn, ruleset_id: str, version: int) -> RuleSet:
        row = conn.execute("SELECT * FROM ruleset_versions WHERE ruleset_id=? AND version=?",
                           (ruleset_id, version)).fetchone()
        if row is None:
            raise NotFoundError("规则版本不存在")
        return RuleSet(row["ruleset_id"], row["version"], row["title"],
                       json.loads(row["limits_json"]), json.loads(row["settlement_json"]),
                       json.loads(row["appeal_json"]), row["effective_from"], row["created_at"])

    def _active_ruleset(self, conn, at: str) -> RuleSet:
        row = conn.execute(
            "SELECT * FROM ruleset_versions WHERE effective_from<=? "
            "ORDER BY effective_from DESC, version DESC LIMIT 1", (at,)
        ).fetchone()
        if row is None:
            raise NotFoundError("当前时间没有生效的规则集合")
        return self._load_ruleset(conn, row["ruleset_id"], row["version"])

    def _load_trip(self, conn, trip_id: str) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM trips WHERE trip_id=?", (trip_id,)).fetchone()
        if row is None:
            raise NotFoundError("任务不存在")
        return dict(row)

    # ------------------------------------------------------------------
    # 主体建档
    # ------------------------------------------------------------------

    def bootstrap_admin(self, *, request_id: str, principal_id: str, display_name: str) -> dict[str, Any]:
        """创建首位管理员（仅当系统中尚无任何主体）。"""

        payload = {"principal_id": principal_id, "display_name": display_name}
        with self.database.transaction(immediate=True) as conn:
            early = self._early_replay(conn, request_id=request_id,
                                          action="bootstrap_admin", payload=payload)
            if early is not None:
                return early
            if conn.execute("SELECT COUNT(*) AS c FROM principals").fetchone()["c"]:
                raise PermissionDenied("系统已初始化，bootstrap 不可再用")
            principal_id = self._id(principal_id, "principal_id")
            display_name = self._text(display_name, "display_name")

            def create():
                conn.execute(
                    "INSERT INTO principals(principal_id,role,driver_id,carrier_id,display_name,active,created_at) "
                    "VALUES(?,?,NULL,NULL,?,1,?)",
                    (principal_id, D.ROLE_ADMIN, display_name, self._now()),
                )
                self._audit(conn, actor_id="bootstrap", action="principal.bootstrapped",
                            resource_type="principal", resource_id=principal_id,
                            detail={"role": D.ROLE_ADMIN})
                return "principal", principal_id, {"principal_id": principal_id, "role": D.ROLE_ADMIN}

            return self._idempotent(conn, request_id=request_id, action="bootstrap_admin",
                                    payload=payload, create=create)

    def register_carrier(self, *, request_id: str, actor_id: str, carrier_id: str, name: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "carrier_id": carrier_id, "name": name}
        with self.database.transaction(immediate=True) as conn:
            early = self._early_replay(conn, request_id=request_id,
                                          action="register_carrier", payload=payload)
            if early is not None:
                return early
            actor = self._principal(conn, actor_id)
            self._require(actor, D.ROLE_ADMIN)
            carrier_id = self._id(carrier_id, "carrier_id")
            name = self._text(name, "name")

            def create():
                try:
                    conn.execute("INSERT INTO carriers(carrier_id,name,created_at) VALUES(?,?,?)",
                                 (carrier_id, name, self._now()))
                except Exception as exc:
                    raise ConflictError("承运人编号已存在") from exc
                self._audit(conn, actor_id=actor_id, action="carrier.registered",
                            resource_type="carrier", resource_id=carrier_id, detail={"name": name})
                return "carrier", carrier_id, {"carrier_id": carrier_id}

            return self._idempotent(conn, request_id=request_id, action="register_carrier",
                                    payload=payload, create=create)

    def register_driver(self, *, request_id: str, actor_id: str, driver_id: str,
                        display_name: str, license_no: str, organization_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "driver_id": driver_id, "display_name": display_name,
                   "license_no": license_no, "organization_id": organization_id}
        with self.database.transaction(immediate=True) as conn:
            early = self._early_replay(conn, request_id=request_id,
                                          action="register_driver", payload=payload)
            if early is not None:
                return early
            actor = self._principal(conn, actor_id)
            self._require(actor, D.ROLE_ADMIN, D.ROLE_CARRIER)
            if actor.role == D.ROLE_CARRIER and organization_id != actor.carrier_id:
                raise PermissionDenied("承运人只能为本企业登记司机")
            if conn.execute("SELECT 1 FROM carriers WHERE carrier_id=?", (organization_id,)).fetchone() is None:
                raise NotFoundError("承运人不存在")
            driver_id = self._id(driver_id, "driver_id")
            display_name = self._text(display_name, "display_name")
            license_no = self._text(license_no, "license_no", 80)

            def create():
                try:
                    conn.execute(
                        "INSERT INTO drivers(driver_id,display_name,license_no,organization_id,active,created_at) "
                        "VALUES(?,?,?,?,1,?)",
                        (driver_id, display_name, license_no, organization_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("司机编号或驾驶证号已存在") from exc
                self._audit(conn, actor_id=actor_id, action="driver.registered",
                            resource_type="driver", resource_id=driver_id,
                            detail={"organization_id": organization_id, "license_no": license_no})
                return "driver", driver_id, {"driver_id": driver_id}

            return self._idempotent(conn, request_id=request_id, action="register_driver",
                                    payload=payload, create=create)

    def register_principal(self, *, request_id: str, actor_id: str, principal_id: str, role: str,
                           display_name: str, driver_id: str | None = None,
                           carrier_id: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "principal_id": principal_id, "role": role,
                   "display_name": display_name, "driver_id": driver_id, "carrier_id": carrier_id}
        with self.database.transaction(immediate=True) as conn:
            early = self._early_replay(conn, request_id=request_id,
                                          action="register_principal", payload=payload)
            if early is not None:
                return early
            actor = self._principal(conn, actor_id)
            self._require(actor, D.ROLE_ADMIN)
            principal_id = self._id(principal_id, "principal_id")
            display_name = self._text(display_name, "display_name")
            if role not in D.ROLES:
                raise ValidationError("role 不合法")
            if role == D.ROLE_DRIVER:
                if not driver_id or conn.execute("SELECT 1 FROM drivers WHERE driver_id=?",
                                                 (driver_id,)).fetchone() is None:
                    raise ValidationError("司机主体必须绑定已登记司机")
                carrier_id = None
            elif role == D.ROLE_CARRIER:
                if not carrier_id or conn.execute("SELECT 1 FROM carriers WHERE carrier_id=?",
                                                  (carrier_id,)).fetchone() is None:
                    raise ValidationError("承运人主体必须绑定已登记承运人")
                driver_id = None
            else:
                driver_id = carrier_id = None

            def create():
                try:
                    conn.execute(
                        "INSERT INTO principals(principal_id,role,driver_id,carrier_id,display_name,active,created_at) "
                        "VALUES(?,?,?,?,?,1,?)",
                        (principal_id, role, driver_id, carrier_id, display_name, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("主体编号已存在") from exc
                self._audit(conn, actor_id=actor_id, action="principal.registered",
                            resource_type="principal", resource_id=principal_id,
                            detail={"role": role, "driver_id": driver_id, "carrier_id": carrier_id})
                return "principal", principal_id, {"principal_id": principal_id, "role": role}

            return self._idempotent(conn, request_id=request_id, action="register_principal",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------
    # 合同版本与规则版本
    # ------------------------------------------------------------------

    def create_contract_version(self, *, request_id: str, actor_id: str, contract_id: str,
                                carrier_id: str, title: str, body: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(body, dict) or not body:
            raise ValidationError("body 必须是非空对象")
        payload = {"actor_id": actor_id, "contract_id": contract_id, "carrier_id": carrier_id,
                   "title": title, "body": body}
        with self.database.transaction(immediate=True) as conn:
            early = self._early_replay(conn, request_id=request_id,
                                          action="create_contract_version", payload=payload)
            if early is not None:
                return early
            actor = self._principal(conn, actor_id)
            self._require(actor, D.ROLE_ADMIN, D.ROLE_CARRIER)
            self._require_carrier_scope(actor, carrier_id)
            contract_id = self._id(contract_id, "contract_id")
            title = self._text(title, "title")
            row = conn.execute("SELECT COALESCE(MAX(version),0) AS v FROM contract_versions WHERE contract_id=?",
                               (contract_id,)).fetchone()
            version = row["v"] + 1

            def create():
                conn.execute(
                    "INSERT INTO contract_versions(contract_id,version,carrier_id,title,body_json,"
                    "effective_from,created_at) VALUES(?,?,?,?,?,?,?)",
                    (contract_id, version, carrier_id, title, canonical_json(body),
                     self._now(), self._now()),
                )
                self._audit(conn, actor_id=actor_id, action="contract.versioned",
                            resource_type="contract", resource_id=f"{contract_id}:v{version}",
                            detail={"contract_id": contract_id, "version": version,
                                    "carrier_id": carrier_id, "body_hash": digest(body)})
                return ("contract_version", f"{contract_id}:v{version}",
                        {"contract_id": contract_id, "version": version})

            return self._idempotent(conn, request_id=request_id, action="create_contract_version",
                                    payload=payload, create=create)

    def create_ruleset(self, *, request_id: str, actor_id: str, ruleset_id: str, title: str,
                       limits: dict[str, int] | None = None, settlement: dict[str, Any] | None = None,
                       appeal: dict[str, Any] | None = None,
                       effective_from: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "ruleset_id": ruleset_id, "title": title,
                   "limits": limits, "settlement": settlement, "appeal": appeal,
                   "effective_from": effective_from}
        with self.database.transaction(immediate=True) as conn:
            early = self._early_replay(conn, request_id=request_id,
                                          action="create_ruleset", payload=payload)
            if early is not None:
                return early
            actor = self._principal(conn, actor_id)
            self._require(actor, D.ROLE_ADMIN, D.ROLE_REGULATOR)
            ruleset_id = self._id(ruleset_id, "ruleset_id")
            title = self._text(title, "title")
            limits = merge_limits(limits)
            settlement = merge_settlement(settlement)
            appeal = merge_appeal(appeal)
            effective_from = self._ts(effective_from, "effective_from", default_now=True)
            row = conn.execute("SELECT COALESCE(MAX(version),0) AS v FROM ruleset_versions WHERE ruleset_id=?",
                               (ruleset_id,)).fetchone()
            version = row["v"] + 1

            def create():
                conn.execute(
                    "INSERT INTO ruleset_versions(ruleset_id,version,title,limits_json,settlement_json,"
                    "appeal_json,effective_from,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (ruleset_id, version, title, canonical_json(limits), canonical_json(settlement),
                     canonical_json(appeal), effective_from, self._now()),
                )
                self._audit(conn, actor_id=actor_id, action="ruleset.versioned",
                            resource_type="ruleset", resource_id=f"{ruleset_id}:v{version}",
                            detail={"ruleset_id": ruleset_id, "version": version,
                                    "effective_from": effective_from,
                                    "limits_hash": digest(limits), "settlement_hash": digest(settlement)})
                return ("ruleset_version", f"{ruleset_id}:v{version}",
                        {"ruleset_id": ruleset_id, "version": version,
                         "effective_from": effective_from})

            return self._idempotent(conn, request_id=request_id, action="create_ruleset",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------
    # 任务建档与派单原子校验
    # ------------------------------------------------------------------

    def create_trip(self, *, request_id: str, actor_id: str, trip_id: str, carrier_id: str,
                    origin: str, destination: str, planned_pickup_at: str, contract_id: str,
                    ruleset_id: str | None = None, estimated_driving_min: int = 0,
                    estimated_work_min: int | None = None, freight_amount: int = 0,
                    co_driver_id: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "trip_id": trip_id, "carrier_id": carrier_id,
                   "origin": origin, "destination": destination,
                   "planned_pickup_at": planned_pickup_at, "contract_id": contract_id,
                   "ruleset_id": ruleset_id, "estimated_driving_min": estimated_driving_min,
                   "estimated_work_min": estimated_work_min, "freight_amount": freight_amount,
                   "co_driver_id": co_driver_id}
        with self.database.transaction(immediate=True) as conn:
            early = self._early_replay(conn, request_id=request_id,
                                          action="create_trip", payload=payload)
            if early is not None:
                return early
            actor = self._principal(conn, actor_id)
            self._require(actor, D.ROLE_ADMIN, D.ROLE_CARRIER)
            self._require_carrier_scope(actor, carrier_id)
            trip_id = self._id(trip_id, "trip_id")
            origin = self._text(origin, "origin", 120)
            destination = self._text(destination, "destination", 120)
            planned_pickup_at = self._ts(planned_pickup_at, "planned_pickup_at")
            estimated_driving_min = max(0, int(estimated_driving_min or 0))
            estimated_work_min = estimated_driving_min if estimated_work_min is None else int(estimated_work_min)
            freight_amount = self._amount(freight_amount, "freight_amount")
            contract = conn.execute(
                "SELECT * FROM contract_versions WHERE contract_id=? ORDER BY version DESC LIMIT 1",
                (contract_id,)).fetchone()
            if contract is None:
                raise NotFoundError("合同不存在")
            if contract["carrier_id"] != carrier_id:
                raise ValidationError("合同不属于该承运人")
            if ruleset_id:
                rs_row = conn.execute(
                    "SELECT * FROM ruleset_versions WHERE ruleset_id=? AND effective_from<=? "
                    "ORDER BY version DESC LIMIT 1", (ruleset_id, planned_pickup_at)).fetchone()
            else:
                rs_row = conn.execute(
                    "SELECT * FROM ruleset_versions WHERE effective_from<=? "
                    "ORDER BY effective_from DESC, version DESC LIMIT 1", (planned_pickup_at,)).fetchone()
            if rs_row is None:
                raise NotFoundError("派单时点没有生效的规则集合")
            ruleset = self._load_ruleset(conn, rs_row["ruleset_id"], rs_row["version"])
            if co_driver_id and self._driver_carrier(conn, co_driver_id) != carrier_id:
                raise ValidationError("副驾必须属于同一承运人")
            snapshot = {"ruleset_id": ruleset.ruleset_id, "ruleset_version": ruleset.version,
                        "limits": ruleset.limits, "settlement": ruleset.settlement, "appeal": ruleset.appeal}

            def create():
                conn.execute(
                    "INSERT INTO trips(trip_id,carrier_id,driver_id,co_driver_id,origin,destination,"
                    "planned_pickup_at,status,contract_id,contract_version,ruleset_id,ruleset_version,"
                    "rule_snapshot_json,estimated_driving_min,estimated_work_min,freight_amount,"
                    "completed_at,forced,created_at) VALUES(?,?,NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,0,?)",
                    (trip_id, carrier_id, co_driver_id, origin, destination, planned_pickup_at,
                     D.TRIP_OPEN, contract_id, contract["version"], ruleset.ruleset_id, ruleset.version,
                     canonical_json(snapshot), estimated_driving_min, estimated_work_min,
                     freight_amount, self._now()),
                )
                self._audit(conn, actor_id=actor_id, action="trip.created",
                            resource_type="trip", resource_id=trip_id,
                            detail={"carrier_id": carrier_id, "contract_version": contract["version"],
                                    "ruleset": f"{ruleset.ruleset_id}:v{ruleset.version}"})
                return "trip", trip_id, {"trip_id": trip_id, "status": D.TRIP_OPEN,
                                         "ruleset": f"{ruleset.ruleset_id}:v{ruleset.version}"}

            return self._idempotent(conn, request_id=request_id, action="create_trip",
                                    payload=payload, create=create)

    def _segments_for_eval(self, conn, driver_id: str, at: str) -> list[dict[str, Any]]:
        """已完成区段 + 截止 at 仍在进行中的开放事件（裁剪到 at）。"""

        segments = [dict(r) for r in conn.execute(
            "SELECT driver_id,trip_id,kind,start_at,end_at,source_event_id FROM work_segments "
            "WHERE driver_id=? AND start_at<? ORDER BY start_at", (driver_id, at))]
        for row in conn.execute(
                "SELECT driver_id,trip_id,kind,start_at FROM work_log_events "
                "WHERE driver_id=? AND end_at IS NULL AND start_at<? AND superseded_by IS NULL",
                (driver_id, at)):
            segments.append({"driver_id": driver_id, "trip_id": row["trip_id"], "kind": row["kind"],
                             "start_at": row["start_at"], "end_at": at,
                             "source_event_id": "open"})
        return segments

    def check_availability(self, *, actor_id: str, driver_id: str, at: str | None = None,
                           trip_id: str | None = None, ruleset_id: str | None = None,
                           trip_driving_min: int | None = None,
                           trip_work_min: int | None = None) -> AvailabilityView:
        """派单前的剩余工时与后续休息窗口校验（只读）。"""

        with self.database.transaction() as conn:
            actor = self._principal(conn, actor_id)
            self._require_driver_access(actor, conn, driver_id)
            at = self._ts(at, "at", default_now=True)
            limits: dict[str, int] | None = None
            drive_min = work_min = 0
            if trip_id:
                trip = self._load_trip(conn, trip_id)
                snapshot = json.loads(trip["rule_snapshot_json"])
                limits = snapshot["limits"]
                drive_min = trip["estimated_driving_min"]
                work_min = trip["estimated_work_min"]
            elif ruleset_id:
                rs = conn.execute("SELECT * FROM ruleset_versions WHERE ruleset_id=? ORDER BY version DESC LIMIT 1",
                                  (ruleset_id,)).fetchone()
                if rs is None:
                    raise NotFoundError("规则集合不存在")
                limits = json.loads(rs["limits_json"])
            if trip_driving_min is not None:
                drive_min = int(trip_driving_min)
            if trip_work_min is not None:
                work_min = int(trip_work_min)
            segments = self._segments_for_eval(conn, driver_id, at)
            result = evaluate_availability(segments, at=at, limits=limits,
                                           trip_driving_min=drive_min, trip_work_min=work_min)
            return AvailabilityView(driver_id, at, result["eligible"], result["reasons"],
                                    result["window_start_at"], result["window_end_at"],
                                    result["continuous_driving_min"], result["driving_last_24h_min"],
                                    result["work_last_24h_min"], 0, result["rests_last_24h"])

    def dispatch_trip(self, *, request_id: str, actor_id: str, trip_id: str, driver_id: str,
                      co_driver_id: str | None = None, at: str | None = None,
                      force: bool = False) -> dict[str, Any]:
        """原子校验主副驾剩余工时与休息窗口，通过则派单；不通过默认拒绝并留痕。

        承运人/管理员可在留痕前提下强制派单（forced=1），作为超时责任归属依据。
        """

        payload = {"actor_id": actor_id, "trip_id": trip_id, "driver_id": driver_id,
                   "co_driver_id": co_driver_id, "at": at, "force": force}
        rejection: ConflictError | None = None
        with self.database.transaction(immediate=True) as conn:
            early = self._early_replay(conn, request_id=request_id,
                                          action="dispatch_trip", payload=payload)
            if early is not None:
                if early["resource_type"] == "dispatch_decision":
                    rejection = ConflictError(
                        "派单被工时/休息规则拒绝：" + ";".join(early["response"]["reasons"]))
                else:
                    return early
            actor = self._principal(conn, actor_id)
            self._require(actor, D.ROLE_ADMIN, D.ROLE_CARRIER, D.ROLE_REGULATOR)
            at = self._ts(at, "at", default_now=True)
            trip = self._load_trip(conn, trip_id)
            if actor.role == D.ROLE_CARRIER:
                self._require_carrier_scope(actor, trip["carrier_id"])
                if self._driver_carrier(conn, driver_id) != actor.carrier_id:
                    raise PermissionDenied("只能派遣本企业司机")
            if actor.role == D.ROLE_REGULATOR and force:
                raise PermissionDenied("监管人员不能强制派单，只能查看责任")
            if trip["status"] != D.TRIP_OPEN or trip["driver_id"]:
                raise ConflictError("任务已派单或已结束")
            snapshot = json.loads(trip["rule_snapshot_json"])
            effective_co = co_driver_id or trip["co_driver_id"]
            checks = []
            for d in [driver_id, effective_co]:
                if not d:
                    continue
                if self._driver_carrier(conn, d) != trip["carrier_id"]:
                    raise ValidationError(f"司机 {d} 不属于承运人 {trip['carrier_id']}")
                segs = self._segments_for_eval(conn, d, at)
                checks.append((d, evaluate_availability(
                    segs, at=at, limits=snapshot["limits"],
                    trip_driving_min=trip["estimated_driving_min"],
                    trip_work_min=trip["estimated_work_min"])))
            ineligible = [(d, r) for d, r in checks if not r["eligible"]]

            def record_decision(eligible: bool, forced_flag: int, reasons: list[str]) -> str:
                decision_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO dispatch_decisions(decision_id,trip_id,driver_id,co_driver_id,"
                    "checked_at,eligible,forced,reasons_json) VALUES(?,?,?,?,?,?,?,?)",
                    (decision_id, trip_id, driver_id, effective_co, at,
                     1 if eligible else 0, forced_flag, canonical_json(reasons)),
                )
                return decision_id

            def create():
                if ineligible and not force:
                    reasons = [f"{d}:{','.join(r['reasons'])}" for d, r in ineligible]
                    record_decision(False, 0, reasons)
                    self._audit(conn, actor_id=actor_id, action="dispatch.rejected",
                                resource_type="trip", resource_id=trip_id,
                                detail={"driver_id": driver_id, "co_driver_id": effective_co,
                                        "reasons": reasons, "at": at})
                    return "dispatch_decision", trip_id, {"eligible": False, "reasons": reasons}
                reasons = [f"{d}:{','.join(r['reasons'])}" for d, r in ineligible]
                decision_id = record_decision(not ineligible, 1 if force and ineligible else 0, reasons)
                conn.execute(
                    "UPDATE trips SET driver_id=?, co_driver_id=?,status=?,"
                    "forced=? WHERE trip_id=?",
                    (driver_id, effective_co, D.TRIP_DISPATCHED,
                     1 if (force and ineligible) else 0, trip_id))
                self._audit(conn, actor_id=actor_id,
                            action="dispatch.forced" if (force and ineligible) else "dispatch.accepted",
                            resource_type="trip", resource_id=trip_id,
                            detail={"driver_id": driver_id, "co_driver_id": effective_co,
                                    "forced": bool(force and ineligible), "reasons": reasons,
                                    "decision_id": decision_id, "at": at,
                                    "checks": {d: {"eligible": r["eligible"], "reasons": r["reasons"],
                                                   "continuous_driving_min": r["continuous_driving_min"],
                                                   "driving_last_24h_min": r["driving_last_24h_min"]}
                                               for d, r in checks}})
                return "trip", trip_id, {"trip_id": trip_id, "status": D.TRIP_DISPATCHED,
                                         "eligible": not bool(ineligible),
                                         "forced": bool(force and ineligible),
                                         "checks": {d: {"eligible": r["eligible"],
                                                        "reasons": r["reasons"],
                                                        "window_start_at": r["window_start_at"],
                                                        "window_end_at": r["window_end_at"]}
                                                    for d, r in checks}}

            receipt = self._idempotent(conn, request_id=request_id, action="dispatch_trip",
                                       payload=payload, create=create)
            if receipt["resource_type"] == "dispatch_decision":
                rejection = ConflictError("派单被工时/休息规则拒绝：" +
                                          ";".join(receipt["response"]["reasons"]))
        if rejection is not None:
            raise rejection
        return receipt

    # ------------------------------------------------------------------
    # 可控时钟工时台账
    # ------------------------------------------------------------------

    def _close_event(self, conn, event: dict[str, Any], end_at: str, frozen: bool = False) -> str:
        if end_at <= event["start_at"]:
            raise ValidationError("结束时间必须晚于开始时间")
        overlap = conn.execute(
            "SELECT 1 FROM work_segments WHERE driver_id=? AND source_event_id<>? "
            "AND start_at<? AND end_at>? LIMIT 1",
            (event["driver_id"], event["event_id"], end_at, event["start_at"])).fetchone()
        if overlap:
            raise ConflictError("关闭时间会与既有工时区段重叠")
        kind = event["kind"]
        if kind == "backfill":
            kind = json.loads(event["payload_json"]).get("target_kind", kind)
        segment_id = uuid.uuid4().hex
        duration = int(round((parse_ts(end_at) - parse_ts(event["start_at"])).total_seconds() / 60))
        conn.execute(
            "INSERT INTO work_segments(segment_id,driver_id,trip_id,kind,start_at,end_at,duration_min,"
            "source_event_id,frozen) VALUES(?,?,?,?,?,?,?,?,?)",
            (segment_id, event["driver_id"], event["trip_id"], kind, event["start_at"], end_at,
                     max(0, duration), event["event_id"], 1 if frozen else 0),
        )
        conn.execute("UPDATE work_log_events SET end_at=? WHERE event_id=?",
                     (end_at, event["event_id"]))
        return segment_id

    def _open_event(self, conn, *, event_id: str, driver_id: str, kind: str, start_at: str,
                    trip_id: str | None, recorded_by: str, payload: dict[str, Any],
                    idem_key: str) -> str:
        conn.execute(
            "INSERT INTO work_log_events(event_id,driver_id,trip_id,kind,start_at,end_at,recorded_at,"
            "recorded_by,idempotency_key,payload_json) VALUES(?,?,?,?,?,NULL,?,?,?,?)",
            (event_id, driver_id, trip_id, kind, start_at, self._now(), recorded_by,
             idem_key, canonical_json(payload)),
        )
        if trip_id:
            conn.execute("UPDATE trips SET status=? WHERE trip_id=? AND status=?",
                         (D.TRIP_IN_PROGRESS, trip_id, D.TRIP_DISPATCHED))
        return event_id

    def start_work(self, *, request_id: str, actor_id: str, driver_id: str, kind: str,
                   start_at: str | None = None, trip_id: str | None = None,
                   payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """打开一段驾驶/装卸/等待/休息/卧铺/救援记录。"""

        payload = payload or {}
        payload = {"actor_id": actor_id, "driver_id": driver_id, "kind": kind,
                        "start_at": start_at, "trip_id": trip_id, "payload": payload}
        with self.database.transaction(immediate=True) as conn:
            early = self._early_replay(conn, request_id=request_id,
                                          action="start_work", payload=payload)
            if early is not None:
                return early
            actor = self._principal(conn, actor_id)
            self._require_driver_access(actor, conn, driver_id)
            if kind not in INTERVAL_KINDS:
                raise ValidationError(f"未知工时类别 {kind}")
            start_at = self._ts(start_at, "start_at", default_now=True)
            self._assert_unfrozen(conn, driver_id, start_at)
            self._assert_no_overlap(conn, driver_id, start_at, None)
            if trip_id:
                trip = self._load_trip(conn, trip_id)
                if trip["status"] in (D.TRIP_CANCELLED, D.TRIP_COMPLETED):
                    raise ConflictError("任务已结束，不能再登记工时")
                if trip["driver_id"] not in (driver_id, None) and trip["co_driver_id"] != driver_id:
                    raise PermissionDenied("该任务不属于该司机")

            def create():
                event_id = uuid.uuid4().hex
                self._open_event(conn, event_id=event_id, driver_id=driver_id, kind=kind,
                                 start_at=start_at, trip_id=trip_id, recorded_by=actor_id,
                                 payload=payload, idem_key=request_id)
                self._audit(conn, actor_id=actor_id, action="work.started",
                            resource_type="work_event", resource_id=event_id,
                            detail={"driver_id": driver_id, "kind": kind, "trip_id": trip_id,
                                    "start_at": start_at})
                return "work_event", event_id, {"event_id": event_id, "kind": kind, "end_at": None}

            return self._idempotent(conn, request_id=request_id, action="start_work",
                                    payload=payload, create=create)

    def stop_work(self, *, request_id: str, actor_id: str, event_id: str,
                  end_at: str | None = None) -> dict[str, Any]:
        """关闭开放工时事件并派生不可变区段。"""

        payload = {"actor_id": actor_id, "event_id": event_id, "end_at": end_at}
        with self.database.transaction(immediate=True) as conn:
            early = self._early_replay(conn, request_id=request_id,
                                          action="stop_work", payload=payload)
            if early is not None:
                return early
            actor = self._principal(conn, actor_id)
            row = conn.execute("SELECT * FROM work_log_events WHERE event_id=?", (event_id,)).fetchone()
            if row is None:
                raise NotFoundError("工时事件不存在")
            event = dict(row)
            self._require_driver_access(actor, conn, event["driver_id"])
            end_at = self._ts(end_at, "end_at", default_now=True)
            self._assert_unfrozen(conn, event["driver_id"], event["start_at"])
            if event["end_at"] is not None:
                raise ConflictError("工时事件已关闭")

            def create():
                segment_id = self._close_event(conn, event, end_at)
                self._audit(conn, actor_id=actor_id, action="work.stopped",
                            resource_type="work_segment", resource_id=segment_id,
                            detail={"event_id": event_id, "driver_id": event["driver_id"],
                                    "kind": event["kind"], "start_at": event["start_at"], "end_at": end_at})
                return "work_segment", segment_id, {"segment_id": segment_id, "event_id": event_id}

            return self._idempotent(conn, request_id=request_id, action="stop_work",
                                    payload=payload, create=create)

    def log_interval(self, *, request_id: str, actor_id: str, driver_id: str, kind: str,
                     start_at: str, end_at: str, trip_id: str | None = None,
                     payload: dict[str, Any] | None = None,
                     idempotency_key: str | None = None) -> dict[str, Any]:
        """原子记录一段完整区间（驾驶/装卸/等待/休息/卧铺/救援/补录）。

        重放同一 request_id 直接返回原回执，不会重复计费。
        补录事件只能落在尚未冻结的区段。
        """

        payload = dict(payload or {})
        actual_kind = kind
        if kind == "backfill":
            actual_kind = payload.get("target_kind", "")
            if actual_kind not in INTERVAL_KINDS:
                raise ValidationError("补录必须通过 payload.target_kind 指定工时类别")
        elif kind not in INTERVAL_KINDS:
            raise ValidationError(f"未知工时类别 {kind}")
        payload = {"actor_id": actor_id, "driver_id": driver_id, "kind": kind,
                        "start_at": start_at, "end_at": end_at, "trip_id": trip_id,
                        "payload": payload, "idempotency_key": idempotency_key}
        with self.database.transaction(immediate=True) as conn:
            early = self._early_replay(conn, request_id=request_id,
                                          action="log_interval", payload=payload)
            if early is not None:
                return early
            actor = self._principal(conn, actor_id)
            self._require_driver_access(actor, conn, driver_id)
            start_at = self._ts(start_at, "start_at")
            end_at = self._ts(end_at, "end_at")
            if end_at <= start_at:
                raise ValidationError("结束时间必须晚于开始时间")
            self._assert_unfrozen(conn, driver_id, start_at)
            self._assert_no_overlap(conn, driver_id, start_at, end_at)
            if trip_id:
                trip = self._load_trip(conn, trip_id)
                if trip["status"] in (D.TRIP_CANCELLED,):
                    raise ConflictError("任务已取消，不能补录工时")
                if trip["driver_id"] not in (driver_id, None) and trip["co_driver_id"] != driver_id:
                    raise PermissionDenied("该任务不属于该司机")

            def create():
                event_id = uuid.uuid4().hex
                key = idempotency_key or request_id
                conn.execute(
                    "INSERT INTO work_log_events(event_id,driver_id,trip_id,kind,start_at,end_at,recorded_at,"
                    "recorded_by,idempotency_key,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (event_id, driver_id, trip_id, kind, start_at, end_at, self._now(),
                     actor_id, key, canonical_json(payload)),
                )
                segment_id = uuid.uuid4().hex
                duration = int(round((parse_ts(end_at) - parse_ts(start_at)).total_seconds() / 60))
                conn.execute(
                    "INSERT INTO work_segments(segment_id,driver_id,trip_id,kind,start_at,end_at,"
                    "duration_min,source_event_id,frozen) VALUES(?,?,?,?,?,?,?,?,0)",
                    (segment_id, driver_id, trip_id, actual_kind, start_at, end_at,
                     max(0, duration), event_id),
                )
                if trip_id:
                    conn.execute("UPDATE trips SET status=? WHERE trip_id=? AND status=?",
                                 (D.TRIP_IN_PROGRESS, trip_id, D.TRIP_DISPATCHED))
                self._audit(conn, actor_id=actor_id,
                            action="work.backfilled" if kind == "backfill" else "work.recorded",
                            resource_type="work_segment", resource_id=segment_id,
                            detail={"event_id": event_id, "driver_id": driver_id, "kind": actual_kind,
                                    "logged_kind": kind, "trip_id": trip_id,
                                    "start_at": start_at, "end_at": end_at})
                return "work_segment", segment_id, {"segment_id": segment_id, "event_id": event_id,
                                                    "kind": actual_kind}

            return self._idempotent(conn, request_id=request_id, action="log_interval",
                                    payload=payload, create=create)

    def shift_change(self, *, request_id: str, actor_id: str, trip_id: str,
                     relieved_driver_id: str, relieving_driver_id: str,
                     at: str | None = None) -> dict[str, Any]:
        """双驾换班：结束交班司机未冻结的驾驶区段，为接班司机开启驾驶。

        只能影响尚未冻结的区段；任一方台账已冻结到换班时刻则拒绝。
        """

        payload = {"actor_id": actor_id, "trip_id": trip_id,
                        "relieved_driver_id": relieved_driver_id,
                        "relieving_driver_id": relieving_driver_id, "at": at}
        with self.database.transaction(immediate=True) as conn:
            early = self._early_replay(conn, request_id=request_id,
                                          action="shift_change", payload=payload)
            if early is not None:
                return early
            actor = self._principal(conn, actor_id)
            self._require(actor, D.ROLE_ADMIN, D.ROLE_CARRIER, D.ROLE_DRIVER)
            at = self._ts(at, "at", default_now=True)
            trip = self._load_trip(conn, trip_id)
            if actor.role == D.ROLE_CARRIER:
                self._require_carrier_scope(actor, trip["carrier_id"])
            elif actor.role == D.ROLE_DRIVER and actor.driver_id not in (
                    relieved_driver_id, relieving_driver_id):
                raise PermissionDenied("只能在本人参与的双驾任务中换班")
            if trip["status"] not in (D.TRIP_DISPATCHED, D.TRIP_IN_PROGRESS):
                raise ConflictError("任务未在执行中，不能换班")
            crew = {trip["driver_id"], trip["co_driver_id"]}
            if relieved_driver_id not in crew or relieving_driver_id not in crew:
                raise ValidationError("换班司机必须是任务主副驾")
            self._assert_unfrozen(conn, relieved_driver_id, at)
            self._assert_unfrozen(conn, relieving_driver_id, at)
            open_row = conn.execute(
                "SELECT * FROM work_log_events WHERE driver_id=? AND trip_id=? AND end_at IS NULL "
                "AND kind='driving' AND superseded_by IS NULL ORDER BY start_at DESC LIMIT 1",
                (relieved_driver_id, trip_id)).fetchone()
            relieving_open = conn.execute(
                "SELECT * FROM work_log_events WHERE driver_id=? AND end_at IS NULL "
                "AND superseded_by IS NULL ORDER BY start_at DESC",
                (relieving_driver_id,)).fetchall()
            blocking = [dict(r) for r in relieving_open if dict(r)["kind"] not in D.REST_KINDS]
            if blocking:
                raise ConflictError("接班司机存在未关闭的工作区段，不能换班")

            def create():
                relieved_event_id = None
                relieved_segment_id = None
                if open_row is not None:
                    event = dict(open_row)
                    relieved_event_id = event["event_id"]
                    relieved_segment_id = self._close_event(conn, event, at)
                event_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO work_log_events(event_id,driver_id,trip_id,kind,start_at,end_at,"
                    "recorded_at,recorded_by,idempotency_key,payload_json) "
                    "VALUES(?,?,?,?,?,NULL,?,?,?,?)",
                    (event_id, relieving_driver_id, trip_id, "driving", at, self._now(),
                     actor_id, request_id,
                     canonical_json({"shift_change": True, "relieved_driver_id": relieved_driver_id})),
                )
                conn.execute(
                    "INSERT INTO work_log_events(event_id,driver_id,trip_id,kind,start_at,end_at,"
                    "recorded_at,recorded_by,idempotency_key,payload_json) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, relieving_driver_id, trip_id, "shift_change", at, at,
                     self._now(), actor_id, request_id + ":note",
                     canonical_json({"relieved_driver_id": relieved_driver_id,
                                     "relieving_driver_id": relieving_driver_id,
                                     "relieved_event_id": relieved_event_id})),
                )
                conn.execute("UPDATE trips SET status=? WHERE trip_id=?",
                             (D.TRIP_IN_PROGRESS, trip_id))
                self._audit(conn, actor_id=actor_id, action="shift.changed",
                            resource_type="trip", resource_id=trip_id,
                            detail={"relieved_driver_id": relieved_driver_id,
                                    "relieving_driver_id": relieving_driver_id,
                                    "relieved_event_id": relieved_event_id,
                                    "relieved_segment_id": relieved_segment_id, "at": at})
                return "shift_change", event_id, {
                    "new_event_id": event_id, "relieved_event_id": relieved_event_id,
                    "relieved_segment_id": relieved_segment_id, "at": at}

            return self._idempotent(conn, request_id=request_id, action="shift_change",
                                    payload=payload, create=create)

    def cancel_trip(self, *, request_id: str, actor_id: str, trip_id: str,
                    at: str | None = None, reason: str = "") -> dict[str, Any]:
        """取消任务：仅关闭未冻结的在途开放区段，已发生区段保留并仍可结算补贴。"""

        payload = {"actor_id": actor_id, "trip_id": trip_id, "at": at, "reason": reason}
        with self.database.transaction(immediate=True) as conn:
            early = self._early_replay(conn, request_id=request_id,
                                          action="cancel_trip", payload=payload)
            if early is not None:
                return early
            actor = self._principal(conn, actor_id)
            self._require(actor, D.ROLE_ADMIN, D.ROLE_CARRIER, D.ROLE_DRIVER)
            at = self._ts(at, "at", default_now=True)
            trip = self._load_trip(conn, trip_id)
            if actor.role == D.ROLE_CARRIER:
                self._require_carrier_scope(actor, trip["carrier_id"])
            elif actor.role == D.ROLE_DRIVER and actor.driver_id != trip["driver_id"]:
                raise PermissionDenied("只能取消本人任务")
            if trip["status"] in (D.TRIP_COMPLETED, D.TRIP_CANCELLED):
                raise ConflictError("任务已结束，不能取消")
            crew = [d for d in (trip["driver_id"], trip["co_driver_id"]) if d]
            for d in crew:
                self._assert_unfrozen(conn, d, at)

            def create():
                closed: list[str] = []
                for d in crew:
                    rows = conn.execute(
                        "SELECT * FROM work_log_events WHERE driver_id=? AND trip_id=? AND end_at IS NULL "
                        "AND superseded_by IS NULL ORDER BY start_at", (d, trip_id)).fetchall()
                    for row in rows:
                        event = dict(row)
                        if event["start_at"] >= at:
                            conn.execute("UPDATE work_log_events SET superseded_by=? WHERE event_id=?",
                                         ("cancelled", event["event_id"]))
                            continue
                        seg = self._close_event(conn, event, at)
                        closed.append(seg)
                conn.execute("UPDATE trips SET status=? WHERE trip_id=?", (D.TRIP_CANCELLED, trip_id))
                self._audit(conn, actor_id=actor_id, action="trip.cancelled",
                            resource_type="trip", resource_id=trip_id,
                            detail={"at": at, "reason": reason, "closed_segments": closed})
                return "trip", trip_id, {"trip_id": trip_id, "status": D.TRIP_CANCELLED,
                                         "closed_segments": closed}

            return self._idempotent(conn, request_id=request_id, action="cancel_trip",
                                    payload=payload, create=create)

    def complete_trip(self, *, request_id: str, actor_id: str, trip_id: str,
                      completed_at: str | None = None) -> dict[str, Any]:
        """完成任务：关闭所有未冻结的在途开放区段。"""

        payload = {"actor_id": actor_id, "trip_id": trip_id, "completed_at": completed_at}
        with self.database.transaction(immediate=True) as conn:
            early = self._early_replay(conn, request_id=request_id,
                                          action="complete_trip", payload=payload)
            if early is not None:
                return early
            actor = self._principal(conn, actor_id)
            self._require(actor, D.ROLE_ADMIN, D.ROLE_CARRIER, D.ROLE_DRIVER)
            completed_at = self._ts(completed_at, "completed_at", default_now=True)
            trip = self._load_trip(conn, trip_id)
            if actor.role == D.ROLE_CARRIER:
                self._require_carrier_scope(actor, trip["carrier_id"])
            elif actor.role == D.ROLE_DRIVER and actor.driver_id != trip["driver_id"]:
                raise PermissionDenied("只能完成本人任务")
            if trip["status"] == D.TRIP_CANCELLED:
                raise ConflictError("任务已取消")
            if trip["status"] == D.TRIP_COMPLETED:
                raise ConflictError("任务已完成")

            def create():
                closed = []
                for d in [x for x in (trip["driver_id"], trip["co_driver_id"]) if x]:
                    rows = conn.execute(
                        "SELECT * FROM work_log_events WHERE driver_id=? AND trip_id=? AND end_at IS NULL "
                        "AND superseded_by IS NULL ORDER BY start_at", (d, trip_id)).fetchall()
                    for row in rows:
                        closed.append(self._close_event(conn, dict(row), completed_at))
                conn.execute("UPDATE trips SET status=?, completed_at=? WHERE trip_id=?",
                             (D.TRIP_COMPLETED, completed_at, trip_id))
                self._audit(conn, actor_id=actor_id, action="trip.completed",
                            resource_type="trip", resource_id=trip_id,
                            detail={"completed_at": completed_at, "closed_segments": closed})
                return "trip", trip_id, {"trip_id": trip_id, "status": D.TRIP_COMPLETED,
                                         "closed_segments": closed}

            return self._idempotent(conn, request_id=request_id, action="complete_trip",
                                    payload=payload, create=create)

    def freeze_ledger(self, *, request_id: str, actor_id: str, driver_id: str,
                      frozen_through: str, reason: str) -> dict[str, Any]:
        """冻结台账：关闭截止时点的开放区段并锁定，之后事件只能作用于未冻结区段。"""

        payload = {"actor_id": actor_id, "driver_id": driver_id,
                        "frozen_through": frozen_through, "reason": reason}
        with self.database.transaction(immediate=True) as conn:
            early = self._early_replay(conn, request_id=request_id,
                                          action="freeze_ledger", payload=payload)
            if early is not None:
                return early
            actor = self._principal(conn, actor_id)
            self._require(actor, D.ROLE_ADMIN, D.ROLE_CARRIER, D.ROLE_REGULATOR)
            self._require_driver_access(actor, conn, driver_id)
            frozen_through = self._ts(frozen_through, "frozen_through")
            self._text(reason, "reason", 200)
            current = self._latest_freeze(conn, driver_id)
            if current is not None and frozen_through <= current:
                raise ConflictError("新的冻结点必须晚于既有冻结点")

            def create():
                closed: list[str] = []
                rows = conn.execute(
                    "SELECT * FROM work_log_events WHERE driver_id=? AND end_at IS NULL "
                    "AND start_at<=? AND superseded_by IS NULL ORDER BY start_at",
                    (driver_id, frozen_through)).fetchall()
                for row in rows:
                    closed.append(self._close_event(conn, dict(row), frozen_through, frozen=True))
                conn.execute("UPDATE work_segments SET frozen=1 WHERE driver_id=? AND frozen=0 AND end_at<=?",
                             (driver_id, frozen_through))
                conn.execute(
                    "INSERT INTO ledger_freezes(driver_id,frozen_through,reason,created_at) "
                    "VALUES(?,?,?,?)", (driver_id, frozen_through, reason, self._now()))
                self._audit(conn, actor_id=actor_id, action="ledger.frozen",
                            resource_type="driver", resource_id=driver_id,
                            detail={"frozen_through": frozen_through, "reason": reason,
                                    "closed_segments": closed})
                return ("ledger_freeze", f"{driver_id}:{frozen_through}",
                        {"driver_id": driver_id, "frozen_through": frozen_through,
                         "closed_segments": closed})

            return self._idempotent(conn, request_id=request_id, action="freeze_ledger",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------
    # 证据、账期与结算
    # ------------------------------------------------------------------

    def register_evidence(self, *, request_id: str, actor_id: str, trip_id: str, kind: str,
                          payload: dict[str, Any], occurred_at: str,
                          received_at: str | None = None) -> dict[str, Any]:
        """登记异常记录证据（含到达时间）。逾期证据可登记但不得据此扣款。"""

        payload = {"actor_id": actor_id, "trip_id": trip_id, "kind": kind,
                        "payload": payload, "occurred_at": occurred_at, "received_at": received_at}
        with self.database.transaction(immediate=True) as conn:
            early = self._early_replay(conn, request_id=request_id,
                                          action="register_evidence", payload=payload)
            if early is not None:
                return early
            actor = self._principal(conn, actor_id)
            self._require(actor, D.ROLE_ADMIN, D.ROLE_CARRIER, D.ROLE_REGULATOR)
            trip = self._load_trip(conn, trip_id)
            if actor.role == D.ROLE_CARRIER:
                self._require_carrier_scope(actor, trip["carrier_id"])
            kind = self._text(kind, "kind", 80)
            if not isinstance(payload, dict) or not payload:
                raise ValidationError("payload 必须是非空对象")
            occurred_at = self._ts(occurred_at, "occurred_at")
            received_at = self._ts(received_at, "received_at", default_now=True)
            if received_at < occurred_at:
                raise ValidationError("证据到达时间不能早于异常发生时间")

            def create():
                evidence_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO deduction_evidence(evidence_id,carrier_id,trip_id,kind,payload_json,"
                    "occurred_at,received_at,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (evidence_id, trip["carrier_id"], trip_id, kind, canonical_json(payload),
                     occurred_at, received_at, actor_id, self._now()))
                snapshot = json.loads(trip["rule_snapshot_json"])
                timely = evidence_is_timely(
                    {"received_at": received_at, "trip_completed_at": trip["completed_at"]},
                    snapshot["settlement"]) if trip["completed_at"] else False
                self._audit(conn, actor_id=actor_id, action="evidence.registered",
                            resource_type="evidence", resource_id=evidence_id,
                            detail={"trip_id": trip_id, "kind": kind, "occurred_at": occurred_at,
                                    "received_at": received_at, "timely": timely,
                                    "payload_hash": digest(payload)})
                return "evidence", evidence_id, {"evidence_id": evidence_id, "timely": timely}

            return self._idempotent(conn, request_id=request_id, action="register_evidence",
                                    payload=payload, create=create)

    def create_period(self, *, request_id: str, actor_id: str, period_id: str, carrier_id: str,
                      period_start: str, period_end: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "period_id": period_id, "carrier_id": carrier_id,
                        "period_start": period_start, "period_end": period_end}
        with self.database.transaction(immediate=True) as conn:
            early = self._early_replay(conn, request_id=request_id,
                                          action="create_period", payload=payload)
            if early is not None:
                return early
            actor = self._principal(conn, actor_id)
            self._require(actor, D.ROLE_ADMIN, D.ROLE_CARRIER)
            self._require_carrier_scope(actor, carrier_id)
            period_id = self._id(period_id, "period_id")
            period_start = self._ts(period_start, "period_start")
            period_end = self._ts(period_end, "period_end")
            if period_end <= period_start:
                raise ValidationError("账期结束必须晚于开始")
            ruleset = self._active_ruleset(conn, period_end)

            def create():
                try:
                    conn.execute(
                        "INSERT INTO ledger_periods(period_id,carrier_id,period_start,period_end,status,"
                        "ruleset_id,ruleset_version,closed_at) VALUES(?,?,?,?,?,?,?,NULL)",
                        (period_id, carrier_id, period_start, period_end, D.PERIOD_OPEN,
                         ruleset.ruleset_id, ruleset.version))
                except Exception as exc:
                    raise ConflictError("账期编号已存在") from exc
                self._audit(conn, actor_id=actor_id, action="period.created",
                            resource_type="period", resource_id=period_id,
                            detail={"carrier_id": carrier_id, "period_start": period_start,
                                    "period_end": period_end,
                                    "ruleset": f"{ruleset.ruleset_id}:v{ruleset.version}"})
                return "period", period_id, {"period_id": period_id,
                                             "ruleset": f"{ruleset.ruleset_id}:v{ruleset.version}"}

            return self._idempotent(conn, request_id=request_id, action="create_period",
                                    payload=payload, create=create)

    def _period_trips(self, conn, period: dict[str, Any]) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT * FROM trips WHERE carrier_id=? AND ("
            " (status=? AND completed_at>=? AND completed_at<?) OR "
            " (status=? AND created_at>=? AND created_at<?))",
            (period["carrier_id"], D.TRIP_COMPLETED, period["period_start"], period["period_end"],
             D.TRIP_CANCELLED, period["period_start"], period["period_end"])).fetchall()
        return [dict(r) for r in rows]

    def _period_segments(self, conn, trips: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not trips:
            return []
        ids = tuple(t["trip_id"] for t in trips)
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"SELECT driver_id,trip_id,kind,start_at,end_at,source_event_id FROM work_segments "
            f"WHERE trip_id IN ({placeholders})", ids).fetchall()
        return [dict(r) for r in rows]

    def settle_period(self, *, request_id: str, actor_id: str, period_id: str) -> dict[str, Any]:
        """按账期规则结算全部已完成/取消任务；按锚点去重，可重复调用增量入账。"""

        payload = {"actor_id": actor_id, "period_id": period_id}
        with self.database.transaction(immediate=True) as conn:
            early = self._early_replay(conn, request_id=request_id,
                                          action="settle_period", payload=payload)
            if early is not None:
                return early
            actor = self._principal(conn, actor_id)
            self._require(actor, D.ROLE_ADMIN, D.ROLE_CARRIER)
            prow = conn.execute("SELECT * FROM ledger_periods WHERE period_id=?", (period_id,)).fetchone()
            if prow is None:
                raise NotFoundError("账期不存在")
            period = dict(prow)
            if actor.role == D.ROLE_CARRIER:
                self._require_carrier_scope(actor, period["carrier_id"])
            if period["status"] == D.PERIOD_CLOSED:
                raise ConflictError("账期已关账，不能再写入明细；如需调整请使用复算")
            ruleset = self._load_ruleset(conn, period["ruleset_id"], period["ruleset_version"])

            def create():
                trips = self._period_trips(conn, period)
                segments = self._period_segments(conn, trips)
                added: list[str] = []
                for trip in trips:
                    crew = {d for d in (trip.get("driver_id"), trip.get("co_driver_id")) if d}
                    for crew_driver in sorted(crew):
                        lines = compute_trip_earnings(
                            trip, segments, ruleset.settlement, driver_id=crew_driver,
                            include_freight=(crew_driver == trip.get("driver_id")))
                        for line in lines:
                            if conn.execute("SELECT 1 FROM settlement_lines WHERE period_id=? AND anchor=?",
                                            (period_id, line["anchor"])).fetchone():
                                continue
                            line_id = uuid.uuid4().hex
                            conn.execute(
                                "INSERT INTO settlement_lines(line_id,period_id,trip_id,driver_id,kind,category,"
                                "amount,evidence_id,status,anchor,rule_ref,memo,created_at) "
                                "VALUES(?,?,?,?,?,?,?,NULL,?,?,?,?,?)",
                                (line_id, period_id, trip["trip_id"], crew_driver, line["kind"],
                                 line["category"], line["amount"], D.LINE_PAYABLE, line["anchor"],
                                 line["rule_ref"], line["memo"], self._now()))
                            added.append(line_id)
                self._audit(conn, actor_id=actor_id, action="period.settled",
                            resource_type="period", resource_id=period_id,
                            detail={"trips": len(trips), "lines_added": len(added)})
                return "period", period_id, {"period_id": period_id, "lines_added": len(added),
                                             "trips": len(trips)}

            return self._idempotent(conn, request_id=request_id, action="settle_period",
                                    payload=payload, create=create)

    def add_deduction(self, *, request_id: str, actor_id: str, period_id: str, trip_id: str,
                      driver_id: str, amount: int, category: str, memo: str,
                      evidence_id: str, evidence_kind: str | None = None,
                      evidence_payload: dict[str, Any] | None = None,
                      evidence_occurred_at: str | None = None,
                      evidence_received_at: str | None = None) -> dict[str, Any]:
        """依据证据登记扣款。证据逾期、账期已关账或申诉进行中均拒绝扣款。"""

        payload = {"actor_id": actor_id, "period_id": period_id, "trip_id": trip_id,
                        "driver_id": driver_id, "amount": amount, "category": category,
                        "evidence_id": evidence_id}
        with self.database.transaction(immediate=True) as conn:
            early = self._early_replay(conn, request_id=request_id,
                                          action="add_deduction", payload=payload)
            if early is not None:
                return early
            actor = self._principal(conn, actor_id)
            self._require(actor, D.ROLE_ADMIN, D.ROLE_CARRIER)
            prow = conn.execute("SELECT * FROM ledger_periods WHERE period_id=?", (period_id,)).fetchone()
            if prow is None:
                raise NotFoundError("账期不存在")
            period = dict(prow)
            if actor.role == D.ROLE_CARRIER:
                self._require_carrier_scope(actor, period["carrier_id"])
            trip = self._load_trip(conn, trip_id)
            if trip["carrier_id"] != period["carrier_id"]:
                raise ValidationError("任务与账期不属于同一承运人")
            if period["status"] == D.PERIOD_CLOSED:
                raise ConflictError("账期已关账，不能新增扣款；请在关账前处理或走复算/申诉")
            amount = self._amount(amount, "amount")
            category = self._text(category, "category", 80)
            memo = self._text(memo, "memo")

            erow = conn.execute("SELECT * FROM deduction_evidence WHERE evidence_id=?",
                                (evidence_id,)).fetchone()
            if erow is None:
                raise NotFoundError("扣款必须先登记证据（register_evidence）")
            evidence = dict(erow)
            if evidence["trip_id"] != trip_id:
                raise ValidationError("证据与任务不匹配")
            ruleset = self._load_ruleset(conn, period["ruleset_id"], period["ruleset_version"])
            if not evidence_is_timely(
                    {"received_at": evidence["received_at"],
                     "trip_completed_at": trip["completed_at"]}, ruleset.settlement):
                raise ConflictError(
                    f"异常证据于 {evidence['received_at']} 才到达，超过任务完成后 "
                    f"{ruleset.settlement['max_deduction_delay_days']} 天的追扣时限，不得扣款")
            open_appeal = conn.execute(
                "SELECT 1 FROM appeals WHERE trip_id=? AND driver_id=? AND status=?",
                (trip_id, driver_id, D.APPEAL_OPEN)).fetchone()
            if open_appeal:
                raise ConflictError("该任务存在进行中的申诉，申诉期间不得继续扣款")
            anchor = f"deduction:{evidence_id}:{driver_id}"
            dup = conn.execute("SELECT 1 FROM settlement_lines WHERE anchor=?",
                               (anchor,)).fetchone()
            if dup:
                raise ConflictError("该证据对该司机的扣款已存在，不能重复扣款")

            def create():
                line_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO settlement_lines(line_id,period_id,trip_id,driver_id,kind,category,"
                    "amount,evidence_id,status,anchor,rule_ref,memo,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (line_id, period_id, trip_id, driver_id, D.LINE_DEDUCTION, category,
                     -amount, evidence_id, D.LINE_PAYABLE, anchor,
                     "settlement.max_deduction_delay_days", memo, self._now()))
                self._audit(conn, actor_id=actor_id, action="deduction.added",
                            resource_type="settlement_line", resource_id=line_id,
                            detail={"period_id": period_id, "trip_id": trip_id,
                                    "driver_id": driver_id, "amount": -amount,
                                    "evidence_id": evidence_id,
                                    "evidence_received_at": evidence["received_at"]})
                return "settlement_line", line_id, {"line_id": line_id, "amount": -amount,
                                                    "anchor": anchor}

            return self._idempotent(conn, request_id=request_id, action="add_deduction",
                                    payload=payload, create=create)

    def close_period(self, *, request_id: str, actor_id: str, period_id: str) -> dict[str, Any]:
        """关账：冻结本账期明细，之后只能复算不能改写；争议金额继续留在托管账户。"""

        payload = {"actor_id": actor_id, "period_id": period_id}
        with self.database.transaction(immediate=True) as conn:
            early = self._early_replay(conn, request_id=request_id,
                                          action="close_period", payload=payload)
            if early is not None:
                return early
            actor = self._principal(conn, actor_id)
            self._require(actor, D.ROLE_ADMIN, D.ROLE_CARRIER)
            prow = conn.execute("SELECT * FROM ledger_periods WHERE period_id=?", (period_id,)).fetchone()
            if prow is None:
                raise NotFoundError("账期不存在")
            period = dict(prow)
            if actor.role == D.ROLE_CARRIER:
                self._require_carrier_scope(actor, period["carrier_id"])
            if period["status"] == D.PERIOD_CLOSED:
                raise ConflictError("账期已关账")

            def create():
                conn.execute("UPDATE ledger_periods SET status=?, closed_at=? WHERE period_id=?",
                             (D.PERIOD_CLOSED, self._now(), period_id))
                self._audit(conn, actor_id=actor_id, action="period.closed",
                            resource_type="period", resource_id=period_id,
                            detail={"closed_at": self._now()})
                return "period", period_id, {"period_id": period_id, "status": D.PERIOD_CLOSED}

            return self._idempotent(conn, request_id=request_id, action="close_period",
                                    payload=payload, create=create)

    def recompute_closed_period(self, *, request_id: str, actor_id: str, period_id: str,
                                ruleset_id: str | None = None,
                                ruleset_version: int | None = None) -> dict[str, Any]:
        """用新规则复算已关账账期，只生成对比记录，不改动原始明细。"""

        payload = {"actor_id": actor_id, "period_id": period_id,
                        "ruleset_id": ruleset_id, "ruleset_version": ruleset_version}
        with self.database.transaction(immediate=True) as conn:
            early = self._early_replay(conn, request_id=request_id,
                                          action="recompute_period", payload=payload)
            if early is not None:
                return early
            actor = self._principal(conn, actor_id)
            self._require(actor, D.ROLE_ADMIN, D.ROLE_CARRIER, D.ROLE_REGULATOR)
            prow = conn.execute("SELECT * FROM ledger_periods WHERE period_id=?", (period_id,)).fetchone()
            if prow is None:
                raise NotFoundError("账期不存在")
            period = dict(prow)
            if actor.role == D.ROLE_CARRIER:
                self._require_carrier_scope(actor, period["carrier_id"])
            if period["status"] != D.PERIOD_CLOSED:
                raise ConflictError("只有已关账账期才能复算")
            if ruleset_id and ruleset_version:
                ruleset = self._load_ruleset(conn, ruleset_id, ruleset_version)
            else:
                ruleset = self._active_ruleset(conn, self._now())
            if (ruleset.ruleset_id, ruleset.version) == (period["ruleset_id"], period["ruleset_version"]):
                raise ConflictError("指定规则与关账时规则相同，没有可对比的规则调整")

            def create():
                trips = self._period_trips(conn, period)
                segments = self._period_segments(conn, trips)
                deductions: list[dict[str, Any]] = []
                for row in conn.execute(
                        "SELECT sl.*, de.received_at, t.completed_at AS trip_completed_at "
                        "FROM settlement_lines sl JOIN deduction_evidence de ON de.evidence_id=sl.evidence_id "
                        "JOIN trips t ON t.trip_id=sl.trip_id WHERE sl.period_id=? AND sl.kind=?",
                        (period_id, D.LINE_DEDUCTION)):
                    deductions.append({
                        "anchor": row["anchor"], "amount": row["amount"], "category": row["category"],
                        "memo": row["memo"], "evidence_id": row["evidence_id"],
                        "status": row["status"],
                        "evidence": {"received_at": row["received_at"],
                                     "trip_completed_at": row["trip_completed_at"]}})
                new_lines = recompute_period(trips=trips, segments=segments, deductions=deductions,
                                             rules=ruleset.settlement)
                recomputed_total = sum(l["amount"] for l in new_lines if l.get("status") != D.LINE_VOID)
                original_total = conn.execute(
                    "SELECT COALESCE(SUM(amount),0) AS total FROM settlement_lines "
                    "WHERE period_id=? AND status!=?", (period_id, D.LINE_VOID)).fetchone()["total"]
                delta = recomputed_total - original_total
                recomputation_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO recomputations(recomputation_id,period_id,ruleset_id,ruleset_version,"
                    "original_total,recomputed_total,delta,lines_json,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (recomputation_id, period_id, ruleset.ruleset_id, ruleset.version,
                     original_total, recomputed_total, delta, canonical_json(new_lines), self._now()))
                self._audit(conn, actor_id=actor_id, action="period.recomputed",
                            resource_type="recomputation", resource_id=recomputation_id,
                            detail={"period_id": period_id, "old_ruleset":
                                    f"{period['ruleset_id']}:v{period['ruleset_version']}",
                                    "new_ruleset": f"{ruleset.ruleset_id}:v{ruleset.version}",
                                    "original_total": original_total,
                                    "recomputed_total": recomputed_total, "delta": delta})
                return "recomputation", recomputation_id, {
                    "recomputation_id": recomputation_id, "period_id": period_id,
                    "old_ruleset": f"{period['ruleset_id']}:v{period['ruleset_version']}",
                    "new_ruleset": f"{ruleset.ruleset_id}:v{ruleset.version}",
                    "original_total": original_total, "recomputed_total": recomputed_total,
                    "delta": delta, "lines": new_lines}

            return self._idempotent(conn, request_id=request_id, action="recompute_period",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------
    # 申诉与争议金额托管
    # ------------------------------------------------------------------

    def file_appeal(self, *, request_id: str, actor_id: str, trip_id: str, reason: str,
                    line_id: str | None = None) -> dict[str, Any]:
        """司机在申诉时限内提出申诉；争议金额单独托管，无争议收入不受阻断。"""

        payload = {"actor_id": actor_id, "trip_id": trip_id, "reason": reason,
                        "line_id": line_id}
        with self.database.transaction(immediate=True) as conn:
            early = self._early_replay(conn, request_id=request_id,
                                          action="file_appeal", payload=payload)
            if early is not None:
                return early
            actor = self._principal(conn, actor_id)
            self._require(actor, D.ROLE_DRIVER)
            trip = self._load_trip(conn, trip_id)
            if trip["driver_id"] != actor.driver_id and trip["co_driver_id"] != actor.driver_id:
                raise PermissionDenied("只能对本人任务提出申诉")
            reason = self._text(reason, "reason", 1000)
            snapshot = json.loads(trip["rule_snapshot_json"])
            appeal_rules = snapshot["appeal"]
            line = None
            reference_time = trip["completed_at"] or trip["created_at"]
            if line_id:
                lrow = conn.execute("SELECT * FROM settlement_lines WHERE line_id=?",
                                    (line_id,)).fetchone()
                if lrow is None:
                    raise NotFoundError("结算明细不存在")
                line = dict(lrow)
                if line["trip_id"] != trip_id or line["driver_id"] != actor.driver_id:
                    raise ValidationError("明细与任务或申诉司机不匹配")
                if line["status"] == D.LINE_VOID:
                    raise ConflictError("该扣款已作废，无需申诉")
                prow = conn.execute("SELECT * FROM ledger_periods WHERE period_id=?",
                                    (line["period_id"],)).fetchone()
                period = dict(prow)
                period_ruleset = self._load_ruleset(conn, period["ruleset_id"], period["ruleset_version"])
                appeal_rules = period_ruleset.appeal
                reference_time = line["created_at"]
            deadline = add_minutes(reference_time,
                                   int(appeal_rules["appeal_window_days"]) * 24 * 60)
            if self._now() > deadline:
                raise ConflictError(f"申诉时限已于 {deadline} 届满")

            def create():
                appeal_id = uuid.uuid4().hex
                amount_held = 0
                evidence_id = line["evidence_id"] if line else None
                if line and line["kind"] == D.LINE_DEDUCTION and line["status"] != D.LINE_HELD:
                    amount_held = -line["amount"]
                    conn.execute("UPDATE settlement_lines SET status=? WHERE line_id=?",
                                 (D.LINE_HELD, line_id))
                conn.execute(
                    "INSERT INTO appeals(appeal_id,trip_id,driver_id,carrier_id,line_id,reason,status,"
                    "evidence_id,amount_held,deadline_at,created_at,resolved_at,resolution_note) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,NULL)",
                    (appeal_id, trip_id, actor.driver_id, trip["carrier_id"], line_id, reason,
                     D.APPEAL_OPEN, evidence_id, amount_held, deadline, self._now()))
                if amount_held:
                    conn.execute(
                        "INSERT INTO escrow_entries(escrow_id,appeal_id,line_id,amount,status,"
                        "released_to_driver,returned_carrier,created_at) VALUES(?,?,?,?,?,0,0,?)",
                        (uuid.uuid4().hex, appeal_id, line_id, amount_held, "held", self._now()))
                self._audit(conn, actor_id=actor_id, action="appeal.filed",
                            resource_type="appeal", resource_id=appeal_id,
                            detail={"trip_id": trip_id, "line_id": line_id,
                                    "amount_held": amount_held, "deadline_at": deadline})
                return "appeal", appeal_id, {"appeal_id": appeal_id, "amount_held": amount_held,
                                             "deadline_at": deadline}

            return self._idempotent(conn, request_id=request_id, action="file_appeal",
                                    payload=payload, create=create)

    def resolve_appeal(self, *, request_id: str, actor_id: str, appeal_id: str,
                       decision: str, note: str) -> dict[str, Any]:
        """监管/管理员裁决：成立则托管金额退还司机，不成立则退还承运人。"""

        payload = {"actor_id": actor_id, "appeal_id": appeal_id,
                        "decision": decision, "note": note}
        with self.database.transaction(immediate=True) as conn:
            early = self._early_replay(conn, request_id=request_id,
                                          action="resolve_appeal", payload=payload)
            if early is not None:
                return early
            actor = self._principal(conn, actor_id)
            self._require(actor, D.ROLE_ADMIN, D.ROLE_REGULATOR)
            arow = conn.execute("SELECT * FROM appeals WHERE appeal_id=?", (appeal_id,)).fetchone()
            if arow is None:
                raise NotFoundError("申诉不存在")
            appeal = dict(arow)
            if appeal["status"] != D.APPEAL_OPEN:
                raise ConflictError("申诉已裁决")
            if decision not in (D.APPEAL_UPHELD, D.APPEAL_REJECTED):
                raise ValidationError("decision 必须是 upheld 或 rejected")
            note = self._text(note, "note", 1000)

            def create():
                new_status = D.APPEAL_UPHELD if decision == "upheld" else D.APPEAL_REJECTED
                conn.execute(
                    "UPDATE appeals SET status=?,resolved_at=?,resolution_note=? WHERE appeal_id=?",
                    (new_status, self._now(), note, appeal_id))
                escrow_row = conn.execute("SELECT * FROM escrow_entries WHERE appeal_id=?",
                                          (appeal_id,)).fetchone()
                payout: dict[str, int] = {"released_to_driver": 0, "returned_carrier": 0}
                if escrow_row is not None:
                    if decision == "upheld":
                        conn.execute(
                            "UPDATE escrow_entries SET status=?,released_to_driver=? WHERE escrow_id=?",
                            ("released_driver", escrow_row["amount"], escrow_row["escrow_id"]))
                        if appeal["line_id"]:
                            conn.execute("UPDATE settlement_lines SET status=? WHERE line_id=?",
                                         (D.LINE_VOID, appeal["line_id"]))
                        payout["released_to_driver"] = escrow_row["amount"]
                    else:
                        conn.execute(
                            "UPDATE escrow_entries SET status=?,returned_carrier=? WHERE escrow_id=?",
                            ("released_carrier", escrow_row["amount"], escrow_row["escrow_id"]))
                        if appeal["line_id"]:
                            conn.execute("UPDATE settlement_lines SET status=? WHERE line_id=?",
                                         (D.LINE_PAYABLE, appeal["line_id"]))
                        payout["returned_carrier"] = escrow_row["amount"]
                self._audit(conn, actor_id=actor_id,
                            action="appeal.upheld" if decision == "upheld" else "appeal.rejected",
                            resource_type="appeal", resource_id=appeal_id,
                            detail={"decision": decision, "note": note, **payout})
                return "appeal", appeal_id, {"appeal_id": appeal_id, "decision": decision, **payout}

            return self._idempotent(conn, request_id=request_id, action="resolve_appeal",
                                    payload=payload, create=create)

    # ------------------------------------------------------------------
    # 查询与三类角色视图
    # ------------------------------------------------------------------

    def _trip_out(self, row) -> dict[str, Any]:
        d = dict(row)
        d["rule_snapshot"] = json.loads(d.pop("rule_snapshot_json"))
        return d

    def _line_out(self, row) -> dict[str, Any]:
        return dict(row)

    def get_trip(self, *, actor_id: str, trip_id: str) -> dict[str, Any]:
        with self.database.transaction() as conn:
            actor = self._principal(conn, actor_id)
            trip = self._load_trip(conn, trip_id)
            if actor.role == D.ROLE_DRIVER and trip["driver_id"] != actor.driver_id \
                    and trip["co_driver_id"] != actor.driver_id:
                raise PermissionDenied("只能查看本人任务")
            if actor.role == D.ROLE_CARRIER:
                self._require_carrier_scope(actor, trip["carrier_id"])
            out = self._trip_out(trip)
            out["segments"] = [dict(r) for r in conn.execute(
                "SELECT segment_id,driver_id,trip_id,kind,start_at,end_at,duration_min,frozen "
                "FROM work_segments WHERE trip_id=? ORDER BY start_at", (trip_id,))]
            out["evidence"] = [dict(r) for r in conn.execute(
                "SELECT evidence_id,kind,occurred_at,received_at,created_by FROM deduction_evidence "
                "WHERE trip_id=? ORDER BY received_at", (trip_id,))]
            return out

    def list_available_trips(self, *, actor_id: str, at: str | None = None) -> list[dict[str, Any]]:
        """司机视角：本企业可接任务，并附本人原子可用性校验结果。"""

        with self.database.transaction() as conn:
            actor = self._principal(conn, actor_id)
            self._require(actor, D.ROLE_DRIVER, D.ROLE_CARRIER, D.ROLE_ADMIN, D.ROLE_REGULATOR)
            at = self._ts(at, "at", default_now=True)
            if actor.role == D.ROLE_DRIVER:
                org = self._driver_carrier(conn, actor.driver_id)
                rows = conn.execute(
                    "SELECT * FROM trips WHERE carrier_id=? AND status=? AND driver_id IS NULL ORDER BY "
                    "planned_pickup_at", (org, D.TRIP_OPEN)).fetchall()
                driver_id = actor.driver_id
            else:
                rows = conn.execute(
                    "SELECT * FROM trips WHERE status=? AND driver_id IS NULL ORDER BY planned_pickup_at",
                    (D.TRIP_OPEN,)).fetchall()
                driver_id = None
            result = []
            for row in rows:
                trip = dict(row)
                entry = {"trip_id": trip["trip_id"], "carrier_id": trip["carrier_id"],
                         "origin": trip["origin"], "destination": trip["destination"],
                         "planned_pickup_at": trip["planned_pickup_at"],
                         "estimated_driving_min": trip["estimated_driving_min"],
                         "freight_amount": trip["freight_amount"]}
                if driver_id:
                    snapshot = json.loads(trip["rule_snapshot_json"])
                    segs = self._segments_for_eval(conn, driver_id, at)
                    evaluation = evaluate_availability(
                        segs, at=at, limits=snapshot["limits"],
                        trip_driving_min=trip["estimated_driving_min"],
                        trip_work_min=trip["estimated_work_min"])
                    entry["availability"] = {k: evaluation[k] for k in
                                             ("eligible", "reasons", "window_start_at", "window_end_at",
                                              "continuous_driving_min", "driving_last_24h_min",
                                              "work_last_24h_min")}
                result.append(entry)
            return result

    def list_settlement_lines(self, *, actor_id: str, period_id: str | None = None,
                              trip_id: str | None = None, driver_id: str | None = None) -> dict[str, Any]:
        """结算明细：司机只看本人，承运人看本企业，监管看全部；附无争议/托管汇总。"""

        query = "SELECT sl.* FROM settlement_lines sl JOIN trips t ON t.trip_id=sl.trip_id WHERE 1=1"
        params: list[Any] = []
        with self.database.transaction() as conn:
            actor = self._principal(conn, actor_id)
            if period_id:
                query += " AND sl.period_id=?"
                params.append(period_id)
            if trip_id:
                query += " AND sl.trip_id=?"
                params.append(trip_id)
            if actor.role == D.ROLE_DRIVER:
                query += " AND sl.driver_id=?"
                params.append(actor.driver_id)
            elif actor.role == D.ROLE_CARRIER:
                query += " AND t.carrier_id=?"
                params.append(actor.carrier_id)
            elif driver_id:
                query += " AND sl.driver_id=?"
                params.append(driver_id)
            elif actor.role not in (D.ROLE_ADMIN, D.ROLE_REGULATOR):
                raise PermissionDenied("无权查看结算明细")
            query += " ORDER BY sl.created_at, sl.line_id"
            lines = [dict(r) for r in conn.execute(query, params)]
            payable = sum(l["amount"] for l in lines if l["status"] == D.LINE_PAYABLE)
            held = sum(-l["amount"] for l in lines if l["status"] == D.LINE_HELD)
            voided = sum(-l["amount"] for l in lines
                         if l["status"] == D.LINE_VOID and l["kind"] == D.LINE_DEDUCTION)
            income = sum(l["amount"] for l in lines
                         if l["kind"] != D.LINE_DEDUCTION and l["status"] == D.LINE_PAYABLE)
            return {"items": lines,
                    "payable_total": payable,
                    "uncontested_income": income,
                    "escrow_held_total": held,
                    "voided_deduction_total": voided}

    def list_appeals(self, *, actor_id: str, status: str | None = None,
                     carrier_id: str | None = None) -> list[dict[str, Any]]:
        """申诉进度：司机看本人，承运人看本企业，监管可按企业过滤。"""

        query = "SELECT * FROM appeals WHERE 1=1"
        params: list[Any] = []
        with self.database.transaction() as conn:
            actor = self._principal(conn, actor_id)
            if actor.role == D.ROLE_DRIVER:
                query += " AND driver_id=?"
                params.append(actor.driver_id)
            elif actor.role == D.ROLE_CARRIER:
                query += " AND carrier_id=?"
                params.append(actor.carrier_id)
            elif actor.role == D.ROLE_ADMIN:
                if carrier_id:
                    query += " AND carrier_id=?"
                    params.append(carrier_id)
            elif actor.role == D.ROLE_REGULATOR:
                if carrier_id:
                    query += " AND carrier_id=?"
                    params.append(carrier_id)
            else:
                raise PermissionDenied("无权查看申诉")
            if status:
                query += " AND status=?"
                params.append(status)
            query += " ORDER BY created_at"
            out = []
            for row in conn.execute(query, params):
                item = dict(row)
                er = conn.execute("SELECT amount,status,released_to_driver,returned_carrier "
                                  "FROM escrow_entries WHERE appeal_id=?",
                                  (item["appeal_id"],)).fetchone()
                item["escrow"] = dict(er) if er else None
                out.append(item)
            return out

    def overtime_responsibility(self, *, actor_id: str, carrier_id: str | None = None,
                                at: str | None = None) -> list[dict[str, Any]]:
        """超时责任：列在途超限司机，并结合派单留痕归属责任。

        实时部分只统计仍在执行任务的司机；历史部分回溯 at 之前最近一次
        “不合格派单”留痕（含当时强制派单），供监管在任务结束后还原责任。
        """

        with self.database.transaction() as conn:
            actor = self._principal(conn, actor_id)
            self._require(actor, D.ROLE_CARRIER, D.ROLE_REGULATOR, D.ROLE_ADMIN)
            at = self._ts(at, "at", default_now=True)
            if actor.role == D.ROLE_CARRIER:
                carrier_id = actor.carrier_id
            dquery = "SELECT * FROM drivers WHERE active=1"
            dparams: list[Any] = []
            if carrier_id:
                dquery += " AND organization_id=?"
                dparams.append(carrier_id)
            drivers = {r["driver_id"]: dict(r) for r in conn.execute(dquery, dparams)}
            ruleset = self._active_ruleset(conn, at)
            findings: list[dict[str, Any]] = []
            seen: set[tuple[str, str]] = set()

            def add_finding(driver_id: str, trip_id: str | None, point: str,
                            result: dict[str, Any] | None, forced: bool) -> None:
                key = (driver_id, trip_id or "-")
                if key in seen:
                    return
                seen.add(key)
                reasons = (result or {}).get("reasons", ["continuous_driving_limit_reached"])
                breaches = [r for r in reasons if "limit" in r] or [
                    "continuous_driving_limit_reached"]
                findings.append({
                    "driver_id": driver_id,
                    "carrier_id": drivers[driver_id]["organization_id"],
                    "at": point, "breach_reasons": breaches,
                    "continuous_driving_min": (result or {}).get("continuous_driving_min", 0),
                    "driving_last_24h_min": (result or {}).get("driving_last_24h_min", 0),
                    "work_last_24h_min": (result or {}).get("work_last_24h_min", 0),
                    "trip_id": trip_id,
                    "responsibility": "carrier_forced_dispatch" if forced else "under_investigation",
                    "forced_dispatch": forced,
                })

            # 1) 实时：仍在途的司机
            tquery = ("SELECT * FROM trips WHERE status IN (?,?)")
            tparams: list[Any] = [D.TRIP_IN_PROGRESS, D.TRIP_DISPATCHED]
            if carrier_id:
                tquery += " AND carrier_id=?"
                tparams.append(carrier_id)
            for trip in conn.execute(tquery, tparams):
                for driver_id in {trip["driver_id"], trip["co_driver_id"]} - {None}:
                    if driver_id not in drivers:
                        continue
                    segs = self._segments_for_eval(conn, driver_id, at)
                    result = evaluate_availability(segs, at=at, limits=ruleset.limits)
                    breaches = [r for r in result["reasons"] if "limit" in r]
                    if not breaches and result["continuous_driving_min"] < ruleset.limits[
                            "max_continuous_driving_min"]:
                        continue
                    decision = conn.execute(
                        "SELECT * FROM dispatch_decisions WHERE trip_id=? "
                        "AND (driver_id=? OR co_driver_id=?) AND checked_at<=? "
                        "ORDER BY checked_at DESC, rowid DESC LIMIT 1",
                        (trip["trip_id"], driver_id, driver_id, at)).fetchone()
                    forced = bool(decision and decision["forced"] == 1
                                  and decision["eligible"] == 0)
                    add_finding(driver_id, trip["trip_id"], at, result, forced)

            # 2) 历史：at 之前最近的不合格派单留痕（任务可能已结束）
            hquery = ("SELECT dd.* FROM dispatch_decisions dd JOIN drivers d ON "
                      "(d.driver_id=dd.driver_id OR d.driver_id=dd.co_driver_id) "
                      "WHERE dd.eligible=0 AND dd.checked_at<=?")
            hparams: list[Any] = [at]
            if carrier_id:
                hquery += " AND d.organization_id=?"
                hparams.append(carrier_id)
            for decision in conn.execute(hquery + " ORDER BY dd.checked_at DESC, dd.rowid DESC",
                                         hparams):
                # 每个司机只保留最近一条历史留痕
                if any(f["driver_id"] == decision["driver_id"] and f["trip_id"] is None
                       for f in findings):
                    continue
                already = any(
                    f["driver_id"] == decision["driver_id"] and f["trip_id"] == decision["trip_id"]
                    for f in findings)
                if already:
                    continue
                add_finding(decision["driver_id"], decision["trip_id"],
                            decision["checked_at"], None, decision["forced"] == 1)
            return findings

    def list_recomputations(self, *, actor_id: str, period_id: str | None = None) -> list[dict[str, Any]]:
        with self.database.transaction() as conn:
            actor = self._principal(conn, actor_id)
            self._require(actor, D.ROLE_ADMIN, D.ROLE_CARRIER, D.ROLE_REGULATOR)
            query = "SELECT * FROM recomputations WHERE 1=1"
            params: list[Any] = []
            if period_id:
                query += " AND period_id=?"
                params.append(period_id)
            if actor.role == D.ROLE_CARRIER:
                query += (" AND period_id IN (SELECT period_id FROM ledger_periods WHERE carrier_id=?)")
                params.append(actor.carrier_id)
            query += " ORDER BY created_at"
            return [dict(r) for r in conn.execute(query, params)]

    def list_segments(self, *, actor_id: str, driver_id: str) -> list[dict[str, Any]]:
        with self.database.transaction() as conn:
            actor = self._principal(conn, actor_id)
            self._require_driver_access(actor, conn, driver_id)
            return [dict(r) for r in conn.execute(
                "SELECT segment_id,driver_id,trip_id,kind,start_at,end_at,duration_min,frozen "
                "FROM work_segments WHERE driver_id=? ORDER BY start_at", (driver_id,))]

    def audit_events(self, after_sequence: int = 0) -> list[dict[str, Any]]:
        rows = self.database.connection.execute(
            "SELECT * FROM audit_events WHERE sequence>? ORDER BY sequence", (after_sequence,)).fetchall()
        return [{"sequence": r["sequence"], "event_id": r["event_id"], "actor_id": r["actor_id"],
                 "action": r["action"], "resource_type": r["resource_type"],
                 "resource_id": r["resource_id"], "detail": json.loads(r["detail_json"]),
                 "previous_hash": r["previous_hash"], "event_hash": r["event_hash"],
                 "occurred_at": r["occurred_at"]} for r in rows]

    def verify_audit(self) -> tuple[bool, int]:
        return verify_chain(self.database.connection)
