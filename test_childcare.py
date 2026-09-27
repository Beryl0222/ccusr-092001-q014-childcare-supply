"""普惠托位供给测算后端的行为测试。

覆盖: 分阶段启用、跨街道服务、候补匿名去重、临时停办、政策追溯生效、
补助追回、重复申报防护、并发容量守卫、数据最小化与角色权限、解释接口。
"""

import http.client
import json
import os
import tempfile
import threading
import unittest

from childcare import calc, store
from childcare.db import connect, init_db, query, transact
from childcare.errors import Conflict, Forbidden, Unauthorized, ValidationError
from childcare.security import pseudonym

SECRET = b"test-secret"


class StoreCase(unittest.TestCase):
    """每个用例一个独立临时库。"""

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        init_db(self.db)
        self.addCleanup(self._remove)

    def _remove(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.db + suffix)
            except OSError:
                pass

    # 常用布景: 合格场地 + 运营机构(托大班)
    def seed_provider(self, conn, site="S1", provider="P1", street="甲街道",
                      cap=20, price=250000, month="2026-01", model=False):
        store.create_site(conn, site, street, "党群中心")
        store.set_compliance(conn, site, "合格")
        store.upsert_phase(conn, site, 1, month, None, "托大班", cap)
        store.register_provider(conn, provider, site, "托育点", month,
                                model=model)
        store.change_status(conn, provider, "建设", month)
        store.change_status(conn, provider, "备案", month)
        store.change_status(conn, provider, "运营", month)
        store.set_capacity(conn, provider, "托大班", month, cap)
        store.add_price(conn, provider, "托大班", month, price)

    # ---------------------------------------------------------- 分阶段启用
    def test_phased_activation(self):
        def work(conn):
            store.create_site(conn, "S1", "甲街道", "公房")
            store.set_compliance(conn, "S1", "合格")
            store.upsert_phase(conn, "S1", 1, "2026-03", None, "托大班", 20)
            store.upsert_phase(conn, "S1", 2, "2026-06", None, "托大班", 15)
            store.register_provider(conn, "P1", "S1", "点", "2026-01")
            store.set_capacity(conn, "P1", "托大班", "2026-01", 100)
            self.assertEqual(
                store.effective_capacity(conn, "P1", "托大班", "2026-02"), 0)
            self.assertEqual(
                store.effective_capacity(conn, "P1", "托大班", "2026-03"), 20)
            self.assertEqual(
                store.effective_capacity(conn, "P1", "托大班", "2026-06"), 35)
        transact(self.db, work)

    def test_non_compliant_site_has_zero_capacity(self):
        def work(conn):
            store.create_site(conn, "S1", "甲街道", "办公用房")
            store.upsert_phase(conn, "S1", 1, "2026-01", None, "托大班", 20)
            store.register_provider(conn, "P1", "S1", "点", "2026-01")
            store.set_capacity(conn, "P1", "托大班", "2026-01", 20)
            self.assertEqual(
                store.effective_capacity(conn, "P1", "托大班", "2026-01"), 0)
        transact(self.db, work)

    # ---------------------------------------------------------- 跨街道服务
    def test_cross_street_supply_reduces_remote_gap(self):
        def work(conn):
            self.seed_provider(conn, cap=30)
            store.set_service_areas(conn, "P1", [
                {"street": "甲街道", "weight": 1},
                {"street": "乙街道", "weight": 2},
            ])
            store.upsert_demand(conn, "G-B1", "乙街道", "2026-03", "托大班", 30)
            result = calc.calculate_month(conn, "2026-03")
            self.assertGreater(result["gap_lines"], 0)
        transact(self.db, work)
        gaps = query(self.db, calc.gaps, "2026-03")
        line = [l for l in gaps["lines"]
                if l["street"] == "乙街道" and l["class_type"] == "托大班"][0]
        # 30 个托位按 1:2 分摊, 乙街道获得 20, 缺口 30-20=10
        self.assertEqual(line["supply"], 20)
        self.assertEqual(line["gap"], 10)

    # ---------------------------------------------------------- 候补匿名去重
    def test_waitlist_dedup_and_minimization(self):
        raw_ref = "身份证号-3101-XXXX"
        pseudo = pseudonym(SECRET, raw_ref)

        def work(conn):
            store.add_waitlist(conn, "2026-03", "托大班", "G1", "甲街道", pseudo)
            # 同一家庭换个网格重复登记, 应去重为一条
            store.add_waitlist(conn, "2026-03", "托大班", "G2", "甲街道", pseudo)
            store.add_waitlist(conn, "2026-03", "托大班", "G1", "甲街道",
                               pseudonym(SECRET, "另一个家庭"))
        transact(self.db, work)

        summary = query(self.db, store.waitlist_summary, "2026-03", "甲街道")
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0]["families"], 2)
        self.assertNotIn("pseudonym", summary[0])

        # 库中任何位置都不得出现家庭原始标识
        conn = connect(self.db)
        try:
            rows = conn.execute("SELECT * FROM waitlist").fetchall()
            self.assertEqual(len(rows), 2)
            for row in rows:
                self.assertNotIn(raw_ref, json.dumps(dict(row)))
            audits = conn.execute("SELECT detail FROM audit_log").fetchall()
            for a in audits:
                self.assertNotIn(raw_ref, a["detail"])
        finally:
            conn.close()

    # ---------------------------------------------------------- 临时停办
    def test_temporary_suspension(self):
        def work(conn):
            self.seed_provider(conn)
            store.change_status(conn, "P1", "暂停", "2026-04",
                                end_month="2026-05", reason="消防整改")
            self.assertEqual(store.status_at(conn, "P1", "2026-03"), "运营")
            self.assertEqual(store.status_at(conn, "P1", "2026-04"), "暂停")
            self.assertEqual(store.status_at(conn, "P1", "2026-05"), "暂停")
            self.assertEqual(store.status_at(conn, "P1", "2026-06"), "运营")
            # 停办期间不能分配托位
            with self.assertRaises(Conflict):
                store.allocate(conn, "P1", "2026-04", "托大班", 1)
        transact(self.db, work)

    def test_suspension_removes_supply_and_subsidy(self):
        def work(conn):
            self.seed_provider(conn)
            store.add_policy(conn, "v1", "运营补助", "",
                             {"per_slot": 50000, "price_cap": 300000},
                             "2026-01", "2026-01")
            store.allocate(conn, "P1", "2026-04", "托大班", 10)
            store.change_status(conn, "P1", "暂停", "2026-04",
                                end_month="2026-04")
            calc.calculate_month(conn, "2026-04")
        transact(self.db, work)
        gaps = query(self.db, calc.gaps, "2026-04")
        supply = sum(l["supply"] for l in gaps["lines"])
        self.assertEqual(supply, 0)
        funding = query(self.db, calc.funding, "2026-04")
        self.assertEqual(
            [l for l in funding["lines"] if l["action"] == "运营补助"], [])

    # ---------------------------------------------------------- 政策追溯生效
    def test_retroactive_policy_adjustment(self):
        def work(conn):
            self.seed_provider(conn)
            store.add_policy(conn, "v1", "运营补助", "",
                             {"per_slot": 50000, "price_cap": 300000},
                             "2026-01", "2026-01")
            store.allocate(conn, "P1", "2026-03", "托大班", 10)
            calc.calculate_month(conn, "2026-03")
        transact(self.db, work)

        funding = query(self.db, calc.funding, "2026-03")
        line = [l for l in funding["lines"] if l["action"] == "运营补助"][0]
        self.assertEqual(line["amount"], 10 * 50000)
        self.assertEqual(line["policy_version"], "v1")
        transact(self.db, calc.confirm_subsidy, line["subsidy_id"], "finance")

        # 6 月发布 v2, 追溯自 1 月适用 → 重算 3 月产生补差
        def work2(conn):
            store.add_policy(conn, "v2", "运营补助", "",
                             {"per_slot": 80000, "price_cap": 300000},
                             "2026-01", "2026-06")
            calc.calculate_month(conn, "2026-03")
        transact(self.db, work2)
        funding = query(self.db, calc.funding, "2026-03")
        delta = [l for l in funding["lines"]
                 if l["action"] == "运营补助" and l["status"] == "测算"]
        self.assertEqual(len(delta), 1)
        self.assertEqual(delta[0]["amount"], 10 * 80000 - 10 * 50000)
        self.assertEqual(delta[0]["policy_version"], "v2")
        transact(self.db, calc.confirm_subsidy, delta[0]["subsidy_id"],
                 "finance")

        # 7 月发布 v3 再次追溯下调 → 自动生成补助追回
        def work3(conn):
            store.add_policy(conn, "v3", "运营补助", "",
                             {"per_slot": 20000, "price_cap": 300000},
                             "2026-01", "2026-07")
            result = calc.calculate_month(conn, "2026-03")
            self.assertEqual(result["adjustments"], 1)
        transact(self.db, work3)
        funding = query(self.db, calc.funding, "2026-03")
        claw = [l for l in funding["lines"] if l["action"] == "补助追回"]
        self.assertEqual(len(claw), 1)
        self.assertEqual(claw[0]["amount"], 10 * 80000 - 10 * 20000)
        self.assertEqual(claw[0]["reason"], "政策追溯调整")

        # 解释: 追回行说明因哪版政策(现行 v3)重算而起, 目标额一并留痕
        explain = query(self.db, calc.explain_subsidy, claw[0]["subsidy_id"])
        self.assertEqual(explain["policy"]["version"], "v3")
        self.assertEqual(explain["inputs"]["target"], 10 * 20000)

    def test_subsidy_explain_shows_policy_version(self):
        def work(conn):
            self.seed_provider(conn)
            store.add_policy(conn, "v1", "运营补助", "",
                             {"per_slot": 50000, "price_cap": 300000},
                             "2026-01", "2026-01")
            store.allocate(conn, "P1", "2026-03", "托大班", 4)
            calc.calculate_month(conn, "2026-03")
        transact(self.db, work)
        funding = query(self.db, calc.funding, "2026-03")
        sid = funding["lines"][0]["subsidy_id"]
        explain = query(self.db, calc.explain_subsidy, sid)
        self.assertEqual(explain["policy"]["version"], "v1")
        self.assertEqual(explain["policy"]["published_month"], "2026-01")
        self.assertEqual(explain["inputs"]["allocated"], 4)
        self.assertEqual(explain["snapshot"]["seq"], 1)

    # ---------------------------------------------------------- 补助追回与重复申报
    def test_manual_clawback_and_over_clawback_rejected(self):
        def work(conn):
            self.seed_provider(conn)
            store.add_policy(conn, "v1", "运营补助", "",
                             {"per_slot": 50000, "price_cap": 300000},
                             "2026-01", "2026-01")
            store.allocate(conn, "P1", "2026-03", "托大班", 10)
            calc.calculate_month(conn, "2026-03")
        transact(self.db, work)
        sid = query(self.db, calc.funding, "2026-03")["lines"][0]["subsidy_id"]
        transact(self.db, calc.confirm_subsidy, sid, "finance")
        transact(self.db, calc.clawback, sid, 100000, "机构停业", "finance")
        with self.assertRaises(Conflict):
            transact(self.db, calc.clawback, sid, 500000, "超额追回", "finance")
        funding = query(self.db, calc.funding, "2026-03")
        net = [n for n in funding["confirmed_net"] if n["action"] == "运营补助"][0]
        self.assertEqual(net["confirmed_net"], 500000 - 100000)

    def test_stale_snapshot_confirm_rejected(self):
        def work(conn):
            self.seed_provider(conn)
            store.add_policy(conn, "v1", "运营补助", "",
                             {"per_slot": 50000, "price_cap": 300000},
                             "2026-01", "2026-01")
            store.allocate(conn, "P1", "2026-03", "托大班", 10)
            calc.calculate_month(conn, "2026-03")
        transact(self.db, work)
        stale = query(self.db, calc.funding, "2026-03")["lines"][0]["subsidy_id"]
        # 重算后旧快照的测算行不可再确认(防重复申报)
        transact(self.db, calc.calculate_month, "2026-03")
        with self.assertRaises(Conflict):
            transact(self.db, calc.confirm_subsidy, stale, "finance")
        # 最新快照的测算行可以确认, 且金额不重复
        fresh = query(self.db, calc.funding, "2026-03")["lines"][0]
        transact(self.db, calc.confirm_subsidy, fresh["subsidy_id"], "finance")
        funding = query(self.db, calc.funding, "2026-03")
        net = [n for n in funding["confirmed_net"] if n["action"] == "运营补助"][0]
        self.assertEqual(net["confirmed_net"], 500000)

    # ---------------------------------------------------------- 并发容量守卫
    def test_concurrent_allocation_never_exceeds_cap(self):
        def work(conn):
            self.seed_provider(conn, cap=15)
        transact(self.db, work)

        results = []
        def try_alloc():
            try:
                transact(self.db, store.allocate, "P1", "2026-03",
                         "托大班", 2, "health")
                results.append("ok")
            except Conflict:
                results.append("full")

        threads = [threading.Thread(target=try_alloc) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        total = query(self.db, store.allocated, "P1", "2026-03", "托大班")
        self.assertLessEqual(total, 15)
        self.assertEqual(total, 2 * results.count("ok"))
        self.assertEqual(results.count("ok"), 7)  # 15 // 2

    def test_capacity_reduction_guard(self):
        def work(conn):
            self.seed_provider(conn, cap=10)
            store.allocate(conn, "P1", "2026-03", "托大班", 8)
            with self.assertRaises(Conflict):
                store.set_capacity(conn, "P1", "托大班", "2026-01", 5)
            store.set_capacity(conn, "P1", "托大班", "2026-01", 8)
            # 场地阶段下调同样受守卫约束
            with self.assertRaises(Conflict):
                store.upsert_phase(conn, "S1", 1, "2026-01", None, "托大班", 4)
        transact(self.db, work)

    # ---------------------------------------------------------- 价格承诺
    def test_price_cap_and_midterm_price_drop(self):
        def work(conn):
            self.seed_provider(conn, price=350000)  # 超出普惠价格上限
            store.add_policy(conn, "v1", "运营补助", "",
                             {"per_slot": 50000, "price_cap": 300000},
                             "2026-01", "2026-01")
            store.allocate(conn, "P1", "2026-03", "托大班", 5)
            store.allocate(conn, "P1", "2026-04", "托大班", 5)
            # 4 月起中途降价至上限以内
            store.add_price(conn, "P1", "托大班", "2026-04", 280000)
            calc.calculate_month(conn, "2026-03")
            calc.calculate_month(conn, "2026-04")
        transact(self.db, work)
        m3 = query(self.db, calc.funding, "2026-03")
        self.assertEqual(
            [l for l in m3["lines"] if l["action"] == "运营补助"], [])
        m4 = query(self.db, calc.funding, "2026-04")
        line = [l for l in m4["lines"] if l["action"] == "运营补助"][0]
        self.assertEqual(line["amount"], 5 * 50000)

    # ---------------------------------------------------------- 建设建议解释
    def test_recommendation_explains_covered_gap(self):
        def work(conn):
            store.upsert_demand(conn, "G1", "甲街道", "2026-03", "托大班", 12)
            store.upsert_demand(conn, "G2", "甲街道", "2026-03", "托大班", 8)
            calc.calculate_month(conn, "2026-03")
        transact(self.db, work)
        recs = query(self.db, calc.recommendations, "2026-03")
        rec = [r for r in recs["lines"]
               if r["street"] == "甲街道" and r["class_type"] == "托大班"][0]
        self.assertEqual(rec["add_slots"], 20)
        explain = query(self.db, calc.explain_recommendation, rec["rec_id"])
        self.assertEqual(explain["covered_gap"], 20)
        self.assertEqual(explain["residual_gap"], 0)
        covered = {c["grid"]: c["unmet"] for c in explain["covered"]}
        self.assertEqual(covered, {"G1": 12, "G2": 8})

    # ---------------------------------------------------------- 机构状态机
    def test_status_machine_validation(self):
        def work(conn):
            store.create_site(conn, "S1", "甲街道", "公房")
            store.register_provider(conn, "P1", "S1", "点", "2026-01")
            with self.assertRaises(ValidationError):
                store.change_status(conn, "P1", "运营", "2026-02")
            store.change_status(conn, "P1", "建设", "2026-02")
            store.change_status(conn, "P1", "备案", "2026-03")
            store.change_status(conn, "P1", "退出", "2026-04")
            with self.assertRaises(ValidationError):
                store.change_status(conn, "P1", "运营", "2026-05")
        transact(self.db, work)

    # ---------------------------------------------------------- 快照可追溯
    def test_snapshots_are_traceable(self):
        def work(conn):
            store.upsert_demand(conn, "G1", "甲街道", "2026-03", "托大班", 5)
            calc.calculate_month(conn, "2026-03", note="初算")
            store.upsert_demand(conn, "G1", "甲街道", "2026-03", "托大班", 9)
            calc.calculate_month(conn, "2026-03", note="需求修订")
        transact(self.db, work)
        conn = connect(self.db)
        try:
            snaps = conn.execute(
                "SELECT seq, note FROM snapshots WHERE month='2026-03'"
                " ORDER BY seq").fetchall()
            self.assertEqual([s["seq"] for s in snaps], [1, 2])
            first = conn.execute(
                "SELECT demand FROM gap_lines gl JOIN snapshots s"
                " ON s.snapshot_id=gl.snapshot_id"
                " WHERE s.month='2026-03' AND s.seq=1").fetchone()
            self.assertEqual(first["demand"], 5)
        finally:
            conn.close()
        latest = query(self.db, calc.gaps, "2026-03")
        self.assertEqual(latest["seq"], 2)
        self.assertEqual(latest["lines"][0]["demand"], 9)


# ---------------------------------------------------------------- HTTP 层

from childcare.api import make_handler  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

TOKENS = {
    "tok-planner": {"role": "planner"},
    "tok-health": {"role": "health"},
    "tok-finance": {"role": "finance"},
    "tok-auditor": {"role": "auditor"},
    "tok-p1": {"role": "provider", "provider_id": "P1"},
    "tok-p2": {"role": "provider", "provider_id": "P2"},
}


class ApiCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, cls.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        init_db(cls.db)
        app = {"db": cls.db, "secret": SECRET, "tokens": TOKENS}
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(cls.db + suffix)
            except OSError:
                pass

    def call(self, method, path, body=None, token=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Token"] = token
        conn.request(method, path,
                     json.dumps(body) if body is not None else None, headers)
        resp = conn.getresponse()
        payload = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, payload

    def test_health_is_public(self):
        status, body = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["service"], "inclusive-childcare-supply")

    def test_auth_required(self):
        status, _ = self.call("POST", "/demand", {"records": []})
        self.assertEqual(status, 401)

    def test_role_forbidden(self):
        status, _ = self.call("POST", "/sites",
                              {"site_id": "X", "street": "甲", "kind": "公房"},
                              token="tok-finance")
        self.assertEqual(status, 403)

    def test_provider_self_scope(self):
        status, _ = self.call(
            "PUT", "/providers/P2/capacity",
            {"class_type": "托大班", "month": "2026-01", "capacity": 5},
            token="tok-p1")
        self.assertEqual(status, 403)

    def test_waitlist_rejects_extra_family_fields(self):
        status, body = self.call(
            "POST", "/waitlist",
            {"month": "2026-03", "class_type": "托大班", "grid": "G1",
             "street": "甲街道", "family_ref": "证件A",
             "name": "张三", "phone": "13800000000"},
            token="tok-health")
        self.assertEqual(status, 400)
        self.assertIn("不允许的字段", body["message"])

    def test_full_flow_over_http(self):
        # 需求与场地
        status, _ = self.call("POST", "/demand", {"records": [
            {"grid": "G1", "street": "甲街道", "month": "2026-03",
             "class_type": "托大班", "demand": 25}]}, token="tok-planner")
        self.assertEqual(status, 201)
        self.assertEqual(self.call("POST", "/sites", {
            "site_id": "S1", "street": "甲街道", "kind": "党群中心"},
            token="tok-planner")[0], 201)
        self.assertEqual(self.call("PUT", "/sites/S1/compliance", {
            "compliance": "合格"}, token="tok-health")[0], 200)
        self.assertEqual(self.call("PUT", "/sites/S1/phases", {
            "phase": 1, "start_month": "2026-01", "class_type": "托大班",
            "capacity": 20}, token="tok-planner")[0], 200)

        # 机构备案到运营
        self.assertEqual(self.call("POST", "/providers", {
            "provider_id": "P1", "site_id": "S1", "name": "托育点",
            "month": "2026-01"}, token="tok-health")[0], 201)
        for st in ("建设", "备案", "运营"):
            self.assertEqual(self.call("POST", "/providers/P1/status", {
                "to_status": st, "month": "2026-01"},
                token="tok-health")[0], 200)

        # 机构自行申报容量与价格
        self.assertEqual(self.call("PUT", "/providers/P1/capacity", {
            "class_type": "托大班", "month": "2026-01", "capacity": 20},
            token="tok-p1")[0], 200)
        self.assertEqual(self.call("POST", "/providers/P1/prices", {
            "class_type": "托大班", "month": "2026-01", "price": 250000},
            token="tok-p1")[0], 201)

        # 候补(匿名)与分配
        self.assertEqual(self.call("POST", "/waitlist", {
            "month": "2026-03", "class_type": "托大班", "grid": "G1",
            "street": "甲街道", "family_ref": "证件A"}, token="tok-p1")[0], 201)
        self.assertEqual(self.call("POST", "/providers/P1/allocations", {
            "month": "2026-03", "class_type": "托大班", "count": 18},
            token="tok-health")[0], 200)
        # 超出合规上限被拒绝
        self.assertEqual(self.call("POST", "/providers/P1/allocations", {
            "month": "2026-03", "class_type": "托大班", "count": 3},
            token="tok-health")[0], 409)

        # 政策与测算
        self.assertEqual(self.call("POST", "/policies", {
            "version": "v1", "action": "运营补助",
            "params": {"per_slot": 50000, "price_cap": 300000},
            "applies_from": "2026-01", "published_month": "2026-01"},
            token="tok-finance")[0], 201)
        status, result = self.call("POST", "/months/2026-03/calculate", {},
                                   token="tok-planner")
        self.assertEqual(status, 201)
        self.assertEqual(result["seq"], 1)

        # 缺口: 需求25 + 候补1 - 供给20 = 6
        _, gaps = self.call("GET", "/months/2026-03/gaps",
                            token="tok-planner")
        line = [l for l in gaps["lines"] if l["street"] == "甲街道"
                and l["class_type"] == "托大班"][0]
        self.assertEqual((line["demand"], line["waitlist"], line["supply"],
                          line["gap"]), (25, 1, 20, 6))

        # 建设建议解释
        _, recs = self.call("GET", "/recommendations?month=2026-03",
                            token="tok-planner")
        self.assertEqual(recs["lines"][0]["add_slots"], 6)
        _, explain = self.call(
            "GET", f"/recommendations/{recs['lines'][0]['rec_id']}/explain",
            token="tok-auditor")
        self.assertEqual(explain["covered"], [{"grid": "G1", "unmet": 6}])

        # 资金: 确认并解释政策版本
        _, funding = self.call("GET", "/months/2026-03/funding",
                               token="tok-finance")
        sub = [l for l in funding["lines"] if l["action"] == "运营补助"][0]
        self.assertEqual(sub["amount"], 18 * 50000)
        self.assertEqual(self.call(
            "POST", f"/subsidies/{sub['subsidy_id']}/confirm", {},
            token="tok-finance")[0], 200)
        _, explain = self.call(
            "GET", f"/subsidies/{sub['subsidy_id']}/explain",
            token="tok-auditor")
        self.assertEqual(explain["policy"]["version"], "v1")

        # 追回
        status, claw = self.call("POST",
                                 f"/subsidies/{sub['subsidy_id']}/clawback",
                                 {"amount": 50000, "reason": "机构重复申报"},
                                 token="tok-finance")
        self.assertEqual(status, 201)
        _, funding = self.call("GET", "/months/2026-03/funding",
                               token="tok-finance")
        net = [n for n in funding["confirmed_net"]
               if n["action"] == "运营补助"][0]
        self.assertEqual(net["confirmed_net"], 18 * 50000 - 50000)

        # 机构角色看不到资金台账
        self.assertEqual(self.call("GET", "/months/2026-03/funding",
                                   token="tok-p1")[0], 403)
        # 审计看不到候补以外的写入端点
        self.assertEqual(self.call("POST", "/policies", {
            "version": "v2", "action": "运营补助", "params": {"per_slot": 1},
            "applies_from": "2026-01", "published_month": "2026-01"},
            token="tok-auditor")[0], 403)


if __name__ == "__main__":
    unittest.main()
