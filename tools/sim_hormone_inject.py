"""终极测试：注入两条记忆，验证「内分泌 → 记忆权重」是否真的联动。

1. 主人发 100 块红包  → DA 飙升 → 记一条高多巴胺记忆（衰减慢一点）
2. 主人骂「太笨了」    → CORT 飙升 → 记一条创伤记忆（is_traumatic，几乎不衰减）

跑完打印：激素前后值、两条记忆的 180 天保留率。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, r"D:\qq-ai-bot")

from plugins import hormones
from plugins import long_memory as lm

SCOPE = "private_100000000"


def snap() -> dict:
    v = hormones.values()
    return {k: round(float(v.get(k) or 0) / 100.0, 2) for k in ("DA", "CORT", "SER", "OXY", "MEL")}


def add_memory(fact: str, subject: str, hs: dict, traumatic: bool = False) -> int:
    lm._load()
    items = lm._archive.setdefault(SCOPE, [])
    nid = max([int(i.get("id") or 0) for i in items] + [0]) + 1
    items.append({
        "id": nid, "subject": subject, "fact": fact,
        "time": time.strftime("%m-%d"), "w": lm.MEM_WEIGHT_START,
        "h": 0, "lu": time.time(), "src": [],
        "hormones": hs,
        **({"is_traumatic": True} if traumatic else {}),
    })
    lm._save()
    return nid


def retention_at(it: dict, days: float) -> float:
    now = lm._now_ts()
    it2 = dict(it)
    it2["lu"] = now - days * 86400.0
    return lm._item_rank(it2, now=now) / lm.MEM_WEIGHT_START


def main() -> None:
    before = snap()
    print("起始激素：", before)

    # ── 1. 红包 ────────────────────────────────────────────────
    hormones.apply_event("praise", 3.0)
    h1 = snap()
    print(f"\n[红包] DA {before['DA']*100:.0f} → {h1['DA']*100:.0f}  CORT {before['CORT']*100:.0f} → {h1['CORT']*100:.0f}")
    nid1 = add_memory("主人给本鱼发了 100 块红包", "主人", h1, traumatic=False)
    print(f"  写入记忆 #{nid1}（快照 DA={h1['DA']} CORT={h1['CORT']}）")

    # ── 2. 批评 ────────────────────────────────────────────────
    hormones.apply_event("insult", 3.0)
    h2 = snap()
    print(f"\n[批评] DA {h1['DA']*100:.0f} → {h2['DA']*100:.0f}  CORT {h1['CORT']*100:.0f} → {h2['CORT']*100:.0f}")
    nid2 = add_memory("主人严厉批评本鱼，说本鱼太笨了", "主人", h2, traumatic=True)
    print(f"  写入记忆 #{nid2}（快照 DA={h2['DA']} CORT={h2['CORT']}，标记创伤）")

    # ── 3. 保留率对比 ──────────────────────────────────────────
    lm._load()
    rows = {i["id"]: i for i in lm._archive.get(SCOPE, []) if isinstance(i, dict)}
    print("\n180 天后保留率：")
    for nid, label in ((nid1, "红包记忆"), (nid2, "批评记忆")):
        it = rows.get(nid)
        if not it:
            continue
        r = retention_at(it, 180)
        print(f"  {label} #{nid}: {r * 100:.1f}%  (is_traumatic={bool(it.get('is_traumatic'))})")

    # ── 4. 创伤库也补一条，供星海/树图展示 ────────────────────
    tp = Path(r"D:\qq-ai-bot\memory\trauma.json")
    try:
        td = json.loads(tp.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        td = {"items": []}
    td.setdefault("items", []).append({
        "subject": "主人",
        "triggers": ["笨", "太笨"],
        "charge": 0.85,
        "since": time.strftime("%Y-%m-%d"),
        "hormones": h2,
    })
    tp.write_text(json.dumps(td, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n创伤库已追加一条（charge=0.85, 触发词=笨）")


if __name__ == "__main__":
    main()
