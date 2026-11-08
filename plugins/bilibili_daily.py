"""B站每日刷视频 + 主人点看。

两个入口
--------
1. 定时：每天固定看 N 条（默认 10）B站热门，看完记一条学习档案
2. 点看：主人发 `/B站 看视频 链接或视频名`，没有参数就随机挑一条热门
   —— 主人指令**不占**每日的 10 条配额

状态：memory/bili_daily.json
  {date, count, seen:[bvid], history:[{bvid,title,summary,at,by}]}
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from pathlib import Path

from nonebot import logger

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "memory" / "bili_daily.json"

DAILY_LIMIT = int(os.getenv("BILI_DAILY_LIMIT", "10") or "10")
WATCH_CFG = ROOT / "memory" / "bili_watch_config.json"
_WC_DEFAULT = {
    "keywords": ["大肥鱼", "鲸鱼娘", "蓝色大肥鱼", "示例角色"],
    "keyword_p": float(os.getenv("BILI_KEYWORD_P", "0.3") or "0.3"),
    "daily_limit": DAILY_LIMIT,
    "window": os.getenv("BILI_DAILY_WINDOW", "9-23"),
}


def _wc() -> dict:
    """看片配置：memory/bili_watch_config.json，改完即刻生效。"""
    base = dict(_WC_DEFAULT)
    try:
        if WATCH_CFG.exists():
            d = json.loads(WATCH_CFG.read_text(encoding="utf-8-sig"))
            if isinstance(d, dict):
                if isinstance(d.get("keywords"), list):
                    base["keywords"] = [str(k).strip() for k in d["keywords"] if str(k).strip()]
                for k in ("keyword_p", "daily_limit"):
                    if d.get(k) is not None:
                        base[k] = type(_WC_DEFAULT[k])(d[k])
                if d.get("window"):
                    base["window"] = str(d["window"])
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[B站刷] 读看片配置失败：{type(e).__name__}: {e}")
    return base
LEARN_TTL = float(os.getenv("BILI_LEARN_TTL_DAYS", "14") or "14")
HOT_PS = 30


# ---------------------------------------------------------------- 状态
def _load() -> dict:
    try:
        d = json.loads(STATE.read_text(encoding="utf-8-sig"))
        if isinstance(d, dict):
            d.setdefault("date", "")
            d.setdefault("count", 0)
            d.setdefault("seen", [])
            d.setdefault("history", [])
            return d
    except Exception:  # noqa: BLE001
        pass
    return {"date": "", "count": 0, "seen": [], "history": []}


def _save(d: dict) -> None:
    try:
        d["history"] = (d.get("history") or [])[-200:]
        d["seen"] = (d.get("seen") or [])[-400:]
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[B站刷] 存状态失败：{e}")


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _roll(d: dict) -> dict:
    if d.get("date") != _today():
        d["date"] = _today()
        d["count"] = 0
    return d


def quota_left() -> int:
    d = _roll(_load())
    return max(0, int(_wc()["daily_limit"]) - int(d.get("count") or 0))


def stats() -> dict:
    d = _roll(_load())
    prog = dict(d.get("part_progress") or {})
    titles = dict(d.get("series_title") or {})
    return {"date": d.get("date"), "count": int(d.get("count") or 0),
            "limit": int(_wc()["daily_limit"]), "left": max(0, int(_wc()["daily_limit"]) - int(d.get("count") or 0)),
            "recent": (d.get("history") or [])[-5:],
            "series": [{"bvid": b, "title": titles.get(b, b), "part": p}
                       for b, p in prog.items()]}


# ---------------------------------------------------------------- 选片
def hot_videos(limit: int = HOT_PS) -> list[dict]:
    """B站热门（不需要登录）。"""
    try:
        import httpx
        r = httpx.get("https://api.bilibili.com/x/web-interface/popular",
                      params={"ps": limit, "pn": 1}, timeout=20,
                      headers={"User-Agent": "Mozilla/5.0",
                               "Referer": "https://www.bilibili.com/"})
        j = r.json()
        out = []
        for v in ((j.get("data") or {}).get("list") or []):
            bvid = str(v.get("bvid") or "")
            if not bvid:
                continue
            out.append({
                "bvid": bvid,
                "title": str(v.get("title") or "")[:60],
                "view": int((v.get("stat") or {}).get("view") or 0),
                "url": f"https://www.bilibili.com/video/{bvid}",
                "owner": str(((v.get("owner") or {}).get("name")) or "")[:20],
                "duration": int(v.get("duration") or 0),
            })
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[B站刷] 拉热门失败：{type(e).__name__}: {e}")
        return []


def search_videos(keyword: str, limit: int = 5) -> list[dict]:
    """按名字搜（匿名可用）。"""
    try:
        import httpx
        from plugins import bili_dl
        jar = bili_dl._cookie_dict()
        r = httpx.get("https://api.bilibili.com/x/web-interface/search/type",
                      params={"search_type": "video", "keyword": keyword, "page": 1},
                      timeout=25, cookies=jar or None,
                      headers={"User-Agent": "Mozilla/5.0",
                               "Referer": "https://www.bilibili.com/"})
        j = r.json()
        out = []
        for v in ((j.get("data") or {}).get("result") or []):
            bvid = str(v.get("bvid") or "")
            if not bvid:
                continue
            title = re.sub(r"<[^>]+>", "", str(v.get("title") or ""))[:60]
            out.append({"bvid": bvid, "title": title,
                        "view": int(v.get("play") or 0),
                        "url": f"https://www.bilibili.com/video/{bvid}",
                        "owner": str(v.get("author") or "")[:20],
                        "duration": str(v.get("duration") or "")})
            if len(out) >= limit:
                break
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[B站刷] 搜索失败：{type(e).__name__}: {e}")
        return []


# 2026-11-07：主人说「/B站学习」，随机挑到一条 **4 小时的 Lofi 陪伴学习视频** ——
# 没旁白、没字幕，她只拿到一个标题，于是回了那句
# 「这视频我压根没看着画面也没听着声儿，就一空标题搁这儿，你让我说啥？」。
# 她没编，是这条视频本身没有可学的内容。所以加两道筛：
#   ① 标题像纯音乐/陪伴学习/直播回放的不选
#   ② 超过 25 分钟的不选（长视频字幕抓不全，而且多半是音乐/回放）
_NO_TALK_KW = ("lofi", "lo-fi", "study with me", "纯音乐", "背景音乐", "白噪音",
               "助眠", "asmr", "直播回放", "回放", "音乐合集", "睡眠", "放松音乐",
               "钢琴曲", "雨声", "学习陪伴", "自习室")
_NO_TALK_MAX_SEC = 25 * 60


def _has_talk(v: dict) -> bool:
    """这条视频里有没有「人说话」值得学。"""
    title = str(v.get("title") or "").lower()
    if any(k in title for k in _NO_TALK_KW):
        return False
    dur = int(v.get("duration") or 0)
    if dur and dur > _NO_TALK_MAX_SEC:
        return False
    return True


def pick_of_day(fresh: bool = True) -> dict | None:
    """优先续追没看完的合集（35% 概率），否则今天还没看过的热门里挑。"""
    d = _roll(_load())
    prog = dict(d.get("part_progress") or {})
    titles = dict(d.get("series_title") or {})
    if prog and random.random() < 0.35:
        bvid = random.choice(list(prog.keys()))
        return {"bvid": bvid, "title": titles.get(bvid, bvid),
                "url": f"https://www.bilibili.com/video/{bvid}",
                "view": 0, "owner": "", "duration": 0, "continue": True}
    seen = set(d.get("seen") or [])
    # 关于她自己的视频：按概率优先搜一把（大肥鱼/鲸鱼娘/…），有没看过的就从中挑
    wc = _wc()
    if wc["keywords"] and random.random() < float(wc["keyword_p"]):
        kw = random.choice(wc["keywords"])
        try:
            hits = search_videos(kw, 8)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[B站刷] 搜「{kw}」失败：{type(e).__name__}: {e}")
            hits = []
        fresh = [v for v in hits if v.get("bvid") not in seen]
        if fresh:
            pick = random.choice(fresh)
            pick["via"] = f"关键词「{kw}」"
            logger.info(f"[B站刷] 从关键词「{kw}」挑到：{pick.get('title', '')[:30]}")
            return pick

    cands = [v for v in hot_videos() if v["bvid"] not in seen]
    if not cands:
        cands = hot_videos()
    # 2026-11-07：剔除没有口播内容的（Lofi/陪伴学习/超长回放），筛空了就退回原表
    _talk = [v for v in cands if _has_talk(v)]
    if _talk:
        cands = _talk
    if not cands:
        return None
    cands.sort(key=lambda x: -x.get("view", 0))
    top = cands[: max(1, min(len(cands), 15))]
    return random.choice(top)


# ---------------------------------------------------------------- 看片
_WATCH_UNTIL = 0.0


def watching() -> bool:
    """是不是正在看 B 站视频（给别的模块判断要不要抢活动状态用）。"""
    return time.time() < _WATCH_UNTIL


def _sample_transcript(text: str, budget: int = 2400) -> tuple[str, str]:
    """三段采样：开头 / 中段 / 结尾各取一份，别只看开头就下结论。

    2026-11-05：以前 media_fetch 把字幕切到 400 字，10 分钟的视频她只看到
    开头 13% 就写感想，只能说套话。现在全文在上面，这里按预算抽三段。
    返回 (采样文本, 覆盖说明)——说明会进 prompt，让她知道自己看了多少。
    """
    t = str(text or "").strip()
    n = len(t)
    if not n:
        return "", ""
    if n <= budget:
        return t, f"（全文 {n} 字，已看全）"
    part = max(300, budget // 3)
    head = t[:part]
    mid_start = max(0, n // 2 - part // 2)
    mid = t[mid_start:mid_start + part]
    tail = t[-part:]
    pct = round(min(100, part * 3 * 100 / n))
    return (
        f"【开头】{head}\n【中段】{mid}\n【结尾】{tail}",
        f"（全片口播 {n} 字，这里抽了开头/中段/结尾共约 {pct}%，中间有略过）",
    )


async def _summarize(res: dict) -> str:
    """把媒体分析结果压成一句她能讲出来的话。"""
    title = str(res.get("title") or "").strip()
    desc = str(res.get("desc") or "").strip()
    transcript = str(res.get("transcript") or "").strip()
    sample, cover = _sample_transcript(transcript)
    # 离线质检信号：告诉她这份稿子哪里不可靠，而不是让她自己猜
    probe = res.get("probe") or {}
    flags: list[str] = []
    if probe.get("suspect") == "dup":
        flags.append("字幕重复句很多，可能是口水话或转写错乱，别当有效内容")
    elif probe.get("suspect") == "empty_bands":
        flags.append("字幕中后段基本是空的，可能没转完，别假装看全")
    elif probe.get("suspect") == "mixed_script":
        flags.append("字幕简繁混用，转写可能有误，拿不准就别说细节")
    note = ("\n【这份稿子的问题】" + "；".join(flags)) if flags else ""
    material = (f"标题：{title}\n画面：{desc[:400]}\n"
                f"字幕/口播{cover}：{sample}{note}")
    try:
        from plugins import ai_group
        reply, _meta = await ai_group._chat(
            [{"role": "system", "content": "你在扮演「蓝色大肥鱼」，一只傲娇嘴损但心软的鲸鱼拟人少女。"},
             {"role": "user", "content": f"{material}\n\n用 2 句话（60 字内）说说这视频讲了啥、你什么感想。"
                                        "口语、有态度，不逐条复述，不提模型和系统。"
                                        "只说你真看到的部分，标了抽段或稿子有问题就别补你没看到的。"}],
            temperature=0.9, max_tokens=200, record_scope="bili_daily", channel="system")
        out = str(reply or "").strip()
        if out:
            return out[:200]
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[B站刷] 生成感想失败：{type(e).__name__}: {e}")
    return (desc or title)[:160]


async def _watch_one_part(url: str, bvid: str, meta: dict, part: int,
                          by: str, total: int) -> dict:
    """看合集里的某一集（或单集视频），返回这一集的结果。"""
    from plugins import media_fetch
    from plugins import bili_dl
    real_url = bili_dl.with_part(url, part) if total > 1 else url
    res = await media_fetch.fetch(real_url)
    title = str(res.get("title") or meta.get("title") or "")
    summary = await _summarize(res)
    part_name = ""
    part_cid = 0
    for pg in (meta.get("pages") or []):
        if int(pg.get("page") or 0) == part:
            part_name = str(pg.get("part") or "")
            # 2026-11-05：同一个 BV 的不同分 P 是不同内容，去重要按 cid 而不是只看 bvid
            try:
                part_cid = int(pg.get("cid") or 0)
            except (TypeError, ValueError):
                part_cid = 0
            break
    if total > 1:
        summary = (f"（合集共 {total} 集，这次看的是第 {part} 集"
                   f"{('：' + part_name) if part_name else ''}）") + summary
    # 2026-11-07：把转写字数带出来 —— 上层要判断「这条视频里到底有没有人说话」。
    # 转写为空时她只能对着一个标题回「没内容」，等于白看一轮。
    _tlen = len(str(res.get("transcript") or "").strip())
    return {"title": title, "summary": summary, "part": part,
            "part_name": part_name, "total": total, "url": real_url,
            "cid": part_cid, "tlen": _tlen}


_CC_FILE = ROOT / "memory" / "bili_comment_config.json"


# ---- 观影后的一个问题（2026-11-05 借 dsh-bili-humanism）----
# 看完一集，如果她对这个视频还剩真正没搞懂的地方，就问一句。
# 关键在克制：允许 SKIP、每天最多一次、不为了问而问——凑出来的问题比不问更烦人。
OWNER_QQ_ID = str(os.getenv("OWNER_QQ_ID", "100000000"))
_ASK_FILE = ROOT / "memory" / "bili_post_view_ask.json"


def _ask_used_today() -> int:
    try:
        d = json.loads(_ASK_FILE.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return 0
    return int(d.get("count") or 0) if d.get("date") == _today() else 0


def _ask_mark() -> None:
    try:
        _ASK_FILE.parent.mkdir(parents=True, exist_ok=True)
        _ASK_FILE.write_text(json.dumps(
            {"date": _today(), "count": _ask_used_today() + 1,
             "at": time.strftime("%Y-%m-%d %H:%M:%S")},
            ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


async def _maybe_ask_question(ep: dict) -> str | None:
    """看完一集问她一个开放问题；没真问题就跳过。返回问出去的那句话或 None。"""
    if _ask_used_today() >= 1:
        return None
    title = str(ep.get("title") or "")[:60]
    summary = str(ep.get("summary") or "")[:120]
    if not title or not summary:
        return None
    try:
        from plugins import ai_group
        reply, _meta = await ai_group._chat(
            [{"role": "system",
              "content": "你在扮演「蓝色大肥鱼」，一只傲娇嘴损但心软的鲸鱼拟人少女。"},
             {"role": "user", "content":
              f"你刚看完一个视频：《{title}》\n你写下的感想：{summary}\n\n"
              "如果这视频里还有你真正没搞明白的地方，用一句口语化的问题问出来，"
              "像在跟人聊天，不是提问机器。要求：\n"
              "· 具体指向视频里的某个点，不要「你觉得呢」这种空问题\n"
              "· 不复述视频内容，不客套，不问「还有什么想了解的」\n"
              "· 如果你其实没什么想问的，只输出 SKIP\n"
              "只输出问题本身，或 SKIP。"}],
            temperature=0.95, max_tokens=120,
            record_scope="bili_daily", channel="system")
        q = str(reply or "").strip().split("\n")[0].strip()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[B站刷] 生成提问失败：{type(e).__name__}: {e}")
        return None
    if not q or "SKIP" in q.upper() or len(q) < 5:
        logger.info("[B站刷] 这集没什么真想问的，跳过提问")
        return None
    q = q[:80]
    try:
        from nonebot import get_bot
        await get_bot().send_private_msg(user_id=int(OWNER_QQ_ID), message=q)
        _ask_mark()
        logger.info(f"[B站刷] 看完提问：{q[:40]}")
        return q
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[B站刷] 提问发送失败：{type(e).__name__}: {e}")
        return None


def _cc() -> dict:
    """评论开关：memory/bili_comment_config.json（改文件即生效，不用重启）。"""
    try:
        if _CC_FILE.exists():
            d = json.loads(_CC_FILE.read_text(encoding="utf-8-sig"))
            if isinstance(d, dict):
                return d
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[B站刷] 读评论配置失败：{type(e).__name__}: {e}")
    return {}


async def _try_auto_comment(bvid: str, title: str, summary: str) -> str | None:
    """看完一条后，她自己决定要不要留一句评论；限频由 bilibili_account 把关。"""
    cc = _cc()
    if not cc.get("enabled", True):
        return None
    if not bvid:
        return None
    hay = f"{title} {summary}"
    for w in cc.get("skip_words") or []:
        if w and w in hay:
            logger.info(f"[B站刷] 这条先不评（命中「{w}」）：{title[:30]}")
            return None
    try:
        p = float(cc.get("p", 0.25))
    except Exception:  # noqa: BLE001
        p = 0.25
    if random.random() > p:
        return None
    try:
        from plugins import bilibili_account as acc

        text = await acc.auto_comment(bvid, title, summary, dry=bool(cc.get("dry", True)))
        return text
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[B站刷] 自动评论失败：{type(e).__name__}: {e}")
        return None


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
            # 她自己的评论：定时刷每集都评；手动点看只评第一集（避免合集一次刷 3 条）
            # 2026-11-05：以前只挂 scheduler，主人手动点看永远不评论
            _do_comment = (by == "scheduler") or (by == "owner" and cur == part)
            ep["comment"] = (await _try_auto_comment(
                bvid, ep.get("title") or "", ep.get("summary") or "")
                if _do_comment else None)
            # 看完问一句（每天最多一次，有真问题才问；失败静默，不打断看片）
            if by == "scheduler":
                try:
                    ep["ask"] = await _maybe_ask_question(ep)
                except Exception as e_ask:  # noqa: BLE001
                    logger.debug(f"[B站刷] 提问跳过：{type(e_ask).__name__}: {e_ask}")
            episodes.append(ep)
            logger.info(f"[B站刷] 看完第 {cur}{('/' + str(_total)) if _total > 1 else ''} 集："
                        f"{ep['title'][:30]}")
            # 每看完一集就落一次进度，中途失败也不丢
            d = _roll(_load())
            if by == "scheduler":
                d["count"] = int(d.get("count") or 0) + 1
            if bvid and bvid not in d["seen"]:
                d["seen"].append(bvid)
            # 2026-11-05：分 P 级去重。以前一个合集看完第 1 集整个 BV 就算"看过"，
            # 后面几集在 seen 里永远撞车；现在按 BV:分P（有 cid 时用 cid）单独记。
            _ep_key = (f"{bvid}:P{cur}" if bvid else "")
            if _ep_key:
                _ep_seen = d.setdefault("seen_ep", [])
                if _ep_key not in _ep_seen:
                    _ep_seen.append(_ep_key)
                    d["seen_ep"] = _ep_seen[-800:]
            d["history"].append({"bvid": bvid, "title": ep["title"],
                                 "summary": ep["summary"],
                                 "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                 "by": by, "part": cur, "total": _total,
                                 "cid": ep.get("cid") or 0,
                                 "comment": ep.get("comment")})
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
                "count": d.get("count", 0), "limit": int(_wc()["daily_limit"]), "url": url,
                "episodes": [], "part": part, "total": _total}
    joined = "\n\n".join(f"P{ep['part']}：{ep['summary']}" for ep in episodes) \
        if len(episodes) > 1 else episodes[0]["summary"]
    return {"title": episodes[0]["title"], "summary": joined, "bvid": bvid,
            "count": d.get("count", 0), "limit": int(_wc()["daily_limit"]),
            "url": url, "episodes": episodes, "part": part, "total": _total}


async def watch_one(by: str = "scheduler") -> dict | None:
    """定时任务用：额度还有就看一条。

    2026-11-07 加兜底：挑到的视频如果**转写是空的**（全程没人说话，或字幕没抓到），
    她拿到的只有标题，只能回「这视频我压根没看着画面也没听着声儿」—— 白看一轮。
    这种情况换一条再看，最多试 2 条；两条都空就如实返回，不硬凑。
    """
    if quota_left() <= 0:
        return None
    last: dict | None = None
    for attempt in range(2):
        v = pick_of_day(fresh=(attempt == 0))
        if not v:
            return last
        res = await watch(v["url"], by=by)
        last = res
        _eps = (res or {}).get("episodes") or []
        if not _eps:
            return res
        if any(int(e.get("tlen") or 0) > 0 for e in _eps):
            return res
        logger.info(f"[B站刷] 这条转写是空的（{str(v.get('title'))[:24]}），换一条再看")
    return last


def resolve_target(arg: str) -> str:
    """主人的参数 → 视频 URL。链接直接用；名字搜第一条；空则随机热门。"""
    arg = (arg or "").strip()
    m = re.search(r"https?://\S+", arg)
    if m:
        return m.group(0)
    m = re.search(r"(BV[0-9A-Za-z]{10})", arg)
    if m:
        return f"https://www.bilibili.com/video/{m.group(1)}"
    if arg:
        hits = search_videos(arg, 8)
        if hits:
            kw = re.sub(r"[\s，,。.!！?？]", "", arg)[:8]
            for h in hits:
                if kw and kw in re.sub(r"[\s，,。.!！?？]", "", h.get("title") or ""):
                    return h["url"]
            return hits[0]["url"]
    v = pick_of_day()
    return v["url"] if v else ""


# ---------------------------------------------------------------- 定时（随机时间）
TICK_MIN = int(os.getenv("BILI_TICK_MIN", "20") or "20")
WINDOW = os.getenv("BILI_DAILY_WINDOW", "9-23")


def _window() -> tuple[int, int]:
    try:
        a, b = str(_wc()["window"]).split("-")
        return int(a), int(b)
    except Exception:  # noqa: BLE001
        return 9, 23


def _should_watch_now() -> bool:
    """按"剩余额度 / 剩余窗口"算概率，把 10 条随机撒在白天。"""
    left = quota_left()
    if left <= 0:
        return False
    h0, h1 = _window()
    now = time.localtime()
    if not (h0 <= now.tm_hour < h1):
        return False
    # 窗口内剩余多少个 tick
    minutes_left = (h1 - now.tm_hour) * 60 - now.tm_min
    slots = max(1, int(minutes_left / max(1, TICK_MIN)))
    p = min(1.0, left / slots)
    return random.random() < p


def register_schedule() -> None:
    try:
        from nonebot_plugin_apscheduler import scheduler
    except Exception:  # noqa: BLE001
        return

    # 2026-11-06 按主人要求：去掉「每 20 分钟摇一次」的定时。
    # 看视频改由行动空间的 watch_bili 决定。函数保留（传 by="agency" 可手动调）。
    async def _daily() -> None:
        try:
            if not _should_watch_now():
                return
            r = await watch_one(by="scheduler")
            if r:
                logger.info(f"[B站刷] 随机看第 {r['count']}/{r['limit']} 条：{r['title'][:30]}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[B站刷] 定时看片失败：{type(e).__name__}: {e}")

    _c = _wc()
    logger.info(f"[B站刷] 定时已关（改由行动空间 watch_bili 决定）；"
                f"配额仍生效：每天 {_c['daily_limit']} 条，关键词 {_c['keywords']}")


try:
    register_schedule()
except Exception:  # noqa: BLE001
    pass
