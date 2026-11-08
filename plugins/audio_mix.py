"""音频混音：人声 + 背景音乐 + 音效，旁白自动压背景（ducking）。

来源：AnyAudio（cloudoyx/any-audio）的混音设计 ——
> `narration` 轨自动 ducking 其他 bus，master 峰值归一化，整段 mix 不削波。

**它是 DSH 插件，装不进 NoneBot**，但那三个处理用 ffmpeg 本来就能做，
不需要它那套 Node 运行时。

它比"手写 NumPy 合成"强在哪（它自己的对比数据）：
    高频细节 6.8% → 11.6%，动态余量更大 —— 说白了就是"像真乐器"。
那部分靠 SoundFont 采样，见 `plugins/soundfont.py`。混音这部分纯 ffmpeg。

对外：
    mix(voice, bgm=None, sfx=None, out=None) -> (路径, 错误说明)
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "audio_mix"

# 混音默认值 —— 照 AnyAudio 的思路：旁白为主，背景压到能听见但不抢话
VOICE_GAIN = 1.0
BGM_GAIN = 0.28        # 背景乐先降到 -11dB 左右
SFX_GAIN = 0.42

# ducking 参数：人声一起，背景立刻让路，说完 0.3 秒缓回来
DUCK_THRESHOLD = 0.05
DUCK_RATIO = 8.0
DUCK_ATTACK = 5        # ms
DUCK_RELEASE = 300     # ms

# 峰值归一化：不用 -14 LUFS（那是流媒体标准，语音太响），用 -16
LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"


def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    # 项目里那份（npm 装的）
    cand = Path(os.environ.get("APPDATA", "")) / "npm" / "ffmpeg"
    if cand.exists():
        return str(cand)
    return "ffmpeg"


def available() -> bool:
    try:
        r = subprocess.run([_ffmpeg(), "-version"], capture_output=True, timeout=8)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _exists(p) -> bool:
    return bool(p) and Path(str(p)).exists()


def mix(voice, bgm=None, sfx=None, out=None, voice_gain: float = VOICE_GAIN,
        bgm_gain: float = BGM_GAIN, sfx_gain: float = SFX_GAIN) -> tuple[str, str]:
    """把几路音频混成一条。返回 (输出路径, 错误说明)。

    - voice 必给（人声）
    - bgm   可选（背景乐），**会被 ducking**：人声一起来就压下去
    - sfx   可选（音效），不 ducking，正常混进去
    - 长度以人声为基准（`duration=first`），背景乐比人声长会被截
    """
    if not available():
        return "", "ffmpeg 不可用"
    if not _exists(voice):
        return "", f"人声文件不存在：{voice}"

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    outp = Path(str(out)) if out else (OUT_DIR / f"mix_{int(__import__('time').time())}.mp3")

    inputs: list[str] = ["-i", str(voice)]
    filters: list[str] = []
    labels: list[str] = []
    idx = 1

    # 人声 —— 要用两次（一次给 sidechain 当触发，一次进 amix），必须 asplit 分叉
    # 踩过的坑：直接 [v] 被两个 filter 消费 → "matches no streams"
    need_duck = _exists(bgm)
    if need_duck:
        filters.append(f"[0:a]volume={voice_gain},asplit=2[v1][v2]")
    else:
        filters.append(f"[0:a]volume={voice_gain}[v2]")

    # 背景乐（要 ducking）—— 循环靠输入层 -stream_loop，不在滤镜里做
    if need_duck:
        inputs += ["-stream_loop", "-1", "-i", str(bgm)]
        filters.append(f"[{idx}:a]volume={bgm_gain}[bg]")
        filters.append(
            f"[bg][v1]sidechaincompress="
            f"threshold={DUCK_THRESHOLD}:ratio={DUCK_RATIO}:"
            f"attack={DUCK_ATTACK}:release={DUCK_RELEASE}[bgd]"
        )
        labels.append("[bgd]")
        idx += 1

    # 音效（不 duck）
    if _exists(sfx):
        inputs += ["-stream_loop", "-1", "-i", str(sfx)]
        filters.append(f"[{idx}:a]volume={sfx_gain}[fx]")
        labels.append("[fx]")
        idx += 1

    labels.append("[v2]")     # 人声本体永远进 amix

    if len(labels) == 1:
        # 只有人声 —— 也走一遍归一化，保证音量一致
        filters.append(f"[v2]{LOUDNORM}[out]")
    else:
        filters.append("".join(labels) + f"amix=inputs={len(labels)}:duration=first:dropout_transition=0[m]")
        filters.append(f"[m]{LOUDNORM}[out]")

    cmd = [_ffmpeg(), "-y", *inputs,
           "-filter_complex", ";".join(filters),
           "-map", "[out]", "-c:a", "libmp3lame", "-q:a", "3", str(outp)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=120)
    except Exception as e:  # noqa: BLE001
        return "", f"ffmpeg 异常：{type(e).__name__}: {e}"
    if r.returncode != 0 or not outp.exists():
        err = (r.stderr or b"").decode("utf-8", "replace")[-300:]
        return "", f"ffmpeg 失败：{err}"
    return str(outp), ""


def duck_only(voice, bgm, out=None) -> tuple[str, str]:
    """只要 ducking 那一层的效果（不做 loudnorm）—— 调试/对比用。"""
    return mix(voice, bgm, None, out)


def probe(path) -> dict:
    """读音频信息（时长/采样率/声道）。

    2026-11-07：这台机器只有 ffmpeg.exe，**没有 ffprobe** ——
    所以走 `ffmpeg -i` 的 stderr 解析（那条 "Duration: 00:00:03.02" 就在里面）。
    """
    if not _exists(path):
        return {}
    try:
        r = subprocess.run([_ffmpeg(), "-i", str(path)],
                           capture_output=True, timeout=20)
        err = (r.stderr or b"").decode("utf-8", "replace")
        import re
        out: dict = {}
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", err)
        if m:
            out["duration"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        m = re.search(r"Audio:\s*([\w-]+).*?(\d+)\s*Hz,\s*(mono|stereo|\d+ channels)", err)
        if m:
            out["codec"] = m.group(1)
            out["rate"] = int(m.group(2))
            out["channels"] = m.group(3)
        m = re.search(r"bitrate:\s*(\d+)\s*kb/s", err)
        if m:
            out["kbps"] = int(m.group(1))
        return out
    except Exception:  # noqa: BLE001
        return {}


def stats() -> dict:
    return {
        "ffmpeg": available(),
        "out_dir": str(OUT_DIR),
        "files": len(list(OUT_DIR.glob("*.mp3"))) if OUT_DIR.exists() else 0,
    }
