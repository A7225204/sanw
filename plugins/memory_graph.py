"""记忆图谱：沿关联边扩散检索（HippoRAG 的做法）。

来源：HippoRAG（Gutiérrez et al., NeurIPS 2024）。
海马体索引理论 —— LLM + 知识图谱 + Personalized PageRank。
论文实测：多跳问答提升 20%，比迭代检索便宜 10-30 倍、快 6-13 倍。

**我们的短板**：现在是「点对点」检索 ——
查「钱」只能找到字面/语义含「钱」的，找不到「欠款」「债主」「200元」这些关联的。

**这个模块给记忆之间建边，检索时沿边扩散一跳。**

边怎么建（不用 LLM 抽实体，零成本）：
    cos(向量_a, 向量_b) >= 0.72   就用现有 bge-m3 向量算
    278 条两两比 = 38k 次余弦，纯 CPU，毫秒级

扩散怎么算：
    seeds = 现有打分的前 k 条
    for s in seeds:
        for n in edges[s]:
            score[n] += 扩散系数 × score[s] × (cos(s,n) − 0.5) × 2

只扩一跳。两跳以上噪声太大、也贵。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "memory" / "memory_graph.json"

EDGE_COS = 0.72       # 建边阈值（和 memory_buffer 的向量阈值一致）
MAX_DEGREE = 8        # 每个节点最多连几个（防枢纽节点）
SPREAD = 0.40         # 扩散系数：邻居能拿到种子分的多少
MAX_SPREAD_K = 5      # 只用前 k 个种子做扩散


_BUILDING = False
_BUILD_LOCK = None


def _start_bg_build() -> bool:
    """起一个后台线程建图。已经在建就跳过。"""
    global _BUILDING, _BUILD_LOCK
    import threading
    if _BUILD_LOCK is None:
        _BUILD_LOCK = threading.Lock()
    with _BUILD_LOCK:
        if _BUILDING:
            return False
        _BUILDING = True

    def _run() -> None:
        global _BUILDING
        try:
            build(force=True)
        except Exception:  # noqa: BLE001
            pass
        finally:
            with _BUILD_LOCK:
                _BUILDING = False

    threading.Thread(target=_run, daemon=True, name="memory-graph-build").start()
    return True


def _load() -> dict:
    try:
        d = json.loads(CACHE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save(d: dict) -> None:
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def build(force: bool = False) -> dict:
    """给当前档案建一次边。返回 {id: [邻居id,...]}。

    只在档案有变化或 force 时重建 —— 全量算一次 ~几十毫秒，但调用点在热路径上，
    所以带缓存（按档案条数 + 最新 lu 判断是否变化）。
    """
    try:
        from plugins import long_memory as lm
        import memory_vec as mv
    except Exception:  # noqa: BLE001
        return {}

    items: list[tuple[str, str]] = []
    sig_parts: list[str] = []
    for scope, arr in lm._archive.items():
        for it in arr:
            if not isinstance(it, dict):
                continue
            f = str(it.get("fact") or "").strip()
            if len(f) < 4:
                continue
            iid = f"{scope}#{it.get('id')}"
            items.append((iid, f))
            sig_parts.append(f"{iid}:{it.get('lu') or 0}")
    sig = str(len(items)) + "|" + str(hash("".join(sig_parts)))

    cached = _load()
    if not force and cached.get("sig") == sig and cached.get("edges"):
        return cached["edges"]

    if not mv.available() or len(items) < 2:
        return {}

    # 2026-11-07：**建图不能阻塞检索**。
    # 实测 280 条全量建要 12 秒 —— 放在检索热路径上，重启后第一次有人问话就会卡 12 秒。
    # 改成：缓存没命中就先返回旧边（或空），同时起一个后台线程慢慢建。
    if not force:
        if _start_bg_build():
            pass
        return cached.get("edges") or {}

    # 分批向量化（memory_vec 单批 16 条）
    vecs: list[list[float]] = []
    for i in range(0, len(items), 16):
        batch = [f for _iid, f in items[i:i + 16]]
        vs = mv.embed(batch)
        if len(vs) != len(batch):
            return {}
        vecs.extend(vs)

    edges: dict[str, list[str]] = {}
    n = len(items)
    for a in range(n):
        best: list[tuple[float, str]] = []
        for b in range(n):
            if a == b:
                continue
            s = mv._cos(vecs[a], vecs[b])
            if s >= EDGE_COS:
                best.append((s, items[b][0]))
        best.sort(reverse=True)
        if best:
            edges[items[a][0]] = [x[1] for x in best[:MAX_DEGREE]]

    _save({"sig": sig, "built_at": time.time(), "edges": edges,
           "nodes": len(items),
           "avg_degree": round(sum(len(v) for v in edges.values()) / max(1, len(edges)), 2)})
    return edges


def spread(base: dict[str, float]) -> dict[str, float]:
    """在一轮打分的基础上做一次扩散。base = {item_id: 分}。

    返回**增量**（只含被扩散到、且原本没分的或分更低的邻居），
    调用方自己决定怎么并进去。
    """
    if not base:
        return {}
    edges = build()
    if not edges:
        return {}
    seeds = sorted(base.items(), key=lambda kv: -kv[1])[:MAX_SPREAD_K]
    add: dict[str, float] = {}
    for sid, sscore in seeds:
        for nid in edges.get(sid, [])[:MAX_DEGREE]:
            # 邻居原本已经有分了，就不再加（避免把本来相关的越推越高）
            if base.get(nid):
                continue
            add[nid] = max(add.get(nid, 0.0), SPREAD * sscore)
    return add


def stats() -> dict:
    d = _load()
    return {
        "nodes": d.get("nodes", 0),
        "edges_total": sum(len(v) for v in (d.get("edges") or {}).values()),
        "avg_degree": d.get("avg_degree", 0),
        "built_at": d.get("built_at", 0),
    }


def line() -> str:
    s = stats()
    if not s["nodes"]:
        return "记忆图谱：还没建"
    return f"记忆图谱：{s['nodes']} 节点 / {s['edges_total']} 条边 / 平均 {s['avg_degree']} 度"
