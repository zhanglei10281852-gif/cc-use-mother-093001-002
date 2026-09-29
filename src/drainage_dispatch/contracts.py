"""降雨态势和排涝设备的基础契约。"""
from dataclasses import dataclass
from enum import StrEnum


class CommandAction(StrEnum):
    START = "start"
    STOP = "stop"
    HOLD = "hold"


@dataclass(frozen=True)
class StormSnapshot:
    storm_id: str
    rainfall_mm: float
    basin_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.rainfall_mm < 0 or not self.basin_ids:
            raise ValueError("降雨量和汇水分区必须有效")


@dataclass(frozen=True)
class DrainageDevice:
    device_id: str
    basin_id: str
    capacity_m3_min: float

    def __post_init__(self) -> None:
        if self.capacity_m3_min <= 0:
            raise ValueError("设备能力必须大于零")
