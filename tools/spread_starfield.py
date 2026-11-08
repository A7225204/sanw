import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\jobs\starfield_layout.py")
s = p.read_text(encoding="utf-8")
changed = []

# 1) UMAP 参数：拉大分布（min_dist 0.25 → 0.65，n_neighbors 收小）
s = s.replace(
"""        emb = umap.UMAP(n_components=3, n_neighbors=min(15, max(2, len(ids) - 1)),
                        min_dist=0.25, metric="cosine", random_state=42).fit_transform(X)""",
"""        emb = umap.UMAP(n_components=3, n_neighbors=max(2, min(8, len(ids) - 1)),
                        min_dist=0.65, spread=1.6, metric="cosine",
                        random_state=42).fit_transform(X)""")
changed.append("UMAP 拉开分布")

# 2) 归一化后：半径撑满 + 少量哈希扰动打散（避免点叠在一起）
s = s.replace(
"""    by_id = {n["id"]: n for n in nodes}
    coords: dict[str, dict] = {}
    for i, nid in enumerate(ids):
        n = by_id[nid]
        r = 0.55 + 0.45 * (1 - float(n.get("importance") or 0))
        coords[nid] = {"x": round(float(unit[i][0]) * r, 5),
                       "y": round(float(unit[i][1]) * r, 5),
                       "z": round(float(unit[i][2]) * r, 5)}
    # 其余节点（主题/根/没有向量的记忆）用确定性坐标补位：两视图节点集合必须一致""",
"""    by_id = {n["id"]: n for n in nodes}
    coords: dict[str, dict] = {}
    for i, nid in enumerate(ids):
        n = by_id[nid]
        # 半径 0.62~1.0 撑开整颗球，配合 UMAP spread 出"星河"的疏密
        r = 0.62 + 0.38 * (1 - float(n.get("importance") or 0))
        # 3% 哈希扰动：把几乎重合的点打散，避免视觉上一坨
        h = hashlib.sha1(nid.encode("utf-8")).hexdigest()
        jx = (int(h[:4], 16) / 0xFFFF - 0.5) * 0.06
        jy = (int(h[4:8], 16) / 0xFFFF - 0.5) * 0.06
        jz = (int(h[8:12], 16) / 0xFFFF - 0.5) * 0.06
        coords[nid] = {"x": round(float(unit[i][0]) * r + jx, 5),
                       "y": round(float(unit[i][1]) * r + jy, 5),
                       "z": round(float(unit[i][2]) * r + jz, 5)}
    # 其余节点（主题/根/没有向量的记忆）用确定性坐标补位：两视图节点集合必须一致""")
if "3% 哈希扰动" not in p.read_text(encoding="utf-8"):
    pass
changed.append("半径撑满 + 哈希扰动")

p.write_text(s, encoding="utf-8")
print("改动:", changed)
