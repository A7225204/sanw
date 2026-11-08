#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 QQ 机器人的人设与记忆暴露给 DSH / 其他 agent 读取。

数据来源：
* 人设：      D:\\qq-ai-bot\\persona.md
* 聊天记忆：  %APPDATA%\\dsh-pet-standalone-webm-chat\\shared_memory.db（chat_history）
* 长期档案：  D:\\qq-ai-bot\\memory\\长期记忆.json
* 好感度：    shared_memory.db（affinity）

用法：
    python tools/bot_memory.py persona
    python tools/bot_memory.py recent 12 [--scope private_100000000]
    python tools/bot_memory.py search 关键词
    python tools/bot_memory.py affinity 100000000
    python tools/bot_memory.py longterm
    python tools/bot_memory.py all\n    python tools/bot_memory.py add --message "主人说的话" --reply "本鱼的回话" [--scope private_100000000] [--dry-run]
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PERSONA = ROOT / "persona.md"
LONGTERM = ROOT / "memory" / "长期记忆.json"
DB = Path(os.getenv("APPDATA", "")) / "dsh-pet-standalone-webm-chat" / "shared_memory.db"
_DB_PATH = DB

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def _db(readonly: bool = True) -> sqlite3.Connection:
    if not _DB_PATH.exists():
        raise SystemExit(f"找不到记忆库：{_DB_PATH}")
    if readonly:
        return sqlite3.connect(f"file:{_DB_PATH}?mode=ro", uri=True, timeout=10)
    return sqlite3.connect(_DB_PATH, timeout=10)


def cmd_persona() -> None:
    text = PERSONA.read_text(encoding="utf-8").strip()
    print("[人设 persona.md]")
    print(text)


def cmd_recent(n: int, scope: str | None) -> None:
    sql = "SELECT timestamp, speaker, user_id, scope, message, reply FROM chat_history"
    args: tuple = ()
    if scope:
        sql += " WHERE scope = ?"
        args = (scope,)
    sql += " ORDER BY id DESC LIMIT ?"
    args = args + (n,)
    with _db() as c:
        rows = c.execute(sql, args).fetchall()
    print(f"[最近 {len(rows)} 条聊天记忆]")
    for ts, speaker, uid, sc, msg, reply in reversed(rows):
        print(f"\n[{ts}] {speaker}({uid}) @ {sc}")
        print(f"  对方：{(msg or '').strip()[:300]}")
        print(f"  本鱼：{(reply or '').strip()[:300]}")


def cmd_search(keyword: str, n: int = 10) -> None:
    with _db() as c:
        rows = c.execute(
            "SELECT timestamp, speaker, scope, message, reply FROM chat_history "
            "WHERE message LIKE ? OR reply LIKE ? ORDER BY id DESC LIMIT ?",
            (f"%{keyword}%", f"%{keyword}%", n),
        ).fetchall()
    print(f"[搜索「{keyword}」：{len(rows)} 条]")
    for ts, speaker, sc, msg, reply in rows:
        print(f"\n[{ts}] {speaker} @ {sc}")
        print(f"  对方：{(msg or '')[:200]}")
        print(f"  本鱼：{(reply or '')[:200]}")


def cmd_affinity(user_id: str) -> None:
    with _db() as c:
        row = c.execute("SELECT score, tag FROM affinity WHERE user_id = ?", (user_id,)).fetchone()
    if not row:
        print(f"QQ {user_id} 暂无好感度记录")
        return
    score, tag = row
    print(f"[好感度] QQ {user_id}：{score}/100" + (f"，标签：{tag}" if tag else ""))


def cmd_add(message: str, reply: str, speaker: str = "主人", user_id: str = "100000000",
            scope: str = "private_100000000", ts: str | None = None, dry: bool = False) -> None:
    message = (message or "").strip()
    reply = (reply or "").strip()
    if not message or not reply:
        raise SystemExit("message 和 reply 都不能为空")
    if len(message) > 4000:
        message = message[:4000]
    if len(reply) > 4000:
        reply = reply[:4000]
    if dry:
        print(f"[dry-run] 将写入 scope={scope} speaker={speaker} user={user_id}")
        print(f"  对方：{message[:120]}")
        print(f"  本鱼：{reply[:120]}")
        return
    with _db(readonly=False) as c:
        cur = c.execute(
            "INSERT INTO chat_history (speaker, user_id, scope, message, reply, timestamp) "
            "VALUES (?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))",
            (speaker, user_id, scope, message, reply, ts),
        )
        c.commit()
        new_id = cur.lastrowid
    print(f"✅ 已写入记忆 #{new_id}（scope={scope}，{speaker} -> 本鱼）")
    print("   桌宠同步引擎会在几秒内把这条注入 manifest.json")


def cmd_longterm() -> None:
    data = json.loads(LONGTERM.read_text(encoding="utf-8"))
    archive = data.get("archive") or {}
    total = sum(len(v) for v in archive.values() if isinstance(v, list))
    print(f"[长期档案] 共 {len(archive)} 个 scope，{total} 条")
    for scope, items in archive.items():
        if not isinstance(items, list):
            continue
        print(f"\n== {scope} ({len(items)})")
        for item in items[-30:]:
            if isinstance(item, dict):
                print(f"  - {item.get('time', '')} [{item.get('subject', '')}] {item.get('fact', '')}")


def cmd_all() -> None:
    print("===== 人设 =====")
    print(PERSONA.read_text(encoding="utf-8").strip()[:2000])
    print("\n===== 最近记忆 =====")
    cmd_recent(8, None)
    print("\n===== 主人好感度 =====")
    cmd_affinity("100000000")
    print("\n===== 长期档案 =====")
    cmd_longterm()


def main() -> None:
    global _DB_PATH
    argv = sys.argv[1:]
    if "--db" in argv:
        idx = argv.index("--db")
        try:
            _DB_PATH = Path(argv[idx + 1])
        except IndexError:
            raise SystemExit("--db 后面要跟路径")
        del argv[idx:idx + 2]
    if not argv:
        cmd_all()
        return
    cmd = argv[0]
    if cmd == "persona":
        cmd_persona()
    elif cmd == "recent":
        n = int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else 10
        scope = None
        if "--scope" in argv:
            scope = argv[argv.index("--scope") + 1]
        cmd_recent(n, scope)
    elif cmd == "search":
        if len(argv) < 2:
            raise SystemExit("用法：bot_memory.py search 关键词")
        cmd_search(argv[1])
    elif cmd == "affinity":
        cmd_affinity(argv[1] if len(argv) > 1 else "100000000")
    elif cmd == "longterm":
        cmd_longterm()
    elif cmd == "all":
        cmd_all()
    elif cmd in ("add", "record"):
        def opt(name: str, default: str = "") -> str:
            return argv[argv.index(name) + 1] if name in argv else default
        cmd_add(
            message=opt("--message"),
            reply=opt("--reply"),
            speaker=opt("--speaker", "主人"),
            user_id=opt("--user", "100000000"),
            scope=opt("--scope-write", opt("--scope", "private_100000000")),
            ts=opt("--time") or None,
            dry="--dry-run" in argv,
        )
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()

