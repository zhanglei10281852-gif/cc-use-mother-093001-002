import unittest

from helpers import OBS, T0, make_service, seed_basic  # noqa: F401  # 先注入 src 路径
from drainage_dispatch import DispatchError
from drainage_dispatch.contracts import CommandAction
from drainage_dispatch.models import Action, Device, DeviceKind
from drainage_dispatch.service import ensure_device_dispatchable


def _big_demand_plan(service):
    """4000m³ 需求 → 方案同时建议 PUMP-1 与 GATE-1。"""
    service.ingest_report("ST-1", "R-1", "waterlogging", "BASIN-A", OBS,
                          {"depth_m": 2.0, "area_m2": 2000})
    service.freeze_snapshot("ST-1")
    return service.generate_plan("ST-1")


class ConflictTests(unittest.TestCase):
    def setUp(self):
        self.service, self.clock = make_service()
        seed_basic(self.service)

    def test_approve_conflict_is_intercepted(self):
        svc = self.service
        plan = _big_demand_plan(svc)
        a1, a2 = plan["actions"][0], plan["actions"][1]
        # 把 GATE-1 的动作改派到 PUMP-1（此时 PUMP-1 尚未被占用，允许改派）
        svc.reassign_action(a2["action_id"], "PUMP-1", by="指挥员")
        svc.approve_action(a1["action_id"], by="指挥员")  # 占用 PUMP-1
        with self.assertRaises(DispatchError) as ctx:
            svc.approve_action(a2["action_id"], by="指挥员")
        self.assertEqual(ctx.exception.code, "DEVICE_CONFLICT")

    def test_reassign_to_occupied_device_is_blocked(self):
        svc = self.service
        plan = _big_demand_plan(svc)
        a1, a2 = plan["actions"][0], plan["actions"][1]
        svc.approve_action(a1["action_id"], by="指挥员")  # 占用 PUMP-1
        with self.assertRaises(DispatchError) as ctx:
            svc.reassign_action(a2["action_id"], "PUMP-1", by="指挥员")
        self.assertEqual(ctx.exception.code, "DEVICE_CONFLICT")

    def test_reassign_to_other_basin_is_blocked(self):
        svc = self.service
        plan = _big_demand_plan(svc)
        a1 = plan["actions"][0]
        with self.assertRaises(DispatchError) as ctx:
            svc.reassign_action(a1["action_id"], "PUMP-2", by="指挥员")
        self.assertEqual(ctx.exception.code, "REASSIGN_BASIN_MISMATCH")

    def test_dispatch_gate_rejects_occupied_device(self):
        device = Device("PUMP-1", "BASIN-A", DeviceKind.PUMP, 30,
                        occupied_by="ACT-X")
        action = Action(action_id="ACT-Y", plan_id="P", storm_id="S",
                        basin_id="BASIN-A", device_id="PUMP-1",
                        command=CommandAction.START, priority=1, reason="",
                        created_at=T0)
        with self.assertRaises(DispatchError) as ctx:
            ensure_device_dispatchable(action, device)
        self.assertEqual(ctx.exception.code, "DEVICE_CONFLICT")

    def test_dispatch_plan_dispatches_all_approved(self):
        svc = self.service
        plan = _big_demand_plan(svc)
        for action in plan["actions"]:
            svc.approve_action(action["action_id"], by="指挥员")
        result = svc.dispatch_plan(plan["plan_id"], by="值班长")
        self.assertEqual(len(result["dispatched"]), 2)
        self.assertEqual(result["conflicts"], [])
        queues = svc.queues("ST-1")
        self.assertEqual(len(queues["pending_receipt"]), 2)

    def test_reassign_frees_old_device(self):
        svc = self.service
        plan = _big_demand_plan(svc)
        a1, a2 = plan["actions"][0], plan["actions"][1]  # PUMP-1、GATE-1
        svc.approve_action(a2["action_id"], by="指挥员")  # 占用 GATE-1
        svc.reassign_action(a2["action_id"], "PUMP-1", by="指挥员")
        devices = {d["device_id"]: d for d in svc.list_devices()}
        self.assertIsNone(devices["GATE-1"]["occupied_by"])
        self.assertEqual(devices["PUMP-1"]["occupied_by"], a2["action_id"])
        with self.assertRaises(DispatchError) as ctx:
            svc.approve_action(a1["action_id"], by="指挥员")  # PUMP-1 已被 A2 占用
        self.assertEqual(ctx.exception.code, "DEVICE_CONFLICT")


if __name__ == "__main__":
    unittest.main()
