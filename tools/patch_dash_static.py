import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\dashboard\server.py")
s = p.read_text(encoding="utf-8")

if "/memory-viz/" in s:
    print("静态路由已存在")
    raise SystemExit

anchor = '        elif path.startswith("/assets/"):'
route = '''        elif path in ("/memory-viz", "/memory-viz/"):
            target = STATIC_DIR / "webapp" / "index.html"
            if not target.exists():
                self._send_json({"error": "webapp 未构建（cd dashboard-web && npm run build）"}, status=404)
                return
            self._send_file(target, "text/html; charset=utf-8")
        elif path.startswith("/memory-viz/"):
            rel = path[len("/memory-viz/"):] or "index.html"
            if ".." in rel:
                self.send_response(404); self.end_headers(); return
            f = STATIC_DIR / "webapp" / rel
            if not f.is_file():
                self.send_response(404); self.end_headers(); return
            ctype = {
                ".html": "text/html; charset=utf-8",
                ".js": "application/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".svg": "image/svg+xml",
                ".png": "image/png",
                ".json": "application/json; charset=utf-8",
                ".ico": "image/x-icon",
            }.get(f.suffix.lower(), "application/octet-stream")
            self._send_file(f, ctype)
        elif path.startswith("/assets/"):'''
if anchor in s:
    s = s.replace(anchor, route, 1)
    p.write_text(s, encoding="utf-8")
    print("已加 /memory-viz 静态路由")
else:
    print("锚点没找到")
