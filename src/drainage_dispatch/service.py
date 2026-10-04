"""联合调度服务：事件溯源存储 + 内存投影。

所有状态变化先追加到事件库再应用到内存投影，进程重启后按事件
顺序重放即可恢复待发令与待回执队列。超时升级由注入时钟驱动，
测试与值班人员可通过 tick 推进虚拟时间，不依赖真实等待。
"""
from __future__ import annotations

import threading
from datetime import timedelta

from .clock import OffsetClock, iso, parse_iso
from .contracts import CommandAction
from .errors import DispatchError
from .models import (
    OCCUPYING_STATES,
    REPORT_KIND_LABELS,
    Action,
    ActionState,
    Device,
    DeviceKind,
    Plan,
    PlanState,
    Report,
    ReportKind,
    Snapshot,
    Storm,
)
from .planning import assess_basins, suggest_actions
from .store import Event, EventStore

# 事件类型
STORM_DECLARED = "StormDeclared"
DEVICE_REGISTERED = "DeviceRegistered"
REPORT_INGESTED = "ReportIngested"
SNAPSHOT_FROZEN = "SnapshotFrozen"
PLAN_GENERATED = "PlanGenerated"
PLAN_SUPERSEDED = "PlanSuperseded"
ACTION_APPROVED = "ActionApproved"
ACTION_REASSIGNED = "ActionReassigned"
ACTION_REVOKED = "ActionRevoked"
COMMAND_DISPATCHED = "CommandDispatched"
ACTION_ESCALATED = "ActionEscalated"
RECEIPT_RECORDED = "ReceiptRecorded"

# 设备注册是全局主数据，不属于任何单场降雨
GLOBAL_SCOPE = "GLOBAL"

RECEIPT_DONE = "done"
RECEIPT_FAILED = "failed"

DEFAULT_ACK_LADDER_SEC = (900.0, 1800.0, 3600.0)  # 逐级升级间隔
DEFAULT_DRAINAGE_WINDOW_MIN = 60.0                # 默认排涝窗口


def ensure_device_dispatchable(action: Action, device: Device) -> None:
    """发令前的设备占用硬校验：同一设备不允许被并行动作占用。"""
    if not device.available:
        raise DispatchError("DEVICE_UNAVAILABLE", f"设备 {device.device_id} 不可用")
    if device.occupied_by not in (None, action.action_id):
        raise DispatchError(
            "DEVICE_CONFLICT",
            f"设备 {device.device_id} 正被动作 {device.occupied_by} 占用，"
            "已拦截重复/相反命令",
        )


class DispatchService:
    def __init__(
        self,
        store: EventStore,
        clock=None,
        ack_ladder_sec=DEFAULT_ACK_LADDER_SEC,
        drainage_window_min: float = DEFAULT_DRAINAGE_WINDOW_MIN,
    ) -> None:
        self._store = store
        self._clock = clock or OffsetClock(store)
        self._ladder = tuple(float(s) for s in ack_ladder_sec)
        if not self._ladder or any(s <= 0 for s in self._ladder):
            raise ValueError("升级阶梯必须是非空正数序列")
        self._window = float(drainage_window_min)
        self._lock = threading.RLock()
        self.storms: dict[str, Storm] = {}
        self.devices: dict[str, Device] = {}
        self.plans: dict[str, Plan] = {}
        self.actions: dict[str, Action] = {}
        self.receipts: dict[str, dict] = {}
        self._events: list[Event] = []
        for event in store.load_all():
            self._apply(event)

    # ------------------------------------------------------------------
    # 事件追加与投影
    # ------------------------------------------------------------------

    def _emit(self, storm_id: str, event_type: str, payload: dict) -> Event:
        event = self._store.append(storm_id, event_type, payload, self._clock.now())
        self._apply(event)
        return event

    def _apply(self, event: Event) -> None:
        p = event.payload
        t = event.event_type
        if t == STORM_DECLARED:
            self.storms[event.storm_id] = Storm(
                storm_id=event.storm_id, name=p["name"],
                basin_ids=tuple(p["basin_ids"]), rainfall_mm=p.get("rainfall_mm"),
                declared_at=event.occurred_at)
        elif t == DEVICE_REGISTERED:
            self.devices[p["device_id"]] = Device(
                device_id=p["device_id"], basin_id=p["basin_id"],
                kind=DeviceKind(p["kind"]), capacity_m3_min=p["capacity_m3_min"],
                available=p.get("available", True))
        elif t == REPORT_INGESTED:
            storm = self.storms[event.storm_id]
            storm.reports[p["report_id"]] = Report(
                report_id=p["report_id"], storm_id=event.storm_id,
                kind=ReportKind(p["kind"]), basin_id=p["basin_id"],
                observed_at=parse_iso(p["observed_at"]), payload=p["payload"],
                ingested_at=event.occurred_at, late=p.get("late", False))
        elif t == SNAPSHOT_FROZEN:
            self.storms[event.storm_id].snapshots.append(Snapshot(
                snapshot_id=p["snapshot_id"], storm_id=event.storm_id,
                version=p["version"], report_ids=tuple(p["report_ids"]),
                frozen_at=event.occurred_at))
        elif t == PLAN_GENERATED:
            plan = Plan(plan_id=p["plan_id"], storm_id=event.storm_id,
                        version=p["version"], snapshot_id=p["snapshot_id"],
                        created_at=event.occurred_at, notes=list(p.get("notes", [])))
            for ap in p["actions"]:
                action = Action(
                    action_id=ap["action_id"], plan_id=plan.plan_id,
                    storm_id=event.storm_id, basin_id=ap["basin_id"],
                    device_id=ap["device_id"], command=CommandAction(ap["command"]),
                    priority=ap["priority"], reason=ap["reason"],
                    created_at=event.occurred_at)
                self.actions[action.action_id] = action
                plan.action_ids.append(action.action_id)
            self.plans[plan.plan_id] = plan
            self.storms[event.storm_id].plan_ids.append(plan.plan_id)
        elif t == PLAN_SUPERSEDED:
            plan = self.plans[p["plan_id"]]
            plan.state = PlanState.SUPERSEDED
            for aid in p["superseded_action_ids"]:
                self.actions[aid].state = ActionState.SUPERSEDED
                self._release_device(aid)
        elif t == ACTION_APPROVED:
            action = self.actions[p["action_id"]]
            action.state = ActionState.APPROVED
            action.approved_by = p["by"]
            action.approved_at = event.occurred_at
            self.devices[action.device_id].occupied_by = action.action_id
        elif t == ACTION_REASSIGNED:
            action = self.actions[p["action_id"]]
            old = self.devices[p["old_device_id"]]
            if old.occupied_by == action.action_id:
                old.occupied_by = None
            action.device_id = p["new_device_id"]
            if action.state in OCCUPYING_STATES:
                self.devices[p["new_device_id"]].occupied_by = action.action_id
            action.reassignments.append({
                "from": p["old_device_id"], "to": p["new_device_id"],
                "by": p["by"], "at": iso(event.occurred_at)})
        elif t == ACTION_REVOKED:
            action = self.actions[p["action_id"]]
            action.state = ActionState.REVOKED
            action.revoked_by = p["by"]
            action.revoke_reason = p.get("reason", "")
            self._release_device(action.action_id)
        elif t == COMMAND_DISPATCHED:
            action = self.actions[p["action_id"]]
            action.state = ActionState.DISPATCHED
            action.command_id = p["command_id"]
            action.dispatched_at = event.occurred_at
            action.deadline_at = parse_iso(p["deadline_at"])
        elif t == ACTION_ESCALATED:
            action = self.actions[p["action_id"]]
            action.state = ActionState.ESCALATED
            action.escalation_level = p["level"]
            action.deadline_at = parse_iso(p["deadline_at"])
        elif t == RECEIPT_RECORDED:
            action = self.actions[p["action_id"]]
            action.receipt_id = p["receipt_id"]
            if p["status"] == RECEIPT_DONE:
                action.state = ActionState.ACKNOWLEDGED
                action.acked_at = event.occurred_at
            else:
                action.state = ActionState.FAILED
            self._release_device(action.action_id)
            self.receipts[p["receipt_id"]] = {
                "receipt_id": p["receipt_id"], "action_id": p["action_id"],
                "status": p["status"], "by": p["by"],
                "payload": p.get("payload", {}),
                "recorded_at": iso(event.occurred_at)}
        self._events.append(event)

    def _release_device(self, action_id: str) -> None:
        for device in self.devices.values():
            if device.occupied_by == action_id:
                device.occupied_by = None

    # ------------------------------------------------------------------
    # 校验辅助
    # ------------------------------------------------------------------

    def _require_storm(self, storm_id: str) -> Storm:
        storm = self.storms.get(storm_id)
        if storm is None:
            raise DispatchError("STORM_NOT_FOUND", f"降雨过程 {storm_id} 不存在", 404)
        return storm

    def _require_action(self, action_id: str) -> Action:
        action = self.actions.get(action_id)
        if action is None:
            raise DispatchError("ACTION_NOT_FOUND", f"动作 {action_id} 不存在", 404)
        return action

    def _require_plan(self, plan_id: str) -> Plan:
        plan = self.plans.get(plan_id)
        if plan is None:
            raise DispatchError("PLAN_NOT_FOUND", f"方案 {plan_id} 不存在", 404)
        return plan

    def _require_device(self, device_id: str) -> Device:
        device = self.devices.get(device_id)
        if device is None:
            raise DispatchError("DEVICE_NOT_FOUND", f"设备 {device_id} 未登记", 404)
        return device

    @staticmethod
    def _validate_report_payload(kind: ReportKind, payload: dict) -> None:
        def nonneg(key: str) -> None:
            value = payload.get(key)
            if value is None:
                return
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
                raise DispatchError("INVALID_REPORT", f"{key} 必须是非负数值", 400)

        if kind == ReportKind.WATERLOGGING:
            nonneg("depth_m")
            nonneg("area_m2")
        elif kind == ReportKind.PIPE_LEVEL:
            nonneg("excess_m3")
            nonneg("level_m")
        elif kind == ReportKind.ROAD_RISK:
            level = payload.get("risk_level")
            if not isinstance(level, int) or isinstance(level, bool) or not 1 <= level <= 5:
                raise DispatchError("INVALID_REPORT", "risk_level 必须是 1-5 的整数", 400)
        elif kind in (ReportKind.PUMP_STATUS, ReportKind.GATE_STATUS):
            if not payload.get("device_id"):
                raise DispatchError("INVALID_REPORT", "设备状态上报需要 device_id", 400)
            if payload.get("status") not in ("ok", "fault"):
                raise DispatchError("INVALID_REPORT", "status 只能是 ok 或 fault", 400)

    # ------------------------------------------------------------------
    # 写入操作
    # ------------------------------------------------------------------

    def declare_storm(self, storm_id: str, basin_ids, name: str = "",
                      rainfall_mm: float | None = None) -> dict:
        """宣布一次降雨过程；相同参数重复宣布幂等。"""
        with self._lock:
            if not storm_id or not basin_ids:
                raise DispatchError(
                    "INVALID_STORM", "降雨过程需要 storm_id 与至少一个汇水分区", 400)
            basin_ids = tuple(str(b) for b in basin_ids)
            if rainfall_mm is not None and float(rainfall_mm) < 0:
                raise DispatchError("INVALID_STORM", "降雨量不能为负", 400)
            existing = self.storms.get(storm_id)
            if existing is not None:
                if (existing.basin_ids == basin_ids and existing.name == name
                        and existing.rainfall_mm == rainfall_mm):
                    return {**self._storm_dict(existing), "duplicate": True}
                raise DispatchError(
                    "STORM_EXISTS", f"降雨过程 {storm_id} 已存在，参数不一致")
            self._emit(storm_id, STORM_DECLARED, {
                "name": name, "basin_ids": list(basin_ids),
                "rainfall_mm": rainfall_mm})
            return self._storm_dict(self.storms[storm_id])

    def register_device(self, device_id: str, basin_id: str, kind: str,
                        capacity_m3_min: float, available: bool = True) -> dict:
        """登记排涝设备；相同参数重复登记幂等。"""
        with self._lock:
            if not device_id or not basin_id:
                raise DispatchError("INVALID_DEVICE", "设备需要 device_id 与 basin_id", 400)
            try:
                kind = DeviceKind(kind)
            except ValueError:
                raise DispatchError("INVALID_DEVICE", f"未知设备类型 {kind}", 400) from None
            capacity = float(capacity_m3_min)
            if capacity <= 0:
                raise DispatchError("INVALID_DEVICE", "设备能力必须大于零", 400)
            existing = self.devices.get(device_id)
            if existing is not None:
                if (existing.basin_id == basin_id and existing.kind == kind
                        and existing.capacity_m3_min == capacity
                        and existing.available == bool(available)):
                    return {**self._device_dict(existing), "duplicate": True}
                raise DispatchError(
                    "DEVICE_EXISTS", f"设备 {device_id} 已登记，参数不一致")
            self._emit(GLOBAL_SCOPE, DEVICE_REGISTERED, {
                "device_id": device_id, "basin_id": basin_id, "kind": str(kind),
                "capacity_m3_min": capacity, "available": bool(available)})
            return self._device_dict(self.devices[device_id])

    def ingest_report(self, storm_id: str, report_id: str, kind: str,
                      basin_id: str, observed_at: str, payload: dict) -> dict:
        """接收积水点/泵站/闸门/管段水位/道路风险上报。

        相同 report_id 与内容的重复上报幂等；观测时间早于最近快照
        冻结时刻的迟到数据只进入工作集，等待触发新版本方案。
        """
        with self._lock:
            storm = self._require_storm(storm_id)
            try:
                kind = ReportKind(kind)
            except ValueError:
                raise DispatchError("INVALID_REPORT", f"未知上报类型 {kind}", 400) from None
            if not report_id:
                raise DispatchError("INVALID_REPORT", "上报需要 report_id", 400)
            payload = dict(payload or {})
            self._validate_report_payload(kind, payload)
            try:
                observed = parse_iso(observed_at)
            except (TypeError, ValueError):
                raise DispatchError(
                    "INVALID_REPORT", f"observed_at 无法解析: {observed_at}", 400) from None
            existing = storm.reports.get(report_id)
            if existing is not None:
                if (existing.kind == kind and existing.basin_id == basin_id
                        and existing.payload == payload
                        and existing.observed_at == observed):
                    return {**self._report_dict(existing), "duplicate": True}
                raise DispatchError(
                    "REPORT_CONFLICT", f"上报 {report_id} 已存在，内容不一致")
            late = bool(storm.snapshots) and observed <= storm.snapshots[-1].frozen_at
            self._emit(storm_id, REPORT_INGESTED, {
                "report_id": report_id, "kind": str(kind), "basin_id": basin_id,
                "observed_at": iso(observed), "payload": payload, "late": late})
            return self._report_dict(storm.reports[report_id])

    def freeze_snapshot(self, storm_id: str) -> dict:
        """把当前全部上报冻结成不可变的态势快照。"""
        with self._lock:
            storm = self._require_storm(storm_id)
            report_ids = [
                r.report_id for r in sorted(
                    storm.reports.values(),
                    key=lambda r: (r.observed_at, r.report_id))
            ]
            if storm.snapshots and tuple(report_ids) == storm.snapshots[-1].report_ids:
                raise DispatchError("NO_NEW_REPORTS", "自上次快照以来没有新的上报")
            version = len(storm.snapshots) + 1
            snapshot_id = f"{storm_id}-SNAP-v{version}"
            self._emit(storm_id, SNAPSHOT_FROZEN, {
                "snapshot_id": snapshot_id, "version": version,
                "report_ids": report_ids})
            return self._snapshot_dict(storm.snapshots[-1])

    def generate_plan(self, storm_id: str) -> dict:
        """依据最新快照生成新版本方案。

        旧方案中未发令的动作作废并释放设备；已发令命令保持原样，
        迟到数据只能通过新版本方案体现。
        """
        with self._lock:
            storm = self._require_storm(storm_id)
            if not storm.snapshots:
                raise DispatchError("NO_SNAPSHOT", "请先冻结态势快照")
            snapshot = storm.snapshots[-1]
            active = self._active_plan(storm)
            if active is not None and active.snapshot_id == snapshot.snapshot_id:
                return {**self._plan_dict(active), "up_to_date": True}
            if active is not None:
                carried = [aid for aid in active.action_ids
                           if self.actions[aid].state
                           in (ActionState.PROPOSED, ActionState.APPROVED)]
                self._emit(storm_id, PLAN_SUPERSEDED, {
                    "plan_id": active.plan_id, "superseded_action_ids": carried,
                    "reason": "新数据到达，生成新版本方案；已发令命令保持不变"})
            reports = [storm.reports[rid] for rid in snapshot.report_ids]
            assessments = assess_basins(reports, storm.basin_ids)
            fault_ids = {
                r.payload["device_id"] for r in reports
                if r.kind in (ReportKind.PUMP_STATUS, ReportKind.GATE_STATUS)
                and r.payload.get("status") == "fault"
            }
            occupied = {d.device_id for d in self.devices.values() if d.occupied_by}
            suggestions, notes = suggest_actions(
                assessments, list(self.devices.values()),
                occupied, fault_ids, self._window)
            version = len(storm.plan_ids) + 1
            plan_id = f"{storm_id}-PLAN-v{version}"
            actions_payload = [{
                "action_id": f"{plan_id}-A{i:02d}",
                "device_id": s.device_id, "basin_id": s.basin_id,
                "command": str(s.command), "priority": s.priority,
                "reason": s.reason,
            } for i, s in enumerate(suggestions, start=1)]
            self._emit(storm_id, PLAN_GENERATED, {
                "plan_id": plan_id, "version": version,
                "snapshot_id": snapshot.snapshot_id,
                "actions": actions_payload, "notes": notes})
            return self._plan_dict(self.plans[plan_id])

    def approve_action(self, action_id: str, by: str = "") -> dict:
        """批准建议动作；批准即预占设备，冲突在此提前拦截。"""
        with self._lock:
            action = self._require_action(action_id)
            if action.state != ActionState.PROPOSED:
                raise DispatchError(
                    "ACTION_STATE", f"动作 {action_id} 当前状态 {action.state}，不能审批")
            device = self._require_device(action.device_id)
            ensure_device_dispatchable(action, device)
            self._emit(action.storm_id, ACTION_APPROVED,
                       {"action_id": action_id, "by": by})
            return self._action_dict(action)

    def reassign_action(self, action_id: str, new_device_id: str, by: str = "") -> dict:
        """把动作改派到同分区的另一台空闲设备。"""
        with self._lock:
            action = self._require_action(action_id)
            if action.state not in (ActionState.PROPOSED, ActionState.APPROVED):
                raise DispatchError(
                    "ACTION_STATE", f"动作 {action_id} 当前状态 {action.state}，不能改派")
            new_device = self._require_device(new_device_id)
            if new_device.device_id == action.device_id:
                raise DispatchError("REASSIGN_SAME_DEVICE", "改派目标与原设备相同", 400)
            if new_device.basin_id != action.basin_id:
                raise DispatchError(
                    "REASSIGN_BASIN_MISMATCH", "改派设备必须属于同一汇水分区", 400)
            ensure_device_dispatchable(action, new_device)
            self._emit(action.storm_id, ACTION_REASSIGNED, {
                "action_id": action_id, "old_device_id": action.device_id,
                "new_device_id": new_device_id, "by": by})
            return self._action_dict(action)

    def revoke_action(self, action_id: str, by: str = "", reason: str = "") -> dict:
        """撤销未发令的动作；已发令命令不可撤销，只能等回执或走新版本方案。"""
        with self._lock:
            action = self._require_action(action_id)
            if action.state in (ActionState.DISPATCHED, ActionState.ESCALATED):
                raise DispatchError(
                    "ALREADY_DISPATCHED",
                    "已发令命令不可撤销，请等待回执或通过新版本方案纠偏")
            if action.state not in (ActionState.PROPOSED, ActionState.APPROVED):
                raise DispatchError(
                    "ACTION_STATE", f"动作 {action_id} 当前状态 {action.state}，不能撤销")
            self._emit(action.storm_id, ACTION_REVOKED,
                       {"action_id": action_id, "by": by, "reason": reason})
            return self._action_dict(action)

    def dispatch_action(self, action_id: str, by: str = "") -> dict:
        """对已批准动作发令；发令前再次硬校验设备占用冲突。"""
        with self._lock:
            action = self._require_action(action_id)
            if action.state != ActionState.APPROVED:
                raise DispatchError(
                    "ACTION_NOT_APPROVED",
                    f"动作 {action_id} 当前状态 {action.state}，不能发令")
            device = self._require_device(action.device_id)
            ensure_device_dispatchable(action, device)
            deadline = self._clock.now() + timedelta(seconds=self._ladder[0])
            self._emit(action.storm_id, COMMAND_DISPATCHED, {
                "command_id": f"CMD-{action.action_id}",
                "action_id": action.action_id, "device_id": device.device_id,
                "command": str(action.command), "by": by,
                "deadline_at": iso(deadline)})
            return self._action_dict(action)

    def dispatch_plan(self, plan_id: str, by: str = "") -> dict:
        """批量发令方案内全部已批准动作；单条冲突被拦截并记录，不影响其余。"""
        with self._lock:
            plan = self._require_plan(plan_id)
            dispatched, conflicts, skipped = [], [], []
            ordered = sorted(
                (self.actions[aid] for aid in plan.action_ids),
                key=lambda a: (a.priority, a.action_id))
            for action in ordered:
                if action.state != ActionState.APPROVED:
                    skipped.append({"action_id": action.action_id,
                                    "state": str(action.state)})
                    continue
                try:
                    self.dispatch_action(action.action_id, by)
                    dispatched.append(action.action_id)
                except DispatchError as exc:
                    conflicts.append({"action_id": action.action_id,
                                      "code": exc.code, "message": exc.message})
            return {"plan_id": plan_id, "dispatched": dispatched,
                    "conflicts": conflicts, "skipped": skipped}

    def record_receipt(self, receipt_id: str, action_id: str, status: str,
                       by: str = "", payload: dict | None = None) -> dict:
        """登记现场回执；相同 receipt_id 与内容重复登记幂等。"""
        with self._lock:
            if not receipt_id:
                raise DispatchError("INVALID_RECEIPT", "回执需要 receipt_id", 400)
            existing = self.receipts.get(receipt_id)
            if existing is not None:
                if existing["action_id"] == action_id and existing["status"] == status:
                    return {**existing, "duplicate": True}
                raise DispatchError(
                    "RECEIPT_CONFLICT", f"回执 {receipt_id} 已存在，内容不一致")
            if status not in (RECEIPT_DONE, RECEIPT_FAILED):
                raise DispatchError("INVALID_RECEIPT", "status 只能是 done 或 failed", 400)
            action = self._require_action(action_id)
            if action.state not in (ActionState.DISPATCHED, ActionState.ESCALATED):
                raise DispatchError(
                    "ACTION_NOT_DISPATCHED", f"动作 {action_id} 未处于待回执状态")
            self._emit(action.storm_id, RECEIPT_RECORDED, {
                "receipt_id": receipt_id, "action_id": action_id,
                "status": status, "by": by, "payload": dict(payload or {})})
            return dict(self.receipts[receipt_id])

    # ------------------------------------------------------------------
    # 时钟与超时升级
    # ------------------------------------------------------------------

    def tick(self, seconds: float | None = None, to: str | None = None) -> dict:
        """推进虚拟时钟并执行超时检查；不依赖真实等待。"""
        with self._lock:
            if seconds is None and to is None:
                raise DispatchError("INVALID_TICK", "tick 需要 seconds 或 to 参数", 400)
            if seconds is not None:
                advance = getattr(self._clock, "advance", None)
                if advance is None:
                    raise DispatchError("CLOCK_NOT_VIRTUAL", "当前时钟不支持推进", 400)
                advance(float(seconds))
            if to is not None:
                advance_to = getattr(self._clock, "advance_to", None)
                if advance_to is None:
                    raise DispatchError("CLOCK_NOT_VIRTUAL", "当前时钟不支持推进", 400)
                try:
                    advance_to(parse_iso(to))
                except ValueError as exc:
                    raise DispatchError("INVALID_TICK", str(exc), 400) from None
            escalations = self.check_timeouts()
            return {"now": iso(self._clock.now()), "escalations": escalations}

    def check_timeouts(self) -> list[dict]:
        """把超过回执期限的动作逐级升级，并顺延下一级期限。"""
        with self._lock:
            now = self._clock.now()
            escalations = []
            pending = [a for a in self.actions.values()
                       if a.state in (ActionState.DISPATCHED, ActionState.ESCALATED)
                       and a.deadline_at is not None]
            for action in sorted(pending, key=lambda a: (a.deadline_at, a.action_id)):
                while (action.escalation_level < len(self._ladder)
                       and action.deadline_at is not None
                       and action.deadline_at <= now):
                    level = action.escalation_level + 1
                    deadline = action.dispatched_at + timedelta(
                        seconds=sum(self._ladder[:level + 1]))
                    self._emit(action.storm_id, ACTION_ESCALATED, {
                        "action_id": action.action_id, "level": level,
                        "deadline_at": iso(deadline)})
                    escalations.append({"action_id": action.action_id,
                                        "level": level,
                                        "deadline_at": iso(deadline)})
            return escalations

    # ------------------------------------------------------------------
    # 查询与重放
    # ------------------------------------------------------------------

    def queues(self, storm_id: str | None = None) -> dict:
        """待发令与待回执队列；重启后由事件重放恢复。"""
        with self._lock:
            actions = [a for a in self.actions.values()
                       if storm_id is None or a.storm_id == storm_id]
            pending_dispatch = sorted(
                (a for a in actions if a.state == ActionState.APPROVED),
                key=lambda a: (a.priority, a.approved_at or a.created_at))
            pending_receipt = sorted(
                (a for a in actions
                 if a.state in (ActionState.DISPATCHED, ActionState.ESCALATED)),
                key=lambda a: (a.deadline_at or a.created_at, a.action_id))
            return {
                "pending_dispatch": [self._action_dict(a) for a in pending_dispatch],
                "pending_receipt": [self._action_dict(a) for a in pending_receipt],
            }

    def replay(self, storm_id: str) -> dict:
        """按事件顺序重放完整决策链。"""
        with self._lock:
            self._require_storm(storm_id)
            events = [{
                "seq": e.seq,
                "event_type": e.event_type,
                "occurred_at": iso(e.occurred_at),
                "summary": self._summarize(e),
            } for e in self._events if e.storm_id == storm_id]
            return {"storm_id": storm_id, "event_count": len(events),
                    "events": events}

    def recovery_order(self, storm_id: str) -> dict:
        """各汇水分区恢复次序：全部分区命令回执完成即视为恢复。"""
        with self._lock:
            storm = self._require_storm(storm_id)
            basin_ids = list(dict.fromkeys([
                *storm.basin_ids,
                *[a.basin_id for a in self.actions.values()
                  if a.storm_id == storm_id],
            ]))
            rows = []
            for basin in basin_ids:
                acted = [a for a in self.actions.values()
                         if a.storm_id == storm_id and a.basin_id == basin
                         and a.dispatched_at is not None]
                acked = [a for a in acted if a.state == ActionState.ACKNOWLEDGED]
                if not acted:
                    status, recovered_at = "no_command", None
                elif len(acked) == len(acted):
                    status = "recovered"
                    recovered_at = max(a.acked_at for a in acked)
                else:
                    status, recovered_at = "pending", None
                rows.append({
                    "basin_id": basin, "status": status,
                    "recovered_at": iso(recovered_at) if recovered_at else None,
                    "commands": len(acted), "acknowledged": len(acked)})
            rows.sort(key=lambda r: (
                0 if r["status"] == "recovered" else 1 if r["status"] == "pending" else 2,
                r["recovered_at"] or "", r["basin_id"]))
            rank = 0
            for row in rows:
                if row["status"] == "recovered":
                    rank += 1
                    row["rank"] = rank
                else:
                    row["rank"] = None
            return {"storm_id": storm_id, "basins": rows}

    def storm_status(self, storm_id: str) -> dict:
        with self._lock:
            return self._storm_dict(self._require_storm(storm_id))

    def list_plans(self, storm_id: str) -> list[dict]:
        with self._lock:
            storm = self._require_storm(storm_id)
            return [self._plan_dict(self.plans[pid]) for pid in storm.plan_ids]

    def plan_view(self, plan_id: str) -> dict:
        with self._lock:
            return self._plan_dict(self._require_plan(plan_id))

    def action_view(self, action_id: str) -> dict:
        with self._lock:
            return self._action_dict(self._require_action(action_id))

    def list_devices(self) -> list[dict]:
        with self._lock:
            return [self._device_dict(d) for d in
                    sorted(self.devices.values(), key=lambda d: d.device_id)]

    # ------------------------------------------------------------------
    # 视图与摘要
    # ------------------------------------------------------------------

    def _active_plan(self, storm: Storm) -> Plan | None:
        for plan_id in reversed(storm.plan_ids):
            plan = self.plans[plan_id]
            if plan.state == PlanState.ACTIVE:
                return plan
        return None

    def _storm_dict(self, storm: Storm) -> dict:
        plans = [self.plans[pid] for pid in storm.plan_ids]
        active = self._active_plan(storm)
        counts: dict[str, int] = {}
        for action in self.actions.values():
            if action.storm_id == storm.storm_id:
                key = str(action.state)
                counts[key] = counts.get(key, 0) + 1
        return {
            "storm_id": storm.storm_id, "name": storm.name,
            "basin_ids": list(storm.basin_ids), "rainfall_mm": storm.rainfall_mm,
            "declared_at": iso(storm.declared_at),
            "report_count": len(storm.reports),
            "snapshots": [self._snapshot_dict(s) for s in storm.snapshots],
            "active_plan": active.plan_id if active else None,
            "plan_versions": [p.plan_id for p in plans],
            "action_states": counts,
        }

    @staticmethod
    def _snapshot_dict(snapshot: Snapshot) -> dict:
        return {
            "snapshot_id": snapshot.snapshot_id, "version": snapshot.version,
            "frozen_at": iso(snapshot.frozen_at),
            "report_ids": list(snapshot.report_ids),
            "report_count": len(snapshot.report_ids),
        }

    def _plan_dict(self, plan: Plan) -> dict:
        return {
            "plan_id": plan.plan_id, "storm_id": plan.storm_id,
            "version": plan.version, "snapshot_id": plan.snapshot_id,
            "state": str(plan.state), "created_at": iso(plan.created_at),
            "notes": list(plan.notes),
            "actions": [self._action_dict(self.actions[aid])
                        for aid in plan.action_ids],
        }

    @staticmethod
    def _action_dict(action: Action) -> dict:
        def opt(dt):
            return iso(dt) if dt else None

        return {
            "action_id": action.action_id, "plan_id": action.plan_id,
            "storm_id": action.storm_id, "basin_id": action.basin_id,
            "device_id": action.device_id, "command": str(action.command),
            "priority": action.priority, "reason": action.reason,
            "state": str(action.state),
            "escalation_level": action.escalation_level,
            "created_at": iso(action.created_at),
            "approved_by": action.approved_by,
            "approved_at": opt(action.approved_at),
            "command_id": action.command_id,
            "dispatched_at": opt(action.dispatched_at),
            "deadline_at": opt(action.deadline_at),
            "receipt_id": action.receipt_id,
            "acked_at": opt(action.acked_at),
            "revoked_by": action.revoked_by,
            "revoke_reason": action.revoke_reason,
            "reassignments": list(action.reassignments),
        }

    @staticmethod
    def _device_dict(device: Device) -> dict:
        return {
            "device_id": device.device_id, "basin_id": device.basin_id,
            "kind": str(device.kind), "capacity_m3_min": device.capacity_m3_min,
            "available": device.available, "occupied_by": device.occupied_by,
        }

    @staticmethod
    def _report_dict(report: Report) -> dict:
        return {
            "report_id": report.report_id, "storm_id": report.storm_id,
            "kind": str(report.kind), "basin_id": report.basin_id,
            "observed_at": iso(report.observed_at), "payload": report.payload,
            "ingested_at": iso(report.ingested_at), "late": report.late,
        }

    @staticmethod
    def _summarize(event: Event) -> str:
        p = event.payload
        t = event.event_type
        if t == STORM_DECLARED:
            return f"宣布降雨过程，汇水分区：{'、'.join(p['basin_ids'])}"
        if t == REPORT_INGESTED:
            label = REPORT_KIND_LABELS.get(ReportKind(p["kind"]), p["kind"])
            suffix = "（迟到数据，仅触发新版本方案）" if p.get("late") else ""
            return f"接收{label}上报 {p['report_id']}（分区 {p['basin_id']}）{suffix}"
        if t == SNAPSHOT_FROZEN:
            return f"冻结态势快照 {p['snapshot_id']}（{len(p['report_ids'])} 条上报）"
        if t == PLAN_GENERATED:
            return f"生成方案 {p['plan_id']}（{len(p['actions'])} 条建议动作）"
        if t == PLAN_SUPERSEDED:
            return (f"方案 {p['plan_id']} 被新版本取代，"
                    f"{len(p['superseded_action_ids'])} 条未发令动作作废")
        if t == ACTION_APPROVED:
            return f"{p['by'] or '指挥员'} 批准动作 {p['action_id']}"
        if t == ACTION_REASSIGNED:
            return (f"动作 {p['action_id']} 改派设备 "
                    f"{p['old_device_id']} → {p['new_device_id']}")
        if t == ACTION_REVOKED:
            return f"动作 {p['action_id']} 被 {p['by'] or '指挥员'} 撤销"
        if t == COMMAND_DISPATCHED:
            return f"发令 {p['command_id']}：设备 {p['device_id']} 执行 {p['command']}"
        if t == ACTION_ESCALATED:
            return f"动作 {p['action_id']} 超时未回执，升级为 {p['level']} 级"
        if t == RECEIPT_RECORDED:
            label = "完成" if p["status"] == RECEIPT_DONE else "失败"
            return f"收到回执 {p['receipt_id']}：动作 {p['action_id']} 执行{label}"
        return t
