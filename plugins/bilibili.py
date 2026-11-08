"""B 站（哔哩哔哩）接入：热搜 / 搜索 / 视频详情 / 热门 / 排行 / UP 主。

用法（私聊直接发；群里要先 @ 本鱼）：
* 直接发 B 站链接或 BV 号     → 视频卡片（标题 / UP / 播放 / 弹幕 / 点赞 / 简介 / 封面）
* `B站热搜`                   → 当前 B 站热搜前 10
* `搜B站 关键词`              → 视频搜索结果前 5
* `B站热门` / `B站排行`       → 热门 / 排行榜前 10
* `B站UP 名字`                → 搜索 UP 主
* `B站`                       → 指令说明

实现全部走 B 站公开 Web 接口（无需登录），见 plugins/bilibili_api.py。
"""
from __future__ import annotations

import base64
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
import nonebot  # noqa: E402
from nonebot import on_message  # noqa: E402
from nonebot.exception import FinishedException  # noqa: E402
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment, PrivateMessageEvent  # noqa: E402
from nonebot.rule import Rule  # noqa: E402

from config import get_config  # noqa: E402
from plugins import bilibili_api as bili  # noqa: E402

cfg = get_config()
logger = nonebot.logger

# 触发词（顺序有讲究：长词在前，避免「B站热搜」被「B站」抢先）
_PREFIXES = (
    "B站热搜", "b站热搜", "B站热榜", "b站热榜",
    "B站热门", "b站热门", "B站排行", "b站排行", "B站排行榜", "b站排行榜",
    "搜B站", "搜b站", "B站搜索", "b站搜索", "B站搜", "b站搜",
    "B站UP", "b站UP", "B站up", "b站up", "B站UP主", "b站UP主",
    "B站视频", "b站视频",
)

_COOLDOWN: dict[str, float] = {}
_COOLDOWN_SECONDS = 2.0


def _plain(event) -> str:
    text = (event.get_plaintext() or "").strip()
    # 去掉开头的 @机器人
    text = re.sub(r"^(@\S+\s*)+", "", text).strip()
    return text


def _is_bili_intent(event) -> bool:
    text = _plain(event)
    if not text:
        return False
    # 2026-11-05：先剥掉开头的斜杠再判断。get_plaintext() 会保留 "/"，
    # 而 _PREFIXES 里写的是「B站热搜」不带斜杠 —— 以前 /B站热搜、/B站热门
    # 全因为这一个字符匹配不上，静默无反应。
    text = text.lstrip("/").strip()
    if "打开B站" in text or "打开哔哩哔哩" in text:
        return False  # 这个交给 ai_group 的开站功能
    # 2026-11-05：`/B站 <链接|BV>` 是"去看这一条"，交给 ai_group 的看片分支。
    # 以前这里只要文本里有 bilibili.com 就直接 True，配上 block=True，
    # 把看片分支整个挡死——主人发链接永远只拿到一张卡片。
    # 注意只让「/B站 + 链接/BV」走这条路，`/B站热搜`、`/B站热门` 仍归本插件。
    if re.match(r"^/?B站\s*(https?://|bilibili\.com|b23\.tv|BV[0-9A-Za-z]{8,})", text, re.I):
        return False
    if "bilibili.com" in text or "b23.tv" in text or "bili2233" in text:
        return True
    if bili.BV_RE.search(text):
        return True
    if bili.AID_RE.search(text):
        return True
    return any(text.startswith(p) for p in _PREFIXES)


def _check(event) -> bool:
    if isinstance(event, GroupMessageEvent):
        if cfg.allow_groups and event.group_id not in cfg.allow_groups:
            return False
        if not event.is_tome():
            return False
    elif not isinstance(event, PrivateMessageEvent):
        return False
    return _is_bili_intent(event)


bili_matcher = on_message(rule=Rule(_check), priority=9, block=True)


async def _image_segment(url: str):
    """B 站图床带防盗链，直接发 URL 在群里可能裂，先下载再 base64 发。"""
    if not url:
        return None
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, headers=bili.HEADERS) as client:
            resp = await client.get(url)
            if resp.status_code == 200 and 1024 < len(resp.content) < 8 * 1024 * 1024:
                return MessageSegment.image("base64://" + base64.b64encode(resp.content).decode())
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[B站] 封面下载失败：{exc}")
    return None


def _scope_key(event) -> str:
    if isinstance(event, GroupMessageEvent):
        return f"g{event.group_id}"
    return f"p{event.user_id}"


@bili_matcher.handle()
async def _(event):
    text = _plain(event)
    scope = _scope_key(event)
    now = time.time()
    if now - _COOLDOWN.get(scope, 0) < _COOLDOWN_SECONDS:
        await bili_matcher.finish()
        return
    _COOLDOWN[scope] = now

    try:
        # ---------- 链接 / BV 号：视频卡片 ----------
        link = bili.LINK_RE.search(text)
        bvid = bili.extract_bvid(text)
        aid = bili.extract_aid(text)
        if link and ("bilibili.com" in link.group(0) or "b23.tv" in link.group(0) or "bili2233" in link.group(0)):
            if not bvid and not aid:
                bvid, aid = await bili.resolve_video(link.group(0))
        if (bvid or aid) and (link or text.strip() in (bvid, f"av{aid}") or "B站视频" in text):
            card = await bili.video_card(bvid, aid)
            cover = await _image_segment(card.get("cover"))
            if cover:
                await bili_matcher.send(Message([MessageSegment.text(card["text"]), cover]))
            else:
                await bili_matcher.send(card["text"])
            await bili_matcher.finish()
            return

        # ---------- 热搜 ----------
        if text.startswith(("B站热搜", "b站热搜", "B站热榜", "b站热榜")):
            items = await bili.trending(10)
            if not items:
                await bili_matcher.finish("B站热搜今天空空的，是不是接口抽风了…")
                return
            await bili_matcher.finish(bili.render_list("🔥 B站热搜 Top10：", items, "keyword"))
            return

        # ---------- 搜索视频 ----------
        for prefix in ("搜B站", "搜b站", "B站搜索", "b站搜索", "B站搜", "b站搜"):
            if text.startswith(prefix):
                keyword = text[len(prefix):].strip("：: ")
                if not keyword:
                    await bili_matcher.finish("搜啥？格式：`搜B站 关键词`")
                    return
                items = await bili.search_videos(keyword, 5)
                if not items:
                    await bili_matcher.finish(f"B站没搜到「{keyword}」，换个词试试？")
                    return
                await bili_matcher.finish(bili.render_list(f"🔍 B站搜「{keyword}」：", items, "video"))
                return

        # ---------- 热门 / 排行 ----------
        if text.startswith(("B站热门", "b站热门")):
            items = await bili.popular(10)
            await bili_matcher.finish(bili.render_list("🔥 B站热门 Top10：", items, "video"))
            return
        if text.startswith(("B站排行", "b站排行", "B站排行榜", "b站排行榜")):
            items = await bili.ranking(10)
            await bili_matcher.finish(bili.render_list("🏆 B站排行榜 Top10：", items, "video"))
            return

        # ---------- UP 主搜索 ----------
        for prefix in ("B站UP主", "b站UP主", "B站UP", "b站UP", "B站up", "b站up"):
            if text.startswith(prefix):
                keyword = text[len(prefix):].strip("：: ")
                if not keyword:
                    await bili_matcher.finish("搜哪个 UP？格式：`B站UP 名字`")
                    return
                items = await bili.search_users(keyword, 3)
                if not items:
                    await bili_matcher.finish(f"没找到 UP 主「{keyword}」")
                    return
                await bili_matcher.finish(bili.render_list(f"👤 B站 UP「{keyword}」：", items, "user"))
                return

        # ---------- 只发「B站」：说明 ----------
        await bili_matcher.finish(
            "📺 **B站玩法**\n"
            "• 直接发 B 站链接 / BV 号 → 视频卡片\n"
            "• `B站热搜` / `B站热门` / `B站排行`\n"
            "• `搜B站 关键词` → 搜视频\n"
            "• `B站UP 名字` → 搜 UP 主\n"
            "• 登录 / 发评论：私聊发 `B站帮助`（主人专用）\n"
            "（群里要先 @ 本鱼）"
        )
    except bili.BiliError as exc:
        logger.warning(f"[B站] 接口错误：{exc}")
        await bili_matcher.finish(f"B站接口抽风了：{exc}")
    except FinishedException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"[B站] 未预期错误：{exc}")
        await bili_matcher.finish("B站这边出错了，等会儿再试…")
