import pathlib

# ---------- 1) 后端：detail 多返回相关节点的文字（而不是只给 id） ----------
p = pathlib.Path(r"D:\qq-ai-bot\server\memory_viz.py")
s = p.read_text(encoding="utf-8")

old = '''                siblings = [n["id"] for n in nodes
                            if n.get("topicId") and n.get("topicId") == (me or {}).get("topicId")
                            and n["id"] != node_id]
                return {
                    "node": me or {"id": node_id, "scope": scope},
                    "sourceMessages": src_msgs,
                    "relatedNodeIds": siblings[:8],
                }'''
new = '''                sib_nodes = [n for n in nodes
                             if n.get("topicId") and n.get("topicId") == (me or {}).get("topicId")
                             and n["id"] != node_id]
                siblings = [n["id"] for n in sib_nodes]
                return {
                    "node": me or {"id": node_id, "scope": scope},
                    "sourceMessages": src_msgs,
                    "relatedNodeIds": siblings[:8],
                    "related": [{"id": n["id"], "summary": n.get("summary") or "",
                                 "kind": n.get("kind") or "memory"} for n in sib_nodes[:8]],
                }'''
if old in s:
    s = s.replace(old, new, 1)

old2 = '''    related = [n["id"] for n in nodes
               if n.get("kind") == "memory" and n.get("topicId") == me.get("topicId")][:8]
    return {"node": me, "sourceMessages": [], "relatedNodeIds": related}'''
new2 = '''    rel_nodes = [n for n in nodes
                 if n.get("kind") == "memory" and n.get("topicId") == me.get("topicId")][:8]
    return {
        "node": me,
        "sourceMessages": [],
        "relatedNodeIds": [n["id"] for n in rel_nodes],
        "related": [{"id": n["id"], "summary": n.get("summary") or "",
                     "kind": n.get("kind") or "memory"} for n in rel_nodes],
    }'''
if old2 in s:
    s = s.replace(old2, new2, 1)

p.write_text(s, encoding="utf-8")
print("detail: 相关节点带上文字")

# ---------- 2) 前端类型 + 展示 ----------
t = pathlib.Path(r"D:\qq-ai-bot\dashboard-web\src\features\memory-viz\types.ts")
u = t.read_text(encoding="utf-8")
if "related?" not in u:
    u = u.replace("""  relatedNodeIds: string[];
}""", """  relatedNodeIds: string[];
  related?: { id: string; summary: string; kind: string }[];
}""")
    t.write_text(u, encoding="utf-8")
    print("types: related 带文字")

d = pathlib.Path(r"D:\qq-ai-bot\dashboard-web\src\features\memory-viz\DetailPanel\MemoryDetailPanel.tsx")
v = d.read_text(encoding="utf-8")
old3 = '''          {d.relatedNodeIds.slice(0, 8).map((id) => (
            <button key={id} style={chipStyle} onClick={() => useVizStore.getState().setSelected(id)}>
              {id.split(':').pop()}
            </button>
          ))}'''
new3 = '''          {(d.related ?? []).map((r) => (
            <button
              key={r.id}
              title={r.id}
              style={chipStyle}
              onClick={() => useVizStore.getState().setSelected(r.id)}
            >
              {r.summary ? r.summary.slice(0, 12) : r.id.split(':').pop()}
            </button>
          ))}'''
if old3 in v:
    v = v.replace(old3, new3, 1)
    d.write_text(v, encoding="utf-8")
    print("详情面板：相关记忆显示文字")
else:
    print("详情面板锚点没找到")
