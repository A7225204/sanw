"""表情包挑选打分：把"情绪先验 + 语义相似 + 标签命中 + 去重 + 黑名单"合成一个分。

借鉴 dsh-nekowhale-stickers 的做法（逐张标注 + 打分检索 + 去重 + 黑白名单），
只取骨架，不引入它的资源包；本模块不依赖 nonebot，可单独测。

数据文件（都可缺省）：
    memory/sticker_tags.json       {"<path>": {"tags": ["摸鱼","吃瓜"], "dir": "大肥鱼→GPT娘", "origin": "梗出处"}}
    memory/sticker_blacklist.json  {"tags": ["舌吻"], "paths": ["xxx.gif"]}
    memory/sticker_recent.json     ["<path>", ...]   最近发过的（新的在前）
"""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TAGS_FILE = ROOT / "memory" / "sticker_tags.json"
BLACK_FILE = ROOT / "memory" / "sticker_blacklist.json"
RECENT_FILE = ROOT / "memory" / "sticker_recent.json"

RECENT_KEEP = int(os.getenv("STICKER_RECENT_KEEP", "24") or "24")
W_EMOTION = float(os.getenv("STICKER_W_EMOTION", "0.18") or "0.18")
W_TAG = float(os.getenv("STICKER_W_TAG", "0.06") or "0.06")
W_DIR = float(os.getenv("STICKER_W_DIR", "0.04") or "0.04")


def _load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def tags_of(path: str | Path) -> dict:
    d = _load(TAGS_FILE, {})
    return d.get(str(path)) or {}


def blacklist() -> tuple[set, set]:
    d = _load(BLACK_FILE, {}) or {}
    tags = {str(x).strip() for x in (d.get("tags") or []) if str(x).strip()}
    paths = {str(x).strip().lower() for x in (d.get("paths") or []) if str(x).strip()}
    return tags, paths


def recent() -> list[str]:
    return [str(x) for x in (_load(RECENT_FILE, []) or [])]


def mark_used(path: str | Path) -> None:
    p = str(path)
    lst = [x for x in recent() if x != p]
    lst.insert(0, p)
    try:
        RECENT_FILE.write_text(json.dumps(lst[:RECENT_KEEP], ensure_ascii=False),
                               encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def is_blocked(path: str | Path) -> bool:
    tags, paths = blacklist()
    if str(path).lower() in paths:
        return True
    mine = set(tags_of(path).get("tags") or [])
    return bool(tags & mine)


def score(path: str | Path, sim: float, mood: str, text: str, mood_hit: bool) -> float:
    """sim: 语义相似度(0~1)；mood_hit: 是否落在当前情绪分类里。"""
    s = max(0.0, min(1.0, float(sim or 0.0)))
    if mood_hit:
        s += W_EMOTION
    info = tags_of(path)
    tags = [str(t) for t in (info.get("tags") or [])]
    if text and tags:
        hit = sum(1 for t in tags if t and t in text)
        s += min(3, hit) * W_TAG
    if info.get("dir"):
        s += 0.0  # 方向型（双人）默认不加分，留给调用方按场景决定
    return s


def rank(files, mood: str, text: str, sims: dict | None = None, mood_dir: str = "") -> list:
    """返回按分数从高到低排好、且已过滤黑名单/最近用过的候选。"""
    sims = sims or {}
    recent_set = set(recent())
    out = []
    for f in files:
        p = str(f)
        if is_blocked(p):
            continue
        md = str(mood_dir or "").replace("\\", "/").lower()
        mood_hit = bool(md) and md in p.replace("\\", "/").lower()
        s = score(p, sims.get(p, 0.0), mood, text, mood_hit)
        # 最近发过：降权而不是禁用（图库小时全禁会挑不出）
        if p in recent_set:
            s -= 0.35
        out.append((s, p))
    out.sort(key=lambda x: x[0], reverse=True)
    return out
