"""命令行入口：值班人员通过 CLI 驱动调度、推进虚拟时钟并重放决策链。"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .errors import DispatchError
from .service import DispatchService
from .store import EventStore


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="drainage-dispatch", description="暴雨排涝联合调度命令行")
    parser.add_argument("--db", default=os.environ.get("DISPATCH_DB", "dispatch.db"),
                        help="事件库路径（默认 dispatch.db，可用 :memory:）")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("declare-storm", help="宣布一次降雨过程")
    p.add_argument("--storm-id", required=True)
    p.add_argument("--basin", dest="basins", action="append", required=True,
                   help="汇水分区，可多次指定")
    p.add_argument("--name", default="")
    p.add_argument("--rainfall-mm", type=float, default=None)

    p = sub.add_parser("register-device", help="登记排涝设备（泵/闸门）")
    p.add_argument("--device-id", required=True)
    p.add_argument("--basin-id", required=True)
    p.add_argument("--kind", choices=["pump", "gate"], required=True)
    p.add_argument("--capacity", type=float, required=True, help="排水能力 m³/min")

    p = sub.add_parser("ingest-report", help="接收积水点/泵站/闸门/管段水位/道路风险上报")
    p.add_argument("--storm-id", required=True)
    p.add_argument("--report-id", required=True)
    p.add_argument("--kind", required=True,
                   choices=["waterlogging", "pump_status", "gate_status",
                            "pipe_level", "road_risk"])
    p.add_argument("--basin-id", required=True)
    p.add_argument("--observed-at", required=True, help="ISO8601 观测时间")
    p.add_argument("--payload", default="{}", help="JSON 对象，如 '{\"depth_m\":0.5}'")

    p = sub.add_parser("freeze", help="冻结当前上报为态势快照")
    p.add_argument("--storm-id", required=True)

    p = sub.add_parser("plan", help="依据最新快照生成新版本方案")
    p.add_argument("--storm-id", required=True)

    p = sub.add_parser("approve", help="批准建议动作")
    p.add_argument("--action-id", required=True)
    p.add_argument("--by", default="")

    p = sub.add_parser("reassign", help="把动作改派到同分区空闲设备")
    p.add_argument("--action-id", required=True)
    p.add_argument("--device-id", required=True)
    p.add_argument("--by", default="")

    p = sub.add_parser("revoke", help="撤销未发令动作")
    p.add_argument("--action-id", required=True)
    p.add_argument("--by", default="")
    p.add_argument("--reason", default="")

    p = sub.add_parser("dispatch", help="对单个已批准动作发令")
    p.add_argument("--action-id", required=True)
    p.add_argument("--by", default="")

    p = sub.add_parser("dispatch-plan", help="批量发令方案内全部已批准动作")
    p.add_argument("--plan-id", required=True)
    p.add_argument("--by", default="")

    p = sub.add_parser("receipt", help="登记现场回执")
    p.add_argument("--receipt-id", required=True)
    p.add_argument("--action-id", required=True)
    p.add_argument("--status", choices=["done", "failed"], required=True)
    p.add_argument("--by", default="")
    p.add_argument("--payload", default="{}")

    p = sub.add_parser("tick", help="推进虚拟时钟并触发超时升级检查")
    p.add_argument("--seconds", type=float, default=None)
    p.add_argument("--to", default=None, help="推进到指定 ISO8601 时刻")

    p = sub.add_parser("queues", help="查看待发令与待回执队列")
    p.add_argument("--storm-id", default=None)

    p = sub.add_parser("storm", help="查看降雨过程态势")
    p.add_argument("--storm-id", required=True)

    p = sub.add_parser("replay", help="重放完整决策链")
    p.add_argument("--storm-id", required=True)

    p = sub.add_parser("recovery-order", help="查看各汇水分区恢复次序")
    p.add_argument("--storm-id", required=True)

    p = sub.add_parser("serve", help="启动 HTTP 调度服务")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)

    return parser


def _run(args, service: DispatchService):
    cmd = args.command
    if cmd == "declare-storm":
        return service.declare_storm(args.storm_id, args.basins, args.name,
                                     args.rainfall_mm)
    if cmd == "register-device":
        return service.register_device(args.device_id, args.basin_id, args.kind,
                                       args.capacity)
    if cmd == "ingest-report":
        return service.ingest_report(args.storm_id, args.report_id, args.kind,
                                     args.basin_id, args.observed_at,
                                     json.loads(args.payload))
    if cmd == "freeze":
        return service.freeze_snapshot(args.storm_id)
    if cmd == "plan":
        return service.generate_plan(args.storm_id)
    if cmd == "approve":
        return service.approve_action(args.action_id, args.by)
    if cmd == "reassign":
        return service.reassign_action(args.action_id, args.device_id, args.by)
    if cmd == "revoke":
        return service.revoke_action(args.action_id, args.by, args.reason)
    if cmd == "dispatch":
        return service.dispatch_action(args.action_id, args.by)
    if cmd == "dispatch-plan":
        return service.dispatch_plan(args.plan_id, args.by)
    if cmd == "receipt":
        return service.record_receipt(args.receipt_id, args.action_id, args.status,
                                      args.by, json.loads(args.payload))
    if cmd == "tick":
        return service.tick(seconds=args.seconds, to=args.to)
    if cmd == "queues":
        return service.queues(args.storm_id)
    if cmd == "storm":
        return service.storm_status(args.storm_id)
    if cmd == "replay":
        return service.replay(args.storm_id)
    if cmd == "recovery-order":
        return service.recovery_order(args.storm_id)
    raise DispatchError("BAD_REQUEST", f"未知命令 {cmd}", 400)


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    service = DispatchService(EventStore(args.db))
    if args.command == "serve":
        from .api import create_server

        httpd = create_server(service, args.host, args.port)
        print(f"调度服务已启动: http://{args.host}:{args.port}（事件库 {args.db}）")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            httpd.server_close()
        return 0
    try:
        result = _run(args, service)
    except DispatchError as exc:
        _print(exc.to_dict())
        return 2
    except (json.JSONDecodeError, ValueError) as exc:
        _print({"error": {"code": "BAD_REQUEST", "message": str(exc)}})
        return 2
    if result is not None:
        _print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
