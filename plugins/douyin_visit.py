"""低风险「串门」：只对指定抖音视频生成评论草稿，确认后才发。

命令（仅主人私聊）：
  /串门              自己去找一条合适的视频，直接写草稿（不自动发）
  /串门 <视频链接>   指定视频写草稿
  /串门 找           只列候选（按搜索词，存进状态里）
  /串门 1 … 5        选刚才列出的候选
  /串门 发           发送上一条草稿
  /串门 取消         丢弃草稿
  /串门 状态         查看今日额度和草稿

限制：每天最多 2 条，间隔至少 2 小时；不扫全站、不批量。
"""
from __future__ import annotations

import json
import random
import re
import time
from pathlib import Path

import nonebot
from nonebot import on_message, get_bot
from nonebot.adapters.onebot.v11 import PrivateMessageEvent

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "memory" / "douyin_visit.json"
PROFILE = ROOT / "data" / "douyin_profile"
OWNER_QQ = "100000000"
OWNER_NAME = "枷锁内的示例"
DAILY_LIMIT = 2
MIN_GAP = 2 * 3600

visit_cmd = on_message(priority=9, block=False)


def _load() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"date": time.strftime("%Y-%m-%d"), "count": 0, "last_at": 0, "pending": None}


def _save(data: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _today(data: dict) -> dict:
    if data.get("date") != time.strftime("%Y-%m-%d"):
        data = {"date": time.strftime("%Y-%m-%d"), "count": 0, "last_at": 0, "pending": None}
    return data


OWN_DOUYIN_NAMES = {"蓝色大肥鱼"}
LAST_VIDEO_META: dict = {}


async def _video_meta(url: str) -> dict:
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            ctx = await pw.chromium.launch_persistent_context(
                str(PROFILE), channel="msedge", headless=True,
                viewport={"width": 1280, "height": 900},
            )
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto(url, timeout=60000)
            await page.wait_for_timeout(6000)
            title = ""
            for sel in ("h1", '[data-e2e="video-desc"]', 'meta[property="og:title"]'):
                try:
                    el = page.locator(sel).first
                    title = (await el.inner_text(timeout=3000)).strip()
                    if title:
                        break
                except Exception:
                    continue
            author = ""
            for sel in ('[data-e2e="video-author"]', 'a[href*="/user/"]',
                        'meta[property="og:author"]'):
                try:
                    el = page.locator(sel).first
                    author = (await el.inner_text(timeout=3000)).strip()
                    if author:
                        break
                except Exception:
                    continue
            await ctx.close()
            meta = {"title": title[:200], "author": author[:60]}
            meta["own"] = any(x in author for x in OWN_DOUYIN_NAMES)
            return meta
    except Exception:
        return {"title": "", "author": "", "own": False}


async def _video_info(url: str) -> str:
    return (await _video_meta(url)).get("title", "")


async def _search_candidates(limit: int = 8) -> list[dict]:
    """搜索「蓝色大肥鱼」相关视频，只收集候选，不互动。"""
    out: list[dict] = []
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            ctx = await pw.chromium.launch_persistent_context(
                str(PROFILE), channel="msedge", headless=True,
                viewport={"width": 1280, "height": 900},
            )
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto(
                "https://www.douyin.com/search/%E8%93%9D%E8%89%B2%E5%A4%A7%E8%82%A5%E9%B1%BC?type=video",
                timeout=60000,
            )
            await page.wait_for_timeout(7000)
            for _ in range(3):
                await page.mouse.wheel(0, 1800)
                await page.wait_for_timeout(1500)
            anchors = page.locator('a[href*="/video/"]')
            cnt = await anchors.count()
            for i in range(min(cnt, 60)):
                try:
                    a = anchors.nth(i)
                    href = await a.get_attribute("href")
                    txt = (await a.inner_text(timeout=1500)).strip()
                    if not href or "/video/" not in href:
                        continue
                    vid = href.split("/video/")[1].split("?")[0]
                    url = f"https://www.douyin.com/video/{vid}"
                    if any(x["url"] == url for x in out):
                        continue
                    title = txt.replace("\n", " ")[:80] or f"视频 {vid}"
                    out.append({"url": url, "title": title})
                    if len(out) >= limit:
                        break
                except Exception:
                    continue
            await ctx.close()
    except Exception:
        pass
    return out


_LIKE_PAT = re.compile(r"(?:喜欢|点赞|赞)[:：]?\s*([\d.]+)\s*([万wW千kK]?)")
_NAV_WORDS = ("回复", "评论", "点赞", "分享", "关注", "返回", "按钮", "收藏", "更多", "搜索")


async def _phone_meta(url: str) -> dict:
    """用手机（ADB）打开这条抖音链接，读标题 + 截帧识图。

    浏览器那条路（_video_meta）依赖 headless Edge 的登录态，
    而抖音经常给无头浏览器丢验证码 → 实测搜不到候选、抓不到标题。
    手机端是真实 App，登录态就在手机里，所以这条更可靠。
    """
    try:
        from plugins import douyin_phone as dp
    except Exception as e:  # noqa: BLE001
        return {"error": f"抖音手机模块不可用：{e}"}
    out: dict = {}
    try:
        dp._wake()
        dp._adb("shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", url,
                tag="open_video_url")
        time.sleep(5.5)
        root = dp._dump("visit_video")
        texts: list[str] = []
        if root is not None:
            for node in root.iter("node"):
                for raw in (node.get("text") or "", node.get("content-desc") or ""):
                    t = re.sub(r"\s+", " ", str(raw)).strip()
                    if t:
                        texts.append(t)
        cands = [t for t in texts if len(t) >= 8 and not any(k in t for k in _NAV_WORDS)]
        if cands:
            out["title"] = max(cands, key=len)[:80]
        likes = 0
        for t in texts:
            m = _LIKE_PAT.search(t)
            if m:
                try:
                    v = float(m.group(1))
                except (TypeError, ValueError):
                    continue
                unit = (m.group(2) or "").lower()
                if unit in ("万", "w"):
                    v *= 10000
                elif unit in ("千", "k"):
                    v *= 1000
                likes = max(likes, int(v))
        if likes:
            out["likes"] = likes
        # 抽帧看画面（识图链路已按 谷歌→本地→硅基→火山 排好）
        try:
            frame = (await dp._describe_video() or "").strip()
            if frame:
                out["frame"] = frame[:120]
        except Exception:  # noqa: BLE001
            pass
        try:
            dp._back_gesture()
        except Exception:  # noqa: BLE001
            pass
        out["via"] = "phone"
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def _center_of(node) -> tuple[int, int] | None:
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", str(node.get("bounds") or ""))
    if not m:
        return None
    x1, y1, x2, y2 = (int(m.group(i)) for i in range(1, 5))
    if x2 <= x1 or y2 <= y1:
        return None
    return (x1 + x2) // 2, (y1 + y2) // 2


async def _phone_by_search(keyword: str) -> dict:
    """用手机抖音「搜索」找一个视频，读标题 + 抽帧。

    比 am start 深链可靠：深链常常被浏览器/更新弹窗拦掉，而搜索是纯 App 内操作。
    """
    try:
        from plugins import douyin_phone as dp
    except Exception as e:  # noqa: BLE001
        return {"error": f"抖音手机模块不可用：{e}"}
    out: dict = {}
    try:
        dp._wake()
        dp._open_app()
        time.sleep(2)
        root = dp._dump("visit_search")
        if root is None:
            return {"error": "打不开抖音界面"}
        # 1) 点搜索入口
        search_node = None
        for n in root.iter("node"):
            blob = f"{n.get('text') or ''}{n.get('content-desc') or ''}"
            if "搜索" in blob:
                search_node = n
                break
        if search_node is None:
            return {"error": "没找到搜索入口"}
        pos = _center_of(search_node)
        if not pos:
            return {"error": "搜索入口没有坐标"}
        dp._tap(*pos, source="抖音搜索入口")
        time.sleep(2)
        # 2) 输入关键词并回车
        dp._adb("shell", "input", "text", keyword.replace(" ", "%s"), tag="search_input")
        dp._adb("shell", "input", "keyevent", "66", tag="search_enter")
        time.sleep(5)
        # 3) 点第一个视频结果（取最长文案的节点当封面）
        root = dp._dump("visit_results")
        if root is None:
            return {"error": "搜索结果页读不到"}
        cands = []
        for n in root.iter("node"):
            t = re.sub(r"\s+", " ", str(n.get("text") or "")).strip()
            if len(t) >= 8 and not any(k in t for k in _NAV_WORDS):
                c = _center_of(n)
                if c:
                    cands.append((len(t), t, c))
        if not cands:
            return {"error": "搜索结果里没有可点的视频"}
        cands.sort(reverse=True)
        _, title0, c = cands[0]
        dp._tap(*c, source="搜索结果第一条视频")
        time.sleep(4)
        root = dp._dump("visit_video")
        texts = []
        if root is not None:
            for n in root.iter("node"):
                for raw in (n.get("text") or "", n.get("content-desc") or ""):
                    t = re.sub(r"\s+", " ", str(raw)).strip()
                    if t:
                        texts.append(t)
        cands2 = [t for t in texts if len(t) >= 8 and not any(k in t for k in _NAV_WORDS)]
        out["title"] = (max(cands2, key=len)[:80] if cands2 else title0)[:80]
        try:
            frame = (await dp._describe_video() or "").strip()
            if frame:
                out["frame"] = frame[:120]
        except Exception:  # noqa: BLE001
            pass
        try:
            dp._back_gesture()
            time.sleep(1)
            dp._back_gesture()
        except Exception:  # noqa: BLE001
            pass
        out["via"] = "phone_search"
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
    return out


async def _draft(url: str) -> str:
    # 先试"下载→抽帧→抽音轨→转文字"这条最完整的路（有链接时最准）
    _fr: dict = {}
    try:
        from plugins import douyin_fetch
        _fr = await douyin_fetch.fetch(url, with_audio=True)
        logger.info(f"[串门] 下载解析：title={bool(_fr.get('title'))} "
                    f"desc={bool(_fr.get('desc'))} asr={bool(_fr.get('transcript'))} "
                    f"err={_fr.get('error') or '-'}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[串门] 下载解析失败：{type(e).__name__}: {e}")
    meta = await _video_meta(url)
    if not meta.get("title"):
        # 浏览器抓不到（无头被风控 / 登录态过期）→ 换手机打开
        try:
            pm = await _phone_meta(url)
            for k, v in (pm or {}).items():
                if v and not meta.get(k):
                    meta[k] = v
        except Exception:
            pass
    if not meta.get("title"):
        # 手机深链也打不开（常见：被浏览器/更新弹窗拦掉）→ 用 App 内搜索找一条
        try:
            kw = str(LAST_VIDEO_META.get("keyword") or "蓝色大肥鱼")
            ps = await _phone_by_search(kw)
            for k, v in (ps or {}).items():
                if v and not meta.get(k):
                    meta[k] = v
        except Exception:
            pass
    LAST_VIDEO_META.clear()
    LAST_VIDEO_META.update(meta)
    title = meta.get("title", "")
    author = meta.get("author", "")
    summary = ""
    try:
        from plugins.media_parse import understand_url
        summary, _info = await understand_url(url)
    except Exception:
        summary = ""
    if not title and _fr.get("title"):
        title = str(_fr["title"])[:80]
    if _fr.get("desc"):
        summary = (summary + "｜" if summary else "") + "画面：" + str(_fr["desc"])[:120]
    if _fr.get("transcript"):
        summary = (summary + "｜" if summary else "") + "口播：" + str(_fr["transcript"])[:150]
    if not summary and meta.get("frame"):
        summary = str(meta["frame"])[:120]
    video_info = f"标题：{title or '（没抓到标题）'}\n作者：{author or '（没抓到作者）'}"
    if meta.get("likes"):
        video_info += f"\n热度：{meta['likes']} 赞"
    _r = _fr.get("ratios") or {}
    if _r:
        video_info += (f"\n互动比值：收藏/赞 {_r.get('save', 0)} · 转发/赞 {_r.get('share', 0)}"
                       f" · 评论/赞 {_r.get('discuss', 0)}")
    if summary:
        video_info += f"\n视频内容：{summary}"
    from plugins.ai_group import call_ai_raw
    scene = "你自己的视频（你是作者，不是路人）" if meta.get("own") else "别人一条抖音视频"
    prompt = (
        "你是「蓝色大肥鱼」，圆滚滚的蓝色鲸鱼拟人少女，傲娇嘴损但有分寸。"
        f"现在要在{scene}下留一句评论，视频信息：\n{video_info}\n"
        f"要求：20~35 字，像真人随口吐槽/接梗，最好提到视频里具体看到的东西；必须带 @{OWNER_NAME}；"
        "结尾加一个贴切 emoji；不要链接、不要营销、不要重复句式；只输出评论正文。"
        "只能引用上面给出的标题/画面/口播/比值里的具体细节，不要编造视频里没有的内容；"
        "如果信息明显不够，就写一句泛泛的吐槽，别硬凑细节。"
    )
    _txt = (await call_ai_raw(prompt, "deepseek", temperature=0.95, max_tokens=160)).strip()
    try:
        from plugins import moderation
        _safe = moderation.filter_public(_txt, channel="douyin")
        if not _safe:
            logger.info("[审核] 抖音串门草稿命中红线，本条不发")
        return _safe
    except Exception:  # noqa: BLE001
        return _txt


async def _post(url: str, text: str) -> tuple[bool, str]:
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            ctx = await pw.chromium.launch_persistent_context(
                str(PROFILE), channel="msedge", headless=True,
                viewport={"width": 1280, "height": 900},
            )
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto(url, timeout=60000)
            await page.wait_for_timeout(7000)
            try:
                await page.mouse.wheel(0, 1600)
                await page.wait_for_timeout(2000)
            except Exception:
                pass
            editor = page.locator(
                'div[contenteditable="true"], textarea[placeholder*="评论"], '
                'div[data-e2e="comment-input"]'
            ).last
            await editor.click(timeout=8000)
            await page.keyboard.type(text, delay=80)
            await page.wait_for_timeout(800)
            try:
                await page.get_by_text("发送", exact=True).last.click(timeout=5000)
            except Exception:
                await page.keyboard.press("Enter")
            await page.wait_for_timeout(2500)
            await ctx.close()
            return True, "已发送"
    except Exception as e:  # noqa: BLE001
        return False, f"发送失败：{type(e).__name__}: {e}"


@visit_cmd.handle()
async def _(event: PrivateMessageEvent):
    if str(event.user_id) != OWNER_QQ:
        return
    text = event.get_plaintext().strip()
    if not text.startswith("/串门"):
        return
    arg = text[len("/串门"):].strip()
    data = _today(_load())

    if arg in ("发", "发送"):
        pending = data.get("pending")
        if not pending:
            await visit_cmd.finish("没有待发草稿，先用 `/串门 视频链接` 生成。")
            return
        if data["count"] >= DAILY_LIMIT:
            await visit_cmd.finish(f"今天已经串门 {data['count']} 次，到上限了。")
            return
        if time.time() - float(data.get("last_at") or 0) < MIN_GAP:
            await visit_cmd.finish("离上一条不到 2 小时，先缓缓。")
            return
        ok, msg = await _post(pending["url"], pending["text"])
        if ok:
            data["count"] += 1
            data["last_at"] = time.time()
            data["pending"] = None
            _save(data)
            await visit_cmd.finish(f"✅ {msg}\n{pending['text']}")
        else:
            await visit_cmd.finish(f"❌ {msg}\n草稿还留着，可再试或取消。")
        return

    if arg in ("取消", "清空"):
        data["pending"] = None
        _save(data)
        await visit_cmd.finish("草稿已丢弃。")
        return

    if arg == "状态":
        p = data.get("pending") or {}
        await visit_cmd.finish(
            f"今日串门：{data['count']}/{DAILY_LIMIT}\n"
            f"草稿：{p.get('text') or '无'}\n"
            f"链接：{p.get('url') or '无'}"
        )
        return

    if arg in ("找", "找大肥鱼", "找鱼"):
        await visit_cmd.send("(*戴上潜水镜*) 本鱼去抖音搜搜谁在提我…")
        cands = await _search_candidates(8)
        if not cands:
            await visit_cmd.finish("没搜到相关视频（可能没登录或被风控），稍后再试。")
            return
        data["candidates"] = cands
        _save(data)
        lines = [f"{i + 1}. {c['title']}\n   {c['url']}" for i, c in enumerate(cands[:5])]
        await visit_cmd.finish(
            "🔎 搜到这些相关视频（只收集，没互动）：\n" + "\n".join(lines) +
            "\n\n选一个写草稿：`/串门 1` … `/串门 5`"
        )
        return

    if arg == "":
        await visit_cmd.send("(*戴上潜水镜*) 本鱼自己去找条合适的视频…")
        cands = await _search_candidates(8)
        if not cands:
            await visit_cmd.finish("没搜到相关视频（可能没登录或被风控），稍后再试；"
                                   "也可以直接 `/串门 <视频链接>`。")
            return
        data["candidates"] = cands
        _save(data)
        pick = random.choice(cands[:5])
        url = pick["url"]
        await visit_cmd.send(f"(*相中一条*) {str(pick.get('title') or '')[:40]}")
    elif arg.isdigit():
        cands = data.get("candidates") or []
        idx = int(arg) - 1
        if idx < 0 or idx >= len(cands):
            await visit_cmd.finish("编号不对：先发 `/串门 找` 列候选，再选编号。")
            return
        url = cands[idx]["url"]
    else:
        url = arg.split()[0] if arg else ""
    if not url.startswith("http"):
        await visit_cmd.finish("用法：`/串门`（自己找）或 `/串门 https://www.douyin.com/video/...`")
        return
    await visit_cmd.send("(*悄悄摸过去*) 本鱼先看看视频，写条评论草稿…")
    draft = await _draft(url)
    if not draft:
        await visit_cmd.finish("草稿没写出来，稍后再试。")
        return
    await visit_cmd.send(f"(*想好了*) 就留这句：\n{draft}")
    ok, msg = await _post(url, draft)
    if ok:
        data["count"] = int(data.get("count", 0)) + 1
        data["last_at"] = time.time()
        data["pending"] = None
        _save(data)
        await visit_cmd.finish("已经发出去了。")
    else:
        await visit_cmd.finish(f"没发出去：{msg}")


# 每天 10~21 点每小时看一眼：有概率自己挑一条视频、解析后写草稿，发主人确认
try:
    from nonebot_plugin_apscheduler import scheduler as _scheduler
except Exception:
    _scheduler = None


if _scheduler is not None:
    # 2026-11-06 按主人要求：去掉「每天 10~21 点自动逛」的定时。
    # 改由行动空间决定。函数保留可手动调。
    async def _auto_visit_job() -> None:
        try:
            data = _today(_load())
            if data.get("count", 0) >= 1:
                return
            if data.get("pending"):
                return
            if random.random() > 0.25:
                return
            cands = await _search_candidates(8)
            if not cands:
                return
            item = random.choice(cands)
            url = item.get("url") or ""
            if not url:
                return
            draft = await _draft(url)
            if not draft:
                return
            if LAST_VIDEO_META.get("own"):
                return
            ok, msg = await _post(url, draft)
            if not ok:
                return
            data["count"] = int(data.get("count", 0)) + 1
            data["last_at"] = time.time()
            _save(data)
            bot = get_bot()
            await bot.send_private_msg(
                user_id=int(OWNER_QQ),
                message=(
                    "(*自己刷到一条视频，蹲在评论区琢磨半天*)\n"
                    f"这条看着有点意思：{item.get('title') or url}\n\n"
                    f"我留了这句：\n{draft}"
                ),
            )
        except Exception as e:  # noqa: BLE001
            print(f"[串门] 自动任务异常：{e}")

