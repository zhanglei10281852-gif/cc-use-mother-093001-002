"""运行时领域模型：上报、告警、快照、方案、动作、命令、回执。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from .clock import fmt_dt, parse_dt
from .contracts import CommandAction


class ReportKind(StrEnum):
    WATERLOGGING = "waterlogging"  # 积水点
    PUMP_STATUS = "pump_status"  # 泵站运行状态
    GATE_STATUS = "gate_status"  # 闸门状态
    PIPE_LEVEL = "pipe_level"  # 管段水位


class AlertSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class ActionStatus(StrEnum):
    PROPOSED = "proposed"  # 方案建议，待审批
    APPROVED = "approved"  # 指挥员已审批，待发令
    DISPATCHED = "dispatched"  # 已发令，待现场回执
    ACKNOWLEDGED = "acknowledged"  # 现场已接收
    COMPLETED = "completed"  # 现场已完成
    FAILED = "failed"  # 现场执行失败
    REVOKED = "revoked"  # 发令前被指挥员撤销
    SUPERSEDED = "superseded"  # 被新版本方案取代


class CommandStatus(StrEnum):
    ISSUED = "issued"
    ACKNOWLEDGED = "acknowledged"
    COMPLETED = "completed"
    FAILED = "failed"


class ReceiptKind(StrEnum):
    ACCEPTED = "accepted"  # 现场已接单
    COMPLETED = "completed"  # 执行完成
    FAILED = "failed"  # 执行失败


class Phase(StrEnum):
    RESPONSE = "response"  # 降雨应对
    RECOVERY = "recovery"  # 恢复退场


TERMINAL_COMMAND_STATES = (CommandStatus.COMPLETED, CommandStatus.FAILED)
ACTIVE_COMMAND_STATES = (CommandStatus.ISSUED, CommandStatus.ACKNOWLEDGED)


def _dt(value):
    return parse_dt(value) if value is not None else None


@dataclass
class TelemetryReport:
    report_id: str
    storm_id: str
    kind: ReportKind
    basin_id: str
    subject_id: str  # 积水点 / 设备 / 管段编号
    observed_at: object  # 现场观测时刻（可能迟到）
    received_at: object  # 系统接收时刻
    metrics: dict = field(default_factory=dict)
    late: bool = False  # 观测时刻早于已冻结快照的数据截止时间

    def to_dict(self) -> dict:
        return {
            "report_id": self.report_id,
            "storm_id": self.storm_id,
            "kind": str(self.kind),
            "basin_id": self.basin_id,
            "subject_id": self.subject_id,
            "observed_at": fmt_dt(self.observed_at),
            "received_at": fmt_dt(self.received_at),
            "metrics": dict(self.metrics),
            "late": self.late,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "TelemetryReport":
        return cls(
            report_id=raw["report_id"],
            storm_id=raw["storm_id"],
            kind=ReportKind(raw["kind"]),
            basin_id=raw["basin_id"],
            subject_id=raw["subject_id"],
            observed_at=_dt(raw["observed_at"]),
            received_at=_dt(raw["received_at"]),
            metrics=dict(raw.get("metrics", {})),
            late=bool(raw.get("late", False)),
        )


@dataclass
class Alert:
    alert_id: str
    storm_id: str
    basin_id: str
    kind: str
    severity: AlertSeverity
    message: str
    raised_at: object

    def to_dict(self) -> dict:
        return {
            "alert_id": self.alert_id,
            "storm_id": self.storm_id,
            "basin_id": self.basin_id,
            "kind": self.kind,
            "severity": str(self.severity),
            "message": self.message,
            "raised_at": fmt_dt(self.raised_at),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Alert":
        return cls(
            alert_id=raw["alert_id"],
            storm_id=raw["storm_id"],
            basin_id=raw["basin_id"],
            kind=raw["kind"],
            severity=AlertSeverity(raw["severity"]),
            message=raw["message"],
            raised_at=_dt(raw["raised_at"]),
        )


@dataclass
class BasinView:
    """冻结时刻某分区的态势视图。"""

    basin_id: str
    pending_volume_m3: float = 0.0
    pipe_level_m: float | None = None
    running_devices: tuple = ()
    subjects: tuple = ()

    def to_dict(self) -> dict:
        return {
            "basin_id": self.basin_id,
            "pending_volume_m3": round(self.pending_volume_m3, 3),
            "pipe_level_m": self.pipe_level_m,
            "running_devices": list(self.running_devices),
            "subjects": list(self.subjects),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "BasinView":
        return cls(
            basin_id=raw["basin_id"],
            pending_volume_m3=float(raw.get("pending_volume_m3", 0.0)),
            pipe_level_m=raw.get("pipe_level_m"),
            running_devices=tuple(raw.get("running_devices", ())),
            subjects=tuple(raw.get("subjects", ())),
        )


@dataclass
class FrozenSnapshot:
    """一次降雨过程在某个决策点的冻结态势，不可变、可追溯。"""

    snapshot_id: str
    storm_id: str
    version: int
    frozen_at: object
    data_cutoff: object | None  # 纳入本快照的最大观测时刻
    basins: dict = field(default_factory=dict)  # basin_id -> BasinView

    def to_dict(self) -> dict:
        return {
            "snapshot_id": self.snapshot_id,
            "storm_id": self.storm_id,
            "version": self.version,
            "frozen_at": fmt_dt(self.frozen_at),
            "data_cutoff": fmt_dt(self.data_cutoff),
            "basins": {k: v.to_dict() for k, v in self.basins.items()},
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "FrozenSnapshot":
        return cls(
            snapshot_id=raw["snapshot_id"],
            storm_id=raw["storm_id"],
            version=int(raw["version"]),
            frozen_at=_dt(raw["frozen_at"]),
            data_cutoff=_dt(raw.get("data_cutoff")),
            basins={k: BasinView.from_dict(v) for k, v in raw.get("basins", {}).items()},
        )


@dataclass
class SuggestedAction:
    action_id: str
    storm_id: str
    plan_version: int
    seq: int
    basin_id: str
    device_id: str
    command: CommandAction
    priority: int  # 1 最高
    phase: Phase
    reasons: tuple = ()
    status: ActionStatus = ActionStatus.PROPOSED
    approved_by: str | None = None
    approved_at: object | None = None
    command_id: str | None = None
    revoke_reason: str | None = None
    reassigned_from: str | None = None

    def to_dict(self) -> dict:
        return {
            "action_id": self.action_id,
            "storm_id": self.storm_id,
            "plan_version": self.plan_version,
            "seq": self.seq,
            "basin_id": self.basin_id,
            "device_id": self.device_id,
            "command": str(self.command),
            "priority": self.priority,
            "phase": str(self.phase),
            "reasons": list(self.reasons),
            "status": str(self.status),
            "approved_by": self.approved_by,
            "approved_at": fmt_dt(self.approved_at),
            "command_id": self.command_id,
            "revoke_reason": self.revoke_reason,
            "reassigned_from": self.reassigned_from,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "SuggestedAction":
        return cls(
            action_id=raw["action_id"],
            storm_id=raw["storm_id"],
            plan_version=int(raw["plan_version"]),
            seq=int(raw["seq"]),
            basin_id=raw["basin_id"],
            device_id=raw["device_id"],
            command=CommandAction(raw["command"]),
            priority=int(raw["priority"]),
            phase=Phase(raw["phase"]),
            reasons=tuple(raw.get("reasons", ())),
            status=ActionStatus(raw.get("status", ActionStatus.PROPOSED)),
            approved_by=raw.get("approved_by"),
            approved_at=_dt(raw.get("approved_at")),
            command_id=raw.get("command_id"),
            revoke_reason=raw.get("revoke_reason"),
            reassigned_from=raw.get("reassigned_from"),
        )


@dataclass
class PlanVersion:
    plan_id: str
    storm_id: str
    version: int
    snapshot_id: str
    created_at: object
    action_ids: tuple = ()
    notes: tuple = ()
    status: str = "active"  # active / superseded

    def to_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "storm_id": self.storm_id,
            "version": self.version,
            "snapshot_id": self.snapshot_id,
            "created_at": fmt_dt(self.created_at),
            "action_ids": list(self.action_ids),
            "notes": list(self.notes),
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "PlanVersion":
        return cls(
            plan_id=raw["plan_id"],
            storm_id=raw["storm_id"],
            version=int(raw["version"]),
            snapshot_id=raw["snapshot_id"],
            created_at=_dt(raw["created_at"]),
            action_ids=tuple(raw.get("action_ids", ())),
            notes=tuple(raw.get("notes", ())),
            status=raw.get("status", "active"),
        )


@dataclass
class Command:
    """已发出的调度命令：一经发出不可改写。"""

    command_id: str
    action_id: str
    storm_id: str
    basin_id: str
    device_id: str
    command: CommandAction
    issued_at: object
    ack_deadline: object
    status: CommandStatus = CommandStatus.ISSUED
    escalation_level: int = 0
    completed_at: object | None = None
    last_escalated_at: object | None = None

    def to_dict(self) -> dict:
        return {
            "command_id": self.command_id,
            "action_id": self.action_id,
            "storm_id": self.storm_id,
            "basin_id": self.basin_id,
            "device_id": self.device_id,
            "command": str(self.command),
            "issued_at": fmt_dt(self.issued_at),
            "ack_deadline": fmt_dt(self.ack_deadline),
            "status": str(self.status),
            "escalation_level": self.escalation_level,
            "completed_at": fmt_dt(self.completed_at),
            "last_escalated_at": fmt_dt(self.last_escalated_at),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Command":
        return cls(
            command_id=raw["command_id"],
            action_id=raw["action_id"],
            storm_id=raw["storm_id"],
            basin_id=raw["basin_id"],
            device_id=raw["device_id"],
            command=CommandAction(raw["command"]),
            issued_at=_dt(raw["issued_at"]),
            ack_deadline=_dt(raw["ack_deadline"]),
            status=CommandStatus(raw.get("status", CommandStatus.ISSUED)),
            escalation_level=int(raw.get("escalation_level", 0)),
            completed_at=_dt(raw.get("completed_at")),
            last_escalated_at=_dt(raw.get("last_escalated_at")),
        )


@dataclass
class Receipt:
    receipt_id: str
    command_id: str
    action_id: str
    storm_id: str
    kind: ReceiptKind
    note: str
    received_at: object
    resulting_status: CommandStatus

    def to_dict(self) -> dict:
        return {
            "receipt_id": self.receipt_id,
            "command_id": self.command_id,
            "action_id": self.action_id,
            "storm_id": self.storm_id,
            "kind": str(self.kind),
            "note": self.note,
            "received_at": fmt_dt(self.received_at),
            "resulting_status": str(self.resulting_status),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Receipt":
        return cls(
            receipt_id=raw["receipt_id"],
            command_id=raw["command_id"],
            action_id=raw["action_id"],
            storm_id=raw["storm_id"],
            kind=ReceiptKind(raw["kind"]),
            note=raw.get("note", ""),
            received_at=_dt(raw["received_at"]),
            resulting_status=CommandStatus(raw["resulting_status"]),
        )
