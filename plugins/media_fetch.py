"""按平台下载并分析视频：抖音 / B站，结果按平台存进 media_cache。

为什么要有这一层
----------------
抖音和 B站 的"看视频"需求是一样的：

    拿标题 → 下载视频 → 抽 3 帧识图 → 抽音轨转文字 → 存一份分析文本

区别只在"怎么拿到视频地址"：
  · 抖音   yt-dlp 过不了签名，改用已登录浏览器截 aweme/detail 接口
  · B站    yt-dlp 直接能用（公开视频不需要登录）

缓存结构（下次分析同一个视频直接读文本，不再下载）：

    data/media_cache/douyin/<id>.mp4
    data/media_cache/douyin/<id>.txt
    data/media_cache/bilibili/<BV号>.mp4
    data/media_cache/bilibili/<BV号>.txt
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from nonebot.log import logger

ROOT = Path(__file__).resolve().parent.parent
CACHE_ROOT = ROOT / "data" / "media_cache"
CACHE_TTL = 45 * 86400

# ---- 字幕完整性与离线质检（2026-11-05，借 dsh-bili-humanism 的证据纪律）----
# 以前字幕在取稿阶段就被 text[:400] 切掉，10 分钟的视频她只看到开头 13%
# 就写感想，只能说套话。现在改成：全文保留在这里，采样交给下游按需做。
TR_MAX = 20000          # 单份字幕保留上限；超出截尾并显式记 dropped，不静默丢中段


def coverage_of(text: str, limit: int = TR_MAX) -> dict:
    """字幕保留情况：留了多少、丢了多少。让自己知道"没看全"，别硬装懂。"""
    n = len(str(text or ""))
    kept = min(n, limit)
    return {
        "state": "full" if kept >= n else "partial",
        "chars": n,
        "kept": kept,
        "dropped": max(0, n - kept),
    }


# 常见繁体字（用于发现简繁混排——混排通常意味着 ASR 或字幕源本身有问题）
_TRAD_HINT = "們這個來說時會點裡為與國學習話語還沒麼樣現實體發問後見親"


def probe_transcript(text: str, duration_sec: float = 0.0) -> dict:
    """离线质检：只读本地文本，不联网、不调模型、不花 token。

    报告信号，不下结论——同一个写法稳定地错和稳定地对，在这里看起来一样。
    """
    t = str(text or "").strip()
    n = len(t)
    if not n:
        return {"chars": 0, "state": "empty"}

    # 分句 + 每 10% 区段的内容量（看中后段是不是空的）
    chunks = [c.strip() for c in re.split(r"[。！？!?\n]+", t) if c.strip()]
    step = max(1, n // 10)
    bands = []
    for i in range(10):
        seg = t[i * step:(i + 1) * step] if i < 9 else t[9 * step:]
        bands.append(len(re.sub(r"\s+", "", seg)))
    empty_bands = sum(1 for x in bands if x < 5)

    # 重复句（模板化）+ 短句占比（"嗯对/对对/走吧"刷屏式转写）
    # 2026-11-05 修：原来阈值 >=8 字，导致"嗯对"这类短句刷屏完全检不出
    from collections import Counter
    rep = Counter(c for c in chunks if len(c) >= 4)
    dup = sum(v - 1 for v in rep.values() if v > 1)
    short_ratio = round(sum(1 for c in chunks if len(c) <= 3) / max(1, len(chunks)), 2)

    # 拉丁串、简繁混用
    latin = re.findall(r"[A-Za-z]{3,}", t)
    trad = sum(1 for ch in t if ch in _TRAD_HINT)

    cpm = round(n / (duration_sec / 60.0), 1) if duration_sec and duration_sec > 0 else None
    return {
        "chars": n,
        "state": "ok",
        "chunks": len(chunks),
        "chars_per_min": cpm,
        "band_chars": bands,
        "empty_bands": empty_bands,
        "dup_sentences": dup,
        "short_ratio": short_ratio,
        "latin_tokens": len(latin),
        "latin_sample": latin[:8],
        "trad_chars": trad,
        "suspect": (
            "empty_bands" if empty_bands >= 4 else
            "filler" if short_ratio >= 0.5 else
            "dup" if dup >= max(3, len(chunks) // 4) else
            "mixed_script" if trad >= 5 else
            ""),
    }


def platform_of(url: str) -> str:
    u = str(url or "").lower()
    if "douyin.com" in u or "iesdouyin" in u:
        return "douyin"
    if "bilibili.com" in u or "b23.tv" in u or "/video/BV" in u.upper():
        return "bilibili"
    return "other"


def _vid(platform: str, url: str) -> str:
    if platform == "bilibili":
        m = re.search(r"(BV[0-9A-Za-z]{8,12})", url)
        if m:
            return m.group(1)
        m = re.search(r"/av(\d+)", url)
        if m:
            return "av" + m.group(1)
    m = re.search(r"(?:video|note|share/video)/(\d{6,})", url) or re.search(r"[?&]id=(\d{6,})", url)
    if m:
        return m.group(1)
    import hashlib
    return hashlib.md5(str(url).encode()).hexdigest()[:16]


def cache_paths(url: str) -> tuple[str, str, Path, Path]:
    plat = platform_of(url)
    vid = _vid(plat, url)
    d = CACHE_ROOT / plat
    return plat, vid, d / f"{vid}.mp4", d / f"{vid}.txt"


def load_cached(url: str) -> dict | None:
    plat, vid, mp4, txt = cache_paths(url)
    try:
        if not txt.exists() or time.time() - txt.stat().st_mtime > CACHE_TTL:
            return None
        raw = txt.read_text(encoding="utf-8")
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return None
        d = json.loads(m.group(0))
        d["cached"] = True
        d["platform"] = plat
        d["video_path"] = str(mp4) if mp4.exists() else ""
        return d
    except Exception:  # noqa: BLE001
        return None


def save_cached(url: str, info: dict, video: Path | None) -> str:
    plat, vid, mp4, txt = cache_paths(url)
    try:
        CACHE_ROOT.joinpath(plat).mkdir(parents=True, exist_ok=True)
        if video is not None and video.exists():
            shutil.copy2(video, mp4)
        verdict = info.get("transcript_verdict")
        transcript_line = info.get("transcript") or (
            f"（转写不可信：{verdict}）" if verdict else "（无音轨/未转写）")
        cov = info.get("coverage") or {}
        coverage_line = (f"采样：{cov.get('samples')} 帧，最大盲区 {cov.get('max_gap')} 秒"
                         if cov else "")
        # 2026-11-05：字幕保留情况单独一行。注意键名是 text_coverage——
        # coverage 这个键归抽帧用（samples/max_gap），别混
        tcov = info.get("text_coverage") or {}
        if tcov:
            _tl = f"字幕：{tcov.get('kept')}/{tcov.get('chars')} 字"
            if tcov.get("state") == "partial":
                _tl += f"（截掉 {tcov.get('dropped')}）"
            else:
                _tl += "（全文）"
            coverage_line = (_tl + ("；" + coverage_line if coverage_line else ""))
        head = [
            f"平台：{'抖音' if plat == 'douyin' else 'B站' if plat == 'bilibili' else plat}",
            f"标题：{info.get('title') or '（无）'}",
            f"链接：{url}",
            f"分析时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            f"画面：{info.get('desc') or '（未识别）'}",
        ]
        if coverage_line:
            head.append(coverage_line)
        head += [
            "",
            f"口播：{transcript_line}",
            "",
            "---- 以下为结构化数据（程序读取用）----",
            json.dumps(info, ensure_ascii=False, indent=1),
        ]
        txt.write_text("\n".join(head), encoding="utf-8")
        logger.info(f"[媒体分析] 已缓存 {plat}/{txt.name}")
        return str(txt)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[媒体分析] 写缓存失败：{e}")
        return ""


async def fetch(url: str, with_audio: bool = True) -> dict:
    """统一入口：抖音走浏览器抓取，B站走 yt-dlp，都带缓存。"""
    plat = platform_of(url)
    hit = load_cached(url)
    if hit:
        logger.info(f"[媒体分析] 命中缓存 {plat}/{_vid(plat, url)}")
        return hit
    if plat == "douyin":
        from plugins import douyin_fetch
        return await douyin_fetch.fetch(url, with_audio=with_audio)
    if plat == "bilibili":
        return await _bili_fetch(url, with_audio=with_audio)
    return {"title": "", "desc": "", "transcript": "", "error": f"不支持的平台：{plat}"}


async def _bili_fetch(url: str, with_audio: bool = True) -> dict:
    """B站：yt-dlp 下载 → 抽帧 → 抽音轨 → 转写 → 缓存。"""
    import asyncio

    from plugins import douyin_fetch as df

    out: dict = {"title": "", "desc": "", "transcript": "", "error": "", "platform": "bilibili"}
    tmp = Path(tempfile.mkdtemp(prefix="bilifetch_"))
    try:
        from plugins import bili_dl
        video, info, dl_err = await asyncio.to_thread(bili_dl.download, url)
        if video is None:  # 回退：老的通用下载（720P、临时目录）
            logger.warning(f"[媒体分析] 1080P 下载失败（{dl_err}），回退 720P")
            video, info = await asyncio.to_thread(df._download, url, tmp, False)
        if not out["title"]:
            out["title"] = str((info or {}).get("title") or "")[:100]
        if video is None:
            out["error"] = "下载失败"
            return out
        # 分镜：把整段视频压成几张宫格图（每张 9 格 + 时间码），比抽 3 帧信息多、比逐帧便宜
        desc_parts: list[str] = []
        try:
            from plugins import storyboard as sb
            info_sb = await asyncio.to_thread(sb.probe, str(video))
            dur = float(info_sb.get("duration") or 0)
            out["duration"] = dur
            out["cuts"] = 0
            # B站自带字幕（带时间码、免费），拿不到就只出图
            subs: list[dict] = []
            try:
                subs = await asyncio.to_thread(bili_dl.fetch_subtitles, url)
                out["subtitle_lines"] = len(subs)
            except Exception as e_sub:  # noqa: BLE001
                logger.warning(f"[媒体分析] 拉字幕失败：{type(e_sub).__name__}: {e_sub}")
            sheets = await asyncio.to_thread(
                sb.build_sheets, str(video), 300.0, 9, str(tmp), 12, subs
            )
            out["sheets"] = len(sheets)
            out["coverage"] = {"samples": sheets[0].get("samples"),
                               "max_gap": sheets[0].get("max_gap")}
            if sheets:
                from plugins.ai_group import _describe_images
                import base64 as _b64mod
                # 成本控制：片子越长越抽样描述（宫格全建、只挑几张识图）
                if dur <= 300:
                    picks = sheets[:3]
                elif dur <= 1200:
                    picks = sheets[:6]
                else:
                    step = max(1, len(sheets) // 6)
                    picks = sheets[::step][:6]
                for sh in picks:
                    try:
                        # 压成 JPEG 再送：PNG 1200px 火山要 70 秒，JPEG 只要 20 秒左右
                        raw = Path(sh["png"]).read_bytes()
                        try:
                            from PIL import Image as _Img
                            import io as _io
                            _im = _Img.open(_io.BytesIO(raw)).convert("RGB")
                            if _im.width > 1280:
                                _im = _im.resize((1280, int(_im.height * 1280 / _im.width)), _Img.LANCZOS)
                            _buf = _io.BytesIO()
                            _im.save(_buf, format="JPEG", quality=82, optimize=True)
                            raw = _buf.getvalue()
                            _mime = "image/jpeg"
                        except Exception:  # noqa: BLE001
                            _mime = "image/png"
                        uri = f"data:{_mime};base64," + _b64mod.b64encode(raw).decode()
                        ds = await _describe_images(
                            [uri],
                            hint=(f"这是一段视频的分镜宫格（每格左上角有格号和时间码），"
                                  f"覆盖 {sh['start']:.0f}~{sh['end']:.0f} 秒。"
                                  "用 60 字概括这段在讲什么/画面在干什么，不要逐格罗列。"),
                        )
                        txt = " ".join(d for d in (ds or []) if d).strip()
                        if txt:
                            desc_parts.append(f"[{sh['start']:.0f}-{sh['end']:.0f}s] {txt}")
                    except Exception as e2:  # noqa: BLE001
                        logger.warning(f"[媒体分析] 分镜识图失败：{type(e2).__name__}: {e2}")
            out["storyboard"] = await asyncio.to_thread(sb.sheet_text, sheets, dur) if sheets else ""
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[媒体分析] 分镜失败，回退抽帧：{type(e).__name__}: {e}")
            frames = await asyncio.to_thread(df._frames, video, 3)
            if frames:
                try:
                    from plugins.ai_group import _describe_images
                    descs = await _describe_images([f"data:image/jpeg;base64,{df._b64(f)}" for f in frames])
                    desc_parts = [d for d in (descs or []) if d]
                except Exception as e3:  # noqa: BLE001
                    logger.warning(f"[媒体分析] 回退抽帧识图也失败：{type(e3).__name__}: {e3}")
        # 音频结构（BPM / 段落 / 说话还是唱）——给观后感当"节奏"依据
        try:
            from tools.audio_structure import analyze as _struct
            _st = await asyncio.to_thread(_struct, str(video))
            out["structure"] = _st
            _secs = "、".join(f"{x['start']:.0f}-{x['end']:.0f}s{x['label']}"
                             for x in (_st.get("sections") or [])[:6])
            desc_parts.append(
                f"节奏：{_st.get('bpm')} BPM，{_st.get('kind')}，"
                f"{len(_st.get('sections') or [])} 段（{_secs}）")
        except Exception as e_st:  # noqa: BLE001
            logger.debug(f"[媒体分析] 音频结构跳过：{type(e_st).__name__}: {e_st}")

        if desc_parts:
            out["desc"] = " ｜ ".join(desc_parts)[:900]
        if with_audio:
            wav = await asyncio.to_thread(df._audio_wav, video)
            if wav:
                try:
                    import asr
                    # 可选：强制门控优先（ASR_GATE_FIRST=1 时先分离再转，最稳但慢）
                    if os.getenv("ASR_GATE_FIRST", "0") in ("1", "true", "on"):
                        try:
                            from plugins import asr_gate
                            gtext0, gmeta0 = await asr_gate.transcribe_gated(wav)
                            out["transcript_meta"] = gmeta0
                            logger.info(f"[媒体分析] {asr_gate.describe(gmeta0)}")
                            if gtext0:
                                text0, why0 = asr.guard_transcript(gtext0)
                                if text0:
                                    # 2026-11-05：不再切到 400 字，全文保留 + 记覆盖与质检
                                    out["transcript"] = text0[:TR_MAX]
                                    out["text_coverage"] = coverage_of(text0)
                                    out["probe"] = probe_transcript(
                                        text0, float(out.get("duration") or 0))
                                    out["url"] = url
                                    await asyncio.to_thread(save_cached, url, out, video)
                                    return out
                        except Exception as e0:  # noqa: BLE001
                            logger.warning(f"[媒体分析] 门控优先失败，回退整段：{e0}")
                    # 2026-11-06：长音频要分段转，整段喂进去只有前 30 秒有效
                    raw = (await asr.transcribe_long(wav) or "").strip()
                    text, why = asr.guard_transcript(raw)
                    if not text:
                        # 整段转写不可信 → 用人声门控重来一遍（只把说话块送识别，从源头防编）
                        logger.info(f"[媒体分析] 整段转写不可信（{why}），改用器乐门控重试")
                        try:
                            from plugins import asr_gate
                            gtext, gmeta = await asr_gate.transcribe_gated(wav)
                            out["transcript_meta"] = gmeta
                            logger.info(f"[媒体分析] {asr_gate.describe(gmeta)}")
                            if gtext:
                                text, why = asr.guard_transcript(gtext)
                                if text:
                                    raw = gtext
                        except Exception as e2:  # noqa: BLE001
                            logger.warning(f"[媒体分析] 门控重试失败：{type(e2).__name__}: {e2}")
                    if text:
                        # 2026-11-05：同上，全文保留 + 覆盖与质检，下游按需采样
                        out["transcript"] = text[:TR_MAX]
                        out["text_coverage"] = coverage_of(text)
                        out["probe"] = probe_transcript(
                            text, float(out.get("duration") or 0))
                        out.setdefault("transcript_meta",
                                       {"gate": "off", "reason": "整段转写通过可信检查"})
                    else:
                        out["transcript_verdict"] = why
                        logger.warning(f"[媒体分析] 转写不可信（{why}），不写口播：{raw[:40]!r}")
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[媒体分析] B站音频转写失败：{type(e).__name__}: {e}")
        out["url"] = url
        await asyncio.to_thread(save_cached, url, out, video)
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
