"""给表情包补 tags（不用视觉，从已有图意描述里抽词）。

数据来源：memory/sticker_index.json（path -> 一句图意描述）
输出：memory/sticker_tags.json（path -> {tags:[...], mood:情绪文件夹}）
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "memory" / "sticker_index.json"
OUT = ROOT / "memory" / "sticker_tags.json"

STOP = {"这个", "一个", "表情", "图片", "意思", "表达", "感觉", "画面", "角色",
        "似乎", "可以", "什么", "怎么", "就是", "还有", "以及", "但是", "然后"}


def _terms(desc: str) -> list[str]:
    parts = re.split(r"[，。、；;,.!?！？\s]+", desc or "")
    out: list[str] = []
    for p in parts:
        p = p.strip("（）()【】[]\"'“”‘’")
        if not p or p in STOP:
            continue
        if 2 <= len(p) <= 6:
            out.append(p)
    # 去重保序
    seen, uniq = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq[:5]


def main() -> None:
    idx = json.loads(INDEX.read_text(encoding="utf-8-sig")) if INDEX.exists() else {}
    old = {}
    if OUT.exists():
        try:
            old = json.loads(OUT.read_text(encoding="utf-8-sig"))
        except Exception:  # noqa: BLE001
            old = {}
    n = 0
    for path, desc in idx.items():
        p = Path(path)
        mood = p.parent.name if p.parent and p.parent.name in (
            "开心", "卖萌", "得意", "无语", "委屈", "生气", "震惊") else ""
        tags = ([mood] if mood else []) + _terms(desc)
        if len(tags) < 2 and not mood:
            continue
        keep = (old.get(path) or {}).get("tags") if isinstance(old.get(path), dict) else None
        old[path] = {"tags": tags, "mood": mood, "auto": True,
                     "desc": (desc or "")[:60], **({"tags": keep} if keep else {})}
        n += 1
    OUT.write_text(json.dumps(old, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"写入 {n} 条标签 -> {OUT}")


if __name__ == "__main__":
    main()
