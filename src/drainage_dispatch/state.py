"""应用状态：由事件流折叠而成，进程重启后重放事件即可恢复。"""
from __future__ import annotations

from dataclasses import dataclass, field

from .clock import parse_dt
from .models import (
    ActionStatus,
    Alert,
    Command,
    CommandStatus,
    FrozenSnapshot,
    PlanVersion,
    Receipt,
    ReceiptKind,
    SuggestedAction,
    TelemetryReport,
)


@dataclass
class StormState:
    storm_id: str
    rainfall_mm: float
    basin_ids: tuple
    opened_at: object
    reports: dict = field(default_factory=dict)  # report_id -> TelemetryReport
    latest_by_subject: dict = field(default_factory=dict)  # subject_id -> TelemetryReport
    alerts: dict = field(default_factory=dict)  # alert_id -> Alert
    snapshots: dict = field(default_factory=dict)  # version -> FrozenSnapshot
    plans: dict = field(default_factory=dict)  # version -> PlanVersion
    actions: dict = field(default_factory=dict)  # action_id -> SuggestedAction
    current_plan_version: int = 0
    working_cutoff: object | None = None  # 工作快照已纳入的最大观测时刻
    last_freeze_seq: int = 0
    last_change_seq: int = 0

    @property
    def plan_stale(self) -> bool:
        """上次冻结之后又有新数据，方案需要出新版本。"""
        return self.last_change_seq > self.last_freeze_seq


class AppState:
    def __init__(self) -> None:
        self.storms: dict[str, StormState] = {}
        self.commands: dict[str, Command] = {}
        self.receipts: dict[str, Receipt] = {}

    # ---- 查询辅助 ----
    def active_commands_by_device(self) -> dict[str, Command]:
        return {
            c.device_id: c
            for c in self.commands.values()
            if c.status in (CommandStatus.ISSUED, CommandStatus.ACKNOWLEDGED)
        }

    def running_devices(self) -> set[str]:
        """由命令历史推断的设备运行态：START 完成即运行，STOP 完成即停机。"""
        from .contracts import CommandAction

        running: set[str] = set()
        by_device: dict[str, list[Command]] = {}
        for c in self.commands.values():
            by_device.setdefault(c.device_id, []).append(c)
        for device_id, cmds in by_device.items():
            state = False
            for c in sorted(cmds, key=lambda c: (c.issued_at, c.command_id)):
                if c.status == CommandStatus.COMPLETED:
                    state = c.command == CommandAction.START
                elif c.status in (CommandStatus.ISSUED, CommandStatus.ACKNOWLEDGED) \
                        and c.command == CommandAction.START:
                    state = True  # 启动命令在执行，视为运行
            if state:
                running.add(device_id)
        return running

    def find_action(self, action_id: str) -> SuggestedAction | None:
        for storm in self.storms.values():
            if action_id in storm.actions:
                return storm.actions[action_id]
        return None

    # ---- 事件折叠 ----
    def apply(self, event: dict) -> None:
        handler = getattr(self, f"_on_{event['type']}", None)
        if handler is None:  # pragma: no cover - 防御未知事件
            raise ValueError(f"未知事件类型: {event['type']}")
        handler(event["payload"], event["seq"])

    def _on_StormOpened(self, p: dict, seq: int) -> None:
        self.storms[p["storm_id"]] = StormState(
            storm_id=p["storm_id"],
            rainfall_mm=float(p["rainfall_mm"]),
            basin_ids=tuple(p["basin_ids"]),
            opened_at=parse_dt(p["opened_at"]),
        )

    def _on_ReportReceived(self, p: dict, seq: int) -> None:
        report = TelemetryReport.from_dict(p["report"])
        storm = self.storms[report.storm_id]
        storm.reports[report.report_id] = report
        current = storm.latest_by_subject.get(report.subject_id)
        if current is None or report.observed_at >= current.observed_at:
            storm.latest_by_subject[report.subject_id] = report
        if storm.working_cutoff is None or report.observed_at > storm.working_cutoff:
            storm.working_cutoff = report.observed_at
        storm.last_change_seq = seq

    def _on_AlertRaised(self, p: dict, seq: int) -> None:
        alert = Alert.from_dict(p["alert"])
        storm = self.storms[alert.storm_id]
        storm.alerts[alert.alert_id] = alert
        storm.last_change_seq = seq

    def _on_SnapshotFrozen(self, p: dict, seq: int) -> None:
        snap = FrozenSnapshot.from_dict(p["snapshot"])
        storm = self.storms[snap.storm_id]
        storm.snapshots[snap.version] = snap
        storm.last_freeze_seq = seq

    def _on_PlanGenerated(self, p: dict, seq: int) -> None:
        plan = PlanVersion.from_dict(p["plan"])
        storm = self.storms[plan.storm_id]
        storm.plans[plan.version] = plan
        storm.current_plan_version = plan.version
        for raw in p.get("actions", []):
            action = SuggestedAction.from_dict(raw)
            action.status = ActionStatus.PROPOSED
            storm.actions[action.action_id] = action

    def _on_ActionsSuperseded(self, p: dict, seq: int) -> None:
        storm = self.storms[p["storm_id"]]
        for action_id in p["action_ids"]:
            storm.actions[action_id].status = ActionStatus.SUPERSEDED
        for plan in storm.plans.values():
            if plan.version < storm.current_plan_version:
                plan.status = "superseded"

    def _on_ActionApproved(self, p: dict, seq: int) -> None:
        action = self.storms[p["storm_id"]].actions[p["action_id"]]
        action.status = ActionStatus.APPROVED
        action.approved_by = p["commander"]
        action.approved_at = parse_dt(p["approved_at"])

    def _on_ActionReassigned(self, p: dict, seq: int) -> None:
        action = self.storms[p["storm_id"]].actions[p["action_id"]]
        action.reassigned_from = p["from_device"]
        action.device_id = p["to_device"]

    def _on_ActionRevoked(self, p: dict, seq: int) -> None:
        action = self.storms[p["storm_id"]].actions[p["action_id"]]
        action.status = ActionStatus.REVOKED
        action.revoke_reason = p.get("reason", "")

    def _on_CommandDispatched(self, p: dict, seq: int) -> None:
        command = Command.from_dict(p["command"])
        self.commands[command.command_id] = command
        action = self.storms[command.storm_id].actions[command.action_id]
        action.status = ActionStatus.DISPATCHED
        action.command_id = command.command_id

    def _on_ReceiptAccepted(self, p: dict, seq: int) -> None:
        receipt = Receipt.from_dict(p["receipt"])
        self.receipts[receipt.receipt_id] = receipt
        command = self.commands[receipt.command_id]
        command.status = receipt.resulting_status
        if receipt.resulting_status == CommandStatus.COMPLETED:
            command.completed_at = receipt.received_at
        action = self.storms[receipt.storm_id].actions[receipt.action_id]
        action.status = {
            CommandStatus.ACKNOWLEDGED: ActionStatus.ACKNOWLEDGED,
            CommandStatus.COMPLETED: ActionStatus.COMPLETED,
            CommandStatus.FAILED: ActionStatus.FAILED,
        }[receipt.resulting_status]

    def _on_CommandEscalated(self, p: dict, seq: int) -> None:
        command = self.commands[p["command_id"]]
        command.escalation_level = int(p["level"])
        command.ack_deadline = parse_dt(p["deadline"])
        command.last_escalated_at = parse_dt(p["escalated_at"])
