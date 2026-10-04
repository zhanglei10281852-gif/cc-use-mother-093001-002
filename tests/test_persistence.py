import unittest

from helpers import T0, ingest_waterlogging, make_service, open_demo_storm

from drainage_dispatch import DispatchService, Registry, VirtualClock


class PersistenceTests(unittest.TestCase):
    """进程重启后，待执行与待回执队列从事件日志完整恢复。"""

    def test_restart_recovers_pending_queues(self):
        service, clock, data_dir = make_service()
        open_demo_storm(service)
        ingest_waterlogging(service, "BASIN-A", "WL-1", 900)
        plan = service.generate_plan("ST-1")
        for a in plan["actions"]:
            service.approve_action(a["action_id"], "值班长")
        cmd = service.dispatch_action(plan["actions"][0]["action_id"])["command"]
        clock.advance(minutes=20)
        service.sweep_timeouts()

        # 模拟进程重启：同一数据目录重新打开
        reopened = DispatchService.open(data_dir, registry=Registry.default(),
                                        clock=VirtualClock("2026-10-03T09:30:00+00:00"))
        queues = reopened.queues()
        self.assertEqual(1, len(queues["pending_dispatch"]))   # 第二个动作待执行
        self.assertEqual(1, len(queues["pending_receipts"]))   # 已发命令待回执
        self.assertEqual(1, queues["pending_receipts"][0]["escalation_level"])
        self.assertEqual(cmd["command_id"], queues["pending_receipts"][0]["command_id"])

    def test_restart_preserves_idempotency_keys(self):
        service, clock, data_dir = make_service()
        open_demo_storm(service)
        service.raise_alert("ST-1", "AL-1", "BASIN-A", "sensor", "critical", "积水")
        ingest_waterlogging(service, "BASIN-A", "WL-1", 100, report_id="R-1")
        plan = service.generate_plan("ST-1")
        service.approve_action(plan["actions"][0]["action_id"], "值班长")
        cmd = service.dispatch_action(plan["actions"][0]["action_id"])["command"]
        service.submit_receipt("RC-1", cmd["command_id"], "accepted")

        reopened = DispatchService.open(data_dir, registry=Registry.default(), clock=VirtualClock())
        self.assertTrue(reopened.raise_alert("ST-1", "AL-1", "BASIN-A", "sensor", "critical", "重发")["duplicate"])
        self.assertTrue(reopened.ingest_report("ST-1", "R-1", "waterlogging", "BASIN-A", "WL-1",
                                               T0, metrics={"volume_m3": 100})["duplicate"])
        self.assertTrue(reopened.submit_receipt("RC-1", cmd["command_id"], "accepted")["duplicate"])
        self.assertEqual(1, len(reopened.state.receipts))

    def test_restart_keeps_command_immutable(self):
        service, clock, data_dir = make_service()
        open_demo_storm(service)
        ingest_waterlogging(service, "BASIN-A", "WL-1", 900)
        plan = service.generate_plan("ST-1")
        service.approve_action(plan["actions"][0]["action_id"], "值班长")
        cmd = service.dispatch_action(plan["actions"][0]["action_id"])["command"]
        service.submit_receipt("RC-1", cmd["command_id"], "completed")

        reopened = DispatchService.open(data_dir, registry=Registry.default(), clock=VirtualClock())
        from drainage_dispatch.errors import ConflictError
        with self.assertRaises(ConflictError):
            reopened.submit_receipt("RC-2", cmd["command_id"], "failed")


if __name__ == "__main__":
    unittest.main()
