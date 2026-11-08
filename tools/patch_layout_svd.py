import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\jobs\starfield_layout.py")
s = p.read_text(encoding="utf-8")

if "_svd_coords" in s:
    print("已有 SVD 分支")
    raise SystemExit

helper = '''

def _semantic_vectors(nodes: list[dict]) -> dict[str, list[float]]:
    """从 memory/vec_index.json 取每条记忆的 bge-m3 向量（按文本 sha1 匹配）。"""
    idx = (_read_json(ROOT / "memory" / "vec_index.json").get("items") or {})
    out: dict[str, list[float]] = {}
    for n in nodes:
        if n.get("kind") != "memory":
            continue
        text = str(n.get("summary") or "")[:300]
        key = hashlib.sha1(text.encode("utf-8")).hexdigest()[:20]
        v = idx.get(key)
        if v:
            out[n["id"]] = v
    return out


def _svd_coords(nodes: list[dict], vectors: dict[str, list[float]]) -> dict[str, dict] | None:
    """没有 umap 时的语义降维：对 bge-m3 向量做 SVD，取前 3 主成分投影到球面。

    比纯哈希球面投影强得多：语义相近的记忆会落在相近方向。
    """
    try:
        import numpy as np
    except Exception:  # noqa: BLE001
        return None
    ids = [n["id"] for n in nodes if n["id"] in vectors]
    if len(ids) < 6:
        return None
    X = np.asarray([vectors[i] for i in ids], dtype="float32")
    X = X - X.mean(axis=0, keepdims=True)
    try:
        U, Sv, _Vt = np.linalg.svd(X, full_matrices=False)
    except Exception:  # noqa: BLE001
        return None
    k = min(3, U.shape[1])
    emb = U[:, :k] * Sv[:k]
    if emb.shape[1] < 3:
        emb = np.pad(emb, ((0, 0), (0, 3 - emb.shape[1])))
    norms = np.linalg.norm(emb, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    unit = emb / norms
    by_id = {n["id"]: n for n in nodes}
    coords: dict[str, dict] = {}
    for i, nid in enumerate(ids):
        n = by_id[nid]
        r = 0.55 + 0.45 * (1 - float(n.get("importance") or 0))
        coords[nid] = {"x": round(float(unit[i][0]) * r, 5),
                       "y": round(float(unit[i][1]) * r, 5),
                       "z": round(float(unit[i][2]) * r, 5)}
    return coords

'''
anchor = "def _umap_coords("
i = s.find(anchor)
if i < 0:
    raise SystemExit("_umap_coords 未找到")
s = s[:i] + helper.lstrip("\n") + "\n" + s[i:]

# rebuild：UMAP → SVD（语义）→ 确定性，逐级降级
old = '''    coords = _umap_coords(nodes, vectors) if use_umap else None
    method = "umap" if coords else "deterministic"
    if not coords:
        coords = _deterministic(nodes)'''
new = '''    coords = _umap_coords(nodes, vectors) if use_umap else None
    method = "umap" if coords else ""
    if not coords:
        # 没有 umap 就用 bge-m3 向量做 SVD 语义投影（比哈希球面好得多）
        sem = _semantic_vectors(nodes)
        coords = _svd_coords(nodes, sem) if sem else None
        method = "svd-semantic" if coords else ""
    if not coords:
        coords = _deterministic(nodes)
        method = "deterministic"'''
if old in s:
    s = s.replace(old, new, 1)
    p.write_text(s, encoding="utf-8")
    print("已加 SVD 语义降维分支（umap → svd → deterministic）")
else:
    print("rebuild 分支没匹配")
