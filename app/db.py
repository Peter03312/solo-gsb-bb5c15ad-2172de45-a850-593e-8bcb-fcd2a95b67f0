"""SQLite 连接、建表初始化与底层读写工具。"""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS sample (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_code         TEXT NOT NULL UNIQUE,            -- 样本编号（业务唯一）
    collected_at        TEXT NOT NULL,                   -- 采集时间（UTC ISO8601）
    collector           TEXT NOT NULL,                   -- 采集人（初始保管人）
    seal_no             TEXT NOT NULL,                   -- 登记封签号
    content_hash        TEXT NOT NULL,                   -- 登记内容指纹（幂等/冲突判定）
    approved_storage_at TEXT,                            -- 入库核定时间；非空即历史锁定
    approved_by         TEXT,                            -- 核定操作人
    approve_note        TEXT,                            -- 核定备注
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS custody_event (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id     INTEGER NOT NULL REFERENCES sample(id),
    event_id      TEXT NOT NULL UNIQUE,                  -- 全局唯一事件号（作废/替换后仍永久占用）
    event_type    TEXT NOT NULL CHECK (event_type IN ('transfer','receive','seal_check','storage')),
    occurred_at   TEXT NOT NULL,                         -- 事件发生时间（UTC ISO8601）
    payload_json  TEXT NOT NULL,                         -- 类型相关字段
    content_hash  TEXT NOT NULL,                         -- 事件内容指纹（同事件号重试比对）
    status        TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active','voided','replaced')),  -- 有效/已作废/已被替换
    correction_id TEXT,                                  -- 使其失效的纠错号（留痕，可溯）
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_event_sample_time ON custody_event(sample_id, occurred_at, id);

CREATE TABLE IF NOT EXISTS correction (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    correction_id   TEXT NOT NULL UNIQUE,                -- 全局唯一纠错号
    sample_id       INTEGER NOT NULL REFERENCES sample(id),
    action          TEXT NOT NULL CHECK (action IN ('void','replace')),  -- 作废/替换
    target_event_id TEXT NOT NULL,                       -- 被纠错的当前有效事件号
    new_event_id    TEXT,                                -- replace 产生的新事件号
    operator        TEXT NOT NULL,                       -- 操作人
    reason          TEXT NOT NULL,                       -- 纠错原因
    content_hash    TEXT NOT NULL,                       -- 纠错内容指纹（同纠错号重试比对）
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_correction_sample ON correction(sample_id, id);
"""

# 既有库就地升级：(表, 新增列, 列定义)。新库由 SCHEMA 直接建全，此处自动跳过。
_MIGRATIONS = (
    ("custody_event", "status",
     "status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','voided','replaced'))"),
    ("custody_event", "correction_id", "correction_id TEXT"),
)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def get_connection() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None：关闭驱动隐式事务，事务由 db_session 显式开启
    conn = sqlite3.connect(str(config.DB_PATH), timeout=config.DB_TIMEOUT, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # WAL 提升读写并发；写请求用 BEGIN IMMEDIATE 串行化，遇锁等待 busy_timeout
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=%d" % int(config.DB_TIMEOUT * 1000))
    return conn


@contextmanager
def db_session(commit: bool = True, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    """单请求单事务。

    immediate=True 时以 BEGIN IMMEDIATE 开启：进入事务即持有写锁，保证
    “先读后写”类操作（纠错、核定、事件写入）基于最新已提交链并彼此串行——
    纠错与核定并发时，核定只能看到最终提交的有效链。
    """
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        yield conn
        if commit:
            conn.commit()
        else:
            conn.rollback()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def init_db() -> None:
    """进程启动时建表（幂等），并对既有库做就地列升级。"""
    with db_session() as conn:
        conn.executescript(SCHEMA)
        for table, column, ddl in _MIGRATIONS:
            _ensure_column(conn, table, column, ddl)


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None
