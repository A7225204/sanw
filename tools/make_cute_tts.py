"""用豆包合成"软萌语气"的参考音，再用 F5-TTS 克隆出更可爱的说话声。

流程：
  豆包 TTS（带 emotion/style）→ 参考音 mp3
    → F5-TTS 克隆 → 3 个候选样本
    → 全部放 outbox 由投递箱发飞书/QQ
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import uuid
from pathlib import Path

import httpx

ROOT = Path(r"D:\qq-ai-bot")
WORK = Path(r"D:\qq-ai-bot\outbox\tts_lab")
WORK.mkdir(parents=True, exist_ok=True)

for line in (ROOT / ".env").read_text(encoding="utf-8", errors="ignore").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

TTS_API_KEY = os.getenv("TTS_API_KEY", "")
TTS_SPEAKER = os.getenv("TTS_SPEAKER", "zh_female_mengyatou_uranus_bigtts")
TTS_RESOURCE = os.getenv("TTS_RESOURCE_ID", "seed-tts-2.0")
TTS_URL = os.getenv("DOUBAO_TTS_URL", "https://openspeech.bytedance.com/api/v3/tts/unidirectional")

# 参考音文本：要软、要短句、要有语气词
REF_TEXTS = {
    "soft": "主人～人家等你等了好久呀，你终于回来啦。",
    "happy": "哇！你回来啦！本鱼今天可开心啦～",
    "coquettish": "哼，你都不理我，人家要生气了嘛……",
}
EMOTION = {"soft": "neutral", "happy": "happy", "coquettish": "sad"}

GEN_TEXT = "主人，本鱼现在可以自己说话了，不用再麻烦外面的服务啦。"


async def doubao(text: str, emotion: str, out: Path) -> bool:
    payload = {
        "req_params": {
            "text": text,
            "speaker": TTS_SPEAKER,
            "audio_params": {"format": "mp3", "sample_rate": 24000,
                             "speech_rate": -8, "loudness_rate": 0},
            "post_process": {"pitch": 2},
        }
    }
    if emotion and emotion != "neutral":
        payload["req_params"]["emotion"] = emotion
        payload["req_params"]["emotion_scale"] = 3
    headers = {
        "X-Api-Key": TTS_API_KEY,
        "X-Api-Resource-Id": TTS_RESOURCE,
        "X-Api-Request-Id": str(uuid.uuid4()),
        "Content-Type": "application/json",
        "Connection": "keep-alive",
        "X-Control-Require-Usage-Tokens-Return": "*",
    }
    buf = bytearray()
    try:
        async with httpx.AsyncClient(timeout=120) as c:
            async with c.stream("POST", TTS_URL, json=payload, headers=headers) as r:
                if r.status_code != 200:
                    print(f"  豆包 {r.status_code}: {(await r.aread())[:120]}", flush=True)
                    return False
                async for chunk in r.aiter_bytes():
                    if chunk[:2] == b"\xff\xfb" or chunk[:3] == b"ID3":
                        buf.extend(chunk)
                        continue
                    for ln in chunk.decode("utf-8", "ignore").splitlines():
                        ln = ln.strip()
                        if not ln.startswith("{"):
                            continue
                        try:
                            obj = json.loads(ln)
                        except Exception:  # noqa: BLE001
                            continue
                        b64 = obj.get("data") or (obj.get("result") or {}).get("data")
                        if b64:
                            buf.extend(base64.b64decode(b64))
        if len(buf) > 2000:
            out.write_bytes(bytes(buf))
            return True
    except Exception as e:  # noqa: BLE001
        print(f"  豆包异常 {type(e).__name__}: {e}", flush=True)
    return False


def f5_clone(ref_wav: Path, ref_text: str, tag: str) -> Path | None:
    from f5_tts.api import F5TTS
    out = WORK / f"clone_{tag}.wav"
    f5 = F5TTS()
    f5.infer(ref_file=str(ref_wav), ref_text=ref_text, gen_text=GEN_TEXT,
             file_wave=str(out), nfe_step=48, speed=0.92)
    return out if out.exists() else None


def to_mp3(wav: Path) -> Path | None:
    from imageio_ffmpeg import get_ffmpeg_exe
    import subprocess
    ff = get_ffmpeg_exe()
    mp3 = wav.with_suffix(".mp3")
    subprocess.run([ff, "-y", "-i", str(wav), "-b:a", "192k", str(mp3)],
                   capture_output=True, timeout=120)
    return mp3 if mp3.exists() else None


async def main() -> int:
    if not TTS_API_KEY:
        print("没有 TTS_API_KEY")
        return 1
    refs: list[tuple[str, Path, str]] = []
    for tag, text in REF_TEXTS.items():
        ref_mp3 = WORK / f"ref_{tag}.mp3"
        if not ref_mp3.exists():
            print(f"合成参考音 {tag}…", flush=True)
            if not await doubao(text, EMOTION[tag], ref_mp3):
                continue
        # 参考音转 wav（F5 用 wav 更稳）
        ref_wav = ref_mp3.with_suffix(".wav")
        if not ref_wav.exists():
            from imageio_ffmpeg import get_ffmpeg_exe
            import subprocess
            subprocess.run([get_ffmpeg_exe(), "-y", "-i", str(ref_mp3),
                            "-ar", "24000", "-ac", "1", str(ref_wav)], capture_output=True)
        refs.append((tag, ref_wav, text))
        print(f"  参考音就绪 {tag}: {ref_wav.stat().st_size//1024}KB", flush=True)

    if not refs:
        print("没有可用参考音")
        return 1

    # 从软萌的开始克隆（只做前 2 个，省时间）
    for tag, ref_wav, text in refs[:2]:
        print(f"F5 克隆（reference={tag}）…", flush=True)
        out = f5_clone(ref_wav, text, tag)
        if not out:
            print("  失败")
            continue
        mp3 = to_mp3(out)
        if mp3:
            final = ROOT / "outbox" / f"萌音_{tag}.mp3"
            mp3.replace(final)
            out.unlink(missing_ok=True)
            print(f"  成品 {final.name} {final.stat().st_size//1024}KB", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
