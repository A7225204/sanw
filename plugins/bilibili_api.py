"""B 站（哔哩哔哩）内容接口的薄封装：只做取数和排版，不依赖 nonebot。

给 `plugins/bilibili.py` 用，也方便单独跑测试：

    python -c "import asyncio, plugins.bilibili_api as b; print(asyncio.run(b.video_card('BV1GJ411x7h7')))"

全部走 B 站公开 Web 接口（不需要登录、不需要 Cookie 登录态）：
* 视频详情  https://api.bilibili.com/x/web-interface/view?bvid=...
* 视频搜索  https://api.bilibili.com/x/web-interface/search/type?search_type=video
* 用户搜索  https://api.bilibili.com/x/web-interface/search/type?search_type=bili_user
* 热搜      https://api.bilibili.com/x/web-interface/search/square
* 热门      https://api.bilibili.com/x/web-interface/popular
* 排行榜    https://api.bilibili.com/x/web-interface/ranking/v2
"""
from __future__ import annotations

import re
import time
import uuid

import httpx

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    # 没有登录态时也带一个 buvid3，能少踩一部分风控
    "Cookie": f"buvid3={uuid.uuid4().hex.upper()}infoc",
}

BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")
AID_RE = re.compile(r"(?:^|[^0-9A-Za-z])av(\d{3,})", re.I)
LINK_RE = re.compile(r"https?://[^\s\"'<>）)】]+")


class BiliError(RuntimeError):
    pass


async def _get_json(url: str, params: dict | None = None) -> dict:
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, headers=HEADERS) as client:
                resp = await client.get(url, params=params)
                data = resp.json()
                if data.get("code") not in (0, None):
                    raise BiliError(str(data.get("message") or f"code={data.get('code')}"))
                return data
        except BiliError:
            raise
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt == 0:
                time.sleep(0.8)
    raise BiliError(f"请求失败：{last_error}")


# ------------------------------------------------------------------ 工具

def fmt_count(value) -> str:
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        return "0"
    if n >= 100_000_000:
        return f"{n / 100_000_000:.1f}亿"
    if n >= 10_000:
        return f"{n / 10_000:.1f}万"
    return str(n)


def fmt_duration(seconds) -> str:
    try:
        s = int(seconds or 0)
    except (TypeError, ValueError):
        s = 0
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def strip_em(text: str) -> str:
    text = re.sub(r"</?em[^>]*>", "", text or "")
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def https(url: str) -> str:
    url = (url or "").strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http://"):
        return "https://" + url[len("http://"):]
    return url


def extract_bvid(text: str) -> str | None:
    m = BV_RE.search(text or "")
    return m.group(0) if m else None


def extract_aid(text: str) -> int | None:
    m = AID_RE.search(text or "")
    return int(m.group(1)) if m else None


async def resolve_video(url: str) -> tuple[str | None, int | None]:
    """短链（b23.tv / bili2233.cn）或普通视频链接 → (BV 号, av 号)。"""
    url = url.strip()
    bvid = extract_bvid(url)
    if bvid:
        return bvid, None
    aid = extract_aid(url)
    if aid:
        return None, aid
    try:
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True, headers=HEADERS) as client:
            resp = await client.get(url)
            final = str(resp.url)
    except Exception as exc:  # noqa: BLE001
        raise BiliError(f"短链打不开：{exc}") from exc
    return extract_bvid(final), extract_aid(final)


async def resolve_bvid(url: str) -> str | None:
    """兼容旧调用：只返回 BV 号。"""
    bvid, _aid = await resolve_video(url)
    return bvid


# ------------------------------------------------------------------ 取数

async def video_info(bvid: str | None = None, aid: int | None = None) -> dict:
    params = {"bvid": bvid} if bvid else {"aid": aid}
    data = await _get_json("https://api.bilibili.com/x/web-interface/view", params)
    return data.get("data") or {}


async def video_card(bvid: str | None = None, aid: int | None = None) -> dict:
    """视频详情 → {"text": str, "cover": https_url}"""
    v = await video_info(bvid=bvid, aid=aid)
    if not v.get("bvid"):
        raise BiliError("这个 BV/av 号查不到视频")
    title = (v.get("title") or "无标题").strip()
    owner = (v.get("owner") or {}).get("name") or "未知 UP"
    stat = v.get("stat") or {}
    desc = re.sub(r"\s+", " ", (v.get("desc") or "")).strip()
    if desc in ("-", "—"):
        desc = ""
    if len(desc) > 70:
        desc = desc[:70] + "…"
    lines = [
        f"📺 {title}",
        f"👤 UP：{owner} ｜ ⏱ {fmt_duration(v.get('duration'))}",
        f"▶ {fmt_count(stat.get('view'))} 播放 ｜ 💬 {fmt_count(stat.get('danmaku'))} 弹幕 ｜ 👍 {fmt_count(stat.get('like'))}",
    ]
    if desc:
        lines.append(f"📝 {desc}")
    lines.append(f"🔗 https://www.bilibili.com/video/{v.get('bvid')}")
    return {"text": "\n".join(lines), "cover": https(v.get("pic") or "")}


async def search_videos(keyword: str, limit: int = 5) -> list[dict]:
    data = await _get_json(
        "https://api.bilibili.com/x/web-interface/search/type",
        {"search_type": "video", "keyword": keyword, "page": 1, "page_size": max(limit, 5)},
    )
    result = (data.get("data") or {}).get("result") or []
    out = []
    for item in result[:limit]:
        out.append({
            "bvid": item.get("bvid") or "",
            "title": strip_em(item.get("title")),
            "author": item.get("author") or "",
            "play": item.get("play"),
            "duration": item.get("duration"),
            "pic": https(item.get("pic") or ""),
        })
    return out


async def search_users(keyword: str, limit: int = 3) -> list[dict]:
    data = await _get_json(
        "https://api.bilibili.com/x/web-interface/search/type",
        {"search_type": "bili_user", "keyword": keyword, "page": 1, "page_size": 5},
    )
    result = (data.get("data") or {}).get("result") or []
    out = []
    for item in result[:limit]:
        out.append({
            "mid": item.get("mid"),
            "uname": strip_em(item.get("uname")),
            "fans": item.get("fans"),
            "videos": item.get("videos"),
            "usign": strip_em(item.get("usign") or ""),
        })
    return out


async def trending(limit: int = 10) -> list[dict]:
    data = await _get_json("https://api.bilibili.com/x/web-interface/search/square", {"limit": limit})
    lst = (((data.get("data") or {}).get("trending") or {}).get("list")) or []
    return [{"keyword": strip_em(x.get("keyword") or x.get("show_name"))} for x in lst[:limit]]


async def popular(limit: int = 10) -> list[dict]:
    data = await _get_json("https://api.bilibili.com/x/web-interface/popular", {"ps": limit, "pn": 1})
    lst = (data.get("data") or {}).get("list") or []
    out = []
    for item in lst[:limit]:
        out.append({
            "bvid": item.get("bvid"),
            "title": strip_em(item.get("title")),
            "author": (item.get("owner") or {}).get("name") or "",
            "view": (item.get("stat") or {}).get("view"),
            "duration": item.get("duration"),
        })
    return out


async def ranking(limit: int = 10) -> list[dict]:
    data = await _get_json("https://api.bilibili.com/x/web-interface/ranking/v2", {"rid": 0, "type": "all"})
    lst = (data.get("data") or {}).get("list") or []
    out = []
    for item in lst[:limit]:
        out.append({
            "bvid": item.get("bvid"),
            "title": strip_em(item.get("title")),
            "author": (item.get("owner") or {}).get("name") or "",
            "view": (item.get("stat") or {}).get("view"),
            "duration": item.get("duration"),
        })
    return out


# ------------------------------------------------------------------ 排版

def render_list(title: str, items: list[dict], kind: str) -> str:
    lines = [title]
    for idx, item in enumerate(items, 1):
        if kind == "keyword":
            lines.append(f"{idx}. {item['keyword']}")
        elif kind == "user":
            sign = f"｜{item['usign'][:30]}" if item.get("usign") else ""
            lines.append(
                f"{idx}. {item['uname']}（粉丝 {fmt_count(item.get('fans'))}｜视频 {item.get('videos') or 0}）{sign}\n"
                f"   https://space.bilibili.com/{item.get('mid')}"
            )
        else:
            duration = item.get("duration")
            if isinstance(duration, (int, float)):
                duration = fmt_duration(duration)
            duration = duration or ""
            lines.append(
                f"{idx}. {item['title']}\n"
                f"   {item.get('author') or '未知 UP'} ｜ {fmt_count(item.get('view') or item.get('play'))} 播放 ｜ {duration}\n"
                f"   https://www.bilibili.com/video/{item.get('bvid')}"
            )
    return "\n".join(lines)


if __name__ == "__main__":
    import asyncio

    async def _demo():
        print(await video_card("BV1GJ411x7h7"))
        print(render_list("热搜", await trending(5), "keyword"))
        print(render_list("搜索", await search_videos("原神", 3), "video"))

    asyncio.run(_demo())
