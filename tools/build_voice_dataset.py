"""给 RVC 训练准备大肥鱼的音色数据集。

来源：D:\\GPT-SoVITS\\dataset\\gpt_sovits_ready\\wavs（56 条，约 9.9 分钟，她的音色）
输出：D:\\qq-ai-bot\\data\\voice_train\\wavs\\*.wav  44.1kHz 单声道、去首尾静音、响度归一
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, r"D:\qq-ai-bot")
from plugins.douyin_fetch import _ffmpeg  # noqa: E402

FF = _ffmpeg()
SRC = Path(r"D:\GPT-SoVITS\dataset\gpt_sovits_ready\wavs")
OUT = Path(r"D:\qq-ai-bot\data\voice_train\wavs")

# silenceremove 去首尾静音；loudnorm 统到 -16 LUFS；aresample 44.1k（RVC 默认采样率）
AF = ("silenceremove=start_periods=1:start_silence=0.05:start_threshold=-45dB,"
      "areverse,silenceremove=start_periods=1:start_silence=0.05:start_threshold=-45dB,areverse,"
      "highpass=f=60,afftdn=nf=-30,"
      "loudnorm=I=-16:TP=-1.5:LRA=11,"
      "aresample=44100")


def duration(path: Path) -> float:
    r = subprocess.run([FF, "-i", str(path)], capture_output=True, text=True,
                       encoding="utf-8", errors="ignore")
    for line in r.stderr.splitlines():
        if "Duration" in line:
            t = line.split("Duration:")[1].split(",")[0].strip()
            h, m, s = t.split(":")
            return int(h) * 3600 + int(m) * 60 + float(s)
    return 0.0


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*.wav"):
        old.unlink()
    total = 0.0
    kept = 0
    for i, src in enumerate(sorted(SRC.glob("*.wav"))):
        dst = OUT / f"feiyu_{i:03d}.wav"
        subprocess.run([FF, "-y", "-i", str(src), "-af", AF, "-ac", "1", "-ar", "44100", str(dst)],
                       capture_output=True, timeout=180)
        if not dst.exists() or dst.stat().st_size < 20 * 1024:
            continue
        d = duration(dst)
        if d < 1.0:          # 太短（<1s）丢掉，RVC 切片会失败
            dst.unlink()
            continue
        total += d
        kept += 1
    print(f"数据集：{kept} 条，总时长 {total/60:.1f} 分钟 → {OUT}")
    if total < 300:
        print("⚠️ 不足 5 分钟，RVC 训练效果会差；建议再补素材")


if __name__ == "__main__":
    main()
