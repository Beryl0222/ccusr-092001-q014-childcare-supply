"""HTTP API：鉴权、角色矩阵、字段白名单、脱敏、容量冲突响应码。"""

import http.client
import json
import threading
import unittest
from http.server import HTTPServer

from childcare.api import ApiApp, make_server
from childcare.storage import Store


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = Store(":memory:")
        # :memory: 数据库按线程隔离，故使用单线程 HTTPServer，
        # 所有请求在同一服务器线程内共享同一内存库。
        app = ApiApp(cls.store)
        cls.httpd = make_server("127.0.0.1", 0, app, server_cls=HTTPServer)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def call(self, method, path, token=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = "Bearer " + token
        conn.request(method, path, data, headers)
        resp = conn.getresponse()
        raw = resp.read().decode()
        conn.close()
        payload = json.loads(raw) if raw else {}
        return resp.status, payload

    PLANNER = "planner-demo-token"
    FUNDING = "funding-demo-token"
    INTAKE = "intake-demo-token"
    AUDITOR = "auditor-demo-token"

    def test_health_open(self):
        code, payload = self.call("GET", "/health")
        self.assertEqual(code, 200)
        self.assertEqual(payload["service"], "inclusive-childcare-supply")

    def test_401_without_token(self):
        code, _ = self.call("GET", "/policies")
        self.assertEqual(code, 401)

    def test_403_for_cross_role(self):
        # 受理员不能发布政策
        code, body = self.call("POST", "/policies", self.INTAKE,
                               {"version_no": "x", "effective_month": "2026-01",
                                "rules": []})
        self.assertEqual(code, 403)
        # 规划员不能登记候补
        code, _ = self.call("POST", "/waitlist", self.PLANNER, {})
        self.assertEqual(code, 403)
        # 资金员不能建场地
        code, _ = self.call("POST", "/sites", self.FUNDING, {})
        self.assertEqual(code, 403)
        # 受理员不能看审计日志
        code, _ = self.call("GET", "/audit-log", self.INTAKE)
        self.assertEqual(code, 403)

    def test_unknown_field_rejected(self):
        code, body = self.call("POST", "/sites", self.PLANNER,
                               {"name": "x", "street": "东街",
                                "id_card_of_owner": "secret"})
        self.assertEqual(code, 400)
        self.assertIn("不允许的字段", body["error"])

    def test_validation_error_400(self):
        code, body = self.call("PUT", "/grids/g1/demand", self.PLANNER,
                               {"month": "2026-13", "slot_type": "托小班",
                                "children_count": 1})
        self.assertEqual(code, 400)

    def test_not_found_404(self):
        code, _ = self.call("GET", "/sites/site_nope", self.PLANNER)
        self.assertEqual(code, 404)

    def test_role_scoped_happy_path_and_minimization(self):
        # 规划员建档
        code, _ = self.call("PUT", "/grids/g1", self.PLANNER,
                            {"name": "g1", "street": "东街"})
        self.assertEqual(code, 200)
        code, site = self.call("POST", "/sites", self.PLANNER,
                               {"name": "center", "street": "东街"})
        self.assertEqual(code, 201)
        sid = site["site_id"]
        code, phase = self.call("POST", f"/sites/{sid}/phases", self.PLANNER,
                                {"seq": 1, "name": "p1",
                                 "compliance_status": "合规",
                                 "compliance_capacity": 20,
                                 "open_month": "2026-01"})
        pid = self.call("POST", "/providers", self.PLANNER,
                        {"name": "sun", "license_no": "LIC-API-1",
                         "site_id": sid, "registered_date": "2026-01-10",
                         "state": "运营"})[1]["provider_id"]
        code, room = self.call("POST", f"/providers/{pid}/classrooms",
                               self.PLANNER,
                               {"phase_id": phase["phase_id"], "name": "A",
                                "slot_type": "托小班", "compliant_capacity": 3,
                                "opened_month": "2026-01"})
        self.assertEqual(code, 201)
        rid, version = room["classroom_id"], room["version"]

        # 建聚合需求（容量 3、需求 10）
        self.call("PUT", "/grids/gapi", self.PLANNER,
                  {"name": "gapi", "street": "东街"})
        self.call("PUT", "/grids/gapi/demand", self.PLANNER,
                  {"month": "2026-03", "slot_type": "托小班",
                   "children_count": 10})

        # 受理员匿名登记 5 个家庭（容量只有 3）
        for i in range(5):
            code, body = self.call("POST", "/waitlist", self.INTAKE,
                                   {"family_token": f"api-family-{i}-secretxx",
                                    "street": "东街", "slot_type": "托小班",
                                    "target_month": "2026-03"})
            self.assertEqual(code, 201)
            # 回显不得包含 token 或匿名键
            self.assertNotIn("family_token", json.dumps(body, ensure_ascii=False))
            self.assertNotIn("anon_key", json.dumps(body, ensure_ascii=False))
        code, run = self.call("POST", "/supply/runs", self.PLANNER,
                              {"month": "2026-03"})
        self.assertEqual(code, 201)
        seated = next(r for r in run["snapshot"]["rows"]
                      if r["slot_type"] == "托小班")
        self.assertEqual(seated["allocated"], 3)

        # 候补汇总只有计数，无任何家庭级标识
        code, summary = self.call(
            "GET", "/streets/%E4%B8%9C%E8%A1%97/waitlist-summary?month=2026-03",
            self.PLANNER)
        self.assertEqual(code, 200)
        text = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn("anon", text)
        self.assertNotIn("secret", text)

        # 容量乐观锁：错误版本号 409
        code, body = self.call(
            "PATCH", f"/classrooms/{rid}/capacity", self.PLANNER,
            {"new_capacity": 2, "expected_version": 99})
        self.assertEqual(code, 409)
        # 下调到在托峰值以下（已配位 3）-> 409
        code, _ = self.call(
            "PATCH", f"/classrooms/{rid}/capacity", self.PLANNER,
            {"new_capacity": 2, "expected_version": version})
        self.assertEqual(code, 409)

    def test_auditor_sees_log_and_intake_cannot(self):
        # 先产生一条审计记录（规划员建网格）
        self.call("PUT", "/grids/audit-g1", self.PLANNER,
                  {"name": "audit-grid", "street": "审计街"})
        code, body = self.call("GET", "/audit-log?limit=20", self.AUDITOR)
        self.assertEqual(code, 200)
        self.assertIn("entries", body)
        # 审计日志记录了角色与动作，且不含家庭明细
        actions = {e["action"] for e in body["entries"]}
        self.assertIn("网格建档", actions)
        code, _ = self.call("GET", "/audit-log", self.INTAKE)
        self.assertEqual(code, 403)


if __name__ == "__main__":
    unittest.main()
