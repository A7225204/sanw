"""B 站消息轮询：监听评论区 @ 本鱼、别人回复本鱼评论的消息，预览报备或自动回评。



和 `plugins/bilibili_account.py` 的区别：

* account 是主人在 QQ 下指令，机器人去指定视频发评论；

* watch 是定时主动拉 B 站消息中心（@我 / 回复我），有人搭话就接住话头。



开关（.env，改完重启 bot 生效）：

* BILI_WATCH_ENABLED        轮询总开关，默认 true

* BILI_WATCH_INTERVAL       轮询间隔秒，默认 60（15~600）

* BILI_WATCH_REPLY_ENABLED  true=自动回评；false=只把新消息和拟回复文案推给主人预览（dry-run）

* BILI_WATCH_NOTIFY_OWNER   有新消息是否报备主人 QQ 私聊，默认 true

* BILI_WATCH_MAX_PER_RUN    单轮最多处理几条，默认 2（1~5）

* BILI_WATCH_REPLY_FEED     是否监听「别人回复本鱼评论」，默认 true；false 时只监听 @ 本鱼

* BILI_WATCH_REPLY_CHANCE   命中事件后随机回复概率（0~1），默认 0.5；未命中静默不回



主人 QQ 指令（私聊直接发；群里要 @ 本鱼，仅主人号可用）：

* `B站看新消息` / `B站看新@`  手动拉一轮新消息（不等定时）

* `B站最近回复`（`B站历史回复`）列出最近已回评的人、视频标题和网页链接

* `B站草稿`    列出预览模式攒下的待回草稿

* `B站补发 n`  按草稿序号补发一条评论

* `B站清空草稿` 清空草稿箱



状态文件：`.bili_watch_state.json`（已处理事件 id + 待回草稿 + 最近回评记录）。

"""

from __future__ import annotations



import asyncio

import json

import os

import random

import re

import sys

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

from memory_manager import add_memory  # noqa: E402

from long_memory import save_daily  # noqa: E402



cfg = get_config()

logger = nonebot.logger



OWNER_QQ_ID = str(os.getenv("OWNER_QQ_ID", "100000000"))

# 主人 B 站 mid（「示例主人」）；主人 @ 她 / 回复她评论时必回，不走随机概率

OWNER_BILI_MID = int(os.getenv("BILI_OWNER_MID", "94255764") or 0)

STATE_FILE = ROOT / ".bili_watch_state.json"



client = bac.BiliClient()





# ------------------------------------------------------------------ 状态持久化



def _load_state() -> dict:

    if not STATE_FILE.exists():

        return {"seen": [], "pending": [], "recent_replies": []}

    try:

        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))

        if not isinstance(data, dict):

            return {"seen": [], "pending": [], "recent_replies": []}

        data.setdefault("seen", [])

        data.setdefault("pending", [])

        data.setdefault("recent_replies", [])

        return data

    except (OSError, ValueError):

        return {"seen": [], "pending": [], "recent_replies": []}





def _save_state(state: dict) -> None:

    state["seen"] = state["seen"][-500:]

    state["pending"] = state["pending"][-100:]

    state["recent_replies"] = state["recent_replies"][-20:]

    STATE_FILE.write_text(

        json.dumps(state, ensure_ascii=False, indent=2),

        encoding="utf-8",

    )





# ------------------------------------------------------------------ 拉取消息中心



def _log_poll_exc(exc: BaseException, where: str = "") -> None:
    """轮询异常分级：网络抖动不算事故。

    2026-11-07：原来一律 logger.exception —— 实测 7 次里 7 次都是
    httpx.ConnectTimeout（凌晨网络抖动），每次都刷一整套堆栈，把真错误淹了。

    连接/读取/超时/远端断开 → warning，一行说清楚；
    其他异常 → 仍然 exception（带堆栈），那才是真 bug。
    """
    name = type(exc).__name__
    msg = str(exc)[:120]
    net_like = any(k in name for k in (
        "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout",
        "ConnectError", "ReadError", "RemoteProtocolError",
        "NetworkError", "TimeoutException",
    ))
    if net_like:
        logger.warning(f"[B站消息轮询] {where} 网络抖动（{name}）：{msg}")
    else:
        logger.exception(f"[B站消息轮询] {where} 未预期错误：{msg}")


async def _fetch_feed_items(kind: str) -> list[dict]:

    """拉 B 站消息中心。kind='at' 拉 @我；kind='reply' 拉回复我的评论。"""

    if not client.logged_in:

        raise bac.BiliAccountError("还没登录 B 站，私聊我发 `B站登录`")

    endpoint = {"at": "x/msgfeed/at", "reply": "x/msgfeed/reply"}[kind]

    async with httpx.AsyncClient(timeout=15.0, headers=bac._headers(), cookies=client.cookies) as c:

        resp = await c.get(

            f"https://api.bilibili.com/{endpoint}",

            params={"ps": 10, "fresh_type": 1},

        )

        data = resp.json()

    code = data.get("code")

    if code not in (0, None):

        msg = data.get("message") or ""

        if code in (-412, -352, -509, -799):

            raise bac.BiliAccountError(f"B站风控拦截（{code} {msg}），过一会儿再试")

        raise bac.BiliAccountError(f"拉取消息失败（{endpoint} {code} {msg}）")

    return (data.get("data") or {}).get("items") or []





def _parse_native_uri(native_uri: str) -> tuple[int, int]:

    """从 bilibili://video/<aid>?comment_root_id=<r>&comment_parent_id=<p> 兜底解析楼层。"""

    root = parent = 0

    m = re.search(r"comment_root_id=(\d+)", native_uri or "")

    if m:

        root = int(m.group(1))

    m = re.search(r"comment_parent_id=(\d+)", native_uri or "")

    if m:

        parent = int(m.group(1))

    return root, parent





def _parse_event(item: dict, feed: str) -> dict | None:

    """把消息中心条目标准化；无效条目返回 None。feed='at' / 'reply'。"""

    user = item.get("user") or {}

    ev_item = item.get("item") or {}

    if ev_item.get("hide_reply_button"):

        return None



    source = (ev_item.get("source_content") or "").strip()

    title = (ev_item.get("title") or "").strip()

    if not source and not title:

        return None



    uri = ev_item.get("uri") or ""

    bvid = bili.extract_bvid(uri) or bili.extract_bvid(source)

    source_id = int(ev_item.get("source_id") or 0)

    root_id = int(ev_item.get("root_id") or 0)

    native_root, native_parent = _parse_native_uri(ev_item.get("native_uri") or "")

    root = root_id or native_root

    parent = source_id or native_parent or root

    if feed == "reply":

        # 回复通知：root 是楼层根评论，parent 是被回复的那条评论

        root = root_id or native_root

        parent = source_id or native_parent or root



    return {

        "event_id": str(item.get("id")),

        "feed": feed,

        "mid": user.get("mid"),

        "nickname": user.get("nickname") or "匿名",

        "source": source,

        "title": title,

        "uri": uri,

        "bvid": bvid,

        "root": root,

        "parent": parent,

        "business": ev_item.get("business") or "",

        "type": ev_item.get("type") or "",

        "at_time": item.get("at_time") or 0,

    }





def _my_mid() -> int | None:

    return int(bac.load_account_meta().get("uid") or 0) or None





def _clean_bili_text(text: str, nickname: str = "") -> str:

    """清理 B 站评论正文：剥 QQ 表情标记、剥开头手写的 @ 昵称。"""

    t = (text or "").strip()

    t = re.sub(r"\[表情[:：]\s*[^\]]*\]", "", t)

    if nickname:

        t = re.sub(rf"^@\s*{re.escape(nickname)}\s*", "", t)

    t = re.sub(r"^@\S+\s*", "", t)

    return t.strip()





# ------------------------------------------------------------------ 单条处理



async def _process_one(ev: dict, state: dict, is_owner: bool = False) -> dict:

    """处理一条新消息，返回结果摘要字典。"""

    result: dict = {"status": "", "ev": ev}



    my_mid = _my_mid()

    if my_mid and ev["mid"] == my_mid:

        result["status"] = "skip_self"

        return result



    if not ev["bvid"]:

        result["status"] = "skip_no_video"

        result["reason"] = "定位不到视频链接"

        return result



    aid, info = await client.video_aid(ev["bvid"])

    result["aid"] = aid

    result["info"] = info



    # ---- 组装视频情境素材（拿不到的字段留空，绝不编造）----

    v_title = (info.get("title") or ev["title"] or "").strip()

    owner_name = (info.get("owner") or {}).get("name") or ""

    stat = info.get("stat") or {}

    desc = re.sub(r"\s+", " ", (info.get("desc") or "")).strip()

    if desc in ("-", "—"):

        desc = ""

    if len(desc) > 80:

        desc = desc[:80] + "…"

    tname = (info.get("tname") or "").strip()

    duration = bili.fmt_duration(info.get("duration"))

    view = bili.fmt_count(stat.get("view"))

    like = bili.fmt_count(stat.get("like"))

    danmaku = bili.fmt_count(stat.get("danmaku"))



    # 对方评论：剥掉开头手写 @ 昵称；空了就标注没留文字

    source_clean = _clean_bili_text(ev["source"] or "", ev["nickname"])



    video_lines = [f"视频标题：《{v_title}》"]

    if owner_name:

        video_lines.append(f"UP 主：{owner_name}")

    meta_bits = []

    if duration and duration != "0:00":

        meta_bits.append(f"时长 {duration}")

    if view != "0":

        meta_bits.append(f"播放 {view}")

    if like != "0":

        meta_bits.append(f"点赞 {like}")

    if danmaku != "0":

        meta_bits.append(f"弹幕 {danmaku}")

    if meta_bits:

        video_lines.append("视频数据：" + "、".join(meta_bits))

    if tname:

        video_lines.append(f"分区：{tname}")

    if desc:

        video_lines.append(f"简介：{desc}")

    video_block = "\n".join(video_lines)



    if source_clean:

        who_line = f"对方（B 站昵称：{ev['nickname']}）说：{source_clean}"

    else:

        who_line = f"对方（B 站昵称：{ev['nickname']}）只 @ 了你，没留下具体文字"



    if ev["feed"] == "reply":

        # 回复通知：优先延续对方评论的话题，标题/简介只作语境补充

        scene = (

            f"【视频信息】\n{video_block}\n\n"

            f"【对方的话】\n{who_line}（这是对方在你评论下面回复你的话，优先延续这条评论的话题）"

        )

        task = (

            "写一句 30~45 字的评论区回复：先接住对方这条评论的话题往下聊，"

            "再结合视频标题和简介顺一句；对方说得有意思就接梗吐槽，认同就附和，别冷场。\n"

            "硬性要求：不要自我介绍、别提 AI 或机器人、不要长文；"

            "不要在正文里 @ 对方昵称（楼层 @ 由系统自动带上）；"

            "不要原样整句念标题，可以化用标题里的关键词；"

            "不知道 UP 主名字或分区就不要瞎编；不要发链接。"

        )

    elif is_owner:

        # 主人 @ 本鱼：先看标题接住情绪和话题，撒娇接梗，不反问视频内容、不催重点

        scene = (

            f"【视频信息】\n{video_block}\n\n"

            f"【对方的话】\n{who_line}"

        )

        task = (

            "写一句 30~45 字的评论区回复：这是主人 @ 你，先看视频标题，"

            "接住标题里的情绪和话题，用撒娇、傲娇、软萌的语气接梗回应；对方评论里有具体话就优先接那句话。\n"

            "硬性要求：不要反问「视频里是什么」、不要催主人说重点；"

            "不要自我介绍、别提 AI 或机器人、不要长文；"

            "不要在正文里 @ 对方昵称（楼层 @ 由系统自动带上）；"

            "不要原样整句念标题，可以化用标题里的关键词；"

            "不知道 UP 主名字或分区就不要瞎编；不要发链接。"

        )

    else:

        # 普通网友 @：先接标题话题；有具体评论接评论；空 @ 时结合标题轻回一句再催重点

        scene = (

            f"【视频信息】\n{video_block}\n\n"

            f"【对方的话】\n{who_line}"

        )

        task = (

            "写一句 30~45 字的评论区回复：先看视频标题，接住标题里的话题自然搭一句；"

            "对方评论里有具体话就优先接他说的话、接梗吐槽；"

            "对方只是空 @、没说内容时，结合标题轻轻回一句，再傲娇地催他说重点，不要反问「视频里是什么」。\n"

            "硬性要求：不要自我介绍、别提 AI 或机器人、不要长文；"

            "不要在正文里 @ 对方昵称（楼层 @ 由系统自动带上）；"

            "不要原样整句念标题，可以化用标题里的关键词；"

            "不知道 UP 主名字或分区就不要瞎编；不要发链接。"

        )

    context = scene + "\n\n" + task



    persona = ""

    mood = ""

    try:

        from plugins.ai_group import CURRENT_DAILY_MOOD, emotion_prompt, just_woke_prompt, meal_prompt

        try:

            persona = (ROOT / "persona.md").read_text(encoding="utf-8")[:900]

        except Exception:

            persona = "圆滚滚的蓝色鲸鱼拟人少女，傲娇嘴损、黏主人。"

        mood = (f"当前状态：{CURRENT_DAILY_MOOD}；情绪：{emotion_prompt()}；"

                f"{just_woke_prompt().strip() or '清醒'}；{meal_prompt()}")

    except Exception:

        pass

    draft = await bac.generate_text(

        "你是大肥鱼，在 B 站评论区回跟你搭话的人。"

        f"人设：{persona}\n{mood}\n"

        "像真人网友随口回一句：有语气词、有情绪、可以接梗吐槽或撒娇，别每条都工整完美；"

        "不要长篇大论，不要正经介绍自己，不要提 AI。",

        context,

    )

    # 剥掉 QQ 专用表情标记与开头手写的 @ 昵称（B 站楼层 @ 由系统自动带）

    draft = _clean_bili_text(draft, ev["nickname"])

    # 经济模型偶尔会敷衍成几个字，太短就带更强约束重生一次兜底

    if len(draft) < 10:

        draft = await bac.generate_text(

            "你是傲娇的大肥鱼，在 B 站评论区回跟你搭话的人。不许只发两三个字，必须写满 30 字以上。",

            context + "\n注意：这次回复必须超过 30 个字，把话头接起来，正文不要 @ 任何人。",

        )

        draft = _clean_bili_text(draft, ev["nickname"])

    result["draft"] = draft



    if cfg.bili_watch.reply_enabled:

        reason = bac.check_limit(ev["bvid"], time.strftime("%Y-%m-%d"), kind="reply")

        if reason:

            result["status"] = "skip_limit"

            result["reason"] = reason

            return result

        if draft in bac.recent_texts():

            result["status"] = "skip_dup"

            return result

        try:

            from plugins import moderation

            _safe = moderation.filter_public(draft, channel="bilibili")

            if not _safe:

                result["status"] = "skip_audit"

                result["reason"] = "命中红线审核，未发布"

                return result

            draft = _safe

        except Exception:  # noqa: BLE001

            pass

        await client.post_comment(aid, ev["bvid"], draft, root=ev["root"], parent=ev["parent"])

        bac.record_action(ev["bvid"], "reply", draft)

        result["status"] = "replied"

    else:

        state["pending"].append({

            "feed": ev["feed"],

            "bvid": ev["bvid"],

            "aid": aid,

            "root": ev["root"],

            "parent": ev["parent"],

            "nickname": ev["nickname"],

            "source": ev["source"],

            "title": info.get("title") or ev["title"],

            "uri": ev["uri"],

            "draft": draft,

            "at_time": ev["at_time"],

        })

        result["status"] = "draft"



    return result





def _video_url(bvid: str | None, fallback_uri: str = "") -> str:

    """由 bvid 拼标准可点开的网页链接；拿不到 bvid 时回退原始 uri。"""

    if bvid:

        return f"https://www.bilibili.com/video/{bvid}"

    return (fallback_uri or "").strip()





def _render_result(res: dict) -> str | None:

    ev = res["ev"]

    title = res.get("info", {}).get("title") or ev["title"]

    crown = "👑 " if OWNER_BILI_MID and ev["mid"] == OWNER_BILI_MID else ""

    who = f"{crown}{ev['nickname']} 在《{title}》下"

    url = _video_url(ev["bvid"], ev.get("uri") or "")

    link = f"\n🔗 {url}" if url else ""

    source = (ev.get("source") or "").strip()[:120]

    if res["status"] == "replied":

        body = f"🐟 刚在B站接了个话\n{who}跟我说：{source or '（只@了我一下）'}\n我回：{res['draft']}{link}"

        return body

    if res["status"] == "draft":

        body = (

            f"🐟 B站有人找我，这条我还没回\n"

            f"{who}说：{source or '（只@了我一下）'}\n"

            f"我想这么回：{res['draft']}{link}"

        )

        return body

    if res["status"] in ("skip_limit", "skip_dup", "skip_self"):

        return None

    if res["status"] == "skip_no_video":

        return f"⏭️ {who}：{res.get('reason')}，跳过"

    return None





async def _remember_reply(res: dict, state: dict) -> None:

    """已自动回评成功的事件：写入主人私聊实时记忆 + 最近回评记录（不入长期档案）。



    这样主人事后在私聊问「刚才在哪个视频回复了谁 / 把链接给我」，模型能从最近记忆里

    读到昵称、标题、对方原话、标准网页链接和本鱼回稿。

    """

    ev = res["ev"]

    title = res.get("info", {}).get("title") or ev["title"]

    url = _video_url(ev["bvid"], ev.get("uri") or "")

    tag = "回复了本鱼的评论" if ev["feed"] == "reply" else "@了本鱼"

    source = (ev["source"] or "").strip()[:120]

    draft = (res["draft"] or "").strip()

    private_scope = f"private_{OWNER_QQ_ID}"

    if source:

        msg = (

            f"【B站回评记录】{ev['nickname']} 在视频《{title}》{tag}：{source}"

            f"｜链接 {url}"

        )

    else:

        msg = f"【B站回评记录】{ev['nickname']} 在视频《{title}》{tag}｜链接 {url}"

    reply = f"本鱼在评论区回复了 @{ev['nickname']}：{draft}"

    try:

        await asyncio.to_thread(add_memory, "本鱼", OWNER_QQ_ID, private_scope, msg, reply)

        await asyncio.to_thread(save_daily, private_scope, "本鱼", OWNER_QQ_ID, msg, reply)

    except Exception as exc:  # noqa: BLE001

        logger.warning(f"[B站消息轮询] 写入回评记忆失败：{exc}")

    state.setdefault("recent_replies", []).append({

        "at_time": ev.get("at_time") or int(time.time()),

        "feed": ev["feed"],

        "nickname": ev["nickname"],

        "title": title,

        "bvid": ev["bvid"],

        "url": url,

        "source": source,

        "draft": draft,

    })

    # 同进程即时可见：同步进 ai_group 私聊内存历史，不用等重启回填才能进上下文

    try:

        from plugins import ai_group as _ag

        if hasattr(_ag, "_history"):

            max_len = getattr(cfg.context, "max_len_per_msg", 200)

            _ag._history[private_scope].append(("本鱼", msg[:max_len], True))

            _ag._history[private_scope].append(("本鱼", reply[:max_len], True))

    except Exception as exc:  # noqa: BLE001

        logger.warning(f"[B站消息轮询] 同步私聊内存历史失败：{exc}")





# ------------------------------------------------------------------ 一轮轮询



_LAST_LOGIN_NOTICE = 0.0

_LOGIN_NOTICE_GAP = 6 * 3600   # 未登录提醒：6 小时最多一次（原来每轮 60s 都私聊，1440 条/天）





async def _run_watch() -> str:

    """拉一轮新消息（@我 / 回复我评论）并处理，返回要报备给主人的文本（空串表示没有新消息）。"""

    global _LAST_LOGIN_NOTICE

    state = _load_state()

    client.refresh()

    if not client.logged_in:

        now = time.time()

        if now - _LAST_LOGIN_NOTICE >= _LOGIN_NOTICE_GAP:

            _LAST_LOGIN_NOTICE = now

            logger.warning("[B站消息轮询] 未登录，本轮跳过（6 小时内不再提醒）")

            return "⚠️ B 站还没登录，私聊我发 `B站登录`。"

        logger.info("[B站消息轮询] 未登录，静默跳过")

        return ""



    feeds: list[str] = ["at"]

    if cfg.bili_watch.watch_reply_feed:

        feeds.append("reply")



    fresh: list[dict] = []

    for feed in feeds:

        try:

            items = await _fetch_feed_items(feed)

        except bac.BiliAccountError as exc:

            logger.warning(f"[B站消息轮询] 拉取 {feed} 消息失败：{exc}")

            continue

        for item in items:

            ev = _parse_event(item, feed)

            if not ev:

                continue

            if ev["event_id"] in state["seen"]:

                continue

            fresh.append(ev)



    def _is_owner(ev: dict) -> bool:

        return bool(OWNER_BILI_MID and ev["mid"] == OWNER_BILI_MID)



    # 主人事件优先排队，先占本轮名额；其余按时间升序

    fresh.sort(key=lambda e: (0 if _is_owner(e) else 1, e["at_time"]))

    fresh = fresh[:cfg.bili_watch.max_per_run]

    if not fresh:

        return ""



    chance = cfg.bili_watch.reply_chance

    # 运行时覆盖：memory/bili_watch_config.json 里的 reply_chance 优先（改文件即刻生效）

    try:

        import json as _json

        _rt = _json.loads((ROOT / "memory" / "bili_watch_config.json").read_text(encoding="utf-8"))

        if isinstance(_rt, dict) and _rt.get("reply_chance") is not None:

            chance = max(0.0, min(1.0, float(_rt["reply_chance"])))

    except Exception:  # noqa: BLE001

        pass

    blocks: list[str] = []

    for ev in fresh:

        # 主人事件必回，不走随机概率；其他人才按概率抽，未命中静默不回

        if not _is_owner(ev) and random.random() >= chance:

            state["seen"].append(ev["event_id"])

            logger.info(

                f"[B站消息轮询] 概率未命中，静默跳过 {ev['feed']} · @{ev['nickname']}"

            )

            continue

        if _is_owner(ev):

            logger.info(f"[B站消息轮询] 主人搭话，必回 {ev['feed']} · @{ev['nickname']}")



        try:

            res = await _process_one(ev, state, is_owner=_is_owner(ev))

        except bac.BiliAccountError as exc:

            logger.warning(f"[B站消息轮询] 处理 {ev['event_id']} 失败：{exc}")

            state["seen"].append(ev["event_id"])

            blocks.append(f"⚠️ @{ev['nickname']} · {ev['title']}：{exc}")

            continue

        except Exception as exc:  # noqa: BLE001

            _log_poll_exc(exc, f"处理 {ev['event_id']}")

            state["seen"].append(ev["event_id"])

            blocks.append(f"⚠️ @{ev['nickname']} · {ev['title']}：出错了，本轮跳过")

            continue



        # 已处理事件统一标记，避免下一轮重复打扰

        state["seen"].append(ev["event_id"])

        status = res["status"]

        if status in ("skip_self", "skip_dup", "skip_limit"):

            logger.info(f"[B站消息轮询] 跳过 @{ev['nickname']}（{status}）")

            continue

        if status == "replied":

            await _remember_reply(res, state)

        block = _render_result(res)

        if block:

            blocks.append(block)



    _save_state(state)

    if not blocks:

        return ""



    mode = "已自动回评" if cfg.bili_watch.reply_enabled else "预览模式（未自动发评论）"

    scope = "@你 + 回复你评论" if cfg.bili_watch.watch_reply_feed else "@你"

    return (

        f"🐟 B站有人跟我搭话啦～（{'已回' if cfg.bili_watch.reply_enabled else '先给你看草稿'}）\n"

        + "\n\n".join(blocks)

    )





async def _notify_owner(text: str) -> None:

    if not text.strip():

        return

    try:

        await get_bot().send_private_msg(user_id=int(OWNER_QQ_ID), message=text)

    except Exception as exc:  # noqa: BLE001

        logger.warning(f"[B站消息轮询] 报备主人失败：{exc}")





# ------------------------------------------------------------------ 定时任务



if cfg.bili_watch.enabled and scheduler is not None:

    @scheduler.scheduled_job(

        "interval",

        seconds=cfg.bili_watch.interval,

        id="bili_watch_feed",

    )

    async def _watch_job() -> None:

        try:

            report = await _run_watch()

            if report:

                logger.info(f"[B站消息轮询] 本轮有新消息：\n{report}")

                if cfg.bili_watch.notify_owner:

                    await _notify_owner(report)

            else:

                logger.info("[B站消息轮询] 本轮无新的搭话消息")

        except Exception as exc:  # noqa: BLE001

            _log_poll_exc(exc, "本轮")

elif not cfg.bili_watch.enabled:

    logger.info("[B站消息轮询] BILI_WATCH_ENABLED=false，定时任务未注册")

else:

    logger.info("[B站消息轮询] 调度器不可用，仅保留主人手动指令")





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

        "B站看新消息", "B站看新@", "B站草稿", "B站补发", "B站清空草稿",

        "B站最近回复", "B站历史回复",

    ))





watch_matcher = on_message(rule=Rule(_check), priority=7, block=True)





def _feed_tag(feed: str) -> str:

    return "回复你" if feed == "reply" else "@你"





def _format_recent_replies(state: dict, limit: int = 10) -> str:

    """把最近已回评记录格式化成面板文本，供主人私聊指令直接查看。"""

    recent = list(reversed(state.get("recent_replies") or []))[:limit]

    if not recent:

        return "本鱼最近还没在 B 站回复过别人捏。"

    lines = [f"🐟 B站最近回复（最近 {len(recent)} 条）："]

    for idx, r in enumerate(recent, 1):

        tag = _feed_tag(r.get("feed", "at"))

        lines.append(

            f"{idx}. [{tag}] @{r['nickname']} ·《{r['title']}》\n"

            f"   对方：{(r.get('source') or '空')[:80]}\n"

            f"   本鱼：{r.get('draft') or ''}\n"

            f"   🔗 {r.get('url') or '无链接'}"

        )

    return "\n".join(lines)





@watch_matcher.handle()

async def _(event) -> None:

    text = _plain(event)



    try:

        if text.startswith(("B站看新消息", "B站看新@")):

            report = await _run_watch()

            await watch_matcher.finish(report or "这轮没有新的搭话消息。")



        elif text.startswith("B站清空草稿"):

            state = _load_state()

            state["pending"] = []

            _save_state(state)

            await watch_matcher.finish("草稿箱已清空。")



        elif text.startswith(("B站最近回复", "B站历史回复")):

            state = _load_state()

            await watch_matcher.finish(_format_recent_replies(state))



        elif text.startswith("B站草稿"):

            state = _load_state()

            pending = state["pending"]

            if not pending:

                await watch_matcher.finish("草稿箱是空的，还没有人来跟本鱼搭话。")

            lines = ["📝 B站待回草稿："]

            for idx, d in enumerate(pending, 1):

                tag = _feed_tag(d.get("feed", "at"))

                lines.append(

                    f"{idx}. [{tag}] @{d['nickname']} ·《{d['title']}》\n"

                    f"   对方：{d['source'][:80]}\n"

                    f"   拟回复：{d['draft']}"

                )

            lines.append("补发：`B站补发 序号`（数字从上面取）")

            await watch_matcher.finish("\n".join(lines))



        elif text.startswith("B站补发"):

            state = _load_state()

            pending = state["pending"]

            if not pending:

                await watch_matcher.finish("草稿箱是空的，没东西可补发。")

            m = re.search(r"B站补发\s*(\d+)", text)

            if not m:

                await watch_matcher.finish("格式：`B站补发 序号`（先发 `B站草稿` 看编号）")

            idx = int(m.group(1)) - 1

            if not (0 <= idx < len(pending)):

                await watch_matcher.finish(f"序号超范围，当前草稿共 {len(pending)} 条。")

            d = pending[idx]



            reason = bac.check_limit(d["bvid"], time.strftime("%Y-%m-%d"))

            if reason:

                await watch_matcher.finish("⏳ " + reason)

            if d["draft"] in bac.recent_texts():

                await watch_matcher.finish("这条刚发过，先别重复发。")



            client.refresh()

            try:

                from plugins import moderation

                _safe = moderation.filter_public(d["draft"], channel="bilibili")

                if not _safe:

                    await watch_matcher.finish("这条内容过了审核没过，先不发。")

                    return

                d["draft"] = _safe

            except Exception:  # noqa: BLE001

                pass

            await client.post_comment(

                d["aid"], d["bvid"], d["draft"], root=d["root"], parent=d["parent"],

            )

            bac.record_action(d["bvid"], "watch_reply", d["draft"])

            pending.pop(idx)

            _save_state(state)

            tag = _feed_tag(d.get("feed", "at"))

            await watch_matcher.finish(

                f"✅ 已补发（{tag}），回复 @{d['nickname']} ·《{d['title']}》：\n> {d['draft']}"

            )



    except bac.BiliAccountError as exc:

        await watch_matcher.finish(f"❌ {exc}")

    except FinishedException:

        raise

    except Exception as exc:  # noqa: BLE001

        logger.exception(f"[B站消息指令] 未预期错误：{exc}")

        await watch_matcher.finish("B站这边出错了，看看后台日志。")

