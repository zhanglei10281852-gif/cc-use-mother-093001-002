import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from drainage_dispatch import DispatchService, Registry, VirtualClock

T0 = "2026-10-03T09:00:00+00:00"


def make_service(**kwargs):
    """每个用例独立数据目录 + 虚拟时钟。"""
    data_dir = tempfile.mkdtemp(prefix="dd_test_")
    clock = VirtualClock(T0)
    service = DispatchService.open(data_dir, registry=Registry.default(), clock=clock, **kwargs)
    return service, clock, data_dir


def open_demo_storm(service, basins=("BASIN-A", "BASIN-B")):
    service.open_storm("ST-1", 72.5, basins)


def ingest_waterlogging(service, basin, subject, volume, observed=T0, report_id=None):
    return service.ingest_report(
        "ST-1", report_id or f"R-{subject}", "waterlogging", basin, subject,
        observed, metrics={"volume_m3": volume})


class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.service, self.clock, self.data_dir = make_service()


if __name__ == "__main__":
    unittest.main()
