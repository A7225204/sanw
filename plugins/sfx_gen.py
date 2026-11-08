"""音效生成：不用下载音效库，用 ffmpeg 现场合成。

来源：AnyAudio 说明里的「sound effects either sampled from the installed banks
or **generated procedurally**」—— 它两条路都有，我们只做第二条。

为什么不用下载：CC0 音效库要么大（几百 MB）、要么来源杂、要么授权不清。
**她需要的音效其实就那么十几种，用合成完全够** ——
雨声是白噪声滤波，脚步是短脉冲，关门是低频冲击。

好处：零依赖、零版权风险、体积为 0、随时能调参数。

用法：
    gen("rain")            -> (路径, 错误)
    gen("rain", seconds=8)
    list_all()             -> 支持的音效名
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "sfx"

# 每种音效的 ffmpeg 合成配方
# 语法：lavfi 输入 + 滤镜链
RECIPES: dict[str, dict] = {
    "rain": {
        "desc": "雨声（白噪声 + 低通 + 轻微起伏）",
        "filter": "anoisesrc=color=brown:amplitude=0.35,"
                  "highpass=f=300,lowpass=f=3000,"
                  "tremolo=f=0.15:d=0.25",
        "default_sec": 10,
    },
    "rain_heavy": {
        "desc": "大雨（褐噪 + 更宽频段 + 雨滴碎响）",
        "filter": "anoisesrc=color=brown:amplitude=0.55,"
                  "highpass=f=200,lowpass=f=6000,"
                  "tremolo=f=0.35:d=0.35",
        "default_sec": 10,
    },
    "wind": {
        "desc": "风声（低频褐噪 + 慢速起伏）",
        "filter": "anoisesrc=color=brown:amplitude=0.4,"
                  "lowpass=f=800,tremolo=f=0.08:d=0.6",
        "default_sec": 12,
    },
    "water": {
        "desc": "流水（棕噪 + 中频带通）",
        "filter": "anoisesrc=color=pink:amplitude=0.4,"
                  "highpass=f=600,lowpass=f=4000,"
                  "tremolo=f=0.6:d=0.15",
        "default_sec": 10,
    },
    "fire": {
        "desc": "柴火（低噪 + 不规则爆裂，用 tremolo 近似）",
        "filter": "anoisesrc=color=brown:amplitude=0.25,"
                  "lowpass=f=1200,tremolo=f=2.7:d=0.85",
        "default_sec": 10,
    },
    "keyboard": {
        "desc": "打字的敲击声（短促脉冲）",
        "filter": "anoisesrc=color=white:amplitude=0.5,"
                  "highpass=f=1500,lowpass=f=8000,"
                  "tremolo=f=11:d=0.95",
        "default_sec": 4,
    },
    "notification": {
        "desc": "提示音（两个短正弦）",
        "filter": None,     # 走专门的正弦拼接
        "default_sec": 1,
    },
    "bell": {
        "desc": "铃声（衰减正弦）",
        "filter": None,
        "default_sec": 2,
    },
    "ambient": {
        "desc": "环境底噪（很轻，垫在语音下面）",
        "filter": "anoisesrc=color=pink:amplitude=0.12,lowpass=f=1500",
        "default_sec": 15,
    },
}


def _ffmpeg() -> str:
    import shutil
    return shutil.which("ffmpeg") or str(
        Path(os.environ.get("APPDATA", "")) / "npm" / "ffmpeg")


def available() -> bool:
    try:
        return subprocess.run([_ffmpeg(), "-version"],
                              capture_output=True, timeout=8).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def list_all() -> dict:
    return {k: v["desc"] for k, v in RECIPES.items()}


def gen(name: str, seconds: float | None = None, out=None) -> tuple[str, str]:
    """生成一个音效。返回 (路径, 错误)。"""
    if not available():
        return "", "ffmpeg 不可用"
    key = str(name or "").lower().strip()
    r = RECIPES.get(key)
    if not r:
        return "", f"不认识的音效：{name}（可用：{'/'.join(RECIPES)}）"
    sec = float(seconds or r["default_sec"])
    sec = max(0.5, min(60.0, sec))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    outp = Path(str(out)) if out else (OUT_DIR / f"{key}_{int(time.time())}.mp3")

    if r["filter"]:
        cmd = [_ffmpeg(), "-y", "-f", "lavfi",
               "-i", f"{r['filter']},volume=0.9",
               "-t", f"{sec:.2f}", "-c:a", "libmp3lame", "-q:a", "5", str(outp)]
    elif key == "notification":
        cmd = [_ffmpeg(), "-y",
               "-f", "lavfi", "-i", f"sine=frequency=880:duration=0.12",
               "-f", "lavfi", "-i", f"sine=frequency=1320:duration=0.16",
               "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1,afade=t=out:st=0.1:d=0.18",
               "-c:a", "libmp3lame", "-q:a", "4", str(outp)]
    else:  # bell
        cmd = [_ffmpeg(), "-y", "-f", "lavfi",
               "-i", f"sine=frequency=1046:duration={sec:.2f}",
               "-af", f"afade=t=in:st=0:d=0.01,afade=t=out:st=0.05:d={max(0.1, sec - 0.1):.2f}",
               "-c:a", "libmp3lame", "-q:a", "4", str(outp)]

    try:
        p = subprocess.run(cmd, capture_output=True, timeout=60)
    except Exception as e:  # noqa: BLE001
        return "", f"生成异常：{type(e).__name__}: {e}"
    if p.returncode != 0 or not outp.exists():
        err = (p.stderr or b"").decode("utf-8", "replace")[-200:]
        return "", f"ffmpeg 失败：{err}"
    return str(outp), ""


def pick(text: str) -> str:
    """从他这句话里猜该配什么音效。空串=不用配。"""
    t = str(text or "")
    rules = [
        (r"(下雨|雨天|雨声|淅沥|淋雨)", "rain"),
        (r"(暴雨|大雨|雨好大)", "rain_heavy"),
        (r"(风|刮风|起风)", "wind"),
        (r"(水|流水|溪|海|浪|洗|泡澡)", "water"),
        (r"(火|柴|烧|烤|篝火)", "fire"),
        (r"(打字|码字|敲键盘|写代码)", "keyboard"),
        (r"(铃|叮|闹钟|提示)", "bell"),
        (r"(安静|深夜|夜|底噪|氛围)", "ambient"),
    ]
    for pat, name in rules:
        if re.search(pat, t):
            return name
    return ""


def stats() -> dict:
    return {
        "ffmpeg": available(),
        "presets": len(RECIPES),
        "files": len(list(OUT_DIR.glob("*.mp3"))) if OUT_DIR.exists() else 0,
    }
