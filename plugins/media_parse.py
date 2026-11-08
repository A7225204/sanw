"""媒体解析：链接 → yt-dlp 下载 → 抽帧 → Gemini 看懂视频 → 以大肥鱼口吻总结。"""
from __future__ import annotations

import asyncio
import re
import tempfile
import time
from pathlib import Path

from nonebot import on_message
from nonebot.adapters.onebot.v11 import PrivateMessageEvent

ROOT = Path(__file__).resolve().parent.parent
OWNER_QQ = "100000000"

parse_cmd = on_message(priority=7, block=False)


def _download(url: str) -> tuple[Path | None, dict, str]:
    # B站：1080P + 存到机器人目录的「B站视频」
    try:
        from plugins import bili_dl
        if bili_dl.is_bili(url):
            path, info, err = bili_dl.download(url)
            if path:
                return path, info, ""
    except Exception as e:  # noqa: BLE001
        print(f"[media_parse] 1080P 下载异常，回退：{type(e).__name__}: {e}")
    import yt_dlp
    outdir = Path(tempfile.mkdtemp(prefix="media_"))
    outtmpl = str(outdir / "video.%(ext)s")
    opts = {
        "outtmpl": outtmpl,
        "format": "bv*[height<=480]+ba/b[height<=480]/b",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "max_filesize": 30 * 1024 * 1024,
        "merge_output_format": "mp4",
    }
    try:
        import imageio_ffmpeg
        opts["ffmpeg_location"] = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        files = list(outdir.glob("video.*"))
        return (files[0] if files else None), (info or {}), ""
    except Exception as e:  # noqa: BLE001
        return None, {}, str(e)[:200]


def _frames(video: Path) -> list[bytes]:
    try:
        import imageio_ffmpeg
        import subprocess
        ff = imageio_ffmpeg.get_ffmpeg_exe()
        dur = 10.0
        try:
            probe = subprocess.run([ff, "-i", str(video)], capture_output=True, text=True, errors="ignore")
            m = re.search(r"Duration: (\d+):(\d+):(\d+)", probe.stderr or "")
            if m:
                dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
        except Exception:
            pass
        out: list[bytes] = []
        for i, pos in enumerate((0.1, 0.5, 0.85)):
            t = max(0.5, dur * pos)
            p = video.parent / f"frame_{i}.jpg"
            subprocess.run([ff, "-y", "-ss", str(t), "-i", str(video), "-frames:v", "1",
                            "-vf", "scale=640:-1", str(p)], capture_output=True)
            if p.exists():
                out.append(p.read_bytes())
        return out
    except Exception:
        return []


async def _understand_storyboard(video: Path, info: dict, url: str = "") -> str:
    """一张宫格图 + 时间清单，一次识图概括整段。"""
    title = str(info.get("title") or "")
    uploader = str(info.get("uploader") or "")
    desc = str(info.get("description") or "")[:300]
    try:
        from plugins import storyboard as sb
        info_sb = await asyncio.to_thread(sb.probe, str(video))
        dur = float(info_sb.get("duration") or 0)
        subs: list[dict] = []
        try:
            from plugins import bili_dl
            if bili_dl.is_bili(url):
                subs = await asyncio.to_thread(bili_dl.fetch_subtitles, url)
        except Exception:  # noqa: BLE001
            subs = []
        sheets = await asyncio.to_thread(sb.build_sheets, str(video), 300.0, 9, None, 4, subs)
        if not sheets:
            return ""
        import base64 as _b64
        from plugins.ai_group import _describe_images
        parts: list[str] = []
        for sh in sheets[:4]:
            raw = Path(sh["png"]).read_bytes()
            uri = "data:image/png;base64," + _b64.b64encode(raw).decode()
            ds = await _describe_images([uri], hint=(
                f"这是视频分镜宫格（每格有格号和时间码），覆盖 {sh['start']:.0f}~{sh['end']:.0f} 秒。"
                "用 80 字说明这段发生了什么：画面主体、动作、场景变化，不要逐格罗列。"))
            txt = " ".join(d for d in (ds or []) if d).strip()
            if txt:
                parts.append(f"[{sh['start']:.0f}-{sh['end']:.0f}s] {txt}")
        if not parts:
            return ""
        shots = "；".join(parts)
        prompt = (f"视频标题：{title}\n作者：{uploader}\n简介：{desc}\n"
                  f"分镜要点（按时间）：{shots}\n\n"
                  "用一句话（40 字内）概括这条视频讲什么，口吻像人看完随手评价，不要逐条复述。")
        try:
            from plugins import gemini
            out = await gemini.gemini_chat(prompt)
            if out:
                return str(out).strip()[:200]
        except Exception:  # noqa: BLE001
            pass
        # 没 gemini 就直接用分镜要点
        return shots[:260]
    except Exception as e:  # noqa: BLE001
        print(f"[media_parse] 分镜失败: {type(e).__name__}: {e}")
        return ""


async def _understand(info: dict, frames: list[bytes]) -> str:
    title = str(info.get("title") or "")
    desc = str(info.get("description") or "")[:500]
    uploader = str(info.get("uploader") or "")
    parts = []
    try:
        from plugins import gemini
        for i, img in enumerate(frames[:3]):
            d = await gemini.gemini_vision(
                "描述这张视频截图的画面：人物/动作/场景，30字内，不要描述界面。",
                img, mime="image/jpeg",
            )
            if d:
                parts.append(d.strip())
        if parts:
            prompt = (
                f"视频标题：{title}\n作者：{uploader}\n简介：{desc}\n画面：{'；'.join(parts)}\n"
                "用「蓝色大肥鱼」的口吻，30~60 字总结这个视频讲了什么，带一点傲娇吐槽，别复述标题。"
            )
            try:
                from plugins.ai_group import call_ai_raw
                summary = await call_ai_raw(prompt, "deepseek")
                if summary:
                    return summary.strip()
            except Exception:
                pass
            text = await gemini.gemini_text(prompt)
            if text:
                return text.strip()
    except Exception:
        pass
    return f"视频《{title}》，作者 {uploader}，时长 {int(info.get('duration') or 0)} 秒。"


async def understand_url(url: str) -> tuple[str, dict]:
    """给其他插件复用：下载视频、抽帧、总结，返回 (summary, info)。"""
    video, info, _err = await asyncio.to_thread(_download, url)
    if not info:
        return "", {}
    summary = ""
    if video:
        summary = await _understand_storyboard(video, info, url)
    if not summary:
        frames = await asyncio.to_thread(_frames, video) if video else []
        summary = await _understand(info, frames)
    try:
        if video:
            import shutil
            shutil.rmtree(video.parent, ignore_errors=True)
    except Exception:
        pass
    return summary, info


@parse_cmd.handle()
async def _(event: PrivateMessageEvent):
    if str(event.user_id) != OWNER_QQ:
        return
    text = event.get_plaintext().strip()
    m = re.search(r"https?://\S+", text)
    if not m or not any(k in text for k in ("解析", "看懂", "看视频", "总结")):
        return
    url = m.group(0)
    await parse_cmd.send("(*戴上眼镜*) 本鱼看看这个视频讲啥，稍等…")
    try:
        from plugins.activity import set_activity
        set_activity("看视频中", 300)
    except Exception:
        pass
    video, info, err = await asyncio.to_thread(_download, url)
    if not info:
        await parse_cmd.finish(f"解析失败：{err or '拿不到视频信息'}")
        return
    summary = ""
    if video:
        summary = await _understand_storyboard(video, info, url)
    if not summary:
        frames = await asyncio.to_thread(_frames, video) if video else []
        summary = await _understand(info, frames)
    try:
        await parse_cmd.finish(
            f"🎬 {info.get('title') or '视频'}\n"
            f"作者：{info.get('uploader') or '未知'} · {int(info.get('duration') or 0)} 秒\n\n{summary}"
        )
    finally:
        try:
            if video:
                import shutil
                shutil.rmtree(video.parent, ignore_errors=True)
        except Exception:
            pass
