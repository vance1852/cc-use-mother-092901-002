"""司机履约与权益保障服务的 HTTP/JSON 边界（仅依赖标准库）。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from transport_coordination.errors import DomainError

from .service import GuaranteeService
from .storage import Database


def route(service: GuaranteeService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    p = parsed.path.strip("/").split("/")
    query = parse_qs(parsed.query)
    actor_id = headers.get("X-Actor-Id", "")

    def q(name: str, default: str | None = None) -> str | None:
        return query.get(name, [default])[0]

    def receipt_status(receipt: dict[str, Any]) -> int:
        return 200 if receipt.get("replayed") else 201

    try:
        if method == "GET" and p == ["health"]:
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}

        # ---- 建档与版本 ----
        if method == "POST" and p == ["bootstrap"]:
            r = service.bootstrap_admin(**body)
            return receipt_status(r), r
        if method == "POST" and p == ["carriers"]:
            r = service.register_carrier(actor_id=actor_id, **body)
            return receipt_status(r), r
        if method == "POST" and p == ["drivers"]:
            r = service.register_driver(actor_id=actor_id, **body)
            return receipt_status(r), r
        if method == "POST" and p == ["principals"]:
            r = service.register_principal(actor_id=actor_id, **body)
            return receipt_status(r), r
        if method == "POST" and p == ["contracts"]:
            r = service.create_contract_version(actor_id=actor_id, **body)
            return receipt_status(r), r
        if method == "POST" and p == ["rulesets"]:
            r = service.create_ruleset(actor_id=actor_id, **body)
            return receipt_status(r), r

        # ---- 任务与派单 ----
        if method == "POST" and p == ["trips"]:
            r = service.create_trip(actor_id=actor_id, **body)
            return receipt_status(r), r
        if method == "POST" and p == ["dispatches"]:
            r = service.dispatch_trip(actor_id=actor_id, **body)
            return receipt_status(r), r
        if method == "GET" and p == ["availability"]:
            view = service.check_availability(actor_id=actor_id, driver_id=q("driver_id", ""),
                                              at=q("at"), trip_id=q("trip_id"),
                                              ruleset_id=q("ruleset_id"))
            return 200, view.__dict__
        if method == "GET" and p == ["trips", "available"]:
            return 200, {"items": service.list_available_trips(actor_id=actor_id, at=q("at"))}
        if method == "GET" and len(p) == 2 and p[0] == "trips":
            return 200, service.get_trip(actor_id=actor_id, trip_id=p[1])
        if method == "POST" and len(p) == 3 and p[0] == "trips" and p[2] == "cancel":
            r = service.cancel_trip(actor_id=actor_id, trip_id=p[1], **body)
            return receipt_status(r), r
        if method == "POST" and len(p) == 3 and p[0] == "trips" and p[2] == "complete":
            r = service.complete_trip(actor_id=actor_id, trip_id=p[1], **body)
            return receipt_status(r), r

        # ---- 工时台账 ----
        if method == "POST" and p == ["work", "start"]:
            r = service.start_work(actor_id=actor_id, **body)
            return receipt_status(r), r
        if method == "POST" and p == ["work", "stop"]:
            r = service.stop_work(actor_id=actor_id, **body)
            return receipt_status(r), r
        if method == "POST" and p == ["work", "intervals"]:
            r = service.log_interval(actor_id=actor_id, **body)
            return receipt_status(r), r
        if method == "POST" and p == ["shift-changes"]:
            r = service.shift_change(actor_id=actor_id, **body)
            return receipt_status(r), r
        if method == "POST" and p == ["freezes"]:
            r = service.freeze_ledger(actor_id=actor_id, **body)
            return receipt_status(r), r
        if method == "GET" and p == ["segments"]:
            return 200, {"items": service.list_segments(actor_id=actor_id,
                                                       driver_id=q("driver_id", ""))}

        # ---- 证据、账期、结算、扣款 ----
        if method == "POST" and p == ["evidence"]:
            r = service.register_evidence(actor_id=actor_id, **body)
            return receipt_status(r), r
        if method == "POST" and p == ["periods"]:
            r = service.create_period(actor_id=actor_id, **body)
            return receipt_status(r), r
        if method == "POST" and p == ["settlements"]:
            r = service.settle_period(actor_id=actor_id, **body)
            return receipt_status(r), r
        if method == "POST" and p == ["deductions"]:
            r = service.add_deduction(actor_id=actor_id, **body)
            return receipt_status(r), r
        if method == "POST" and len(p) == 3 and p[0] == "periods" and p[2] == "close":
            r = service.close_period(actor_id=actor_id, period_id=p[1], **body)
            return receipt_status(r), r
        if method == "POST" and p == ["recomputations"]:
            r = service.recompute_closed_period(actor_id=actor_id, **body)
            return receipt_status(r), r
        if method == "GET" and p == ["recomputations"]:
            return 200, {"items": service.list_recomputations(
                actor_id=actor_id, period_id=q("period_id"))}
        if method == "GET" and p == ["settlements"]:
            return 200, service.list_settlement_lines(
                actor_id=actor_id, period_id=q("period_id"), trip_id=q("trip_id"),
                driver_id=q("driver_id"))

        # ---- 申诉与托管 ----
        if method == "POST" and p == ["appeals"]:
            r = service.file_appeal(actor_id=actor_id, **body)
            return receipt_status(r), r
        if method == "POST" and len(p) == 3 and p[0] == "appeals" and p[2] == "resolve":
            r = service.resolve_appeal(actor_id=actor_id, appeal_id=p[1], **body)
            return receipt_status(r), r
        if method == "GET" and p == ["appeals"]:
            return 200, {"items": service.list_appeals(
                actor_id=actor_id, status=q("status"), carrier_id=q("carrier_id"))}

        # ---- 监管视图与审计 ----
        if method == "GET" and p == ["overtime-responsibility"]:
            return 200, {"items": service.overtime_responsibility(
                actor_id=actor_id, carrier_id=q("carrier_id"), at=q("at"))}
        if method == "GET" and p == ["audit-events"]:
            after = int(q("after_sequence", "0") or "0")
            return 200, {"items": service.audit_events(after)}

        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: GuaranteeService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动司机履约与权益保障服务")
    parser.add_argument("--database", default="driver_guarantee.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = GuaranteeService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
