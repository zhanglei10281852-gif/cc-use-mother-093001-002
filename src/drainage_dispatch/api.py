"""基于标准库 http.server 的 REST 接口。"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .errors import DispatchError
from .service import DispatchService


def make_server(service: DispatchService, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    routes = _build_routes(service)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # 静默访问日志
            pass

        def _handle(self, method: str) -> None:
            try:
                body = b""
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    body = self.rfile.read(length)
                payload = json.loads(body) if body else {}
                for pat, verb, fn in routes:
                    if verb != method:
                        continue
                    m = pat.fullmatch(self.path.split("?", 1)[0])
                    if m:
                        data = fn(payload, **m.groupdict())
                        return self._send(200, {"ok": True, "data": data})
                return self._send(404, {"ok": False, "error": {"type": "NotFound", "message": self.path}})
            except DispatchError as exc:
                return self._send(exc.status, {"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}})
            except (json.JSONDecodeError, ValueError, KeyError) as exc:
                return self._send(400, {"ok": False, "error": {"type": "BadRequest", "message": str(exc)}})

        def _send(self, status: int, obj: dict) -> None:
            raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

    return ThreadingHTTPServer((host, port), Handler)


def _build_routes(service: DispatchService):
    R = []
    add = lambda method, pattern, fn: R.append((re.compile(pattern), method, fn))

    add("GET", r"/health", lambda p: {"status": "up"})
    add("POST", r"/storms", lambda p: service.open_storm(
        p["storm_id"], p["rainfall_mm"], p["basin_ids"], at=p.get("at")))
    add("GET", r"/storms/(?P<storm_id>[^/]+)/snapshot", lambda p, storm_id: service.snapshot_view(storm_id))
    add("GET", r"/storms/(?P<storm_id>[^/]+)/plans", lambda p, storm_id: service.list_plans(storm_id))
    add("POST", r"/storms/(?P<storm_id>[^/]+)/plans", lambda p, storm_id: service.generate_plan(
        storm_id, at=p.get("at")))
    add("POST", r"/storms/(?P<storm_id>[^/]+)/reports", lambda p, storm_id: service.ingest_report(
        storm_id, p["report_id"], p["kind"], p["basin_id"], p["subject_id"],
        p["observed_at"], metrics=p.get("metrics"), received_at=p.get("received_at"),
        auto_plan=bool(p.get("auto_plan", False))))
    add("POST", r"/storms/(?P<storm_id>[^/]+)/alerts", lambda p, storm_id: service.raise_alert(
        storm_id, p["alert_id"], p["basin_id"], p["kind"], p["severity"], p["message"], at=p.get("at")))
    add("POST", r"/storms/(?P<storm_id>[^/]+)/dispatch-plan", lambda p, storm_id: service.dispatch_plan(
        storm_id, at=p.get("at")))
    add("GET", r"/storms/(?P<storm_id>[^/]+)/replay", lambda p, storm_id: service.replay(storm_id))
    add("POST", r"/storms/(?P<storm_id>[^/]+)/verify-recovery", lambda p, storm_id: service.verify_recovery(
        storm_id, order=p.get("order")))
    add("POST", r"/actions/(?P<action_id>[^/]+)/approve", lambda p, action_id: service.approve_action(
        action_id, p["commander"], note=p.get("note", ""), at=p.get("at")))
    add("POST", r"/actions/(?P<action_id>[^/]+)/reassign", lambda p, action_id: service.reassign_action(
        action_id, p["device_id"], p["commander"], at=p.get("at")))
    add("POST", r"/actions/(?P<action_id>[^/]+)/revoke", lambda p, action_id: service.revoke_action(
        action_id, p["commander"], reason=p.get("reason", ""), at=p.get("at")))
    add("POST", r"/actions/(?P<action_id>[^/]+)/dispatch", lambda p, action_id: service.dispatch_action(
        action_id, at=p.get("at")))
    add("POST", r"/receipts", lambda p: service.submit_receipt(
        p["receipt_id"], p["command_id"], p["kind"], note=p.get("note", ""), at=p.get("at")))
    add("POST", r"/tick", lambda p: service.sweep_timeouts(at=p.get("at")))
    add("GET", r"/queues", lambda p: service.queues())
    return R
