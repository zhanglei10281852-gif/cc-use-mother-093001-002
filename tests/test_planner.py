import unittest

from helpers import T0, ingest_waterlogging, make_service, open_demo_storm

from drainage_dispatch.contracts import CommandAction


class PlannerRuleTests(unittest.TestCase):
    """方案生成规则：容量、设备约束、道路风险。"""

    def setUp(self):
        self.service, self.clock, _ = make_service()
        open_demo_storm(self.service, basins=("BASIN-A", "BASIN-B", "BASIN-C"))

    def _plan(self):
        return self.service.generate_plan("ST-1")

    def test_r2_greedy_pump_selection(self):
        # BASIN-A 高风险窗口 30min：900m³ → 需求 30m³/min → PUMP-1(22)+PUMP-2(18)
        ingest_waterlogging(self.service, "BASIN-A", "WL-1", 900)
        out = self._plan()
        starts = [a for a in out["actions"] if a["command"] == "start"]
        pump_starts = {a["device_id"] for a in starts if a["device_id"].startswith("PUMP")}
        self.assertEqual({"PUMP-1", "PUMP-2"}, pump_starts)

    def test_r1_overflow_starts_all_pumps(self):
        # 需求 400m³/min 远超 BASIN-A 泵组 40m³/min
        ingest_waterlogging(self.service, "BASIN-A", "WL-1", 12000)
        out = self._plan()
        starts = [a for a in out["actions"]
                  if a["command"] == "start" and a["device_id"].startswith("PUMP")]
        self.assertTrue(all(a["priority"] == 1 for a in starts))
        self.assertTrue(any("R1-超能力" in r for a in starts for r in a["reasons"]))

    def test_r3_backflow_closes_gates(self):
        ingest_waterlogging(self.service, "BASIN-A", "WL-1", 600)
        self.service.ingest_report("ST-1", "R-PIPE", "pipe_level", "BASIN-A", "PIPE-1",
                                   T0, metrics={"level_m": 3.6})  # 临界 3.5
        out = self._plan()
        gate_stops = [a for a in out["actions"]
                      if a["device_id"] == "GATE-1" and a["command"] == "stop"]
        self.assertEqual(1, len(gate_stops))
        self.assertEqual(1, gate_stops[0]["priority"])

    def test_r4_recovery_stops_running_devices(self):
        # 先发令启动 PUMP-3，随后 BASIN-B 积水排尽 → 新方案建议停机恢复
        ingest_waterlogging(self.service, "BASIN-B", "WL-2", 500)
        plan1 = self._plan()
        target = next(a for a in plan1["actions"] if a["device_id"] == "PUMP-3")
        self.service.approve_action(target["action_id"], "cmdr")
        self.service.dispatch_action(target["action_id"])
        # 现场上报积水清零（迟到数据触发新版本）
        self.clock.advance(minutes=20)
        ingest_waterlogging(self.service, "BASIN-B", "WL-2", 0.0,
                            observed="2026-10-03T09:20:00+00:00", report_id="R-WL-2B")
        plan2 = self._plan()
        recovery = [a for a in plan2["actions"]
                    if a["device_id"] == "PUMP-3" and a["command"] == "stop"]
        self.assertEqual(1, len(recovery))
        self.assertEqual("recovery", recovery[0]["phase"])

    def test_r6_hold_near_critical_level(self):
        ingest_waterlogging(self.service, "BASIN-B", "WL-2", 100)
        self.service.ingest_report("ST-1", "R-PIPE-B", "pipe_level", "BASIN-B", "PIPE-B1",
                                   T0, metrics={"level_m": 2.5})  # 0.8*3.0=2.4 ≤ 2.5 < 3.0
        out = self._plan()
        holds = [a for a in out["actions"]
                 if a["device_id"] == "GATE-2" and a["command"] == CommandAction.HOLD]
        self.assertEqual(1, len(holds))

    def test_interlock_gate_skipped_when_pump_started(self):
        # GATE-1 与 PUMP-1 同组联锁：R2 选中 PUMP-1 后 R5 不再建议开 GATE-1
        ingest_waterlogging(self.service, "BASIN-A", "WL-1", 900)
        out = self._plan()
        gate_starts = [a for a in out["actions"]
                       if a["device_id"] == "GATE-1" and a["command"] == "start"]
        self.assertEqual([], gate_starts)
        self.assertTrue(any("联锁" in n for n in out["plan"]["notes"]))

    def test_priority_orders_high_risk_basin_first(self):
        ingest_waterlogging(self.service, "BASIN-A", "WL-1", 900)   # high
        ingest_waterlogging(self.service, "BASIN-C", "WL-3", 2400, report_id="R-WL-3")  # low
        out = self._plan()
        basins_in_order = [a["basin_id"] for a in out["actions"]]
        self.assertEqual(basins_in_order, sorted(basins_in_order, key=lambda b: 0 if b == "BASIN-A" else 1))
        a_priorities = [a["priority"] for a in out["actions"] if a["basin_id"] == "BASIN-A"]
        c_priorities = [a["priority"] for a in out["actions"] if a["basin_id"] == "BASIN-C"]
        self.assertLess(max(a_priorities), max(c_priorities))


if __name__ == "__main__":
    unittest.main()
