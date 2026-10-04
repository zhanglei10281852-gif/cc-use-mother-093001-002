import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "cli.db")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args, expect_ok=True):
        result = subprocess.run(
            [sys.executable, "run_cli.py", "--db", self.db, *args],
            capture_output=True, text=True, cwd=ROOT)
        if expect_ok:
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0)
        return json.loads(result.stdout)

    def test_smoke_without_args(self):
        result = subprocess.run([sys.executable, "run_cli.py"],
                                capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["storm"], "ST-01")
        self.assertEqual(payload["action"], "start")

    def test_cli_decision_chain_and_replay(self):
        self.run_cli("declare-storm", "--storm-id", "ST-CLI",
                     "--basin", "BASIN-A", "--basin", "BASIN-B")
        self.run_cli("register-device", "--device-id", "PUMP-1",
                     "--basin-id", "BASIN-A", "--kind", "pump", "--capacity", "30")
        self.run_cli("ingest-report", "--storm-id", "ST-CLI", "--report-id", "R-1",
                     "--kind", "waterlogging", "--basin-id", "BASIN-A",
                     "--observed-at", "2026-10-04T07:50:00+00:00",
                     "--payload", '{"depth_m": 0.5, "area_m2": 1200}')
        self.run_cli("freeze", "--storm-id", "ST-CLI")
        plan = self.run_cli("plan", "--storm-id", "ST-CLI")
        aid = plan["actions"][0]["action_id"]
        self.run_cli("approve", "--action-id", aid, "--by", "指挥员")
        self.run_cli("dispatch", "--action-id", aid, "--by", "值班长")

        # 默认阶梯 900/1800/3600：推进 3000 秒应连升两级，全程虚拟时钟
        ticked = self.run_cli("tick", "--seconds", "3000")
        self.assertEqual([e["level"] for e in ticked["escalations"]], [1, 2])

        queues = self.run_cli("queues", "--storm-id", "ST-CLI")
        self.assertEqual(len(queues["pending_receipt"]), 1)

        self.run_cli("receipt", "--receipt-id", "RC-1", "--action-id", aid,
                     "--status", "done", "--by", "现场")
        replay = self.run_cli("replay", "--storm-id", "ST-CLI")
        types = [e["event_type"] for e in replay["events"]]
        self.assertEqual(types[0], "StormDeclared")
        self.assertLess(types.index("CommandDispatched"),
                        types.index("ReceiptRecorded"))
        self.assertIn("ActionEscalated", types)

        order = self.run_cli("recovery-order", "--storm-id", "ST-CLI")
        by_basin = {b["basin_id"]: b for b in order["basins"]}
        self.assertEqual(by_basin["BASIN-A"]["status"], "recovered")
        self.assertEqual(by_basin["BASIN-A"]["rank"], 1)
        self.assertEqual(by_basin["BASIN-B"]["status"], "no_command")

    def test_cli_reports_dispatch_error_as_json(self):
        self.run_cli("declare-storm", "--storm-id", "ST-CLI", "--basin", "BASIN-A")
        body = self.run_cli("freeze", "--storm-id", "ST-CLI", expect_ok=True)
        self.assertEqual(body["version"], 1)
        # 没有新上报时再次冻结应报 NO_NEW_REPORTS
        error = self.run_cli("freeze", "--storm-id", "ST-CLI", expect_ok=False)
        self.assertEqual(error["error"]["code"], "NO_NEW_REPORTS")


if __name__ == "__main__":
    unittest.main()
