"""建设建议缓解了哪部分缺口：可解释、可追溯到供需运行。"""

import unittest

from childcare import ConflictError
from tests.conftest import Scenario, StoreTest


class RecommendationTest(StoreTest):
    def _gap_scenario(self, demand=12, capacity=8):
        sc = self.sc
        sc.demand("2026-03", demand)
        for i in range(capacity):
            sc.family(f"family-rec-{i:04d}")
        sc.supply_run("2026-03")
        return sc

    def test_recommendation_relieves_gap_and_links_run(self):
        sc = self._gap_scenario()
        snap = sc.snap()
        # 需求 12、容量 8、配位 8 -> 缺口 4
        self.assertEqual(snap["gap"], 4)
        rec_id = sc.supply.recommend(
            "东街", "托小班", ["2026-03"], proposed_capacity=5,
            actor="planner", site_id=sc.site, phase_id=sc.phase)
        detail = sc.supply.explain_recommendation(rec_id)
        # 建议 5 个但缺口只有 4 -> 实际缓解 4，缺口后为 0
        self.assertEqual(detail["gap_before"], 4)
        self.assertEqual(detail["proposed_capacity"], 5)
        self.assertEqual(detail["relieved"], 4)
        self.assertEqual(detail["gap_after"], 0)
        relief = detail["relief_detail"]
        self.assertEqual(len(relief), 1)
        self.assertEqual(relief[0]["month"], "2026-03")
        self.assertEqual(relief[0]["gap_before"], 4)
        self.assertEqual(relief[0]["relieved"], 4)
        # 缓解明细锚定到当时的供需运行 id
        self.assertEqual(relief[0]["run_id"], snap["run_id"])

    def test_recommendation_multi_month_partial_relief(self):
        sc = self.sc
        # 3、4 月缺口分别为 4、6（容量 8，需求 12、14）
        sc.demand("2026-03", 12)
        sc.demand("2026-04", 14)
        for i in range(8):
            sc.family(f"family-rec-{i:04d}", month="2026-03")
        sc.supply_run("2026-03")
        sc.supply_run("2026-04")
        rec_id = sc.supply.recommend(
            "东街", "托小班", ["2026-03", "2026-04"],
            proposed_capacity=5, actor="planner")
        detail = sc.supply.explain_recommendation(rec_id)
        # 3 月缓解 min(4,5)=4，4 月缓解 min(6,5)=5，合计 9
        self.assertEqual(detail["relieved"], 9)
        months = {r["month"]: r["relieved"] for r in detail["relief_detail"]}
        self.assertEqual(months, {"2026-03": 4, "2026-04": 5})
        self.assertEqual(detail["gap_before"], 4 + 6)
        # 4 月仍剩 1 个缺口
        self.assertEqual(detail["gap_after"], 1)

    def test_recommendation_requires_existing_run(self):
        sc = self.sc
        with self.assertRaises(ConflictError):
            sc.supply.recommend("东街", "托小班", ["2026-09"], 5, "planner")

    def test_no_gap_no_relief_recorded(self):
        sc = self.sc
        sc.demand("2026-03", 5)  # 需求小于容量 8，无缺口
        for i in range(5):
            sc.family(f"family-rec-{i:04d}")
        sc.supply_run("2026-03")
        rec_id = sc.supply.recommend(
            "东街", "托小班", ["2026-03"], 5, "planner")
        detail = sc.supply.explain_recommendation(rec_id)
        self.assertEqual(detail["relieved"], 0)
        self.assertEqual(detail["relief_detail"], [])

    def test_superseded_run_preserves_history(self):
        # 重算后旧运行置 superseded，但建议锚定的快照仍可追溯
        sc = self._gap_scenario()
        rec_id = sc.supply.recommend(
            "东街", "托小班", ["2026-03"], 5, "planner")
        before = sc.supply.explain_recommendation(rec_id)
        old_run = before["relief_detail"][0]["run_id"]
        # 新增供给后重算，缺口变化
        sc.demand("2026-03", 12)
        sc.supply_run("2026-03")
        old = self.store.get_run(old_run)
        self.assertEqual(old["status"], "superseded")
        # 旧建议仍指向旧运行，历史结论不被改写
        self.assertEqual(
            sc.supply.explain_recommendation(rec_id)["relief_detail"][0]["run_id"],
            old_run)


if __name__ == "__main__":
    unittest.main()
