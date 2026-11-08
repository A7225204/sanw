"""本地别名 / 同义表：让「打拳」「练拳」「功夫」这类换说法也能命中同一条记忆。

设计约束
--------
1. **纯字符串表**：不调模型、不花 token、不依赖 embedding。
2. **可手工维护**：`memory/alias_map.json` 存在就用它（每行一组等价词）；不存在用内置默认表。
3. **只做"够具体"的扩展**：组内词长 ≥2，避免把「记忆」「东西」这种泛词也扩进去造成误命中。

被谁用
------
- 根目录 `long_memory.py`：fuzzy 回忆时的关键词匹配（问"打拳那次"能查到当年写的是"功夫"）。
- `plugins/long_memory.py`：档案访问强化时的匹配（提到同义词也算用到这条记忆）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FILE = ROOT / "memory" / "alias_map.json"

# 内置默认组：第一个是标准词，其余是等价说法。刻意只收"具体"的词。
DEFAULT_GROUPS: list[list[str]] = [
    ["打拳", "拳击", "练拳", "搏击", "功夫", "出拳"],
    ["3dmax", "3d max", "3d", "cr渲染器", "tyflow", "渲染"],
    ["白饭", "米饭", "干饭", "吃饭", "白米饭"],
    ["摸鱼", "偷懒", "摆烂", "划水"],
    ["桌宠", "小肥鱼", "桌面分身", "宠物"],
    ["抖音", "短视频", "douyin"],
    ["qq空间", "空间", "说说", "qzone"],
    ["缓存", "命中率", "cache", "令牌"],
    ["鲸鱼", "鱼娘", "本鱼", "大肥鱼", "鲸鱼娘"],
    ["唱歌", "唱歌功能", "唱歌服务", "单曲"],
    ["识图", "看图", "图片识别", "vision"],
    ["记忆", "档案", "长期记忆", "记住"],
]

_CACHE: list[list[str]] | None = None
_INDEX: dict[str, list[str]] | None = None


def _norm(s: str) -> str:
    return re.sub(r"[\s，。！？、：；,.!?;:\-—…·\"'“”‘’（）()【】\[\]~～*#@]+", "", str(s or "")).lower()


def groups() -> list[list[str]]:
    """读别名分组：优先 memory/alias_map.json（格式 {"groups": [[...], ...]}），否则用默认。"""
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    data = None
    try:
        if FILE.exists():
            raw = json.loads(FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = raw.get("groups")
            elif isinstance(raw, list):
                data = raw
    except Exception:  # noqa: BLE001
        data = None
    out: list[list[str]] = []
    for grp in data or DEFAULT_GROUPS:
        if not isinstance(grp, list):
            continue
        terms = [_norm(x) for x in grp if isinstance(x, str) and len(_norm(x)) >= 2]
        if len(terms) >= 2:
            out.append(terms)
    _CACHE = out
    return out


def _index() -> dict[str, list[str]]:
    global _INDEX
    if _INDEX is not None:
        return _INDEX
    idx: dict[str, list[str]] = {}
    for grp in groups():
        for t in grp:
            idx[t] = grp
    _INDEX = idx
    return idx


def reload() -> None:
    """热重载（改了 alias_map.json 后调用，不用重启）。"""
    global _CACHE, _INDEX
    _CACHE = None
    _INDEX = None


_WORD_RE = re.compile(r"[A-Za-z0-9]{2,12}|[\u4e00-\u9fff]{2,6}")

# 太泛的词：命中它们不能说明"聊的是同一件事"，打分时排除
_BROAD = {"记忆", "档案", "记住", "东西", "说说", "空间"}


def keywords(text: str, limit: int = 6) -> list[str]:
    """粗抽候选词（中文按连续片段）。注意中文无空格，长片段不一定是词，
    真正判断"提到了哪个别名"请用 terms_in()/match_score()。"""
    return [_norm(w) for w in _WORD_RE.findall(str(text or ""))][:limit]


def terms_in(text: str, min_len: int = 2) -> list[str]:
    """扫描 text，返回其中出现的别名表词（含标准词与同义词）。

    宁可多扫几十个词条（毫秒级），也不去做中文分词——避免"你还记得打拳"这种
    整段被当成一个词、真正的词反而漏掉。
    """
    t = _norm(text)
    if not t:
        return []
    out: list[str] = []
    for term in _index():
        if len(term) >= min_len and term in t:
            out.append(term)
    return out


def expand_from_text(text: str, min_len: int = 2) -> set[str]:
    """从一句话里找出别名词并扩展成整组同义词（含原词）。"""
    hits = terms_in(text, min_len)
    idx = _index()
    out: set[str] = set(hits)
    for h in hits:
        out.update(idx.get(h, []))
    return out


def expand(words: list[str]) -> set[str]:
    """把候选词扩成同义词集合（双向：组内任一出现，整组都算）。"""
    out: set[str] = set()
    idx = _index()
    for w in words:
        w = _norm(w)
        if len(w) < 2:
            continue
        out.add(w)
        grp = idx.get(w)
        if grp:
            out.update(grp)
    return out


def match_score(query: str, text: str, min_len: int = 2) -> int:
    """query 与 text 的"命中强度"：命中的同义词/原词里最长长度，没命中返回 0。

    「你说打拳」对「那天聊到功夫和练拳」应得 2（功夫），而不是 0。
    近似泛词（见 _BROAD）不参与打分，避免「记忆」「说说」把一堆不相干的事点亮。
    """
    q = _norm(query)
    t = _norm(text)
    if not q or not t:
        return 0
    best = 0
    for w in expand_from_text(q, min_len=2):
        if w in _BROAD or len(w) < min_len:
            continue
        if w in t and len(w) > best:
            best = len(w)
    return best
