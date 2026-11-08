# -*- coding: utf-8 -*-
"""一次性迁移：旧 memory.db → 新 shared_memory.db（幂等，可重复运行）。

背景
----
早期桌宠直接对话写的是 `memory.db`（表：chat_history，只有 speaker/message/reply/timestamp，
没有 user_id、scope，也没有 affinity 表）。后端切换到新库 `shared_memory.db` 后，这几条早期
记录一直留在旧库里。本脚本把它们补迁进新库，去重后插入，不删旧库。

用法
----
    python migrate_old_memory.py            # 预览：只打印将迁移/跳过哪些行，不写库
    python migrate_old_memory.py --apply    # 实际写入新库

映射（按探查结果：旧库里 speaker 都是主人「枷锁内的示例」）
------------------------------------------------------------
    speaker 枷锁内的示例 -> user_id 100000000, scope private_100000000
其他未登记 speaker：user_id/scope 置空并打印警告，不臆测归属。
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

BASE = Path(os.getenv("APPDATA") or Path.home() / ".local" / "share") / "dsh-pet-standalone-webm-chat"
OLD_DB = BASE / "memory.db"
NEW_DB = BASE / "shared_memory.db"

# speaker -> (user_id, scope)
SPEAKER_MAP = {
    "枷锁内的示例": ("100000000", "private_100000000"),
}


def ensure_schema(conn: sqlite3.Connection) -> None:
    c = conn.cursor()
    c.execute(
        """CREATE TABLE IF NOT EXISTS chat_history
           (id INTEGER PRIMARY KEY AUTOINCREMENT,
            speaker TEXT, user_id TEXT, scope TEXT,
            message TEXT, reply TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS affinity
           (user_id TEXT PRIMARY KEY, score INTEGER DEFAULT 50, tag TEXT DEFAULT '')"""
    )
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
    conn.commit()


def main() -> int:
    apply = "--apply" in sys.argv
    print(f"旧库: {OLD_DB}")
    print(f"新库: {NEW_DB}")
    print(f"模式: {'写入(--apply)' if apply else '预览(只打印，不写库)'}\n")

    if not OLD_DB.exists():
        print("旧库不存在，无需迁移。")
        return 0

    old = sqlite3.connect(f"file:{OLD_DB.as_posix()}?mode=ro", uri=True, timeout=10)
    old_rows = old.execute(
        "SELECT speaker, message, reply, timestamp FROM chat_history ORDER BY id"
    ).fetchall()
    old.close()
    print(f"旧库待迁移记录: {len(old_rows)} 条")

    conn = sqlite3.connect(NEW_DB, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL;")
    ensure_schema(conn)

    existing = {
        (r[0], r[1], r[2])
        for r in conn.execute("SELECT speaker, message, reply FROM chat_history")
    }

    inserted, skipped = 0, 0
    warnings: list[str] = []
    for speaker, message, reply, ts in old_rows:
        key = (speaker, message, reply)
        if key in existing:
            skipped += 1
            print(f"  [跳过·已存在] {speaker}: {(message or '')[:30]!r}")
            continue
        user_id, scope = SPEAKER_MAP.get(speaker, ("", ""))
        if not user_id:
            warnings.append(f"未登记 speaker={speaker!r}，user_id/scope 置空")
        if apply:
            conn.execute(
                "INSERT INTO chat_history (speaker, user_id, scope, message, reply, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (speaker, user_id, scope, message, reply, ts),
            )
            existing.add(key)
        inserted += 1
        print(f"  [{'写入' if apply else '待写入'}] {speaker} ({scope or 'scope空'}) "
              f"msg={(message or '')[:30]!r} reply={(reply or '')[:30]!r} ts={ts}")

    if apply:
        conn.commit()

    old_total = conn.execute("SELECT COUNT(*) FROM chat_history").fetchone()[0]
    aff_total = conn.execute("SELECT COUNT(*) FROM affinity").fetchone()[0]
    conn.close()

    print(f"\n结果: 新插入 {inserted} 条，跳过重复 {skipped} 条")
    print(f"迁移后新库 chat_history 共 {old_total} 条，affinity {aff_total} 条（本次不动好感度）")
    for w in warnings:
        print("警告:", w)
    if not apply and inserted:
        print("\n确认无误后加 --apply 正式写入。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
