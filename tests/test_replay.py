import unittest

from helpers import OBS, make_service, seed_basic


class ReplayAndRecoveryOrderTests(unittest.TestCase):
    def setUp(self):
        self.service, self.clock = make_service()
        seed_basic(self.service)
        svc = self.service
        svc.ingest_report("ST-1", "R-1", "waterlogging", "BASIN-A", OBS,
                          {"depth_m": 0.5, "area_m2": 1200})
        svc.ingest_report("ST-1", "R-2", "waterlogging", "BASIN-B", OBS,
                          {"depth_m": 0.4, "area_m2": 1000})
        svc.ingest_report("ST-1", "R-3", "road_risk", "BASIN-A", OBS,
                          {"risk_level": 5})
        svc.freeze_snapshot("ST-1")
        plan = svc.generate_plan("ST-1")
        self.aid_a = next(a["action_id"] for a in plan["actions"]
                          if a["basin_id"] == "BASIN-A")
        self.aid_b = next(a["action_id"] for a in plan["actions"]
                          if a["basin_id"] == "BASIN-B")
        for aid in (self.aid_a, self.aid_b):
            svc.approve_action(aid, by="指挥员")
            svc.dispatch_action(aid, by="值班长")

    def test_replay_returns_ordered_decision_chain(self):
        replay = self.service.replay("ST-1")
        types = [e["event_type"] for e in replay["events"]]
        self.assertEqual(types, [
            "StormDeclared",
            "ReportIngested", "ReportIngested", "ReportIngested",
            "SnapshotFrozen",
            "PlanGenerated",
            "ActionApproved", "CommandDispatched",
            "ActionApproved", "CommandDispatched",
        ])
        seqs = [e["seq"] for e in replay["events"]]
        self.assertEqual(seqs, sorted(seqs))
        self.assertTrue(all(e["summary"] for e in replay["events"]))
        declared = replay["events"][0]
        self.assertIn("BASIN-A", declared["summary"])

    def test_recovery_order_reflects_receipt_times(self):
        svc = self.service
        self.clock.advance(300)
        svc.record_receipt("RC-B", self.aid_b, "done", by="现场")  # BASIN-B 先恢复
        self.clock.advance(300)
        svc.record_receipt("RC-A", self.aid_a, "done", by="现场")

        order = svc.recovery_order("ST-1")
        basins = order["basins"]
        self.assertEqual(basins[0]["basin_id"], "BASIN-B")
        self.assertEqual(basins[0]["rank"], 1)
        self.assertEqual(basins[1]["basin_id"], "BASIN-A")
        self.assertEqual(basins[1]["rank"], 2)
        self.assertLess(basins[0]["recovered_at"], basins[1]["recovered_at"])

    def test_recovery_order_marks_pending_basins(self):
        svc = self.service
        svc.record_receipt("RC-B", self.aid_b, "done", by="现场")
        order = svc.recovery_order("ST-1")
        by_basin = {b["basin_id"]: b for b in order["basins"]}
        self.assertEqual(by_basin["BASIN-B"]["status"], "recovered")
        self.assertEqual(by_basin["BASIN-A"]["status"], "pending")
        self.assertIsNone(by_basin["BASIN-A"]["rank"])


if __name__ == "__main__":
    unittest.main()
