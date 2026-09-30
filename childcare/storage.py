"""SQLite 存储层。

写入一律走 ``BEGIN IMMEDIATE`` 事务，容量扣减类操作在同一事务内
重读计数并加锁，保证并发下“已分配托位不超过合规上限”。
"""

from __future__ import annotations

import hmac
import json
import os
import sqlite3
import threading
import uuid
from typing import Any, Iterable

from .domain import (
    CAPACITY_ACTIONS,
    PROVIDER_STATES,
    SLOT_TYPES,
    VALID_TRANSITIONS,
    ConflictError,
    NotFoundError,
    ValidationError,
    check_month,
    require,
)

SCHEMA_VERSION = "1"


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 需求侧：只保存网格聚合数据，不保存任何家庭身份信息
CREATE TABLE IF NOT EXISTS grids (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    street TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS demand (
    id TEXT PRIMARY KEY,
    grid_id TEXT NOT NULL REFERENCES grids(id),
    month TEXT NOT NULL,
    slot_type TEXT NOT NULL,
    children_count INTEGER NOT NULL CHECK (children_count >= 0),
    urgent_count INTEGER NOT NULL DEFAULT 0 CHECK (urgent_count >= 0),
    source_batch TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (grid_id, month, slot_type)
);

-- 场地与分期启用
CREATE TABLE IF NOT EXISTS sites (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    street TEXT NOT NULL,
    address TEXT,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS site_phases (
    id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(id),
    seq INTEGER NOT NULL,
    name TEXT NOT NULL,
    compliance_status TEXT NOT NULL DEFAULT '待核验'
        CHECK (compliance_status IN ('合规', '整改中', '不合规', '待核验')),
    compliance_ref TEXT,
    open_month TEXT,
    close_month TEXT,
    compliance_capacity INTEGER NOT NULL DEFAULT 0 CHECK (compliance_capacity >= 0),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (site_id, seq)
);
-- 跨街道服务：场地可服务的街道清单（含本街道）
CREATE TABLE IF NOT EXISTS site_service_streets (
    site_id TEXT NOT NULL REFERENCES sites(id),
    street TEXT NOT NULL,
    PRIMARY KEY (site_id, street)
);

-- 机构备案与状态轨迹
CREATE TABLE IF NOT EXISTS providers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    license_no TEXT NOT NULL UNIQUE,
    site_id TEXT NOT NULL REFERENCES sites(id),
    registered_date TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT '规划' CHECK (state IN (%s)),
    tags TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS provider_events (
    id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL REFERENCES providers(id),
    date TEXT NOT NULL,
    month TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    reason TEXT,
    actor TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 班型容量（乐观锁 version + 事务内分配数校验）
CREATE TABLE IF NOT EXISTS classrooms (
    id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL REFERENCES providers(id),
    phase_id TEXT NOT NULL REFERENCES site_phases(id),
    name TEXT NOT NULL,
    slot_type TEXT NOT NULL CHECK (slot_type IN (%s)),
    compliant_capacity INTEGER NOT NULL CHECK (compliant_capacity >= 0),
    opened_month TEXT NOT NULL,
    closed_month TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'stopped')),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 候补家庭：仅存 HMAC 匿名键，不存姓名/证件/电话等任何明细
CREATE TABLE IF NOT EXISTS waitlist (
    id TEXT PRIMARY KEY,
    anon_key TEXT NOT NULL UNIQUE,
    street TEXT NOT NULL,
    slot_type TEXT NOT NULL,
    target_month TEXT NOT NULL,
    urgent INTEGER NOT NULL DEFAULT 0,
    priority_score INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'waiting'
        CHECK (status IN ('waiting', 'fulfilled', 'expired')),
    first_seen_month TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS allocations (
    id TEXT PRIMARY KEY,
    waitlist_id TEXT NOT NULL REFERENCES waitlist(id),
    classroom_id TEXT NOT NULL REFERENCES classrooms(id),
    month TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'allocated'
        CHECK (status IN ('allocated', 'released')),
    run_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_allocation_family
    ON allocations(waitlist_id, month) WHERE status = 'allocated';
CREATE INDEX IF NOT EXISTS ix_allocations_classroom
    ON allocations(classroom_id, month, status);

-- 价格承诺与实际收费
CREATE TABLE IF NOT EXISTS price_commitments (
    id TEXT PRIMARY KEY,
    classroom_id TEXT NOT NULL REFERENCES classrooms(id) UNIQUE,
    monthly_fee_cents INTEGER NOT NULL CHECK (monthly_fee_cents >= 0),
    committed_date TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS price_reports (
    id TEXT PRIMARY KEY,
    classroom_id TEXT NOT NULL REFERENCES classrooms(id),
    month TEXT NOT NULL,
    actual_fee_cents INTEGER NOT NULL CHECK (actual_fee_cents >= 0),
    reporter TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (classroom_id, month)
);

-- 政策版本（允许追溯生效）
CREATE TABLE IF NOT EXISTS policy_versions (
    id TEXT PRIMARY KEY,
    version_no TEXT NOT NULL UNIQUE,
    effective_month TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'superseded')),
    retroactive INTEGER NOT NULL DEFAULT 0,
    supersedes_policy_id TEXT REFERENCES policy_versions(id),
    note TEXT,
    published_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS policy_rules (
    id TEXT PRIMARY KEY,
    policy_id TEXT NOT NULL REFERENCES policy_versions(id),
    action TEXT NOT NULL CHECK (action IN (%s)),
    slot_type TEXT CHECK (slot_type IS NULL OR slot_type IN (%s)),
    basis TEXT NOT NULL CHECK (basis IN
        ('capacity_one_time', 'enrolled_monthly', 'site_monthly',
         'award_one_time', 'provider_monthly')),
    amount_cents INTEGER NOT NULL CHECK (amount_cents_cents_or_zero),
    min_commitment_months INTEGER NOT NULL DEFAULT 0,
    price_tolerance_cents INTEGER NOT NULL DEFAULT 0,
    tag TEXT
);

-- 申报与补助（dedupe_key 防重复申报；同口径重算通过 original_claim_id 挂补差/追回链）
CREATE TABLE IF NOT EXISTS claims (
    id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL REFERENCES providers(id),
    classroom_id TEXT REFERENCES classrooms(id),
    site_id TEXT REFERENCES sites(id),
    policy_id TEXT REFERENCES policy_versions(id),
    run_id TEXT,
    action TEXT NOT NULL,
    month TEXT NOT NULL,
    basis_qty INTEGER NOT NULL DEFAULT 0,
    amount_cents INTEGER NOT NULL DEFAULT 0,
    rule_id TEXT REFERENCES policy_rules(id),
    status TEXT NOT NULL DEFAULT 'submitted'
        CHECK (status IN ('submitted', 'approved', 'paid', 'void',
                          'supplement', 'clawed_back')),
    dedupe_key TEXT NOT NULL,
    original_claim_id TEXT REFERENCES claims(id),
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_claim_dedupe
    ON claims(dedupe_key) WHERE status <> 'void' AND original_claim_id IS NULL;

CREATE TABLE IF NOT EXISTS clawbacks (
    id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES claims(id),
    run_id TEXT,
    reason TEXT NOT NULL
        CHECK (reason IN ('价格违约', '停业', '提前退出', '政策标准下调', '重复申报')),
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    recovered INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 月度测算运行（不可变；重算生成新行并标记旧行为 superseded）
CREATE TABLE IF NOT EXISTS calc_runs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('supply', 'funding')),
    month TEXT NOT NULL,
    policy_id TEXT REFERENCES policy_versions(id),
    status TEXT NOT NULL DEFAULT 'frozen'
        CHECK (status IN ('frozen', 'superseded')),
    supersedes_run_id TEXT REFERENCES calc_runs(id),
    note TEXT,
    created_by TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
-- 同一口径只允许一个 frozen 运行；NULL 政策（供需侧）需单独的部分索引
CREATE UNIQUE INDEX IF NOT EXISTS uq_run_frozen_policy
    ON calc_runs(kind, month, policy_id)
    WHERE status='frozen' AND policy_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_run_frozen_nopolicy
    ON calc_runs(kind, month)
    WHERE status='frozen' AND policy_id IS NULL;
CREATE TABLE IF NOT EXISTS monthly_supply (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES calc_runs(id),
    street TEXT NOT NULL,
    slot_type TEXT NOT NULL,
    demand INTEGER NOT NULL,
    urgent_demand INTEGER NOT NULL,
    accessible_capacity INTEGER NOT NULL,
    allocated INTEGER NOT NULL,
    newly_allocated INTEGER NOT NULL DEFAULT 0,
    urgent_allocated INTEGER NOT NULL,
    waitlist_count INTEGER NOT NULL,
    gap INTEGER NOT NULL,
    urgent_gap INTEGER NOT NULL,
    shared_capacity INTEGER NOT NULL DEFAULT 0,
    UNIQUE (run_id, street, slot_type)
);
CREATE TABLE IF NOT EXISTS recommendations (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES calc_runs(id),
    site_id TEXT REFERENCES sites(id),
    phase_id TEXT REFERENCES site_phases(id),
    street TEXT NOT NULL,
    slot_type TEXT NOT NULL,
    months TEXT NOT NULL,
    gap_before INTEGER NOT NULL,
    proposed_capacity INTEGER NOT NULL,
    relieved INTEGER NOT NULL,
    gap_after INTEGER NOT NULL,
    rationale TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS recommendation_relief (
    recommendation_id TEXT NOT NULL REFERENCES recommendations(id),
    run_id TEXT NOT NULL REFERENCES calc_runs(id),
    street TEXT NOT NULL,
    slot_type TEXT NOT NULL,
    month TEXT NOT NULL,
    gap_before INTEGER NOT NULL,
    relieved INTEGER NOT NULL,
    PRIMARY KEY (recommendation_id, run_id, street, slot_type, month)
);
CREATE TABLE IF NOT EXISTS run_funding_entries (
    run_id TEXT NOT NULL REFERENCES calc_runs(id),
    claim_id TEXT NOT NULL REFERENCES claims(id),
    PRIMARY KEY (run_id, claim_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (datetime('now')),
    actor TEXT NOT NULL,
    role TEXT NOT NULL,
    action TEXT NOT NULL,
    entity TEXT NOT NULL,
    entity_id TEXT,
    detail TEXT
);
""" % (
    ",".join("'" + s + "'" for s in PROVIDER_STATES),
    ",".join("'" + s + "'" for s in SLOT_TYPES),
    ",".join("'" + s + "'" for s in CAPACITY_ACTIONS),
    ",".join("'" + s + "'" for s in SLOT_TYPES),
)

# SQLite 不能在 CHECK 里写“或为零”的奇怪命名，单独建常规约束
SCHEMA = SCHEMA.replace("CHECK (amount_cents_cents_or_zero)", "CHECK (amount_cents >= 0)")


class Store:
    """线程安全的 SQLite 仓库；每个线程持有独立连接。"""

    def __init__(self, path: str = ":memory:"):
        self.path = path
        self._tls = threading.local()
        self._salt = None  # 首个连接建立时填充
        # 共享缓存内存库的唯一名，避免多个 :memory: 实例互相串数据
        self._mem_name = "ccmem_" + uuid.uuid4().hex
        # 立即建立主连接并初始化；该连接常驻，保证共享内存库不被回收，
        # 文件库下其余连接也共享这里建好的表与 salt。
        self._salt = self._init_connection(self._conn())

    # ---------- 连接与事务 ----------
    def _connect_kwargs(self):
        if self.path == ":memory:":
            # 共享缓存内存库：同进程所有连接看到同一份数据（每连接各自独立的
            # :memory: 会让多线程服务器丢数据）。主连接常驻保证库不被回收。
            return (f"file:{self._mem_name}?mode=memory&cache=shared",
                    {"uri": True})
        return (self.path, {"uri": False})

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            dsn, kwargs = self._connect_kwargs()
            conn = sqlite3.connect(dsn, timeout=15, isolation_level=None, **kwargs)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=15000")
            if self.path != ":memory:":
                conn.execute("PRAGMA journal_mode=WAL")
            salt = self._init_connection(conn)
            if self._salt is None:
                self._salt = salt
            self._tls.conn = conn
        return conn

    def _init_connection(self, conn: sqlite3.Connection) -> bytes:
        """在（可能全新的）连接上建表并取得家庭哈希盐。幂等。"""
        conn.executescript(SCHEMA)
        row = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO meta(key,value) VALUES('schema_version',?)",
                (SCHEMA_VERSION,))
        salt_row = conn.execute(
            "SELECT value FROM meta WHERE key='family_salt'").fetchone()
        if salt_row is None:
            salt = os.urandom(32)
            conn.execute("INSERT INTO meta(key,value) VALUES('family_salt',?)",
                         (salt.hex(),))
        else:
            salt = bytes.fromhex(salt_row["value"])
        return salt

    class _Tx:
        def __init__(self, store: "Store"):
            self.store = store
            self.conn = store._conn()

        def __enter__(self) -> sqlite3.Connection:
            # IMMEDIATE 立即取写锁，串行化所有容量/余额类变更
            self.conn.execute("BEGIN IMMEDIATE")
            return self.conn

        def __exit__(self, exc_type, exc, tb) -> None:
            if exc_type is None:
                self.conn.execute("COMMIT")
            else:
                self.conn.execute("ROLLBACK")

    def tx(self) -> "Store._Tx":
        return Store._Tx(self)

    # ---------- 工具 ----------
    @staticmethod
    def _rows(conn, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return list(conn.execute(sql, tuple(params)))

    @staticmethod
    def _one(conn, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return conn.execute(sql, tuple(params)).fetchone()

    def family_anon_key(self, family_token: str) -> str:
        """对家庭去重标识做 HMAC；原始值绝不落库。"""
        require(isinstance(family_token, str) and len(family_token) >= 6,
                "家庭去重标识长度不足，且不得包含姓名等明文信息")
        return hmac.new(self._salt, family_token.encode("utf-8"), "sha256").hexdigest()

    def audit(self, actor: str, role: str, action: str, entity: str,
              entity_id: str | None = None, detail: dict | None = None) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO audit_log(actor,role,action,entity,entity_id,detail)"
                " VALUES(?,?,?,?,?,?)",
                (actor, role, action, entity, entity_id,
                 json.dumps(detail, ensure_ascii=False) if detail else None),
            )

    # ---------- 网格与需求 ----------
    def upsert_grid(self, grid_id: str, name: str, street: str) -> None:
        require(grid_id and name and street, "网格 id/名称/街道必填")
        with self.tx() as c:
            c.execute(
                "INSERT INTO grids(id,name,street) VALUES(?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, street=excluded.street",
                (grid_id, name, street),
            )

    def upsert_demand(self, grid_id: str, month: str, slot_type: str,
                      children_count: int, urgent_count: int = 0,
                      source_batch: str | None = None) -> None:
        check_month(month)
        require(slot_type in SLOT_TYPES, f"未知托位类型：{slot_type}")
        require(isinstance(children_count, int) and children_count >= 0, "需求数应为非负整数")
        require(isinstance(urgent_count, int) and 0 <= urgent_count <= children_count,
                "紧迫需求数应在 0 与总需求之间")
        with self.tx() as c:
            if self._one(c, "SELECT 1 FROM grids WHERE id=?", (grid_id,)) is None:
                raise NotFoundError(f"网格不存在：{grid_id}")
            c.execute(
                "INSERT INTO demand(id,grid_id,month,slot_type,children_count,"
                "urgent_count,source_batch) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(grid_id,month,slot_type) DO UPDATE SET "
                "children_count=excluded.children_count, urgent_count=excluded.urgent_count,"
                "source_batch=excluded.source_batch",
                (_uid("dem"), grid_id, month, slot_type, children_count,
                 urgent_count, source_batch),
            )

    def known_streets(self) -> set[str]:
        rows = self._conn().execute(
            "SELECT street FROM grids UNION SELECT street FROM sites").fetchall()
        return {r["street"] for r in rows}

    def demand_for_month(self, month: str) -> list[sqlite3.Row]:
        return self._rows(
            self._conn(),
            "SELECT g.street, d.slot_type, SUM(d.children_count) AS demand, "
            "SUM(d.urgent_count) AS urgent "
            "FROM demand d JOIN grids g ON g.id=d.grid_id "
            "WHERE d.month=? GROUP BY g.street, d.slot_type",
            (month,),
        )

    # ---------- 场地与分期 ----------
    def create_site(self, name: str, street: str, address: str | None = None,
                    note: str | None = None) -> str:
        require(name and street, "场地名称与所属街道必填")
        site_id = _uid("site")
        with self.tx() as c:
            c.execute("INSERT INTO sites(id,name,street,address,note) VALUES(?,?,?,?,?)",
                      (site_id, name, street, address, note))
            c.execute("INSERT INTO site_service_streets(site_id,street) VALUES(?,?)",
                      (site_id, street))
        return site_id

    def get_site(self, site_id: str) -> sqlite3.Row:
        row = self._one(self._conn(), "SELECT * FROM sites WHERE id=?", (site_id,))
        if row is None:
            raise NotFoundError(f"场地不存在：{site_id}")
        return row

    def add_service_street(self, site_id: str, street: str) -> None:
        with self.tx() as c:
            if self._one(c, "SELECT 1 FROM sites WHERE id=?", (site_id,)) is None:
                raise NotFoundError(f"场地不存在：{site_id}")
            c.execute("INSERT OR IGNORE INTO site_service_streets(site_id,street) VALUES(?,?)",
                      (site_id, street))

    def service_streets(self, site_id: str) -> list[str]:
        return [r["street"] for r in self._rows(
            self._conn(),
            "SELECT street FROM site_service_streets WHERE site_id=? ORDER BY street",
            (site_id,))]

    def add_phase(self, site_id: str, seq: int, name: str,
                  compliance_status: str, compliance_capacity: int,
                  compliance_ref: str | None = None,
                  open_month: str | None = None,
                  close_month: str | None = None) -> str:
        require(compliance_status in ("合规", "整改中", "不合规", "待核验"),
                "合规结论必须为 合规/整改中/不合规/待核验")
        require(isinstance(compliance_capacity, int) and compliance_capacity >= 0,
                "合规容量应为非负整数")
        if open_month:
            check_month(open_month)
        if close_month:
            check_month(close_month)
            require(open_month is not None and close_month >= open_month,
                    "关闭月份不得早于启用月份")
        phase_id = _uid("phase")
        with self.tx() as c:
            if self._one(c, "SELECT 1 FROM sites WHERE id=?", (site_id,)) is None:
                raise NotFoundError(f"场地不存在：{site_id}")
            try:
                c.execute(
                    "INSERT INTO site_phases(id,site_id,seq,name,compliance_status,"
                    "compliance_ref,open_month,close_month,compliance_capacity)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (phase_id, site_id, seq, name, compliance_status, compliance_ref,
                     open_month, close_month, compliance_capacity),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"场地分期序号冲突：{site_id}#{seq}") from exc
        return phase_id

    def update_phase_compliance(self, phase_id: str, compliance_status: str,
                                compliance_capacity: int,
                                compliance_ref: str | None = None) -> None:
        require(compliance_status in ("合规", "整改中", "不合规", "待核验"),
                "合规结论必须为 合规/整改中/不合规/待核验")
        require(isinstance(compliance_capacity, int) and compliance_capacity >= 0,
                "合规容量应为非负整数")
        with self.tx() as c:
            row = self._one(c, "SELECT id FROM site_phases WHERE id=?", (phase_id,))
            if row is None:
                raise NotFoundError(f"分期不存在：{phase_id}")
            c.execute(
                "UPDATE site_phases SET compliance_status=?, compliance_capacity=?,"
                "compliance_ref=COALESCE(?,compliance_ref) WHERE id=?",
                (compliance_status, compliance_capacity, compliance_ref, phase_id),
            )

    def set_phase_schedule(self, phase_id: str, open_month: str | None,
                           close_month: str | None) -> None:
        if open_month:
            check_month(open_month)
        if close_month:
            check_month(close_month)
            require(open_month is not None and close_month >= open_month,
                    "关闭月份不得早于启用月份")
        with self.tx() as c:
            if self._one(c, "SELECT 1 FROM site_phases WHERE id=?", (phase_id,)) is None:
                raise NotFoundError(f"分期不存在：{phase_id}")
            c.execute("UPDATE site_phases SET open_month=?, close_month=? WHERE id=?",
                      (open_month, close_month, phase_id))

    def phases(self, site_id: str) -> list[sqlite3.Row]:
        return self._rows(self._conn(),
                          "SELECT * FROM site_phases WHERE site_id=? ORDER BY seq",
                          (site_id,))

    # ---------- 机构 ----------
    def create_provider(self, name: str, license_no: str, site_id: str,
                        registered_date: str, state: str = "规划",
                        tags: list[str] | None = None) -> str:
        from .domain import check_date
        check_date(registered_date)
        require(state in PROVIDER_STATES, f"未知机构状态：{state}")
        provider_id = _uid("prov")
        with self.tx() as c:
            if self._one(c, "SELECT 1 FROM sites WHERE id=?", (site_id,)) is None:
                raise NotFoundError(f"场地不存在：{site_id}")
            try:
                c.execute(
                    "INSERT INTO providers(id,name,license_no,site_id,registered_date,"
                    "state,tags) VALUES(?,?,?,?,?,?,?)",
                    (provider_id, name, license_no, site_id, registered_date, state,
                     json.dumps(tags or [], ensure_ascii=False)),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"备案号重复：{license_no}") from exc
            c.execute(
                "INSERT INTO provider_events(id,provider_id,date,month,from_state,"
                "to_state,reason,actor) VALUES(?,?,?,?,?,?,?,?)",
                (_uid("evt"), provider_id, registered_date, registered_date[:7],
                 None, state, "备案建立", "system"),
            )
        return provider_id

    def get_provider(self, provider_id: str) -> sqlite3.Row:
        row = self._one(self._conn(), "SELECT * FROM providers WHERE id=?", (provider_id,))
        if row is None:
            raise NotFoundError(f"机构不存在：{provider_id}")
        return row

    def transition_provider(self, provider_id: str, to_state: str, date: str,
                            reason: str, actor: str) -> None:
        from .domain import check_date
        check_date(date)
        require(to_state in PROVIDER_STATES, f"未知机构状态：{to_state}")
        with self.tx() as c:
            row = self._one(c, "SELECT state FROM providers WHERE id=?", (provider_id,))
            if row is None:
                raise NotFoundError(f"机构不存在：{provider_id}")
            current = row["state"]
            require(to_state == current or to_state in VALID_TRANSITIONS[current],
                    f"非法状态迁移：{current} -> {to_state}")
            if to_state == current:
                return
            c.execute("UPDATE providers SET state=? WHERE id=?", (to_state, provider_id))
            c.execute(
                "INSERT INTO provider_events(id,provider_id,date,month,from_state,"
                "to_state,reason,actor) VALUES(?,?,?,?,?,?,?,?)",
                (_uid("evt"), provider_id, date, date[:7], current, to_state,
                 reason, actor),
            )

    def provider_state_in_month(self, c, provider_id: str, month: str) -> str | None:
        """取该月最后一天之前最近一次事件后的状态；无记录返回 None。"""
        row = c.execute(
            "SELECT to_state FROM provider_events WHERE provider_id=? AND month<=? "
            "ORDER BY date DESC, id DESC LIMIT 1",
            (provider_id, month),
        ).fetchone()
        return row["to_state"] if row else None

    def provider_first_month_in_state(self, c, provider_id: str, state: str) -> str | None:
        row = c.execute(
            "SELECT MIN(month) AS m FROM provider_events WHERE provider_id=? AND to_state=?",
            (provider_id, state),
        ).fetchone()
        return row["m"]

    def providers(self) -> list[sqlite3.Row]:
        return self._rows(self._conn(), "SELECT * FROM providers ORDER BY id")

    def provider_events(self, provider_id: str) -> list[sqlite3.Row]:
        return self._rows(
            self._conn(),
            "SELECT * FROM provider_events WHERE provider_id=? ORDER BY date, id",
            (provider_id,))

    # ---------- 班型与容量 ----------
    def create_classroom(self, provider_id: str, phase_id: str, name: str,
                         slot_type: str, compliant_capacity: int,
                         opened_month: str, closed_month: str | None = None) -> str:
        check_month(opened_month)
        if closed_month:
            check_month(closed_month)
            require(closed_month >= opened_month, "关闭月份不得早于启用月份")
        require(slot_type in SLOT_TYPES, f"未知托位类型：{slot_type}")
        require(isinstance(compliant_capacity, int) and compliant_capacity >= 0,
                "合规容量应为非负整数")
        classroom_id = _uid("room")
        with self.tx() as c:
            p = self._one(c, "SELECT site_id FROM providers WHERE id=?", (provider_id,))
            if p is None:
                raise NotFoundError(f"机构不存在：{provider_id}")
            ph = self._one(c, "SELECT * FROM site_phases WHERE id=?", (phase_id,))
            if ph is None:
                raise NotFoundError(f"场地分期不存在：{phase_id}")
            require(ph["site_id"] == p["site_id"], "班型必须落在机构备案场地上")
            total = c.execute(
                "SELECT COALESCE(SUM(compliant_capacity),0) AS s FROM classrooms "
                "WHERE phase_id=? AND status='active'", (phase_id,)).fetchone()["s"]
            require(total + compliant_capacity <= ph["compliance_capacity"],
                    f"同分期班型容量合计 {total + compliant_capacity} "
                    f"超过合规上限 {ph['compliance_capacity']}")
            c.execute(
                "INSERT INTO classrooms(id,provider_id,phase_id,name,slot_type,"
                "compliant_capacity,opened_month,closed_month) VALUES(?,?,?,?,?,?,?,?)",
                (classroom_id, provider_id, phase_id, name, slot_type,
                 compliant_capacity, opened_month, closed_month),
            )
        return classroom_id

    def get_classroom(self, classroom_id: str) -> sqlite3.Row:
        row = self._one(self._conn(), "SELECT * FROM classrooms WHERE id=?", (classroom_id,))
        if row is None:
            raise NotFoundError(f"班型不存在：{classroom_id}")
        return row

    def active_allocated(self, c, classroom_id: str, month: str | None = None) -> int:
        """已分配（在配）托位数；容量下调时对所有未关闭月份取最大值。"""
        if month:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM allocations WHERE classroom_id=? AND month=? "
                "AND status='allocated'", (classroom_id, month)).fetchone()
            return row["n"]
        row = c.execute(
            "SELECT COUNT(*) AS n FROM allocations a JOIN classrooms cr ON cr.id=a.classroom_id "
            "WHERE a.classroom_id=? AND a.status='allocated' "
            "AND (cr.closed_month IS NULL OR a.month <= cr.closed_month)",
            (classroom_id,)).fetchone()
        return row["n"]

    def allocated_by_month(self, c, classroom_id: str) -> list[sqlite3.Row]:
        return list(c.execute(
            "SELECT month, COUNT(*) AS n FROM allocations "
            "WHERE classroom_id=? AND status='allocated' GROUP BY month",
            (classroom_id,)))

    def set_capacity(self, classroom_id: str, new_capacity: int,
                     expected_version: int, actor: str) -> int:
        """并发安全下调/上调容量。

        - 乐观锁：expected_version 不匹配 -> ConflictError；
        - 硬约束：新容量不得小于任何未关闭月份的已分配数，亦不得超过分期合规上限。
        返回新版本号。
        """
        require(isinstance(new_capacity, int) and new_capacity >= 0,
                "合规容量应为非负整数")
        require(isinstance(expected_version, int) and expected_version >= 1,
                "expected_version 应为正整数")
        with self.tx() as c:
            row = self._one(c, "SELECT * FROM classrooms WHERE id=?", (classroom_id,))
            if row is None:
                raise NotFoundError(f"班型不存在：{classroom_id}")
            if row["version"] != expected_version:
                raise ConflictError(
                    f"容量已被他人更新（服务器版本 v{row['version']}，客户端基于 v{expected_version}）")
            phase = self._one(c, "SELECT * FROM site_phases WHERE id=?",
                              (row["phase_id"],))
            # 上调：同分期班型合计仍受分期合规上限约束
            total = c.execute(
                "SELECT COALESCE(SUM(compliant_capacity),0) AS s FROM classrooms "
                "WHERE phase_id=? AND status='active' AND id<>?",
                (row["phase_id"], classroom_id)).fetchone()["s"]
            require(total + new_capacity <= phase["compliance_capacity"],
                    f"调整后同分期班型容量合计 {total + new_capacity} "
                    f"超过合规上限 {phase['compliance_capacity']}")
            # 下调：不得低于任何月份的在托人数（常规托位自配位月起持续占座，
            # 临时体验仅当月占座），逐月取峰值
            peak = self._occupancy_peak(c, classroom_id)
            if new_capacity < peak:
                raise ConflictError(
                    f"容量下调被拒：新容量 {new_capacity} < 历史在托峰值 {peak}")
            c.execute(
                "UPDATE classrooms SET compliant_capacity=?, version=version+1 WHERE id=?",
                (new_capacity, classroom_id))
        return expected_version + 1

    def _occupancy_peak(self, c, classroom_id: str) -> int:
        """逐月在托人数峰值：常规托位自配位月起持续占座，临时体验仅当月。"""
        rows = c.execute(
            "SELECT a.waitlist_id AS wid, a.month AS m, w.slot_type AS st "
            "FROM allocations a JOIN waitlist w ON w.id=a.waitlist_id "
            "WHERE a.classroom_id=? AND a.status='allocated'",
            (classroom_id,)).fetchall()
        if not rows:
            return 0
        months = sorted({r["m"] for r in rows})
        regular = [(r["m"], r["wid"]) for r in rows if r["st"] != "临时体验"]
        peak = 0
        for m in months:
            seated = {wid for am, wid in regular if am <= m}
            seated |= {r["wid"] for r in rows
                       if r["st"] == "临时体验" and r["m"] == m}
            peak = max(peak, len(seated))
        return peak

    def stop_classroom(self, classroom_id: str, close_month: str) -> None:
        check_month(close_month)
        with self.tx() as c:
            if self._one(c, "SELECT 1 FROM classrooms WHERE id=?", (classroom_id,)) is None:
                raise NotFoundError(f"班型不存在：{classroom_id}")
            active = c.execute(
                "SELECT COUNT(*) AS n FROM allocations WHERE classroom_id=? AND status='allocated' "
                "AND month>?", (classroom_id, close_month)).fetchone()["n"]
            require(active == 0, f"关闭月份之后仍有 {active} 个在配托位，须先释放")
            c.execute("UPDATE classrooms SET status='stopped', closed_month=? WHERE id=?",
                      (close_month, classroom_id))

    # ---------- 候补与派位 ----------
    def upsert_waitlist(self, family_token: str, street: str, slot_type: str,
                        target_month: str, urgent: bool, priority_score: int,
                        current_month: str) -> str:
        """跨街道重复申报按 HMAC 匿名键全局去重。"""
        check_month(target_month)
        require(slot_type in SLOT_TYPES, f"未知托位类型：{slot_type}")
        anon = self.family_anon_key(family_token)
        with self.tx() as c:
            row = self._one(c, "SELECT id, status FROM waitlist WHERE anon_key=?", (anon,))
            if row is None:
                wid = _uid("fam")
                c.execute(
                    "INSERT INTO waitlist(id,anon_key,street,slot_type,target_month,"
                    "urgent,priority_score,status,first_seen_month) "
                    "VALUES(?,?,?,?,?,?,?,'waiting',?)",
                    (wid, anon, street, slot_type, target_month,
                     1 if urgent else 0, priority_score, current_month),
                )
                return wid
            # 已建档：覆盖最新意愿，重新进入候补（ fulfilled 也允许换月份重新排队）
            c.execute(
                "UPDATE waitlist SET street=?, slot_type=?, target_month=?, urgent=?,"
                "priority_score=?, status='waiting', updated_at=datetime('now') WHERE id=?",
                (street, slot_type, target_month, 1 if urgent else 0,
                 priority_score, row["id"]),
            )
            return row["id"]

    def occupancy_in_month(self, c, classroom_id: str, month: str) -> int:
        """班型当月在托人数：常规托位累计历史配位，临时体验仅当月。"""
        rows = c.execute(
            "SELECT a.waitlist_id AS wid, a.month AS m, w.slot_type AS st "
            "FROM allocations a JOIN waitlist w ON w.id=a.waitlist_id "
            "WHERE a.classroom_id=? AND a.status='allocated'",
            (classroom_id,)).fetchall()
        seated = {r["wid"] for r in rows
                  if r["st"] != "临时体验" and r["m"] <= month}
        seated |= {r["wid"] for r in rows
                   if r["st"] == "临时体验" and r["m"] == month}
        return len(seated)

    def allocate(self, waitlist_id: str, classroom_id: str, month: str,
                 run_id: str) -> str:
        with self.tx() as c:
            w = self._one(c, "SELECT * FROM waitlist WHERE id=?", (waitlist_id,))
            if w is None:
                raise NotFoundError("候补记录不存在")
            if w["status"] != "waiting":
                raise ConflictError("该候补家庭已配位或已失效")
            room = self._one(c, "SELECT * FROM classrooms WHERE id=?", (classroom_id,))
            if room is None:
                raise NotFoundError("班型不存在")
            if room["slot_type"] != w["slot_type"]:
                raise ConflictError("托位类型与候补意愿不符")
            site = self._one(c, "SELECT site_id FROM providers WHERE id=?",
                             (room["provider_id"],))
            served = {r["street"] for r in self._rows(
                c, "SELECT street FROM site_service_streets WHERE site_id=?",
                (site["site_id"],))}
            if w["street"] not in served:
                raise ConflictError("该场地不服务候补家庭所在街道（跨街道未备案）")
            used = self.occupancy_in_month(c, classroom_id, month)
            if used >= room["compliant_capacity"]:
                raise ConflictError(
                    f"班型 {classroom_id} 在 {month} 已达合规上限 "
                    f"{room['compliant_capacity']}（在托 {used}）")
            alloc_id = _uid("alloc")
            c.execute(
                "INSERT INTO allocations(id,waitlist_id,classroom_id,month,status,run_id)"
                " VALUES(?,?,?,?,'allocated',?)",
                (alloc_id, waitlist_id, classroom_id, month, run_id))
            c.execute("UPDATE waitlist SET status='fulfilled' WHERE id=?", (waitlist_id,))
            return alloc_id

    def has_frozen_supply_after(self, month: str) -> bool:
        """是否存在更晚月份的冻结供需运行（用于封闭历史、保护在托连续性）。"""
        row = self._one(
            self._conn(),
            "SELECT 1 AS x FROM calc_runs WHERE kind='supply' AND status='frozen' "
            "AND policy_id IS NULL AND month>? LIMIT 1", (month,))
        return row is not None

    def rewind_supply_month(self, month: str) -> None:
        """重算月份：旧 supply 运行已置 superseded，释放其派位并让家庭重新排队。

        旧运行的月度快照行原样保留，保证历史结论可追溯。
        """
        with self.tx() as c:
            released = c.execute(
                "SELECT DISTINCT waitlist_id FROM allocations "
                "WHERE month=? AND status='allocated' AND run_id IN "
                "(SELECT id FROM calc_runs WHERE kind='supply' AND month=? "
                "AND status='superseded')",
                (month, month)).fetchall()
            c.execute(
                "UPDATE allocations SET status='released' "
                "WHERE month=? AND status='allocated' AND run_id IN "
                "(SELECT id FROM calc_runs WHERE kind='supply' AND month=? "
                "AND status='superseded')",
                (month, month))
            for r in released:
                other = c.execute(
                    "SELECT COUNT(*) AS n FROM allocations WHERE waitlist_id=? "
                    "AND status='allocated'", (r["waitlist_id"],)).fetchone()["n"]
                if other == 0:
                    c.execute("UPDATE waitlist SET status='waiting' WHERE id=?",
                              (r["waitlist_id"],))

    def waiting_families(self, month: str) -> list[sqlite3.Row]:
        """当月到期仍在候补的匿名家庭（目标月 <= 当月）。"""
        return self._rows(
            self._conn(),
            "SELECT * FROM waitlist WHERE status='waiting' AND target_month<=? "
            "ORDER BY urgent DESC, priority_score DESC, first_seen_month, rowid",
            (month,))

    def active_allocation_count(self, classroom_id: str, month: str) -> int:
        return self.active_allocated(self._conn(), classroom_id, month)

    def allocations_in_month(self, month: str) -> list[sqlite3.Row]:
        return self._rows(
            self._conn(),
            "SELECT a.*, w.street AS family_street, w.urgent AS family_urgent, "
            "w.slot_type AS slot_type "
            "FROM allocations a JOIN waitlist w ON w.id=a.waitlist_id "
            "WHERE a.month=? AND a.status='allocated'", (month,))

    def enrolled_counts(self, month: str) -> dict[str, int]:
        """各班型当月在托人数（用于运营补助计提）。

        常规托位（乳儿/托小/托大）自配位月起持续占座至班型关闭；
        “临时体验”仅在体验当月占座。按家庭去重，一个家庭只计一次。
        """
        rows = self._rows(
            self._conn(),
            """
            SELECT a.classroom_id AS cid, COUNT(DISTINCT a.waitlist_id) AS n
            FROM allocations a
            JOIN classrooms cr ON cr.id = a.classroom_id
            JOIN waitlist w ON w.id = a.waitlist_id
            WHERE a.status='allocated'
              AND cr.status='active'
              AND a.month <= ?
              AND (cr.closed_month IS NULL OR cr.closed_month >= ?)
              AND (w.slot_type='临时体验' AND a.month=?
                   OR w.slot_type<>'临时体验')
            GROUP BY a.classroom_id
            """,
            (month, month, month),
        )
        return {r["cid"]: r["n"] for r in rows}

    def coverage_by_street(self, month: str) -> dict[tuple[str, str], dict]:
        """当月在托孩子按（家庭所在街道, 托位类型）聚合（去重家庭）。

        常规托位自配位月起持续在托，临时体验仅当月；只统计当月处于“运营”
        状态机构下的班型（暂停/退出期间不形成覆盖）。
        """
        c = self._conn()
        active_providers = [
            r["provider_id"] for r in self.effective_classrooms(month)]
        if not active_providers:
            return {}
        placeholders = ",".join("?" * len(active_providers))
        base_where = (
            "a.status='allocated' AND cr.status='active' "
            f"AND cr.provider_id IN ({placeholders}) "
            "AND a.month <= ? "
            "AND (cr.closed_month IS NULL OR cr.closed_month >= ?) "
            "AND ((w.slot_type='临时体验' AND a.month=?) OR w.slot_type<>'临时体验')"
        )
        params = active_providers + [month, month, month]

        def query(extra, extra_params=()):
            return self._rows(
                c,
                "SELECT w.street AS street, w.slot_type AS slot_type, "
                "COUNT(DISTINCT a.waitlist_id) AS n "
                "FROM allocations a JOIN classrooms cr ON cr.id=a.classroom_id "
                "JOIN waitlist w ON w.id=a.waitlist_id "
                f"WHERE {base_where} {extra} GROUP BY w.street, w.slot_type",
                tuple(params) + tuple(extra_params))

        result: dict[tuple[str, str], dict] = {}
        for r in query(""):
            result[(r["street"], r["slot_type"])] = {
                "allocated": r["n"], "urgent": 0}
        for r in query("AND w.urgent=1"):
            result[(r["street"], r["slot_type"])]["urgent"] = r["n"]
        return result

    # ---------- 价格 ----------
    def commit_price(self, classroom_id: str, monthly_fee_cents: int,
                     committed_date: str) -> None:
        from .domain import check_date
        check_date(committed_date)
        require(isinstance(monthly_fee_cents, int) and monthly_fee_cents >= 0,
                "承诺月费应为非负整数（分）")
        with self.tx() as c:
            if self._one(c, "SELECT 1 FROM classrooms WHERE id=?", (classroom_id,)) is None:
                raise NotFoundError(f"班型不存在：{classroom_id}")
            c.execute(
                "INSERT INTO price_commitments(id,classroom_id,monthly_fee_cents,"
                "committed_date) VALUES(?,?,?,?) "
                "ON CONFLICT(classroom_id) DO UPDATE SET "
                "monthly_fee_cents=excluded.monthly_fee_cents,"
                "committed_date=excluded.committed_date",
                (_uid("price"), classroom_id, monthly_fee_cents, committed_date),
            )

    def report_price(self, classroom_id: str, month: str, actual_fee_cents: int,
                     reporter: str) -> None:
        check_month(month)
        require(isinstance(actual_fee_cents, int) and actual_fee_cents >= 0,
                "实际月费应为非负整数（分）")
        with self.tx() as c:
            if self._one(c, "SELECT 1 FROM classrooms WHERE id=?", (classroom_id,)) is None:
                raise NotFoundError(f"班型不存在：{classroom_id}")
            c.execute(
                "INSERT INTO price_reports(id,classroom_id,month,actual_fee_cents,reporter)"
                " VALUES(?,?,?,?,?) ON CONFLICT(classroom_id,month) DO UPDATE SET "
                "actual_fee_cents=excluded.actual_fee_cents, reporter=excluded.reporter",
                (_uid("pr"), classroom_id, month, actual_fee_cents, reporter),
            )

    # ---------- 政策 ----------
    def create_policy(self, version_no: str, effective_month: str,
                      rules: list[dict], retroactive: bool = False,
                      supersedes_policy_id: str | None = None,
                      note: str | None = None) -> str:
        check_month(effective_month)
        require(rules, "政策至少包含一条规则")
        for r in rules:
            require(r["action"] in CAPACITY_ACTIONS, f"未知政策动作：{r['action']}")
            require(r["basis"] in ("capacity_one_time", "enrolled_monthly",
                                   "site_monthly", "award_one_time",
                                   "provider_monthly"),
                    f"未知计提基础：{r['basis']}")
            st = r.get("slot_type")
            require(st is None or st in SLOT_TYPES, f"规则托位类型非法：{st}")
            require(isinstance(r["amount_cents"], int) and r["amount_cents"] >= 0,
                    "补助金额应为非负整数（分）")
        policy_id = _uid("pol")
        with self.tx() as c:
            if supersedes_policy_id and self._one(
                    c, "SELECT 1 FROM policy_versions WHERE id=?",
                    (supersedes_policy_id,)) is None:
                raise NotFoundError("被替代的政策版本不存在")
            try:
                c.execute(
                    "INSERT INTO policy_versions(id,version_no,effective_month,status,"
                    "retroactive,supersedes_policy_id,note) VALUES(?,?,?,'active',?,?,?)",
                    (policy_id, version_no, effective_month,
                     1 if retroactive else 0, supersedes_policy_id, note),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"政策版本号重复：{version_no}") from exc
            for r in rules:
                c.execute(
                    "INSERT INTO policy_rules(id,policy_id,action,slot_type,basis,"
                    "amount_cents,min_commitment_months,price_tolerance_cents,tag)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (_uid("rule"), policy_id, r["action"], r.get("slot_type"),
                     r["basis"], r["amount_cents"],
                     r.get("min_commitment_months", 0),
                     r.get("price_tolerance_cents", 0), r.get("tag")),
                )
        return policy_id

    def get_policy(self, policy_id: str) -> sqlite3.Row:
        row = self._one(self._conn(),
                        "SELECT * FROM policy_versions WHERE id=?", (policy_id,))
        if row is None:
            raise NotFoundError(f"政策版本不存在：{policy_id}")
        return row

    def policy_by_version_no(self, version_no: str) -> sqlite3.Row:
        row = self._one(self._conn(),
                        "SELECT * FROM policy_versions WHERE version_no=?", (version_no,))
        if row is None:
            raise NotFoundError(f"政策版本不存在：{version_no}")
        return row

    def policy_rules(self, policy_id: str) -> list[sqlite3.Row]:
        return self._rows(
            self._conn(),
            "SELECT * FROM policy_rules WHERE policy_id=?", (policy_id,))

    def policy_for_month(self, month: str) -> sqlite3.Row | None:
        """当月适用政策：生效月不晚于该月的所有版本中，最新发布的一版。

        - 非追溯政策的 effective_month 即生效起点，更早的月份不会选中它；
        - 追溯政策发布后，其 effective_month 可早于发布月，从而覆盖历史月份；
        - 版本新旧以发布顺序（rowid）为准，而非生效月——后发布的追溯下调
          政策即使生效月更早，也应优先适用。
        """
        return self._one(
            self._conn(),
            "SELECT * FROM policy_versions WHERE status='active' AND effective_month<=? "
            "ORDER BY rowid DESC LIMIT 1",
            (month,))

    def policies(self) -> list[sqlite3.Row]:
        return self._rows(self._conn(),
                          "SELECT * FROM policy_versions ORDER BY effective_month DESC")

    # ---------- 申报 / 补助 / 追回 ----------
    @staticmethod
    def claim_dedupe_key(provider_id: str, action: str,
                         classroom_id: str | None, month: str) -> str:
        return "|".join([provider_id, action, classroom_id or "-", month])

    def record_claim(self, provider_id: str, action: str, month: str,
                     policy_id: str, basis_qty: int, amount_cents: int,
                     run_id: str, classroom_id: str | None = None,
                     site_id: str | None = None,
                     original_claim_id: str | None = None,
                     rule_id: str | None = None,
                     status: str = "approved", note: str | None = None) -> str:
        check_month(month)
        require(action in CAPACITY_ACTIONS, f"未知政策动作：{action}")
        key = self.claim_dedupe_key(provider_id, action, classroom_id, month)
        with self.tx() as c:
            if self._one(c, "SELECT 1 FROM providers WHERE id=?", (provider_id,)) is None:
                raise NotFoundError(f"机构不存在：{provider_id}")
            if original_claim_id is None:
                existing = self._one(
                    c,
                    "SELECT id,status,amount_cents,policy_id FROM claims "
                    "WHERE dedupe_key=? AND original_claim_id IS NULL AND status<>'void'",
                    (key,))
                if existing is not None:
                    if (existing["policy_id"] == policy_id
                            and existing["amount_cents"] == amount_cents
                            and existing["status"] == status):
                        c.execute("INSERT OR IGNORE INTO run_funding_entries"
                                  "(run_id,claim_id) VALUES(?,?)",
                                  (run_id, existing["id"]))
                        return existing["id"]  # 幂等：同一测算重放
                    raise ConflictError(
                        f"重复申报被拦截：{action} {month} 已存在申报 "
                        f"{existing['id']}（{existing['status']}）")
            else:
                if self._one(c, "SELECT 1 FROM claims WHERE id=?",
                             (original_claim_id,)) is None:
                    raise NotFoundError("原申报不存在，无法挂接补差链")
            claim_id = _uid("claim")
            c.execute(
                "INSERT INTO claims(id,provider_id,classroom_id,site_id,policy_id,"
                "run_id,action,month,basis_qty,amount_cents,rule_id,status,dedupe_key,"
                "original_claim_id,note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (claim_id, provider_id, classroom_id, site_id, policy_id, run_id,
                 action, month, basis_qty, amount_cents, rule_id, status, key,
                 original_claim_id, note),
            )
            c.execute("INSERT OR IGNORE INTO run_funding_entries(run_id,claim_id) VALUES(?,?)",
                      (run_id, claim_id))
            return claim_id

    def add_clawback(self, claim_id: str, run_id: str, reason: str,
                     amount_cents: int, recovered: bool = False) -> str:
        require(reason in ("价格违约", "停业", "提前退出", "政策标准下调", "重复申报"),
                f"未知追回原因：{reason}")
        require(isinstance(amount_cents, int) and amount_cents > 0,
                "追回金额应为正整数（分）")
        claw_id = _uid("claw")
        with self.tx() as c:
            row = self._one(c, "SELECT status, amount_cents FROM claims WHERE id=?",
                            (claim_id,))
            if row is None:
                raise NotFoundError(f"申报不存在：{claim_id}")
            already = c.execute(
                "SELECT COALESCE(SUM(amount_cents),0) AS s FROM clawbacks WHERE claim_id=?",
                (claim_id,)).fetchone()["s"]
            require(already + amount_cents <= row["amount_cents"],
                    f"累计追回 {already + amount_cents} 超过补助金额 {row['amount_cents']}")
            c.execute(
                "INSERT INTO clawbacks(id,claim_id,run_id,reason,amount_cents,recovered)"
                " VALUES(?,?,?,?,?,?)",
                (claw_id, claim_id, run_id, reason, amount_cents,
                 1 if recovered else 0))
            c.execute("UPDATE claims SET status='clawed_back' WHERE id=?", (claim_id,))
        return claw_id

    def claim(self, claim_id: str) -> sqlite3.Row:
        row = self._one(self._conn(), "SELECT * FROM claims WHERE id=?", (claim_id,))
        if row is None:
            raise NotFoundError(f"申报不存在：{claim_id}")
        return row

    def find_active_claim_by_key(self, dedupe_key: str) -> sqlite3.Row | None:
        return self._one(
            self._conn(),
            "SELECT * FROM claims WHERE dedupe_key=? AND status<>'void' "
            "AND original_claim_id IS NULL ORDER BY created_at DESC LIMIT 1",
            (dedupe_key,))

    def active_supplements(self, root_claim_id: str) -> list[sqlite3.Row]:
        return self._rows(
            self._conn(),
            "SELECT * FROM claims WHERE original_claim_id=? AND status='supplement' "
            "ORDER BY created_at", (root_claim_id,))

    def void_claim(self, claim_id: str, note: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE claims SET status='void', note=? WHERE id=?",
                      (note, claim_id))

    def clawback_total(self, claim_id: str) -> int:
        return self._one(
            self._conn(),
            "SELECT COALESCE(SUM(amount_cents),0) AS s FROM clawbacks WHERE claim_id=?",
            (claim_id,))["s"]

    def declare_claim(self, provider_id: str, action: str, month: str,
                      classroom_id: str | None, declared_amount_cents: int,
                      actor: str) -> str:
        """机构自行申报；同口径重复申报直接冲突。"""
        check_month(month)
        require(action in CAPACITY_ACTIONS, f"未知政策动作：{action}")
        require(isinstance(declared_amount_cents, int) and declared_amount_cents >= 0,
                "申报金额应为非负整数（分）")
        key = self.claim_dedupe_key(provider_id, action, classroom_id, month)
        with self.tx() as c:
            if self._one(c, "SELECT 1 FROM providers WHERE id=?", (provider_id,)) is None:
                raise NotFoundError(f"机构不存在：{provider_id}")
            existing = self.find_active_claim_by_key(key)
            if existing is not None:
                raise ConflictError(
                    f"重复申报被拦截：{action} {month} 已存在 {existing['status']} 申报"
                    f"（{existing['id']}），不得就同一班型/月份再次申报")
            claim_id = _uid("claim")
            site_row = self._one(c, "SELECT site_id FROM providers WHERE id=?",
                                 (provider_id,))
            c.execute(
                "INSERT INTO claims(id,provider_id,classroom_id,site_id,policy_id,"
                "run_id,action,month,basis_qty,amount_cents,status,dedupe_key,note)"
                " VALUES(?,?,?,?,NULL,NULL,?,?,?,?,'submitted',?,?)",
                (claim_id, provider_id, classroom_id, site_row["site_id"],
                 action, month, 0, declared_amount_cents, key,
                 f"机构申报（{actor}）"),
            )
            return claim_id

    def settle_declaration(self, claim_id: str, policy_id: str, run_id: str,
                           basis_qty: int, approved_amount_cents: int,
                           voided: bool, note: str) -> None:
        with self.tx() as c:
            if voided:
                c.execute("UPDATE claims SET status='void', note=? WHERE id=?",
                          (note, claim_id))
            else:
                c.execute(
                    "UPDATE claims SET status='approved', policy_id=?, run_id=?,"
                    "basis_qty=?, amount_cents=?, note=? WHERE id=?",
                    (policy_id, run_id, basis_qty, approved_amount_cents, note,
                     claim_id),
                )
                c.execute("INSERT OR IGNORE INTO run_funding_entries(run_id,claim_id)"
                          " VALUES(?,?)", (run_id, claim_id))

    def link_claim_to_run(self, claim_id: str, run_id: str) -> None:
        with self.tx() as c:
            c.execute("INSERT OR IGNORE INTO run_funding_entries(run_id,claim_id)"
                      " VALUES(?,?)", (run_id, claim_id))

    def one_time_claims(self, provider_id: str, action: str) -> list[sqlite3.Row]:
        return self._rows(
            self._conn(),
            "SELECT * FROM claims WHERE provider_id=? AND action=? AND status<>'void' "
            "AND original_claim_id IS NULL ORDER BY month", (provider_id, action))

    def set_claim_note(self, claim_id: str, note: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE claims SET note=? WHERE id=?", (note, claim_id))

    def claims_for_run(self, run_id: str) -> list[sqlite3.Row]:
        return self._rows(
            self._conn(),
            "SELECT cl.* FROM claims cl JOIN run_funding_entries e ON e.claim_id=cl.id "
            "WHERE e.run_id=? ORDER BY cl.month, cl.action", (run_id,))

    def clawbacks_for_claim(self, claim_id: str) -> list[sqlite3.Row]:
        return self._rows(self._conn(),
                          "SELECT * FROM clawbacks WHERE claim_id=? ORDER BY id",
                          (claim_id,))

    def _discard_run_adjustments(self, c, run_id: str) -> None:
        """同口径重算时作废旧运行产生的补差行、冲销其追回，避免重复累计。"""
        # 该运行挂接的机构原始申报不删除（回到 submitted 之外的状态保持 approved，
        # 由新一轮 reconcile 重新比对）；仅作废系统补差行。
        c.execute(
            "UPDATE claims SET status='void' "
            "WHERE run_id=? AND original_claim_id IS NOT NULL AND status='supplement'",
            (run_id,))
        # 冲销该运行登记的追回，并恢复父申报状态
        claws = c.execute(
            "SELECT DISTINCT claim_id FROM clawbacks WHERE run_id=?", (run_id,)
        ).fetchall()
        c.execute("DELETE FROM clawbacks WHERE run_id=?", (run_id,))
        for row in claws:
            remaining = c.execute(
                "SELECT COUNT(*) AS n FROM clawbacks WHERE claim_id=?",
                (row["claim_id"],)).fetchone()["n"]
            if remaining == 0:
                c.execute(
                    "UPDATE claims SET status='approved' WHERE id=? AND status='clawed_back'",
                    (row["claim_id"],))

    def net_paid_cents(self, claim_id: str) -> int:
        """原申报（或申报链根）+ 有效补差 - 追回 的净支付额。"""
        with self.tx() as c:
            root = c.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
            if root is None:
                raise NotFoundError(f"申报不存在：{claim_id}")
            if root["original_claim_id"]:
                root = c.execute("SELECT * FROM claims WHERE id=?",
                                 (root["original_claim_id"],)).fetchone()
            paid = root["amount_cents"] if root["status"] != "void" else 0
            supplements = c.execute(
                "SELECT COALESCE(SUM(amount_cents),0) AS s FROM claims "
                "WHERE original_claim_id=? AND status='supplement'",
                (root["id"],)).fetchone()["s"]
            clawed = c.execute(
                "SELECT COALESCE(SUM(amount_cents),0) AS s FROM clawbacks WHERE claim_id=?",
                (root["id"],)).fetchone()["s"]
            return paid + supplements - clawed

    # ---------- 测算运行与快照 ----------
    def create_run(self, kind: str, month: str, created_by: str,
                   policy_id: str | None = None,
                   supersedes_run_id: str | None = None,
                   note: str | None = None) -> str:
        require(kind in ("supply", "funding"), "运行类型必须为 supply/funding")
        check_month(month)
        run_id = _uid("run")
        with self.tx() as c:
            if policy_id and self._one(c, "SELECT 1 FROM policy_versions WHERE id=?",
                                       (policy_id,)) is None:
                raise NotFoundError(f"政策版本不存在：{policy_id}")
            if supersedes_run_id:
                old = self._one(c, "SELECT id FROM calc_runs WHERE id=?",
                                (supersedes_run_id,))
                if old is None:
                    raise NotFoundError("被替代的运行不存在")
            # 同口径重算：旧运行置 superseded（快照行仍保留可读）。
            # 资金侧不同政策版本是不同口径：追溯新政重算不冻结旧政策运行。
            if policy_id:
                olds = self._rows(
                    c,
                    "SELECT id FROM calc_runs WHERE kind=? AND month=? AND status='frozen' "
                    "AND policy_id=?", (kind, month, policy_id))
            else:
                olds = self._rows(
                    c,
                    "SELECT id FROM calc_runs WHERE kind=? AND month=? AND status='frozen' "
                    "AND policy_id IS NULL", (kind, month))
            for old in olds:
                self._discard_run_adjustments(c, old["id"])
                c.execute("UPDATE calc_runs SET status='superseded' WHERE id=?",
                          (old["id"],))
            try:
                c.execute(
                    "INSERT INTO calc_runs(id,kind,month,policy_id,status,"
                    "supersedes_run_id,note,created_by) VALUES(?,?,?,?,'frozen',?,?,?)",
                    (run_id, kind, month, policy_id, supersedes_run_id, note, created_by),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"{kind} 运行已存在（月份+政策）") from exc
        return run_id

    def get_run(self, run_id: str) -> sqlite3.Row:
        row = self._one(self._conn(), "SELECT * FROM calc_runs WHERE id=?", (run_id,))
        if row is None:
            raise NotFoundError(f"测算运行不存在：{run_id}")
        return row

    def latest_run(self, kind: str, month: str,
                   policy_id: str | None = None) -> sqlite3.Row | None:
        # 以 rowid 为准取最新运行（created_at 秒级精度不足以区分同秒重算）
        if policy_id is not None:
            return self._one(
                self._conn(),
                "SELECT * FROM calc_runs WHERE kind=? AND month=? AND policy_id=? "
                "ORDER BY rowid DESC LIMIT 1", (kind, month, policy_id))
        return self._one(
            self._conn(),
            "SELECT * FROM calc_runs WHERE kind=? AND month=? AND policy_id IS NULL "
            "ORDER BY rowid DESC LIMIT 1", (kind, month))

    def save_supply_snapshot(self, run_id: str, rows: list[dict]) -> None:
        with self.tx() as c:
            for r in rows:
                c.execute(
                    "INSERT INTO monthly_supply(id,run_id,street,slot_type,demand,"
                    "urgent_demand,accessible_capacity,allocated,newly_allocated,"
                    "urgent_allocated,waitlist_count,gap,urgent_gap,shared_capacity) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (_uid("snap"), run_id, r["street"], r["slot_type"], r["demand"],
                     r["urgent_demand"], r["accessible_capacity"], r["allocated"],
                     r.get("newly_allocated", 0),
                     r["urgent_allocated"], r["waitlist_count"], r["gap"],
                     r["urgent_gap"], r["shared_capacity"]),
                )

    def supply_snapshot(self, run_id: str) -> list[sqlite3.Row]:
        return self._rows(self._conn(),
                          "SELECT * FROM monthly_supply WHERE run_id=? "
                          "ORDER BY street, slot_type", (run_id,))

    def save_recommendation(self, run_id: str, street: str, slot_type: str,
                            months: list[str], gap_before: int, proposed_capacity: int,
                            relieved_rows: list[dict], rationale: str,
                            site_id: str | None = None,
                            phase_id: str | None = None) -> str:
        total_relief = sum(r["relieved"] for r in relieved_rows)
        rec_id = _uid("rec")
        with self.tx() as c:
            c.execute(
                "INSERT INTO recommendations(id,run_id,site_id,phase_id,street,"
                "slot_type,months,gap_before,proposed_capacity,relieved,gap_after,"
                "rationale) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (rec_id, run_id, site_id, phase_id, street, slot_type,
                 json.dumps(months), gap_before, proposed_capacity, total_relief,
                 max(0, gap_before - total_relief), rationale),
            )
            for r in relieved_rows:
                c.execute(
                    "INSERT INTO recommendation_relief(recommendation_id,run_id,street,"
                    "slot_type,month,gap_before,relieved) VALUES(?,?,?,?,?,?,?)",
                    (rec_id, r.get("run_id", run_id), r["street"], r["slot_type"],
                     r["month"], r["gap_before"], r["relieved"]),
                )
        return rec_id

    def recommendation(self, rec_id: str) -> dict:
        with self.tx() as c:
            r = self._one(c, "SELECT * FROM recommendations WHERE id=?", (rec_id,))
            if r is None:
                raise NotFoundError(f"建议不存在：{rec_id}")
            relief = self._rows(
                c,
                "SELECT * FROM recommendation_relief WHERE recommendation_id=? "
                "ORDER BY month, street", (rec_id,))
        d = dict(r)
        d["months"] = json.loads(d["months"])
        d["relief_detail"] = [dict(x) for x in relief]
        return d

    # ---------- 查询：有效供给（引擎用） ----------
    def effective_classrooms(self, month: str) -> list[sqlite3.Row]:
        """当月真正可用的班型：分期已合规启用、班型在运行窗口、机构当月状态为运营。"""
        rows = self._rows(
            self._conn(),
            """
            SELECT cr.id AS classroom_id, cr.slot_type, cr.compliant_capacity,
                   cr.provider_id, cr.opened_month, p.site_id,
                   s.street AS home_street
            FROM classrooms cr
            JOIN providers p ON p.id = cr.provider_id
            JOIN sites s ON s.id = p.site_id
            JOIN site_phases ph ON ph.id = cr.phase_id
            WHERE cr.status='active'
              AND cr.opened_month <= ?
              AND (cr.closed_month IS NULL OR cr.closed_month >= ?)
              AND ph.compliance_status='合规'
              AND ph.open_month IS NOT NULL AND ph.open_month <= ?
              AND (ph.close_month IS NULL OR ph.close_month >= ?)
            """,
            (month, month, month, month),
        )
        result = []
        c = self._conn()
        for r in rows:
            state = self.provider_state_in_month(c, r["provider_id"], month)
            if state == "运营":
                result.append(r)
        return result
