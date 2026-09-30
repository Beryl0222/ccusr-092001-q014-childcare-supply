"""场景四：机构临时停办（暂停/恢复）。"""

import unittest

from tests.conftest import Scenario, StoreTest


class ProviderSuspensionTest(StoreTest):
    def _operating_with_children(self):
        sc = self.sc
        sc.demand("2026-03", 8)
        for i in range(8):
            sc.family(f"family-susp-{i:04d}")
        sc.policy()
        sc.supply_run("2026-03")
        sc.fund_run("2026-01")   # 建设补助
        sc.fund_run("2026-03")   # 运营补助
        return sc

    def test_suspension_excludes_supply(self):
        sc = self._operating_with_children()
        # 4 月暂停：有效供给清零
        self.store.transition_provider(sc.provider, "暂停", "2026-04-10",
                                       "场地维修", "tester")
        sc.demand("2026-04", 8)
        sc.supply_run("2026-04")
        self.assertEqual(sc.snap(month="2026-04")["accessible_capacity"], 0)

    def test_suspension_pays_no_operating_subsidy(self):
        sc = self._operating_with_children()
        self.store.transition_provider(sc.provider, "暂停", "2026-04-10",
                                       "场地维修", "tester")
        result = sc.fund_run("2026-04")
        self.assertEqual(result["created"], [])
        self.assertEqual(result["clawbacks"], [])

    def test_resume_restores_supply_and_subsidy(self):
        sc = self._operating_with_children()
        self.store.transition_provider(sc.provider, "暂停", "2026-04-10",
                                       "维修", "tester")
        sc.fund_run("2026-04")
        # 5 月恢复运营
        self.store.transition_provider(sc.provider, "运营", "2026-05-01",
                                       "复工", "tester")
        sc.demand("2026-05", 8)
        sc.supply_run("2026-05")
        self.assertEqual(sc.snap(month="2026-05")["accessible_capacity"], 8)
        result = sc.fund_run("2026-05")
        ops = [c for c in result["created"] if c["action"] == "运营补助"]
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]["amount"], 8 * 30_000)

    def test_illegal_transition_rejected(self):
        from childcare import ValidationError
        # 运营不能直接回规划；退出为终态
        with self.assertRaises(ValidationError):
            self.store.transition_provider(
                self.sc.provider, "规划", "2026-02-01", "x", "t")
        self.store.transition_provider(
            self.sc.provider, "退出", "2026-08-01", "关停", "t")
        with self.assertRaises(ValidationError):
            self.store.transition_provider(
                self.sc.provider, "运营", "2026-09-01", "复活", "t")


if __name__ == "__main__":
    unittest.main()
