"""潜意识缓冲：新记忆先进这里，反复出现才值得进长期档案。

来源：RecMem（华为云 + 港中文，arXiv 2605.16045）的**递归触发巩固**。

它的洞察：
> 现有记忆系统对每一条交互都调 LLM 提炼事实，token 成本极高。
> 但大部分交互根本不值得记 —— 只有**反复出现**的才该进长期记忆。

做法（认知科学里的 multi-store theory + 互补学习系统）：
    新交互 → 只做轻量编码，存进「潜意识层」（**不调 LLM**）
          ↓
    当它能找到 ≥θcount 条语义相似的旧交互（cos ≥ θsim）
          ↓
    才调 LLM 提炼成长期记忆

论文实测：token 成本降 87%，准确率还比 Mem0 / A-Mem / MemoryOS 都高。

**2026-11-07 第二次改：相似度判断改成「向量优先 + 2-gram 兜底」**

一开始只用 2-gram，因为"不调 LLM 就零成本"。实测同一件事的不同说法：

    「明天去医院复查」vs「明天陪主人去医院复查」
        2-gram 0.67   向量余弦 0.898
    「明天去医院复查」vs「后天得去趟医院」
        2-gram 0.30   向量余弦 0.841     ← 2-gram 几乎判不出来
    「明天去医院复查」vs「今天想吃红烧肉」
        2-gram 0.00   向量余弦 0.555

**2-gram 对"换说法"完全失效** —— 而那正是最容易重复出现的形式。

本地 bge-m3（127.0.0.1:7801）向量化不花钱，所以：
- `push` 时只存文本，零成本
- `recurring` 时才批量 embed（分批，每批 16 条）
- 向量服务不可用就退回 2-gram（阈值不同）
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILE = ROOT / "memory" / "subconscious.json"

# 两个阈值分开：向量余弦的基线偏高（无关也有 0.55），2-gram 天然低一档
SIM_VEC = 0.72       # 向量：同主题实测 0.84+，无关 0.555，取中间偏上
SIM_GRAM = 0.32      # 2-gram：同主题不同说法只有 0.3~0.67，只能低阈值凑
COUNT_THRESHOLD = 4
TTL_DAYS = 7
MAX_ITEMS = 600
EMBED_BATCH = 16     # memory_vec.embed 单批上限

_CLEAN_RE = re.compile(r"[\s，。！？、：；,.!?;:\-—…·\"'“”‘’（）()【】\[\]~～*#@]+")


def _bigrams(text: str) -> set[str]:
    t = _CLEAN_RE.sub("", str(text or ""))
    return {t[i:i + 2] for i in range(len(t) - 1)} if len(t) >= 2 else ({t} if t else set())


def _sim_gram(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / max(1, min(len(a), len(b)))


def _load() -> list[dict]:
    try:
        d = json.loads(FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, list) else []
    except Exception:  # noqa: BLE001
        return []


def _save(items: list[dict]) -> None:
    try:
        FILE.parent.mkdir(parents=True, exist_ok=True)
        FILE.write_text(json.dumps(items[-MAX_ITEMS:], ensure_ascii=False, indent=0),
                        encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _prune(items: list[dict]) -> list[dict]:
    cutoff = time.time() - TTL_DAYS * 86400
    return [x for x in items if float(x.get("at") or 0) >= cutoff]


def push(scope: str, text: str, kind: str = "") -> float:
    """往潜意识层记一条。返回它和现有内容的最大 2-gram 相似度（仅供参考）。

    **这里不做向量化** —— push 每次都调的话，写入路径会变慢。
    向量比对留给 recurring()，那里才批量算。
    """
    txt = str(text or "").strip()
    if len(txt) < 4:
        return 0.0
    items = _prune(_load())
    new_bg = _bigrams(txt)
    best = 0.0
    for it in items[-40:]:                 # 只跟最近 40 条比，别全量扫
        s = _sim_gram(new_bg, _bigrams(str(it.get("t") or "")))
        if s > best:
            best = s
    items.append({
        "at": time.time(),
        "scope": str(scope or ""),
        "t": txt[:120],
        "k": str(kind or ""),
        "g": round(best, 3),
    })
    _save(items)
    return best


def _embed_all(texts: list[str]) -> list[list[float]]:
    """批量向量化（分批，每批 16 条）。失败返回空列表。"""
    try:
        import memory_vec as mv
        if not mv.available():
            return []
    except Exception:  # noqa: BLE001
        return []
    out: list[list[float]] = []
    for i in range(0, len(texts), EMBED_BATCH):
        batch = texts[i:i + EMBED_BATCH]
        try:
            vs = mv.embed(batch) or []
        except Exception:  # noqa: BLE001
            return []
        if len(vs) != len(batch):
            return []                      # 有任何一批不齐，整轮退回 2-gram
        out.extend(vs)
    return out


def recurring(scope: str = "", min_count: int = COUNT_THRESHOLD) -> list[dict]:
    """找出「反复出现但还没升级」的话题簇。

    **向量优先**（能认出换说法），向量不可用才退回 2-gram。
    返回 [{text, count, scope, members, mode}]，按 count 降序。
    """
    items = _prune(_load())
    if scope:
        items = [x for x in items if str(x.get("scope")) == str(scope)]
    n = len(items)
    if n < min_count:
        return []

    texts = [str(x.get("t") or "") for x in items]
    vecs = _embed_all(texts)
    use_vec = len(vecs) == n
    thr = SIM_VEC if use_vec else SIM_GRAM
    mode = "vec" if use_vec else "gram"

    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    bgs = [_bigrams(t) for t in texts]
    try:
        import memory_vec as mv
    except Exception:  # noqa: BLE001
        mv = None

    for i in range(n):
        for j in range(i + 1, n):
            if use_vec and mv:
                s = mv._cos(vecs[i], vecs[j])
            else:
                s = _sim_gram(bgs[i], bgs[j])
            if s >= thr:
                union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    clusters: list[dict] = []
    for _root, members in groups.items():
        if len(members) < min_count:
            continue
        rep = max(members, key=lambda m: len(texts[m]))
        clusters.append({
            "text": texts[rep][:80],
            "count": len(members),
            "scope": str(items[rep].get("scope") or ""),
            "members": [texts[m][:60] for m in members[:5]],
            "mode": mode,
        })
    clusters.sort(key=lambda x: -x["count"])
    return clusters


def stats() -> dict:
    items = _prune(_load())
    return {
        "count": len(items),
        "scopes": len({str(x.get("scope")) for x in items}),
        "clusters": len(recurring()),
    }


def line() -> str:
    """给 prompt / 日志的一行：有几个话题在反复出现。空串表示没有。"""
    cs = recurring()
    if not cs:
        return ""
    top = cs[0]
    return f"有件事反复出现 {top['count']} 次：{top['text'][:30]}"
