"""SQLite 存储层：建表、连接管理、通用查询辅助。"""

from __future__ import annotations

import os
import sqlite3
import stat
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT    NOT NULL,
    role          TEXT    NOT NULL DEFAULT 'user',
    disabled      INTEGER NOT NULL DEFAULT 0,
    must_change   INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id             TEXT PRIMARY KEY,
    owner_id       INTEGER,
    owner_name     TEXT NOT NULL DEFAULT '',
    title          TEXT NOT NULL DEFAULT '',
    source_name    TEXT NOT NULL DEFAULT '',
    source_path    TEXT NOT NULL DEFAULT '',
    source_ext     TEXT NOT NULL DEFAULT '',
    source_bytes   INTEGER NOT NULL DEFAULT 0,
    kind           TEXT NOT NULL DEFAULT 'document',
    pages          INTEGER NOT NULL DEFAULT 0,
    settings_json  TEXT NOT NULL DEFAULT '{}',
    state          TEXT NOT NULL DEFAULT 'queued',
    message        TEXT NOT NULL DEFAULT '',
    error          TEXT NOT NULL DEFAULT '',
    backend        TEXT NOT NULL DEFAULT '',
    printer        TEXT NOT NULL DEFAULT '',
    pass1_path     TEXT NOT NULL DEFAULT '',
    pass2_path     TEXT NOT NULL DEFAULT '',
    pass1_pages    INTEGER NOT NULL DEFAULT 0,
    pass2_pages    INTEGER NOT NULL DEFAULT 0,
    cups_id1       TEXT NOT NULL DEFAULT '',
    cups_id2       TEXT NOT NULL DEFAULT '',
    command1       TEXT NOT NULL DEFAULT '',
    command2       TEXT NOT NULL DEFAULT '',
    continued_at   TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    finished_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_state   ON jobs(state);

CREATE TABLE IF NOT EXISTS login_attempts (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ip       TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL DEFAULT '',
    at       REAL NOT NULL,
    ok       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_attempts_at ON login_attempts(at);

CREATE TABLE IF NOT EXISTS audit (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    at     TEXT NOT NULL,
    actor  TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: Path | str) -> sqlite3.Connection:
    """打开连接（autocommit，需要事务时显式 BEGIN）。"""
    path = str(path)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """建表 + 版本记录。"""
    conn.executescript(SCHEMA)
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current < SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    if os.name == "posix":
        try:
            os.chmod(conn.execute("PRAGMA database_list").fetchone()[2], stat.S_IRUSR | stat.S_IWUSR)
        except (OSError, TypeError, IndexError):
            pass


@contextmanager
def transaction(conn: sqlite3.Connection, immediate: bool = True) -> Iterator[sqlite3.Connection]:
    """显式事务；immediate=True 时立刻拿写锁，避免并发重复提交。"""
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def query_all(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, tuple(params)).fetchall()


def query_one(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
    return conn.execute(sql, tuple(params)).fetchone()


def execute(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
    return conn.execute(sql, tuple(params))


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def kv_get(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = query_one(conn, "SELECT value FROM kv WHERE key = ?", (key,))
    return row["value"] if row else default


def kv_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    execute(
        conn,
        "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


class Database:
    """按线程持有连接的数据库句柄（Flask 请求线程 + 后台工作线程各一份）。"""

    def __init__(self, path: Path | str) -> None:
        import threading

        self.path = Path(path)
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._initialized = False

    def _new_connection(self) -> sqlite3.Connection:
        conn = connect(self.path)
        with self._init_lock:
            if not self._initialized:
                init_db(conn)
                self._initialized = True
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_connection()
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            finally:
                self._local.conn = None

    def initialize(self) -> None:
        """显式初始化（建表 + 迁移）。"""
        _ = self.conn
