import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\jobs\starfield_layout.py")
s = p.read_text(encoding="utf-8")

old = '''    norms = np.linalg.norm(emb, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    unit = emb / norms
    by_id = {n["id"]: n for n in nodes}'''
new = '''    # 每列标准化：把 UMAP 挤在一起的细微差异放大，星团才散得开
    mu = emb.mean(axis=0, keepdims=True)
    sd = emb.std(axis=0, keepdims=True)
    sd[sd == 0] = 1.0
    emb = (emb - mu) / sd
    norms = np.linalg.norm(emb, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    unit = emb / norms
    by_id = {n["id"]: n for n in nodes}'''
if old in s and "每列标准化" not in s:
    s = s.replace(old, new, 1)

# 与哈希球面方向混合 35%，进一步打散（保留 UMAP 的语义聚类倾向）
old2 = '''        coords[nid] = {"x": round(float(unit[i][0]) * r + jx, 5),
                       "y": round(float(unit[i][1]) * r + jy, 5),
                       "z": round(float(unit[i][2]) * r + jz, 5)}'''
new2 = '''        hd = _unit_from_hash(nid)
        mix = (0.62 * float(unit[i][0]) + 0.38 * hd[0],
               0.62 * float(unit[i][1]) + 0.38 * hd[1],
               0.62 * float(unit[i][2]) + 0.38 * hd[2])
        mn = math.sqrt(sum(c * c for c in mix)) or 1.0
        mix = (mix[0] / mn, mix[1] / mn, mix[2] / mn)
        coords[nid] = {"x": round(mix[0] * r + jx, 5),
                       "y": round(mix[1] * r + jy, 5),
                       "z": round(mix[2] * r + jz, 5)}'''
if old2 in s:
    s = s.replace(old2, new2, 1)

# 哈希方向助手
if "_unit_from_hash" not in s.split("def _svd_coords")[0]:
    helper = '''

def _unit_from_hash(nid: str) -> tuple[float, float, float]:
    """由 id 哈希得到球面单位向量（用于打散/兜底）。"""
    seed = hashlib.sha1(nid.encode("utf-8")).hexdigest()
    u = int(seed[:8], 16) / 0xFFFFFFFF
    v = int(seed[8:16], 16) / 0xFFFFFFFF
    theta = 2 * math.pi * u
    phi = math.acos(2 * v - 1)
    return (math.sin(phi) * math.cos(theta), math.sin(phi) * math.sin(theta), math.cos(phi))

'''
    i = s.find("def _umap_coords(")
    s = s[:i] + helper.lstrip("\n") + "\n" + s[i:]

# SVD 分支同样放大
s = s.replace('''    X = X - X.mean(axis=0, keepdims=True)''',
'''    X = X - X.mean(axis=0, keepdims=True)
    _sd = X.std(axis=0, keepdims=True)
    _sd[_sd == 0] = 1.0
    X = X / _sd''', 1)

p.write_text(s, encoding="utf-8")
print("已加：列标准化 + 哈希方向混合 38%")
