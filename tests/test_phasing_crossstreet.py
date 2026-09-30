"""场景一/二：同一场地分阶段启用、跨街道服务。"""

import unittest

from childcare import ConflictError, ValidationError
from tests.conftest import Scenario, StoreTest


class PhasedOpeningTest(StoreTest):
    def test_phase_two_only_counts_after_open(self):
        sc = self.sc
        sc.demand("2026-03", 10)
        # 8 个家庭候补，一期容量 8 全部可配
        for i in range(10):
            sc.family(f"family-aaaa-{i:04d}")
        sc.supply_run("2026-03")
        self.assertEqual(sc.snap()["accessible_capacity"], 8)
        self.assertEqual(sc.snap()["gap"], 2)

        # 二期：合规但排期 2026-05 才启用 -> 3、4 月不计入供给
        phase2 = self.store.add_phase(
            sc.site, 2, "二期", "合规", 12, open_month="2026-05")
        room2 = self.store.create_classroom(
            sc.provider, phase2, "托小B", "托小班", 12, "2026-05")
        sc.supply_run("2026-04")
        self.assertEqual(sc.snap(month="2026-04")["accessible_capacity"], 8)

        # 5 月二期启用，容量并入（8+12=20）
        sc.supply_run("2026-05")
        self.assertEqual(sc.snap(month="2026-05")["accessible_capacity"], 20)

    def test_noncompliant_phase_excluded(self):
        sc = self.sc
        sc.demand("2026-03", 30)
        # 二期容量 12 但仍在整改中 -> 不产生合规托位
        self.store.add_phase(sc.site, 2, "二期", "整改中", 12,
                             open_month="2026-02")
        sc.supply_run("2026-03")
        self.assertEqual(sc.snap()["accessible_capacity"], 8)

        # 整改通过后（同月）即计入
        phase_rows = self.store.phases(sc.site)
        p2 = next(p for p in phase_rows if p["seq"] == 2)
        self.store.update_phase_compliance(p2["id"], "合规", 12)
        room = self.store.create_classroom(
            sc.provider, p2["id"], "托小B", "托小班", 12, "2026-02")
        sc.supply_run("2026-03")
        self.assertEqual(sc.snap()["accessible_capacity"], 20)

    def test_phase_closed_month_excluded(self):
        sc = self.sc
        # 一期仅开放到 2026-04，5 月关闭
        self.store.set_phase_schedule(sc.phase, "2026-01", "2026-04")
        sc.supply_run("2026-04")
        self.assertEqual(sc.snap(month="2026-04")["accessible_capacity"], 8)
        sc.supply_run("2026-05")
        self.assertEqual(sc.snap(month="2026-05")["accessible_capacity"], 0)

    def test_classroom_total_cannot_exceed_phase_capacity(self):
        sc = self.sc
        # 一期合规上限 20，已有 8，再加 15 -> 合计 23 被拒
        with self.assertRaises(ValidationError):
            self.store.create_classroom(
                sc.provider, sc.phase, "托小C", "托小班", 15, "2026-02")


class CrossStreetTest(StoreTest):
    def _two_street_demand(self):
        sc = self.sc
        # 东街场地容量 8，西街无场地
        self.store.upsert_grid("g2", "西街一网格", "西街")
        sc.demand("2026-03", 10)                       # 东街需求 10
        self.store.upsert_demand("g2", "2026-03", "托小班", 6)  # 西街需求 6
        return sc

    def test_no_cross_service_before_registration(self):
        sc = self._two_street_demand()
        # 西街家庭无法配位（场地未备案服务西街）
        for i in range(6):
            sc.family(f"west-family-{i:04d}", street="西街")
        for i in range(10):
            sc.family(f"east-family-{i:04d}", street="东街")
        sc.supply_run("2026-03")
        west = sc.snap(street="西街")
        self.assertEqual(west["accessible_capacity"], 0)
        self.assertEqual(west["allocated"], 0)
        self.assertEqual(west["gap"], 6)

    def test_cross_service_after_registration(self):
        sc = self._two_street_demand()
        # 备案跨街道服务西街
        self.store.add_service_street(sc.site, "西街")
        for i in range(10):
            sc.family(f"east-family-{i:04d}", street="东街")
        for i in range(6):
            sc.family(f"west-family-{i:04d}", street="西街")
        sc.supply_run("2026-03")
        east = sc.snap(street="东街")
        west = sc.snap(street="西街")
        # 同一 8 个托位对两条街道都“可及”，西街共享容量=8
        self.assertEqual(east["accessible_capacity"], 8)
        self.assertEqual(west["accessible_capacity"], 8)
        self.assertEqual(west["shared_capacity"], 8)
        # 本街道优先：东街先占满，物理总量只有 8，故总配位 <= 8
        total = east["allocated"] + west["allocated"]
        self.assertLessEqual(total, 8)
        self.assertEqual(east["allocated"], 8)
        # 西街家庭因名额被本街道优先占满而未配位
        self.assertEqual(west["allocated"], 0)
        self.assertEqual(west["gap"], 6)

    def test_allocate_to_unregistered_street_rejected(self):
        sc = self._two_street_demand()
        wid = sc.family("west-family-0001", street="西街")
        # 直接尝试派到东街场地理应被拒（跨街道未备案）
        run = sc.supply.run_month("2026-03", "tester")
        with self.assertRaises(ConflictError):
            self.store.allocate(wid, sc.room, "2026-03", run)


if __name__ == "__main__":
    unittest.main()
