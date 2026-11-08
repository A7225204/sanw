"""一次性迁移：按历史互动把现有关系初始化到四个阶段里。

新表 relation_stage 是空的 —— 但主人和她已经聊了几天，
不该从头算成「初识」。这里按 chat_history 的互动条数 + affinity 分数回填一次。

规则跟 relation_stage.STAGES 保持一致，只是 depth 直接取"历史消息数"。
"""
import sys
import time

ROOT = r"D:\qq-ai-bot"
sys.path.insert(0, ROOT)

from memory_manager import _connect
from plugins import relation_stage as rs


def main() -> None:
    with _connect() as c:
        cur = c.cursor()
        cur.execute("""
            SELECT user_id, COUNT(*) AS n FROM chat_history
            WHERE user_id != '' AND user_id IS NOT NULL
            GROUP BY user_id ORDER BY n DESC
        """)
        rows = cur.fetchall()
    print(f"有聊天记录的用户：{len(rows)} 个")

    # 有没有过 repair（和好）记录
    repair_uids: set[str] = set()
    try:
        import json
        from pathlib import Path
        for f in (Path(ROOT) / "memory").glob("*.json"):
            try:
                t = f.read_text(encoding="utf-8")
            except Exception:
                continue
            if "repair" in t and "user_id" in t:
                d = json.loads(t)
                if isinstance(d, dict):
                    for k, v in d.items():
                        if isinstance(v, dict) and v.get("repair"):
                            repair_uids.add(str(k))
    except Exception:
        pass

    done = 0
    for uid, n in rows:
        uid = str(uid)
        try:
            from memory_manager import get_affinity
            aff = int(get_affinity(uid))
        except Exception:
            aff = 50
        stage = "初识"
        for name, min_depth, min_aff, need_repair in rs.STAGES:
            if int(n) < min_depth or aff < min_aff:
                continue
            if need_repair and uid not in repair_uids:
                continue
            stage = name
        warmth = min(100.0, 35.0 + min(60.0, int(n) * 0.6))
        with _connect() as c:
            cur = c.cursor()
            rs._ensure_table(cur)
            cur.execute(
                "INSERT OR REPLACE INTO relation_stage "
                "(user_id, stage, depth, warmth, repairs, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (uid, stage, int(n), warmth,
                 1 if uid in repair_uids else 0, time.time()))
            c.commit()
        if int(n) >= 20 or stage != "初识":
            print(f"  {uid}: {n} 条 → {stage}（好感 {aff}，温暖 {warmth:.0f}）")
        done += 1
    print(f"回填完成：{done} 个用户")


if __name__ == "__main__":
    main()
