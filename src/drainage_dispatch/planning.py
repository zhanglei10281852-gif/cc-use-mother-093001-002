"""方案生成：依据汇水分区容量、设备约束与道路风险生成建议动作。

生成过程是纯函数：输入快照内的上报、设备注册表与当前占用集合，
输出确定性的建议动作序列，同一快照重放结果一致。
"""
from __future__ import annotations

from dataclasses import dataclass

from .contracts import CommandAction
from .models import Device, DeviceKind, Report, ReportKind


@dataclass(frozen=True)
class BasinAssessment:
    basin_id: str
    demand_m3: float       # 待排水量（积水点体积 + 管段超蓄）
    road_risk: int         # 道路风险等级（取上报最大值，0 表示无上报）
    waterlogging_points: int


@dataclass(frozen=True)
class SuggestedAction:
    device_id: str
    basin_id: str
    command: CommandAction
    priority: int
    reason: str


def assess_basins(reports: list[Report], basin_ids: tuple[str, ...]) -> dict[str, BasinAssessment]:
    """汇总快照内上报，估算各分区待排水量与道路风险。"""
    demand = {b: 0.0 for b in basin_ids}
    risk = {b: 0 for b in basin_ids}
    points = {b: 0 for b in basin_ids}
    for report in reports:
        demand.setdefault(report.basin_id, 0.0)
        risk.setdefault(report.basin_id, 0)
        points.setdefault(report.basin_id, 0)
        if report.kind == ReportKind.WATERLOGGING:
            depth = float(report.payload.get("depth_m", 0.0))
            area = float(report.payload.get("area_m2", 0.0))
            demand[report.basin_id] += depth * area
            points[report.basin_id] += 1
        elif report.kind == ReportKind.PIPE_LEVEL:
            demand[report.basin_id] += float(report.payload.get("excess_m3", 0.0))
        elif report.kind == ReportKind.ROAD_RISK:
            level = int(report.payload.get("risk_level", 0))
            risk[report.basin_id] = max(risk[report.basin_id], level)
    return {
        b: BasinAssessment(b, demand[b], risk[b], points[b]) for b in demand
    }


def suggest_actions(
    assessments: dict[str, BasinAssessment],
    devices: list[Device],
    occupied_ids: set[str],
    fault_ids: set[str],
    window_min: float,
) -> tuple[list[SuggestedAction], list[str]]:
    """按道路风险优先排序，为每个分区挑选可用设备直至覆盖需求。

    设备约束：已占用、故障上报、登记不可用的设备一律不参与；
    泵优先于闸门，同类型按能力降序。能力缺口写入 notes 供升级研判。
    """
    suggestions: list[SuggestedAction] = []
    notes: list[str] = []
    ordered = sorted(
        assessments.values(),
        key=lambda a: (-a.road_risk, -a.demand_m3, a.basin_id),
    )
    priority = 0
    for basin in ordered:
        if basin.demand_m3 <= 0:
            if basin.road_risk > 0:
                notes.append(
                    f"分区 {basin.basin_id} 道路风险 {basin.road_risk} 级"
                    "但暂无积水上报，安排巡查"
                )
            continue
        needed_rate = basin.demand_m3 / window_min
        candidates = sorted(
            (d for d in devices
             if d.basin_id == basin.basin_id and d.available
             and d.device_id not in occupied_ids and d.device_id not in fault_ids),
            key=lambda d: (0 if d.kind == DeviceKind.PUMP else 1,
                           -d.capacity_m3_min, d.device_id),
        )
        covered = 0.0
        for device in candidates:
            if covered >= needed_rate:
                break
            priority += 1
            suggestions.append(SuggestedAction(
                device_id=device.device_id,
                basin_id=basin.basin_id,
                command=CommandAction.START,
                priority=priority,
                reason=(
                    f"分区{basin.basin_id}待排{basin.demand_m3:.0f}m³，"
                    f"道路风险{basin.road_risk}级，"
                    f"启用{device.device_id}（{device.capacity_m3_min:.0f}m³/min）"
                ),
            ))
            covered += device.capacity_m3_min
        if covered < needed_rate:
            gap = (needed_rate - covered) * window_min
            notes.append(
                f"分区 {basin.basin_id} 排水能力缺口约 {gap:.0f}m³，建议请求区级支援"
            )
    return suggestions, notes
