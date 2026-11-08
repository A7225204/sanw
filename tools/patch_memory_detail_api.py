import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\dashboard\server.py")
s = p.read_text(encoding="utf-8")

if "collect_memory_detail" in s:
    print("已存在")
    raise SystemExit

add = '''

def collect_memory_detail(scope: str, limit: int = 24) -> dict:
    """某个作用域的详情：长期记忆 + 短期对话 + 记忆锚点。"""
    if not scope or len(scope) > 64:
        return {"error": "scope 不合法"}
    archive = (_read_json(ROOT / "memory" / "长期记忆.json") or {}).get("archive") or {}
    facts = [it for it in (archive.get(scope) or []) if isinstance(it, dict) and it.get("fact")]
    facts_sorted = sorted(facts, key=lambda it: float(it.get("w") or 0), reverse=True)

    def w_of(it):
        try:
            return float(it.get("w") or 0)
        except Exception:  # noqa: BLE001
            return 0.0

    anchors = [{"fact": str(it.get("fact"))[:120], "subject": str(it.get("subject") or ""),
                "time": str(it.get("time") or ""), "w": w_of(it)} for it in facts_sorted[:5]]

    recent: list[dict] = []
    try:
        with sqlite3.connect(str(DB_PATH), timeout=8) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "select timestamp, message, substr(reply,1,160) as reply from chat_history "
                "where scope=? order by rowid desc limit ?", (scope, limit)).fetchall()
            recent = [{"ts": r["timestamp"], "user": r["message"], "bot": r["reply"]} for r in rows]
    except Exception as exc:  # noqa: BLE001
        recent = [{"ts": "", "user": f"(读取失败: {exc})", "bot": ""}]

    return {
        "scope": scope,
        "facts": [{"fact": str(it.get("fact"))[:160], "subject": str(it.get("subject") or ""),
                   "time": str(it.get("time") or ""), "w": w_of(it)} for it in facts_sorted[:80]],
        "facts_total": len(facts),
        "recent": recent,
        "anchors": anchors,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
'''
i = s.find("def collect_status()")
s = s[:i] + add.lstrip("\n") + "\n\n" + s[i:]

old = '''        elif path == "/api/persona":'''
new = '''        elif path.startswith("/api/memory_detail"):
            try:
                qs = parse_qs(urlparse(self.path).query)
                scope = (qs.get("scope") or [""])[0]
                self._send_json(collect_memory_detail(scope))
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)
        elif path == "/api/persona":'''
if old in s:
    s = s.replace(old, new, 1)
else:
    print("路由锚点没找到")

if "from urllib.parse import" not in s:
    s = s.replace("import sqlite3", "import sqlite3\nfrom urllib.parse import parse_qs, urlparse", 1)

p.write_text(s, encoding="utf-8")
print("已加 /api/memory_detail")
