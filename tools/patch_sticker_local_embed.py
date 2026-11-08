import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\plugins\sticker_vec.py")
s = p.read_text(encoding="utf-8")

if "_local_embed" in s:
    print("已支持本地服务")
    raise SystemExit

helper = '''

LOCAL_EMBED_URL = os.getenv("LOCAL_EMBED_URL", "http://127.0.0.1:7801/embed")
LOCAL_EMBED_TIMEOUT = float(os.getenv("LOCAL_EMBED_TIMEOUT", "3") or "3")


def _local_embed(texts: list[str]) -> list[list[float]]:
    """优先走本地 bge-m3 服务（免费、离线）；不通就返回空。"""
    import json as _json
    import urllib.request
    try:
        req = urllib.request.Request(
            LOCAL_EMBED_URL,
            data=_json.dumps({"texts": texts}, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=LOCAL_EMBED_TIMEOUT) as r:
            data = _json.loads(r.read().decode("utf-8", "ignore"))
        vecs = data.get("vectors") or []
        if vecs and len(vecs) == len(texts):
            return [[float(x) for x in v] for v in vecs]
    except Exception:  # noqa: BLE001
        pass
    return []


def _embed(texts: list[str]) -> list[list[float]]:
    """本地服务优先，失败回退硅基流动（若余额够）。"""
    if not texts:
        return []
    v = _local_embed(texts)
    if v:
        return v
    try:
        from memory_vec import embed as _remote
        return _remote(texts)
    except Exception:  # noqa: BLE001
        return []


def _available() -> bool:
    try:
        import urllib.request
        with urllib.request.urlopen(LOCAL_EMBED_URL.replace("/embed", "/health"), timeout=1.5) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        pass
    try:
        from memory_vec import available as _av
        return _av()
    except Exception:  # noqa: BLE001
        return False
'''

anchor = "def _key(text: str) -> str:"
i = s.find(anchor)
s = s[:i] + helper.lstrip("\n") + "\n\n" + s[i:]

# build/best 里换成 _embed / _available
s = s.replace('''    try:
        from memory_vec import available, embed
    except Exception:  # noqa: BLE001
        return {"built": 0, "total": 0, "available": False}
    if not available():
        return {"built": 0, "total": 0, "available": False}''',
'''    if not _available():
        return {"built": 0, "total": 0, "available": False}''')
s = s.replace("        vecs = embed([d for _, d in batch])", "        vecs = _embed([d for _, d in batch])")

s = s.replace('''    try:
        from memory_vec import available, embed
    except Exception:  # noqa: BLE001
        return None
    if not available():
        return None''',
'''    if not _available():
        return None''')
s = s.replace("        vecs = embed([t[:200]])", "        vecs = _embed([t[:200]])")

s = s.replace('''    try:
        from memory_vec import available
        avail = available()
    except Exception:  # noqa: BLE001
        avail = False
    return {"enabled": ENABLED, "vectors": len(idx), "min_sim": MIN_SIM, "available": avail}''',
'''    return {"enabled": ENABLED, "vectors": len(idx), "min_sim": MIN_SIM,
            "available": _available(), "local_url": LOCAL_EMBED_URL}''')

p.write_text(s, encoding="utf-8")
print("sticker_vec 已改为本地服务优先")
