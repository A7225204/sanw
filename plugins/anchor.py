# -*- coding: utf-8 -*-
"""原话锚：把她自己最近说过的几句原话，挑出来放进 system prompt 的慢变块。

想法来自 dsh-hippocampus：身份来自证据（自己说过的话），不是来自说明（关于自己的描述）。
隔离设计：
- 只读 chat_history 的 reply 列，不写任何东西；
- 按 30 分钟一个桶冻结，同一段内完全不变（否则每轮变锚会把前缀缓存打掉）；
- 块进 slow_blocks（历史之后的动态区），不进 stable，缓存友好。
开关：环境变量 ANCHOR_ENABLED=0 关闭；ANCHOR_K 控制条数（默认 4）。
"""
from __future__ import annotations

import os
import sqlite3
import time
from typing import Any

try:
    from nonebot.log import logger
except Exception:  # noqa: BLE001
    import logging
    logger = logging.getLogger("anchor")

BUCKET_SECONDS = 30 * 60
_CACHE: dict[tuple[str, int], tuple[str, ...]] = {}

try:
    from memory_manager import DB_PATH
except Exception:  # noqa: BLE001
    from pathlib import Path

    DB_PATH = Path(os.getenv("APPDATA", "")) / "dsh-pet-standalone-webm-chat" / "shared_memory.db"


def _enabled() -> bool:
    return os.getenv("ANCHOR_ENABLED", "1").lower() not in ("0", "false", "no")


def _k() -> int:
    try:
        return max(2, min(6, int(os.getenv("ANCHOR_K", "4"))))
    except Exception:  # noqa: BLE001
        return 4


def _usable(text: str) -> bool:
    t = (text or "").strip()
    if not (12 <= len(t) <= 90):
        return False
    if "http" in t or "【" in t:  # 链接、内部标记不要
        return False
    if t.startswith("[表情") or t.startswith("（"):  # 纯表情/纯动作不算"话"
        return False
    return True


def _candidates(scope: str, limit: int = 200) -> list[str]:
    try:
        con = sqlite3.connect(str(DB_PATH), timeout=5)
        try:
            rows = con.execute(
                "SELECT reply FROM chat_history WHERE scope = ? AND reply IS NOT NULL "
                "ORDER BY id DESC LIMIT ?",
                (scope, limit),
            ).fetchall()
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[锚] 读 chat_history 失败：{type(e).__name__}: {e}")
        return []
    out: list[str] = []
    seen = set()
    for (reply,) in rows:
        t = (reply or "").strip()
        if not _usable(t):
            continue
        key = t[:16]
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


def anchor_lines(scope: str) -> tuple[str, ...]:
    """同一个 30 分钟桶里返回完全相同的几条；换桶才重挑。"""
    if not _enabled() or not scope:
        return ()
    bucket = int(time.time() // BUCKET_SECONDS)
    key = (scope, bucket)
    hit = _CACHE.get(key)
    if hit is not None:
        return hit
    pool = _candidates(scope)
    if not pool:
        _CACHE[key] = ()
        return ()
    k = min(_k(), len(pool))
    # 用桶号当种子：同桶稳定，跨桶换一批（不引入 random 的不可复现）
    idx = sorted({(bucket * 7 + i * 13) % len(pool) for i in range(k * 3)})[:k]
    picked = tuple(pool[i] for i in idx)
    # 只留最近 4 个桶，防止字典无限长
    for old in [kk for kk in _CACHE if kk[0] == scope and kk[1] < bucket - 4]:
        _CACHE.pop(old, None)
    _CACHE[key] = picked
    return picked


def anchor_block(scope: str) -> str:
    lines = anchor_lines(scope)
    if not lines:
        return ""
    body = "\n".join("· " + t for t in lines)
    return "【我的原话（不是设定，是我自己说过的口气）】\n" + body


def stats(scope: str) -> dict[str, Any]:
    return {"enabled": _enabled(), "k": _k(), "cached": sum(1 for kk in _CACHE if kk[0] == scope),
            "total_candidates": len(_candidates(scope, 50))}
