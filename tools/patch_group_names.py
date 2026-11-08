import pathlib

# ---------- server：用 group_permissions.json 里的群名 ----------
p = pathlib.Path(r"D:\qq-ai-bot\dashboard\server.py")
s = p.read_text(encoding="utf-8")

if "_scope_display" not in s:
    helper = '''

def _scope_display() -> dict[str, str]:
    """作用域 → 人类可读名字（群名从 group_permissions.json 取）。"""
    names: dict[str, str] = {"global_learn": "自学池", "douyin": "抖音"}
    try:
        gp = _read_json(ROOT / "memory" / "group_permissions.json") or {}
        for gid, info in (gp.get("groups") or {}).items():
            nm = str((info or {}).get("name") or "").strip()
            if nm:
                names[f"group_{gid}"] = nm
    except Exception:  # noqa: BLE001
        pass
    return names


'''
    i = s.find("def collect_memory_graph(")
    s = s[:i] + helper.lstrip("\n") + "\n" + s[i:]

old_label = '''    def label_of(scope: str) -> str:
        if scope in scope_label:
            return scope_label[scope]
        if scope.startswith("group_"):
            return "群 " + scope.split("_", 1)[1][-6:]
        if scope.startswith("private_"):
            who = scope.split("_", 1)[1]
            return "私聊 " + ("主人" if who.isdigit() and len(who) <= 9 else who[-6:])
        return scope'''
new_label = '''    names = _scope_display()

    def label_of(scope: str) -> str:
        if scope in names:
            return names[scope]
        if scope in scope_label:
            return scope_label[scope]
        if scope.startswith("group_"):
            return "群 " + scope.split("_", 1)[1][-6:]
        if scope.startswith("private_"):
            who = scope.split("_", 1)[1]
            return "私聊 " + ("主人" if who.isdigit() and len(who) <= 9 else who[-6:])
        return scope'''
if old_label in s:
    s = s.replace(old_label, new_label, 1)
else:
    print("label_of 未匹配")

# 返回里带上 labels
s = s.replace('''    return {
        "nodes": nodes,
        "links": links,
        "stats": {''','''    return {
        "nodes": nodes,
        "links": links,
        "labels": {**names, **{sc: label_of(sc) for sc, _ in scopes}},
        "stats": {''', 1)

# 详情里也带名字
s = s.replace('''    return {
        "scope": scope,
        "facts":''', '''    return {
        "scope": scope,
        "label": _scope_display().get(scope) or scope,
        "facts":''', 1)

p.write_text(s, encoding="utf-8")
print("server 已支持群名")

# ---------- 前端：左栏和详情用 labels ----------
p2 = pathlib.Path(r"D:\qq-ai-bot\dashboard\index.html")
h = p2.read_text(encoding="utf-8")

h = h.replace('''  function scopeName(g){
    return g.replace('group_','群 ').replace('private_','私聊 ').replace('global_learn','自学池');
  }''', '''  let scopeLabels = {};
  function scopeName(g){
    return scopeLabels[g] || g.replace('group_','群 ').replace('private_','私聊 ').replace('global_learn','自学池');
  }''', 1)

h = h.replace('''    byId = new Map(nodes.map(n => [n.id, n]));
    links = data.links.filter(l => byId.has(l.source) && byId.has(l.target));''',
'''    scopeLabels = data.labels || {};
    byId = new Map(nodes.map(n => [n.id, n]));
    links = data.links.filter(l => byId.has(l.source) && byId.has(l.target));''', 1)

h = h.replace('''      const nm = g.replace('group_','群 ').replace('private_','私聊 ');''',
              '''      const nm = g.replace('group_','群 ').replace('private_','私聊 ');''', 1)

# 详情标题用后端 label
h = h.replace('''      memBody.innerHTML =
        `<h3>${scopeName(scope)}</h3>''', '''      memBody.innerHTML =
        `<h3>${d.label || scopeName(scope)}</h3>''', 1)

p2.write_text(h, encoding="utf-8")
print("前端已用 labels")
