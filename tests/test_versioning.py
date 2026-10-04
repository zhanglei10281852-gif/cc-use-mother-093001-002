import unittest

from helpers import OBS, make_service, seed_basic, seed_plan


class LateDataVersioningTests(unittest.TestCase):
    def setUp(self):
        self.service, self.clock = make_service()
        seed_basic(self.service)

    def test_late_report_triggers_new_version_without_rewriting_dispatched(self):
        svc = self.service
        plan1 = seed_plan(svc)
        a1 = plan1["actions"][0]["action_id"]
        svc.approve_action(a1, by="指挥员")
        svc.dispatch_action(a1, by="值班长")

        # 迟到的管段水位上报：观测时间早于快照冻结时刻
        late = svc.ingest_report("ST-1", "R-3", "pipe_level", "BASIN-A",
                                 "2026-10-04T07:55:00+00:00",
                                 {"excess_m3": 2400})
        self.assertTrue(late["late"])

        svc.freeze_snapshot("ST-1")
        plan2 = svc.generate_plan("ST-1")
        self.assertEqual(plan2["version"], 2)
        self.assertEqual(svc.storm_status("ST-1")["active_plan"],
                         plan2["plan_id"])

        # 已发令命令不被改写：设备、命令、状态全部保持
        view = svc.action_view(a1)
        self.assertEqual(view["state"], "dispatched")
        self.assertEqual(view["command"], "start")
        self.assertEqual(view["device_id"], "PUMP-1")

        # 新方案只能使用空闲设备（PUMP-1 仍被已发令动作占用）
        devices = {a["device_id"] for a in plan2["actions"]}
        self.assertIn("GATE-1", devices)
        self.assertNotIn("PUMP-1", devices)

    def test_undispatched_actions_are_superseded_by_new_version(self):
        svc = self.service
        svc.ingest_report("ST-1", "R-1", "waterlogging", "BASIN-A", OBS,
                          {"depth_m": 2.0, "area_m2": 2000})  # 4000m³
        svc.freeze_snapshot("ST-1")
        plan1 = svc.generate_plan("ST-1")
        self.assertEqual(len(plan1["actions"]), 2)  # PUMP-1 + GATE-1
        a1, a2 = plan1["actions"][0], plan1["actions"][1]
        svc.approve_action(a1["action_id"], by="指挥员")
        svc.dispatch_action(a1["action_id"], by="值班长")
        # a2 保持未发令

        svc.ingest_report("ST-1", "R-2", "pipe_level", "BASIN-A",
                          "2026-10-04T07:55:00+00:00", {"excess_m3": 500})
        svc.freeze_snapshot("ST-1")
        plan2 = svc.generate_plan("ST-1")

        self.assertEqual(svc.action_view(a2["action_id"])["state"], "superseded")
        self.assertEqual(svc.action_view(a1["action_id"])["state"], "dispatched")
        # GATE-1 被释放后可被新版本方案重新启用
        self.assertIn("GATE-1", {a["device_id"] for a in plan2["actions"]})
        plans = svc.list_plans("ST-1")
        self.assertEqual(plans[0]["state"], "superseded")
        self.assertEqual(plans[1]["state"], "active")

    def test_freeze_without_new_reports_is_rejected(self):
        svc = self.service
        seed_plan(svc)
        with self.assertRaises(Exception) as ctx:
            svc.freeze_snapshot("ST-1")
        self.assertEqual(ctx.exception.code, "NO_NEW_REPORTS")


if __name__ == "__main__":
    unittest.main()
