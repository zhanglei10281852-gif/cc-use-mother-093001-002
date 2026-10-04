"""值班团队命令行：覆盖上报、方案、审批、发令、回执、升级、重放全流程。"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .clock import parse_dt
from .errors import DispatchError, ValidationError
from .registry import Registry
from .service import DispatchService


def _metric(text: str) -> tuple[str, object]:
    key, _, value = text.partition("=")
    if not _:
        raise argparse.ArgumentTypeError(f"指标格式应为 k=v: {text}")
    low = value.lower()
    if low in ("true", "false"):
        return key, low == "true"
    try:
        return key, float(value)
    except ValueError:
        return key, value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="drainage-dispatch", description="暴雨排涝联合调度")
    parser.add_argument("--data-dir", default=os.environ.get("DD_DATA_DIR", "dd_data"), help="事件日志目录")
    parser.add_argument("--registry", default=None, help="注册表 JSON 路径（缺省用内置示例）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("open-storm", help="开启一次降雨过程")
    p.add_argument("--storm", required=True)
    p.add_argument("--rainfall", type=float, required=True)
    p.add_argument("--basins", required=True, help="逗号分隔，如 BASIN-A,BASIN-B")
    p.add_argument("--at", default=None)

    p = sub.add_parser("ingest", help="接入现场上报（积水点/泵站/闸门/管段水位）")
    p.add_argument("--storm", required=True)
    p.add_argument("--report-id", required=True)
    p.add_argument("--kind", required=True, choices=["waterlogging", "pump_status", "gate_status", "pipe_level"])
    p.add_argument("--basin", required=True)
    p.add_argument("--subject", required=True)
    p.add_argument("--observed-at", required=True)
    p.add_argument("--at", default=None, help="接收时刻，缺省用系统当前时间")
    p.add_argument("--metric", type=_metric, action="append", default=[], help="k=v，可重复")
    p.add_argument("--auto-plan", action="store_true", help="迟到数据立即触发新版本方案")

    p = sub.add_parser("alert", help="上报告警（相同 alert-id 幂等）")
    p.add_argument("--storm", required=True)
    p.add_argument("--alert-id", required=True)
    p.add_argument("--basin", required=True)
    p.add_argument("--kind", default="manual")
    p.add_argument("--severity", default="warning", choices=["info", "warning", "critical"])
    p.add_argument("--message", default="")
    p.add_argument("--at", default=None)

    p = sub.add_parser("plan", help="冻结快照并生成新版本方案")
    p.add_argument("--storm", required=True)
    p.add_argument("--at", default=None)

    p = sub.add_parser("plans", help="查看方案版本与动作")
    p.add_argument("--storm", required=True)

    p = sub.add_parser("snapshot", help="查看冻结快照与工作快照")
    p.add_argument("--storm", required=True)

    p = sub.add_parser("approve", help="审批建议动作")
    p.add_argument("--action", required=True)
    p.add_argument("--commander", required=True)
    p.add_argument("--note", default="")
    p.add_argument("--at", default=None)

    p = sub.add_parser("reassign", help="改派设备（发令前）")
    p.add_argument("--action", required=True)
    p.add_argument("--device", required=True)
    p.add_argument("--commander", required=True)
    p.add_argument("--at", default=None)

    p = sub.add_parser("revoke", help="撤销建议动作（发令前）")
    p.add_argument("--action", required=True)
    p.add_argument("--commander", required=True)
    p.add_argument("--reason", default="")
    p.add_argument("--at", default=None)

    p = sub.add_parser("dispatch", help="发出单个动作命令")
    p.add_argument("--action", required=True)
    p.add_argument("--at", default=None)

    p = sub.add_parser("dispatch-plan", help="按优先级批量发出当前版本已审批动作")
    p.add_argument("--storm", required=True)
    p.add_argument("--at", default=None)

    p = sub.add_parser("receipt", help="提交现场回执（相同 receipt-id 幂等）")
    p.add_argument("--receipt-id", required=True)
    p.add_argument("--command", required=True)
    p.add_argument("--kind", required=True, choices=["accepted", "completed", "failed"])
    p.add_argument("--note", default="")
    p.add_argument("--at", default=None)

    p = sub.add_parser("tick", help="推进到指定时刻并扫描超时升级")
    p.add_argument("--at", default=None, help="缺省用系统当前时间")

    p = sub.add_parser("queues", help="查看待执行与待回执队列")
    p.add_argument("--at", default=None)

    p = sub.add_parser("replay", help="重放完整决策链")
    p.add_argument("--storm", required=True)

    p = sub.add_parser("verify-recovery", help="验证各分区恢复次序")
    p.add_argument("--storm", required=True)
    p.add_argument("--order", default=None, help="期望次序，逗号分隔；缺省按注册表优先级")

    p = sub.add_parser("serve", help="启动 HTTP 接口")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    return parser


def _service(args) -> DispatchService:
    registry = Registry.load(args.registry) if args.registry else Registry.default()
    return DispatchService.open(args.data_dir, registry=registry)


def _emit(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    service = _service(args)
    try:
        if args.cmd == "open-storm":
            out = service.open_storm(args.storm, args.rainfall, args.basins.split(","), at=args.at)
        elif args.cmd == "ingest":
            out = service.ingest_report(
                args.storm, args.report_id, args.kind, args.basin, args.subject,
                parse_dt(args.observed_at), metrics=dict(args.metric),
                received_at=args.at, auto_plan=args.auto_plan)
        elif args.cmd == "alert":
            out = service.raise_alert(args.storm, args.alert_id, args.basin, args.kind,
                                      args.severity, args.message, at=args.at)
        elif args.cmd == "plan":
            out = service.generate_plan(args.storm, at=args.at)
        elif args.cmd == "plans":
            out = service.list_plans(args.storm)
        elif args.cmd == "snapshot":
            out = service.snapshot_view(args.storm)
        elif args.cmd == "approve":
            out = service.approve_action(args.action, args.commander, note=args.note, at=args.at)
        elif args.cmd == "reassign":
            out = service.reassign_action(args.action, args.device, args.commander, at=args.at)
        elif args.cmd == "revoke":
            out = service.revoke_action(args.action, args.commander, reason=args.reason, at=args.at)
        elif args.cmd == "dispatch":
            out = service.dispatch_action(args.action, at=args.at)
        elif args.cmd == "dispatch-plan":
            out = service.dispatch_plan(args.storm, at=args.at)
        elif args.cmd == "receipt":
            out = service.submit_receipt(args.receipt_id, args.command, args.kind,
                                         note=args.note, at=args.at)
        elif args.cmd == "tick":
            out = service.sweep_timeouts(at=args.at)
        elif args.cmd == "queues":
            out = service.queues(at=args.at)
        elif args.cmd == "replay":
            out = service.replay(args.storm)
        elif args.cmd == "verify-recovery":
            order = args.order.split(",") if args.order else None
            out = service.verify_recovery(args.storm, order=order)
        elif args.cmd == "serve":
            from .api import make_server

            server = make_server(service, args.host, args.port)
            print(f"排涝调度接口已启动: http://{args.host}:{args.port} (数据目录 {args.data_dir})")
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            return 0
        else:  # pragma: no cover
            raise ValidationError(f"未知命令 {args.cmd}")
    except DispatchError as exc:
        _emit({"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}})
        return 2
    _emit({"ok": True, "data": out})
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
