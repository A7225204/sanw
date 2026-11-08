import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\plugins\bilibili_daily.py")
s = p.read_text(encoding="utf-8")

# 1) 合集进度写进长期记忆（她会"记得在追哪部、看到第几集"）
old = '''    if _total > 1 and bvid:
        d.setdefault("part_progress", {})[bvid] = part'''
new = '''    if _total > 1 and bvid:
        d.setdefault("part_progress", {})[bvid] = part
        d.setdefault("series_title", {})[bvid] = title
        # 写进长期记忆，聊天时能自然带出"我在追《xx》，看到第 k 集"
        try:
            from plugins import long_memory
            long_memory.add_learned(
                f"在追B站合集《{title[:30]}》，已看到第 {part}/{_total} 集",
                source="B站追剧", kind="news", ttl_days=120)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[B站刷] 写追剧记忆失败：{type(e).__name__}: {e}")'''
if old in s:
    s = s.replace(old, new, 1)
    print("bilibili_daily: 追剧进度写长期记忆")

# 2) 挑片时给"没追完的合集"一点机会，别只顾着刷热门
old2 = '''def pick_of_day(fresh: bool = True) -> dict | None:
    """今天还没看过的热门里挑一条（偏热度高的，但带随机）。"""
    d = _roll(_load())'''
new2 = '''def pick_of_day(fresh: bool = True) -> dict | None:
    """优先续追没看完的合集（35% 概率），否则今天还没看过的热门里挑。"""
    d = _roll(_load())
    prog = dict(d.get("part_progress") or {})
    titles = dict(d.get("series_title") or {})
    if prog and random.random() < 0.35:
        bvid = random.choice(list(prog.keys()))
        return {"bvid": bvid, "title": titles.get(bvid, bvid),
                "url": f"https://www.bilibili.com/video/{bvid}",
                "view": 0, "owner": "", "duration": 0, "continue": True}'''
if old2 in s:
    s = s.replace(old2, new2, 1)
    print("bilibili_daily: 挑片会续追")

# 3) stats 里带上追剧进度（网页端显示）
old3 = '''def stats() -> dict:
    d = _roll(_load())
    return {"date": d.get("date"), "count": int(d.get("count") or 0),
            "limit": DAILY_LIMIT, "left": max(0, DAILY_LIMIT - int(d.get("count") or 0)),
            "recent": (d.get("history") or [])[-5:]}'''
new3 = '''def stats() -> dict:
    d = _roll(_load())
    prog = dict(d.get("part_progress") or {})
    titles = dict(d.get("series_title") or {})
    return {"date": d.get("date"), "count": int(d.get("count") or 0),
            "limit": DAILY_LIMIT, "left": max(0, DAILY_LIMIT - int(d.get("count") or 0)),
            "recent": (d.get("history") or [])[-5:],
            "series": [{"bvid": b, "title": titles.get(b, b), "part": p}
                       for b, p in prog.items()]}'''
if old3 in s:
    s = s.replace(old3, new3, 1)
    print("bilibili_daily: stats 带追剧进度")

p.write_text(s, encoding="utf-8")
