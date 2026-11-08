import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\server\memory_viz_all.py")
s = p.read_text(encoding="utf-8")

if "owner_links" in s:
    print("已支持主人为中心")
    raise SystemExit

old = '''def _cluster_layout(entries: list[tuple[str, list[dict], dict]], ring: float = 1.55,
                    shrink: float = 0.42) -> dict[str, dict]:
    """把每个 scope 当成一个星团，摆在大圆环上；团内坐标按 shrink 压小。"""
    centers: dict[str, tuple[float, float, float]] = {}
    n = max(1, len(entries))
    for i, (scope, _nodes, _coords) in enumerate(entries):
        ang = 2 * math.pi * i / n
        # 两层环：多的在外圈，避免所有团挤一圈
        r = ring if i % 2 == 0 else ring * 0.62
        y = 0.18 if i % 3 == 0 else (-0.18 if i % 3 == 1 else 0.0)
        centers[scope] = (r * math.cos(ang), y, r * math.sin(ang))'''

new = '''OWNER_SCOPE = "private_100000000"


def _cluster_layout(entries: list[tuple[str, list[dict], dict]], ring: float = 1.15,
                    shrink: float = 0.5) -> tuple[dict[str, dict], dict[str, tuple[float, float, float]]]:
    """主人星团放正中心，其它（他聊过/拉她去的群）按活跃度由内向外扩散。"""
    centers: dict[str, tuple[float, float, float]] = {}
    others = [e for e in entries if e[0] != OWNER_SCOPE]
    owner = [e for e in entries if e[0] == OWNER_SCOPE]

    if owner:
        centers[OWNER_SCOPE] = (0.0, 0.0, 0.0)

    # 活跃度高的靠近主人（条数多=聊得多）
    others.sort(key=lambda e: -len(e[1]))
    n = max(1, len(others))
    for i, (scope, nodes, _coords) in enumerate(others):
        # 由内向外：第 1 圈 ~1.15，之后每 0.55 一圈，一圈最多 6 个
        ring_idx = i // 6
        r = ring + ring_idx * 0.62
        ang = 2 * math.pi * (i % 6) / min(6, max(1, n - ring_idx * 6)) + ring_idx * 0.5
        y = ((i % 3) - 1) * 0.16
        centers[scope] = (r * math.cos(ang), y, r * math.sin(ang))'''

if old in s:
    s = s.replace(old, new, 1)
else:
    print("布局函数没匹配")

# 返回值补 centers（原函数 return placed, centers）
s = s.replace('''    return placed, centers  # type: ignore[return-value]''',
               '''    return placed, centers''')

# 跨团边：加"主人 → 每个星河"的连线
old_cross = '''    return 200, {
        "scope": ALL_SCOPE,
        "nodes": out_nodes,
        "edges": edges,'''
new_cross = '''    # 主人 → 其它星河：他聊过的群 / 拉她去的群，用一条金线串起来
    owner_links = 0
    owner_root = f"scope:{OWNER_SCOPE}"
    if owner_root in by_id:
        max_cnt = max((len(nodes) for sc, nodes, _c in entries if sc != OWNER_SCOPE), default=1)
        for scope, nodes, _coords in entries:
            if scope == OWNER_SCOPE:
                continue
            root_id = f"scope:{scope}"
            if root_id not in by_id:
                continue
            edges.append({
                "source": owner_root, "target": root_id,
                "weight": round(min(1.0, 0.35 + 0.65 * len(nodes) / max(1, max_cnt)), 4),
                "kind": "owner", "label": mv._scope_label(scope),
            })
            owner_links += 1

    return 200, {
        "scope": ALL_SCOPE,
        "nodes": out_nodes,
        "edges": edges,
        "ownerLinks": owner_links,'''
if old_cross in s:
    s = s.replace(old_cross, new_cross, 1)

p.write_text(s, encoding="utf-8")
print("已加：主人居中 + 主人→各星河 金线")
