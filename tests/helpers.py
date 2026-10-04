import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from drainage_dispatch import DispatchService, EventStore, ManualClock  # noqa: E402

T0 = datetime(2026, 10, 4, 8, 0, 0, tzinfo=timezone.utc)
OBS = "2026-10-04T07:50:00+00:00"


def make_service(ladder=(60.0, 120.0, 240.0)):
    clock = ManualClock(T0)
    service = DispatchService(EventStore(":memory:"), clock=clock,
                              ack_ladder_sec=ladder)
    return service, clock


def seed_basic(service, storm_id="ST-1"):
    service.declare_storm(storm_id, ["BASIN-A", "BASIN-B"], name="国庆暴雨")
    service.register_device("PUMP-1", "BASIN-A", "pump", 30)
    service.register_device("PUMP-2", "BASIN-B", "pump", 20)
    service.register_device("GATE-1", "BASIN-A", "gate", 50)


def seed_plan(service, storm_id="ST-1"):
    """积水 600m³ + 道路风险 → 方案 v1 仅建议 PUMP-1。"""
    service.ingest_report(storm_id, "R-1", "waterlogging", "BASIN-A", OBS,
                          {"depth_m": 0.5, "area_m2": 1200})
    service.ingest_report(storm_id, "R-2", "road_risk", "BASIN-A", OBS,
                          {"risk_level": 4})
    service.freeze_snapshot(storm_id)
    return service.generate_plan(storm_id)
