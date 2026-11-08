"""从 chat_history 回填 nick_names.json（昵称 ↔ QQ 号），让记忆详情能显示发言人 QQ。"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import sqlite3
import time

ROOT = pathlib.Path(r"D:\qq-ai-bot")
STORE = ROOT / "memory" / "nick_names.json"
DB = pathlib.Path(os.environ["APPDATA"]) / "dsh-pet-standalone-webm-chat" / "shared_memory.db"


def main() -> int:
    if not DB.exists():
        print("找不到聊天库")
        return 1
    data = {}
    if STORE.exists():
        try:
            data = json.loads(STORE.read_text(encoding="utf-8-sig")) or {}
        except Exception:  # noqa: BLE001
            data = {}
    bak = STORE.with_name(f"nick_names.json.bak_{time.strftime('%Y%m%d_%H%M')}")
    if STORE.exists():
        shutil.copy2(STORE, bak)

    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "select scope, speaker, user_id, timestamp from chat_history "
        "where scope like 'group_%' and user_id is not null and speaker is not null "
        "order by timestamp asc"
    ).fetchall()
    conn.close()

    added = updated = 0
    for r in rows:
        scope = str(r["scope"] or "")
        gid = scope.split("_", 1)[1]
        uid = str(r["user_id"] or "").strip()
        name = str(r["speaker"] or "").strip()[:24]
        if not gid or not uid or not name:
            continue
        g = data.setdefault(gid, {})
        rec = g.get(uid)
        if not isinstance(rec, dict):
            g[uid] = {"first": name, "current": name, "seen": 1,
                      "updated_at": str(r["timestamp"] or "")}
            added += 1
        else:
            if str(rec.get("current") or "") != name:
                rec["current"] = name
                updated += 1
            rec["seen"] = int(rec.get("seen") or 0) + 1

    STORE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"备份: {bak.name if STORE.exists() else '（原来没有）'}")
    print(f"新增 {added} 人 / 更新昵称 {updated} 次，覆盖 {len(data)} 个群")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
