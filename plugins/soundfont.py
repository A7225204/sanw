"""真乐器渲染：SoundFont + MIDI 出真实音色。

来源：AnyAudio（cloudoyx/any-audio）的核心卖点 ——
> 真实乐器音色 —— rendered from local sample banks instead of hand-written oscillators。

它给的数据：同一段 bossa nova，采样版比 NumPy 合成版**高频细节 6.8%→11.6%，动态余量更大**。
说白了就是「听起来像有人真的在弹」，而不是正弦波拼的。

**我们这边缺什么**：ACE-Step 能出整首带人声的歌，但那是「模型想象的音乐」；
要「真实钢琴/弦乐」这种确定的音色，得靠采样库。

依赖（**当前机器上没有，用到再装**）：
1. `fluidsynth.exe`（Windows 下个 exe 丢进 PATH，或者放 data/soundfont/）
2. 一个 `.sf2` 采样库 —— 推荐 **GeneralUser GS**（免费，~30MB）
   https://schristiancollins.com/generaluser.php

没有这两样时，`available()` 返回 False，`render()` 返回明确提示，不会静默失败。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SF_DIR = ROOT / "data" / "soundfont"
OUT_DIR = ROOT / "data" / "soundfont_out"


def _fluidsynth() -> str | None:
    """找 fluidsynth：PATH → 项目目录 → npm 目录。"""
    p = shutil.which("fluidsynth") or shutil.which("fluidsynth.exe")
    if p:
        return p
    for cand in (SF_DIR / "fluidsynth.exe", SF_DIR / "bin" / "fluidsynth.exe"):
        if cand.exists():
            return str(cand)
    return None


def _sf2() -> Path | None:
    """找采样库：项目目录下任意 .sf2 / .sf3，取最大的那个。"""
    if not SF_DIR.exists():
        return None
    cands = sorted(list(SF_DIR.glob("*.sf2")) + list(SF_DIR.glob("*.sf3")),
                   key=lambda x: -x.stat().st_size)
    return cands[0] if cands else None


def available() -> tuple[bool, str]:
    """能不能用。返回 (ok, 说明)。"""
    f = _fluidsynth()
    s = _sf2()
    if not f and not s:
        return False, ("缺两样：fluidsynth.exe 和 .sf2 采样库。"
                       "推荐 GeneralUser GS（免费 30MB）放进 data/soundfont/")
    if not f:
        return False, "有采样库但缺 fluidsynth.exe（放 data/soundfont/ 或加进 PATH）"
    if not s:
        return False, f"有 fluidsynth 但 data/soundfont/ 里没有 .sf2 文件"
    return True, f"就绪：{Path(f).name} + {s.name}（{s.stat().st_size // 1024}KB）"


def write_midi(notes: list[dict], out_midi=None, tempo: int = 120,
               program: int = 0) -> tuple[str, str]:
    """把音符列表写成 MIDI 文件。

    notes = [{"pitch": 60, "start": 0.0, "dur": 0.5, "vel": 90}, ...]
        pitch  MIDI 音高（60 = C4）
        start  起始秒
        dur    时长秒
        vel    力度 0~127

    program 是 General MIDI 音色号（0=大钢琴，40=小提琴，48=弦乐合奏…）
    """
    try:
        from midiutil import MIDIFile
    except ImportError:
        return "", "缺 midiutil（pip install midiutil）"
    if not notes:
        return "", "没有音符"
    m = MIDIFile(1)
    tr = 0
    m.addTempo(tr, 0, tempo)
    m.addProgramChange(tr, 0, 0, max(0, min(127, int(program))))
    for n in notes:
        try:
            m.addNote(tr, 0, max(0, min(127, int(n["pitch"]))),
                      max(0.0, float(n.get("start", 0))),
                      max(0.05, float(n.get("dur", 0.4))),
                      max(1, min(127, int(n.get("vel", 90)))))
        except Exception:  # noqa: BLE001
            continue
    outp = Path(str(out_midi)) if out_midi else (
        SF_DIR.parent / f"midi_{int(__import__('time').time())}.mid")
    outp.parent.mkdir(parents=True, exist_ok=True)
    with open(outp, "wb") as f:
        m.writeFile(f)
    return str(outp), ""


def render(notes: list[dict], out=None, tempo: int = 120,
           program: int = 0) -> tuple[str, str]:
    """音符 → 真实乐器音色（wav）。返回 (路径, 错误)。"""
    ok, why = available()
    if not ok:
        return "", why
    midi, err = write_midi(notes, tempo=tempo, program=program)
    if not midi:
        return "", err

    f = _fluidsynth()
    s = _sf2()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    outp = Path(str(out)) if out else (
        OUT_DIR / f"render_{int(__import__('time').time())}.wav")
    cmd = [f, "-ni", "-g", "0.8", "-F", str(outp), str(s), str(midi)]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=180)
    except Exception as e:  # noqa: BLE001
        return "", f"fluidsynth 异常：{type(e).__name__}: {e}"
    Path(midi).unlink(missing_ok=True)
    if p.returncode != 0 or not outp.exists():
        err = (p.stderr or b"").decode("utf-8", "replace")[-200:]
        return "", f"fluidsynth 失败：{err}"
    return str(outp), ""


# General MIDI 音色号速查（写曲子时用）
GM = {
    0: "Acoustic Grand Piano", 4: "Electric Piano", 24: "Acoustic Guitar",
    32: "Acoustic Bass", 40: "Violin", 42: "Cello", 48: "String Ensemble",
    56: "Trumpet", 65: "Alto Sax", 73: "Flute", 80: "Square Lead",
    88: "Pad (warm)", 118: "Synth Drum",
}


def stats() -> dict:
    ok, why = available()
    return {
        "ready": ok,
        "detail": why,
        "sf_dir": str(SF_DIR),
        "sf2": (_sf2().name if _sf2() else None),
        "renders": len(list(OUT_DIR.glob("*.wav"))) if OUT_DIR.exists() else 0,
    }
