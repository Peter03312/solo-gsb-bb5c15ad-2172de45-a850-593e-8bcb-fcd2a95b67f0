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
    event_id      TEXT NOT NULL UNIQUE,                  -- 全局唯一事件号
    event_type    TEXT NOT NULL CHECK (event_type IN ('transfer','receive','seal_check','storage')),
    occurred_at   TEXT NOT NULL,                         -- 事件发生时间（UTC ISO8601）
    payload_json  TEXT NOT NULL,                         -- 类型相关字段
    content_hash  TEXT NOT NULL,                        -- 事件内容指纹（同事件号重试比对）
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_event_sample_time ON custody_event(sample_id, occurred_at, id);
"""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def get_connection() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(config.DB_PATH), timeout=config.DB_TIMEOUT)
    conn.row_factory = sqlite3.Row
    # WAL 提升读写并发，遇到锁立即报错由服务层转 409
    conn.execute("PRAGMA journal_mode=WAL");
    conn.execute("PRAGMA foreign_keys=ON");
    conn.execute("PRAGMA busy_timeout=%d" % int(config.DB_TIMEOUT * 1000))
    return conn


@contextmanager
def db_session(commit: bool = True) -> Iterator[sqlite3.Connection]:
    conn = get_connection()
    try:
        yield conn
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """进程启动时建表（幂等）。"""
    with db_session() as conn:
        conn.executescript(SCHEMA)


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None
