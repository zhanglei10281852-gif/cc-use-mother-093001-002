import unittest

from drainage_dispatch import DispatchError
from helpers import OBS, make_service, seed_basic, seed_plan


class IdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.service, self.clock = make_service()
        seed_basic(self.service)

    def test_duplicate_report_is_ignored(self):
        svc = self.service
        first = svc.ingest_report("ST-1", "R-1", "waterlogging", "BASIN-A", OBS,
                                  {"depth_m": 0.5, "area_m2": 1200})
        again = svc.ingest_report("ST-1", "R-1", "waterlogging", "BASIN-A", OBS,
                                  {"depth_m": 0.5, "area_m2": 1200})
        self.assertFalse(first.get("duplicate", False))
        self.assertTrue(again["duplicate"])
        self.assertEqual(svc.storm_status("ST-1")["report_count"], 1)

    def test_conflicting_report_is_rejected(self):
        svc = self.service
        svc.ingest_report("ST-1", "R-1", "waterlogging", "BASIN-A", OBS,
                          {"depth_m": 0.5, "area_m2": 1200})
        with self.assertRaises(DispatchError) as ctx:
            svc.ingest_report("ST-1", "R-1", "waterlogging", "BASIN-A", OBS,
                              {"depth_m": 9.9, "area_m2": 1200})
        self.assertEqual(ctx.exception.code, "REPORT_CONFLICT")

    def test_duplicate_receipt_is_ignored(self):
        svc = self.service
        plan = seed_plan(svc)
        aid = plan["actions"][0]["action_id"]
        svc.approve_action(aid, by="指挥员")
        svc.dispatch_action(aid, by="值班长")
        first = svc.record_receipt("RC-1", aid, "done", by="现场")
        again = svc.record_receipt("RC-1", aid, "done", by="现场")
        self.assertFalse(first.get("duplicate", False))
        self.assertTrue(again["duplicate"])
        self.assertEqual(svc.action_view(aid)["state"], "acknowledged")
        # 重复回执不产生新事件
        types = [e["event_type"] for e in svc.replay("ST-1")["events"]]
        self.assertEqual(types.count("ReceiptRecorded"), 1)

    def test_conflicting_receipt_is_rejected(self):
        svc = self.service
        plan = seed_plan(svc)
        aid = plan["actions"][0]["action_id"]
        svc.approve_action(aid, by="指挥员")
        svc.dispatch_action(aid, by="值班长")
        svc.record_receipt("RC-1", aid, "done", by="现场")
        with self.assertRaises(DispatchError) as ctx:
            svc.record_receipt("RC-1", aid, "failed", by="现场")
        self.assertEqual(ctx.exception.code, "RECEIPT_CONFLICT")

    def test_duplicate_storm_and_device_registration(self):
        svc = self.service
        storm = svc.declare_storm("ST-1", ["BASIN-A", "BASIN-B"], name="国庆暴雨")
        self.assertTrue(storm["duplicate"])
        with self.assertRaises(DispatchError) as ctx:
            svc.declare_storm("ST-1", ["BASIN-C"])
        self.assertEqual(ctx.exception.code, "STORM_EXISTS")

        device = svc.register_device("PUMP-1", "BASIN-A", "pump", 30)
        self.assertTrue(device["duplicate"])
        with self.assertRaises(DispatchError) as ctx:
            svc.register_device("PUMP-1", "BASIN-A", "pump", 99)
        self.assertEqual(ctx.exception.code, "DEVICE_EXISTS")

    def test_plan_regeneration_without_new_data_is_stable(self):
        svc = self.service
        plan = seed_plan(svc)
        again = svc.generate_plan("ST-1")
        self.assertTrue(again["up_to_date"])
        self.assertEqual(again["plan_id"], plan["plan_id"])
        self.assertEqual(svc.storm_status("ST-1")["plan_versions"],
                         [plan["plan_id"]])


if __name__ == "__main__":
    unittest.main()
