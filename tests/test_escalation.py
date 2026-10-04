import unittest

from helpers import make_service, seed_basic, seed_plan


class EscalationTests(unittest.TestCase):
    def setUp(self):
        # 升级阶梯：60s / 120s / 240s，全程虚拟时钟，不依赖真实等待
        self.service, self.clock = make_service(ladder=(60.0, 120.0, 240.0))
        seed_basic(self.service)
        plan = seed_plan(self.service)
        self.aid = plan["actions"][0]["action_id"]
        self.service.approve_action(self.aid, by="指挥员")
        self.service.dispatch_action(self.aid, by="值班长")

    def test_timeout_escalates_through_levels(self):
        svc = self.service
        result = svc.tick(seconds=61)
        self.assertEqual([e["level"] for e in result["escalations"]], [1])
        self.assertEqual(svc.action_view(self.aid)["state"], "escalated")

        result = svc.tick(seconds=120)  # 累计 181s > 60+120
        self.assertEqual([e["level"] for e in result["escalations"]], [2])

        result = svc.tick(seconds=240)  # 累计 421s > 60+120+240
        self.assertEqual([e["level"] for e in result["escalations"]], [3])
        self.assertEqual(svc.action_view(self.aid)["escalation_level"], 3)

        # 阶梯用尽后不再产生新升级
        result = svc.tick(seconds=10000)
        self.assertEqual(result["escalations"], [])

    def test_single_tick_crosses_multiple_levels(self):
        result = self.service.tick(seconds=500)
        self.assertEqual([e["level"] for e in result["escalations"]], [1, 2, 3])

    def test_receipt_before_deadline_prevents_escalation(self):
        svc = self.service
        svc.record_receipt("RC-1", self.aid, "done", by="现场")
        result = svc.tick(seconds=10000)
        self.assertEqual(result["escalations"], [])
        self.assertEqual(svc.action_view(self.aid)["state"], "acknowledged")

    def test_escalated_action_still_accepts_receipt(self):
        svc = self.service
        svc.tick(seconds=61)
        svc.record_receipt("RC-1", self.aid, "done", by="现场")
        self.assertEqual(svc.action_view(self.aid)["state"], "acknowledged")
        self.assertEqual(svc.queues("ST-1")["pending_receipt"], [])

    def test_failed_receipt_frees_device_for_replan(self):
        svc = self.service
        svc.record_receipt("RC-1", self.aid, "failed", by="现场")
        self.assertEqual(svc.action_view(self.aid)["state"], "failed")
        devices = {d["device_id"]: d for d in svc.list_devices()}
        self.assertIsNone(devices["PUMP-1"]["occupied_by"])


if __name__ == "__main__":
    unittest.main()
