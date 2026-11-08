"""同一模型、不同推理参数对比：找出"不好听"是参数问题还是训练问题。

输出到 D:\\qq-ai-bot\\outbox\\ 由投递箱发到 QQ。
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, r"D:\qq-ai-bot")
from plugins import song_mix  # noqa: E402

SVC = Path(r"D:\so-vits-svc\.venv\Scripts\svc.exe")
WORK = Path(r"D:\so-vits-svc")
VOCALS = WORK / "out" / "vocals_in.wav"
BACK = Path(r"D:\Seed-VC\sep\htdemucs\sing15_00005\no_vocals.mp3")
OUT_BOX = Path(r"D:\qq-ai-bot\outbox")
MODEL = r"logs\44k\G_120.pth"
CONFIG = r"configs\config.json"

CASES = [
    ("A_crepe_t0", "crepe", 0),
    ("B_harvest_t0", "harvest", 0),
    ("C_harvest_t+2", "harvest", 2),
    ("D_harvest_t-2", "harvest", -2),
]


def main() -> None:
    if not VOCALS.exists():
        print("缺输入人声", VOCALS)
        return
    OUT_BOX.mkdir(parents=True, exist_ok=True)
    for tag, fm, transpose in CASES:
        raw = WORK / "out" / f"{tag}.wav"
        cmd = [str(SVC), "infer", str(VOCALS), "-m", MODEL, "-c", CONFIG,
               "-s", "feiyu", "-fm", fm, "-t", str(transpose), "-o", str(raw)]
        p = subprocess.run(cmd, cwd=str(WORK), capture_output=True, timeout=600)
        if not raw.exists():
            print(f"{tag} 失败：{(p.stderr or b'')[-200:]}")
            continue
        final = OUT_BOX / f"对比_{tag}_带伴奏.mp3"
        ok, err = song_mix.mix(raw, BACK, final)
        print(f"{tag}: {'OK' if ok else err}", flush=True)


if __name__ == "__main__":
    main()
