"""回填长期档案缺失的 kind / hormones / src 三个字段。

- kind      : 按 fact 内容推断（事件类词 → exp，其余 long）
- hormones  : 补当前内分泌快照（旧数据当年没存）
- src       : 用 _match_sources 从 chat_history 里模糊找回原始消息

必须停 bot 再跑，否则 bot 下次 _save 会把改动覆盖回去。
"""
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, r"D:\qq-ai-bot")

from plugins import long_memory as ltm   # noqa: E402

DB = Path.home() / "AppData" / "Roaming" / "dsh-pet-standalone-webm-chat" / "shared_memory.db"

EXP_WORDS = ("吵架", "被骂", "骂了", "红红包", "发了", "今天", "昨天", "刚才",
             "夸", "批评", "答应", "道歉", "吵", "怼", "生气", "哭", "笑死",
             "吃", "喝", "去了", "来了", "收到", "给了")


def infer_kind(fact: str) -> str:
    t = str(fact or "")
    # 明显的"事件感"词 → exp；否则 long
    if any(w in t for w in EXP_WORDS) and len(t) <= 30:
        return "exp"
    return "long"


def main() -> None:
    ltm._load()
    try:
        from plugins import hormones
        hs = {k: round(float(v) / 100.0, 2) for k, v in (hormones.values() or {}).items()}
    except Exception:
        hs = {}

    # 从 chat_history 拉最近消息，给 _match_sources 做匹配源
    pending = []
    try:
        con = sqlite3.connect(str(DB), timeout=10)
        rows = con.execute(
            "select speaker, message, timestamp from chat_history order by rowid desc limit 3000"
        ).fetchall()
        con.close()
        # _match_sources 期望 [[昵称, 文本, 时间戳], ...]
        pending = [[r[0], r[1], r[2]] for r in rows if r[1]]
    except Exception as e:  # noqa: BLE001
        print("读 chat_history 失败:", e)

    n_kind = n_hor = n_src = 0
    for scope, items in (ltm._archive or {}).items():
        for it in items or []:
            fact = str(it.get("fact") or "")
            if not fact:
                continue
            if not it.get("kind"):
                it["kind"] = infer_kind(fact)
                n_kind += 1
            if not it.get("hormones") and hs:
                it["hormones"] = dict(hs)
                n_hor += 1
            if not it.get("src") and pending:
                try:
                    src = ltm._match_sources(fact, pending)
                    if src:
                        it["src"] = src
                        n_src += 1
                except Exception:
                    pass
    ltm._save()
    print(f"回填完成：kind +{n_kind} / hormones +{n_hor} / src +{n_src}")


if __name__ == "__main__":
    main()
