"""抖音视频内容获取：下载 → 抽帧 + 抽音轨 → 识图 + 转文字。

为什么要它
----------
手机那条路（ADB 截屏）只能看到"画面"，拿不到音频，而且没有 screenrecord 时
连多帧都受限。有视频链接时走这条路更完整：

    yt-dlp 下载 mp4（尽量带音频）
      → ffmpeg 抽 3 帧（0.2 / 0.5 / 0.8 位置）
      → ffmpeg 抽音轨转 16k wav
      → 帧丢识图链路，wav 丢本地 SenseVoice
      → 合成 {title, desc, transcript}

抖音反爬较严，yt-dlp 往往需要登录 cookie；这里按顺序尝试：
  1) --cookies-from-browser edge（你本机 Edge 的抖音登录态）
  2) 不带 cookie 直接下（公开视频有时可行）
全程静默失败，返回空 dict，由调用方决定降级。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from nonebot.log import logger

ROOT = Path(__file__).resolve().parent.parent


def _ffmpeg() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return "ffmpeg"


def _ffmpeg_dir() -> str:
    """yt-dlp 只认名为 ffmpeg(.exe) 的可执行文件。

    imageio-ffmpeg 给的是 ffmpeg-win-x86_64-v7.1.exe，名字不对 → 复制一份成
    data/bin/ffmpeg.exe，再把目录告诉 yt-dlp。
    """
    try:
        src = Path(_ffmpeg())
        d = ROOT / "data" / "bin"
        d.mkdir(parents=True, exist_ok=True)
        dst = d / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        if not dst.exists() or dst.stat().st_size != src.stat().st_size:
            import shutil as _sh
            _sh.copy2(src, dst)
        return str(d)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[媒体分析] 准备 ffmpeg 失败：{e}")
        return ""


def _download(url: str, out_dir: Path, browser_cookie: bool = True) -> tuple[Path | None, dict]:
    """返回 (视频路径, info)。优先带浏览器 cookie，失败再裸下。"""
    try:
        import yt_dlp
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[抖音下载] 没有 yt-dlp：{e}")
        return None, {}
    base = {
        "outtmpl": str(out_dir / "v.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        # 必须带音轨：B 站常把视频/音频分成两路，只下 mp4 会没有声音
        "format": "bv*[height<=720]+ba/b[height<=720]/b",
        "socket_timeout": 25,
        "retries": 1,
        "nocheckcertificate": True,
        "noprogress": True,
        "ffmpeg_location": _ffmpeg_dir(),
    }
    tries = [("no-cookie", {})]
    if browser_cookie:
        tries.insert(0, ("edge-cookie", {"cookiesfrombrowser": ("edge", None, None, None)}))
    for label, extra in tries:
        try:
            with yt_dlp.YoutubeDL({**base, **extra}) as ydl:
                info = ydl.extract_info(url, download=True)
            if not info:
                continue
            fp = info.get("requested_downloads") or []
            path = Path(fp[0]["filepath"]) if fp and fp[0].get("filepath") else None
            if path is None or not path.exists():
                cands = sorted(out_dir.glob("v.*"), key=lambda p: p.stat().st_size, reverse=True)
                path = cands[0] if cands else None
            if path and path.exists() and path.stat().st_size > 50 * 1024:
                logger.info(f"[抖音下载] {label} 成功：{path.name} {path.stat().st_size // 1024}KB")
                return path, info
            logger.info(f"[抖音下载] {label} 没拿到有效文件")
        except Exception as e:  # noqa: BLE001
            logger.info(f"[抖音下载] {label} 失败：{type(e).__name__}: {str(e)[:120]}")
    return None, {}


def _frames(video: Path, n: int = 3) -> list[bytes]:
    """抽 n 帧（按比例取，避开片头黑场）。"""
    ff = _ffmpeg()
    out: list[bytes] = []
    fracs = [0.2, 0.5, 0.8][:max(1, n)]
    for i, frac in enumerate(fracs):
        p = video.parent / f"f{i}.jpg"
        try:
            subprocess.run(
                [ff, "-y", "-ss", str(round(frac * 30, 1)), "-i", str(video),
                 "-frames:v", "1", "-vf", "scale=720:-1", str(p)],
                capture_output=True, timeout=30,
            )
            if p.exists() and p.stat().st_size > 2000:
                out.append(p.read_bytes())
        except Exception:  # noqa: BLE001
            continue
    return out


def _audio_wav(video: Path) -> bytes:
    """抽音轨转 16k 单声道 wav（SenseVoice 要求）。"""
    ff = _ffmpeg()
    wav = video.parent / "a.wav"
    try:
        subprocess.run(
            [ff, "-y", "-i", str(video), "-vn", "-ar", "16000", "-ac", "1",
             "-f", "wav", str(wav)],
            capture_output=True, timeout=90,
        )
        if wav.exists() and wav.stat().st_size > 2000:
            return wav.read_bytes()
    except Exception:  # noqa: BLE001
        pass
    return b""


# ---------------- 硬筛 + 互动结构打分（0 token，可调） ----------------
SHORT_SEC = 12          # 时长低于这个直接跳过
SHORT_TEXT = 12         # 文案长度低于这个……
MIN_INTERACTION = 3     # ……且互动数低于这个，才跳过（两个条件同时成立）

# 播放量不参与：抖音对自己的非作者视频经常返回 0，拿它打分等于瞎算


def _should_skip(meta: dict) -> tuple[bool, str]:
    """第 0 层硬筛：只看"有没有判断依据"，不看内容好坏。"""
    dur = int(meta.get("duration") or 0)
    if dur and dur < SHORT_SEC:
        return True, f"时长 {dur}s < {SHORT_SEC}s"
    title = str(meta.get("title") or "").strip()
    total = sum(int(meta.get(k) or 0) for k in ("like", "comment", "collect", "share"))
    if not title and not meta.get("video_url"):
        return True, "没文案也没媒体"
    if 0 < len(title) <= SHORT_TEXT and total < MIN_INTERACTION:
        return True, f"文案 {len(title)} 字且互动 {total} 条"
    return False, ""


def _interaction(meta: dict) -> dict:
    """第 1 层互动结构打分：关键不是点赞绝对值，是三个比值。

    收藏/赞 高 ⇒ 有保存价值；转发/赞 高 ⇒ 想转给别人；评论/赞 高 ⇒ 引发讨论。
    拿不到点赞数时返回中性 0.5，不阻断后续分析。
    """
    like = int(meta.get("like") or 0)
    if like <= 0:
        return {"save": 0.0, "share": 0.0, "discuss": 0.0, "score": 0.5}
    save = int(meta.get("collect") or 0) / like
    share = int(meta.get("share") or 0) / like
    discuss = int(meta.get("comment") or 0) / like
    score = round(min(1.0, save * 0.4 + share * 0.35 + discuss * 0.25), 3)
    return {"save": round(save, 3), "share": round(share, 3),
            "discuss": round(discuss, 3), "score": score}


async def fetch(url: str, with_audio: bool = True) -> dict:
    """下载 + 抽帧 + 抽音轨 + 识别，返回 {title, desc, transcript, error, cached}。"""
    hit = load_cached(url)
    if hit:
        logger.info(f"[抖音下载] 命中缓存（{_vid_of(url)}），不再下载")
        return hit
    out: dict = {"title": "", "desc": "", "transcript": "", "error": ""}
    tmp = Path(tempfile.mkdtemp(prefix="dyfetch_"))
    try:
        # 1) 用已登录浏览器抓标题 + 播放/音频地址（yt-dlp 过不了抖音签名）
        meta = await _run(_browser_extract, url)
        out["title"] = str(meta.get("title") or "")[:80]
        out["stats"] = {k: int(meta.get(k) or 0) for k in ("like", "comment", "collect", "share")}
        out["ratios"] = _interaction(meta)
        _skip, _why = _should_skip(meta)
        if _skip:
            # 证据都不够，不值当下载 + 抽帧 + 识图 + ASR
            logger.info(f"[抖音硬筛] 跳过 {_vid_of(url)}：{_why}")
            out["skipped"] = _why
            return out
        vurl = meta.get("video_url") or ""
        aurl = meta.get("audio_url") or ""
        video = None
        if vurl:
            cand = tmp / "v.mp4"
            if await _run(_http_download, vurl, cand):
                video = cand
        if video is None:
            # 浏览器没抓到就退回 yt-dlp 试一次
            video, info = await _run(_download, url, tmp)
            if not out["title"]:
                out["title"] = str((info or {}).get("title") or "")[:80]
        if video is None:
            out["error"] = "下载失败（浏览器与 yt-dlp 都没拿到媒体）"
            return out
        # 2) 抽帧识图
        frames = await _run(_frames, video, 3)
        if frames:
            try:
                from plugins.ai_group import _describe_images
                descs = await _describe_images([f"data:image/jpeg;base64,{_b64(f)}" for f in frames])
                out["desc"] = " ｜ ".join(d for d in (descs or []) if d)[:200]
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[抖音下载] 抽帧识图失败：{type(e).__name__}: {e}")
        # 3) 音频转文字（优先单独下音频，失败就从视频抽音轨）
        if with_audio:
            # ⚠️ 顺序很重要：music.play_url 是"背景音乐"，转出来是歌词。
            # 说话内容在视频音轨里，所以先抽视频音轨，实在没有才退到 BGM。
            wav = await _run(_audio_wav, video)
            if not wav and aurl:
                ap = tmp / "a.m4a"
                if await _run(_http_download, aurl, ap):
                    wav = await _run(_to_wav16k, ap)
            if wav:
                try:
                    import asr
                    raw = (await asr._transcribe_wav(wav) or "").strip()
                    text, why = asr.guard_transcript(raw)
                    if text:
                        out["transcript"] = text[:400]
                    else:
                        out["transcript_verdict"] = why
                        logger.warning(f"[抖音下载] 转写不可信（{why}），不写口播：{raw[:40]!r}")
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[抖音下载] 音频转写失败：{type(e).__name__}: {e}")
        out["url"] = url
        out["video_id"] = _vid_of(url)
        out["duration"] = meta.get("duration") or 0
        try:
            await _run(save_cached, url, out, video)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[抖音下载] 缓存保存失败：{type(e).__name__}: {e}")
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _to_wav16k(src: Path) -> bytes:
    """把任意音频转 16k 单声道 wav。"""
    ff = _ffmpeg()
    wav = src.parent / "a16.wav"
    try:
        subprocess.run([ff, "-y", "-i", str(src), "-vn", "-ar", "16000", "-ac", "1",
                        "-f", "wav", str(wav)], capture_output=True, timeout=120)
        if wav.exists() and wav.stat().st_size > 2000:
            return wav.read_bytes()
    except Exception:  # noqa: BLE001
        pass
    return b""


def _b64(raw: bytes) -> str:
    import base64
    return base64.b64encode(raw).decode()


async def _run(fn, *args):
    import asyncio
    return await asyncio.to_thread(fn, *args)

# ---------------- 缓存：视频旁边放一份同名分析文本 ----------------
CACHE_DIR = ROOT / "data" / "media_cache" / "douyin"
CACHE_TTL = 45 * 86400          # 45 天内直接用缓存，不再下载


def _vid_of(url: str) -> str:
    """从链接/短链里取出视频 id（取不到就用链接哈希）。"""
    import hashlib
    import re
    m = re.search(r"(?:video|note|share/video)/(\d{6,})", str(url))
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=(\d{6,})", str(url))
    if m:
        return m.group(1)
    return hashlib.md5(str(url).encode()).hexdigest()[:16]


def _cache_paths(vid: str) -> tuple[Path, Path]:
    return CACHE_DIR / f"{vid}.mp4", CACHE_DIR / f"{vid}.txt"


def load_cached(url: str) -> dict | None:
    """有缓存分析文本就直接用（下次调用不用重复看视频）。"""
    import json as _j
    import re as _re
    vid = _vid_of(url)
    vid_path, txt_path = _cache_paths(vid)
    try:
        if not txt_path.exists():
            return None
        import time as _t
        if _t.time() - txt_path.stat().st_mtime > CACHE_TTL:
            return None
        raw = txt_path.read_text(encoding="utf-8")
        # 文本文件第一段是给人看的摘要，后面是 JSON
        m = _re.search(r"\{.*\}", raw, _re.S)
        if m:
            d = _j.loads(m.group(0))
            d["cached"] = True
            d["video_path"] = str(vid_path) if vid_path.exists() else ""
            return d
    except Exception:  # noqa: BLE001
        return None
    return None


def save_cached(url: str, info: dict, video: Path | None) -> str:
    """把分析结果写成同目录同名 txt（人看摘要 + JSON 结构）。"""
    import json as _j
    import time as _t
    vid = _vid_of(url)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    vid_path, txt_path = _cache_paths(vid)
    if video is not None and video.exists():
        try:
            import shutil as _sh
            _sh.copy2(video, vid_path)
        except Exception:  # noqa: BLE001
            pass
    verdict = info.get("transcript_verdict")
    transcript_line = info.get("transcript") or (
        f"（转写不可信：{verdict}）" if verdict else "（无音轨/未转写）")
    head = [
        f"标题：{info.get('title') or '（无）'}",
        f"链接：{url}",
        f"分析时间：{_t.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"画面：{info.get('desc') or '（未识别）'}",
        "",
        f"口播：{transcript_line}",
        "",
        "---- 以下为结构化数据（程序读取用）----",
        _j.dumps(info, ensure_ascii=False, indent=1),
    ]
    try:
        txt_path.write_text("\n".join(head), encoding="utf-8")
        logger.info(f"[抖音下载] 分析已缓存：{txt_path.name}")
        return str(txt_path)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[抖音下载] 写缓存失败：{e}")
        return ""

# ---------------- 浏览器抓取（绕开 yt-dlp 的签名要求） ----------------

PROFILE = ROOT / "data" / "douyin_profile"


def _browser_extract(url: str, timeout: float = 30.0) -> dict:
    """用已登录的 Edge 打开视频页，截 aweme/detail 的 JSON，拿标题和播放地址。

    yt-dlp 的抖音解析要求 msToken 等签名 cookie，纯 cookie 文件过不了；
    而浏览器里页面自己能拿到数据——我们只要把那个响应截下来。
    """
    import asyncio
    import json as _j

    async def _run() -> dict:
        from playwright.async_api import async_playwright
        found: dict = {}

        async def on_resp(resp):
            try:
                if "/aweme/v1/web/aweme/detail/" not in resp.url:
                    return
                txt = await resp.text()
                data = _j.loads(txt)
                detail = (data.get("aweme_detail") or {})
                if not detail:
                    return
                video = detail.get("video") or {}
                play = ((video.get("play_addr") or {}).get("url_list") or [])
                music = ((detail.get("music") or {}).get("play_url") or {}).get("url_list") or []
                stats = detail.get("statistics") or {}
                found.update({
                    "title": str(detail.get("desc") or "")[:120],
                    "video_url": (play[0] if play else ""),
                    "audio_url": (music[0] if music else ""),
                    "duration": int((video.get("duration") or 0) / 1000) if video.get("duration") else 0,
                    "like": int(stats.get("digg_count") or 0),
                    "comment": int(stats.get("comment_count") or 0),
                    "collect": int(stats.get("collect_count") or 0),
                    "share": int(stats.get("share_count") or 0),
                })
            except Exception:  # noqa: BLE001
                pass

        async with async_playwright() as pw:
            ctx = await pw.chromium.launch_persistent_context(
                str(PROFILE), channel="msedge", headless=True,
                viewport={"width": 1280, "height": 900})
            try:
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                page.on("response", lambda r: asyncio.create_task(on_resp(r)))
                await page.goto(url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
                for _ in range(int(timeout / 1.0)):
                    if found.get("video_url"):
                        break
                    await asyncio.sleep(1.0)
            finally:
                await ctx.close()
        return found

    try:
        return asyncio.run(_run())
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[抖音下载] 浏览器抓取失败：{type(e).__name__}: {str(e)[:120]}")
        return {}


def _http_download(url: str, out: Path, ua: str = "") -> bool:
    """用普通 http 下媒体文件（播放地址带签名，不需要 cookie）。"""
    try:
        import httpx
        headers = {"User-Agent": ua or "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                   "Referer": "https://www.douyin.com/"}
        with httpx.stream("GET", url, headers=headers, timeout=120, follow_redirects=True) as r:
            if r.status_code != 200:
                return False
            with out.open("wb") as f:
                for chunk in r.iter_bytes(65536):
                    f.write(chunk)
        return out.exists() and out.stat().st_size > 50 * 1024
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[抖音下载] http 下载失败：{type(e).__name__}: {str(e)[:100]}")
        return False
