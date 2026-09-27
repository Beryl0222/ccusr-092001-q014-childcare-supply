"""月度供需与资金测算引擎。

每次测算为当月生成一个新快照(seq 递增), 旧快照与已确认补助保留:
- 供需: 需求(网格聚合)+匿名候补 对 运营机构有效容量(按服务范围分摊到街道);
- 资金: 按当月应适用的政策版本计算应发额, 与已确认台账对冲,
  追溯生效的新政策自动产生补差或补助追回行, 全程可解释。
"""

import json
from collections import defaultdict

from .domain import CLASS_TYPES, check_month
from .errors import Conflict, NotFound, ValidationError
from .store import (_now, allocated, audit, effective_capacity, get_provider,
                    get_site, price_at, status_at)


# ---------------------------------------------------------------- 政策选择

def _pick_policy(conn, action, class_type, month):
    """选取某月应适用的政策版本。

    在适用起始月不晚于当月的版本中, 班型专属优先于通用,
    其余按发布月、政策序号取最新; 发布月晚于当月但适用起始月
    早于当月的版本即为追溯生效, 重算时会被选中。
    """
    rows = conn.execute(
        "SELECT * FROM policies WHERE action=? AND applies_from<=?"
        " AND (class_type='' OR class_type=?)",
        (action, month, class_type),
    ).fetchall()
    if not rows:
        return None
    rows.sort(
        key=lambda r: (bool(class_type) and r["class_type"] == class_type,
                       r["published_month"], r["policy_id"]),
        reverse=True,
    )
    return rows[0]


def _params(policy):
    return json.loads(policy["params"])


# ---------------------------------------------------------------- 补助台账

def _net_paid(conn, provider_id, month, class_type, action):
    """某(机构, 月, 班型, 动作)的已确认净额(追回计负)。"""
    r = conn.execute(
        """
        SELECT COALESCE(SUM(CASE WHEN action='补助追回' THEN -amount
                                 ELSE amount END), 0) AS net
        FROM subsidies
        WHERE provider_id=? AND month=? AND class_type=? AND status='确认'
          AND (action=? OR (action='补助追回' AND base_action=?))
        """,
        (provider_id, month, class_type, action, action),
    ).fetchone()
    return r["net"]


def _confirmed_keys(conn, provider_id, month):
    """机构当月已有确认台账的(动作, 班型)键集合。"""
    keys = set()
    for r in conn.execute(
            "SELECT action, class_type, base_action FROM subsidies"
            " WHERE provider_id=? AND month=? AND status='确认'",
            (provider_id, month)):
        if r["action"] == "补助追回":
            keys.add((r["base_action"], r["class_type"]))
        else:
            keys.add((r["action"], r["class_type"]))
    return keys


def _subsidy_targets(conn, provider, month, status):
    """计算机构当月的应发补助(目标额), 返回行列表。"""
    targets = []
    pid = provider["provider_id"]
    if status == "退出":
        return targets
    site = get_site(conn, provider["site_id"])

    # 建设补助: 当月有阶段新启用(一次性, 按启用托位计)
    if status in ("建设", "备案", "运营") and site["compliance"] == "合格":
        for ct in CLASS_TYPES:
            started = conn.execute(
                "SELECT COALESCE(SUM(capacity),0) AS s FROM site_phases"
                " WHERE site_id=? AND class_type=? AND start_month=?",
                (site["site_id"], ct, month),
            ).fetchone()["s"]
            if not started:
                continue
            pol = _pick_policy(conn, "建设补助", ct, month)
            if pol:
                per = _params(pol)["per_slot"]
                targets.append({
                    "action": "建设补助", "class_type": ct,
                    "quantity": started, "amount": started * per,
                    "policy_id": pol["policy_id"],
                    "inputs": {"new_slots": started, "per_slot": per},
                })

    if status != "运营":
        return targets

    # 运营补助: 按已分配托位计, 须有价格承诺且不超过政策价格上限
    for ct in CLASS_TYPES:
        alloc = allocated(conn, pid, month, ct)
        if not alloc:
            continue
        pol = _pick_policy(conn, "运营补助", ct, month)
        if not pol:
            continue
        params = _params(pol)
        price = price_at(conn, pid, ct, month)
        cap_price = params.get("price_cap")
        if price is None or (cap_price is not None and price > cap_price):
            continue
        targets.append({
            "action": "运营补助", "class_type": ct,
            "quantity": alloc, "amount": alloc * params["per_slot"],
            "policy_id": pol["policy_id"],
            "inputs": {"allocated": alloc, "price": price,
                       "per_slot": params["per_slot"]},
        })

    # 租金减免: 按场地类型定额
    pol = _pick_policy(conn, "租金减免", "", month)
    if pol:
        params = _params(pol)
        kinds = params.get("kinds")
        if not kinds or site["kind"] in kinds:
            targets.append({
                "action": "租金减免", "class_type": "",
                "quantity": 1, "amount": params["per_month"],
                "policy_id": pol["policy_id"],
                "inputs": {"site_kind": site["kind"]},
            })

    # 示范奖励: 示范机构定额
    if provider["model"]:
        pol = _pick_policy(conn, "示范奖励", "", month)
        if pol:
            targets.append({
                "action": "示范奖励", "class_type": "",
                "quantity": 1, "amount": _params(pol)["per_month"],
                "policy_id": pol["policy_id"],
                "inputs": {"model": True},
            })
    return targets


# ---------------------------------------------------------------- 月度测算

def calculate_month(conn, month, note="", actor=""):
    """对某月执行完整测算, 生成新快照; 须在事务内调用。

    返回各口径行数。未确认的旧测算行作废, 已确认台账不动;
    应发与已确认净额的差额自动生成补差或补助追回行。
    """
    check_month(month)
    seq = conn.execute(
        "SELECT COALESCE(MAX(seq),0) AS s FROM snapshots WHERE month=?",
        (month,)).fetchone()["s"] + 1
    snapshot_id = conn.execute(
        "INSERT INTO snapshots(month, seq, note, created_at) VALUES (?,?,?,?)",
        (month, seq, note or "", _now()),
    ).lastrowid
    conn.execute(
        "UPDATE subsidies SET status='废止' WHERE month=? AND status='测算'",
        (month,))

    providers = conn.execute("SELECT * FROM providers").fetchall()
    statuses = {p["provider_id"]: status_at(conn, p["provider_id"], month)
                for p in providers}

    # 供给: 运营机构的有效容量按服务范围权重分摊到街道
    supply = defaultdict(int)
    for p in providers:
        pid = p["provider_id"]
        if statuses[pid] != "运营":
            continue
        site = get_site(conn, p["site_id"])
        areas = conn.execute(
            "SELECT street, weight FROM service_areas WHERE provider_id=?",
            (pid,)).fetchall()
        if not areas:
            areas = [{"street": site["street"], "weight": 1.0}]
        total_w = sum(a["weight"] for a in areas)
        anchor = max(areas, key=lambda a: a["weight"])["street"]
        for ct in CLASS_TYPES:
            cap = effective_capacity(conn, pid, ct, month)
            if cap <= 0:
                continue
            assigned = 0
            for a in areas:
                share = int(cap * a["weight"] / total_w)
                supply[(a["street"], ct)] += share
                assigned += share
            supply[(anchor, ct)] += cap - assigned  # 舍入余量归入权重最大街道

    # 需求与候补
    demand = defaultdict(int)
    for r in conn.execute(
            "SELECT street, class_type, SUM(demand) AS d FROM grid_demand"
            " WHERE month=? GROUP BY street, class_type", (month,)):
        demand[(r["street"], r["class_type"])] = r["d"]
    waiting = defaultdict(int)
    for r in conn.execute(
            "SELECT street, class_type, COUNT(*) AS c FROM waitlist"
            " WHERE month=? GROUP BY street, class_type", (month,)):
        waiting[(r["street"], r["class_type"])] = r["c"]

    # 缺口明细
    n_gap = 0
    gaps = {}
    for key in sorted(set(demand) | set(waiting) | set(supply)):
        street, ct = key
        d, w, s = demand[key], waiting[key], supply[key]
        gap = d + w - s
        conn.execute(
            "INSERT INTO gap_lines(snapshot_id, street, class_type, demand,"
            " waitlist, supply, gap) VALUES (?,?,?,?,?,?,?)",
            (snapshot_id, street, ct, d, w, s, gap),
        )
        gaps[key] = gap
        n_gap += 1

    # 建设建议: 对每个正缺口给出新增托位建议及其覆盖的网格明细
    n_rec = 0
    for (street, ct), gap in sorted(gaps.items()):
        if gap <= 0:
            continue
        detail = _grid_breakdown(conn, month, street, ct, supply[(street, ct)])
        conn.execute(
            "INSERT INTO recommendations(snapshot_id, street, class_type,"
            " add_slots, covered_gap, residual_gap, detail)"
            " VALUES (?,?,?,?,?,?,?)",
            (snapshot_id, street, ct, gap, gap, 0,
             json.dumps(detail, ensure_ascii=False)),
        )
        n_rec += 1

    # 补助: 应发与已确认台账对冲, 差额入账
    n_sub = n_adj = 0
    for p in providers:
        pid = p["provider_id"]
        targets = _subsidy_targets(conn, p, month, statuses[pid])
        tmap = {(t["action"], t["class_type"]): t for t in targets}
        for key in sorted(set(tmap) | _confirmed_keys(conn, pid, month)):
            action, ct = key
            t = tmap.get(key)
            target_amt = t["amount"] if t else 0
            delta = target_amt - _net_paid(conn, pid, month, ct, action)
            if delta > 0:
                conn.execute(
                    "INSERT INTO subsidies(snapshot_id, provider_id, month,"
                    " action, class_type, policy_id, quantity, amount, status,"
                    " inputs, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,'测算',?,?)",
                    (snapshot_id, pid, month, action, ct, t["policy_id"],
                     t["quantity"], delta,
                     json.dumps(t["inputs"], ensure_ascii=False), _now()),
                )
                n_sub += 1
            elif delta < 0:
                original = conn.execute(
                    "SELECT subsidy_id, policy_id FROM subsidies"
                    " WHERE provider_id=? AND month=? AND class_type=?"
                    " AND action=? AND status='确认'"
                    " ORDER BY subsidy_id DESC LIMIT 1",
                    (pid, month, ct, action),
                ).fetchone()
                # 追回行记录导致调整的现行政策版本(无应发时沿用原政策)
                policy_id = t["policy_id"] if t else original["policy_id"]
                conn.execute(
                    "INSERT INTO subsidies(snapshot_id, provider_id, month,"
                    " action, class_type, base_action, policy_id, quantity,"
                    " amount, status, clawback_of, reason, inputs, created_at)"
                    " VALUES (?,?,?,'补助追回',?,?,?,0,?,'测算',?,?,?,?)",
                    (snapshot_id, pid, month, ct, action,
                     policy_id, -delta, original["subsidy_id"],
                     "政策追溯调整", json.dumps(
                         {"target": target_amt}, ensure_ascii=False), _now()),
                )
                n_adj += 1

    audit(conn, actor, "月度测算", f"month:{month}",
          {"seq": seq, "gap_lines": n_gap, "recommendations": n_rec,
           "subsidy_lines": n_sub, "adjustments": n_adj})
    return {"snapshot_id": snapshot_id, "month": month, "seq": seq,
            "gap_lines": n_gap, "recommendations": n_rec,
            "subsidy_lines": n_sub, "adjustments": n_adj}


def _grid_breakdown(conn, month, street, class_type, supply):
    """把街道级缺口拆到网格, 供建设建议解释"缓解了哪部分缺口"。"""
    needs = defaultdict(int)
    for r in conn.execute(
            "SELECT grid, SUM(demand) AS d FROM grid_demand"
            " WHERE month=? AND street=? AND class_type=? GROUP BY grid",
            (month, street, class_type)):
        needs[r["grid"]] += r["d"]
    for r in conn.execute(
            "SELECT grid, COUNT(*) AS c FROM waitlist"
            " WHERE month=? AND street=? AND class_type=? GROUP BY grid",
            (month, street, class_type)):
        needs[r["grid"]] += r["c"]
    total = sum(needs.values())
    covered = []
    for grid in sorted(needs):
        share = supply * needs[grid] // total if total else 0
        unmet = needs[grid] - share
        if unmet > 0:
            covered.append({"grid": grid, "unmet": unmet})
    return covered


# ---------------------------------------------------------------- 确认与追回

def _get_subsidy(conn, subsidy_id):
    r = conn.execute(
        "SELECT * FROM subsidies WHERE subsidy_id=?", (subsidy_id,)).fetchone()
    if r is None:
        raise NotFound(f"补助记录不存在: {subsidy_id}")
    return r


def confirm_subsidy(conn, subsidy_id, actor=""):
    """确认一笔测算中的补助。

    防重复申报: 仅最新快照中的测算行可确认; 旧快照行已废止,
    同一键的应付净额由台账对冲保证, 不会重复发放。
    """
    line = _get_subsidy(conn, subsidy_id)
    if line["status"] != "测算":
        raise Conflict(f"仅测算状态的补助可确认(当前: {line['status']})")
    if line["snapshot_id"] is None:
        raise Conflict("手工记录无需确认")
    seq = conn.execute(
        "SELECT seq FROM snapshots WHERE snapshot_id=?",
        (line["snapshot_id"],)).fetchone()["seq"]
    latest = conn.execute(
        "SELECT COALESCE(MAX(seq),0) AS s FROM snapshots WHERE month=?",
        (line["month"],)).fetchone()["s"]
    if seq != latest:
        raise Conflict("该测算已被更新版本取代, 请确认最新快照中的记录")
    conn.execute(
        "UPDATE subsidies SET status='确认' WHERE subsidy_id=?", (subsidy_id,))
    audit(conn, actor, "确认补助", f"subsidy:{subsidy_id}",
          {"month": line["month"], "action": line["action"],
           "amount": line["amount"]})
    return dict(conn.execute(
        "SELECT * FROM subsidies WHERE subsidy_id=?", (subsidy_id,)).fetchone())


def clawback(conn, subsidy_id, amount, reason, actor=""):
    """对已确认补助发起追回(如重复申报、中途违约、停业)。

    追回行直接入账为确认状态, 追回总额不得超过该键已确认净额。
    """
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise ValidationError(f"追回金额必须为正整数: {amount!r}")
    if not reason or not reason.strip():
        raise ValidationError("追回须说明原因")
    line = _get_subsidy(conn, subsidy_id)
    if line["action"] == "补助追回":
        raise ValidationError("不能对追回记录再发起追回")
    if line["status"] != "确认":
        raise Conflict("仅已确认的补助可追回")
    paid = _net_paid(conn, line["provider_id"], line["month"],
                     line["class_type"], line["action"])
    if amount > paid:
        raise Conflict(f"追回金额({amount})超过已确认净额({paid})")
    new_id = conn.execute(
        "INSERT INTO subsidies(snapshot_id, provider_id, month, action,"
        " class_type, base_action, policy_id, quantity, amount, status,"
        " clawback_of, reason, inputs, created_at)"
        " VALUES (NULL,?,?,'补助追回',?,?,?,0,?,'确认',?,?,?,?)",
        (line["provider_id"], line["month"], line["class_type"],
         line["action"], line["policy_id"], amount, subsidy_id,
         reason.strip(), json.dumps({"manual": True}, ensure_ascii=False),
         _now()),
    ).lastrowid
    audit(conn, actor, "补助追回", f"subsidy:{new_id}",
          {"of": subsidy_id, "amount": amount, "reason": reason.strip()})
    return new_id


# ---------------------------------------------------------------- 查询与解释

def _latest_snapshot(conn, month):
    r = conn.execute(
        "SELECT * FROM snapshots WHERE month=? ORDER BY seq DESC LIMIT 1",
        (month,)).fetchone()
    if r is None:
        raise NotFound(f"{month}尚未测算")
    return r


def gaps(conn, month):
    """当月最新快照的街道×班型供需缺口。"""
    check_month(month)
    snap = _latest_snapshot(conn, month)
    lines = [dict(r) for r in conn.execute(
        "SELECT street, class_type, demand, waitlist, supply, gap"
        " FROM gap_lines WHERE snapshot_id=? ORDER BY street, class_type",
        (snap["snapshot_id"],))]
    return {"month": month, "seq": snap["seq"], "lines": lines}


def funding(conn, month):
    """当月资金台账: 最新快照的测算行 + 已确认净额。"""
    check_month(month)
    snap = _latest_snapshot(conn, month)
    lines = [dict(r) for r in conn.execute(
        """
        SELECT s.subsidy_id, s.provider_id, s.action, s.class_type,
               s.base_action, s.quantity, s.amount, s.status, s.reason,
               s.clawback_of, p.version AS policy_version
        FROM subsidies s JOIN policies p ON p.policy_id = s.policy_id
        WHERE s.month=? AND s.status != '废止'
          AND (s.snapshot_id=? OR s.snapshot_id IS NULL)
        ORDER BY s.subsidy_id
        """,
        (month, snap["snapshot_id"]))]
    nets = [dict(r) for r in conn.execute(
        """
        SELECT provider_id,
               CASE WHEN action='补助追回' THEN base_action ELSE action
                   END AS action,
               class_type,
               SUM(CASE WHEN action='补助追回' THEN -amount
                        ELSE amount END) AS confirmed_net
        FROM subsidies
        WHERE month=? AND status='确认'
        GROUP BY provider_id,
                 CASE WHEN action='补助追回' THEN base_action ELSE action END,
                 class_type
        ORDER BY provider_id, action, class_type
        """,
        (month,))]
    return {"month": month, "seq": snap["seq"],
            "lines": lines, "confirmed_net": nets}


def recommendations(conn, month):
    """当月最新快照的建设建议列表。"""
    check_month(month)
    snap = _latest_snapshot(conn, month)
    rows = conn.execute(
        "SELECT rec_id, street, class_type, add_slots, covered_gap,"
        " residual_gap FROM recommendations WHERE snapshot_id=?"
        " ORDER BY rec_id",
        (snap["snapshot_id"],)).fetchall()
    return {"month": month, "seq": snap["seq"],
            "lines": [dict(r) for r in rows]}


def explain_recommendation(conn, rec_id):
    """解释某项建设建议缓解了哪部分缺口(网格级)。"""
    r = conn.execute(
        "SELECT * FROM recommendations WHERE rec_id=?", (rec_id,)).fetchone()
    if r is None:
        raise NotFound(f"建设建议不存在: {rec_id}")
    snap = conn.execute(
        "SELECT month, seq FROM snapshots WHERE snapshot_id=?",
        (r["snapshot_id"],)).fetchone()
    return {
        "rec_id": r["rec_id"],
        "month": snap["month"], "seq": snap["seq"],
        "street": r["street"], "class_type": r["class_type"],
        "add_slots": r["add_slots"],
        "covered_gap": r["covered_gap"],
        "residual_gap": r["residual_gap"],
        "covered": json.loads(r["detail"]),
    }


def explain_subsidy(conn, subsidy_id):
    """解释某笔补助: 采用了哪版政策、计费输入与关联追回。"""
    line = _get_subsidy(conn, subsidy_id)
    pol = conn.execute(
        "SELECT * FROM policies WHERE policy_id=?",
        (line["policy_id"],)).fetchone()
    snap = None
    if line["snapshot_id"] is not None:
        snap = dict(conn.execute(
            "SELECT month, seq FROM snapshots WHERE snapshot_id=?",
            (line["snapshot_id"],)).fetchone())
    clawbacks = [dict(r) for r in conn.execute(
        "SELECT subsidy_id, amount, reason, status, created_at"
        " FROM subsidies WHERE clawback_of=? ORDER BY subsidy_id",
        (subsidy_id,))]
    return {
        "subsidy_id": line["subsidy_id"],
        "provider_id": line["provider_id"],
        "month": line["month"],
        "action": line["action"],
        "class_type": line["class_type"],
        "base_action": line["base_action"],
        "amount": line["amount"],
        "quantity": line["quantity"],
        "status": line["status"],
        "reason": line["reason"],
        "clawback_of": line["clawback_of"],
        "inputs": json.loads(line["inputs"] or "{}"),
        "policy": {
            "policy_id": pol["policy_id"],
            "version": pol["version"],
            "action": pol["action"],
            "class_type": pol["class_type"],
            "applies_from": pol["applies_from"],
            "published_month": pol["published_month"],
            "params": json.loads(pol["params"]),
        },
        "snapshot": snap,
        "clawbacks": clawbacks,
    }


def list_audit(conn, limit=200):
    rows = conn.execute(
        "SELECT id, at, role, action, entity, detail FROM audit_log"
        " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]
