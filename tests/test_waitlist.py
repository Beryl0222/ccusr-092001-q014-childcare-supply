"""场景三：候补家庭匿名去重、紧迫优先派位、数据最小化。"""

import unittest

from childcare import ValidationError
from childcare.storage import Store
from tests.conftest import Scenario, StoreTest


class WaitlistDedupTest(StoreTest):
    def test_same_family_token_collapses_to_one_row(self):
        sc = self.sc
        w1 = sc.family("family-unique-0001", street="东街")
        # 同一家庭跨街道、跨月份重复申报 -> 同一受理号，不产生第二条候补
        w2 = self.store.upsert_waitlist(
            "family-unique-0001", "西街", "托小班", "2026-04", False, 0, "2026-02")
        self.assertEqual(w1, w2)
        with self.store.tx() as c:
            n = c.execute("SELECT COUNT(*) AS n FROM waitlist").fetchone()["n"]
        self.assertEqual(n, 1)

    def test_original_token_never_persisted(self):
        sc = self.sc
        sc.family("family-secret-value-0001")
        # 库内只有 HMAC 匿名键，原值不可检索
        with self.store.tx() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM waitlist")]
        self.assertEqual(len(rows), 1)
        self.assertNotIn("family-secret-value-0001", rows[0]["anon_key"])
        self.assertEqual(len(rows[0]["anon_key"]), 64)  # sha256 hex

    def test_short_token_rejected(self):
        with self.assertRaises(ValidationError):
            self.sc.family("abc")

    def test_distinct_tokens_distinct_families(self):
        sc = self.sc
        ids = {sc.family(f"family-distinct-{i:04d}") for i in range(5)}
        self.assertEqual(len(ids), 5)


class UrgentPriorityTest(StoreTest):
    def test_urgent_families_allocated_first(self):
        sc = self.sc
        sc.demand("2026-03", 8)
        # 6 个普通家庭先登记
        for i in range(6):
            sc.family(f"family-normal-{i:04d}", urgent=False, score=10)
        # 2 个紧迫家庭后登记
        for i in range(2):
            sc.family(f"family-urgent-{i:04d}", urgent=True, score=10)
        run = sc.supply_run("2026-03")
        snap = sc.snap()
        # 容量 8 恰好配满，其中 2 个紧迫家庭都应获配
        self.assertEqual(snap["allocated"], 8)
        self.assertEqual(snap["urgent_allocated"], 2)

    def test_urgent_gap_when_capacity_short(self):
        sc = self.sc
        sc.demand("2026-03", 10, urgent=5)
        # 容量 8，5 个紧迫家庭全部获配，紧迫缺口=0；总缺口=2
        for i in range(5):
            sc.family(f"family-u-{i:04d}", urgent=True)
        for i in range(5):
            sc.family(f"family-n-{i:04d}", urgent=False)
        sc.supply_run("2026-03")
        snap = sc.snap()
        self.assertEqual(snap["urgent_allocated"], 5)
        self.assertEqual(snap["gap"], 2)

    def test_priority_score_breaks_tie(self):
        # 容量 3，4 个普通家庭，分数高者先配
        store = Store(":memory:")
        sc = Scenario(store, capacity=3)
        sc.demand("2026-03", 4)
        low = sc.family("family-low-score-01", score=1)
        high = sc.family("family-high-score-1", score=99)
        mid1 = sc.family("family-mid-score-01", score=50)
        mid2 = sc.family("family-mid-score-02", score=50)
        sc.supply_run("2026-03")
        with store.tx() as c:
            status = {r["id"]: r["status"]
                      for r in c.execute("SELECT id,status FROM waitlist")}
        self.assertEqual(status[high], "fulfilled")
        self.assertEqual(status[mid1], "fulfilled")
        self.assertEqual(status[mid2], "fulfilled")
        self.assertEqual(status[low], "waiting")


if __name__ == "__main__":
    unittest.main()
