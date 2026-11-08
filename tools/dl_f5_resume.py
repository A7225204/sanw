"""从 HuggingFace 下载 F5-TTS 权重，带断点续传 + 多次重试。

之前卡在 976MB：单次连接被掐。这里改成 Range 续传，失败接着下。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import httpx

HUB = Path(os.environ["USERPROFILE"]) / ".cache" / "huggingface" / "hub"
REV = "0" * 40

JOBS = [
    ("SWivid/F5-TTS", "F5TTS_v1_Base/model_1250000.safetensors",
     "F5TTS_v1_Base/model_1250000.safetensors"),
    ("SWivid/F5-TTS", "F5TTS_v1_Base/vocab.txt", "F5TTS_v1_Base/vocab.txt"),
    ("charactr/vocos-mel-24khz", "config.yaml", "config.yaml"),
    ("charactr/vocos-mel-24khz", "pytorch_model.bin", "pytorch_model.bin"),
]


def cache_dir(repo: str) -> Path:
    return HUB / ("models--" + repo.replace("/", "--"))


def download(repo: str, remote: str, local: str, tries: int = 12) -> bool:
    root = cache_dir(repo)
    snap = root / "snapshots" / REV
    dst = snap / local
    dst.parent.mkdir(parents=True, exist_ok=True)
    (root / "refs").mkdir(parents=True, exist_ok=True)
    (root / "refs" / "main").write_text(REV, encoding="utf-8")
    part = dst.with_suffix(dst.suffix + ".part")
    url = f"https://huggingface.co/{repo}/resolve/main/{remote}"

    for attempt in range(1, tries + 1):
        have = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            with httpx.stream("GET", url, headers=headers, timeout=120,
                              follow_redirects=True) as r:
                if r.status_code not in (200, 206):
                    print(f"[{local}] HTTP {r.status_code}（第 {attempt} 次）", flush=True)
                    time.sleep(2)
                    continue
                total = have + int(r.headers.get("content-length") or 0)
                mode = "ab" if (r.status_code == 206 and have) else "wb"
                if mode == "wb":
                    have = 0
                done = have
                last = done
                with part.open(mode) as f:
                    for chunk in r.iter_bytes(1 << 20):
                        f.write(chunk)
                        done += len(chunk)
                        if done - last > 200 * 1024 * 1024:
                            last = done
                            print(f"  {local}: {done/1e6:.0f}/{total/1e6:.0f}MB", flush=True)
            if total and part.stat().st_size >= total * 0.995:
                part.replace(dst)
                print(f"[完成] {local} {dst.stat().st_size/1e6:.0f}MB（第 {attempt} 次）", flush=True)
                return True
            print(f"  {local} 未下完（{part.stat().st_size/1e6:.0f}/{total/1e6:.0f}MB），续传…", flush=True)
        except Exception as e:  # noqa: BLE001
            got = part.stat().st_size / 1e6 if part.exists() else 0
            print(f"  {local} 断线（已 {got:.0f}MB）：{type(e).__name__}，重试 {attempt}/{tries}", flush=True)
            time.sleep(2)

    if part.exists():
        part.replace(dst)
        print(f"[降级] {local} 用 {dst.stat().st_size/1e6:.0f}MB 的部分文件继续", flush=True)
        return dst.stat().st_size > 10 * 1024 * 1024
    return False


def main() -> int:
    ok = 0
    for job in JOBS:
        if download(*job):
            ok += 1
    print(f"\n完成 {ok}/{len(JOBS)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
