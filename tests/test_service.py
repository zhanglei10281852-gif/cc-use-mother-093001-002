import unittest

from helpers import OBS, make_service, seed_basic, seed_plan


class HappyPathTests(unittest.TestCase):
    def setUp(self):
        self.service, self.clock = make_service()
        seed_basic(self.service)

    def test_full_command_cycle(self):
        plan = seed_plan(self.service)
        self.assertEqual(plan["version"], 1)
        self.assertEqual(len(plan["actions"]), 1)  # 600m³/60min=10，PUMP-1 即可覆盖
        action = plan["actions"][0]
        self.assertEqual(action["device_id"], "PUMP-1")
        self.assertEqual(action["command"], "start")
        self.assertIn("BASIN-A", action["reason"])

        approved = self.service.approve_action(action["action_id"], by="指挥员")
        self.assertEqual(approved["state"], "approved")

        dispatched = self.service.dispatch_action(action["action_id"], by="值班长")
        self.assertEqual(dispatched["state"], "dispatched")
        self.assertEqual(dispatched["command_id"], f"CMD-{action['action_id']}")

        queues = self.service.queues("ST-1")
        self.assertEqual(queues["pending_dispatch"], [])
        self.assertEqual(len(queues["pending_receipt"]), 1)

        receipt = self.service.record_receipt("RC-1", action["action_id"], "done",
                                              by="现场")
        self.assertEqual(receipt["status"], "done")
        queues = self.service.queues("ST-1")
        self.assertEqual(queues["pending_receipt"], [])
        self.assertEqual(self.service.action_view(action["action_id"])["state"],
                         "acknowledged")

    def test_plan_priority_follows_road_risk(self):
        svc = self.service
        svc.ingest_report("ST-1", "R-1", "waterlogging", "BASIN-A", OBS,
                          {"depth_m": 0.5, "area_m2": 1200})
        svc.ingest_report("ST-1", "R-2", "waterlogging", "BASIN-B", OBS,
                          {"depth_m": 0.4, "area_m2": 2000})
        svc.ingest_report("ST-1", "R-3", "road_risk", "BASIN-B", OBS,
                          {"risk_level": 5})
        svc.freeze_snapshot("ST-1")
        plan = svc.generate_plan("ST-1")
        self.assertEqual(len(plan["actions"]), 2)
        # BASIN-B 道路风险更高，排优先级第一
        self.assertEqual(plan["actions"][0]["basin_id"], "BASIN-B")
        self.assertEqual(plan["actions"][0]["priority"], 1)
        self.assertEqual(plan["actions"][1]["basin_id"], "BASIN-A")

    def test_capacity_gap_is_noted(self):
        svc = self.service
        svc.ingest_report("ST-1", "R-1", "waterlogging", "BASIN-B", OBS,
                          {"depth_m": 2.0, "area_m2": 5000})  # 10000m³ 远超 PUMP-2
        svc.freeze_snapshot("ST-1")
        plan = svc.generate_plan("ST-1")
        self.assertEqual(len(plan["actions"]), 1)
        self.assertTrue(any("缺口" in note for note in plan["notes"]))

    def test_fault_device_excluded_from_plan(self):
        svc = self.service
        svc.ingest_report("ST-1", "R-1", "waterlogging", "BASIN-A", OBS,
                          {"depth_m": 0.5, "area_m2": 1200})
        svc.ingest_report("ST-1", "R-2", "pump_status", "BASIN-A", OBS,
                          {"device_id": "PUMP-1", "status": "fault"})
        svc.freeze_snapshot("ST-1")
        plan = svc.generate_plan("ST-1")
        devices = {a["device_id"] for a in plan["actions"]}
        self.assertNotIn("PUMP-1", devices)
        self.assertIn("GATE-1", devices)  # 泵故障后由闸门补位

    def test_revoke_before_dispatch_releases_device(self):
        plan = seed_plan(self.service)
        aid = plan["actions"][0]["action_id"]
        self.service.approve_action(aid, by="指挥员")
        revoked = self.service.revoke_action(aid, by="指挥员", reason="雨势减弱")
        self.assertEqual(revoked["state"], "revoked")
        devices = {d["device_id"]: d for d in self.service.list_devices()}
        self.assertIsNone(devices["PUMP-1"]["occupied_by"])

    def test_dispatched_command_cannot_be_revoked(self):
        plan = seed_plan(self.service)
        aid = plan["actions"][0]["action_id"]
        self.service.approve_action(aid, by="指挥员")
        self.service.dispatch_action(aid, by="值班长")
        with self.assertRaises(Exception) as ctx:
            self.service.revoke_action(aid, by="指挥员")
        self.assertEqual(ctx.exception.code, "ALREADY_DISPATCHED")


if __name__ == "__main__":
    unittest.main()
