"""在托连续性保护：已封闭历史月不允许直接重算。"""

import unittest

from childcare import ConflictError
from tests.conftest import Scenario, StoreTest


class HistoryClosureTest(StoreTest):
    def test_cannot_rewrite_history_after_later_frozen_run(self):
        sc = self.sc
        sc.demand("2026-03", 8)
        sc.demand("2026-04", 8)
        for i in range(8):
            sc.family(f"family-cont-{i:04d}", month="2026-03")
        sc.supply_run("2026-03")
        sc.supply_run("2026-04")
        # 已有 4 月冻结运行后，默认不能重算 3 月
        with self.assertRaises(ConflictError):
            sc.supply_run("2026-03")
        # 同月内重复重算 4 月仍允许（最新月修正）
        sc.supply_run("2026-04")

    def test_force_allows_correction(self):
        sc = self.sc
        sc.demand("2026-03", 8)
        sc.demand("2026-04", 8)
        for i in range(8):
            sc.family(f"family-cont-{i:04d}", month="2026-03")
        sc.supply_run("2026-03")
        sc.supply_run("2026-04")
        # 显式 force 可纠错重开历史
        run = sc.supply.run_month("2026-03", "tester", force=True)
        self.assertTrue(run.startswith("run_"))


if __name__ == "__main__":
    unittest.main()
