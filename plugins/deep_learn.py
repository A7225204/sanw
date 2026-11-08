"""强化学习：主人指定一个主题，联网定向深挖，并提示哪些网站需要 VPN。"""
from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import urlparse

from nonebot import on_message
from nonebot.adapters.onebot.v11 import PrivateMessageEvent

OWNER = "100000000"
learn_cmd = on_message(priority=5, block=False)

# 国内直连通常打不开、需要代理的站点
VPN_HOSTS = (
    "youtube.com", "youtu.be", "google.com", "googleapis.com",
    "behance.net", "artstation.com", "pinterest.com", "instagram.com",
    "twitter.com", "x.com", "vimeo.com", "github.com", "github.io",
    "medium.com", "reddit.com", "facebook.com", "cgtrader.com",
    "gumroad.com", "blender.org", "sidefx.com", "fxtd.com",
)


def _domain(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _needs_vpn(url: str) -> bool:
    d = _domain(url)
    return any(d == h or d.endswith("." + h) for h in VPN_HOSTS)


def _extract_topic(text: str) -> str:
    t = text.strip()
    for lead in ("强化学习一下", "强化学习", "针对", "去学", "深挖"):
        if t.startswith(lead):
            t = t[len(lead):].strip(" ：:，。,")
    for tail in ("强化学习", "这块", "这方面", "一下"):
        if t.endswith(tail):
            t = t[: -len(tail)].strip(" ：:，。,")
    return t[:60]


async def _learn(topic: str) -> str:
    from plugins import web_search
    from plugins.ai_group import call_ai_raw

    items = await web_search._free_web_results(topic)
    material = "\n".join(f"{i+1}. {t}：{s}（{u}）" for i, (t, s, u) in enumerate(items)) if items else "（没搜到结果）"
    urls = [u for _, _, u in items] if items else []

    prompt = (
        f"你是 3ds Max / Corona / tyFlow / Photoshop / AE / PR / 达芬奇 的老手，"
        f"主人要针对「{topic}」做一次强化学习。\n"
        f"搜索结果：\n{material[:2500]}\n\n"
        "请输出：\n"
        "1. 3~5 条能直接上手的要点/参数/流程，生活化说，别写目录；\n"
        "2. 2~4 个值得看的网站或教程，标明它主要讲什么；\n"
        "3. 如果内容偏少，直说没查到多少。\n"
        "只输出正文，不要 markdown 标题、不要自我介绍。"
    )
    try:
        answer = await call_ai_raw(prompt, "deepseek", temperature=0.5, max_tokens=700)
    except Exception as e:  # noqa: BLE001
        return f"这次没学成：{type(e).__name__}: {e}"

    blocked = [u for u in urls if _needs_vpn(u)]
    domestic = [u for u in urls if u and not _needs_vpn(u)]
    tip = ""
    if blocked:
        tip = "\n\n需要挂 VPN 才能看的：\n" + "\n".join(f"- {u}" for u in blocked[:3])
        tip += "\n这些是海外站，直连大概率打不开。"
    elif domestic:
        tip = "\n\n这次搜到的多是国内可直连的站，不用 VPN。"

    # 记进专家知识档案，之后专家模式能直接用
    try:
        from plugins.expert_mode import _load_knowledge, _save_knowledge
        facts = _load_knowledge()
        facts.append({"subject": f"强化学习·{topic}", "fact": (answer or "")[:160],
                      "ts": time.strftime("%Y-%m-%d")})
        _save_knowledge(facts)
    except Exception:
        pass

    return (answer or "").strip()[:1200] + tip


@learn_cmd.handle()
async def _(event: PrivateMessageEvent):
    if str(event.user_id) != OWNER:
        return
    text = event.get_plaintext().strip()
    if "强化学习" not in text and not text.startswith("深挖"):
        return
    topic = _extract_topic(text)
    if not topic or topic in ("什么", "一下", "这个"):
        await learn_cmd.finish("要学什么？说具体点，比如「强化学习 Corona 产品打光」。")
        return
    await learn_cmd.send(f"(*搬小板凳*) 好，本鱼针对「{topic}」去扒一圈，顺便看看哪些站要翻墙。")
    result = await _learn(topic)
    await learn_cmd.finish(result[:1800])
