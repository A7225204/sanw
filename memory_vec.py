"""语义召回：用 BGE-M3 把「长期记忆档案」按意思检索，补上关键词/别名够不着的地方。

为什么需要
----------
现有召回靠 alias_map（打拳 → 拳/功夫/泰拳）+ 2-gram 重合。它解决"同义词"，
解决不了"意思相近但用词完全不同"：
    问「他最近在练啥」  档案写「周末去拳馆练泰拳」 → 现在捞不到
    问「上次说的那个肉」档案写「红烧肉的学费一直欠着」→ 现在捞不到

做法
----
- 档案向量**预先算好**存本地（memory/vec_index.json），只在档案变化时补算，不重复花钱；
- 每轮只 embed 用户这一句（bge-m3，约 0.3 秒）；
- 候选合并：关键词排在前 N 的 ∪ 向量相似度高的，再按分数排；
- 候选多于 8 条时可用 bge-reranker-v2-m3 精排（默认开，可用 MEM_VEC_RERANK=0 关）；
- 任何一步失败/超时都返回原顺序，聊天不受影响。

成本（实测）：bge-m3 1024 维、0.28 秒；reranker 0.18 秒；都走硅基赠送额度。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INDEX_FILE = ROOT / "memory" / "vec_index.json"
EMBED_MODEL = os.getenv("MEM_VEC_MODEL", "Pro/BAAI/bge-m3")
RERANK_MODEL = os.getenv("MEM_VEC_RERANK_MODEL", "Pro/BAAI/bge-reranker-v2-m3")
RERANK_MAX_DOCS = int(os.getenv("MEM_VEC_RERANK_MAX", "8") or "8")
ENABLED = os.getenv("MEM_VEC_ENABLED", "1").lower() in ("1", "true", "yes")
RERANK_ON = os.getenv("MEM_VEC_RERANK", "1").lower() in ("1", "true", "yes")
TIMEOUT = float(os.getenv("MEM_VEC_TIMEOUT", "3") or "3")
MIN_SIM = float(os.getenv("MEM_VEC_MIN_SIM", "0.32") or "0.32")
MAX_BUILD_PER_TURN = int(os.getenv("MEM_VEC_MAX_BUILD", "24") or "24")

_INDEX: dict | None = None
_Q_CACHE: dict[str, tuple[float, list[float]]] = {}
_R_CACHE: dict[str, tuple[float, list[int]]] = {}
_CACHE_TTL = 10.0


def _cache_get(store: dict, key: str):
    hit = store.get(key)
    if not hit:
        return None
    ts, val = hit
    if time.time() - ts > _CACHE_TTL:
        store.pop(key, None)
        return None
    return val


def _cache_put(store: dict, key: str, val) -> None:
    if len(store) > 64:
        store.clear()
    store[key] = (time.time(), val)


def _env(key: str) -> str:
    v = (os.getenv(key) or "").strip()
    if v:
        return v
    try:
        for line in (ROOT / ".env").read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, val = line.split("=", 1)
                if k.strip() == key:
                    return val.strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def _cfg() -> tuple[str, str]:
    base = (_env("SILICONFLOW_BASE_URL") or "https://api.siliconflow.cn/v1").rstrip("/")
    return base, _env("SILICONFLOW_API_KEY")


def available() -> bool:
    if not ENABLED:
        return False
    _, key = _cfg()
    return bool(key)


def _post(path: str, payload: dict, timeout: float | None = None) -> dict | None:
    base, key = _cfg()
    if not key:
        return None
    import urllib.error
    import urllib.request
    req = urllib.request.Request(
        base + path, data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout or TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8", "ignore"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


LOCAL_EMBED_URL = os.getenv("LOCAL_EMBED_URL", "http://127.0.0.1:7801/embed")


def _local_embed(texts: list[str]) -> list[list[float]]:
    """本地 bge-m3（tools/embed_server.py，免费离线）。失败返回空。"""
    import urllib.request
    try:
        req = urllib.request.Request(
            LOCAL_EMBED_URL,
            data=json.dumps({"texts": [str(t)[:400] for t in texts]}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode("utf-8"))
        v = d.get("vectors") or []
        return [list(map(float, x)) for x in v] if len(v) == len(texts) else []
    except Exception:  # noqa: BLE001
        return []


def embed(texts: list[str]) -> list[list[float]]:
    """批量向量化（最多 16 条一批）。

    2026-10-31：优先本地 bge-m3。之前只走 SiliconFlow，账户 402 余额不足时
    embed() 一直返回空 → 整个语义召回静默失效（代码在、从没生效过）。
    """
    texts = [str(t)[:600] for t in texts if str(t).strip()]
    if not texts:
        return []
    _local = _local_embed(texts)
    if _local:
        return _local
    out: list[list[float]] = []
    for i in range(0, len(texts), 16):
        chunk = texts[i:i + 16]
        data = _post("/embeddings", {"model": EMBED_MODEL, "input": chunk})
        if not data:
            return []
        try:
            for item in data.get("data") or []:
                out.append([float(x) for x in item["embedding"]])
        except (KeyError, TypeError, ValueError):
            return []
    return out


def _load_index() -> dict:
    global _INDEX
    if _INDEX is None:
        try:
            data = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
            _INDEX = data if isinstance(data, dict) else {"items": {}}
        except Exception:  # noqa: BLE001
            _INDEX = {"items": {}}
        _INDEX.setdefault("items", {})
    return _INDEX


def _save_index() -> None:
    try:
        INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
        INDEX_FILE.write_text(json.dumps(_load_index(), ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _key(text: str) -> str:
    return hashlib.sha1(str(text).encode("utf-8")).hexdigest()[:20]


def _item_text(it: dict) -> str:
    subj = str(it.get("subject") or "").strip()
    fact = str(it.get("fact") or "").strip()
    return (f"{subj}：{fact}" if subj else fact)[:300]


def sync(items: list[dict]) -> int:
    """确保这批档案有向量；返回本轮补算的条数。只补缺的，不算已有的。"""
    idx = _load_index()["items"]
    todo = []
    for it in items:
        text = _item_text(it)
        if not text:
            continue
        k = _key(text)
        if k not in idx:
            todo.append((k, text))
    if not todo:
        return 0
    todo = todo[:MAX_BUILD_PER_TURN]  # 别在一轮里算太多，超出的下轮补
    vecs = embed([t for _, t in todo])
    if not vecs or len(vecs) != len(todo):
        return 0
    for (k, _), v in zip(todo, vecs):
        idx[k] = v
    _save_index()
    return len(todo)


def _cos(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / (na * nb)


def rerank(query: str, docs: list[str], top_n: int = 6) -> list[int]:
    """用 bge-reranker 精排，返回按相关度排序的下标。失败返回空列表。"""
    if not RERANK_ON or len(docs) < 2:
        return []
    ck = str(hash((query[:120], tuple(str(d)[:60] for d in docs))))
    cached = _cache_get(_R_CACHE, ck)
    if cached is not None:
        return cached
    data = _post("/rerank", {"model": RERANK_MODEL, "query": str(query)[:300],
                             "documents": [str(d)[:400] for d in docs]})
    if not data:
        return []
    rows = data.get("results") or []
    try:
        out = [int(r["index"]) for r in sorted(rows, key=lambda r: -float(r.get("relevance_score") or 0))][:top_n]
        _cache_put(_R_CACHE, ck, out)
        return out
    except (KeyError, TypeError, ValueError):
        return []


# 只在"像在问记忆"时才把用户这句话发给 embedding 服务（减少外发频率 + 省延迟）
_GATE_CUES = (
    "记得", "还记得", "记不记得", "上次", "上回", "之前", "以前", "那次", "那天",
    "我说过", "你说过", "答应", "约定", "说好", "啥时候", "什么时候", "几点",
    "叫什么", "名字", "外号", "喜欢", "讨厌", "是不是", "有没有", "哪个", "那个",
    "叫什么来着", "忘了", "查一下", "想起来",
)
GATE_ON = os.getenv("MEM_VEC_GATE", "1").lower() in ("1", "true", "yes")


def should_use(query: str) -> bool:
    """这句值不值得做语义检索（同时决定要不要外发）。

    规则：带问号，或命中"记忆类信号词"，且长度 ≥ 6。
    普通寒暄/指令/"在吗"这类直接走关键词，不外发、不多等 0.4 秒。
    """
    if not GATE_ON:
        return True
    t = str(query or "").strip()
    if len(t) < 6:
        return False
    if "?" in t or "？" in t:
        return True
    # 2026-11-07：中文疑问语气也要算。
    # 事故实测：「主人钱还够吗」被判定不值得向量检索（没问号、也没命中信号词），
    # 结果「主人身上仅剩200元撑到15号」这条明明余弦 0.729 的记忆根本没机会被捞到，
    # 检索出来全是 P 站图片相关。中文问句很多时候不带问号。
    if re.search(r"(吗|呢|么|啥|咋|几|多少|哪|谁|为什么|怎么|是不是|有没有|能不能|够不够)", t):
        return True
    return any(c in t for c in _GATE_CUES)


def hybrid_order(scope: str, query: str, ordered: list[dict], top_n: int = 8) -> list[dict]:
    """在"关键词排序"基础上，用语义相似度再捞一遍，合并成新顺序。

    - 关键词排前的几条保底保留（精确命中不能丢）；
    - 向量相似度 ≥ MIN_SIM 的补进来；
    - 候选 > 8 且开了重排，就用 reranker 定最终顺序。
    全流程任何失败都原样返回 ordered。
    """
    if not available() or not query or not ordered:
        return ordered
    try:
        q = _cache_get(_Q_CACHE, query)
        if q is None:
            qv = embed([query])
            if not qv:
                return ordered
            q = qv[0]
            _cache_put(_Q_CACHE, query, q)
        sync(ordered)
        idx = _load_index()["items"]
        scored: list[tuple[float, dict]] = []
        for it in ordered:
            v = idx.get(_key(_item_text(it)))
            if v:
                scored.append((_cos(q, v), it))
        if not scored:
            return ordered
        scored.sort(key=lambda x: -x[0])

        picked: list[dict] = []
        seen: set[int] = set()

        def _add(it):
            iid = id(it)
            if iid not in seen:
                seen.add(iid)
                picked.append(it)

        for it in ordered[:4]:          # 关键词保底
            _add(it)
        for sim, it in scored[:6]:      # 语义补充
            if sim >= MIN_SIM:
                _add(it)
        for it in ordered:              # 其余按原顺序补满
            if len(picked) >= max(top_n, 8):
                break
            _add(it)

        if RERANK_ON and len(picked) > 8:
            head = picked[:8]
            order = rerank(query, [_item_text(x) for x in head], top_n=6)
            if order:
                head = [head[i] for i in order]
            return head + picked[8:]
        return picked
    except Exception:  # noqa: BLE001
        return ordered


def stats() -> dict:
    idx = _load_index()["items"]
    return {"enabled": available(), "vectors": len(idx), "model": EMBED_MODEL,
            "rerank": bool(RERANK_ON), "min_sim": MIN_SIM}
