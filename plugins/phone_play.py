"""「去玩手机」：主人一句话，她自己选抖音/B站刷一会儿，回来汇报看了啥。

抖音这一路（2026-10-26 升级）
---------------------------
原来只是把页面上能看到的标题念三条。现在补齐成一条完整链路：

    刷 4 屏 → 每条记下「标题 + 点赞数 + 话题标签」
      → 按点赞数挑出最热的一条
      → 点进去、录 5 秒屏抽 3 帧、交给识图看画面
      → 把「标题 + 画面 + 热度 + 标签」压成一句事实写进长期记忆
      → 回来汇报：刷到啥 / 最火的哪条 / 画面是什么 / 带了哪些 # 话题

这么做的好处：热度是真实数字（不是随机挑），画面是真看过（不是只读标题），
并且学到的内容进 global_learn，之后聊天能自然带出来。

B 站这一路仍是只读热门榜（不下载、不点进去），因为 B 站有公开 API，够用。
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from pathlib import Path

import httpx
from nonebot import on_message
from nonebot.adapters.onebot.v11 import PrivateMessageEvent
from nonebot.log import logger

ROOT = Path(__file__).resolve().parent.parent
OWNER_QQ = "100000000"

play_cmd = on_message(priority=9, block=False)

# 热度：抖音的点赞数在 content-desc 里形如「未点赞，喜欢4254，按钮」「1.2万赞」
_LIKE_PATTERNS = (
    re.compile(r"(?:喜欢|点赞|赞)[:：]?\s*([\d.]+)\s*([万wW千kK]?)"),
    re.compile(r"([\d.]+)\s*([万wW千kK]?)\s*(?:点赞|喜欢|个赞)"),
)
_TAG_RE = re.compile(r"#([^\s#，。！？]{1,14})")
# 纯计数/纯按钮/导航文案，不能当标题
_NOISE_RE = re.compile(r"(未点赞|未选中|已点赞|按钮|评论\s*\d|收藏\s*\d|分享\s*\d|"
                       r"^\d+$|关注$|推荐$|首页$|朋友$|消息$|我$|搜索$|更多$|广告$)")
_SKIP_EXACT = ("按钮", "首页", "朋友", "消息", "我", "关注", "推荐", "搜索", "评论",
               "收藏", "分享", "广告", "直播", "打开", "返回", "更多")


def _to_num(num: str, unit: str) -> int:
    try:
        v = float(num)
    except (TypeError, ValueError):
        return 0
    unit = (unit or "").lower()
    if unit in ("万", "w"):
        v *= 10000
    elif unit in ("千", "k"):
        v *= 1000
    return int(v)


def _clean(t: str) -> str:
    return re.sub(r"\s+", " ", str(t or "")).strip()


def _screen_item(root) -> dict | None:
    """一屏 = 一个视频：从整屏节点里挑出「标题 + 点赞数 + 标签」。

    抖音的节点结构很碎（作者名、喜欢N、评论N、收藏N 各自一个节点），
    所以按"整屏"聚合，而不是按节点聚合。
    """
    texts: list[str] = []
    likes = 0
    for n in root.iter("node"):
        for raw in (n.get("text") or "", n.get("content-desc") or ""):
            t = _clean(raw)
            if not t:
                continue
            texts.append(t)
            for pat in _LIKE_PATTERNS:
                m = pat.search(t)
                if m:
                    likes = max(likes, _to_num(m.group(1), m.group(2)))
    # 标题候选：长度 ≥8、不含计数/按钮字样、不是纯导航词
    cands: list[tuple[str, str]] = []
    for node in root.iter("node"):
        for raw in (node.get("text") or "", node.get("content-desc") or ""):
            t = _clean(raw)
            if len(t) >= 8 and t not in _SKIP_EXACT and not _NOISE_RE.search(t):
                cands.append((t, str(node.get("bounds") or "")))
    if not cands:
        return None
    title_raw, bounds = max(cands, key=lambda x: len(x[0]))
    title = title_raw[:80]
    tags: list[str] = []
    for t in texts:
        for g in _TAG_RE.findall(t):
            if g not in tags:
                tags.append(g)
    return {"title": title, "likes": likes, "tags": tags[:8], "bounds": bounds}


def _center(bounds: str) -> tuple[int, int] | None:
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", str(bounds or ""))
    if not m:
        return None
    x1, y1, x2, y2 = (int(m.group(i)) for i in range(1, 5))
    if x2 <= x1 or y2 <= y1:
        return None
    return (x1 + x2) // 2, (y1 + y2) // 2


def _play_douyin(screens: int = 4) -> dict:
    """刷抖音：收 screens 屏标题+热度 → 挑最热 → 点进去看画面 → 返回汇总。"""
    try:
        from plugins import douyin_phone as dp
    except Exception as e:  # noqa: BLE001
        return {"items": [], "error": f"抖音模块不可用：{e}"}

    try:
        dp._wake()
        time.sleep(1)
        dp._open_app()
        time.sleep(2)
    except Exception as e:  # noqa: BLE001
        return {"items": [], "error": f"打开抖音失败：{e}"}

    try:
        on_feed = _goto_feed(dp)
        logger.info(f"[玩手机] 已切到推荐信息流={on_feed}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[玩手机] 切信息流失败：{type(e).__name__}: {e}")

    seen: dict[str, dict] = {}
    logger.info(f"[玩手机] 开始刷抖音，最多 {screens} 屏")
    for i in range(screens):
        try:
            root = dp._dump(f"play_{i}")
        except Exception:
            root = None
        if root is not None:
            it = _screen_item(root)
            if it:
                key = it["title"][:24]
                if key not in seen or it["likes"] > seen[key]["likes"]:
                    seen[key] = it
        if i < 3:
            try:
                # 注意：douyin_phone._swipe 只接受 refresh/unlock/album 三个预置手势，
                # 刷视频要自己发 input swipe（上滑 = 从下往上）
                dp._adb("shell", "input", "swipe", "540", "1700", "540", "600", "350",
                        tag="play_next")
                time.sleep(1.8)
            except Exception:
                break

    items = sorted(seen.values(), key=lambda x: -x["likes"])
    logger.info(f"[玩手机] 收到 {len(items)} 条，最热={items[0]['likes'] if items else 0} 赞")
    if not items:
        return {"items": [], "error": ""}

    hottest = items[0]
    # 识图不能在 to_thread 里做：ai_group 的共享 httpx.AsyncClient 绑在主事件循环上，
    # 工作线程里 asyncio.run() 会新建环路，客户端报 "attached to a different loop"，
    # 于是描述永远是空。这里只负责点进去，识图交给调用方在主循环上 await。
    pos = _center(hottest.get("bounds") or "") if (hottest["likes"] > 0 or len(items) <= 2) else None
    if pos:
        try:
            dp._tap(*pos, "挑最热那条点进去")
            time.sleep(2.5)
        except Exception:  # noqa: BLE001
            pos = None

    tags: list[str] = []
    for it in items[:6]:
        for t in it["tags"]:
            if t not in tags:
                tags.append(t)

    return {"items": items, "hottest": hottest, "frame": "", "tags": tags[:12],
            "tap": pos, "error": ""}


async def _play_bilibili() -> list[str]:
    try:
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "Mozilla/5.0"}) as cl:
            r = await cl.get("https://api.bilibili.com/x/web-interface/popular", params={"ps": 8, "pn": 1})
        data = r.json().get("data", {}).get("list") or []
        picks = []
        for v in data[:3]:
            picks.append(f"{v.get('title')}（UP：{v.get('owner', {}).get('name')}）")
        return picks
    except Exception:
        return []


def _choose_platform(text: str) -> str:
    if "抖音" in text and "B站" not in text and "b站" not in text:
        return "douyin"
    if ("B站" in text or "b站" in text) and "抖音" not in text:
        return "bili"
    return random.choice(["douyin", "bili"])


STATE_FILE = ROOT / "memory" / "play_phone.json"
PLAY_DAILY_CAP = int(os.getenv("PLAY_PHONE_DAILY_CAP", "10") or "10")
# 手动（主人叫她去玩）→ 刷 3 屏；自动（她自己想玩）→ 刷 4 屏且受每日 10 次限制
MANUAL_SCREENS = int(os.getenv("PLAY_PHONE_MANUAL_SCREENS", "3") or "3")
AUTO_SCREENS = int(os.getenv("PLAY_PHONE_AUTO_SCREENS", "4") or "4")


def _state() -> dict:
    try:
        d = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _state_save(d: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except Exception:  # noqa: BLE001
        pass


def _today_count() -> int:
    d = _state()
    day = time.strftime("%Y-%m-%d")
    return int(d.get("count") or 0) if d.get("date") == day else 0


def _count_one(manual: bool) -> int:
    d = _state()
    day = time.strftime("%Y-%m-%d")
    if d.get("date") != day:
        d = {"date": day, "count": 0, "manual": 0, "auto": 0}
    d["count"] = int(d.get("count") or 0) + 1
    d["manual" if manual else "auto"] = int(d.get("manual" if manual else "auto") or 0) + 1
    _state_save(d)
    return d["count"]


AUTO_PLAY_ENABLED = os.getenv("PLAY_PHONE_AUTO", "0").lower() in ("1", "true", "yes")


def can_self_play() -> tuple[bool, int]:
    """她自己想玩手机时的额度检查。

    ⚠️ 默认关闭（PLAY_PHONE_AUTO=0）：自动刷手机每次都要调识图，等于持续烧额度。
    现在只有主人手动叫才会玩；这个函数留给以后想开放时用，开着才受每日上限约束。
    """
    used = _today_count()
    if not AUTO_PLAY_ENABLED:
        return False, used
    return (used < PLAY_DAILY_CAP), used


HOT_INDEX = ROOT / "memory" / "douyin_hot.json"


def _append_hot_index(pack: dict) -> None:
    """把刷到的热门单独存一份索引（标题/点赞/话题/画面），给以后"照着热度选题"用。

    和长期记忆分开的原因：长期记忆会被权重淘汰、会被 30 天时效清掉，
    而这里要的是"最近哪些题材火"，需要按时间排、能一眼看趋势。
    """
    h = pack.get("hottest") or {}
    if not h:
        return
    try:
        rows = []
        if HOT_INDEX.exists():
            try:
                rows = json.loads(HOT_INDEX.read_text(encoding="utf-8")) or []
            except Exception:  # noqa: BLE001
                rows = []
        rows.append({
            "ts": time.strftime("%Y-%m-%d %H:%M"),
            "title": str(h.get("title", ""))[:90],
            "likes": int(h.get("likes") or 0),
            "tags": list(pack.get("tags") or [])[:10],
            "frame": str(pack.get("frame") or "")[:80],
            "all": [{"title": str(i.get("title", ""))[:60], "likes": int(i.get("likes") or 0)}
                    for i in (pack.get("items") or [])[:6]],
        })
        rows = rows[-300:]
        HOT_INDEX.parent.mkdir(parents=True, exist_ok=True)
        tmp = HOT_INDEX.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(HOT_INDEX)
        logger.info(f"[玩手机] 热门索引 +1（共 {len(rows)} 条）")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[玩手机] 写热门索引失败：{type(e).__name__}: {e}")


def _save_to_memory(pack: dict) -> None:
    """把「最火的这条」压成一句事实写进长期记忆（自学档案）。"""
    h = pack.get("hottest") or {}
    if not h:
        return
    try:
        from plugins import long_memory as ltm
        parts = [str(h.get("title", ""))[:50]]
        if h.get("likes"):
            parts.append(f"点赞{h['likes']}")
        if pack.get("frame"):
            parts.append(str(pack["frame"])[:50])
        if pack.get("tags"):
            parts.append("话题：" + " ".join("#" + t for t in pack["tags"][:5]))
        fact = "抖音热门：" + "｜".join(p for p in parts if p)
        # 30 天时效：给"以后照着这些热度选题"留出窗口（默认 news 只有 7 天）
        ltm.add_learned(fact[:120], source="douyin", kind="news", ttl_days=30)
        _append_hot_index(pack)
    except Exception:  # noqa: BLE001
        pass


@play_cmd.handle()
async def _(event: PrivateMessageEvent):
    if str(event.user_id) != OWNER_QQ:
        return
    text = event.get_plaintext().strip()
    if not any(k in text for k in ("去玩手机", "玩手机", "刷抖音", "刷B站", "刷b站", "玩会儿手机")):
        return
    platform = _choose_platform(text)
    name = "抖音" if platform == "douyin" else "B站"
    await play_cmd.send(f"(*放下手里的事*) 好，本鱼去刷会儿{name}，一会儿回来跟你讲。")
    try:
        from plugins.activity import set_activity
        set_activity(f"在刷{name}", 600)
    except Exception:
        pass

    if platform == "douyin":
        _cnt = _count_one(manual=True)
        logger.info(f"[玩手机] 今日第 {_cnt}/{PLAY_DAILY_CAP} 次（主人手动叫的，不受额度限制）")
        pack = await asyncio.to_thread(_play_douyin, MANUAL_SCREENS)
        items = pack.get("items") or []
        if not items:
            await play_cmd.finish(
                f"(*耷拉尾巴*) 抖音没刷出东西，可能没登录或被风控了。{pack.get('error') or ''}"
            )
            return
        # 主循环上识图（原因见 _play_douyin 注释），做完再退回列表
        if pack.get("tap"):
            try:
                from plugins import douyin_phone as _dp
                pack["frame"] = (await _dp._describe_video() or "").strip()
                logger.info(f"[玩手机] 画面描述长度={len(pack['frame'])}")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[玩手机] 识图失败：{type(e).__name__}: {e}")
            try:
                await asyncio.to_thread(_dp._back_gesture)
            except Exception:  # noqa: BLE001
                pass
        hottest = pack.get("hottest") or {}
        top3 = "\n".join(
            f"· {it['title'][:40]}" + (f"（{it['likes']} 赞）" if it["likes"] else "")
            for it in items[:3]
        )
        msg = [f"(*甩尾巴*) 本鱼刷完抖音回来啦，一共记住 {len(items)} 条：", top3]
        if hottest:
            msg.append(f"\n🔥 最火的是：{str(hottest.get('title', ''))[:50]}"
                       + (f"（{hottest['likes']} 赞）" if hottest.get("likes") else ""))
        if pack.get("frame"):
            msg.append(f"👀 画面：{str(pack['frame'])[:80]}")
        if pack.get("tags"):
            msg.append("🏷 " + " ".join("#" + t for t in pack["tags"][:8]))
        _save_to_memory(pack)
        msg.append("\n这条我记下来了，以后聊到能想起来。")
        await play_cmd.finish("\n".join(msg))
        return

    picks = await _play_bilibili()
    if not picks:
        await play_cmd.finish("(*耷拉尾巴*) B站没刷出东西。")
        return
    lines = "\n".join(f"· {p}" for p in picks[:3])
    await play_cmd.finish(
        f"(*甩尾巴*) 本鱼刷完{name}回来啦，看了这几条：\n{lines}\n\n主人要不要也去看看？"
    )
