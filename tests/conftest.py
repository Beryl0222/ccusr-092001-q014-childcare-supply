"""测试夹具：构建一个东街党群中心托育点的标准场景。"""

import os
import tempfile
import unittest

from childcare.storage import Store
from childcare.engine import SupplyEngine, FundingEngine


def build_policy_rules(construction=1_000_000, operation=30_000,
                       commitment=12, tolerance=0):
    return [
        {"action": "建设补助", "basis": "capacity_one_time",
         "amount_cents": construction, "slot_type": "托小班",
         "min_commitment_months": commitment},
        {"action": "运营补助", "basis": "enrolled_monthly",
         "amount_cents": operation, "slot_type": "托小班",
         "price_tolerance_cents": tolerance},
    ]


class Scenario:
    """标准场景：东街场地、阳光机构、托小A 班型（容量 8）。"""

    def __init__(self, store: Store, capacity: int = 8,
                 phase_capacity: int = 20, state: str = "运营",
                 open_month: str = "2026-01"):
        self.store = store
        self.supply = SupplyEngine(store)
        self.funding = FundingEngine(store)
        store.upsert_grid("g1", "东街一网格", "东街")
        self.site = store.create_site("党群中心托育点", "东街", address="东街1号")
        self.phase = store.add_phase(
            self.site, 1, "一期", "合规", phase_capacity, open_month=open_month)
        self.provider = store.create_provider(
            "阳光托育", "LIC-001", self.site, "2026-01-10", state=state)
        if capacity > 0:
            self.room = store.create_classroom(
                self.provider, self.phase, "托小A", "托小班",
                capacity, open_month)
        else:
            self.room = None

    def demand(self, month, children, urgent=0, grid="g1", slot="托小班"):
        self.store.upsert_demand(grid, month, slot, children, urgent)

    def family(self, token, street="东街", slot="托小班", month="2026-03",
               urgent=False, score=0, first_seen="2026-02"):
        return self.store.upsert_waitlist(
            token, street, slot, month, urgent, score, first_seen)

    def supply_run(self, month):
        return self.supply.run_month(month, "tester")

    def policy(self, version="v2026.1", effective="2026-01",
               rules=None, retroactive=False, note=None):
        return self.store.create_policy(
            version, effective, rules or build_policy_rules(),
            retroactive=retroactive, note=note)

    def fund_run(self, month, policy_id=None):
        return self.funding.run_month(month, "tester", policy_id=policy_id)

    def snap(self, street="东街", slot="托小班", month="2026-03"):
        rows = self.supply.monthly_snapshot(month)["rows"]
        return next(r for r in rows
                    if r["street"] == street and r["slot_type"] == slot)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.sc = Scenario(self.store)

    def tearDown(self):
        pass


def temp_db_store():
    """供多线程并发测试使用的文件型 SQLite。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)  # 让 Store 自己创建
    return Store(path), path
