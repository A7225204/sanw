# -*- coding: utf-8 -*-
"""按字查：/查话 某句话 —— 查这句原话在不在这条线上、是谁说的。

    /查话 你答应过吗
    /查话 "上次那个房间卡"

和向量回忆分工：回忆回答"有没有像的"，这个回答"有没有这一句"。
只查当前 scope（私聊的私聊、群里的群），不跨 scope 外泄。
"""
from __future__ import annotations

import logging
import sqlite3

from nonebot import on_command
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent

try:
    from nonebot.log import logger
except Exception:  # noqa: BLE001
    logger = logging.getLogger("trace_cmd")

from memory_manager import DB_PATH

trace_cmd = on_command("查话", priority=5, block=False)


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def search(scope: str, q: str, limit: int = 5) -> list[dict]:
    con = sqlite3.connect(str(DB_PATH), timeout=5)
    try:
        rows = con.execute(
            "SELECT speaker, message, reply, timestamp FROM chat_history "
            "WHERE scope = ? AND (message LIKE ? ESCAPE '\\' OR reply LIKE ? ESCAPE '\\') "
            "ORDER BY id DESC LIMIT ?",
            (scope, _like(q), _like(q), limit),
        ).fetchall()
    finally:
        con.close()
    out = []
    for speaker, message, reply, ts in rows:
        who = []
        if q in (message or ""):
            who.append("对方说的")
        if q in (reply or ""):
            who.append("我说的")
        out.append({"who": "、".join(who) or "包含", "speaker": speaker or "",
                    "time": str(ts or ""), "hit": q})
    return out


@trace_cmd.handle()
async def _(event: MessageEvent):
    if isinstance(event, GroupMessageEvent) and not event.is_tome():
        return
    raw = event.get_plaintext().strip().lstrip("/／")
    if raw.startswith("查话"):
        raw = raw[2:]
    q = raw.strip().strip('"“”').strip()
    if len(q) < 2:
        await trace_cmd.finish("查什么？发 /查话 要查的那句话（至少两个字）")
    scope = f"group_{event.group_id}" if isinstance(event, GroupMessageEvent) else f"private_{event.user_id}"
    try:
        rows = search(scope, q, 5)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[查话] 查询失败：{type(e).__name__}: {e}")
        await trace_cmd.finish("查询失败了，看日志 [查话]。")
    if not rows:
        await trace_cmd.finish(f"没有这一句（按字查，不找像的）。\nscope={scope}")
    lines = [f"按字查到 {len(rows)} 处（{scope}）："]
    for r in rows:
        lines.append(f"· {r['who']}｜{r['time']}｜{r['speaker']}")
    await trace_cmd.finish("\n".join(lines))
