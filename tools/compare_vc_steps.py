"""对比不同变声参数下的杂音程度，产出可试听样本。

A: 当前线上参数（15 步、4 段拼接参考音）
B: 25 步 + 单段干净参考音 + 人声轻度降噪
"""
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, r"D:\qq-ai-bot")
from plugins.douyin_fetch import _ffmpeg  # noqa: E402
from plugins import seedvc, song_mix  # noqa: E402

FF = _ffmpeg()
VOCALS = Path(r"D:\Seed-VC\sep\htdemucs\sing15_00005\vocals.mp3")
BACK = Path(r"D:\Seed-VC\sep\htdemucs\sing15_00005\no_vocals.mp3")
OUT = Path(r"D:\qq-ai-bot\歌曲")
CLIPS = Path(r"D:\GPT-SoVITS\dataset\gpt_sovits_ready\wavs")


def clean_ref() -> Path:
    """单段最长的语音 + 去静音，做成更干净的参考音。"""
    wavs = sorted(CLIPS.glob("*.wav"), key=lambda p: p.stat().st_size, reverse=True)
    best = wavs[0]
    dst = Path(r"D:\Seed-VC\ref_feiyu_clean.wav")
    subprocess.run(
        [FF, "-y", "-i", str(best),
         "-af", "silenceremove=start_periods=1:start_silence=0.1:start_threshold=-45dB,"
                "areverse,silenceremove=start_periods=1:start_silence=0.1:start_threshold=-45dB,areverse,"
                "atempo=1.0,aresample=44100",
         "-ac", "1", str(dst)],
        capture_output=True, timeout=120)
    print(f"干净参考音：{best.name} → {dst.stat().st_size // 1024}KB", flush=True)
    return dst


def denoise(src: Path, dst: Path) -> Path:
    subprocess.run(
        [FF, "-y", "-i", str(src), "-af", "afftdn=nf=-28,highpass=f=70",
         "-b:a", "192k", str(dst)],
        capture_output=True, timeout=180)
    print(f"人声降噪：{dst.stat().st_size // 1024}KB", flush=True)
    return dst


def run(tag: str, steps: int, ref: Path, vocal_src: Path) -> None:
    tmp = Path(tempfile.mkdtemp(prefix=f"cmp_{tag}_"))
    print(f"\n=== {tag}: steps={steps} ===", flush=True)
    conv, err = seedvc.convert(str(vocal_src), steps, True) if ref == seedvc.REF else _convert_with_ref(vocal_src, ref, steps)
    if not conv:
        print(f"{tag} 变声失败: {err}", flush=True)
        return
    out = OUT / f"对比_{tag}_带伴奏.mp3"
    final, err2 = song_mix.mix(Path(conv), BACK, out)
    print(f"{tag} 成品: {final} {out.stat().st_size // 1024 if out.exists() else 0}KB err={err2}", flush=True)


def _convert_with_ref(src: Path, ref: Path, steps: int) -> tuple[str | None, str]:
    """用指定参考音跑 Seed-VC（seedvc.convert 固定用默认参考音，这里临时替换）。"""
    old = seedvc.REF
    try:
        seedvc.REF = ref
        return seedvc.convert(str(src), steps, True)
    finally:
        seedvc.REF = old


if __name__ == "__main__":
    ref = clean_ref()
    dn = denoise(VOCALS, Path(tempfile.gettempdir()) / "vocals_dn.mp3")
    run("B_25步_降噪", 25, ref, dn)
    print("\n全部完成", flush=True)
