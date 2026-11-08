import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\plugins\bilibili_daily.py")
s = p.read_text(encoding="utf-8")

start = s.find("async def watch(")
end = s.find("async def watch_one(")
if start < 0 or end < 0:
    raise SystemExit("锚点未找到")

new = '''async def _watch_one_part(url: str, bvid: str, meta: dict, part: int,
                          by: str, total: int) -> dict:
    """看合集里的某一集（或单集视频），返回这一集的结果。"""
    from plugins import media_fetch
    from plugins import bili_dl
    real_url = bili_dl.with_part(url, part) if total > 1 else url
    res = await media_fetch.fetch(real_url)
    title = str(res.get("title") or meta.get("title") or "")
    summary = await _summarize(res)
    part_name = ""
    for pg in (meta.get("pages") or []):
        if int(pg.get("page") or 0) == part:
            part_name = str(pg.get("part") or "")
            break
    if total > 1:
        summary = (f"（合集共 {total} 集，这次看的是第 {part} 集"
                   f"{('：' + part_name) if part_name else ''}）") + summary
    return {"title": title, "summary": summary, "part": part,
            "part_name": part_name, "total": total, "url": real_url}


async def watch(url: str, by: str = "scheduler", part: int = 0,
                count: int = 1) -> dict:
    """看视频：下载+分镜+识图（media_fetch 负责），再记进学习/追剧档案。

    合集（多P）支持一次连看 count 集：从"上次看到第几集"往下数，
    每集各记一条历史，进度写进长期记忆。
    """
    from plugins import bili_dl
    meta = await asyncio.to_thread(bili_dl.video_meta, url)
    _total = int(meta.get("total") or 1)
    bvid = str(meta.get("bvid") or "")
    if part <= 0:
        d0 = _roll(_load())
        prog = (d0.get("part_progress") or {}).get(bvid)
        part = max(1, int(prog or 0) + 1) if prog else 1
    part = max(1, min(part, max(1, _total)))
    count = max(1, int(count or 1))
    if _total > 1:
        count = min(count, max(1, _total - part + 1))

    global _WATCH_UNTIL
    _WATCH_UNTIL = time.time() + 3600 * max(1, count)
    try:
        from plugins.activity import set_activity
        set_activity("看B站视频中", 1800 * max(1, count))
    except Exception:  # noqa: BLE001
        pass

    episodes: list[dict] = []
    try:
        for i in range(count):
            cur = part + i
            if _total > 1 and cur > _total:
                break
            ep = await _watch_one_part(url, bvid, meta, cur, by, _total)
            episodes.append(ep)
            logger.info(f"[B站刷] 看完第 {cur}{('/' + str(_total)) if _total > 1 else ''} 集："
                        f"{ep['title'][:30]}")
            # 每看完一集就落一次进度，中途失败也不丢
            d = _roll(_load())
            if by == "scheduler":
                d["count"] = int(d.get("count") or 0) + 1
            if bvid and bvid not in d["seen"]:
                d["seen"].append(bvid)
            d["history"].append({"bvid": bvid, "title": ep["title"],
                                 "summary": ep["summary"],
                                 "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                 "by": by, "part": cur, "total": _total})
            if _total > 1 and bvid:
                d.setdefault("part_progress", {})[bvid] = cur
                d.setdefault("series_title", {})[bvid] = ep["title"]
            _save(d)
            try:
                from plugins import long_memory
                long_memory.add_learned(
                    f"B站视频《{ep['title'][:40]}》：{ep['summary'][:100]}",
                    source="B站热门", kind="news", ttl_days=LEARN_TTL)
                if _total > 1:
                    long_memory.add_learned(
                        f"在追B站合集《{ep['title'][:30]}》，已看到第 {cur}/{_total} 集",
                        source="B站追剧", kind="news", ttl_days=120)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[B站刷] 记学习失败：{type(e).__name__}: {e}")
    finally:
        _WATCH_UNTIL = 0.0
        try:
            from plugins.activity import set_activity
            set_activity("刚看完B站视频", 180)
        except Exception:  # noqa: BLE001
            pass

    d = _roll(_load())
    if not episodes:
        return {"title": meta.get("title") or "", "summary": "没看成", "bvid": bvid,
                "count": d.get("count", 0), "limit": DAILY_LIMIT, "url": url,
                "episodes": [], "part": part, "total": _total}
    joined = "\\n\\n".join(f"P{ep['part']}：{ep['summary']}" for ep in episodes) \\
        if len(episodes) > 1 else episodes[0]["summary"]
    return {"title": episodes[0]["title"], "summary": joined, "bvid": bvid,
            "count": d.get("count", 0), "limit": DAILY_LIMIT,
            "url": url, "episodes": episodes, "part": part, "total": _total}


'''
s = s[:start] + new + s[end:]
p.write_text(s, encoding="utf-8")
print("bilibili_daily: watch 支持连看多集")
