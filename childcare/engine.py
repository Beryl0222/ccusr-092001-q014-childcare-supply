"""供需与资金测算引擎。

所有月度结论通过不可变的 calc_runs 表达：重算生成新运行，旧运行置为
superseded 但快照保留，因此每条建议、每笔补助都能回溯到当时的口径。
资金侧每次政策重算在原申报上挂“补差/追回”调整链，净支付 =
原申报 + 有效补差 - 累计追回。
"""

from __future__ import annotations

import json
from collections import defaultdict

from .domain import (
    SLOT_TYPES,
    ConflictError,
    NotFoundError,
    check_month,
    month_between,
    require,
)
from .storage import Store


# =====================================================================
# 供需侧
# =====================================================================
class SupplyEngine:
    def __init__(self, store: Store):
        self.store = store

    def run_month(self, month: str, actor: str, note: str | None = None,
                  force: bool = False) -> str:
        """冻结某月供需快照：重算作废旧运行并重新派位，旧快照仍可查。

        若已有更晚月份的冻结运行，默认拒绝重算——常规在托自配位月起按月
        延续，重写历史起始月会破坏后续月份的座位连续性；确需重开历史须
        从最新月份起逐月重算，或显式 force（仅用于纠错）。
        """
        check_month(month)
        if not force and self.store.has_frozen_supply_after(month):
            raise ConflictError(
                f"{month} 之后已有冻结的供需运行，不能直接重算历史月份；"
                "请从最新月份起逐月重算，或显式声明 force 纠错")
        run_id = self.store.create_run("supply", month, actor, note=note)
        self.store.rewind_supply_month(month)

        # 街道 -> 班型 -> 容量池（本街道与跨街道合计）
        rooms = self.store.effective_classrooms(month)
        pools: dict[tuple[str, str], list[dict]] = defaultdict(list)
        capacity_totals: dict[tuple[str, str], dict] = defaultdict(
            lambda: {"capacity": 0, "shared": 0})
        for r in rooms:
            served = self.store.service_streets(r["site_id"])
            entry = {
                "classroom_id": r["classroom_id"],
                "capacity": r["compliant_capacity"],
                "home_street": r["home_street"],
            }
            for street in served:
                pools[(street, r["slot_type"])].append(entry)
                capacity_totals[(street, r["slot_type"])]["capacity"] += (
                    r["compliant_capacity"])
                if street != r["home_street"]:
                    capacity_totals[(street, r["slot_type"])]["shared"] += (
                        r["compliant_capacity"])

        new_alloc_keys: dict[tuple[str, str], dict] = defaultdict(
            lambda: {"allocated": 0, "urgent": 0})

        # 候补派位：紧迫优先、其次分数、再按登记先后；容量在 allocate 事务内强校验
        for fam in self.store.waiting_families(month):
            target = self._pick_classroom(
                pools.get((fam["street"], fam["slot_type"]), []),
                month, fam["street"])
            if target is None:
                continue
            try:
                self.store.allocate(fam["id"], target, month, run_id)
            except ConflictError:
                continue
            k = (fam["street"], fam["slot_type"])
            new_alloc_keys[k]["allocated"] += 1
            new_alloc_keys[k]["urgent"] += 1 if fam["urgent"] else 0

        demand_rows = self.store.demand_for_month(month)
        demand_map: dict[tuple[str, str], dict] = {}
        streets: set[str] = set()
        for d in demand_rows:
            demand_map[(d["street"], d["slot_type"])] = {
                "demand": d["demand"], "urgent": d["urgent"]}
            streets.add(d["street"])
        for street, _slot in pools:
            streets.add(street)
        # 即便当月无需求、供给也已关闭，仍保留全部建档街道，便于看到归零
        streets |= self.store.known_streets()

        # 覆盖口径：当月在托（含历史月份延续配位，按家庭去重）。
        # 仅数当月新派位会低估覆盖、虚增缺口；暂停/退出机构下的孩子不计入。
        coverage = self.store.coverage_by_street(month)
        # 派位后仍 waiting 的家庭即未满足候补（匿名计数，不含任何身份信息）
        waiting_counts: dict[tuple[str, str], int] = defaultdict(int)
        for fam in self.store.waiting_families(month):
            waiting_counts[(fam["street"], fam["slot_type"])] += 1

        snapshot = []
        for street in sorted(streets):
            for slot_type in SLOT_TYPES:
                dm = demand_map.get((street, slot_type),
                                    {"demand": 0, "urgent": 0})
                totals = capacity_totals[(street, slot_type)]
                cov = coverage.get((street, slot_type),
                                   {"allocated": 0, "urgent": 0})
                # 实际未覆盖需求 = 聚合需求 - 当月在托人数（含跨月延续）。
                # 不能用“需求-可及容量”：跨街道共享座位会被两条街道重复计入，
                # 实际在托才消解争用，故在托数反映真实覆盖。
                gap = max(0, dm["demand"] - cov["allocated"])
                urgent_gap = max(0, dm["urgent"] - cov["urgent"])
                snapshot.append({
                    "street": street,
                    "slot_type": slot_type,
                    "demand": dm["demand"],
                    "urgent_demand": dm["urgent"],
                    "accessible_capacity": totals["capacity"],
                    "shared_capacity": totals["shared"],
                    "allocated": cov["allocated"],
                    "newly_allocated": new_alloc_keys[
                        (street, slot_type)]["allocated"],
                    "urgent_allocated": cov["urgent"],
                    "waitlist_count": waiting_counts[(street, slot_type)],
                    "gap": gap,
                    "urgent_gap": urgent_gap,
                })
        self.store.save_supply_snapshot(run_id, snapshot)
        return run_id

    def _pick_classroom(self, classrooms: list[dict], month: str,
                        family_street: str) -> str | None:
        """在可服务该街道的班型中选一个有空额的；本街道班型优先，尽量保留跨街道名额。"""
        candidates = []
        with self.store.tx() as c:
            for room in classrooms:
                used = self.store.occupancy_in_month(c, room["classroom_id"], month)
                remaining = room["capacity"] - used
                if remaining <= 0:
                    continue
                candidates.append((0 if room["home_street"] == family_street else 1,
                                   -remaining, room["classroom_id"]))
        if not candidates:
            return None
        candidates.sort()
        return candidates[0][2]

    # ---------- 建设建议与缺口缓解 ----------
    def recommend(self, street: str, slot_type: str, months: list[str],
                  proposed_capacity: int, actor: str,
                  site_id: str | None = None, phase_id: str | None = None,
                  rationale: str | None = None) -> str:
        """登记建设建议并逐月测算缓解量。

        缓解口径：以各月最近一次供需快照的缺口为准，relieved = min(缺口, 建议容量)，
        逐月明细连同所引用的 run_id 一起落库，可直接回答“缓解了哪部分缺口”。
        """
        require(slot_type in SLOT_TYPES, f"未知托位类型：{slot_type}")
        require(isinstance(proposed_capacity, int) and proposed_capacity > 0,
                "建议新增容量应为正整数")
        months = sorted(set(months))
        require(months, "至少覆盖一个月份")
        for m in months:
            check_month(m)

        if phase_id:
            with self.store.tx() as c:
                phase_row = c.execute(
                    "SELECT * FROM site_phases WHERE id=?", (phase_id,)).fetchone()
            if phase_row is None:
                raise NotFoundError(f"分期不存在：{phase_id}")
            if site_id is None:
                site_id = phase_row["site_id"]
            require(phase_row["site_id"] == site_id, "建议绑定的场地与分期不一致")
        if site_id:
            self.store.get_site(site_id)

        relief_rows = []
        gap_before_total = 0
        anchor_run_id = None
        for m in months:
            run = self.store.latest_run("supply", m)
            if run is None:
                raise ConflictError(f"月份 {m} 尚无供需测算结果，无法评估建议")
            anchor_run_id = anchor_run_id or run["id"]
            snap = next((dict(r) for r in self.store.supply_snapshot(run["id"])
                         if r["street"] == street and r["slot_type"] == slot_type),
                        None)
            if snap is None:
                continue
            gap_before = snap["gap"]
            gap_before_total += gap_before
            relieved = min(gap_before, proposed_capacity)
            if relieved > 0:
                relief_rows.append({
                    "run_id": run["id"],
                    "street": street,
                    "slot_type": slot_type,
                    "month": m,
                    "gap_before": gap_before,
                    "relieved": relieved,
                })

        if rationale is None:
            covered = sum(r["relieved"] for r in relief_rows)
            rationale = (f"拟在{street}新增{slot_type}托位 {proposed_capacity} 个，"
                         f"覆盖 {len(months)} 个月，预计缓解缺口 {covered} 个")
        return self.store.save_recommendation(
            anchor_run_id, street, slot_type, months, gap_before_total,
            proposed_capacity, relief_rows, rationale,
            site_id=site_id, phase_id=phase_id)

    def explain_recommendation(self, rec_id: str) -> dict:
        return self.store.recommendation(rec_id)

    def monthly_snapshot(self, month: str) -> dict:
        check_month(month)
        run = self.store.latest_run("supply", month)
        if run is None:
            raise NotFoundError(f"月份 {month} 尚无供需测算结果")
        return {
            "run_id": run["id"],
            "month": month,
            "status": run["status"],
            "rows": [dict(r) for r in self.store.supply_snapshot(run["id"])],
        }


# =====================================================================
# 资金侧
# =====================================================================
class FundingEngine:
    """按月计提补助，并对历史申报做版本化核对（补差/追回）。"""

    ONE_TIME_BASES = {"capacity_one_time", "award_one_time"}

    def __init__(self, store: Store):
        self.store = store

    # ---------- 主入口 ----------
    def run_month(self, month: str, actor: str,
                  policy_id: str | None = None,
                  note: str | None = None) -> dict:
        check_month(month)
        policy_row = (self.store.get_policy(policy_id) if policy_id
                      else self.store.policy_for_month(month))
        if policy_row is None:
            raise ConflictError(f"月份 {month} 没有适用的政策版本")
        run_id = self.store.create_run(
            "funding", month, actor, policy_id=policy_row["id"], note=note)
        rules = self.store.policy_rules(policy_row["id"])

        due = self._compute_entitlements(month, rules)
        summary = self._reconcile(month, run_id, policy_row, due)
        commitment_claws = self._check_construction_commitments(month, run_id, rules)
        summary["clawbacks"].extend(commitment_claws)
        return {"run_id": run_id, "month": month,
                "policy_id": policy_row["id"],
                "policy_version_no": policy_row["version_no"], **summary}

    # ---------- 应发 entitlement 计算 ----------
    def _compute_entitlements(self, month: str, rules: list) -> dict[str, dict]:
        """返回 {业务键(dedupe_key): entitlement}。"""
        due: dict[str, dict] = {}
        rooms = self.store.effective_classrooms(month)
        # 在托人数跨月延续：自配位月起持续占座（临时体验除外）
        enroll = self.store.enrolled_counts(month)

        providers = {r["provider_id"]: self.store.get_provider(r["provider_id"])
                     for r in rooms}
        sites_with_operating_provider = {p["site_id"]: pid
                                         for pid, p in providers.items()}

        for rule in rules:
            action, basis = rule["action"], rule["basis"]
            if basis in ("capacity_one_time", "enrolled_monthly"):
                for room in rooms:
                    if rule["slot_type"] and rule["slot_type"] != room["slot_type"]:
                        continue
                    if basis == "capacity_one_time":
                        if room["opened_month"] != month:
                            continue  # 建设补助仅在班型启用当月计提一次
                        qty = room["compliant_capacity"]
                        amount = qty * rule["amount_cents"]
                    else:
                        qty = enroll.get(room["classroom_id"], 0)
                        amount = qty * rule["amount_cents"]
                        if amount > 0 and self._price_breached(
                                room["classroom_id"], month,
                                rule["price_tolerance_cents"]):
                            # 实际收费高于承诺价（含容差）：当月运营补助不予计提
                            qty, amount = 0, 0
                    if amount <= 0:
                        continue
                    key = self.store.claim_dedupe_key(
                        room["provider_id"], action, room["classroom_id"], month)
                    self._put_due(due, key, action, month, amount, qty,
                                  room["provider_id"], room["classroom_id"],
                                  room["site_id"], rule)
            elif basis == "site_monthly":
                for site_id, pid in sites_with_operating_provider.items():
                    p = providers[pid]
                    if rule["tag"] and rule["tag"] not in json.loads(p["tags"]):
                        continue
                    key = self.store.claim_dedupe_key(pid, action, None, month)
                    self._put_due(due, key, action, month, rule["amount_cents"],
                                  1, pid, None, site_id, rule)
            elif basis in ("provider_monthly", "award_one_time"):
                for pid, p in providers.items():
                    if rule["tag"] and rule["tag"] not in json.loads(p["tags"]):
                        continue
                    if basis == "award_one_time":
                        with self.store.tx() as c:
                            first = self.store.provider_first_month_in_state(
                                c, pid, "运营")
                        if first != month:
                            continue
                    key = self.store.claim_dedupe_key(pid, action, None, month)
                    self._put_due(due, key, action, month, rule["amount_cents"],
                                  1, pid, None, p["site_id"], rule)
        return due

    def _put_due(self, due: dict, key: str, action: str, month: str,
                 amount: int, qty: int, provider_id: str,
                 classroom_id: str | None, site_id: str | None, rule) -> None:
        if key in due:
            raise ConflictError(f"政策规则在同一口径上重复计提：{key}")
        due[key] = {
            "action": action, "month": month, "amount": amount, "qty": qty,
            "provider_id": provider_id, "classroom_id": classroom_id,
            "site_id": site_id, "rule_id": rule["id"],
        }

    def _price_breached(self, classroom_id: str, month: str,
                        tolerance_cents: int) -> bool:
        """实际收费高于承诺价 + 容差即违约。

        说明：普惠机构承诺的是收费上限，中途降价不构成违约、不触发追回
        （价格留痕仍保留供监管复核）；只有突破上限的涨价才冲减运营补助。
        """
        with self.store.tx() as c:
            comm = c.execute(
                "SELECT monthly_fee_cents FROM price_commitments WHERE classroom_id=?",
                (classroom_id,)).fetchone()
            actual = c.execute(
                "SELECT actual_fee_cents FROM price_reports WHERE classroom_id=? AND month=?",
                (classroom_id, month)).fetchone()
        if comm is None or actual is None:
            return False
        return actual["actual_fee_cents"] > comm["monthly_fee_cents"] + tolerance_cents

    # ---------- 核对：申报、补差、追回 ----------
    def _reconcile(self, month: str, run_id: str, policy_row,
                   due: dict[str, dict]) -> dict:
        created, supplements, clawbacks, voided = [], [], [], []

        with self.store.tx() as c:
            prior_rows = [dict(r) for r in c.execute(
                "SELECT * FROM claims WHERE month=? AND original_claim_id IS NULL "
                "AND status<>'void' ORDER BY created_at", (month,)).fetchall()]

        for old in prior_rows:
            key = old["dedupe_key"]
            ent = due.get(key)

            if old["status"] == "submitted":
                declared = old["amount_cents"]
                if ent is None or declared > ent["amount"]:
                    reason = (f"{month} 不符合计提条件（停业/不合规/未启用/价格违约）"
                              if ent is None
                              else f"申报 {declared} 分超过核定 {ent['amount']} 分")
                    self.store.settle_declaration(
                        old["id"], policy_row["id"], run_id, 0, 0, True,
                        reason + "，申报驳回以防套取补助")
                    voided.append(old["id"])
                    if ent is not None and declared > ent["amount"]:
                        # 驳回虚高申报后，按核定额生成系统核定单
                        self._create_approved(run_id, policy_row, ent, created)
                else:
                    self.store.settle_declaration(
                        old["id"], policy_row["id"], run_id, ent["qty"], declared,
                        False,
                        f"机构申报 {declared} 分，核定应发 {ent['amount']} 分，"
                        "未超报，按申报额发放")
                continue

            # 已核定/已支付的历史申报：与本版政策口径下的“当前净支付”比较
            net_paid = self.store.net_paid_cents(old["id"])
            if ent is None:
                # 当月不再符合计提条件（停业/退出/不合规/价格违约）：全额追回
                if net_paid > 0:
                    reason = self._ineligibility_reason(old)
                    cb = self.store.add_clawback(old["id"], run_id, reason, net_paid)
                    clawbacks.append({"claim_id": old["id"], "amount": net_paid,
                                      "reason": reason, "clawback_id": cb})
            elif net_paid < ent["amount"]:
                delta = ent["amount"] - net_paid
                cid = self.store.record_claim(
                    old["provider_id"], old["action"], month, policy_row["id"],
                    ent["qty"], delta, run_id,
                    classroom_id=old["classroom_id"], site_id=old["site_id"],
                    original_claim_id=old["id"], rule_id=ent["rule_id"],
                    status="supplement",
                    note=(f"按政策 {policy_row['version_no']} 重算补差 {delta} 分"
                          + ("（追溯生效）" if policy_row["retroactive"] else "")))
                supplements.append({"claim_id": cid, "amount": delta,
                                    "root_claim_id": old["id"]})
            elif net_paid > ent["amount"]:
                delta = net_paid - ent["amount"]
                reason = self._downgrade_reason(old, ent, policy_row)
                cb = self.store.add_clawback(old["id"], run_id, reason, delta)
                clawbacks.append({"claim_id": old["id"], "amount": delta,
                                  "reason": reason, "clawback_id": cb})
            self.store.link_claim_to_run(old["id"], run_id)

        # 全新出现的应发项
        for key, ent in due.items():
            if any(r["dedupe_key"] == key for r in prior_rows):
                continue
            self._create_approved(run_id, policy_row, ent, created)

        return {"created": created, "supplements": supplements,
                "clawbacks": clawbacks, "voided": voided}

    def _create_approved(self, run_id: str, policy_row, ent: dict,
                         created: list) -> None:
        cid = self.store.record_claim(
            ent["provider_id"], ent["action"], ent["month"], policy_row["id"],
            ent["qty"], ent["amount"], run_id,
            classroom_id=ent["classroom_id"], site_id=ent["site_id"],
            rule_id=ent["rule_id"],
            note=f"依据政策 {policy_row['version_no']} 按月核定")
        created.append({"claim_id": cid, "action": ent["action"],
                        "amount": ent["amount"], "qty": ent["qty"]})

    def _ineligibility_reason(self, old_claim: dict) -> str:
        pid, month = old_claim["provider_id"], old_claim["month"]
        with self.store.tx() as c:
            state = self.store.provider_state_in_month(c, pid, month)
        if state == "退出":
            return "提前退出"
        if state == "暂停":
            return "停业"
        if old_claim["action"] == "运营补助":
            return "价格违约"
        return "政策标准下调"

    def _downgrade_reason(self, old_claim: dict, ent: dict, policy_row) -> str:
        if old_claim["policy_id"] != policy_row["id"]:
            return "政策标准下调"
        return self._ineligibility_reason(old_claim)

    # ---------- 建设补助的最低运营承诺 ----------
    def _check_construction_commitments(self, month: str, run_id: str,
                                        rules: list) -> list:
        """机构当月（或已）退出且运营未满承诺月数，按比例追回建设补助。"""
        result = []
        rule = next((r for r in rules if r["action"] == "建设补助"
                     and r["min_commitment_months"] > 0), None)
        if rule is None:
            return result
        with self.store.tx() as c:
            exits = [dict(r) for r in c.execute(
                "SELECT provider_id FROM provider_events WHERE to_state='退出' AND month<=?",
                (month,)).fetchall()]
        for e in exits:
            pid = e["provider_id"]
            for old in self.store.one_time_claims(pid, "建设补助"):
                if self.store.clawback_total(old["id"]) > 0:
                    continue
                served = len(month_between(old["month"], month))
                required = rule["min_commitment_months"]
                if served >= required:
                    continue
                recover = old["amount_cents"] * (required - served) // required
                if recover > 0:
                    cb = self.store.add_clawback(old["id"], run_id, "提前退出", recover)
                    self.store.set_claim_note(
                        old["id"],
                        f"机构于 {month} 前退出，承诺运营 {required} 个月、"
                        f"实际 {served} 个月，按比例追回建设补助 {recover} 分")
                    result.append({"claim_id": old["id"], "amount": recover,
                                   "reason": "提前退出", "clawback_id": cb})
        return result

    # ---------- 解释 ----------
    def explain_claim(self, claim_id: str) -> dict:
        """回答“某笔补助采用了哪版政策、如何计算、是否被追回”。"""
        claim = self.store.claim(claim_id)
        root_id = claim["original_claim_id"] or claim["id"]
        root = self.store.claim(root_id)
        policy = self.store.get_policy(claim["policy_id"])
        rules = {r["id"]: dict(r) for r in self.store.policy_rules(policy["id"])}
        provider = self.store.get_provider(claim["provider_id"])
        run = self.store.get_run(claim["run_id"]) if claim["run_id"] else None
        clawbacks = [dict(x) for x in
                     self.store.clawbacks_for_claim(root_id)]
        with self.store.tx() as c:
            children = [dict(r) for r in c.execute(
                "SELECT * FROM claims WHERE original_claim_id=? ORDER BY created_at",
                (root_id,)).fetchall()]
        matched = rules.get(claim["rule_id"]) if claim["rule_id"] else None
        return {
            "claim": {k: claim[k] for k in
                      ("id", "action", "month", "basis_qty", "amount_cents",
                       "status", "note", "classroom_id", "provider_id",
                       "rule_id", "original_claim_id")},
            "chain_root": root_id,
            "net_paid_cents": self.store.net_paid_cents(root_id),
            "provider": {"id": provider["id"], "name": provider["name"]},
            "policy": {"id": policy["id"], "version_no": policy["version_no"],
                       "effective_month": policy["effective_month"],
                       "retroactive": bool(policy["retroactive"])},
            "matched_rule": matched,
            "calc_run": ({"id": run["id"], "month": run["month"],
                          "status": run["status"]} if run else None),
            "clawbacks": clawbacks,
            "adjustments": children,
        }

    def run_summary(self, run_id: str) -> dict:
        run = self.store.get_run(run_id)
        require(run["kind"] == "funding", "该运行不是资金测算")
        claims = [dict(c) for c in self.store.claims_for_run(run_id)]
        # 净支付合计：该运行涉及的每条申报链去重到根后求净支付
        chain_roots = {c["original_claim_id"] or c["id"] for c in claims}
        total = sum(self.store.net_paid_cents(r) for r in chain_roots)
        return {"run": dict(run), "claims": claims,
                "chain_count": len(chain_roots),
                "net_paid_total_cents": total}
