"""汇水分区与排涝设备的静态注册表（容量、约束、道路风险）。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ValidationError

RISK_LEVELS = ("high", "medium", "low")


@dataclass(frozen=True)
class BasinSpec:
    basin_id: str
    area_km2: float
    storage_capacity_m3: float
    road_risk: str  # high / medium / low
    priority: int  # 数值越小越优先恢复


@dataclass(frozen=True)
class DeviceSpec:
    device_id: str
    basin_id: str
    kind: str  # pump / gate
    capacity_m3_min: float
    interlock_group: str | None = None  # 同组设备不允许同时处于启动命令


@dataclass(frozen=True)
class Registry:
    basins: dict[str, BasinSpec]
    devices: dict[str, DeviceSpec]
    drain_windows_minutes: dict[str, float]
    pipe_critical_level_m: dict[str, float]
    ack_timeout_minutes: float = 15.0
    escalation_intervals_minutes: tuple[float, ...] = (15.0, 30.0)
    drain_epsilon_m3: float = 1.0

    def basin(self, basin_id: str) -> BasinSpec:
        try:
            return self.basins[basin_id]
        except KeyError:
            raise ValidationError(f"未知汇水分区: {basin_id}") from None

    def device(self, device_id: str) -> DeviceSpec:
        try:
            return self.devices[device_id]
        except KeyError:
            raise ValidationError(f"未知排涝设备: {device_id}") from None

    def window_for(self, basin_id: str) -> float:
        risk = self.basin(basin_id).road_risk
        return float(self.drain_windows_minutes.get(risk, 60.0))

    def critical_level(self, basin_id: str) -> float:
        return float(self.pipe_critical_level_m.get(basin_id, 3.0))

    def basin_devices(self, basin_id: str, kind: str | None = None) -> list[DeviceSpec]:
        found = [d for d in self.devices.values() if d.basin_id == basin_id]
        if kind:
            found = [d for d in found if d.kind == kind]
        return sorted(found, key=lambda d: (-d.capacity_m3_min, d.device_id))

    def escalation_interval(self, level: int) -> float:
        """第 level 次升级后的下一次确认宽限（分钟）。"""
        if not self.escalation_intervals_minutes:
            return self.ack_timeout_minutes
        idx = min(level - 1, len(self.escalation_intervals_minutes) - 1)
        return float(self.escalation_intervals_minutes[idx])

    @classmethod
    def from_dict(cls, raw: dict) -> "Registry":
        basins = {b["basin_id"]: BasinSpec(**b) for b in raw.get("basins", [])}
        devices = {d["device_id"]: DeviceSpec(**d) for d in raw.get("devices", [])}
        for dev in devices.values():
            if dev.basin_id not in basins:
                raise ValidationError(f"设备 {dev.device_id} 指向未知分区 {dev.basin_id}")
        windows = raw.get("drain_windows_minutes") or {"high": 30, "medium": 60, "low": 120}
        for b in basins.values():
            if b.road_risk not in windows:
                raise ValidationError(f"分区 {b.basin_id} 的道路风险等级缺少排水窗口配置")
        return cls(
            basins=basins,
            devices=devices,
            drain_windows_minutes={k: float(v) for k, v in windows.items()},
            pipe_critical_level_m={k: float(v) for k, v in raw.get("pipe_critical_level_m", {}).items()},
            ack_timeout_minutes=float(raw.get("ack_timeout_minutes", 15)),
            escalation_intervals_minutes=tuple(float(x) for x in raw.get("escalation_intervals_minutes", (15, 30))),
            drain_epsilon_m3=float(raw.get("drain_epsilon_m3", 1.0)),
        )

    @classmethod
    def load(cls, path: str | Path) -> "Registry":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    @classmethod
    def default(cls) -> "Registry":
        """内置示例注册表，与 config/registry.json 保持一致。"""
        return cls.from_dict(
            {
                "ack_timeout_minutes": 15,
                "escalation_intervals_minutes": [15, 30],
                "drain_windows_minutes": {"high": 30, "medium": 60, "low": 120},
                "pipe_critical_level_m": {"BASIN-A": 3.5, "BASIN-B": 3.0, "BASIN-C": 4.0},
                "basins": [
                    {"basin_id": "BASIN-A", "area_km2": 2.4, "storage_capacity_m3": 18000, "road_risk": "high", "priority": 1},
                    {"basin_id": "BASIN-B", "area_km2": 1.6, "storage_capacity_m3": 12000, "road_risk": "medium", "priority": 2},
                    {"basin_id": "BASIN-C", "area_km2": 3.1, "storage_capacity_m3": 26000, "road_risk": "low", "priority": 3},
                ],
                "devices": [
                    {"device_id": "PUMP-1", "basin_id": "BASIN-A", "kind": "pump", "capacity_m3_min": 22.0, "interlock_group": "A-1"},
                    {"device_id": "PUMP-2", "basin_id": "BASIN-A", "kind": "pump", "capacity_m3_min": 18.0},
                    {"device_id": "GATE-1", "basin_id": "BASIN-A", "kind": "gate", "capacity_m3_min": 30.0, "interlock_group": "A-1"},
                    {"device_id": "PUMP-3", "basin_id": "BASIN-B", "kind": "pump", "capacity_m3_min": 18.0},
                    {"device_id": "GATE-2", "basin_id": "BASIN-B", "kind": "gate", "capacity_m3_min": 25.0},
                    {"device_id": "PUMP-4", "basin_id": "BASIN-C", "kind": "pump", "capacity_m3_min": 20.0},
                ],
            }
        )
