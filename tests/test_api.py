import json
import threading
import unittest
import urllib.error
import urllib.request

from helpers import OBS, make_service  # noqa: F401  # 先注入 src 路径
from drainage_dispatch.api import create_server


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service, cls.clock = make_service()
        cls.httpd = create_server(cls.service, "127.0.0.1", 0)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)

    def _post(self, path, body):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_full_flow_over_http(self):
        status, _ = self._post("/storms", {
            "storm_id": "ST-API", "basin_ids": ["BASIN-A"], "name": "接口演练"})
        self.assertEqual(status, 200)
        status, _ = self._post("/devices", {
            "device_id": "PUMP-9", "basin_id": "BASIN-A", "kind": "pump",
            "capacity_m3_min": 25})
        self.assertEqual(status, 200)
        status, report = self._post("/storms/ST-API/reports", {
            "report_id": "R-1", "kind": "waterlogging", "basin_id": "BASIN-A",
            "observed_at": OBS, "payload": {"depth_m": 0.6, "area_m2": 900}})
        self.assertEqual(status, 200)
        self.assertFalse(report["late"])
        status, _ = self._post("/storms/ST-API/freeze", {})
        self.assertEqual(status, 200)
        status, plan = self._post("/storms/ST-API/plans", {})
        self.assertEqual(status, 200)
        aid = plan["actions"][0]["action_id"]
        status, _ = self._post(f"/actions/{aid}/approve", {"by": "指挥员"})
        self.assertEqual(status, 200)
        status, dispatched = self._post(f"/actions/{aid}/dispatch", {"by": "值班长"})
        self.assertEqual(status, 200)
        self.assertEqual(dispatched["state"], "dispatched")
        status, receipt = self._post("/receipts", {
            "receipt_id": "RC-1", "action_id": aid, "status": "done",
            "by": "现场"})
        self.assertEqual(status, 200)
        # 相同回执重复提交幂等
        status, again = self._post("/receipts", {
            "receipt_id": "RC-1", "action_id": aid, "status": "done",
            "by": "现场"})
        self.assertEqual(status, 200)
        self.assertTrue(again["duplicate"])

        status, replay = self._get("/storms/ST-API/replay")
        self.assertEqual(status, 200)
        self.assertEqual(replay["events"][-1]["event_type"], "ReceiptRecorded")
        status, order = self._get("/storms/ST-API/recovery-order")
        self.assertEqual(order["basins"][0]["status"], "recovered")

    def test_unknown_storm_returns_404(self):
        status, body = self._get("/storms/NOPE")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "STORM_NOT_FOUND")

    def test_invalid_report_returns_400(self):
        self._post("/storms", {"storm_id": "ST-BAD", "basin_ids": ["BASIN-A"]})
        status, body = self._post("/storms/ST-BAD/reports", {
            "report_id": "R-1", "kind": "road_risk", "basin_id": "BASIN-A",
            "observed_at": OBS, "payload": {"risk_level": 9}})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "INVALID_REPORT")

    def test_unknown_route_returns_404(self):
        status, body = self._get("/no-such-route")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
