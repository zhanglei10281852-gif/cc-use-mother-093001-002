import json
import unittest
import urllib.request
from http.client import HTTPConnection

from helpers import make_service, open_demo_storm

from drainage_dispatch.api import make_server


class ApiSmokeTests(unittest.TestCase):
    """通过真实 HTTP 往返验证接口层（随机空闲端口，无需真实等待）。"""

    @classmethod
    def setUpClass(cls):
        cls.service, cls.clock, _ = make_service()
        cls.server = make_server(cls.service, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        import threading

        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _call(self, method, path, body=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=json.dumps(body or {}),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        return resp.status, data

    def test_full_flow_over_http(self):
        st, out = self._call("POST", "/storms", {
            "storm_id": "ST-API", "rainfall_mm": 60.0, "basin_ids": ["BASIN-A"],
            "at": "2026-10-03T10:00:00+00:00"})
        self.assertEqual(200, st)
        st, out = self._call("POST", "/storms/ST-API/reports", {
            "report_id": "R-API-1", "kind": "waterlogging", "basin_id": "BASIN-A",
            "subject_id": "WL-1", "observed_at": "2026-10-03T10:00:00+00:00",
            "metrics": {"volume_m3": 800}})
        self.assertEqual(200, st)
        st, out = self._call("POST", "/storms/ST-API/plans",
                             {"at": "2026-10-03T10:01:00+00:00"})
        self.assertEqual(200, st)
        actions = out["data"]["actions"]
        self.assertTrue(actions)
        for a in actions:
            st, _ = self._call("POST", f"/actions/{a['action_id']}/approve",
                               {"commander": "值班长", "at": "2026-10-03T10:02:00+00:00"})
            self.assertEqual(200, st)
        st, out = self._call("POST", "/storms/ST-API/dispatch-plan",
                             {"at": "2026-10-03T10:03:00+00:00"})
        self.assertEqual(200, st)
        self.assertTrue(all(r["ok"] for r in out["data"]["results"]))

        st, out = self._call("GET", "/queues")
        self.assertEqual(2, len(out["data"]["pending_receipts"]))
        cmd_id = out["data"]["pending_receipts"][0]["command_id"]

        # 回执幂等：同一 receipt_id 重复提交返回 duplicate
        body = {"receipt_id": "RC-API-1", "command_id": cmd_id, "kind": "accepted",
                "at": "2026-10-03T10:05:00+00:00"}
        st, out1 = self._call("POST", "/receipts", body)
        st2, out2 = self._call("POST", "/receipts", body)
        self.assertFalse(out1["data"]["duplicate"])
        self.assertTrue(out2["data"]["duplicate"])

        # 超时升级（虚拟时刻，无需等待）
        st, out = self._call("POST", "/tick", {"at": "2026-10-03T10:20:00+00:00"})
        self.assertEqual(1, len(out["data"]["escalated"]))

        # 决策链重放
        st, out = self._call("GET", "/storms/ST-API/replay")
        types = [e["type"] for e in out["data"]["chain"]]
        self.assertIn("CommandEscalated", types)

        # 恢复次序验证接口可用
        st, out = self._call("POST", "/storms/ST-API/verify-recovery", {})
        self.assertTrue(out["data"]["ok"])

    def test_error_mapping(self):
        st, out = self._call("GET", "/storms/NOPE/snapshot")
        self.assertEqual(404, st)
        self.assertEqual("NotFoundError", out["error"]["type"])
        st, out = self._call("POST", "/storms", {"storm_id": "BAD", "rainfall_mm": -1,
                                                 "basin_ids": ["BASIN-A"]})
        self.assertEqual(400, st)


if __name__ == "__main__":
    unittest.main()
