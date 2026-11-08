"""表情包语义检索：把"图意描述"和回复正文都做向量，按相似度跨分类挑图。

为什么不用 2-gram
----------------
现有的 2-gram 重合只在"字面撞词"时生效：
    图意「委屈巴巴，寻求安慰或同情。」 回复「本鱼困了」→ 重合 0，退回随机
向量能抓住"意思相近"：困了 ≈ 委屈/疲惫 → 命中委屈或无语的图。

做法
----
- 101 张图的描述预先向量化，存 memory/sticker_vec.json（一次性，只有增删图才补算）
- 每条回复只 embed 一句（bge-m3，约 0.3 秒），跨 7 个情绪分类统一打分
- 相似度 ≥ STICKER_VEC_MIN_SIM 才用，否则交给原来的情绪兜底（不乱配）
- 任何一步失败/没 key 都返回 None，不影响原有逻辑
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX_FILE = ROOT / "memory" / "sticker_vec.json"
STICKER_INDEX = ROOT / "memory" / "sticker_index.json"

# 阈值偏保守：宁可不用，也别配错
MIN_SIM = float(os.getenv("STICKER_VEC_MIN_SIM", "0.55") or "0.55")
ENABLED = os.getenv("STICKER_VEC_ENABLED", "1").lower() in ("1", "true", "yes")

_INDEX: dict | None = None
_Q_CACHE: dict[str, tuple[float, list[float]]] = {}
_CACHE_TTL = 60.0


LOCAL_EMBED_URL = os.getenv("LOCAL_EMBED_URL", "http://127.0.0.1:7801/embed")
LOCAL_EMBED_TIMEOUT = float(os.getenv("LOCAL_EMBED_TIMEOUT", "3") or "3")


def _local_embed(texts: list[str]) -> list[list[float]]:
    """优先走本地 bge-m3 服务（免费、离线）；不通就返回空。"""
    import json as _json
    import urllib.request
    try:
        req = urllib.request.Request(
            LOCAL_EMBED_URL,
            data=_json.dumps({"texts": texts}, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=LOCAL_EMBED_TIMEOUT) as r:
            data = _json.loads(r.read().decode("utf-8", "ignore"))
        vecs = data.get("vectors") or []
        if vecs and len(vecs) == len(texts):
            return [[float(x) for x in v] for v in vecs]
    except Exception:  # noqa: BLE001
        pass
    return []


def _embed(texts: list[str]) -> list[list[float]]:
    """本地服务优先，失败回退硅基流动（若余额够）。"""
    if not texts:
        return []
    v = _local_embed(texts)
    if v:
        return v
    try:
        from memory_vec import embed as _remote
        return _remote(texts)
    except Exception:  # noqa: BLE001
        return []


def _available() -> bool:
    try:
        import urllib.request
        with urllib.request.urlopen(LOCAL_EMBED_URL.replace("/embed", "/health"), timeout=1.5) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        pass
    try:
        from memory_vec import available as _av
        return _av()
    except Exception:  # noqa: BLE001
        return False


def _key(text: str) -> str:
    return hashlib.sha1(str(text).encode("utf-8")).hexdigest()[:20]


def _load() -> dict:
    global _INDEX
    if _INDEX is None:
        try:
            data = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
            _INDEX = data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            _INDEX = {}
        _INDEX.setdefault("items", {})
    return _INDEX


def _save() -> None:
    try:
        INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
        INDEX_FILE.write_text(json.dumps(_load(), ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _cos(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / (na * nb)


def _descriptions() -> dict[str, str]:
    try:
        raw = json.loads(STICKER_INDEX.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for path, desc in raw.items():
        d = str(desc or "").strip()
        if d and Path(path).exists():
            # 分类名也是强信号，拼进描述里
            out[path] = f"{Path(path).parent.name}：{d}"
    return out


def build(force: bool = False) -> dict:
    """确保每张图都有向量。返回 {built, total, available}。"""
    if not _available():
        return {"built": 0, "total": 0, "available": False}
    descs = _descriptions()
    idx = _load()["items"]
    if force:
        idx.clear()
    todo = [(p, d) for p, d in descs.items() if _key(d) not in idx]
    built = 0
    for i in range(0, len(todo), 16):
        batch = todo[i:i + 16]
        vecs = _embed([d for _, d in batch])
        if not vecs or len(vecs) != len(batch):
            break
        for (path, desc), v in zip(batch, vecs):
            idx[_key(desc)] = {"path": path, "desc": desc, "vec": v}
            built += 1
    if built:
        _save()
    return {"built": built, "total": len(idx), "available": True}


def best(text: str, min_sim: float | None = None) -> tuple[str, float] | None:
    """给一句回复找最贴的表情包：返回 (路径, 相似度) 或 None。"""
    if not ENABLED:
        return None
    t = str(text or "").strip()
    if len(t) < 4:
        return None
    if not _available():
        return None
    idx = _load()["items"]
    if not idx:
        meta = build()
        if not meta.get("built"):
            return None
        idx = _load()["items"]
    # 查缓存
    hit = _Q_CACHE.get(t)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        q = hit[1]
    else:
        vecs = _embed([t[:200]])
        if not vecs:
            return None
        q = vecs[0]
        if len(_Q_CACHE) > 64:
            _Q_CACHE.clear()
        _Q_CACHE[t] = (time.time(), q)

    best_path, best_sim = None, 0.0
    for item in idx.values():
        sim = _cos(q, item.get("vec") or [])
        if sim > best_sim:
            best_path, best_sim = item.get("path"), sim
    if not best_path or not Path(best_path).exists():
        return None
    if best_sim < (min_sim if min_sim is not None else MIN_SIM):
        return None
    return best_path, best_sim


def stats() -> dict:
    idx = _load()["items"]
    return {"enabled": ENABLED, "vectors": len(idx), "min_sim": MIN_SIM,
            "available": _available(), "local_url": LOCAL_EMBED_URL}
