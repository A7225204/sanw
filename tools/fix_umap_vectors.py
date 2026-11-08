import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\jobs\starfield_layout.py")
s = p.read_text(encoding="utf-8")

# 1) _umap_coords 改成吃 {node_id: vec}
s = s.replace('''    ids, mats = [], []
    for n in nodes:
        if n.get("kind") != "memory":
            continue
        v = vectors.get(str(n.get("summary") or "")[:300])
        if not v:
            continue
        ids.append(n["id"])
        mats.append(v)''',
'''    ids, mats = [], []
    for n in nodes:
        if n.get("kind") != "memory":
            continue
        v = vectors.get(n["id"])
        if not v:
            continue
        ids.append(n["id"])
        mats.append(v)''')

# 2) rebuild 里改成用 _semantic_vectors（按 id 取向量）
old = '''    nodes, _ = mv.build_nodes(scope)
    vectors: dict = {}
    if use_umap:
        idx = (_read_json(ROOT / "memory" / "vec_index.json").get("items") or {})
        # vec_index 是 sha1(text)->向量，这里按文本 hash 查
        for n in nodes:
            if n.get("kind") != "memory":
                continue
            key = hashlib.sha1(str(n.get("summary") or "")[:300].encode("utf-8")).hexdigest()[:20]
            if key in idx:
                vectors[str(n.get("summary") or "")[:300]] = idx[key]
    coords = _umap_coords(nodes, vectors) if use_umap else None
    method = "umap" if coords else ""
    if not coords:
        # 没有 umap 就用 bge-m3 向量做 SVD 语义投影（比哈希球面好得多）
        sem = _semantic_vectors(nodes)
        coords = _svd_coords(nodes, sem) if sem else None
        method = "svd-semantic" if coords else ""'''
new = '''    nodes, _ = mv.build_nodes(scope)
    sem = _semantic_vectors(nodes)
    coords = _umap_coords(nodes, sem) if (use_umap and sem) else None
    method = "umap" if coords else ""
    if not coords:
        # 没有 umap 就用 bge-m3 向量做 SVD 语义投影（比哈希球面好得多）
        coords = _svd_coords(nodes, sem) if sem else None
        method = "svd-semantic" if coords else ""'''
if old in s:
    s = s.replace(old, new, 1)
    p.write_text(s, encoding="utf-8")
    print("已修 UMAP 向量来源（按 node id）")
else:
    print("rebuild 段没匹配")
