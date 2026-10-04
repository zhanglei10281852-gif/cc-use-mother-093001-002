import unittest

from helpers import T0, ingest_waterlogging, make_service, open_demo_storm


def dispatched_command(service, basin="BASIN-A", volume=900.0):
    ingest_waterlogging(service, basin, "WL-X", volume)
    plan = service.generate_plan("ST-1")
    action = plan["actions"][0]
    service.approve_action(action["action_id"], "值班长")
    return service.dispatch_action(action["action_id"])["command"]


class EscalationTests(unittest.TestCase):
    """超时升级完全由虚拟时钟驱动，不做任何真实等待。"""

    def setUp(self):
        self.service, self.clock, _ = make_service()
        open_demo_storm(self.service)

    def test_no_escalation_before_deadline(self):
        dispatched_command(self.service)
        self.clock.advance(minutes=14)
        out = self.service.sweep_timeouts()
        self.assertEqual([], out["escalated"])

    def test_escalation_levels_progress_with_virtual_time(self):
        cmd = dispatched_command(self.service)
        self.clock.advance(minutes=16)  # 超过 15min 确认时限
        out1 = self.service.sweep_timeouts()
        self.assertEqual(1, len(out1["escalated"]))
        self.assertEqual(1, out1["escalated"][0]["level"])
        self.assertEqual("值班长", out1["escalated"][0]["notify"])
        self.clock.advance(minutes=16)  # 一级宽限 15min 后仍无回执
        out2 = self.service.sweep_timeouts()
        self.assertEqual(2, out2["escalated"][0]["level"])
        self.assertEqual("区防指", out2["escalated"][0]["notify"])
        command = self.service.state.commands[cmd["command_id"]]
        self.assertEqual(2, command.escalation_level)

    def test_acknowledged_command_not_escalated(self):
        cmd = dispatched_command(self.service)
        self.service.submit_receipt("RC-1", cmd["command_id"], "accepted")
        self.clock.advance(minutes=60)
        out = self.service.sweep_timeouts()
        self.assertEqual([], out["escalated"])

    def test_sweep_is_idempotent_at_same_moment(self):
        dispatched_command(self.service)
        self.clock.advance(minutes=20)
        first = self.service.sweep_timeouts()
        again = self.service.sweep_timeouts()  # 同一时刻重复扫描不重复升级
        self.assertEqual(1, len(first["escalated"]))
        self.assertEqual([], again["escalated"])

    def test_escalated_commands_visible_in_queues(self):
        dispatched_command(self.service)
        self.clock.advance(minutes=30)
        self.service.sweep_timeouts()
        queues = self.service.queues()
        self.assertEqual(1, len(queues["escalated"]))
        self.assertEqual(1, queues["pending_receipts"][0]["escalation_level"])
        # 升级后进入新宽限期；越过新截止时刻仍未回执则再次逾期
        self.assertFalse(queues["pending_receipts"][0]["overdue"])
        self.clock.advance(minutes=20)
        self.assertTrue(self.service.queues()["pending_receipts"][0]["overdue"])


if __name__ == "__main__":
    unittest.main()
