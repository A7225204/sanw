"""人声门控转写：先分离人声，只把"说话块"送去 ASR。

借鉴 dsh-soundtry 的 gate 思路（源头防幻觉，而不是事后整段丢弃）：
    分离人声(demucs) → silencedetect 找人声区间 → 合并/切块 → 只转这些块

与 asr.guard_transcript 的分工：
    · 本模块负责"根本不把器乐段送去识别"（前奏/间奏/尾奏没有机会被编）
    · guard_transcript 仍作为兜底，检查单块文本是否像幻觉

失败一律降级：分离失败 / 找不到说话区间 / 全空 → 返回 (None, meta)，调用方走原路径。
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import tempfile
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

GATE_ENABLED = os.getenv("ASR_GATE", "1") not in ("0", "false", "off")
GATE_MIN_SECONDS = float(os.getenv("ASR_GATE_MIN_SECONDS", "60"))   # 短片没必要门控
SILENCE_DB = os.getenv("ASR_GATE_DB", "-32")
MIN_SIL = float(os.getenv("ASR_GATE_SILENCE", "0.7"))
MIN_BLOCK = 1.2
MAX_BLOCKS = int(os.getenv("ASR_GATE_MAX_BLOCKS", "40"))


def _ffmpeg() -> str:
    import shutil
    for c in (os.getenv("DSH_FFMPEG_PATH"), "D:/qq-ai-bot/data/bin/ffmpeg.exe",
              "D:/FormatFactory/ffmpeg.exe"):
        if c and os.path.exists(c):
            return c
    return shutil.which("ffmpeg") or "ffmpeg"


def _wav_seconds(data: bytes) -> float:
    try:
        with wave.open(__import__("io").BytesIO(data)) as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except Exception:  # noqa: BLE001
        return 0.0


def _separate_vocals(wav_path: Path, work: Path) -> Path | None:
    """用唱歌链路现成的 demucs 分离（D:\\Seed-VC 的 venv），失败返回 None。"""
    try:
        from plugins import song_mix
        vocals, _back, err = song_mix.separate(wav_path, work)
        if vocals and Path(vocals).exists():
            return Path(vocals)
        if err:
            print(f"[门控] 分离失败：{err[:120]}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[门控] 分离异常：{type(e).__name__}: {e}", flush=True)
    return None


_TAG = re.compile(r"(silence_(?:start|end)):\s*(-?\d+(?:\.\d+)?)")


def _speech_regions(vocals: Path, total: float) -> list[tuple[float, float]]:
    """用 silencedetect 把"没人说话"的区间扣掉，剩下的就是人声区间。"""
    ff = _ffmpeg()
    try:
        p = subprocess.run(
            [ff, "-hide_banner", "-i", str(vocals), "-af",
             f"silencedetect=noise={SILENCE_DB}dB:d={MIN_SIL}", "-f", "null", "-"],
            capture_output=True, timeout=180,
        )
    except Exception as e:  # noqa: BLE001
        print(f"[门控] silencedetect 失败：{e}", flush=True)
        return []
    text = (p.stderr or b"").decode("utf-8", "ignore")
    sil: list[tuple[float, float]] = []
    start = None
    for name, val in _TAG.findall(text):
        try:
            v = float(val)
        except ValueError:
            continue
        if name == "silence_start":
            start = max(0.0, v)
        elif start is not None:
            sil.append((start, max(start, v)))
            start = None
    if start is not None:
        sil.append((start, total))

    regions: list[tuple[float, float]] = []
    cur = 0.0
    for a, b in sorted(sil):
        if a > cur:
            regions.append((cur, min(a, total)))
        cur = max(cur, b)
    if cur < total:
        regions.append((cur, total))

    merged: list[tuple[float, float]] = []
    for a, b in regions:
        if merged and a - merged[-1][1] < 0.6:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    return [(a, b) for a, b in merged if b - a >= MIN_BLOCK]


def _cut(vocals: Path, a: float, b: float, out: Path) -> bool:
    ff = _ffmpeg()
    try:
        subprocess.run(
            [ff, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{a:.3f}",
             "-to", f"{b:.3f}", "-i", str(vocals), "-ar", "16000", "-ac", "1",
             "-c:a", "pcm_s16le", str(out)],
            capture_output=True, timeout=120,
        )
        return out.exists() and out.stat().st_size > 2000
    except Exception:  # noqa: BLE001
        return False


async def transcribe_gated(wav: bytes) -> tuple[str | None, dict]:
    """返回 (文本 或 None, 元信息)。

    meta: {"gate": "on"/"off", "reason", "coverage", "blocks", "empty_blocks", "seconds"}
    """
    total = _wav_seconds(wav)
    meta = {"gate": "off", "reason": "", "coverage": 0.0, "blocks": 0,
            "empty_blocks": 0, "seconds": round(total, 1), "missed": []}
    if not GATE_ENABLED:
        meta["reason"] = "ASR_GATE=0"
        return None, meta
    if total and total < GATE_MIN_SECONDS:
        meta["reason"] = f"音频只有 {total:.0f}s，短片不门控"
        return None, meta
    try:
        import asr
    except Exception as e:  # noqa: BLE001
        meta["reason"] = f"asr 不可用：{e}"
        return None, meta

    with tempfile.TemporaryDirectory(prefix="asrgate_") as td:
        work = Path(td)
        src = work / "src.wav"
        src.write_bytes(wav)
        vocals = await asyncio.to_thread(_separate_vocals, src, work)
        if not vocals:
            meta["reason"] = "人声分离失败"
            return None, meta
        regions = await asyncio.to_thread(_speech_regions, vocals, total or 0.0)
        if not regions:
            meta["reason"] = "分离后没找到人声区间"
            return None, meta
        regions = regions[:MAX_BLOCKS]
        speech = sum(b - a for a, b in regions)
        texts: list[str] = []
        empty = 0
        missed: list[list[float]] = []
        for i, (a, b) in enumerate(regions):
            seg = work / f"seg_{i}.wav"
            if not await asyncio.to_thread(_cut, vocals, a, b, seg):
                empty += 1
                missed.append([round(a, 1), round(b, 1)])
                continue
            try:
                # 2026-11-06：改走 transcribe_long —— 服务端单次只吃约 30 秒，
                # VAD 切出来的段如果超过这个长度，后半句会被静默丢掉。
                txt = await asr.transcribe_long(seg.read_bytes())
            except Exception:  # noqa: BLE001
                txt = ""
            if txt and txt.strip():
                texts.append(txt.strip())
            else:
                empty += 1
                missed.append([round(a, 1), round(b, 1)])
        meta.update({"gate": "on", "reason": "", "blocks": len(regions),
                     "empty_blocks": empty, "missed": missed[:8],
                     "coverage": round(speech / total, 3) if total else 0.0})
        joined = "".join(t if t.endswith(("。", "！", "？", "…")) else t + "。"
                         for t in texts).strip()
        return (joined or None), meta


def describe(meta: dict) -> str:
    """给日志/报告用的一句话。"""
    if meta.get("gate") != "on":
        return f"（未启用人声门控：{meta.get('reason') or '未知原因'}）"
    return (f"（人声切分：整段 {meta.get('seconds')}s → {meta.get('blocks')} 块、"
            f"覆盖率 {meta.get('coverage', 0) * 100:.0f}%，空块 {meta.get('empty_blocks')}"
            + (f"，未认出 {meta.get('missed')[:3]}" if meta.get("missed") else "") + "）")
