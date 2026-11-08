"""B 站下载：默认 1080P，存到机器人目录下的「B站视频」文件夹。

以前走 douyin_fetch 的通用下载：画质写死 720、临时目录在 C 盘。
分镜要看清画面（字幕、表情、小字），720 不够；缓存也不该落在系统盘。
"""
from __future__ import annotations

import ast
import json
import os
import tempfile
from pathlib import Path

try:
    from nonebot.log import logger
except Exception:  # noqa: BLE001
    import logging
    logger = logging.getLogger("bili_dl")

ROOT = Path(__file__).resolve().parent.parent
VIDEO_DIR = Path(os.getenv("BILI_VIDEO_DIR", str(ROOT / "B站视频")))
COOKIE_JSON = ROOT / ".bili_cookies.json"

MAX_HEIGHT = int(os.getenv("BILI_MAX_HEIGHT", "1080") or "1080")
MAX_FILESIZE_MB = int(os.getenv("BILI_MAX_FILESIZE_MB", "800") or "800")


def is_bili(url: str) -> bool:
    u = str(url or "").lower()
    return "bilibili.com" in u or "b23.tv" in u or "bili2233" in u


def _ffmpeg_dir() -> str:
    """yt-dlp 只认名为 ffmpeg.exe 的可执行文件。"""
    try:
        import shutil
        import imageio_ffmpeg
        src = Path(imageio_ffmpeg.get_ffmpeg_exe())
        dst_dir = ROOT / "data" / "bin"
        dst = dst_dir / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        dst_dir.mkdir(parents=True, exist_ok=True)
        if not dst.exists() or dst.stat().st_size != src.stat().st_size:
            shutil.copy2(src, dst)
        return str(dst_dir)
    except Exception:  # noqa: BLE001
        return ""


def _cookie_dict() -> dict:
    """读 .bili_cookies.json → {name: value}。兼容 dict / 字符串化 dict / 列表。"""
    try:
        data = json.loads(COOKIE_JSON.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        return {}
    ck = data.get("cookies") if isinstance(data, dict) else None
    if isinstance(ck, str):
        try:
            ck = ast.literal_eval(ck)
        except Exception:  # noqa: BLE001
            ck = {}
    if isinstance(ck, list):
        return {str(c.get("name")): c.get("value") for c in ck if isinstance(c, dict) and c.get("name")}
    if isinstance(ck, dict):
        return {str(k): v for k, v in ck.items()}
    return {}


def cookie_file() -> str:
    """把 cookie 转成 yt-dlp 认的 Netscape 文件；没有 cookie 返回空串。"""
    jar = _cookie_dict()
    if not jar:
        return ""
    try:
        lines = ["# Netscape HTTP Cookie File"]
        for k, v in jar.items():
            if not k or v is None:
                continue
            lines.append(f".bilibili.com\tTRUE\t/\tFALSE\t0\t{k}\t{v}")
        p = Path(tempfile.gettempdir()) / "bili_cookies_netscape.txt"
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(p)
    except Exception:  # noqa: BLE001
        return ""


def _write_sidecar(video: Path, info: dict) -> Path:
    """下载完在视频旁边写一份 txt：标题 / 链接 / 时长 + 有分析文本就一起带上。"""
    url = str(info.get("webpage_url") or info.get("original_url") or "")
    title = str(info.get("title") or "")
    dur = info.get("duration")
    lines = [
        f"标题：{title}",
        f"链接：{url}",
        f"UP主：{info.get('uploader') or ''}",
        f"时长：{int(dur) // 60}分{int(dur) % 60}秒" if dur else "时长：?",
        f"下载时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
    ]
    # 有缓存分析文本就带上（观后感）
    try:
        from plugins import media_fetch
        cached = media_fetch.load_cached(url)
        if cached:
            text = str(cached.get("analysis") or cached.get("text") or "")
            if text:
                # 观后感落盘前去一次 AI 味（规则层，零成本）
                try:
                    from plugins.humanize import strip_cliches
                    _hz = strip_cliches(text)
                    if _hz:
                        text = _hz
                except Exception:  # noqa: BLE001
                    pass
                lines += ["", "── 观后感 ──", text]
    except Exception:  # noqa: BLE001
        pass
    txt = video.with_suffix(".txt")
    txt.write_text(chr(10).join(lines), encoding="utf-8")
    logger.info(f"[B站下载] 观后感已写 {txt.name}")
    return txt


def download(url: str, out_dir: Path | str | None = None,
             max_height: int | None = None) -> tuple[Path | None, dict, str]:
    """下载 B 站视频。返回 (路径, info, 错误)。"""
    try:
        import yt_dlp
    except Exception as e:  # noqa: BLE001
        return None, {}, f"没有 yt-dlp：{e}"
    h = int(max_height or MAX_HEIGHT)
    target = Path(out_dir) if out_dir else VIDEO_DIR
    target.mkdir(parents=True, exist_ok=True)
    # 三种画质按顺序退让：1080 → 720 → 最好
    formats = [
        f"bv*[height<={h}]+ba/b[height<={h}]/b",
        "bv*[height<=720]+ba/b[height<=720]/b",
        "bv*+ba/b",
    ]
    base = {
        "outtmpl": str(target / "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 25,
        "retries": 2,
        "nocheckcertificate": True,
        "noprogress": True,
        "merge_output_format": "mp4",
        "max_filesize": MAX_FILESIZE_MB * 1024 * 1024,
    }
    ff = _ffmpeg_dir()
    if ff:
        base["ffmpeg_location"] = ff
    cf = cookie_file()
    if cf:
        base["cookiefile"] = cf

    last_err = ""
    for fmt in formats:
        try:
            with yt_dlp.YoutubeDL({**base, "format": fmt}) as ydl:
                info = ydl.extract_info(url, download=True)
            if not info:
                continue
            rd = info.get("requested_downloads") or []
            path = Path(rd[0]["filepath"]) if rd and rd[0].get("filepath") else None
            if path is None or not path.exists():
                cands = sorted(target.glob(f"{info.get('id')}.*"),
                               key=lambda p: p.stat().st_size, reverse=True)
                path = cands[0] if cands else None
            if path and path.exists() and path.stat().st_size > 50 * 1024:
                height = info.get("height") or (rd[0].get("height") if rd else None)
                logger.info(f"[B站下载] {path.name} {path.stat().st_size // 1024}KB "
                            f"画质={height or '?'}p")
                try:
                    _write_sidecar(path, info)
                except Exception as _e:  # noqa: BLE001
                    logger.warning(f"[B站下载] 写观后感 txt 失败：{type(_e).__name__}: {_e}")
                return path, info, ""
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {str(e)[:160]}"
            logger.warning(f"[B站下载] 画质 {fmt} 失败：{last_err}")
            continue
    return None, {}, last_err or "下载失败"

# ---------------------------------------------------------------- WBI 签名
# B站 /x/player/v2 已被 wbi/v2 取代：不带签名时返回的字幕会串到别的视频。
_MIXIN_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]


def _wbi_keys(jar: dict) -> tuple[str, str]:
    try:
        import httpx
        r = httpx.get("https://api.bilibili.com/x/web-interface/nav", timeout=15,
                      headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"},
                      cookies=jar or None).json()
        wbi = ((r.get("data") or {}).get("wbi_img") or {})
        img = str(wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
        sub = str(wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
        return img, sub
    except Exception:  # noqa: BLE001
        return "", ""


def _wbi_sign(params: dict, img_key: str, sub_key: str) -> dict:
    import hashlib
    import time as _t
    raw = img_key + sub_key
    mixin = "".join(raw[i] for i in _MIXIN_TAB if i < len(raw))[:32]
    p = dict(params)
    p["wts"] = int(_t.time())
    items = []
    for k in sorted(p):
        v = str(p[k])
        v = "".join(ch for ch in v if ch not in "!'()*")
        items.append(f"{k}={v}")
    query = "&".join(items)
    p["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()
    return p

def fetch_subtitles(url: str) -> list[dict]:
    """拉 B 站自带字幕（CC / AI 字幕），返回 [{"from":秒,"to":秒,"text":...}]。

    字幕是平台给的，比自己跑 ASR 准，而且带时间码、不花钱。
    拿不到就返回空列表，调用方照常出图（只是没有台词）。
    """
    import re as _re
    try:
        import httpx
    except Exception:  # noqa: BLE001
        return []
    m = _re.search(r"(BV[0-9A-Za-z]{10})", str(url or ""))
    if not m:
        return []
    bvid = m.group(1)
    jar = _cookie_dict()
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"}
    try:
        with httpx.Client(timeout=20, headers=headers, cookies=jar or None) as cli:
            v = cli.get("https://api.bilibili.com/x/web-interface/view", params={"bvid": bvid}).json()
            data = v.get("data") or {}
            cid = data.get("cid") or ((data.get("pages") or [{}])[0].get("cid"))
            if not cid:
                return []
            img_k, sub_k = _wbi_keys(jar)
            params = {"bvid": bvid, "cid": cid}
            if img_k and sub_k:
                params = _wbi_sign(params, img_k, sub_k)
                api = "https://api.bilibili.com/x/player/wbi/v2"
            else:
                api = "https://api.bilibili.com/x/player/v2"
            p2 = cli.get(api, params=params).json()
            subs = (((p2.get("data") or {}).get("subtitle") or {}).get("subtitles") or [])
            if not subs:
                return []
            # 优先中文字幕
            subs.sort(key=lambda x: 0 if "zh" in str(x.get("lan")) else 1)
            # 逐个候选试：subtitle_url 有 "//host/..."、"/bfs/..."、完整 https 三种写法
            for cand in subs:
                su = str(cand.get("subtitle_url") or "").strip()
                if not su:
                    continue
                if su.startswith("//"):
                    su = "https:" + su
                elif su.startswith("/"):
                    su = "https://aisubtitle.hdslb.com" + su
                try:
                    j = cli.get(su).json()
                except Exception:  # noqa: BLE001
                    continue
                out = []
                for it in (j.get("body") or []):
                    txt = str(it.get("content") or "").strip()
                    if not txt:
                        continue
                    out.append({"from": float(it.get("from") or 0),
                                "to": float(it.get("to") or 0),
                                "text": txt})
                if out:
                    # 兜底校验：字幕时间不能超过视频时长太多（防串到别人视频）
                    try:
                        dur = float(data.get("duration") or 0)
                    except Exception:  # noqa: BLE001
                        dur = 0.0
                    if dur and max(x["to"] for x in out) > dur * 1.2 + 10:
                        logger.warning(f"[B站字幕] {bvid} 字幕时长对不上（{max(x['to'] for x in out):.0f}s > 视频 {dur:.0f}s），丢弃")
                        continue
                    logger.info(f"[B站字幕] {bvid} 拿到 {len(out)} 条（{cand.get('lan')}）")
                    return out
            return []
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[B站字幕] 拉取失败：{type(e).__name__}: {e}")
        return []

def video_meta(url: str) -> dict:
    """查视频基本信息 + 分P（合集）。

    返回 {"bvid","title","total","pages":[{page,part,duration}]}；
    查不到就 total=1，按单集处理。
    """
    import re as _re
    m = _re.search(r"(BV[0-9A-Za-z]{10})", str(url or ""))
    out = {"bvid": m.group(1) if m else "", "title": "", "total": 1, "pages": []}
    if not out["bvid"]:
        return out
    try:
        import httpx
        r = httpx.get("https://api.bilibili.com/x/web-interface/view",
                      params={"bvid": out["bvid"]}, timeout=20,
                      headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"},
                      cookies=_cookie_dict() or None)
        d = (r.json() or {}).get("data") or {}
        pages = d.get("pages") or []
        out["title"] = str(d.get("title") or "")
        out["pages"] = [{"page": int(pg.get("page") or i + 1),
                         "part": str(pg.get("part") or f"P{i + 1}")[:60],
                         "duration": int(pg.get("duration") or 0)}
                        for i, pg in enumerate(pages)]
        out["total"] = len(pages) or 1
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[B站] 查分P失败：{type(e).__name__}: {e}")
    return out


def with_part(url: str, part: int) -> str:
    """把 ?p=N 拼到链接上（1 就不拼）。"""
    if int(part or 1) <= 1:
        return url
    base = str(url or "").split("?")[0]
    return f"{base}?p={int(part)}"
