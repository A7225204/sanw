"""一次性回填：给已有记忆条目补 sourceMessages（从 chat_history 里做 2-gram 匹配）。

之后新记忆由 long_memory._match_sources 自动写 src，这里只处理存量。
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import sqlite3
import time

ROOT = pathlib.Path(r"D:\qq-ai-bot")
LM = ROOT / "memory" / "长期记忆.json"
DB = pathlib.Path(os.environ["APPDATA"]) / "dsh-pet-standalone-webm-chat" / "shared_memory.db"

PUNCT = re.compile(r"[\s，。！？、：；,.!?;:\-—…·\"'“”‘’（）()【】\[\]~～*#@]+")


def bigrams(text: str) -> set[str]:
    t = PUNCT.sub("", str(text or ""))
    if len(t) < 2:
        return {t} if t else set()
    return {t[i:i + 2] for i in range(len(t) - 1)}


def main() -> int:
    if not DB.exists():
        print("找不到聊天库：", DB)
        return 1
    data = json.loads(LM.read_text(encoding="utf-8-sig"))
    arch = data.get("archive") or {}

    bak = LM.with_name(f"长期记忆.json.bak_srcbackfill_{time.strftime('%Y%m%d_%H%M')}")
    shutil.copy2(LM, bak)

    conn = sqlite3.connect(str(DB), timeout=15)
    conn.row_factory = sqlite3.Row
    filled = total = 0
    for scope, items in arch.items():
        try:
            rows = conn.execute(
                "select message, timestamp from chat_history where scope=? order by rowid desc limit 800",
                (scope,),
            ).fetchall()
        except Exception:  # noqa: BLE001
            rows = []
        if not rows:
            continue
        msgs = [(str(r["message"] or ""), str(r["timestamp"] or "")) for r in rows]
        for it in items or []:
            if not isinstance(it, dict):
                continue
            total += 1
            if it.get("src"):
                filled += 1
                continue
            fb = bigrams(str(it.get("fact") or ""))
            if not fb:
                continue
            scored = []
            for text, ts in msgs:
                tb = bigrams(text)
                if not tb:
                    continue
                ratio = len(fb & tb) / max(1, len(fb))
                if ratio >= 0.3:
                    scored.append((ratio, text[:80], ts))
            scored.sort(key=lambda x: -x[0])
            if scored:
                it["src"] = [
                    {"s": str(it.get("subject") or "群友")[:20], "t": t, "ts": ts}
                    for _r, t, ts in scored[:3]
                ]
                filled += 1
    conn.close()

    LM.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"备份: {bak.name}")
    print(f"回填 {filled}/{total} 条记忆的来源消息")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
