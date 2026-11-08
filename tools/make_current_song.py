"""独立跑现行唱歌链路：ACE 1.5(40步) → demucs → Seed-VC → 混伴奏。

产物放 outbox，投递箱双发 QQ + 飞书。
"""
from __future__ import annotations

import pathlib
import shutil
import sys
import time

sys.path.insert(0, r"D:\qq-ai-bot")

import os

for line in pathlib.Path(r"D:\qq-ai-bot\.env").read_text(encoding="utf-8", errors="ignore").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

SONG = "摸鱼下班"
LYRICS = """闹钟又响了，我还想再睡一会儿
老板的消息，我假装没看见
偷偷摸鱼是我的小秘密
下班铃一响，我跑得比谁都快"""

OUTBOX = pathlib.Path(r"D:\qq-ai-bot\outbox")
WORK = pathlib.Path(r"D:\qq-ai-bot\logs\song_lab")
WORK.mkdir(parents=True, exist_ok=True)


def main() -> int:
    from plugins import ace_sing15, seedvc, song_mix

    t0 = time.time()
    print("① ACE 生成中（40 步，约 90~110 秒）…", flush=True)
    raw, _msg = ace_sing15.sing(LYRICS, seconds=30.0, language="zh")
    raw_path = pathlib.Path(str(raw))
    print(f"   完成 {time.time()-t0:.0f}s → {raw_path.name}", flush=True)

    print("② 分离 + 变声 + 混伴奏（一步到位）…", flush=True)
    t1 = time.time()
    out, err = song_mix.sing_with_backing(str(raw_path))
    if err:
        print("   错误:", err)
    if not out or not pathlib.Path(out).exists():
        print("没有成品")
        return 1
    final = OUTBOX / f"现行链路_{SONG}{pathlib.Path(out).suffix}"
    shutil.copy2(out, final)
    print(f"   成品 {final.name} {final.stat().st_size//1024}KB（{time.time()-t1:.0f}s）")
    print(f"总耗时 {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
