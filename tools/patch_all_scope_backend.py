import pathlib

# 1) dashboard 路由：scope="__all__" 时走聚合
p = pathlib.Path(r"D:\qq-ai-bot\dashboard\server.py")
s = p.read_text(encoding="utf-8")
if "memory_viz_all" not in s:
    s = s.replace("from server import memory_viz  # noqa: E402",
                  "from server import memory_viz  # noqa: E402\nfrom server import memory_viz_all  # noqa: E402", 1)
    s = s.replace('''                if not scope:
                    self._send_json({"error": "scope 必填"}, status=400); return
                self._send_json(memory_viz.tree(scope))''',
'''                if not scope:
                    self._send_json({"error": "scope 必填"}, status=400); return
                if scope == "__all__":
                    self._send_json(memory_viz_all.tree_all()); return
                self._send_json(memory_viz.tree(scope))''', 1)
    s = s.replace('''                if not scope:
                    self._send_json({"error": "scope 必填"}, status=400); return
                code, payload = memory_viz.starfield(scope)''',
'''                if not scope:
                    self._send_json({"error": "scope 必填"}, status=400); return
                if scope == "__all__":
                    code, payload = memory_viz_all.starfield_all()
                else:
                    code, payload = memory_viz.starfield(scope)''', 1)
    p.write_text(s, encoding="utf-8")
    print("dashboard 路由已支持 __all__")

# 2) scopes 接口最前面加一条"全部星海"
p2 = pathlib.Path(r"D:\qq-ai-bot\server\memory_viz.py")
s2 = p2.read_text(encoding="utf-8")
if '"全部星海"' not in s2:
    s2 = s2.replace('''    out.sort(key=lambda x: -x["count"])
    return {"scopes": out,''',
'''    total = sum(x["count"] for x in out)
    out.sort(key=lambda x: -x["count"])
    out.insert(0, {"scope": "__all__", "label": "全部星海", "count": total,
                   "topics": sum(x["topics"] for x in out)})
    return {"scopes": out,''', 1)
    p2.write_text(s2, encoding="utf-8")
    print("scopes 已加「全部星海」")
