"""场景五/六：政策追溯生效、重复申报拦截、价格违约与补助追回。"""

import unittest

from childcare import ConflictError
from tests.conftest import Scenario, StoreTest, build_policy_rules


def rules_v(operation=30_000, construction=1_000_000, commitment=12,
            tolerance=0):
    return build_policy_rules(construction, operation, commitment, tolerance)


class FundingBase(StoreTest):
    def _funded(self, month="2026-03", families=8):
        sc = self.sc
        sc.demand(month, max(families, 10) if families else 0)
        for i in range(families):
            sc.family(f"family-fund-{i:04d}", month=month)
        sc.policy()
        sc.supply_run(month)
        sc.fund_run("2026-01")  # 建设补助
        r = sc.fund_run(month)
        return sc, r


class ConstructionSubsidyTest(FundingBase):
    def test_one_time_on_open_month_only(self):
        sc, r3 = self._funded()
        construction = [c for c in r3["created"] if c["action"] == "建设补助"]
        self.assertEqual(len(construction), 0)  # 3 月不重复计提
        operation = [c for c in r3["created"] if c["action"] == "运营补助"]
        self.assertEqual(operation[0]["amount"], 8 * 30_000)

    def test_construction_amount_uses_capacity(self):
        sc, _ = self._funded()
        r1 = sc.fund_run("2026-01")  # 幂等重放，返回同一 frozen 运行
        claims = sc.funding.run_summary(r1["run_id"])["claims"]
        build = next(c for c in claims if c["action"] == "建设补助")
        self.assertEqual(build["amount_cents"], 8 * 1_000_000)
        self.assertEqual(build["basis_qty"], 8)


class DuplicateClaimTest(FundingBase):
    def test_duplicate_declaration_blocked(self):
        sc, r3 = self._funded()
        room_claim = next(c for c in r3["created"]
                          if c["action"] == "运营补助")
        # 已核定后再次就同机构/班型/月份/动作申报 -> 拦截
        with self.assertRaises(ConflictError):
            self.store.declare_claim(
                sc.provider, "运营补助", "2026-03", sc.room, 999_999, "机构")

    def test_overstated_declaration_voided_but_true_amount_paid(self):
        # 机构先虚高申报 999999（核定额仅 240000）-> 驳回虚高单，系统按核定发
        sc = self.sc
        sc.demand("2026-03", 10)
        for i in range(8):
            sc.family(f"family-fund-{i:04d}")
        sc.policy()
        sc.supply_run("2026-03")
        cid = self.store.declare_claim(
            sc.provider, "运营补助", "2026-03", sc.room, 999_999, "机构")
        r = sc.fund_run("2026-03")
        self.assertIn(cid, r["voided"])
        # 核定部分仍应发放（系统核定单），金额 240000
        approved = [c for c in r["created"] if c["action"] == "运营补助"]
        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0]["amount"], 240_000)

    def test_honest_declaration_paid_as_declared(self):
        sc = self.sc
        sc.demand("2026-03", 10)
        for i in range(8):
            sc.family(f"family-fund-{i:04d}")
        sc.policy()
        sc.supply_run("2026-03")
        cid = self.store.declare_claim(
            sc.provider, "运营补助", "2026-03", sc.room, 200_000, "机构")
        r = sc.fund_run("2026-03")
        self.assertEqual(r["voided"], [])
        self.assertEqual(self.store.claim(cid)["status"], "approved")
        self.assertEqual(self.store.claim(cid)["amount_cents"], 200_000)


class PriceBreachTest(FundingBase):
    def _setup_price(self, committed, actual_month="2026-04"):
        sc = self.sc
        sc.demand("2026-03", 10)
        for i in range(8):
            sc.family(f"family-fund-{i:04d}")
        sc.policy(rules=rules_v(tolerance=0))
        sc.supply_run("2026-03")
        sc.fund_run("2026-03")
        self.store.commit_price(sc.room, committed, "2026-01-01")
        return sc

    def test_price_hike_disallows_operating_subsidy(self):
        sc = self._setup_price(200_000)
        # 4 月实际收费 250000 > 承诺 200000，涨价违约
        sc.demand("2026-04", 10)
        sc.supply_run("2026-04")
        self.store.report_price(sc.room, "2026-04", 250_000, "监督员")
        r = sc.fund_run("2026-04")
        self.assertEqual(
            [c for c in r["created"] if c["action"] == "运营补助"], [])

    def test_price_hike_after_payment_triggers_clawback(self):
        sc = self._setup_price(200_000)
        sc.demand("2026-04", 10)
        sc.supply_run("2026-04")
        # 先按正常价完成 4 月测算并发了补助
        r_paid = sc.fund_run("2026-04")
        self.assertTrue(any(c["action"] == "运营补助" for c in r_paid["created"]))
        # 随后查实 4 月实际涨价，重算 4 月 -> 价格违约追回
        self.store.report_price(sc.room, "2026-04", 250_000, "监督员")
        r = sc.fund_run("2026-04")
        self.assertEqual(len(r["clawbacks"]), 1)
        self.assertEqual(r["clawbacks"][0]["reason"], "价格违约")
        self.assertEqual(r["clawbacks"][0]["amount"], 8 * 30_000)

    def test_price_drop_does_not_breach(self):
        # 普惠承诺的是收费上限，中途降价不违约、不追回
        sc = self._setup_price(200_000)
        sc.demand("2026-04", 10)
        sc.supply_run("2026-04")
        self.store.report_price(sc.room, "2026-04", 150_000, "监督员")
        r = sc.fund_run("2026-04")
        self.assertTrue(any(c["action"] == "运营补助" for c in r["created"]))
        self.assertEqual(r["clawbacks"], [])

    def test_price_within_tolerance_allowed(self):
        sc = self.sc
        sc.demand("2026-03", 10)
        for i in range(8):
            sc.family(f"family-fund-{i:04d}")
        sc.policy(rules=rules_v(tolerance=2_000))
        sc.supply_run("2026-03")
        self.store.commit_price(sc.room, 200_000, "2026-01-01")
        self.store.report_price(sc.room, "2026-03", 201_500, "监督员")
        r = sc.fund_run("2026-03")
        self.assertTrue(any(c["action"] == "运营补助" for c in r["created"]))


class SuspensionClawbackTest(FundingBase):
    def test_midmonth_suspension_claws_back_paid_subsidy(self):
        sc = self.sc
        sc.demand("2026-03", 10)
        for i in range(8):
            sc.family(f"family-fund-{i:04d}")
        sc.policy()
        sc.supply_run("2026-03")
        first = sc.fund_run("2026-03")  # 先发 3 月运营补助
        op = next(c for c in first["created"] if c["action"] == "运营补助")
        # 机构 3 月中旬暂停，重算 3 月 -> 停业追回
        self.store.transition_provider(sc.provider, "暂停", "2026-03-15",
                                       "违规停业整顿", "tester")
        again = sc.fund_run("2026-03")
        self.assertEqual(len(again["clawbacks"]), 1)
        self.assertEqual(again["clawbacks"][0]["reason"], "停业")
        self.assertEqual(again["clawbacks"][0]["amount"], op["amount"])


class ExitClawbackTest(FundingBase):
    def test_early_exit_proportional_construction_clawback(self):
        sc, r3 = self._funded()
        # 1 月启用领建设补助，承诺 12 个月，7 月退出 -> 服务 7 个月，追回 5/12
        self.store.transition_provider(sc.provider, "退出", "2026-07-15",
                                       "关停", "tester")
        r = sc.fund_run("2026-07")
        build = [c for c in r["clawbacks"] if c["reason"] == "提前退出"]
        self.assertEqual(len(build), 1)
        self.assertEqual(build[0]["amount"], 8_000_000 * 5 // 12)

    def test_exit_after_commitment_no_clawback(self):
        sc, _ = self._funded()
        # 承诺 12 个月，满 12 个月后退出 -> 不追回建设补助
        self.store.transition_provider(sc.provider, "退出", "2026-12-31",
                                       "到期", "tester")
        r = sc.fund_run("2026-12")
        self.assertEqual(
            [c for c in r["clawbacks"] if c["reason"] == "提前退出"], [])


class RetroactivePolicyTest(FundingBase):
    def _paid_under_v1(self):
        sc = self.sc
        sc.demand("2026-03", 10)
        for i in range(8):
            sc.family(f"family-fund-{i:04d}")
        sc.policy(version="v1", rules=rules_v(operation=30_000))
        sc.supply_run("2026-03")
        sc.fund_run("2026-01")
        r = sc.fund_run("2026-03")
        op = next(c for c in r["created"] if c["action"] == "运营补助")
        return sc, op

    def test_retroactive_increase_creates_supplement(self):
        sc, op = self._paid_under_v1()
        sc.policy(version="v2", effective="2026-02", retroactive=True,
                  rules=rules_v(operation=40_000))
        r = sc.fund_run("2026-03")
        self.assertEqual(len(r["supplements"]), 1)
        self.assertEqual(r["supplements"][0]["amount"], 8 * 10_000)
        self.assertEqual(r["supplements"][0]["root_claim_id"], op["claim_id"])
        # 净支付 = 24万 + 8万
        self.assertEqual(self.store.net_paid_cents(op["claim_id"]), 320_000)

    def test_retroactive_decrease_creates_clawback(self):
        sc, op = self._paid_under_v1()
        sc.policy(version="v2-low", effective="2026-01", retroactive=True,
                  rules=rules_v(operation=20_000))
        r = sc.fund_run("2026-03")
        self.assertEqual(r["supplements"], [])
        self.assertEqual(len(r["clawbacks"]), 1)
        self.assertEqual(r["clawbacks"][0]["reason"], "政策标准下调")
        self.assertEqual(r["clawbacks"][0]["amount"], 8 * 10_000)
        self.assertEqual(self.store.net_paid_cents(op["claim_id"]), 160_000)

    def test_rerun_same_policy_is_idempotent(self):
        sc, op = self._paid_under_v1()
        sc.policy(version="v2", effective="2026-02", retroactive=True,
                  rules=rules_v(operation=40_000))
        sc.fund_run("2026-03")
        # 同口径再次重算：旧调整作废后重新生成，净支付稳定
        again = sc.fund_run("2026-03")
        self.assertEqual(len(again["supplements"]), 1)
        self.assertEqual(again["supplements"][0]["amount"], 80_000)
        self.assertEqual(again["clawbacks"], [])
        self.assertEqual(self.store.net_paid_cents(op["claim_id"]), 320_000)

    def test_successive_policy_versions_adjust_net(self):
        sc, op = self._paid_under_v1()
        # v2 上调到 4 万
        sc.policy(version="v2", effective="2026-02", retroactive=True,
                  rules=rules_v(operation=40_000))
        sc.fund_run("2026-03")
        self.assertEqual(self.store.net_paid_cents(op["claim_id"]), 320_000)
        # v3 追溯下调到 2.5 万：净支付应回落至 20 万（追回 12 万）
        sc.policy(version="v3", effective="2026-01", retroactive=True,
                  rules=rules_v(operation=25_000))
        r = sc.fund_run("2026-03")
        self.assertEqual(r["clawbacks"][0]["amount"], 120_000)
        self.assertEqual(self.store.net_paid_cents(op["claim_id"]), 200_000)


class ExplainabilityTest(FundingBase):
    def test_explain_claim_names_policy_and_rule(self):
        sc, r = self._funded()
        cid = r["created"][-1]["claim_id"]
        ex = sc.funding.explain_claim(cid)
        self.assertEqual(ex["policy"]["version_no"], "v2026.1")
        self.assertEqual(ex["matched_rule"]["action"], "运营补助")
        self.assertEqual(ex["matched_rule"]["amount_cents"], 30_000)
        self.assertEqual(ex["claim"]["basis_qty"], 8)
        self.assertIsNotNone(ex["calc_run"])
        self.assertEqual(ex["net_paid_cents"], 240_000)

    def test_policy_for_month_picks_latest_effective(self):
        sc, _ = self._funded()
        # v2 追溯生效，适用起点设为 2026-01（发布于其后）
        sc.policy(version="v2", effective="2026-01", retroactive=True,
                  rules=rules_v(operation=40_000))
        picked = self.store.policy_for_month("2026-03")
        self.assertEqual(picked["version_no"], "v2")
        # 追溯起点为 1 月，故 1 月也选 v2
        picked_jan = self.store.policy_for_month("2026-01")
        self.assertEqual(picked_jan["version_no"], "v2")

    def test_non_retroactive_policy_does_not_reach_before_effective(self):
        sc, _ = self._funded()
        # v2 从 2026-02 起、非追溯：1 月仍适用 v1
        sc.policy(version="v2", effective="2026-02", retroactive=False,
                  rules=rules_v(operation=40_000))
        self.assertEqual(
            self.store.policy_for_month("2026-01")["version_no"], "v2026.1")
        self.assertEqual(
            self.store.policy_for_month("2026-02")["version_no"], "v2")

    def test_explicit_policy_can_recompute_history(self):
        # 追溯政策即便生效月晚于目标月，也可通过显式指定 policy_id 重算历史
        sc, _ = self._funded()
        v2 = sc.policy(version="v2", effective="2026-02", retroactive=True,
                       rules=rules_v(operation=40_000))
        # 1 月只有建设补助（容量单价两版相同），无补差；流程不报错
        r = sc.fund_run("2026-01", policy_id=v2)
        self.assertEqual(r["policy_version_no"], "v2")


if __name__ == "__main__":
    unittest.main()
