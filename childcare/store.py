"""数据访问层: 所有写操作须在 IMMEDIATE 事务内调用(见 db.transact)。

容量不变量(并发安全):
  任一(机构, 月份, 班型)的已分配托位 ≤ min(申报容量, 场地分阶段合规容量),
  且场地须为"合格"。分配、申报容量调整、场地阶段调整都在同一事务内校验。
"""

import json
import sqlite3
from datetime import datetime, timezone

from .domain import (ACTIONS, COMPLIANCES, TRANSITIONS, check_action,
                     check_class_type, check_month, check_status)
from .errors import Conflict, NotFound, ValidationError


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _nonneg_int(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(f"{label}必须为非负整数: {value!r}")
    return value


def _positive_int(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationError(f"{label}必须为正整数: {value!r}")
    return value


def _require_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label}不能为空")
    return value.strip()


def _row(conn, sql, args, what):
    r = conn.execute(sql, args).fetchone()
    if r is None:
        raise NotFound(f"{what}不存在")
    return r


def audit(conn, role, action, entity, detail=""):
    """写审计日志; detail 不得包含家庭标识等敏感明细。"""
    if not isinstance(detail, str):
        detail = json.dumps(detail, ensure_ascii=False, sort_keys=True)
    conn.execute(
        "INSERT INTO audit_log(at, role, action, entity, detail) VALUES (?,?,?,?,?)",
        (_now(), role, action, entity, detail),
    )


# ---------------------------------------------------------------- 需求(聚合口径)

def upsert_demand(conn, grid, street, month, class_type, demand, actor=""):
    """上报人口网格聚合需求; 同网格同月同班型重复上报按覆盖处理。"""
    _require_text(grid, "网格")
    _require_text(street, "街道")
    check_month(month)
    check_class_type(class_type)
    demand = _nonneg_int(demand, "需求人数")
    conn.execute(
        """
        INSERT INTO grid_demand(grid, street, month, class_type, demand, updated_at)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(grid, month, class_type)
        DO UPDATE SET street=excluded.street, demand=excluded.demand,
                      updated_at=excluded.updated_at
        """,
        (grid, street, month, class_type, demand, _now()),
    )
    audit(conn, actor, "上报需求", f"grid:{grid}",
          {"month": month, "class_type": class_type, "demand": demand})


# ---------------------------------------------------------------- 场地与分阶段启用

def create_site(conn, site_id, street, kind, actor=""):
    _require_text(site_id, "场地编号")
    _require_text(street, "街道")
    _require_text(kind, "场地类型")
    try:
        conn.execute(
            "INSERT INTO sites(site_id, street, kind, compliance, created_at)"
            " VALUES (?,?,?,'待评估',?)",
            (site_id, street, kind, _now()),
        )
    except sqlite3.IntegrityError:
        raise Conflict(f"场地已存在: {site_id}")
    audit(conn, actor, "登记场地", f"site:{site_id}", {"street": street, "kind": kind})


def get_site(conn, site_id):
    return _row(conn, "SELECT * FROM sites WHERE site_id=?", (site_id,), "场地")


def set_compliance(conn, site_id, compliance, note="", actor=""):
    """登记合规结论(合格/不合格/待评估)。"""
    if compliance not in COMPLIANCES:
        raise ValidationError(f"未知合规结论: {compliance!r}, 应为 {list(COMPLIANCES)}")
    get_site(conn, site_id)
    conn.execute(
        "UPDATE sites SET compliance=?, compliance_note=? WHERE site_id=?",
        (compliance, note or "", site_id),
    )
    audit(conn, actor, "合规结论", f"site:{site_id}",
          {"compliance": compliance, "note": note or ""})


def _site_cap_at(conn, site_id, class_type, month, phases=None):
    """场地在某月的合规容量: 该月已启用且未停用的各阶段容量之和。"""
    if phases is None:
        phases = conn.execute(
            "SELECT start_month, end_month, capacity FROM site_phases"
            " WHERE site_id=? AND class_type=?",
            (site_id, class_type),
        ).fetchall()
    return sum(
        p["capacity"] if isinstance(p, sqlite3.Row) else p["capacity"]
        for p in phases
        if p["start_month"] <= month
        and (p["end_month"] is None or p["end_month"] >= month)
    )


def upsert_phase(conn, site_id, phase, start_month, end_month, class_type,
                 capacity, actor=""):
    """登记/调整场地阶段(分阶段启用); 下调容量受已分配托位守卫约束。"""
    site = get_site(conn, site_id)
    _positive_int(phase, "阶段序号")
    check_month(start_month, "启用月")
    if end_month is not None:
        check_month(end_month, "停用月")
        if end_month < start_month:
            raise ValidationError("停用月早于启用月")
    check_class_type(class_type)
    capacity = _nonneg_int(capacity, "阶段容量")

    # 在内存中应用变更, 校验该场地机构既有分配不突破新的合规上限
    phases = [dict(p) for p in conn.execute(
        "SELECT phase, start_month, end_month, capacity FROM site_phases"
        " WHERE site_id=? AND class_type=?",
        (site_id, class_type),
    )]
    phases = [p for p in phases if p["phase"] != phase]
    phases.append({"phase": phase, "start_month": start_month,
                   "end_month": end_month, "capacity": capacity})
    provider = conn.execute(
        "SELECT provider_id FROM providers WHERE site_id=?", (site_id,)).fetchone()
    if provider:
        pid = provider["provider_id"]
        for alloc in conn.execute(
                "SELECT month, allocated FROM allocations"
                " WHERE provider_id=? AND class_type=?", (pid, class_type)):
            cap = _site_cap_at(conn, site_id, class_type, alloc["month"], phases)
            eff = _effective(conn, pid, class_type, alloc["month"],
                             site=site, site_cap=cap)
            if alloc["allocated"] > eff:
                raise Conflict(
                    f"场地阶段调整将使 {alloc['month']} 已分配托位"
                    f"({alloc['allocated']})超过合规上限({eff})")

    conn.execute(
        """
        INSERT INTO site_phases(site_id, phase, start_month, end_month,
                                class_type, capacity)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(site_id, phase, class_type)
        DO UPDATE SET start_month=excluded.start_month,
                      end_month=excluded.end_month, capacity=excluded.capacity
        """,
        (site_id, phase, start_month, end_month, class_type, capacity),
    )
    audit(conn, actor, "场地阶段", f"site:{site_id}",
          {"phase": phase, "start_month": start_month, "end_month": end_month,
           "class_type": class_type, "capacity": capacity})


# ---------------------------------------------------------------- 机构与状态机

def register_provider(conn, provider_id, site_id, name, month, model=False, actor=""):
    """机构备案登记, 初始状态为"规划"。"""
    _require_text(provider_id, "机构编号")
    _require_text(name, "机构名称")
    check_month(month, "登记月")
    get_site(conn, site_id)
    try:
        conn.execute(
            "INSERT INTO providers(provider_id, site_id, name, model, created_at)"
            " VALUES (?,?,?,?,?)",
            (provider_id, site_id, name, 1 if model else 0, _now()),
        )
    except sqlite3.IntegrityError:
        raise Conflict("机构编号已存在或场地已被其他机构占用")
    conn.execute(
        "INSERT INTO provider_status_log(provider_id, to_status, month, reason, at)"
        " VALUES (?,?,?,?,?)",
        (provider_id, "规划", month, "登记", _now()),
    )
    audit(conn, actor, "机构登记", f"provider:{provider_id}",
          {"site_id": site_id, "month": month})


def get_provider(conn, provider_id):
    return _row(conn, "SELECT * FROM providers WHERE provider_id=?",
                (provider_id,), "机构")


def status_at(conn, provider_id, month):
    """推导机构在某月的状态。

    临时停办(暂停)若带预计恢复月, 过了恢复月自动回到停办前状态。
    """
    rows = conn.execute(
        "SELECT to_status, month, end_month FROM provider_status_log"
        " WHERE provider_id=? AND month<=? ORDER BY month, id",
        (provider_id, month),
    ).fetchall()
    status = "规划"
    for r in rows:
        if (r["to_status"] == "暂停" and r["end_month"]
                and month > r["end_month"]):
            continue  # 临时停办已结束
        status = r["to_status"]
    return status


def change_status(conn, provider_id, to_status, month, end_month=None,
                  reason="", actor=""):
    """状态变更; 临时停办(暂停)可附预计恢复月, 到期自动恢复。"""
    get_provider(conn, provider_id)
    check_status(to_status)
    check_month(month, "生效月")
    if to_status == "暂停" and end_month is not None:
        check_month(end_month, "预计恢复月")
        if end_month < month:
            raise ValidationError("预计恢复月早于停办月")
    current = status_at(conn, provider_id, month)
    if current == to_status:
        raise ValidationError(f"机构已处于{to_status}状态")
    if to_status not in TRANSITIONS.get(current, set()):
        raise ValidationError(f"不允许从{current}变更为{to_status}")
    conn.execute(
        "INSERT INTO provider_status_log"
        " (provider_id, to_status, month, end_month, reason, at)"
        " VALUES (?,?,?,?,?,?)",
        (provider_id, to_status, month, end_month, reason or "", _now()),
    )
    audit(conn, actor, "状态变更", f"provider:{provider_id}",
          {"to": to_status, "month": month, "end_month": end_month,
           "reason": reason or ""})


def set_service_areas(conn, provider_id, areas, actor=""):
    """设置跨街道服务范围(整体替换); areas: [{"street":..,"weight":..}]。"""
    get_provider(conn, provider_id)
    if not isinstance(areas, list) or not areas:
        raise ValidationError("服务范围不能为空")
    seen = set()
    for a in areas:
        street = _require_text(a.get("street"), "街道")
        weight = a.get("weight", 1.0)
        if not isinstance(weight, (int, float)) or isinstance(weight, bool) \
                or weight <= 0:
            raise ValidationError(f"服务权重必须为正数: {weight!r}")
        if street in seen:
            raise ValidationError(f"服务街道重复: {street}")
        seen.add(street)
    conn.execute("DELETE FROM service_areas WHERE provider_id=?", (provider_id,))
    conn.executemany(
        "INSERT INTO service_areas(provider_id, street, weight) VALUES (?,?,?)",
        [(provider_id, a["street"].strip(), a.get("weight", 1.0)) for a in areas],
    )
    audit(conn, actor, "服务范围", f"provider:{provider_id}",
          {"streets": sorted(seen)})


# ---------------------------------------------------------------- 容量与价格

def _declared_at(conn, provider_id, class_type, month, history=None):
    """机构申报容量在某月的取值(最近一次不晚于该月的申报)。"""
    if history is None:
        r = conn.execute(
            "SELECT capacity FROM capacities"
            " WHERE provider_id=? AND class_type=? AND month<=?"
            " ORDER BY month DESC LIMIT 1",
            (provider_id, class_type, month),
        ).fetchone()
        return r["capacity"] if r else 0
    candidates = [m for m in history if m <= month]
    return history[max(candidates)] if candidates else 0


def _effective(conn, provider_id, class_type, month, site=None, site_cap=None):
    """有效容量 = min(申报容量, 场地合规容量); 场地不合格则为 0。"""
    p = get_provider(conn, provider_id)
    if site is None:
        site = get_site(conn, p["site_id"])
    if site["compliance"] != "合格":
        return 0
    if site_cap is None:
        site_cap = _site_cap_at(conn, p["site_id"], class_type, month)
    return min(_declared_at(conn, provider_id, class_type, month), site_cap)


def effective_capacity(conn, provider_id, class_type, month):
    return _effective(conn, provider_id, class_type, month)


def set_capacity(conn, provider_id, class_type, month, capacity, actor=""):
    """申报/调整班型容量; 下调不得使任一月份已分配托位超过合规上限。"""
    p = get_provider(conn, provider_id)
    check_class_type(class_type)
    check_month(month, "生效月")
    capacity = _nonneg_int(capacity, "申报容量")

    history = {r["month"]: r["capacity"] for r in conn.execute(
        "SELECT month, capacity FROM capacities"
        " WHERE provider_id=? AND class_type=?", (provider_id, class_type))}
    history[month] = capacity
    site = get_site(conn, p["site_id"])
    for alloc in conn.execute(
            "SELECT month, allocated FROM allocations"
            " WHERE provider_id=? AND class_type=? AND month>=?",
            (provider_id, class_type, month)):
        declared = _declared_at(conn, provider_id, class_type,
                                alloc["month"], history)
        site_cap = _site_cap_at(conn, p["site_id"], class_type, alloc["month"])
        eff = min(declared, site_cap) if site["compliance"] == "合格" else 0
        if alloc["allocated"] > eff:
            raise Conflict(
                f"容量调整将使 {alloc['month']} 已分配托位"
                f"({alloc['allocated']})超过合规上限({eff})")

    conn.execute(
        """
        INSERT INTO capacities(provider_id, class_type, month, capacity)
        VALUES (?,?,?,?)
        ON CONFLICT(provider_id, class_type, month)
        DO UPDATE SET capacity=excluded.capacity
        """,
        (provider_id, class_type, month, capacity),
    )
    audit(conn, actor, "申报容量", f"provider:{provider_id}",
          {"class_type": class_type, "month": month, "capacity": capacity})


def add_price(conn, provider_id, class_type, month, price, actor=""):
    """登记价格承诺(分/月); 中途调价按新生效月另起一行, 历史保留。"""
    get_provider(conn, provider_id)
    check_class_type(class_type)
    check_month(month, "生效月")
    price = _nonneg_int(price, "承诺价格")
    conn.execute(
        """
        INSERT INTO price_commitments(provider_id, class_type, month, price)
        VALUES (?,?,?,?)
        ON CONFLICT(provider_id, class_type, month)
        DO UPDATE SET price=excluded.price
        """,
        (provider_id, class_type, month, price),
    )
    audit(conn, actor, "价格承诺", f"provider:{provider_id}",
          {"class_type": class_type, "month": month, "price": price})


def price_at(conn, provider_id, class_type, month):
    r = conn.execute(
        "SELECT price FROM price_commitments"
        " WHERE provider_id=? AND class_type=? AND month<=?"
        " ORDER BY month DESC, id DESC LIMIT 1",
        (provider_id, class_type, month),
    ).fetchone()
    return r["price"] if r else None


# ---------------------------------------------------------------- 匿名候补

def add_waitlist(conn, month, class_type, grid, street, pseudonym, actor=""):
    """登记匿名候补; 同一家庭(伪名)同月同班型重复登记自动去重。

    入库内容仅为测算所需: 月份、班型、网格、街道与伪名,
    不含姓名、联系方式等任何家庭明细。
    """
    check_month(month)
    check_class_type(class_type)
    _require_text(grid, "网格")
    _require_text(street, "街道")
    _require_text(pseudonym, "家庭伪名")
    now = _now()
    conn.execute(
        """
        INSERT INTO waitlist(month, class_type, grid, street, pseudonym,
                             first_seen, last_seen)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(month, class_type, pseudonym)
        DO UPDATE SET grid=excluded.grid, street=excluded.street,
                      last_seen=excluded.last_seen
        """,
        (month, class_type, grid, street, pseudonym, now, now),
    )
    audit(conn, actor, "候补登记", f"grid:{grid}",
          {"month": month, "class_type": class_type})


def waitlist_summary(conn, month=None, street=None):
    """候补汇总(仅聚合计数, 任何角色都拿不到伪名明细)。"""
    sql = ("SELECT month, street, class_type, COUNT(*) AS families"
           " FROM waitlist")
    cond, args = [], []
    if month:
        check_month(month)
        cond.append("month=?")
        args.append(month)
    if street:
        cond.append("street=?")
        args.append(street)
    if cond:
        sql += " WHERE " + " AND ".join(cond)
    sql += " GROUP BY month, street, class_type ORDER BY month, street, class_type"
    return [dict(r) for r in conn.execute(sql, args)]


# ---------------------------------------------------------------- 政策版本

def add_policy(conn, version, action, class_type, params, applies_from,
               published_month, actor=""):
    """发布政策版本; applies_from 早于 published_month 即为追溯生效。"""
    _require_text(version, "政策版本")
    check_action(action)
    if action == "补助追回":
        raise ValidationError("补助追回由系统按台账生成, 不可作为政策发布")
    class_type = class_type or ""
    if class_type:
        check_class_type(class_type)
    if not isinstance(params, dict):
        raise ValidationError("政策参数须为对象")
    if action in ("建设补助", "运营补助"):
        _nonneg_int(params.get("per_slot"), "每托位补助额(per_slot)")
    if action in ("租金减免", "示范奖励"):
        _nonneg_int(params.get("per_month"), "每月补助额(per_month)")
    check_month(applies_from, "适用起始月")
    check_month(published_month, "发布月")
    try:
        cur = conn.execute(
            "INSERT INTO policies(version, action, class_type, params,"
            " applies_from, published_month, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (version, action, class_type,
             json.dumps(params, ensure_ascii=False, sort_keys=True),
             applies_from, published_month, _now()),
        )
    except sqlite3.IntegrityError:
        raise Conflict(f"政策版本已存在: {version}/{action}/{class_type or '全部'}")
    audit(conn, actor, "发布政策", f"policy:{version}",
          {"action": action, "class_type": class_type,
           "applies_from": applies_from, "published_month": published_month})
    return cur.lastrowid


def list_policies(conn, action=None):
    if action:
        check_action(action)
        rows = conn.execute(
            "SELECT * FROM policies WHERE action=? ORDER BY policy_id",
            (action,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM policies ORDER BY policy_id").fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 托位分配(并发守卫)

def allocated(conn, provider_id, month, class_type):
    r = conn.execute(
        "SELECT allocated FROM allocations"
        " WHERE provider_id=? AND month=? AND class_type=?",
        (provider_id, month, class_type),
    ).fetchone()
    return r["allocated"] if r else 0


def allocate(conn, provider_id, month, class_type, count, actor=""):
    """分配托位; 在调用方事务内保证已分配总数不超过合规上限。"""
    get_provider(conn, provider_id)
    check_month(month)
    check_class_type(class_type)
    count = _positive_int(count, "分配数量")
    status = status_at(conn, provider_id, month)
    if status != "运营":
        raise Conflict(f"机构{month}状态为{status}, 不能分配托位")
    cap = _effective(conn, provider_id, class_type, month)
    current = allocated(conn, provider_id, month, class_type)
    if current + count > cap:
        raise Conflict(
            f"分配后托位({current + count})将超过{month}合规上限({cap})")
    conn.execute(
        """
        INSERT INTO allocations(provider_id, month, class_type, allocated)
        VALUES (?,?,?,?)
        ON CONFLICT(provider_id, month, class_type)
        DO UPDATE SET allocated = allocated + excluded.allocated
        """,
        (provider_id, month, class_type, count),
    )
    audit(conn, actor, "分配托位", f"provider:{provider_id}",
          {"month": month, "class_type": class_type, "count": count})
    return current + count
