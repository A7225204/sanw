# -*- coding: utf-8 -*-
"""LLM 令牌用量累计。

- 只追加 token_usage 表，供仪表盘只读聚合；
- 写入全程静默容错，任何异常都不允许阻断机器人聊天主流程；
- 价格不在本模块估算，费用由仪表盘读取 .env 单价后按 token 汇总。
"""
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from memory_manager import DB_PATH  # 复用主记忆库路径
except Exception:  # 隔离探针把 memory_manager 桩成空模块时回退默认路径
    _base = os.getenv("APPDATA") or str(Path.home() / ".local" / "share")
    DB_PATH = Path(_base) / "dsh-pet-standalone-webm-chat" / "shared_memory.db"

_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS token_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  date TEXT NOT NULL,
  scope TEXT NOT NULL,
  provider TEXT DEFAULT 'ark',
  prompt_tokens INTEGER DEFAULT 0,
  completion_tokens INTEGER DEFAULT 0,
  cached_tokens INTEGER DEFAULT 0,
  total_tokens INTEGER DEFAULT 0,
  calls INTEGER DEFAULT 1,
  cost REAL DEFAULT 0,
  channel TEXT DEFAULT '',
  gid TEXT DEFAULT ''
)
"""

_LOCAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS local_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  date TEXT NOT NULL,
  task TEXT NOT NULL,
  calls INTEGER DEFAULT 1,
  seconds REAL DEFAULT 0
)
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_table() -> None:
    """确保表与索引存在；失败静默。"""
    try:
        with _lock, _connect() as conn:
            conn.execute(_SCHEMA)
            conn.execute(_LOCAL_SCHEMA)
            cols = [r[1] for r in conn.execute("PRAGMA table_info(token_usage)").fetchall()]
            if "provider" not in cols:
                conn.execute("ALTER TABLE token_usage ADD COLUMN provider TEXT DEFAULT 'ark'")
            # 2026-10-28（t3 阶段一）：按通道/会话拆命中率。只加列，不迁移历史数据。
            if "channel" not in cols:
                conn.execute("ALTER TABLE token_usage ADD COLUMN channel TEXT DEFAULT ''")
            if "gid" not in cols:
                conn.execute("ALTER TABLE token_usage ADD COLUMN gid TEXT DEFAULT ''")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_token_date ON token_usage(date)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_token_scope ON token_usage(scope)")
            # 2026-10-28（t9）：兼容视图。审计/验收脚本里长期把它写成 `usage`
            # （历史写法，见 docs/缓存改造说明.md §11），建一个只读视图当别名，
            # 让 `select … from usage` 这类老命令继续能跑；真实表仍是 token_usage。
            conn.execute("CREATE VIEW IF NOT EXISTS usage AS SELECT * FROM token_usage")
            conn.commit()
    except Exception:
        pass


def record(usage: dict[str, Any] | None, scope: str, cost: float = 0.0, provider: str = "ark",
           channel: str = "", gid: str = "") -> None:
    """记录一次 LLM 调用用量。空 usage 跳过；任何异常静默吞掉。

    channel：qq / feishu / internal:<scope>；空时自动用 scope 兜底（历史数据里是空）；
    调用方显式传了就以调用方为准。gid：会话标识
    （private_<uid> / <群号>）。两者只为面板按通道/会话拆命中率，不参与计价。
    """
    try:
        usage = usage or {}
        pin = int(usage.get("prompt_tokens") or 0)
        details = usage.get("prompt_tokens_details") or {}
        cached = int(
            details.get("cached_tokens")
            or usage.get("prompt_cache_hit_tokens")
            or usage.get("cached_tokens")
            or 0
        )
        pout = int(usage.get("completion_tokens") or 0)
        total = int(usage.get("total_tokens") or 0) or (pin + pout)
        if pin == 0 and pout == 0 and total == 0 and not cost:
            return
        now = datetime.now()
        ts = now.strftime("%Y-%m-%d %H:%M:%S")
        date = now.strftime("%Y-%m-%d")
        with _lock, _connect() as conn:
            conn.execute(_SCHEMA)
            conn.execute(
                "INSERT INTO token_usage"
                "(ts, date, scope, provider, prompt_tokens, completion_tokens, "
                "cached_tokens, total_tokens, calls, cost, channel, gid) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                (ts, date, scope, str(provider or "ark"), pin, pout, cached, total,
                 float(cost or 0.0), str(channel or scope or ""), str(gid or "")),
            )
            conn.commit()
    except Exception:
        pass


# 模块导入时兜底建表，避免首次调用前表不存在。
init_table()


def record_vision(provider: str, seconds: float) -> None:
    """记一次识图调用落在哪条通道上（谷歌/硅基/火山/本地），用于面板按通道统计。

    谷歌是免费的不产生 token，只能靠这个计数，否则面板上完全看不见它。
    """
    try:
        now = datetime.now()
        with _lock, _connect() as conn:
            conn.execute(_SCHEMA)
            conn.execute(_LOCAL_SCHEMA)
            conn.execute(
                "INSERT INTO local_usage(ts, date, task, calls, seconds) VALUES(?, ?, ?, 1, ?)",
                (now.strftime("%Y-%m-%d %H:%M:%S"), now.strftime("%Y-%m-%d"),
                 f"vision:{str(provider or 'unknown')}", float(seconds or 0)),
            )
            conn.commit()
    except Exception:
        pass


def record_local(task: str, seconds: float) -> None:
    """记录一次本地推理（GPU 秒数），不产生 API 费用。"""
    try:
        now = datetime.now()
        with _lock, _connect() as conn:
            conn.execute(_SCHEMA)
            conn.execute(_LOCAL_SCHEMA)
            conn.execute(
                "INSERT INTO local_usage(ts, date, task, calls, seconds) VALUES(?, ?, ?, 1, ?)",
                (now.strftime("%Y-%m-%d %H:%M:%S"), now.strftime("%Y-%m-%d"),
                 str(task or "local"), float(seconds or 0)),
            )
            conn.commit()
    except Exception:
        pass
