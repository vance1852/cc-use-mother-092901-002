"""司机履约与权益保障项目的 HTTP/JSON 边界。

/driver-rights 前缀下的接口由本包处理，其余路径回退到基础服务路由，
因此组织、操作者、审计等基础能力在同一进程内可用。
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from transport_coordination import api as base_api
from transport_coordination.errors import DomainError, ValidationError

from .service import DriverRightsService
from .storage import DriverRightsDatabase

PREFIX = "/driver-rights"


def _query(parsed, name: str) -> str | None:
    return parse_qs(parsed.query).get(name, [None])[0]


def route(service: DriverRightsService, method: str, path: str,
          body: dict[str, Any] | None, headers: dict[str, str] | None = None
          ) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到司机权益领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    if not parsed.path.startswith(PREFIX + "/") and parsed.path != PREFIX:
        return base_api.route(service, method, path, body, headers)
    resource = parsed.path[len(PREFIX):]
    try:
        if method == "POST" and resource == "/drivers":
            receipt = service.register_driver(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and resource == "/regulations":
            receipt = service.publish_regulation(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and resource == "/contracts":
            receipt = service.publish_contract(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and resource == "/tasks":
            receipt = service.create_task(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and resource == "/tasks/assign":
            receipt = service.assign_task(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and resource == "/tasks/accept":
            receipt = service.accept_task(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and resource == "/tasks/cancel":
            receipt = service.cancel_task(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and resource == "/tasks/complete":
            receipt = service.complete_task(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and resource == "/events":
            receipt = service.record_event(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and resource == "/evidence":
            receipt = service.submit_evidence(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and resource == "/deductions":
            receipt = service.propose_deduction(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and resource == "/deductions/cancel":
            receipt = service.cancel_deduction(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and resource == "/settlements/run":
            receipt = service.settle_task(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and resource == "/periods/close":
            receipt = service.close_period(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and resource == "/periods/recompute":
            receipt = service.recompute_period(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and resource == "/statements/pay":
            receipt = service.mark_statement_paid(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and resource == "/appeals":
            receipt = service.file_appeal(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and resource == "/appeals/resolve":
            receipt = service.resolve_appeal(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and resource == "/tasks/available":
            return 200, {"items": service.available_tasks(actor_id)}
        if method == "GET" and resource == "/tasks/detail":
            task_id = _query(parsed, "task_id")
            if not task_id:
                raise ValidationError("task_id 不能为空")
            return 200, service.task_detail(actor_id, task_id)
        if method == "GET" and resource == "/statements":
            return 200, {"items": service.list_statements(
                actor_id, driver_id=_query(parsed, "driver_id"),
                organization_id=_query(parsed, "organization_id"),
                month=_query(parsed, "month"))}
        if method == "GET" and resource == "/statements/detail":
            statement_id = _query(parsed, "statement_id")
            if not statement_id:
                raise ValidationError("statement_id 不能为空")
            return 200, service.statement_detail(actor_id, statement_id)
        if method == "GET" and resource == "/violations":
            return 200, {"items": service.list_violations(
                actor_id, organization_id=_query(parsed, "organization_id"))}
        if method == "GET" and resource == "/appeals":
            return 200, {"items": service.list_appeals(
                actor_id, organization_id=_query(parsed, "organization_id"),
                driver_id=_query(parsed, "driver_id"))}
        if method == "GET" and resource == "/recompute-reports":
            return 200, {"items": service.list_recompute_reports(
                actor_id, organization_id=_query(parsed, "organization_id"))}
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DriverRightsService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
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
    """启动司机履约与权益保障 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动司机履约与权益保障服务")
    parser.add_argument("--database", default="driver_rights.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()
    database = DriverRightsDatabase(args.database)
    Handler.service = DriverRightsService(database)
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
