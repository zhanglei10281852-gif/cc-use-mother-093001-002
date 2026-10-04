"""规则化调度方案生成器。

依据汇水分区容量、设备约束（能力、联锁、占用）和道路风险，
把冻结快照翻译成可审批的建议动作。所有规则确定性输出，便于审计与重放。
"""
from __future__ import annotations

from .contracts import CommandAction
from .models import BasinView, Phase, SuggestedAction, ActionStatus
from .registry import Registry

RISK_PRIORITY = {"high": 1, "medium": 2, "low": 3}


def build_plan_actions(
    storm_id: str,
    plan_version: int,
    views: dict[str, BasinView],
    registry: Registry,
    running_devices: set[str],
) -> tuple[list[SuggestedAction], list[str]]:
    """根据各分区态势视图生成建议动作，返回 (动作列表, 方案备注)。

    running_devices：据命令历史与现场上报推断的运行中设备。
    设备占用与联锁的强制拦截在发令环节执行，这里只做方案内去重。
    """
    candidates: list[dict] = []
    notes: list[str] = []
    planned_start_groups: set[str] = set()

    def try_start(dev, basin_id, priority, phase, reason) -> None:
        """START 建议需过运行态与方案内联锁两道闸。"""
        if dev.device_id in running_devices:
            notes.append(f"{dev.device_id} 已在运行或已有启动命令在执行，跳过重复启动建议")
            return
        if dev.interlock_group and dev.interlock_group in planned_start_groups:
            notes.append(f"{dev.device_id} 与本方案中已建议启动的设备同组联锁({dev.interlock_group})，跳过")
            return
        candidates.append(
            {"basin_id": basin_id, "device_id": dev.device_id, "command": CommandAction.START,
             "priority": priority, "phase": phase, "reasons": (reason,)}
        )
        if dev.interlock_group:
            planned_start_groups.add(dev.interlock_group)

    for basin_id in sorted(views, key=lambda b: (registry.basin(b).priority, b)):
        view = views[basin_id]
        spec = registry.basin(basin_id)
        pumps = registry.basin_devices(basin_id, kind="pump")
        gates = registry.basin_devices(basin_id, kind="gate")
        window = registry.window_for(basin_id)
        critical = registry.critical_level(basin_id)
        volume = view.pending_volume_m3
        risk_prio = RISK_PRIORITY.get(spec.road_risk, 2)
        level = view.pipe_level_m

        backflow = level is not None and level >= critical
        warn = level is not None and not backflow and level >= 0.8 * critical

        if volume > registry.drain_epsilon_m3:
            required_flow = volume / window
            total_pump = sum(p.capacity_m3_min for p in pumps)
            overflow = required_flow > total_pump and total_pump > 0
            if volume > spec.storage_capacity_m3:
                notes.append(
                    f"{basin_id} 待排水量 {volume:.0f}m³ 超过调蓄容量 {spec.storage_capacity_m3:.0f}m³，存在漫溢风险"
                )
            if backflow:
                for gate in gates:
                    candidates.append(
                        {"basin_id": basin_id, "device_id": gate.device_id, "command": CommandAction.STOP,
                         "priority": 1, "phase": Phase.RESPONSE,
                         "reasons": (f"R3-防倒灌: 管段水位 {level:.2f}m ≥ 临界 {critical:.2f}m，关闭闸门",)})
                selected = pumps  # 重力排涝失效，全部泵投入
                reason = f"R3-防倒灌: 管段水位 {level:.2f}m，改泵排，需求流量 {required_flow:.1f}m³/min"
                for p in selected:
                    try_start(p, basin_id, 1, Phase.RESPONSE, reason)
            elif overflow:
                reason = (
                    f"R1-超能力: 待排 {volume:.0f}m³/{window:.0f}min 需求 {required_flow:.1f}m³/min "
                    f"> 泵组能力 {total_pump:.1f}m³/min，全部投入"
                )
                for p in pumps:
                    try_start(p, basin_id, 1, Phase.RESPONSE, reason)
            else:
                # R2-常规排水：按能力从大到小贪心选取，覆盖需求流量即可
                selected, covered = [], 0.0
                for p in pumps:
                    if covered >= required_flow:
                        break
                    selected.append(p)
                    covered += p.capacity_m3_min
                reason = (
                    f"R2-排水: 待排 {volume:.0f}m³，道路风险 {spec.road_risk} 窗口 {window:.0f}min，"
                    f"需求流量 {required_flow:.1f}m³/min"
                )
                for p in selected:
                    try_start(p, basin_id, risk_prio, Phase.RESPONSE, reason)
                # R5-重力辅助：管段水位低时开闸分担
                if gates and (level is None or level < 0.5 * critical):
                    gate = gates[0]
                    try_start(gate, basin_id, 2, Phase.RESPONSE,
                              f"R5-重力辅助: 管段水位安全，开启 {gate.device_id} 分担流量")
            if warn:
                for gate in gates:
                    candidates.append(
                        {"basin_id": basin_id, "device_id": gate.device_id, "command": CommandAction.HOLD,
                         "priority": 2, "phase": Phase.RESPONSE,
                         "reasons": (f"R6-水位警戒: 管段水位 {level:.2f}m 接近临界 {critical:.2f}m，闸门保持",)})
        else:
            # R4-恢复：待排尽，将在运行设备有序退场
            running = {d for d in running_devices
                       if d in registry.devices and registry.devices[d].basin_id == basin_id}
            for dev_id in sorted(running):
                candidates.append(
                    {"basin_id": basin_id, "device_id": dev_id, "command": CommandAction.STOP,
                     "priority": 3, "phase": Phase.RECOVERY,
                     "reasons": (f"R4-恢复: {basin_id} 待排水量已降至 {volume:.1f}m³ 以下，{dev_id} 有序停机",)})

    candidates.sort(key=lambda c: (c["priority"], registry.basin(c["basin_id"]).priority,
                                   c["basin_id"], c["device_id"]))
    actions: list[SuggestedAction] = []
    plan_id = f"{storm_id}-PLAN-v{plan_version}"
    for seq, cand in enumerate(candidates, start=1):
        actions.append(
            SuggestedAction(
                action_id=f"{plan_id}-A{seq:02d}",
                storm_id=storm_id,
                plan_version=plan_version,
                seq=seq,
                basin_id=cand["basin_id"],
                device_id=cand["device_id"],
                command=cand["command"],
                priority=cand["priority"],
                phase=cand["phase"],
                reasons=cand["reasons"],
                status=ActionStatus.PROPOSED,
            )
        )
    return actions, notes
