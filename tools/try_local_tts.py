"""用 F5-TTS 零样本克隆大肥鱼的声音说一句话。

参考音频：从她的语料里挑一条带文本的（metadata.csv 里有对应文字）
输出：D:\\qq-ai-bot\\outbox\\本地TTS_试听.mp3（投递箱会发到 QQ）
"""
from __future__ import annotations

import csv
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(r"D:\GPT-SoVITS\dataset\gpt_sovits_ready")
OUT_BOX = Path(r"D:\qq-ai-bot\outbox")
GEN_TEXT = "主人，本鱼现在可以自己说话了，不用再麻烦外面的服务啦。"


def pick_reference() -> tuple[Path, str]:
    """挑一条 4~10 秒、文本完整的参考。"""
    rows = []
    with (ROOT / "metadata.csv").open(encoding="utf-8", errors="ignore") as f:
        for r in csv.DictReader(f):
            wav = ROOT / r["file_name"]
            text = (r.get("text") or "").strip()
            if wav.exists() and 6 <= len(text) <= 40:
                rows.append((wav, text))
    if not rows:
        raise SystemExit("没有合适的参考音频")
    return rows[0]


def main() -> int:
    ref_wav, ref_text = pick_reference()
    print(f"参考音频：{ref_wav.name}")
    print(f"参考文本：{ref_text[:40]}")
    OUT_BOX.mkdir(parents=True, exist_ok=True)
    out_wav = OUT_BOX / "本地TTS_试听.wav"

    from f5_tts.api import F5TTS  # 延迟导入，避免加载失败时没有提示

    t0 = time.time()
    f5 = F5TTS()
    print(f"模型加载 {time.time()-t0:.0f}s", flush=True)
    t1 = time.time()
    f5.infer(
        ref_file=str(ref_wav),
        ref_text=ref_text,
        gen_text=GEN_TEXT,
        file_wave=str(out_wav),
    )
    print(f"合成 {time.time()-t1:.1f}s → {out_wav}")

    # 转 mp3 方便 QQ 播放
    try:
        sys.path.insert(0, r"D:\qq-ai-bot")
        from plugins.douyin_fetch import _ffmpeg
        ff = _ffmpeg()
        mp3 = out_wav.with_suffix(".mp3")
        subprocess.run([ff, "-y", "-i", str(out_wav), "-b:a", "192k", str(mp3)],
                       capture_output=True, timeout=120)
        if mp3.exists():
            out_wav.unlink(missing_ok=True)
            print(f"已转 mp3：{mp3.stat().st_size // 1024}KB")
    except Exception as e:  # noqa: BLE001
        print("转 mp3 失败（wav 也能发）：", e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
