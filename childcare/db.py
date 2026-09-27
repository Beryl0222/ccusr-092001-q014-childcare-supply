"""SQLite 持久层: schema 定义、连接与事务助手。

并发模型: WAL 模式 + BEGIN IMMEDIATE 串行化所有写事务,
容量守卫(已分配托位不得超过合规上限)在事务内完成, 因此并发安全。
"""

import sqlite3
from contextlib import contextmanager

SCHEMA = """
-- 人口网格聚合需求(仅存聚合口径, 不含任何家庭明细)
CREATE TABLE IF NOT EXISTS grid_demand (
    grid        TEXT NOT NULL,
    street      TEXT NOT NULL,
    month       TEXT NOT NULL,
    class_type  TEXT NOT NULL,
    demand      INTEGER NOT NULL CHECK (demand >= 0),
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (grid, month, class_type)
);

-- 改造场地(党群中心/公房/办公用房等)及其合规结论
CREATE TABLE IF NOT EXISTS sites (
    site_id          TEXT PRIMARY KEY,
    street           TEXT NOT NULL,
    kind             TEXT NOT NULL,
    compliance       TEXT NOT NULL DEFAULT '待评估',
    compliance_note  TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL
);

-- 同一场地分阶段启用: 每个阶段按班型给出启用月与容量
CREATE TABLE IF NOT EXISTS site_phases (
    site_id      TEXT NOT NULL REFERENCES sites(site_id),
    phase        INTEGER NOT NULL,
    start_month  TEXT NOT NULL,
    end_month    TEXT,
    class_type   TEXT NOT NULL,
    capacity     INTEGER NOT NULL CHECK (capacity >= 0),
    PRIMARY KEY (site_id, phase, class_type)
);

-- 机构备案(一个场地对应一个托育点)
CREATE TABLE IF NOT EXISTS providers (
    provider_id  TEXT PRIMARY KEY,
    site_id      TEXT NOT NULL UNIQUE REFERENCES sites(site_id),
    name         TEXT NOT NULL,
    model        INTEGER NOT NULL DEFAULT 0,   -- 是否示范机构
    created_at   TEXT NOT NULL
);

-- 机构状态流水(含临时停办的起止月), 状态由流水推导, 不直接改写
CREATE TABLE IF NOT EXISTS provider_status_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id  TEXT NOT NULL REFERENCES providers(provider_id),
    to_status    TEXT NOT NULL,
    month        TEXT NOT NULL,   -- 生效月
    end_month    TEXT,            -- 临时停办的预计恢复月
    reason       TEXT NOT NULL DEFAULT '',
    at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_status_log ON provider_status_log(provider_id, month);

-- 跨街道服务范围(缺省为场地所在街道)
CREATE TABLE IF NOT EXISTS service_areas (
    provider_id  TEXT NOT NULL REFERENCES providers(provider_id),
    street       TEXT NOT NULL,
    weight       REAL NOT NULL CHECK (weight > 0),
    PRIMARY KEY (provider_id, street)
);

-- 机构申报班型容量(按生效月形成历史)
CREATE TABLE IF NOT EXISTS capacities (
    provider_id  TEXT NOT NULL REFERENCES providers(provider_id),
    class_type   TEXT NOT NULL,
    month        TEXT NOT NULL,
    capacity     INTEGER NOT NULL CHECK (capacity >= 0),
    PRIMARY KEY (provider_id, class_type, month)
);

-- 价格承诺(普惠价, 按月生效; 中途调价另起一行)
CREATE TABLE IF NOT EXISTS price_commitments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id  TEXT NOT NULL REFERENCES providers(provider_id),
    class_type   TEXT NOT NULL,
    month        TEXT NOT NULL,
    price        INTEGER NOT NULL CHECK (price >= 0),  -- 单位: 分/月
    UNIQUE (provider_id, class_type, month)
);

-- 匿名候补: 家庭仅以 HMAC 伪名出现, 原始标识在入库前即丢弃
CREATE TABLE IF NOT EXISTS waitlist (
    month       TEXT NOT NULL,
    class_type  TEXT NOT NULL,
    grid        TEXT NOT NULL,
    street      TEXT NOT NULL,
    pseudonym   TEXT NOT NULL,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    PRIMARY KEY (month, class_type, pseudonym)
);

-- 补助政策版本: applies_from 可早于 published_month, 即追溯生效
CREATE TABLE IF NOT EXISTS policies (
    policy_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    version          TEXT NOT NULL,
    action           TEXT NOT NULL,
    class_type       TEXT NOT NULL DEFAULT '',   -- 空=适用于全部班型
    params           TEXT NOT NULL,              -- JSON, 如 {"per_slot": 50000, "price_cap": 300000}
    applies_from     TEXT NOT NULL,
    published_month  TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    UNIQUE (version, action, class_type)
);

-- 月度测算快照: 每次重算生成新 seq, 旧快照保留可追溯
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    month        TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    note         TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    UNIQUE (month, seq)
);

-- 快照内的街道×班型供需缺口
CREATE TABLE IF NOT EXISTS gap_lines (
    snapshot_id  INTEGER NOT NULL REFERENCES snapshots(snapshot_id),
    street       TEXT NOT NULL,
    class_type   TEXT NOT NULL,
    demand       INTEGER NOT NULL,
    waitlist     INTEGER NOT NULL,
    supply       INTEGER NOT NULL,
    gap          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gap_lines ON gap_lines(snapshot_id);

-- 补助台账: 每行记录测算/确认金额、所用政策版本与计费输入;
-- 追回行为 action='补助追回' 的负数语义行, 通过 clawback_of 关联原记录
CREATE TABLE IF NOT EXISTS subsidies (
    subsidy_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id  INTEGER REFERENCES snapshots(snapshot_id),  -- 手工追回行为 NULL
    provider_id  TEXT NOT NULL,
    month        TEXT NOT NULL,
    action       TEXT NOT NULL,
    class_type   TEXT NOT NULL DEFAULT '',
    base_action  TEXT NOT NULL DEFAULT '',     -- 追回行指向的原动作
    policy_id    INTEGER NOT NULL REFERENCES policies(policy_id),
    quantity     INTEGER NOT NULL DEFAULT 0,
    amount       INTEGER NOT NULL,             -- 单位: 分
    status       TEXT NOT NULL DEFAULT '测算', -- 测算/确认/废止
    clawback_of  INTEGER REFERENCES subsidies(subsidy_id),
    reason       TEXT NOT NULL DEFAULT '',
    inputs       TEXT NOT NULL DEFAULT '{}',   -- 计费输入快照(JSON)
    created_at   TEXT NOT NULL,
    UNIQUE (snapshot_id, provider_id, action, class_type, base_action)
);
CREATE INDEX IF NOT EXISTS idx_subsidies ON subsidies(provider_id, month);

-- 托位分配(并发守卫: 事务内校验已分配不超过合规上限)
CREATE TABLE IF NOT EXISTS allocations (
    provider_id  TEXT NOT NULL REFERENCES providers(provider_id),
    month        TEXT NOT NULL,
    class_type   TEXT NOT NULL,
    allocated    INTEGER NOT NULL CHECK (allocated >= 0),
    PRIMARY KEY (provider_id, month, class_type)
);

-- 建设建议: 记录缓解了哪部分缺口(网格级明细存于 detail)
CREATE TABLE IF NOT EXISTS recommendations (
    rec_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id   INTEGER NOT NULL REFERENCES snapshots(snapshot_id),
    street        TEXT NOT NULL,
    class_type    TEXT NOT NULL,
    add_slots     INTEGER NOT NULL,
    covered_gap   INTEGER NOT NULL,
    residual_gap  INTEGER NOT NULL,
    detail        TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_recs ON recommendations(snapshot_id);

-- 操作审计(不记录家庭标识等敏感内容)
CREATE TABLE IF NOT EXISTS audit_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      TEXT NOT NULL,
    role    TEXT NOT NULL,
    action  TEXT NOT NULL,
    entity  TEXT NOT NULL,
    detail  TEXT NOT NULL DEFAULT ''
);
"""


def connect(path):
    """打开连接: WAL、外键、忙等待, 供单请求/单线程使用。"""
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(path):
    """建库建表(幂等)。"""
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()
    return path


@contextmanager
def immediate(conn):
    """IMMEDIATE 写事务: 进入即取写锁, 保证检查-写入序列的原子性。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def transact(path, fn, *args, **kwargs):
    """在独立连接的 IMMEDIATE 事务中执行 fn(conn, *args, **kwargs)。"""
    conn = connect(path)
    try:
        with immediate(conn):
            return fn(conn, *args, **kwargs)
    finally:
        conn.close()


def query(path, fn, *args, **kwargs):
    """只读查询助手。"""
    conn = connect(path)
    try:
        return fn(conn, *args, **kwargs)
    finally:
        conn.close()
