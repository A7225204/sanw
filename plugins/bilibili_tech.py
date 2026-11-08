"""B 站主动技术监测：按关键词扫 AI 陪伴 / 拟人方向最新投稿，命中即留言报备，主人 QQ 拍板再学。

和 `plugins/bilibili_watch.py` 的区别：
* watch 是被动等别人 @ 本鱼、回复本鱼评论；
* tech 是定时主动搜关键词新投稿，命中同类 AI 陪伴 / 拟人视频就去留一条言，再 QQ 私聊问主人要不要学。

开关（.env，改完重启 bot 生效）：
* BILI_TECH_ENABLED      模块总开关，默认 true
* BILI_TECH_INTERVAL     扫描间隔秒，默认 7200（2 小时）
* BILI_TECH_KEYWORDS     监测关键词，逗号分隔；留空用内置「AI 陪伴机器人,AI 拟人,AI 角色对话」
* BILI_TECH_MAX_PER_RUN  单轮最多发现几条新线索，默认 2
* BILI_TECH_MAX_PER_DAY  每天主动留言上限，默认 1（保守额度）
* BILI_TECH_NOTIFY_OWNER 发现线索是否 QQ 私聊报备主人，默认 true
* BILI_TECH_FRESH_HOURS  只收最近多少小时内新投稿，默认 48

主人 QQ 指令（私聊直接发；群里要 @ 本鱼，仅主人号可用）：
* `B站待学`        列出待批准线索
* `B站学 n`        批准第 n 条，联网提炼后入 global_learn 自学档案
* `B站不学 n`      跳过第 n 条
* `B站待学清空`    清空待学清单

状态文件：`.bili_tech_state.json`（已处理 bvid + 待学线索 + 当日留言计数）。
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace as _dc_replace
import os
import re
import sys
import random
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
import nonebot  # noqa: E402
from nonebot import on_message, get_bot  # noqa: E402
from nonebot.exception import FinishedException  # noqa: E402
from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent  # noqa: E402
from nonebot.rule import Rule  # noqa: E402

try:
    from nonebot_plugin_apscheduler import scheduler  # noqa: E402
except Exception:  # standalone 测试 / driver 未初始化时降级，不炸导入
    scheduler = None

from config import get_config  # noqa: E402
from plugins import bilibili_api as bili  # noqa: E402
from plugins import bilibili_account as bac  # noqa: E402
from plugins import long_memory as ltm  # noqa: E402

cfg = get_config()
logger = nonebot.logger

# 运行期覆盖：memory/bili_tech_config.json（改文件即生效，不用动 .env）
_OVR_FILE = ROOT / "memory" / "bili_tech_config.json"


def _apply_tech_override(c):
    """把 JSON 里的字段覆盖到 BiliTechConfig；读不到/写错就原样返回。"""
    try:
        d = json.loads(_OVR_FILE.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        return c
    if not isinstance(d, dict):
        return c
    try:
        from dataclasses import replace as _dc_replace
    except Exception:  # noqa: BLE001
        return c
    fields = {}
    for k in ("enabled", "notify_owner"):
        if k in d:
            fields[k] = bool(d[k])
    for k in ("interval", "max_per_run", "max_per_day", "fresh_hours"):
        if k in d:
            try:
                fields[k] = int(d[k])
            except Exception:  # noqa: BLE001
                pass
    if isinstance(d.get("keywords"), list) and d["keywords"]:
        fields["keywords"] = tuple(str(x).strip() for x in d["keywords"] if str(x).strip())
    if not fields:
        return c
    try:
        return _dc_replace(c, **fields)
    except Exception:  # noqa: BLE001
        return c


try:
    cfg = _dc_replace(cfg, bili_tech=_apply_tech_override(cfg.bili_tech))
except Exception:  # noqa: BLE001 —— 整体不可替换时保持原配置
    pass

OWNER_QQ_ID = str(os.getenv("OWNER_QQ_ID", "100000000"))
OWNER_BILI_NAME = os.getenv("BILI_OWNER_BILI_NAME", "示例主人")
STATE_FILE = ROOT / ".bili_tech_state.json"

client = bac.BiliClient()


# ------------------------------------------------------------------ 状态持久化

def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {"seen": [], "pending": [], "last_day": "", "commented_today": 0}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"seen": [], "pending": [], "last_day": "", "commented_today": 0}
        data.setdefault("seen", [])
        data.setdefault("pending", [])
        data.setdefault("last_day", "")
        data.setdefault("commented_today", 0)
        return data
    except (OSError, ValueError):
        return {"seen": [], "pending": [], "last_day": "", "commented_today": 0}


def _save_state(state: dict) -> None:
    state["seen"] = state["seen"][-1000:]
    state["pending"] = state["pending"][-100:]
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ------------------------------------------------------------------ 登录态 / 搜索

def _my_mid() -> int:
    """自己的 UID：meta 里没有就退回 cookies 的 DedeUserID（老 cookie 文件没有 uid 字段）。"""
    uid = bac.load_account_meta().get("uid")
    if not uid:
        uid = bac.load_cookies().get("DedeUserID")
    try:
        return int(uid or 0)
    except (TypeError, ValueError):
        return 0


async def _ensure_buvid() -> str:
    """B 站搜索要求浏览器指纹（buvid3），没有就会被 -412 风控。

    第一次调 spi 接口拿 buvid3/buvid4，写回 .bili_cookies.json，之后复用。
    """
    cookies = dict(client.cookies or {})
    if cookies.get("buvid3"):
        return str(cookies["buvid3"])
    try:
        async with httpx.AsyncClient(timeout=10.0, headers=bac._headers()) as c:
            r = await c.get("https://api.bilibili.com/x/frontend/finger/spi")
        d = (r.json() or {}).get("data") or {}
        b3 = d.get("b_3") or d.get("buvid3") or ""
        b4 = d.get("b_4") or d.get("buvid4") or ""
        if b3:
            cookies["buvid3"] = b3
            if b4:
                cookies["buvid4"] = b4
            client.cookies = cookies
            try:
                bac.save_cookies(cookies)
            except Exception:  # noqa: BLE001
                pass
            logger.info("[B站技术监测] 已补浏览器指纹 buvid3（搜索不再被 412）")
        return str(b3)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[B站技术监测] 取 buvid3 失败：{type(exc).__name__}: {exc}")
        return ""


async def _search_latest(keyword: str, limit: int = 2) -> list[dict]:
    """登录态 + wbi 签名搜视频，按最新发布排序；风控抛 BiliAccountError。"""
    await _ensure_buvid()
    params = {
        "search_type": "video",
        "keyword": keyword,
        "page": 1,
        "page_size": limit,
        "order": "pubdate",
    }
    signed = await client.sign(params)
    async with httpx.AsyncClient(
        timeout=15.0,
        headers=bac._headers("https://search.bilibili.com/"),
        cookies=client.cookies,
    ) as c:
        resp = await c.get(
            "https://api.bilibili.com/x/web-interface/search/type", params=signed
        )
    data = resp.json()
    code = data.get("code")
    if code not in (0, None):
        msg = data.get("message") or ""
        if code in (-412, -352, -509, -799):
            raise bac.BiliAccountError(f"搜索被风控拦截（{code} {msg}），过一会儿再试")
        raise bac.BiliAccountError(f"搜索失败（{code} {msg}）")
    return (data.get("data") or {}).get("result") or []


FOLLOW_PER_RUN = 8          # 每轮串门看几个 UP
FOLLOW_CACHE_HOURS = 24


async def _following_list(state: dict) -> list[dict]:
    """拿关注列表（缓存 24 小时）。"""
    now = time.time()
    cached = state.get("followings") or []
    if cached and (now - float(state.get("followings_at") or 0)) < FOLLOW_CACHE_HOURS * 3600:
        return cached
    mid = _my_mid()
    if not mid:
        return []
    try:
        async with httpx.AsyncClient(timeout=20.0,
                                     headers=bac._headers("https://space.bilibili.com/%d/fans/follow" % mid),
                                     cookies=client.cookies) as c:
            r = await c.get("https://api.bilibili.com/x/relation/followings",
                            params={"vmid": mid, "pn": 1, "ps": 50})
        d = r.json()
        if d.get("code") != 0:
            logger.warning(f"[B站技术监测] 关注列表失败：{d.get('code')} {d.get('message')}")
            return cached
        lst = [{"mid": int(u.get("mid") or 0), "uname": str(u.get("uname") or "")}
               for u in ((d.get("data") or {}).get("list") or []) if u.get("mid")]
        state["followings"] = lst
        state["followings_at"] = now
        logger.info(f"[B站技术监测] 关注列表已刷新：{len(lst)} 人")
        return lst
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[B站技术监测] 关注列表异常：{type(exc).__name__}: {exc}")
        return cached


async def _following_candidates(state: dict, fresh_cutoff: float, seen: set) -> list[dict]:
    """串门：看关注的 UP 最近发的视频，按最新排序。每轮轮换 FOLLOW_PER_RUN 个。"""
    ups = await _following_list(state)
    if not ups:
        return []
    start = int(state.get("follow_cursor") or 0) % len(ups)
    picked = [ups[(start + i) % len(ups)] for i in range(min(FOLLOW_PER_RUN, len(ups)))]
    state["follow_cursor"] = (start + len(picked)) % len(ups)
    my_mid = _my_mid()
    out: list[dict] = []
    for up in picked:
        try:
            params = {"mid": int(up["mid"]), "ps": 2, "pn": 1, "order": "pubdate", "keyword": ""}
            signed = await client.sign(dict(params))
            async with httpx.AsyncClient(
                timeout=20.0,
                headers=bac._headers("https://space.bilibili.com/%d/video" % int(up["mid"])),
                cookies=client.cookies,
            ) as c:
                r = await c.get("https://api.bilibili.com/x/space/wbi/arc/search", params=signed)
            d = r.json()
            code = d.get("code")
            if code in (-412, -352, -509, -799):
                logger.warning(f"[B站技术监测] 串门被风控（{code}），本轮停止串门")
                break
            if code != 0:
                logger.debug(f"[B站技术监测] 串门 {up['uname']} 失败：{code}")
                continue
            for v in (((d.get("data") or {}).get("list") or {}).get("vlist") or []):
                bvid = str(v.get("bvid") or "")
                pub = int(v.get("created") or 0)
                if not bvid or bvid in seen or pub < fresh_cutoff:
                    continue
                if my_mid and int(up["mid"]) == my_mid:
                    continue
                out.append({
                    "bvid": bvid,
                    "title": bili.strip_em(v.get("title") or ""),
                    "author": up["uname"] or "未知 UP",
                    "author_mid": int(up["mid"]),
                    "play": v.get("play"),
                    "pubdate": pub,
                    "keyword": "关注·" + (up["uname"] or "UP"),
                    "source": "following",
                })
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[B站技术监测] 串门 {up.get('uname')} 异常：{type(exc).__name__}: {exc}")
        await asyncio.sleep(2 + random.uniform(0, 2))
    return out


async def _collect_candidates(state: dict) -> list[dict]:
    """遍历关键词收集新鲜候选，返回标准化线索列表。"""
    now = time.time()
    fresh_cutoff = now - cfg.bili_tech.fresh_hours * 3600
    seen = set(state["seen"])
    my_mid = _my_mid()
    out: list[dict] = []

    # 被风控过就退避 3 小时，期间不再打搜索接口
    if float(state.get("search_blocked_until") or 0) > now:
        left = int((float(state["search_blocked_until"]) - now) / 60)
        logger.info(f"[B站技术监测] 搜索风控冷却中，还有 {left} 分钟")
        return []

    # 先串门：关注的人发新视频优先于关键词搜索（也更像"她自己刷 B站"）
    follow = await _following_candidates(state, fresh_cutoff, seen)
    if follow:
        follow.sort(key=lambda x: x["pubdate"], reverse=True)
        out.extend(follow[:1])          # 每轮最多串门 1 条，别把关键词线索挤掉
        logger.info(f"[B站技术监测] 串门发现 {len(follow)} 条新视频")
        if len(out) >= cfg.bili_tech.max_per_run:
            out.sort(key=lambda x: x["pubdate"], reverse=True)
            return out[:cfg.bili_tech.max_per_run]

    kws = list(cfg.bili_tech.keywords or [])
    if not kws:
        return out
    # 每轮只打 3 个关键词（轮换起点），别把 10 个一口气刷完
    cap = min(3, len(kws))
    start = int(state.get("kw_cursor") or 0) % len(kws)
    picked = [kws[(start + i) % len(kws)] for i in range(cap)]
    state["kw_cursor"] = (start + cap) % len(kws)
    logger.info(f"[B站技术监测] 本轮关键词：{'、'.join(picked)}")

    for kw in picked:
        try:
            rows = await _search_latest(kw, limit=2)
        except bac.BiliAccountError as exc:
            msg = str(exc)
            if "-412" in msg or "412" in msg or "风控" in msg:
                state["search_blocked_until"] = now + 3 * 3600
                logger.warning(f"[B站技术监测] 命中风控，搜索暂停 3 小时：{msg}")
                break
            logger.warning(f"[B站技术监测] 关键词「{kw}」搜索失败：{msg}")
            await asyncio.sleep(8)
            continue
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[B站技术监测] 关键词「{kw}」未预期错误：{type(exc).__name__}: {exc}")
            await asyncio.sleep(4)
            continue

        for item in rows:
            bvid = item.get("bvid") or ""
            pubdate = int(item.get("pubdate") or 0)
            author_mid = int(item.get("mid") or item.get("author_mid") or 0)
            if not bvid or bvid in seen:
                continue
            if pubdate < fresh_cutoff:
                continue
            if my_mid and author_mid == my_mid:
                continue
            out.append({
                "bvid": bvid,
                "title": bili.strip_em(item.get("title") or ""),
                "author": item.get("author") or "未知 UP",
                "author_mid": author_mid,
                "play": item.get("play"),
                "pubdate": pubdate,
                "keyword": kw,
            })
        # 关键词之间随机放慢（6~11 秒）：固定节奏反而容易被识别
        await asyncio.sleep(6 + random.uniform(0, 5))

    # 新的排前面
    out.sort(key=lambda x: x["pubdate"], reverse=True)
    return out[:cfg.bili_tech.max_per_run]


# ------------------------------------------------------------------ 留言生成与发送

async def _make_comment(scene_title: str, author: str, desc: str, keyword: str) -> str:
    _stray = str(keyword or "").startswith("关注·")
    head = ("你是傲娇的大肥鱼，刷到关注的 UP 主发了新视频，随口在评论区搭一句。"
            if _stray else
            "你是傲娇的大肥鱼，在 B 站评论区刷到一个 AI 陪伴 / 拟人方向的新视频，随口搭一句。")
    system = (
        head
        + "25~40 字，口语、有梗、别客套、别夸得太假；"
        + "不要 @ 任何人、不要喊人来看、不要提主人；"
        + "别提 AI、机器人、模型、训练、技术名词；不要发链接、不要分点、不要自我介绍。"
    )
    context = (
        "视频标题：" + str(scene_title) + chr(10)
        + "UP 主：" + str(author) + chr(10)
        + "简介：" + str(desc or "")[:200] + chr(10)
        + "监测方向：" + str(keyword) + chr(10)
        + "写一条主评论：对视频内容随口吐槽或接一句，别喊人来看。"
    )
    text = await bac.generate_text(system, context)
    return _clean_comment(text)


def _clean_comment(text: str) -> str:
    """把模型输出收拾成一条能发出去的评论：去 @、去表情标记、去空标点、去多余句号。"""
    t = str(text or "")
    t = re.sub(r"\[表情[:：][^\]]*\]", "", t)
    t = re.sub(r"@\S+", "", t)
    for _w in ("快来看", "来看看", "看看呗", "过来看", "，看看"):
        t = t.replace(_w, "")
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"([，,。.!！?？])\s*([，,。.!！?？])+", lambda m: m.group(1), t)
    t = re.sub(r"([，,、])\s*([。.!！?？])", lambda m: m.group(2), t)
    t = t.strip(" ，,、;；:：")
    if not re.search(r"[。.!！?？]$", t):
        t += "。"
    return t


async def _process_candidates(state: dict, candidates: list[dict]) -> list[dict]:
    """逐条生成留言；额度内自动发，额度外只报备；线索入待学清单。"""
    today = time.strftime("%Y-%m-%d")
    if state["last_day"] != today:
        state["last_day"] = today
        state["commented_today"] = 0

    blocks: list[dict] = []
    for cand in candidates:
        try:
            aid, info = await client.video_aid(cand["bvid"])
            cand["aid"] = aid
            cand["uri"] = f"https://www.bilibili.com/video/{cand['bvid']}"
            cand["desc"] = info.get("desc") or ""
        except bac.BiliAccountError as exc:
            logger.warning(f"[B站技术监测] 《{cand['title']}》取详情失败：{exc}")
            state["seen"].append(cand["bvid"])
            continue

        # 先按"下载→抽帧→音频转写"把视频看一遍（有缓存就直接用），再写留言
        try:
            from plugins import media_fetch
            _mf = await media_fetch.fetch(cand["uri"], with_audio=True)
            if _mf:
                _extra = []
                if _mf.get("desc"):
                    _extra.append("画面：" + str(_mf["desc"])[:120])
                if _mf.get("transcript"):
                    _extra.append("口播：" + str(_mf["transcript"])[:150])
                if _extra:
                    cand["desc"] = (cand.get("desc", "") + "\n" + "\n".join(_extra)).strip()
                logger.info(f"[B站技术监测] 视频解析 cached={bool(_mf.get('cached'))} "
                            f"画面={bool(_mf.get('desc'))} 口播={bool(_mf.get('transcript'))}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[B站技术监测] 视频解析失败：{type(e).__name__}: {e}")
        message = await _make_comment(
            info.get("title") or cand["title"],
            (info.get("owner") or {}).get("name") or cand["author"],
            cand["desc"],
            cand["keyword"],
        )

        commented = False
        note = ""
        # 当日主动留言额度 + 通用限频
        # 2026-11-07：**必须传 kind**。
        # `KIND_DAILY_CAP["tech_comment"] = 10` 早就按主人要求改成 10 了，
        # 但这里一直没传 kind，所以那条分类型上限从来没生效过 ——
        # 实际卡住的是下面 `cfg.bili_tech.max_per_day`（当时是 1）。
        # 主人看到「今日主动留言额度已用满（≤2）」还以为是 2，其实只有 1。
        limit_reason = bac.check_limit(cand["bvid"], today, kind="tech_comment")
        if state["commented_today"] >= cfg.bili_tech.max_per_day:
            note = f"今日主动留言额度已用满（≤{cfg.bili_tech.max_per_day}），本轮只报备不留言"
        elif limit_reason:
            note = limit_reason
        elif message in bac.recent_texts():
            note = "留言内容刚发过，不重复发"
        else:
            try:
                # 红线审核：公开发布前过一遍，命中就跳过不发
                try:
                    from plugins import moderation
                    _safe = moderation.filter_public(message, channel="bilibili")
                    if not _safe:
                        state["seen"].append(cand["bvid"])
                        logger.info(f"[B站技术监测] 评论被审核拦截，跳过《{cand['title'][:20]}》")
                        continue
                    message = _safe
                except Exception:  # noqa: BLE001
                    pass
                _rpid = await client.post_comment(aid, cand["bvid"], message)
                try:
                    state.setdefault("posted", []).append(
                        {"bvid": cand["bvid"], "aid": aid, "rpid": int(_rpid or 0),
                         "text": message, "at": time.strftime("%Y-%m-%d %H:%M:%S")})
                    state["posted"] = state["posted"][-50:]
                except Exception:  # noqa: BLE001
                    pass
                bac.record_action(cand["bvid"], "tech_comment", message)
                state["commented_today"] += 1
                commented = True
            except bac.BiliAccountError as exc:
                note = f"留言失败：{exc}"
                logger.warning(f"[B站技术监测] 《{cand['title']}》留言失败：{exc}")

        entry = {
            "bvid": cand["bvid"],
            "aid": cand.get("aid"),
            "title": info.get("title") or cand["title"],
            "author": (info.get("owner") or {}).get("name") or cand["author"],
            "uri": cand["uri"],
            "keyword": cand["keyword"],
            "comment": message,
            "commented": commented,
            "note": note,
            "pubdate": cand["pubdate"],
            "at_time": int(time.time()),
        }
        state["pending"].append(entry)
        state["seen"].append(cand["bvid"])
        blocks.append(entry)

        # 主评论之间慢一拍
        if commented:
            await asyncio.sleep(3)

    return blocks


# ------------------------------------------------------------------ 报备文本

def _render_report(entries: list[dict]) -> str:
    if not entries:
        return ""
    lines = [f"🐟 B站刷到 {len(entries)} 条 AI 陪伴 / 拟人新线索："]
    for idx, e in enumerate(entries, 1):
        tag = "✅已留言" if e["commented"] else "📝仅报备"
        head = f"{idx}. [{tag}] 《{e['title']}》"
        lines.append(head)
        lines.append(f"   UP：{e['author']} ｜ {e['uri']}")
        if e.get("note"):
            lines.append(f"   备注：{e['note']}")
        if e["commented"]:
            # 2026-11-07 主人：「她想留言就留言吧，不需要我确认，告诉我就行」。
            # 已留言的条目直接报告结果，不再挂「要学这个回…」——
            # 那行是「要不要把这条知识入库」的选择，不是「要不要发评论」，
            # 但放在已留言条目下面容易被读成「等你点头」。
            lines.append(f"   已留言：{e['comment']}")
            lines.append("   （已直接发出，不用你确认）")
        else:
            lines.append(f"   拟留言：{e['comment']}")
            lines.append("   （这条没发：见上面的备注）")
        # 入库选择只对"值得学"的条目问，和发不发评论无关
        lines.append("   要把这条记进知识库回「B站学 序号」，不回就不记")
    return "\n".join(lines)


async def _notify_owner(text: str) -> None:
    if not text.strip():
        return
    try:
        await get_bot().send_private_msg(user_id=int(OWNER_QQ_ID), message=text)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[B站技术监测] 报备主人失败：{exc}")


# ------------------------------------------------------------------ 定时扫描

async def _run_tech_scan() -> str:
    # 2026-11-07：**刷 B 站的时候要让面板知道**。
    # 原来这个函数从头到尾没碰过 activity_state —— 她明明在网上翻视频、
    # 还往私聊报了 2 条线索，面板「现在：」那行却一直写「发呆」。
    # 主人问「和她发消息说去B站了，网页全息仪表盘没同步」，就是这条。
    # TTL 给 10 分钟：一轮扫描要搜、要解析、要识图，实测 15:52:02→15:53:51 约 110 秒，
    # 但中途还会下视频、调 VLM，留足余量；扫完主动清掉（见下面的 finally）。
    _act = None
    try:
        from plugins.activity import set_activity as _set_act
        _set_act("在 B 站刷线索", 600, detail="搜 AI 陪伴 / 拟人投稿")
        _act = _set_act
    except Exception:  # noqa: BLE001
        pass

    state = _load_state()
    client.refresh()
    if not client.logged_in:
        if _act:
            try: _act("发呆", 1)      # 没登录就别挂着「在刷 B 站」
            except Exception: pass
        return "⚠️ B 站还没登录，私聊我发 `B站登录`（技术监测需要登录态搜索）。"

    try:
        candidates = await _collect_candidates(state)
    except bac.BiliAccountError as exc:
        logger.warning(f"[B站技术监测] 拉取候选失败：{exc}")
        if _act:
            try: _act("发呆", 1)
            except Exception: pass
        return ""

    if not candidates:
        _save_state(state)
        logger.info("[B站技术监测] 本轮无新的 AI 陪伴 / 拟人投稿")
        if _act:
            try: _act("发呆", 1)
            except Exception: pass
        return ""

    entries = await _process_candidates(state, candidates)
    _save_state(state)

    report = _render_report(entries)
    if report and cfg.bili_tech.notify_owner:
        logger.info(f"[B站技术监测] 本轮 {len(entries)} 条线索，已报备主人")
    # 扫完了：把活动状态让回去（不写死成「发呆」太久，1 秒 TTL 让下一次聊天自然覆盖）
    if _act:
        try: _act("刚刷完 B 站", 180)
        except Exception: pass
    return report


if cfg.bili_tech.enabled and scheduler is not None:
    logger.info("[B站技术监测] 已注册定时任务：每 {} 秒扫新投稿，每天最多 {} 条留言".format(
        cfg.bili_tech.interval, cfg.bili_tech.max_per_day))

    @scheduler.scheduled_job(
        "interval",
        seconds=cfg.bili_tech.interval,
        id="bili_tech_scan",
    )
    async def _tech_job() -> None:
        try:
            report = await _run_tech_scan()
            if report:
                await _notify_owner(report)
            else:
                logger.info("[B站技术监测] 本轮无新线索")
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[B站技术监测] 未预期错误：{exc}")
elif not cfg.bili_tech.enabled:
    logger.info("[B站技术监测] BILI_TECH_ENABLED=false，定时任务未注册")
else:
    logger.info("[B站技术监测] 调度器不可用，仅保留主人手动指令")


# ------------------------------------------------------------------ 主人手动指令

def _plain(event) -> str:
    text = (event.get_plaintext() or "").strip()
    return re.sub(r"^(@\S+\s*)+", "", text).strip()


def _is_owner(event) -> bool:
    return str(getattr(event, "user_id", "")) == OWNER_QQ_ID


def _check(event) -> bool:
    if not _is_owner(event):
        return False
    if isinstance(event, GroupMessageEvent):
        if not event.is_tome():
            return False
    elif not isinstance(event, PrivateMessageEvent):
        return False
    text = _plain(event)
    return text.startswith((
        "B站待学", "B站学", "B站不学",
    ))


tech_matcher = on_message(rule=Rule(_check), priority=8, block=True)


def _pending_index(state: dict, raw: str) -> int | None:
    m = re.search(r"(?:B站学|B站不学)\s*(\d+)", raw)
    return int(m.group(1)) - 1 if m else None


@tech_matcher.handle()
async def _(event) -> None:
    text = _plain(event)
    state = _load_state()

    try:
        if text.startswith("B站待学清空"):
            state["pending"] = []
            _save_state(state)
            await tech_matcher.finish("待学清单已清空。")

        if text.startswith("B站待学"):
            pending = state["pending"]
            if not pending:
                await tech_matcher.finish("待学清单是空的，还没刷到需要你拍板的新视频。")
            lines = ["📝 B站待学清单："]
            for idx, e in enumerate(pending, 1):
                tag = "已留言" if e["commented"] else "仅报备"
                lines.append(
                    f"{idx}. [{tag}] 《{e['title']}》\n"
                    f"   UP：{e['author']} ｜ {e['uri']}\n"
                    f"   留言：{e['comment']}"
                )
            lines.append("批准：`B站学 序号`；跳过：`B站不学 序号`")
            await tech_matcher.finish("\n".join(lines))

        if text.startswith("B站不学"):
            idx = _pending_index(state, text)
            pending = state["pending"]
            if idx is None or not (0 <= idx < len(pending)):
                await tech_matcher.finish("格式：`B站不学 序号`（先 `B站待学` 看编号）")
            skipped = pending.pop(idx)
            _save_state(state)
            await tech_matcher.finish(f"⏭️ 已跳过《{skipped['title']}》，不进自学档案。")

        if text.startswith("B站学"):
            idx = _pending_index(state, text)
            pending = state["pending"]
            if idx is None or not (0 <= idx < len(pending)):
                await tech_matcher.finish("格式：`B站学 序号`（先 `B站待学` 看编号）")
            entry = pending[idx]

            # 联网提炼成事实短句，主人批准后才进 global_learn
            from plugins import web_search as ws  # 延迟导入，避免顶层循环依赖
            prompt = (
                f"B 站 AI 陪伴 / 拟人方向新视频：《{entry['title']}》，UP：{entry['author']}。"
                f"链接：{entry['uri']}\n"
                "请联网检索这条视频相关的公开讨论和做法，用 1~2 句大白话总结值得本鱼学到的要点"
                "（语气、情绪、记忆、反应节奏、人设关系等任选相关讲）。"
                "只给事实和做法：不要评论站队、不要推荐具体产品、不要新闻腔调、不要链接、不要分点，60 字以内。"
            )
            res = await ws.web_chat([{"role": "user", "content": prompt}])
            if not res:
                await tech_matcher.finish("联网提炼失败，线索先留在清单里，稍后再发 `B站学 序号` 重试。")
            fact, _meta = res
            fact = fact.strip().replace("\n", " ")
            if len(fact) < 6:
                await tech_matcher.finish("提炼内容太短，线索先留在清单里，稍后再试。")

            ltm.add_pending(
                "global_learn", "自学·B站新技术",
                f"{entry['title']}：{fact}"[:120],
            )
            await ltm.consolidate("global_learn")
            pending.pop(idx)
            _save_state(state)
            await tech_matcher.finish(
                f"✅ 已批准并学进档案：《{entry['title']}》\n> {fact}"
            )

    except bac.BiliAccountError as exc:
        await tech_matcher.finish(f"❌ {exc}")
    except FinishedException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"[B站技术监测指令] 未预期错误：{exc}")
        await tech_matcher.finish("B站这边出错了，看看后台日志。")
