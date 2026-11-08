import pathlib

# ---------- 1) 后端：跨星河连线优先按 QQ 号（同一个人），昵称兜底 ----------
p = pathlib.Path(r"D:\qq-ai-bot\server\memory_viz_all.py")
s = p.read_text(encoding="utf-8")
old = '''    # 跨团边：同名主题（同一个人）连一条紫色虚线
    topics: dict[str, list[dict]] = {}
    for n in out_nodes:
        if n.get("kind") != "topic":
            continue
        topics.setdefault(str(n.get("summary") or "").strip(), []).append(n)
    cross = 0
    for name, group in topics.items():
        if len(group) < 2 or not name:
            continue
        group.sort(key=lambda x: -float(x.get("importance") or 0))
        for i in range(len(group) - 1):
            edges.append({
                "source": group[i]["id"], "target": group[i + 1]["id"],
                "weight": round(min(1.0, 0.4 + 0.2 * len(group)), 4),
                "kind": "cross", "label": name,
            })
            cross += 1'''
new = '''    # 跨团边：同一个人（优先按 QQ 号匹配，昵称兜底）连一条绿线
    topics_by_key: dict[str, list[dict]] = {}
    for n in out_nodes:
        if n.get("kind") != "topic":
            continue
        name = str(n.get("summary") or "").strip()
        if not name:
            continue
        scope = str(n.get("scope") or "")
        info = mv._speaker_info(scope, name)
        # 有 QQ 就按 QQ 归并（同名不同人/改名都能对上），没有就按昵称
        key = f"uid:{info['uid']}" if info.get("uid") else f"name:{name}"
        topics_by_key.setdefault(key, []).append(n)

    cross = 0
    for key, group in topics_by_key.items():
        # 只连"不同星河之间"的人，同一个群里的多个主题不连
        scopes = {str(n.get("scope")) for n in group}
        if len(scopes) < 2:
            continue
        group.sort(key=lambda x: (str(x.get("scope")), -float(x.get("importance") or 0)))
        for i in range(len(group) - 1):
            a, b = group[i], group[i + 1]
            if str(a.get("scope")) == str(b.get("scope")):
                continue
            edges.append({
                "source": a["id"], "target": b["id"],
                "weight": round(min(1.0, 0.45 + 0.15 * len(group)), 4),
                "kind": "cross",
                "label": str(a.get("summary") or key),
                "viaQq": key.startswith("uid:"),
            })
            cross += 1'''
if old in s:
    s = s.replace(old, new, 1)
    p.write_text(s, encoding="utf-8")
    print("跨星河连线：按 QQ 号归并（绿线）")
else:
    print("跨团边锚点没找到")

# ---------- 2) 前端：cross 边改成绿色 ----------
r = pathlib.Path(r"D:\qq-ai-bot\dashboard-web\src\features\memory-viz\StarfieldView\starfieldRenderer.ts")
t = r.read_text(encoding="utf-8")
t = t.replace("const crossLines = mkLines(crossPos, 0xffc861, 0.28);   // 与主人金线合并，略淡",
              "const crossLines = mkLines(crossPos, 0x3ddc84, 0.5);    // 同一个人跨星河：绿线")
t = t.replace("const c = new THREE.Color(s.owner ? 0xffe0a8 : s.cross ? 0xffd9a0 : 0xeaf6ff);",
              "const c = new THREE.Color(s.owner ? 0xffe0a8 : s.cross ? 0xa8ffd0 : 0xeaf6ff);")
r.write_text(t, encoding="utf-8")
print("渲染器：跨星河线改为绿色")
