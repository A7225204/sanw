import pathlib

# ---------- 1) bili_dl：查合集分P ----------
p = pathlib.Path(r"D:\qq-ai-bot\plugins\bili_dl.py")
s = p.read_text(encoding="utf-8")
if "def video_meta(" not in s:
    add = '''

def video_meta(url: str) -> dict:
    """查视频基本信息 + 分P（合集）。

    返回 {"bvid","title","total","pages":[{page,part,duration}]}；
    查不到就 total=1，按单集处理。
    """
    import re as _re
    m = _re.search(r"(BV[0-9A-Za-z]{10})", str(url or ""))
    out = {"bvid": m.group(1) if m else "", "title": "", "total": 1, "pages": []}
    if not out["bvid"]:
        return out
    try:
        import httpx
        r = httpx.get("https://api.bilibili.com/x/web-interface/view",
                      params={"bvid": out["bvid"]}, timeout=20,
                      headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"},
                      cookies=_cookie_dict() or None)
        d = (r.json() or {}).get("data") or {}
        pages = d.get("pages") or []
        out["title"] = str(d.get("title") or "")
        out["pages"] = [{"page": int(pg.get("page") or i + 1),
                         "part": str(pg.get("part") or f"P{i + 1}")[:60],
                         "duration": int(pg.get("duration") or 0)}
                        for i, pg in enumerate(pages)]
        out["total"] = len(pages) or 1
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[B站] 查分P失败：{type(e).__name__}: {e}")
    return out


def with_part(url: str, part: int) -> str:
    """把 ?p=N 拼到链接上（1 就不拼）。"""
    if int(part or 1) <= 1:
        return url
    base = str(url or "").split("?")[0]
    return f"{base}?p={int(part)}"
'''
    s = s.rstrip() + add
    p.write_text(s, encoding="utf-8")
    print("bili_dl: 加分P查询")

# ---------- 2) bilibili_daily：合集按集推进，不谎称看完 ----------
q = pathlib.Path(r"D:\qq-ai-bot\plugins\bilibili_daily.py")
t = q.read_text(encoding="utf-8")

t = t.replace('''async def watch(url: str, by: str = "scheduler") -> dict:''',
'''async def watch(url: str, by: str = "scheduler", part: int = 0) -> dict:''')

t = t.replace('''    from plugins import media_fetch
    global _WATCH_UNTIL''',
'''    from plugins import media_fetch
    from plugins import bili_dl
    meta = await asyncio.to_thread(bili_dl.video_meta, url)
    _total = int(meta.get("total") or 1)
    if part <= 0:
        # 合集续看：记住上次看到第几集，下次接着下一集
        _d0 = _roll(_load())
        _prog = (_d0.get("part_progress") or {}).get(meta.get("bvid") or "")
        part = max(1, int(_prog or 0) + 1) if _prog else 1
    part = max(1, min(part, max(1, _total)))
    if _total > 1:
        url = bili_dl.with_part(url, part)
    global _WATCH_UNTIL''')

t = t.replace('''    title = str(res.get("title") or "")
    summary = await _summarize(res)''',
'''    title = str(res.get("title") or meta.get("title") or "")
    summary = await _summarize(res)
    if _total > 1:
        _pj = ""
        for _pg in (meta.get("pages") or []):
            if int(_pg.get("page") or 0) == part:
                _pj = str(_pg.get("part") or "")
                break
        _note = f"（合集共 {_total} 集，这次看的是第 {part} 集{('：' + _pj) if _pj else ''}）"
        summary = _note + summary''')

t = t.replace('''    d["history"].append({"bvid": bvid, "title": title, "summary": summary,
                         "at": time.strftime("%Y-%m-%d %H:%M:%S"), "by": by})''',
'''    d["history"].append({"bvid": bvid, "title": title, "summary": summary,
                         "at": time.strftime("%Y-%m-%d %H:%M:%S"), "by": by,
                         "part": part, "total": _total})
    if _total > 1 and bvid:
        d.setdefault("part_progress", {})[bvid] = part''')

q.write_text(t, encoding="utf-8")
print("bilibili_daily: 合集按集推进")
