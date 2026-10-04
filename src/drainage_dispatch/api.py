"""标准库 HTTP JSON 接口，供值班系统与移动终端对接。"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .errors import DispatchError
from .service import DispatchService


class _Handler(BaseHTTPRequestHandler):
    server_version = "DrainageDispatch/1.0"

    def log_message(self, *args):  # 静默访问日志
        pass

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def _handle(self, method: str) -> None:
        try:
            body = {}
            if method == "POST":
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                    if not isinstance(body, dict):
                        raise DispatchError("BAD_REQUEST", "请求体必须是 JSON 对象", 400)
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            for route_method, pattern, fn in self.server.routes:
                if route_method != method:
                    continue
                match = pattern.fullmatch(path)
                if match:
                    return self._send(200, fn(match.groups(), body))
            self._send(404, {"error": {"code": "NOT_FOUND",
                                       "message": f"无此路由 {method} {path}"}})
        except DispatchError as exc:
            self._send(exc.status, exc.to_dict())
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            self._send(400, {"error": {"code": "BAD_REQUEST", "message": str(exc)}})
        except Exception as exc:  # 兜底，避免连接悬挂
            self._send(500, {"error": {"code": "INTERNAL", "message": str(exc)}})

    def _send(self, status: int, obj: dict) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def create_server(service: DispatchService, host: str = "127.0.0.1",
                  port: int = 8080) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), _Handler)
    httpd.routes = _build_routes(service)
    return httpd


def _build_routes(service: DispatchService):
    routes = []

    def add(method, pattern, fn):
        routes.append((method, re.compile(pattern), fn))

    add("GET", r"/health", lambda g, b: {"ok": True})
    add("POST", r"/storms", lambda g, b: service.declare_storm(
        b.get("storm_id"), b.get("basin_ids") or [],
        b.get("name", ""), b.get("rainfall_mm")))
    add("GET", r"/storms/([^/]+)", lambda g, b: service.storm_status(g[0]))
    add("POST", r"/devices", lambda g, b: service.register_device(
        b.get("device_id"), b.get("basin_id"), b.get("kind"),
        b.get("capacity_m3_min"), b.get("available", True)))
    add("GET", r"/devices", lambda g, b: {"devices": service.list_devices()})
    add("POST", r"/storms/([^/]+)/reports", lambda g, b: service.ingest_report(
        g[0], b.get("report_id"), b.get("kind"), b.get("basin_id"),
        b.get("observed_at"), b.get("payload") or {}))
    add("POST", r"/storms/([^/]+)/freeze", lambda g, b: service.freeze_snapshot(g[0]))
    add("POST", r"/storms/([^/]+)/plans", lambda g, b: service.generate_plan(g[0]))
    add("GET", r"/storms/([^/]+)/plans", lambda g, b: {"plans": service.list_plans(g[0])})
    add("GET", r"/storms/([^/]+)/queues", lambda g, b: service.queues(g[0]))
    add("GET", r"/storms/([^/]+)/replay", lambda g, b: service.replay(g[0]))
    add("GET", r"/storms/([^/]+)/recovery-order",
        lambda g, b: service.recovery_order(g[0]))
    add("GET", r"/queues", lambda g, b: service.queues())
    add("GET", r"/plans/([^/]+)", lambda g, b: service.plan_view(g[0]))
    add("POST", r"/plans/([^/]+)/dispatch",
        lambda g, b: service.dispatch_plan(g[0], b.get("by", "")))
    add("GET", r"/actions/([^/]+)", lambda g, b: service.action_view(g[0]))
    add("POST", r"/actions/([^/]+)/approve",
        lambda g, b: service.approve_action(g[0], b.get("by", "")))
    add("POST", r"/actions/([^/]+)/revoke", lambda g, b: service.revoke_action(
        g[0], b.get("by", ""), b.get("reason", "")))
    add("POST", r"/actions/([^/]+)/reassign", lambda g, b: service.reassign_action(
        g[0], b.get("device_id"), b.get("by", "")))
    add("POST", r"/actions/([^/]+)/dispatch",
        lambda g, b: service.dispatch_action(g[0], b.get("by", "")))
    add("POST", r"/receipts", lambda g, b: service.record_receipt(
        b.get("receipt_id"), b.get("action_id"), b.get("status"),
        b.get("by", ""), b.get("payload")))
    add("POST", r"/tick", lambda g, b: service.tick(
        seconds=b.get("seconds"), to=b.get("to")))
    return routes
