import tempfile
import unittest
from pathlib import Path

from helpers import OBS, T0, seed_basic  # noqa: F401  # 先注入 src 路径
from drainage_dispatch import DispatchService, EventStore, ManualClock


class RestartRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "events.db")

    def tearDown(self):
        self.tmp.cleanup()

    def _build(self, clock):
        return DispatchService(EventStore(self.db_path), clock=clock,
                               ack_ladder_sec=(60.0, 120.0, 240.0))

    def test_pending_queues_survive_restart(self):
        clock = ManualClock(T0)
        svc = self._build(clock)
        seed_basic(svc)
        svc.ingest_report("ST-1", "R-1", "waterlogging", "BASIN-A", OBS,
                          {"depth_m": 2.0, "area_m2": 2000})  # PUMP-1 + GATE-1
        svc.freeze_snapshot("ST-1")
        plan = svc.generate_plan("ST-1")
        a1, a2 = plan["actions"][0]["action_id"], plan["actions"][1]["action_id"]
        svc.approve_action(a1, by="指挥员")
        svc.approve_action(a2, by="指挥员")
        svc.dispatch_action(a1, by="值班长")  # a1 待回执，a2 待发令

        # 模拟进程重启：同一事件库重建服务
        restored = self._build(ManualClock(T0))
        queues = restored.queues("ST-1")
        self.assertEqual([a["action_id"] for a in queues["pending_receipt"]], [a1])
        self.assertEqual([a["action_id"] for a in queues["pending_dispatch"]], [a2])

        # 重启后回执仍可登记且幂等
        restored.record_receipt("RC-1", a1, "done", by="现场")
        again = restored.record_receipt("RC-1", a1, "done", by="现场")
        self.assertTrue(again["duplicate"])
        self.assertEqual(restored.action_view(a1)["state"], "acknowledged")

    def test_escalation_state_survives_restart(self):
        clock = ManualClock(T0)
        svc = self._build(clock)
        seed_basic(svc)
        svc.ingest_report("ST-1", "R-1", "waterlogging", "BASIN-A", OBS,
                          {"depth_m": 0.5, "area_m2": 1200})
        svc.freeze_snapshot("ST-1")
        aid = svc.generate_plan("ST-1")["actions"][0]["action_id"]
        svc.approve_action(aid, by="指挥员")
        svc.dispatch_action(aid, by="值班长")
        clock.advance(61)
        svc.check_timeouts()

        restored = self._build(ManualClock(T0))
        view = restored.action_view(aid)
        self.assertEqual(view["state"], "escalated")
        self.assertEqual(view["escalation_level"], 1)

    def test_virtual_clock_offset_persists_across_processes(self):
        # 默认 OffsetClock：tick 偏移写入事件库，跨进程仍然有效
        svc = DispatchService(EventStore(self.db_path),
                              ack_ladder_sec=(60.0, 120.0, 240.0))
        seed_basic(svc)
        svc.ingest_report("ST-1", "R-1", "waterlogging", "BASIN-A", OBS,
                          {"depth_m": 0.5, "area_m2": 1200})
        svc.freeze_snapshot("ST-1")
        aid = svc.generate_plan("ST-1")["actions"][0]["action_id"]
        svc.approve_action(aid, by="指挥员")
        svc.dispatch_action(aid, by="值班长")
        svc.tick(seconds=61)

        restored = DispatchService(EventStore(self.db_path),
                                   ack_ladder_sec=(60.0, 120.0, 240.0))
        self.assertEqual(restored.action_view(aid)["escalation_level"], 1)
        result = restored.tick(seconds=120)
        self.assertEqual([e["level"] for e in result["escalations"]], [2])


if __name__ == "__main__":
    unittest.main()
