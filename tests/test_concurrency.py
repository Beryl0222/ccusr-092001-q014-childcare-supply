"""并发安全：容量更新与派位在竞争下不越过合规上限。

使用文件型 SQLite（:memory: 对每线程是独立库），多线程经 BEGIN IMMEDIATE
串行化写事务；用 Barrier 让竞争同时发生。
"""

import threading
import unittest

from childcare import ConflictError
from childcare.storage import Store
from tests.conftest import Scenario, temp_db_store


class ConcurrentCapacityTest(unittest.TestCase):
    def test_concurrent_allocation_never_exceeds_capacity(self):
        store, path = temp_db_store()
        sc = Scenario(store, capacity=3)
        sc.demand("2026-03", 20)
        # 只创建冻结运行拿 run_id，不触发自动派位（派位由并发线程竞争完成）
        run_id = store.create_run("supply", "2026-03", "init")
        wids = [sc.family(f"family-race-{i:04d}") for i in range(12)]

        results, lock = [], threading.Lock()
        barrier = threading.Barrier(len(wids))

        # 每个线程用独立 Store（独立连接）指向同一文件库
        def worker(wid):
            local = Store(path)
            barrier.wait()
            try:
                local.allocate(wid, sc.room, "2026-03", run_id)
                with lock:
                    results.append("ok")
            except ConflictError:
                with lock:
                    results.append("conflict")
            except Exception as exc:  # noqa: BLE001
                with lock:
                    results.append(("error", repr(exc)))

        threads = [threading.Thread(target=worker, args=(w,)) for w in wids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertNotIn("error", [r[0] if isinstance(r, tuple) else r
                                   for r in results])
        self.assertEqual(results.count("ok"), 3, results)
        self.assertEqual(results.count("conflict"), 9)
        # 落库在配数严格等于容量上限，绝不超过
        self.assertEqual(store.active_allocation_count(sc.room, "2026-03"), 3)

    def test_optimistic_lock_blocks_stale_capacity_update(self):
        store, path = temp_db_store()
        sc = Scenario(store, capacity=5)
        outcomes, lock = {}, threading.Lock()
        barrier = threading.Barrier(2)

        def update(version, new_cap, tag):
            local = Store(path)
            barrier.wait()
            try:
                local.set_capacity(sc.room, new_cap, version, tag)
                with lock:
                    outcomes[tag] = "ok"
            except ConflictError:
                with lock:
                    outcomes[tag] = "conflict"

        # 两个客户端都基于 v1，一个调到 5（不变），一个调到 4；只允许一个成功
        t1 = threading.Thread(target=update, args=(1, 5, "a"))
        t2 = threading.Thread(target=update, args=(1, 4, "b"))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(sorted(outcomes.values()).count("ok"), 1, outcomes)
        self.assertEqual(sorted(outcomes.values()).count("conflict"), 1, outcomes)
        # 输家必须带着新版本号重试才生效
        current = store.get_classroom(sc.room)
        final_version = current["version"]
        store.set_capacity(sc.room, 4, final_version, "retry")
        self.assertEqual(store.get_classroom(sc.room)["compliant_capacity"], 4)
        self.assertEqual(store.get_classroom(sc.room)["version"], final_version + 1)

    def test_capacity_cannot_drop_below_seated_under_race(self):
        store, path = temp_db_store()
        sc = Scenario(store, capacity=5)
        sc.demand("2026-03", 5)
        for i in range(5):
            sc.family(f"family-seat-{i:04d}")
        sc.supply_run("2026-03")  # 5 个全部配位
        # 任何把容量调到在托峰值以下的尝试都被拒绝
        with self.assertRaises(ConflictError):
            store.set_capacity(sc.room, 4, 1, "downsize")
        # 调到不低于 5 可以
        store.set_capacity(sc.room, 6, 1, "upsize")
        self.assertEqual(store.get_classroom(sc.room)["compliant_capacity"], 6)


if __name__ == "__main__":
    unittest.main()
