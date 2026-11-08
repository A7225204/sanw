"""语音消息识别（ASR）：让她听得见 QQ 里的语音。

链路
----
record 段 → 取音频（URL 下载 / 本地路径 / NapCat get_record）→ ffmpeg 转 16k 单声道 wav
        → 硅基流动 SenseVoiceSmall → 文字

为什么这么选
------------
- 硅基流动有免费额度，**不占显存、不抢 ComfyUI**，只传音频不传别的东西；
- 本地 whisper 要占显存或很慢，先不上；
- 全链路任何一步失败都返回空串，调用方按"没听清"处理，**绝不编造**。

已知限制
--------
QQ 新版语音是 silk 编码，ffmpeg 解不了。这里先试 ffmpeg，失败再试 pilk（若装了）。
两者都没有就返回空串，并在日志里写清楚，方便判断要不要补解码器。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent

STATE = ROOT / "memory" / "asr_usage.json"
ASR_MODEL = "FunAudioLLM/SenseVoiceSmall"
MAX_SECONDS = 60
_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\u2190-\u21FF\u2B00-\u2BFF]+"
)


def _env() -> dict:
    out = {}
    try:
        for line in (ROOT / ".env").read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except Exception:  # noqa: BLE001
        pass
    return out


def _key() -> str:
    return (os.getenv("SILICONFLOW_API_KEY") or _env().get("SILICONFLOW_API_KEY") or "").strip()


def _clean(text: str) -> str:
    t = _EMOJI_RE.sub("", str(text or "")).strip()
    t = re.sub(r"\s+", " ", t)
    return t[:120]


def _bump(ok: bool, seconds: float = 0.0) -> None:
    """记录用量（本地统计，便于看免费额度还剩多少）。"""
    try:
        data = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    except Exception:  # noqa: BLE001
        data = {}
    data["ok"] = int(data.get("ok") or 0) + (1 if ok else 0)
    data["fail"] = int(data.get("fail") or 0) + (0 if ok else 1)
    data["seconds"] = round(float(data.get("seconds") or 0) + seconds, 1)
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


async def _fetch(data: dict, event=None) -> bytes:
    """从 record 段里把音频拿下来：URL 优先，其次本地路径，最后问 NapCat 要。"""
    url = str(data.get("url") or "").strip()
    if url.startswith("http"):
        import httpx
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as cli:
            r = await cli.get(url, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200 and r.content:
                return r.content
    for key in ("path", "file"):
        p = str(data.get(key) or "").strip()
        if p and Path(p).exists():
            return Path(p).read_bytes()
    # NapCat 的 get_record 能拿到服务器上的落地文件
    if event is not None:
        try:
            info = await event.bot.call_api("get_record", file=str(data.get("file") or ""))
            p = str((info or {}).get("file") or "")
            if p and Path(p).exists():
                return Path(p).read_bytes()
        except Exception:  # noqa: BLE001
            pass
    return b""


def _to_wav(raw: bytes, suffix: str) -> bytes:
    """任意音频 → 16k 单声道 wav。silk 先试 pilk 解码再转。"""
    import imageio_ffmpeg
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    tmpdir = Path(tempfile.mkdtemp(prefix="asr_"))
    src = tmpdir / f"in{suffix}"
    src.write_bytes(raw)
    wav = tmpdir / "out.wav"

    def _run(inp: Path) -> bool:
        try:
            subprocess.run(
                [ff, "-y", "-i", str(inp), "-ar", "16000", "-ac", "1", str(wav)],
                capture_output=True, timeout=45,
            )
            return wav.exists() and wav.stat().st_size > 2000
        except Exception:  # noqa: BLE001
            return False

    if _run(src):
        return wav.read_bytes()
    # ffmpeg 解不了 → 可能是 silk，试试 pilk
    try:
        import pilk  # type: ignore
    except Exception:  # noqa: BLE001
        return b""
    try:
        pcm = tmpdir / "out.pcm"
        pilk.decode(str(src), str(pcm))
        if _run(pcm):
            return wav.read_bytes()
    except Exception:  # noqa: BLE001
        return b""
    return b""


async def _transcribe_wav(wav: bytes) -> str:
    key = _key()
    if not key:
        return ""
    import httpx
    url = "https://api.siliconflow.cn/v1/audio/transcriptions"
    try:
        async with httpx.AsyncClient(timeout=60.0) as cli:
            r = await cli.post(
                url,
                headers={"Authorization": f"Bearer {key}"},
                files={"file": ("a.wav", wav, "audio/wav")},
                data={"model": ASR_MODEL},
            )
            if r.status_code != 200:
                return ""
            payload = r.json()
            secs = float((payload.get("usage") or {}).get("seconds") or 0)
            text = _clean(payload.get("text") or "")
            _bump(bool(text), secs)
            return text
    except Exception:  # noqa: BLE001
        _bump(False)
        return ""


async def transcribe_from_event(event) -> str:
    """把一个消息事件里的语音转成文字；没有语音或失败返回空串。"""
    try:
        segs = [s for s in event.get_message() if getattr(s, "type", "") == "record"]
    except Exception:  # noqa: BLE001
        return ""
    if not segs:
        return ""
    texts: list[str] = []
    for seg in segs[:2]:
        data = getattr(seg, "data", {}) or {}
        try:
            raw = await _fetch(data, event)
            if not raw:
                continue
            name = str(data.get("file") or "a.amr")
            suffix = ("." + name.rsplit(".", 1)[-1]) if "." in name else ".amr"
            wav = await asyncio.to_thread(_to_wav, raw, suffix)
            if not wav:
                continue
            txt = await _transcribe_wav(wav)
            if txt:
                texts.append(txt)
        except Exception:  # noqa: BLE001
            continue
    return "；".join(texts)[:200]


# ---------------------------------------------------------------- 转写可信度守卫（A 档）
# 不换 ASR，只在「写进口播/记忆」之前判一次：这条转写像不像 ASR 编出来的。
# 依据是 faster-whisper 类模型的典型幻觉形态——空、过短、同一句模板刷满全片。
_MIN_CHARS = 6
_TEMPLATE_MIN_SEGS = 6
_TEMPLATE_MAX_UNIQUE = 0.4


def transcript_trust(text: str) -> tuple[bool, str]:
    """(可信, 原因)。不可信时调用方不要把文本当台词用。"""
    t = (text or "").strip()
    if not t:
        return False, "空转写"
    core = re.sub(r"[\s\W_]+", "", t)
    if len(core) < _MIN_CHARS:
        return False, f"过短({len(core)}字)"

    segs = [s.strip() for s in re.split(r"[。！？!?.…；;\n]+", t) if s.strip()]
    if len(segs) >= _TEMPLATE_MIN_SEGS:
        uniq = len(set(segs)) / len(segs)
        if uniq <= _TEMPLATE_MAX_UNIQUE:
            return False, f"重复模板(unique {uniq:.2f})"

    if len(core) >= 8:
        half = len(core) // 2
        for size in range(2, min(21, half) + 1):
            unit = core[:size]
            if unit and core == (unit * (len(core) // size + 1))[: len(core)]:
                return False, f"整段重复({size}字模板)"

    return True, ""


def guard_transcript(text: str) -> tuple[str, str]:
    """可信就原样返回（空原因），不可信返回空串 + 原因。"""
    ok, why = transcript_trust(text)
    return ((text or "").strip(), "") if ok else ("", why)

# 2026-11-06：SenseVoiceSmall 单次只吃约 30 秒 —— 60 秒的音频只转出 120 字，
# 后面全丢。实测：15 秒 76 字 / 30 秒 91 字 / 60 秒 120 字（越算越慢=只转前段）。
# 于是 3 分钟的视频，她只「听」到了前 20 秒，难怪总说「字幕到这儿就断了」。
ASR_CHUNK_SEC = 25          # 每段秒数（留余量，30 是硬上限）
ASR_WAV_BYTES_PER_SEC = 32000   # 16k * 2 byte * 单声道


def _split_wav(wav: bytes, chunk_sec: int = ASR_CHUNK_SEC) -> list[bytes]:
    """把 wav 按秒切段（纯字节操作，不调 ffmpeg）。

    只处理标准 16k/16bit/单声道 PCM，头部 44 字节；非法输入返回 [wav] 原样。
    """
    if len(wav) < 1024:
        return [wav]
    head = wav[:44]
    if head[:4] != b"RIFF" or b"fmt " not in head:
        return [wav]
    try:
        # 找 data 块
        pos = wav.find(b"data")
        if pos < 0:
            return [wav]
        data = wav[pos + 8:]
    except Exception:  # noqa: BLE001
        return [wav]
    step = chunk_sec * ASR_WAV_BYTES_PER_SEC
    if len(data) <= step:
        return [wav]
    out = []
    for i in range(0, len(data), step):
        chunk = data[i:i + step]
        if len(chunk) < 1600:      # 太短的尾巴丢掉（<50ms 没内容）
            continue
        # 拼一个最小可用的 wav 头（44 字节标准 PCM）
        n = len(chunk)
        hdr = (b"RIFF" + (36 + n).to_bytes(4, "little") + b"WAVEfmt " +
               (16).to_bytes(4, "little") + (1).to_bytes(2, "little") +
               (1).to_bytes(2, "little") + (16000).to_bytes(4, "little") +
               (32000).to_bytes(4, "little") + (2).to_bytes(2, "little") +
               (16).to_bytes(2, "little") + b"data" + n.to_bytes(4, "little"))
        out.append(hdr + chunk)
    return out or [wav]


async def transcribe_long(wav: bytes, chunk_sec: int = ASR_CHUNK_SEC) -> str:
    """长音频转写：切段逐段转，再把结果接起来。

    2026-11-06 新增。原来的 _transcribe_wav 直接喂整段，
    超过 ~30 秒的部分服务端不处理，转出来是半截话。
    """
    chunks = _split_wav(wav, chunk_sec)
    if len(chunks) <= 1:
        return await _transcribe_wav(wav)
    parts: list[str] = []
    for i, ch in enumerate(chunks):
        try:
            t = await _transcribe_wav(ch)
        except Exception:  # noqa: BLE001
            t = ""
        if t:
            parts.append(t.strip())
        try:
            from nonebot.log import logger as _lg
            _lg.debug(f"[ASR] 第 {i+1}/{len(chunks)} 段 → {len(t or chr(39)+chr(39))} 字")
        except Exception:
            pass
    return "".join(parts)
