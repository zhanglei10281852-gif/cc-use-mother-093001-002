"""调度领域模型：上报、快照、方案、动作与设备占用。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from .contracts import CommandAction


class ReportKind(StrEnum):
    WATERLOGGING = "waterlogging"  # 积水点
    PUMP_STATUS = "pump_status"    # 泵站状态
    GATE_STATUS = "gate_status"    # 闸门状态
    PIPE_LEVEL = "pipe_level"      # 管段水位
    ROAD_RISK = "road_risk"        # 道路风险


REPORT_KIND_LABELS = {
    ReportKind.WATERLOGGING: "积水点",
    ReportKind.PUMP_STATUS: "泵站",
    ReportKind.GATE_STATUS: "闸门",
    ReportKind.PIPE_LEVEL: "管段水位",
    ReportKind.ROAD_RISK: "道路风险",
}


class DeviceKind(StrEnum):
    PUMP = "pump"
    GATE = "gate"


class ActionState(StrEnum):
    PROPOSED = "proposed"          # 已建议，待审批
    APPROVED = "approved"          # 已审批，待发令（设备已预占）
    DISPATCHED = "dispatched"      # 已发令，待回执
    ESCALATED = "escalated"        # 超时未回执，已升级
    ACKNOWLEDGED = "acknowledged"  # 现场已回执完成
    FAILED = "failed"              # 现场回执失败
    REVOKED = "revoked"            # 发令前被撤销
    SUPERSEDED = "superseded"      # 被新版本方案取代


# 处于这些状态的动作视为占用设备，冲突命令必须在发令前拦截
OCCUPYING_STATES = frozenset(
    {ActionState.APPROVED, ActionState.DISPATCHED, ActionState.ESCALATED}
)


class PlanState(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"


@dataclass
class Report:
    report_id: str
    storm_id: str
    kind: ReportKind
    basin_id: str
    observed_at: datetime
    payload: dict
    ingested_at: datetime
    late: bool = False  # 观测时间早于最近快照冻结时刻，只能触发新版本方案


@dataclass
class Snapshot:
    snapshot_id: str
    storm_id: str
    version: int
    report_ids: tuple[str, ...]
    frozen_at: datetime


@dataclass
class Action:
    action_id: str
    plan_id: str
    storm_id: str
    basin_id: str
    device_id: str
    command: CommandAction
    priority: int
    reason: str
    created_at: datetime
    state: ActionState = ActionState.PROPOSED
    escalation_level: int = 0
    approved_by: str | None = None
    approved_at: datetime | None = None
    command_id: str | None = None
    dispatched_at: datetime | None = None
    deadline_at: datetime | None = None
    receipt_id: str | None = None
    acked_at: datetime | None = None
    revoked_by: str | None = None
    revoke_reason: str = ""
    reassignments: list[dict] = field(default_factory=list)


@dataclass
class Plan:
    plan_id: str
    storm_id: str
    version: int
    snapshot_id: str
    created_at: datetime
    action_ids: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    state: PlanState = PlanState.ACTIVE


@dataclass
class Device:
    device_id: str
    basin_id: str
    kind: DeviceKind
    capacity_m3_min: float
    available: bool = True
    occupied_by: str | None = None  # 当前占用设备的动作 id


@dataclass
class Storm:
    storm_id: str
    name: str
    basin_ids: tuple[str, ...]
    rainfall_mm: float | None
    declared_at: datetime
    reports: dict[str, Report] = field(default_factory=dict)
    snapshots: list[Snapshot] = field(default_factory=list)
    plan_ids: list[str] = field(default_factory=list)
