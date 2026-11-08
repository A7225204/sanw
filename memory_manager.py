"""SQLite 记忆与好感度存储（带 scope 隔离）。"""
from __future__ import annotations

import os
import random
import sqlite3
from pathlib import Path
from typing import Optional


def _default_db_path() -> Path:
    base = os.getenv("APPDATA")
    if not base:
        base = str(Path.home() / ".local" / "share")
    return Path(base) / "dsh-pet-standalone-webm-chat" / "shared_memory.db"


DB_PATH = _default_db_path()

# 保留天数，0 表示永久（默认永久，不再 30 天自动删）
_RETENTION_DAYS = int(os.getenv("MEMORY_RETENTION_DAYS", "0") or "0")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        c = conn.cursor()
        c.execute(
            """CREATE TABLE IF NOT EXISTS chat_history
               (id INTEGER PRIMARY KEY AUTOINCREMENT,
                speaker TEXT,
                user_id TEXT,
                scope TEXT,
                message TEXT,
                reply TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS affinity
               (user_id TEXT PRIMARY KEY,
                score INTEGER DEFAULT 50,
                tag TEXT DEFAULT '')"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS user_tags
               (user_id TEXT NOT NULL,
                scope TEXT NOT NULL DEFAULT '',
                tag TEXT DEFAULT '',
                PRIMARY KEY (user_id, scope))"""
        )
        # 旧版本只有全局 tag，迁到 scope='' 一行，群/私聊标签互不覆盖
        try:
            c.execute(
                "INSERT OR IGNORE INTO user_tags(user_id, scope, tag) "
                "SELECT user_id, '', tag FROM affinity WHERE tag IS NOT NULL AND tag != ''"
            )
        except sqlite3.OperationalError:
            pass
        # 兼容旧表
        for ddl in (
            "ALTER TABLE chat_history ADD COLUMN user_id TEXT",
            "ALTER TABLE chat_history ADD COLUMN scope TEXT",
            "ALTER TABLE affinity ADD COLUMN tag TEXT DEFAULT ''",
        ):
            try:
                c.execute(ddl)
            except sqlite3.OperationalError:
                pass
        c.execute("CREATE INDEX IF NOT EXISTS idx_chat_user ON chat_history(user_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_chat_scope ON chat_history(scope)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_chat_ts ON chat_history(timestamp)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_affinity_score ON affinity(score)")
        conn.commit()


# ---------------------------------------------------------------------------
# 聊天记忆
# ---------------------------------------------------------------------------
def add_memory(speaker: str, user_id: str, scope: str, message: str, reply: str) -> None:
    """写入一条聊天记忆。scope 形如 group_{gid} 或 private_{uid}。"""
    init_db()
    with _connect() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO chat_history (speaker, user_id, scope, message, reply) "
            "VALUES (?, ?, ?, ?, ?)",
            (speaker, user_id, scope, message, reply),
        )
        # 只有配置了保留天数才清理；默认 0 表示永久
        if _RETENTION_DAYS > 0 and random.randint(1, 50) == 1:
            c.execute(
                "DELETE FROM chat_history WHERE timestamp < date('now', ?)",
                (f"-{_RETENTION_DAYS} days",),
            )
        conn.commit()


def _scope_clause(scope: Optional[str]) -> tuple[str, tuple]:
    if scope:
        return " AND scope = ?", (scope,)
    return "", ()


def get_recent_memory(limit: int = 100, scope: Optional[str] = None) -> list[dict]:
    init_db()
    clause, params = _scope_clause(scope)
    with _connect() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT speaker, message, reply FROM chat_history "
            "WHERE 1=1" + clause + " ORDER BY id DESC LIMIT ?",
            (*params, limit),
        )
        rows = c.fetchall()
    return [{"speaker": r[0], "message": r[1], "reply": r[2]} for r in reversed(rows)]


def get_today_memory(limit: int = 200, scope: Optional[str] = None) -> list[dict]:
    init_db()
    clause, params = _scope_clause(scope)
    with _connect() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT speaker, message FROM chat_history "
            "WHERE date(timestamp, 'localtime') = date('now', 'localtime')"
            + clause +
            " ORDER BY id DESC LIMIT ?",
            (*params, limit),
        )
        rows = c.fetchall()
    return [{"speaker": r[0], "message": r[1]} for r in reversed(rows)]


def count_memory(scope: Optional[str] = None, today: bool = False) -> int:
    """统计聊天记忆条数；scope 隔离，today=True 只数今天。"""
    init_db()
    clause = ""
    params: list = []
    if today:
        clause += " AND date(timestamp, 'localtime') = date('now', 'localtime')"
    if scope:
        clause += " AND scope = ?"
        params.append(scope)
    with _connect() as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM chat_history WHERE 1=1" + clause, params)
        row = c.fetchone()
    return int(row[0]) if row else 0


def get_last_message(user_id: str, scope: Optional[str] = None):
    init_db()
    clause, params = _scope_clause(scope)
    with _connect() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT speaker, message FROM chat_history "
            "WHERE user_id = ?" + clause + " ORDER BY id DESC LIMIT 1",
            (user_id, *params),
        )
        row = c.fetchone()
    return row if row else None


def get_user_recent_messages(user_id: str, limit: int = 10,
                             scope: Optional[str] = None) -> list[str]:
    init_db()
    clause, params = _scope_clause(scope)
    with _connect() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT message FROM chat_history "
            "WHERE user_id = ?" + clause + " ORDER BY id DESC LIMIT ?",
            (user_id, *params, limit),
        )
        rows = c.fetchall()
    return [r[0] for r in reversed(rows)]


# ---------------------------------------------------------------------------
# 好感度 / 外号
# ---------------------------------------------------------------------------
def _ensure_affinity_row(c: sqlite3.Cursor, user_id: str) -> None:
    c.execute(
        "INSERT OR IGNORE INTO affinity (user_id, score) VALUES (?, 50)",
        (user_id,),
    )


def get_affinity(user_id: str) -> int:
    init_db()
    with _connect() as conn:
        c = conn.cursor()
        _ensure_affinity_row(c, user_id)
        c.execute("SELECT score FROM affinity WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        conn.commit()
    return int(row[0]) if row and row[0] is not None else 50


def update_affinity(user_id: str, delta: int) -> int:
    init_db()
    with _connect() as conn:
        c = conn.cursor()
        _ensure_affinity_row(c, user_id)
        c.execute(
            "UPDATE affinity SET score = MAX(0, MIN(100, score + ?)) "
            "WHERE user_id = ?",
            (delta, user_id),
        )
        c.execute("SELECT score FROM affinity WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        conn.commit()
    return int(row[0]) if row and row[0] is not None else 50


def get_tag(user_id: str, scope: str = "") -> str:
    init_db()
    with _connect() as conn:
        c = conn.cursor()
        c.execute("SELECT tag FROM user_tags WHERE user_id = ? AND scope = ?",
                  (user_id, scope or ""))
        row = c.fetchone()
        if (not row or not row[0]) and scope:
            # 旧数据只有全局外号，先兼容；不会写回全局
            c.execute("SELECT tag FROM user_tags WHERE user_id = ? AND scope = ''",
                      (user_id,))
            row = c.fetchone()
    return (row[0] or "") if row else ""


def set_tag(user_id: str, tag: str, scope: str = "") -> None:
    init_db()
    with _connect() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO user_tags(user_id, scope, tag) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id, scope) DO UPDATE SET tag = excluded.tag",
            (user_id, scope or "", tag),
        )
        conn.commit()


def get_affinity_extremes():
    """返回 (最高好感用户, 最低好感用户)，每项为 (user_id, score, tag) 或 None。"""
    init_db()
    with _connect() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT user_id, score, tag FROM affinity "
            "ORDER BY score DESC, user_id ASC LIMIT 1"
        )
        highest = c.fetchone()
        c.execute(
            "SELECT user_id, score, tag FROM affinity "
            "ORDER BY score ASC, user_id ASC LIMIT 1"
        )
        lowest = c.fetchone()
    return highest, lowest