import pathlib

# ---------- 1) 后端：禁止直接删主题（除非显式 force），防止一次删掉一大片 ----------
p = pathlib.Path(r"D:\qq-ai-bot\server\memory_viz.py")
s = p.read_text(encoding="utf-8")

old = '''def delete_node(node_id: str) -> dict:'''
new = '''def delete_node(node_id: str, force: bool = False) -> dict:'''
if old in s and "force: bool = False" not in s:
    s = s.replace(old, new, 1)

old2 = '''    if kind == "topic":
        # 删主题 = 删该主题下所有记忆
        nodes, _ = build_nodes(scope)'''
new2 = '''    if kind == "topic":
        # 删主题会连带删掉它下面所有记忆 —— 默认拒绝，必须显式 force
        if not force:
            nodes, _ = build_nodes(scope)
            cnt = len([n for n in nodes
                       if n.get("topicId") == node_id and n.get("kind") == "memory"])
            return {"ok": False, "error": f"这是主题节点，下面有 {cnt} 条记忆，需逐条删除",
                    "needForce": True, "memoryCount": cnt}
        nodes, _ = build_nodes(scope)'''
if old2 in s:
    s = s.replace(old2, new2, 1)
p.write_text(s, encoding="utf-8")
print("后端：删除主题需要 force")

# ---------- 2) 路由：DELETE 支持 ?force=1 ----------
d = pathlib.Path(r"D:\qq-ai-bot\dashboard\server.py")
u = d.read_text(encoding="utf-8")
old3 = '''        m = _re.match(r"^/api/memory/node/([^/]+)$", path)
        if m and method == "DELETE":
            self._send_json(memory_viz.delete_node(m.group(1)))
            return True'''
new3 = '''        m = _re.match(r"^/api/memory/node/([^/]+)$", path)
        if m and method == "DELETE":
            force = "force=1" in self.path
            self._send_json(memory_viz.delete_node(m.group(1), force=force))
            return True'''
if old3 in u:
    u = u.replace(old3, new3, 1)
    d.write_text(u, encoding="utf-8")
    print("路由：DELETE 支持 force")

# ---------- 3) 前端：只有记忆节点显示删除；主题节点显示条数提示 ----------
f = pathlib.Path(r"D:\qq-ai-bot\dashboard-web\src\features\memory-viz\DetailPanel\MemoryDetailPanel.tsx")
v = f.read_text(encoding="utf-8")
old4 = '''          <button
            style={{ ...btnStyle, borderColor: '#7a3a3a', color: '#ff9c9c' }}
            onClick={() => {
              if (confirm('确定删除这条记忆？')) remove.mutate();
            }}
          >
            删除
          </button>'''
new4 = '''          {n.kind === 'memory' ? (
            <button
              style={{ ...btnStyle, borderColor: '#7a3a3a', color: '#ff9c9c' }}
              onClick={() => {
                if (confirm(`确定删除这条记忆？\\n\\n${n.summary.slice(0, 40)}`)) remove.mutate();
              }}
            >
              删除这条记忆
            </button>
          ) : (
            <span style={{ color: '#5f7e96', fontSize: 12 }}>
              这是{n.kind === 'topic' ? '主题' : '分组'}节点，删除请逐条操作（避免一次清空一大片）
            </span>
          )}'''
if old4 in v:
    v = v.replace(old4, new4, 1)
    f.write_text(v, encoding="utf-8")
    print("面板：只有记忆节点可删除")
else:
    print("面板删除按钮锚点没找到")
