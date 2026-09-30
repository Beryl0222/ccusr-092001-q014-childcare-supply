"""政策动作覆盖：租金减免（按场地月度）、示范奖励（一次性）、按机构月度。"""

import unittest

from tests.conftest import Scenario, StoreTest


class OtherPolicyActionsTest(StoreTest):
    def _ready(self, families=8):
        sc = self.sc
        sc.demand("2026-03", 10)
        for i in range(families):
            sc.family(f"family-pol-{i:04d}")
        sc.supply_run("2026-03")
        return sc

    def test_rent_reduction_per_site_monthly(self):
        sc = self._ready()
        rules = [
            {"action": "租金减免", "basis": "site_monthly",
             "amount_cents": 50_000},  # 每个运营场地每月 500 元
        ]
        sc.store.create_policy("v-rent", "2026-03", rules)
        r = sc.fund_run("2026-03")
        rent = [c for c in r["created"] if c["action"] == "租金减免"]
        self.assertEqual(len(rent), 1)
        self.assertEqual(rent[0]["amount"], 50_000)
        # 同一场地只有一个机构运营，不重复计提（按场地去重）
        self.assertEqual(rent[0]["qty"], 1)

    def test_demonstration_award_one_time_on_first_operating_month(self):
        sc = self._ready()
        rules = [
            {"action": "示范奖励", "basis": "award_one_time",
             "amount_cents": 2_000_000},
        ]
        sc.store.create_policy("v-award", "2026-01", rules)
        # 机构 2026-01 即运营：仅在首个运营月计提一次
        r_jan = sc.fund_run("2026-01")
        awards_jan = [c for c in r_jan["created"] if c["action"] == "示范奖励"]
        self.assertEqual(len(awards_jan), 1)
        self.assertEqual(awards_jan[0]["amount"], 2_000_000)
        # 后续月份不再计提
        r_mar = sc.fund_run("2026-03")
        self.assertFalse(
            [c for c in r_mar["created"] if c["action"] == "示范奖励"])

    def test_provider_monthly_subsidy(self):
        sc = self._ready()
        rules = [
            {"action": "租金减免", "basis": "provider_monthly",
             "amount_cents": 10_000},
        ]
        sc.store.create_policy("v-pm", "2026-03", rules)
        r = sc.fund_run("2026-03")
        items = [c for c in r["created"] if c["action"] == "租金减免"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["amount"], 10_000)

    def test_tag_filter_limits_rule(self):
        sc = self._ready()
        # 给机构打示范标签，规则只对 tag=示范 生效
        import json
        with sc.store.tx() as c:
            c.execute("UPDATE providers SET tags=? WHERE id=?",
                      (json.dumps(["示范"], ensure_ascii=False), sc.provider))
        rules_yes = [{"action": "示范奖励", "basis": "provider_monthly",
                      "amount_cents": 100_000, "tag": "示范"}]
        rules_no = [{"action": "示范奖励", "basis": "provider_monthly",
                     "amount_cents": 100_000, "tag": "其他标签"}]
        sc.store.create_policy("v-tag-yes", "2026-03", rules_yes)
        sc.store.create_policy("v-tag-no", "2026-04", rules_no)
        r_yes = sc.fund_run("2026-03")
        self.assertTrue(
            [c for c in r_yes["created"] if c["action"] == "示范奖励"])
        r_no = sc.fund_run("2026-04")
        self.assertFalse(
            [c for c in r_no["created"] if c["action"] == "示范奖励"])


if __name__ == "__main__":
    unittest.main()
