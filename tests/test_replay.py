import unittest

from helpers import T0, ingest_waterlogging, make_service, open_demo_storm


def run_full_scenario(service, clock):
    """两分区完整流程：启动命令完成后设备转入运行态，再生成恢复方案。"""
    open_demo_storm(service)
    ingest_waterlogging(service, "BASIN-A", "WL-A", 900)
    ingest_waterlogging(service, "BASIN-B", "WL-B", 500, report_id="R-WL-B")
    service.raise_alert("ST-1", "AL-1", "BASIN-A", "sensor", "critical", "积水严重")
    plan1 = service.generate_plan("ST-1")
    for a in plan1["actions"]:
        service.approve_action(a["action_id"], "值班长")
    service.dispatch_plan("ST-1")
    # 现场先接单、再回报"已启动"：START 命令完结，设备转入运行态
    for item in service.queues()["pending_receipts"]:
        service.submit_receipt(f"RC-ACC-{item['command_id']}", item["command_id"], "accepted")
    for item in service.queues()["pending_receipts"]:
        service.submit_receipt(f"RC-RUN-{item['command_id']}", item["command_id"], "completed")
    # 积水排尽 → 恢复方案
    clock.advance(minutes=30)
    ingest_waterlogging(service, "BASIN-A", "WL-A", 0.0,
                        observed="2026-10-03T09:30:00+00:00", report_id="R-WL-A2")
    ingest_waterlogging(service, "BASIN-B", "WL-B", 0.0,
                        observed="2026-10-03T09:30:00+00:00", report_id="R-WL-B2")
    plan2 = service.generate_plan("ST-1")
    recovery = [a for a in plan2["actions"] if a["phase"] == "recovery"]
    for a in recovery:
        service.approve_action(a["action_id"], "值班长")
        service.dispatch_action(a["action_id"])
    return plan1, plan2, recovery


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.service, self.clock, _ = make_service()

    def test_replay_chain_covers_full_decision_path(self):
        run_full_scenario(self.service, self.clock)
        chain = self.service.replay("ST-1")["chain"]
        types = [e["type"] for e in chain]
        for expected in ["StormOpened", "ReportReceived", "AlertRaised", "SnapshotFrozen",
                         "PlanGenerated", "ActionApproved", "CommandDispatched", "ReceiptAccepted"]:
            self.assertIn(expected, types)
        # 链条按事件序号单调递增
        seqs = [e["seq"] for e in chain]
        self.assertEqual(seqs, sorted(seqs))
        # 方案条目链接到快照与动作
        plan_entry = next(e for e in chain if e["type"] == "PlanGenerated")
        self.assertIn("snapshot_id", plan_entry["links"])
        self.assertTrue(plan_entry["links"]["action_ids"])
        # 命令条目链接回动作
        cmd_entry = next(e for e in chain if e["type"] == "CommandDispatched")
        self.assertIn("action_id", cmd_entry["links"])

    def test_recovery_order_ok_when_priority_respected(self):
        _, _, recovery = run_full_scenario(self.service, self.clock)
        # BASIN-A(优先级1) 先完成恢复，BASIN-B(优先级2) 后完成
        by_basin = {}
        for a in recovery:
            by_basin.setdefault(a["basin_id"], a)
        self.clock.advance(minutes=5)
        cmd_a = self.service.state.find_action(by_basin["BASIN-A"]["action_id"]).command_id
        self.service.submit_receipt("RC-DONE-A", cmd_a, "completed")
        self.clock.advance(minutes=5)
        cmd_b = self.service.state.find_action(by_basin["BASIN-B"]["action_id"]).command_id
        self.service.submit_receipt("RC-DONE-B", cmd_b, "completed")
        out = self.service.verify_recovery("ST-1")
        self.assertTrue(out["ok"], out)
        self.assertEqual([], out["violations"])
        self.assertEqual(["BASIN-A", "BASIN-B"], out["expected_order"])

    def test_recovery_order_violation_detected(self):
        _, _, recovery = run_full_scenario(self.service, self.clock)
        by_basin = {}
        for a in recovery:
            by_basin.setdefault(a["basin_id"], a)
        # 故意让 BASIN-B 先于 BASIN-A 完成恢复
        self.clock.advance(minutes=5)
        cmd_b = self.service.state.find_action(by_basin["BASIN-B"]["action_id"]).command_id
        self.service.submit_receipt("RC-DONE-B", cmd_b, "completed")
        self.clock.advance(minutes=5)
        cmd_a = self.service.state.find_action(by_basin["BASIN-A"]["action_id"]).command_id
        self.service.submit_receipt("RC-DONE-A", cmd_a, "completed")
        out = self.service.verify_recovery("ST-1")
        self.assertFalse(out["ok"])
        self.assertEqual(1, len(out["violations"]))
        self.assertEqual("BASIN-A", out["violations"][0]["expected_first"])

    def test_verify_recovery_with_explicit_order(self):
        _, _, recovery = run_full_scenario(self.service, self.clock)
        out = self.service.verify_recovery("ST-1", order=["BASIN-B", "BASIN-A"])
        # 尚未有恢复完成回执 → 无违例但全部未完成
        self.assertTrue(out["ok"])
        self.assertEqual(["BASIN-B", "BASIN-A"], out["incomplete"])


if __name__ == "__main__":
    unittest.main()
