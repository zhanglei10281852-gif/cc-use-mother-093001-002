import unittest

from helpers import T0, ingest_waterlogging, make_service, open_demo_storm

from drainage_dispatch.errors import (
    ConflictError,
    DeviceConflictError,
    NotFoundError,
    StateTransitionError,
    ValidationError,
)


def approved_plan(service, basin="BASIN-A", volume=900.0):
    """生成方案并全部审批，返回方案动作列表。"""
    ingest_waterlogging(service, basin, "WL-X", volume)
    plan = service.generate_plan("ST-1")
    for a in plan["actions"]:
        service.approve_action(a["action_id"], "值班长")
    return plan["actions"]


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.service, self.clock, _ = make_service()
        open_demo_storm(self.service)

    def test_full_chain_propose_approve_dispatch_receipt(self):
        actions = approved_plan(self.service)
        first = actions[0]
        cmd = self.service.dispatch_action(first["action_id"])["command"]
        self.assertEqual("issued", cmd["status"])
        r1 = self.service.submit_receipt("RC-1", cmd["command_id"], "accepted")
        self.assertEqual("acknowledged", r1["receipt"]["resulting_status"])
        r2 = self.service.submit_receipt("RC-2", cmd["command_id"], "completed")
        self.assertEqual("completed", r2["receipt"]["resulting_status"])
        action = self.service.state.find_action(first["action_id"])
        self.assertEqual("completed", str(action.status))

    def test_dispatch_requires_approval(self):
        ingest_waterlogging(self.service, "BASIN-A", "WL-X", 900)
        plan = self.service.generate_plan("ST-1")
        with self.assertRaises(StateTransitionError):
            self.service.dispatch_action(plan["actions"][0]["action_id"])

    def test_receipt_for_terminal_command_rejected(self):
        actions = approved_plan(self.service)
        cmd = self.service.dispatch_action(actions[0]["action_id"])["command"]
        self.service.submit_receipt("RC-1", cmd["command_id"], "completed")
        with self.assertRaises(ConflictError):
            self.service.submit_receipt("RC-2", cmd["command_id"], "failed")

    def test_revoke_only_before_dispatch(self):
        actions = approved_plan(self.service)
        ok = self.service.revoke_action(actions[-1]["action_id"], "值班长", "重复建议")
        self.assertEqual("revoked", ok["action"]["status"])
        cmd = self.service.dispatch_action(actions[0]["action_id"])["command"]
        with self.assertRaises(StateTransitionError):
            self.service.revoke_action(actions[0]["action_id"], "值班长")

    def test_reassign_device_before_dispatch(self):
        actions = approved_plan(self.service)
        start_a = next(a for a in actions if a["device_id"] == "PUMP-1")
        out = self.service.reassign_action(start_a["action_id"], "PUMP-2", "值班长")
        self.assertEqual("PUMP-2", out["action"]["device_id"])
        self.assertEqual("PUMP-1", out["action"]["reassigned_from"])

    def test_reassign_rejects_cross_basin_and_occupied(self):
        actions = approved_plan(self.service)
        start_a = next(a for a in actions if a["device_id"] == "PUMP-1")
        with self.assertRaises(ValidationError):
            self.service.reassign_action(start_a["action_id"], "PUMP-3", "值班长")  # BASIN-B 设备
        # PUMP-2 被占后不可改派过去
        other = next(a for a in actions if a["device_id"] == "PUMP-2")
        self.service.dispatch_action(other["action_id"])
        with self.assertRaises(DeviceConflictError):
            self.service.reassign_action(start_a["action_id"], "PUMP-2", "值班长")

    def test_unknown_ids_raise_not_found(self):
        with self.assertRaises(NotFoundError):
            self.service.dispatch_action("NOPE")
        with self.assertRaises(NotFoundError):
            self.service.submit_receipt("RC-X", "CMD-NOPE", "accepted")


class IdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.service, self.clock, _ = make_service()
        open_demo_storm(self.service)

    def test_duplicate_alert_is_idempotent(self):
        self.service.raise_alert("ST-1", "AL-1", "BASIN-A", "sensor", "critical", "积水")
        again = self.service.raise_alert("ST-1", "AL-1", "BASIN-A", "sensor", "critical", "积水(重发)")
        self.assertTrue(again["duplicate"])
        self.assertEqual(1, len(self.service.state.storms["ST-1"].alerts))

    def test_duplicate_report_is_idempotent(self):
        ingest_waterlogging(self.service, "BASIN-A", "WL-1", 100, report_id="R-1")
        again = ingest_waterlogging(self.service, "BASIN-A", "WL-1", 999, report_id="R-1")
        self.assertTrue(again["duplicate"])
        storm = self.service.state.storms["ST-1"]
        self.assertEqual(100.0, storm.reports["R-1"].metrics["volume_m3"])

    def test_duplicate_receipt_is_idempotent(self):
        actions = approved_plan(self.service)
        cmd = self.service.dispatch_action(actions[0]["action_id"])["command"]
        first = self.service.submit_receipt("RC-1", cmd["command_id"], "accepted")
        again = self.service.submit_receipt("RC-1", cmd["command_id"], "accepted")
        self.assertTrue(again["duplicate"])
        self.assertEqual(first["receipt"]["received_at"], again["receipt"]["received_at"])
        self.assertEqual(1, len(self.service.state.receipts))

    def test_duplicate_dispatch_returns_same_command(self):
        actions = approved_plan(self.service)
        cmd1 = self.service.dispatch_action(actions[0]["action_id"])["command"]
        cmd2 = self.service.dispatch_action(actions[0]["action_id"])
        self.assertTrue(cmd2["duplicate"])
        self.assertEqual(cmd1["command_id"], cmd2["command"]["command_id"])

    def test_open_storm_idempotent_but_not_mutable(self):
        self.service.open_storm("ST-2", 10, ("BASIN-A",))
        dup = self.service.open_storm("ST-2", 10, ("BASIN-A",))
        self.assertTrue(dup["duplicate"])
        with self.assertRaises(ConflictError):
            self.service.open_storm("ST-2", 99, ("BASIN-A",))


class DeviceConflictTests(unittest.TestCase):
    def setUp(self):
        self.service, self.clock, _ = make_service()
        open_demo_storm(self.service)

    def test_same_device_double_dispatch_blocked(self):
        # 两个动作指向同一设备：改派制造冲突场景
        actions = approved_plan(self.service)
        a1, a2 = actions[0], actions[1]
        self.service.reassign_action(a2["action_id"], a1["device_id"], "值班长")
        self.service.dispatch_action(a1["action_id"])
        with self.assertRaises(DeviceConflictError):
            self.service.dispatch_action(a2["action_id"])

    def test_opposite_commands_same_device_blocked(self):
        # 同一设备先 START 后 STOP（相反命令）必须被拦截
        actions = approved_plan(self.service)
        start = next(a for a in actions if a["device_id"] == "PUMP-2")
        self.service.dispatch_action(start["action_id"])
        self.clock.advance(minutes=5)
        ingest_waterlogging(self.service, "BASIN-A", "WL-X", 0.0,
                            observed="2026-10-03T09:20:00+00:00", report_id="R-CLEAR")
        plan2 = self.service.generate_plan("ST-1")
        stop = next(a for a in plan2["actions"]
                    if a["device_id"] == "PUMP-2" and a["command"] == "stop")
        self.service.approve_action(stop["action_id"], "值班长")
        with self.assertRaises(DeviceConflictError):
            self.service.dispatch_action(stop["action_id"])

    def test_interlock_group_blocked_before_dispatch(self):
        # PUMP-1 启动命令在执行，新版本方案建议开 GATE-1（同组联锁 A-1）→ 发令前拦截
        actions = approved_plan(self.service)
        pump = next(a for a in actions if a["device_id"] == "PUMP-1")
        self.service.dispatch_action(pump["action_id"])
        self.clock.advance(minutes=5)
        plan2 = self.service.generate_plan("ST-1")
        gate = next(a for a in plan2["actions"] if a["device_id"] == "GATE-1")
        self.service.approve_action(gate["action_id"], "值班长")
        with self.assertRaises(DeviceConflictError):
            self.service.dispatch_action(gate["action_id"])

    def test_dispatch_plan_collects_conflicts_without_partial_failure(self):
        actions = approved_plan(self.service)
        a1, a2 = actions[0], actions[1]
        self.service.reassign_action(a2["action_id"], a1["device_id"], "值班长")
        out = self.service.dispatch_plan("ST-1")
        oks = [r for r in out["results"] if r["ok"]]
        errs = [r for r in out["results"] if not r["ok"]]
        self.assertTrue(oks and errs)
        self.assertIn(a2["action_id"], {r["action_id"] for r in errs})


class LateDataTests(unittest.TestCase):
    def setUp(self):
        self.service, self.clock, _ = make_service()
        open_demo_storm(self.service)

    def test_late_report_flagged_and_snapshot_untouched(self):
        ingest_waterlogging(self.service, "BASIN-A", "WL-1", 600)
        plan1 = self.service.generate_plan("ST-1")
        snap_before = self.service.snapshot_view("ST-1")["latest_frozen"]
        # 观测时刻早于快照数据截止 → 迟到
        late = self.service.ingest_report(
            "ST-1", "R-LATE", "waterlogging", "BASIN-A", "WL-1",
            "2026-10-03T08:30:00+00:00", metrics={"volume_m3": 50})
        self.assertTrue(late["report"]["late"])
        snap_after = self.service.snapshot_view("ST-1")["latest_frozen"]
        self.assertEqual(snap_before, snap_after)  # 冻结快照不被改写

    def test_late_data_triggers_new_plan_version_only(self):
        actions = approved_plan(self.service)
        cmd = self.service.dispatch_action(actions[0]["action_id"])["command"]
        self.service.ingest_report(
            "ST-1", "R-LATE", "waterlogging", "BASIN-A", "WL-X",
            "2026-10-03T08:30:00+00:00", metrics={"volume_m3": 5000}, auto_plan=True)
        plans = self.service.list_plans("ST-1")["plans"]
        self.assertEqual(2, len(plans))
        # 已发命令保持原样，不被迟到数据改写
        kept = self.service.state.commands[cmd["command_id"]]
        self.assertEqual("issued", str(kept.status))
        self.assertEqual(cmd["device_id"], kept.device_id)

    def test_new_version_supersedes_pending_actions(self):
        actions = approved_plan(self.service)
        self.service.dispatch_action(actions[0]["action_id"])  # 一个发令
        stale_ids = {a["action_id"] for a in actions[1:]}
        self.clock.advance(minutes=10)
        ingest_waterlogging(self.service, "BASIN-A", "WL-X", 300,
                            observed="2026-10-03T09:10:00+00:00", report_id="R-NEW")
        out = self.service.generate_plan("ST-1")
        self.assertEqual(stale_ids, set(out["superseded"]))
        # 被取代的动作不可再发令
        with self.assertRaises(StateTransitionError):
            self.service.dispatch_action(actions[1]["action_id"])
        # 已发令动作状态不受影响
        action0 = self.service.state.find_action(actions[0]["action_id"])
        self.assertEqual("dispatched", str(action0.status))


if __name__ == "__main__":
    unittest.main()
