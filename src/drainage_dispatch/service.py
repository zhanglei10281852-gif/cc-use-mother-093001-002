"""排涝联合调度核心服务。

设计要点：
- 事件溯源：每次状态变更先落盘再应用，进程重启后重放恢复；
- 方案版本化：迟到数据只能触发新版本方案，已发命令不可改写；
- 幂等：上报 / 告警 / 回执按业务键去重，重复提交返回原结果；
- 发令前拦截：设备占用与联锁冲突在 CommandDispatched 事件产生前被拒绝；
- 时间可注入：超时升级由 sweep_timeouts(at) 驱动，不依赖真实等待。
"""
from __future__ import annotations

import threading
from datetime import timedelta
from pathlib import Path

from . import events as ev
from .clock import Clock, SystemClock, fmt_dt, parse_dt
from .contracts import CommandAction
from .errors import (
    ConflictError,
    DeviceConflictError,
    NotFoundError,
    StateTransitionError,
    ValidationError,
)
from .events import EventStore
from .models import (
    TERMINAL_COMMAND_STATES,
    ActionStatus,
    Alert,
    AlertSeverity,
    BasinView,
    Command,
    CommandStatus,
    FrozenSnapshot,
    Phase,
    PlanVersion,
    Receipt,
    ReceiptKind,
    ReportKind,
    SuggestedAction,
    TelemetryReport,
)
from .planner import build_plan_actions
from .registry import Registry
from .state import AppState, StormState

ESCALATION_NOTIFY = {1: "值班长", 2: "区防指"}


def _notify_target(level: int) -> str:
    return ESCALATION_NOTIFY.get(level, "市防指")


class DispatchService:
    def __init__(self, store: EventStore, registry: Registry, clock: Clock | None = None):
        self.store = store
        self.registry = registry
        self.clock = clock or SystemClock()
        self.state = AppState()
        self._lock = threading.RLock()
        for event in self.store.iter_events():
            self.state.apply(event)

    @classmethod
    def open(
        cls,
        data_dir: str | Path,
        registry: Registry | None = None,
        clock: Clock | None = None,
    ) -> "DispatchService":
        """打开（或创建）数据目录并恢复状态——进程重启后的恢复入口。"""
        store = EventStore(Path(data_dir) / "events.jsonl")
        return cls(store, registry or Registry.default(), clock)

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _now(self, at) -> object:
        return parse_dt(at) if at is not None else self.clock.now()

    def _storm(self, storm_id: str) -> StormState:
        try:
            return self.state.storms[storm_id]
        except KeyError:
            raise NotFoundError(f"未知降雨过程: {storm_id}") from None

    def _action(self, action_id: str) -> SuggestedAction:
        action = self.state.find_action(action_id)
        if action is None:
            raise NotFoundError(f"未知调度动作: {action_id}")
        return action

    def _command(self, command_id: str) -> Command:
        try:
            return self.state.commands[command_id]
        except KeyError:
            raise NotFoundError(f"未知调度命令: {command_id}") from None

    def _append(self, type_: str, at, payload: dict) -> dict:
        event = self.store.append(type_, at, payload)
        self.state.apply(event)
        return event

    # ------------------------------------------------------------------
    # 降雨过程与数据接入
    # ------------------------------------------------------------------
    def open_storm(self, storm_id: str, rainfall_mm: float, basin_ids, at=None) -> dict:
        with self._lock:
            at = self._now(at)
            basin_ids = tuple(basin_ids)
            if rainfall_mm < 0 or not basin_ids:
                raise ValidationError("降雨量和汇水分区必须有效")
            for b in basin_ids:
                self.registry.basin(b)
            existing = self.state.storms.get(storm_id)
            if existing is not None:
                if existing.rainfall_mm == float(rainfall_mm) and existing.basin_ids == basin_ids:
                    return {"storm_id": storm_id, "duplicate": True}
                raise ConflictError(f"降雨过程 {storm_id} 已存在且参数不一致")
            self._append(
                ev.STORM_OPENED, at,
                {"storm_id": storm_id, "rainfall_mm": float(rainfall_mm),
                 "basin_ids": list(basin_ids), "opened_at": fmt_dt(at)},
            )
            return {"storm_id": storm_id, "duplicate": False, "opened_at": fmt_dt(at)}

    def _build_views(self, storm: StormState) -> dict[str, BasinView]:
        views = {b: BasinView(basin_id=b) for b in storm.basin_ids}
        for report in storm.latest_by_subject.values():
            view = views.get(report.basin_id)
            if view is None:
                continue
            view.subjects = tuple(sorted((*view.subjects, report.subject_id)))
            m = report.metrics
            if report.kind == ReportKind.WATERLOGGING:
                volume = m.get("volume_m3")
                if volume is None:
                    volume = float(m.get("depth_m", 0.0)) * float(m.get("area_m2", 0.0))
                view.pending_volume_m3 += float(volume)
            elif report.kind == ReportKind.PIPE_LEVEL:
                level = float(m.get("level_m", 0.0))
                view.pipe_level_m = level if view.pipe_level_m is None else max(view.pipe_level_m, level)
            elif report.kind in (ReportKind.PUMP_STATUS, ReportKind.GATE_STATUS):
                if m.get("running"):
                    view.running_devices = tuple(sorted((*view.running_devices, report.subject_id)))
        return views

    def ingest_report(
        self,
        storm_id: str,
        report_id: str,
        kind: str,
        basin_id: str,
        subject_id: str,
        observed_at,
        metrics: dict | None = None,
        received_at=None,
        auto_plan: bool = False,
    ) -> dict:
        """接入现场上报。相同 report_id 幂等；观测时刻早于已冻结快照即标记迟到。

        迟到数据只更新工作快照（可触发新版本方案），绝不改写已冻结快照与已发命令。
        """
        with self._lock:
            storm = self._storm(storm_id)
            self.registry.basin(basin_id)
            if basin_id not in storm.basin_ids:
                raise ValidationError(f"{basin_id} 不属于降雨过程 {storm_id}")
            if report_id in storm.reports:
                return {"report": storm.reports[report_id].to_dict(), "duplicate": True}
            received = self._now(received_at)
            observed = parse_dt(observed_at)
            latest_snap = storm.snapshots.get(len(storm.snapshots)) if storm.snapshots else None
            late = latest_snap is not None and latest_snap.data_cutoff is not None \
                and observed <= latest_snap.data_cutoff
            report = TelemetryReport(
                report_id=report_id, storm_id=storm_id, kind=ReportKind(kind),
                basin_id=basin_id, subject_id=subject_id, observed_at=observed,
                received_at=received, metrics=dict(metrics or {}), late=late,
            )
            self._append(ev.REPORT_RECEIVED, received, {"report": report.to_dict()})
            result = {"report": report.to_dict(), "duplicate": False, "plan_stale": storm.plan_stale}
            if auto_plan:
                result["plan"] = self.generate_plan(storm_id, at=received)
            return result

    def raise_alert(
        self,
        storm_id: str,
        alert_id: str,
        basin_id: str,
        kind: str,
        severity: str,
        message: str,
        at=None,
    ) -> dict:
        """告警上报，相同 alert_id 幂等（重复转发不会产生第二条）。"""
        with self._lock:
            storm = self._storm(storm_id)
            self.registry.basin(basin_id)
            if alert_id in storm.alerts:
                return {"alert": storm.alerts[alert_id].to_dict(), "duplicate": True}
            at = self._now(at)
            alert = Alert(
                alert_id=alert_id, storm_id=storm_id, basin_id=basin_id, kind=kind,
                severity=AlertSeverity(severity), message=message, raised_at=at,
            )
            self._append(ev.ALERT_RAISED, at, {"alert": alert.to_dict()})
            return {"alert": alert.to_dict(), "duplicate": False}

    # ------------------------------------------------------------------
    # 快照与方案
    # ------------------------------------------------------------------
    def generate_plan(self, storm_id: str, at=None) -> dict:
        """冻结当前态势快照并生成新版本方案；上一版本未执行动作全部取代。"""
        with self._lock:
            storm = self._storm(storm_id)
            at = self._now(at)
            views = self._build_views(storm)
            snap_version = len(storm.snapshots) + 1
            snapshot = FrozenSnapshot(
                snapshot_id=f"{storm_id}-SNAP-v{snap_version}",
                storm_id=storm_id, version=snap_version, frozen_at=at,
                data_cutoff=storm.working_cutoff, basins=views,
            )
            self._append(ev.SNAPSHOT_FROZEN, at, {"snapshot": snapshot.to_dict()})

            # 取代旧版本中仍未发令的动作（已发命令不受影响）
            stale_ids = [
                a.action_id
                for a in sorted(storm.actions.values(), key=lambda a: a.action_id)
                if a.status in (ActionStatus.PROPOSED, ActionStatus.APPROVED)
            ]
            plan_version = storm.current_plan_version + 1
            if stale_ids:
                self._append(
                    ev.ACTIONS_SUPERSEDED, at,
                    {"storm_id": storm_id, "action_ids": stale_ids,
                     "reason": f"方案 v{plan_version} 生成，旧版本未执行动作作废",
                     "by_plan": f"{storm_id}-PLAN-v{plan_version}"},
                )

            running = self.state.running_devices()
            for view in views.values():
                running.update(view.running_devices)
            actions, notes = build_plan_actions(storm_id, plan_version, views, self.registry, running)
            plan = PlanVersion(
                plan_id=f"{storm_id}-PLAN-v{plan_version}", storm_id=storm_id,
                version=plan_version, snapshot_id=snapshot.snapshot_id, created_at=at,
                action_ids=tuple(a.action_id for a in actions), notes=tuple(notes),
            )
            self._append(
                ev.PLAN_GENERATED, at,
                {"plan": plan.to_dict(), "actions": [a.to_dict() for a in actions]},
            )
            return {"plan": plan.to_dict(), "actions": [a.to_dict() for a in actions],
                    "superseded": stale_ids}

    def list_plans(self, storm_id: str) -> dict:
        storm = self._storm(storm_id)
        return {
            "storm_id": storm_id,
            "plans": [storm.plans[v].to_dict() for v in sorted(storm.plans)],
            "actions": [a.to_dict() for a in sorted(storm.actions.values(), key=lambda a: a.action_id)],
        }

    def snapshot_view(self, storm_id: str) -> dict:
        storm = self._storm(storm_id)
        latest = storm.snapshots.get(len(storm.snapshots)) if storm.snapshots else None
        return {
            "storm_id": storm_id,
            "latest_frozen": latest.to_dict() if latest else None,
            "working": {k: v.to_dict() for k, v in self._build_views(storm).items()},
            "working_cutoff": fmt_dt(storm.working_cutoff),
            "plan_stale": storm.plan_stale,
        }

    # ------------------------------------------------------------------
    # 审批 / 改派 / 撤销
    # ------------------------------------------------------------------
    def approve_action(self, action_id: str, commander: str, note: str = "", at=None) -> dict:
        with self._lock:
            action = self._action(action_id)
            if action.status == ActionStatus.APPROVED:
                return {"action": action.to_dict(), "duplicate": True}
            if action.status != ActionStatus.PROPOSED:
                raise StateTransitionError(f"动作 {action_id} 当前状态 {action.status} 不可审批")
            at = self._now(at)
            self._append(
                ev.ACTION_APPROVED, at,
                {"storm_id": action.storm_id, "action_id": action_id,
                 "commander": commander, "note": note, "approved_at": fmt_dt(at)},
            )
            return {"action": self._action(action_id).to_dict(), "duplicate": False}

    def reassign_action(self, action_id: str, to_device_id: str, commander: str, at=None) -> dict:
        """改派设备：仅发令前允许，且目标设备必须通过占用/联锁检查。"""
        with self._lock:
            action = self._action(action_id)
            if action.status not in (ActionStatus.PROPOSED, ActionStatus.APPROVED):
                raise StateTransitionError(f"动作 {action_id} 已发令，不可改派，请走新版本方案")
            target = self.registry.device(to_device_id)
            if target.basin_id != action.basin_id:
                raise ValidationError(f"设备 {to_device_id} 不属于分区 {action.basin_id}，不允许跨区改派")
            if to_device_id == action.device_id:
                raise ValidationError("改派目标与原设备相同")
            self._check_device_free(to_device_id, action.command)
            at = self._now(at)
            self._append(
                ev.ACTION_REASSIGNED, at,
                {"storm_id": action.storm_id, "action_id": action_id,
                 "from_device": action.device_id, "to_device": to_device_id,
                 "commander": commander},
            )
            return {"action": self._action(action_id).to_dict()}

    def revoke_action(self, action_id: str, commander: str, reason: str = "", at=None) -> dict:
        with self._lock:
            action = self._action(action_id)
            if action.status == ActionStatus.REVOKED:
                return {"action": action.to_dict(), "duplicate": True}
            if action.status not in (ActionStatus.PROPOSED, ActionStatus.APPROVED):
                raise StateTransitionError(
                    f"动作 {action_id} 已发令不可撤销（已执行命令不可改写），请在新版本方案中调整"
                )
            at = self._now(at)
            self._append(
                ev.ACTION_REVOKED, at,
                {"storm_id": action.storm_id, "action_id": action_id,
                 "commander": commander, "reason": reason},
            )
            return {"action": self._action(action_id).to_dict(), "duplicate": False}

    # ------------------------------------------------------------------
    # 发令（冲突在事件产生前被拦截）
    # ------------------------------------------------------------------
    def _check_device_free(self, device_id: str, command: CommandAction) -> None:
        active = self.state.active_commands_by_device()
        holder = active.get(device_id)
        if holder is not None:
            raise DeviceConflictError(
                f"设备 {device_id} 已被命令 {holder.command_id}({holder.command}) 占用，"
                f"拒绝再下达 {command}"
            )
        if command == CommandAction.START:
            group = self.registry.device(device_id).interlock_group
            if group:
                for other in active.values():
                    if other.command != CommandAction.START:
                        continue
                    spec = self.registry.devices.get(other.device_id)
                    if spec and spec.interlock_group == group:
                        raise DeviceConflictError(
                            f"设备 {device_id} 与 {other.device_id} 同组联锁({group})，"
                            f"{other.device_id} 已有启动命令 {other.command_id}，拒绝发令"
                        )

    def dispatch_action(self, action_id: str, at=None) -> dict:
        with self._lock:
            action = self._action(action_id)
            if action.status in (ActionStatus.DISPATCHED, ActionStatus.ACKNOWLEDGED,
                                 ActionStatus.COMPLETED, ActionStatus.FAILED):
                return {"command": self._command(action.command_id).to_dict(), "duplicate": True}
            if action.status != ActionStatus.APPROVED:
                raise StateTransitionError(f"动作 {action_id} 状态 {action.status} 不可发令（需先审批）")
            storm = self._storm(action.storm_id)
            if action.plan_version != storm.current_plan_version:
                raise StateTransitionError(f"动作 {action_id} 所属方案版本已被取代，不可发令")
            self._check_device_free(action.device_id, action.command)
            at = self._now(at)
            deadline = at + timedelta(minutes=self.registry.ack_timeout_minutes)
            command = Command(
                command_id=f"CMD-{action_id}", action_id=action_id, storm_id=action.storm_id,
                basin_id=action.basin_id, device_id=action.device_id, command=action.command,
                issued_at=at, ack_deadline=deadline,
            )
            self._append(ev.COMMAND_DISPATCHED, at, {"command": command.to_dict()})
            return {"command": command.to_dict(), "duplicate": False}

    def dispatch_plan(self, storm_id: str, at=None) -> dict:
        """按优先级批量发出当前版本中已审批的动作；冲突逐条拦截，不影响其余。"""
        with self._lock:
            storm = self._storm(storm_id)
            at = self._now(at)
            results = []
            approved = sorted(
                (a for a in storm.actions.values()
                 if a.status == ActionStatus.APPROVED and a.plan_version == storm.current_plan_version),
                key=lambda a: (a.priority, a.seq),
            )
            for action in approved:
                try:
                    out = self.dispatch_action(action.action_id, at=at)
                    results.append({"action_id": action.action_id, "ok": True,
                                    "command_id": out["command"]["command_id"]})
                except DeviceConflictError as exc:
                    results.append({"action_id": action.action_id, "ok": False, "error": str(exc)})
            return {"storm_id": storm_id, "plan_version": storm.current_plan_version, "results": results}

    # ------------------------------------------------------------------
    # 现场回执（幂等）
    # ------------------------------------------------------------------
    def submit_receipt(self, receipt_id: str, command_id: str, kind: str, note: str = "", at=None) -> dict:
        with self._lock:
            if receipt_id in self.state.receipts:
                return {"receipt": self.state.receipts[receipt_id].to_dict(), "duplicate": True}
            command = self._command(command_id)
            kind = ReceiptKind(kind)
            if command.status in TERMINAL_COMMAND_STATES:
                raise ConflictError(
                    f"命令 {command_id} 已终态({command.status})，已执行命令不可改写"
                )
            if kind == ReceiptKind.ACCEPTED and command.status != CommandStatus.ISSUED:
                raise ConflictError(f"命令 {command_id} 状态 {command.status} 不可重复接单")
            received = self._now(at)
            if received < command.issued_at:
                raise ValidationError("回执时间早于发令时间")
            resulting = {
                ReceiptKind.ACCEPTED: CommandStatus.ACKNOWLEDGED,
                ReceiptKind.COMPLETED: CommandStatus.COMPLETED,
                ReceiptKind.FAILED: CommandStatus.FAILED,
            }[kind]
            receipt = Receipt(
                receipt_id=receipt_id, command_id=command_id, action_id=command.action_id,
                storm_id=command.storm_id, kind=kind, note=note,
                received_at=received, resulting_status=resulting,
            )
            self._append(
                ev.RECEIPT_ACCEPTED, received,
                {"receipt": receipt.to_dict(), "storm_id": command.storm_id},
            )
            return {"receipt": receipt.to_dict(), "duplicate": False}

    # ------------------------------------------------------------------
    # 超时升级（虚拟时间驱动，不依赖真实等待）
    # ------------------------------------------------------------------
    def sweep_timeouts(self, at=None) -> dict:
        with self._lock:
            now = self._now(at)
            escalated = []
            for command in sorted(self.state.commands.values(), key=lambda c: c.command_id):
                if command.status != CommandStatus.ISSUED or now <= command.ack_deadline:
                    continue
                level = command.escalation_level + 1
                deadline = now + timedelta(minutes=self.registry.escalation_interval(level))
                payload = {
                    "storm_id": command.storm_id, "command_id": command.command_id,
                    "action_id": command.action_id, "level": level,
                    "deadline": fmt_dt(deadline), "escalated_at": fmt_dt(now),
                    "notify": _notify_target(level),
                }
                self._append(ev.COMMAND_ESCALATED, now, payload)
                escalated.append(payload)
            return {"at": fmt_dt(now), "escalated": escalated}

    # ------------------------------------------------------------------
    # 队列 / 重放 / 恢复次序
    # ------------------------------------------------------------------
    def queues(self, at=None) -> dict:
        with self._lock:
            now = self._now(at)
            pending_dispatch = []
            for storm in self.state.storms.values():
                for a in storm.actions.values():
                    if a.status == ActionStatus.APPROVED and a.plan_version == storm.current_plan_version:
                        pending_dispatch.append({
                            "action_id": a.action_id, "storm_id": a.storm_id,
                            "basin_id": a.basin_id, "device_id": a.device_id,
                            "command": str(a.command), "priority": a.priority,
                        })
            pending_dispatch.sort(key=lambda x: (x["priority"], x["action_id"]))
            pending_receipts, escalated = [], []
            for c in sorted(self.state.commands.values(), key=lambda c: c.command_id):
                if c.status in TERMINAL_COMMAND_STATES:
                    continue
                item = {
                    "command_id": c.command_id, "storm_id": c.storm_id, "basin_id": c.basin_id,
                    "device_id": c.device_id, "command": str(c.command),
                    "stage": "acceptance" if c.status == CommandStatus.ISSUED else "completion",
                    "ack_deadline": fmt_dt(c.ack_deadline),
                    "overdue": now > c.ack_deadline,
                    "escalation_level": c.escalation_level,
                }
                pending_receipts.append(item)
                if c.escalation_level > 0:
                    escalated.append(item)
            return {
                "at": fmt_dt(now),
                "pending_dispatch": pending_dispatch,
                "pending_receipts": pending_receipts,
                "escalated": escalated,
            }

    def replay(self, storm_id: str) -> dict:
        """重放完整决策链：上报→快照→方案→审批→发令→回执→升级。"""
        self._storm(storm_id)
        chain = []
        for event in self.store.iter_events():
            p = event["payload"]
            t = event["type"]
            sid = p.get("storm_id") or (p.get("report") or {}).get("storm_id") \
                or (p.get("alert") or {}).get("storm_id") \
                or (p.get("snapshot") or {}).get("storm_id") \
                or (p.get("plan") or {}).get("storm_id") \
                or (p.get("command") or {}).get("storm_id") \
                or (p.get("receipt") or {}).get("storm_id")
            if sid != storm_id:
                continue
            chain.append({"seq": event["seq"], "at": event["at"], "type": t,
                          **_describe_event(t, p)})
        return {"storm_id": storm_id, "chain": chain}

    def verify_recovery(self, storm_id: str, order: list[str] | None = None) -> dict:
        """验证各分区恢复次序：恢复类动作的首个完成时间应按分区优先级先后有序。"""
        storm = self._storm(storm_id)
        expected = list(order) if order else sorted(
            storm.basin_ids, key=lambda b: (self.registry.basin(b).priority, b)
        )
        for b in expected:
            if b not in storm.basin_ids:
                raise ValidationError(f"{b} 不属于降雨过程 {storm_id}")
        first_done: dict[str, object] = {}
        for a in storm.actions.values():
            if a.phase != Phase.RECOVERY or a.status != ActionStatus.COMPLETED or not a.command_id:
                continue
            cmd = self.state.commands[a.command_id]
            if cmd.completed_at is None:
                continue
            prev = first_done.get(a.basin_id)
            if prev is None or cmd.completed_at < prev:
                first_done[a.basin_id] = cmd.completed_at
        violations = []
        for i, earlier in enumerate(expected):
            for later in expected[i + 1:]:
                t_early, t_late = first_done.get(earlier), first_done.get(later)
                if t_early is not None and t_late is not None and t_early > t_late:
                    violations.append({
                        "expected_first": earlier, "actual_first": later,
                        "earlier_completed_at": fmt_dt(t_early),
                        "later_completed_at": fmt_dt(t_late),
                    })
        actual = [
            {"basin_id": b, "first_recovery_completed_at": fmt_dt(first_done.get(b))}
            for b in expected
        ]
        return {
            "storm_id": storm_id,
            "expected_order": expected,
            "actual": actual,
            "incomplete": [b for b in expected if b not in first_done],
            "violations": violations,
            "ok": not violations,
        }


def _describe_event(type_: str, p: dict) -> dict:
    """为决策链条目生成摘要与关联链接。"""
    if type_ == ev.STORM_OPENED:
        return {"ref": p["storm_id"],
                "summary": f"降雨过程开始: 雨量 {p['rainfall_mm']}mm, 分区 {len(p['basin_ids'])} 个",
                "links": {}}
    if type_ == ev.REPORT_RECEIVED:
        r = p["report"]
        tag = "【迟到】" if r.get("late") else ""
        return {"ref": r["report_id"],
                "summary": f"{tag}收到上报 {r['report_id']}({r['kind']}/{r['subject_id']})",
                "links": {"basin_id": r["basin_id"]}}
    if type_ == ev.ALERT_RAISED:
        a = p["alert"]
        return {"ref": a["alert_id"],
                "summary": f"告警 {a['alert_id']}[{a['severity']}] {a['message']}",
                "links": {"basin_id": a["basin_id"]}}
    if type_ == ev.SNAPSHOT_FROZEN:
        s = p["snapshot"]
        return {"ref": s["snapshot_id"],
                "summary": f"冻结态势快照 {s['snapshot_id']}(数据截止 {s.get('data_cutoff')})",
                "links": {}}
    if type_ == ev.ACTIONS_SUPERSEDED:
        return {"ref": p["by_plan"],
                "summary": f"{len(p['action_ids'])} 个未执行动作被新版本取代: {', '.join(p['action_ids'])}",
                "links": {"action_ids": p["action_ids"]}}
    if type_ == ev.PLAN_GENERATED:
        plan = p["plan"]
        return {"ref": plan["plan_id"],
                "summary": f"生成方案 {plan['plan_id']}: {len(plan['action_ids'])} 个建议动作",
                "links": {"snapshot_id": plan["snapshot_id"], "action_ids": plan["action_ids"]}}
    if type_ == ev.ACTION_APPROVED:
        return {"ref": p["action_id"], "summary": f"审批通过 {p['action_id']}({p['commander']})",
                "links": {"action_id": p["action_id"]}}
    if type_ == ev.ACTION_REASSIGNED:
        return {"ref": p["action_id"],
                "summary": f"改派 {p['action_id']}: {p['from_device']} → {p['to_device']}({p['commander']})",
                "links": {"action_id": p["action_id"]}}
    if type_ == ev.ACTION_REVOKED:
        return {"ref": p["action_id"], "summary": f"撤销 {p['action_id']}: {p.get('reason', '')}",
                "links": {"action_id": p["action_id"]}}
    if type_ == ev.COMMAND_DISPATCHED:
        c = p["command"]
        return {"ref": c["command_id"],
                "summary": f"发令 {c['command_id']}: {c['device_id']} {c['command']}"
                           f"(回执截止 {c['ack_deadline']})",
                "links": {"action_id": c["action_id"], "device_id": c["device_id"]}}
    if type_ == ev.RECEIPT_ACCEPTED:
        r = p["receipt"]
        return {"ref": r["receipt_id"],
                "summary": f"回执 {r['receipt_id']}: {r['command_id']} → {r['kind']}",
                "links": {"command_id": r["command_id"], "action_id": r["action_id"]}}
    if type_ == ev.COMMAND_ESCALATED:
        return {"ref": p["command_id"],
                "summary": f"升级 {p['command_id']} 至 L{p['level']}，通知 {p['notify']}",
                "links": {"command_id": p["command_id"], "action_id": p["action_id"]}}
    return {"ref": "-", "summary": type_, "links": {}}
