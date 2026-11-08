"""手动把 F5-TTS 权重下到 HF 缓存目录（绕开 hf_hub 卡住的问题）。"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import httpx

HUB = Path(os.environ["USERPROFILE"]) / ".cache" / "huggingface" / "hub"
REV = "0" * 40

JOBS = [
    ("SWivid/F5-TTS", "F5TTS_v1_Base/model_1250000.safetensors", "F5TTS_v1_Base/model_1250000.safetensors"),
    ("SWivid/F5-TTS", "F5TTS_v1_Base/vocab.txt", "F5TTS_v1_Base/vocab.txt"),
    ("charactr/vocos-mel-24khz", "config.yaml", "config.yaml"),
    ("charactr/vocos-mel-24khz", "pytorch_model.bin", "pytorch_model.bin"),
]


def cache_dir(repo: str) -> Path:
    return HUB / ("models--" + repo.replace("/", "--"))


def download(repo: str, remote: str, local: str) -> bool:
    root = cache_dir(repo)
    snap = root / "snapshots" / REV
    dst = snap / local
    dst.parent.mkdir(parents=True, exist_ok=True)
    (root / "refs").mkdir(parents=True, exist_ok=True)
    (root / "refs" / "main").write_text(REV, encoding="utf-8")
    if dst.exists() and dst.stat().st_size > 1024 * 1024:
        print(f"[跳过] {local} {dst.stat().st_size/1e6:.0f}MB", flush=True)
        return True
    url = f"https://huggingface.co/{repo}/resolve/main/{remote}"
    t0 = time.time()
    part = dst.with_suffix(dst.suffix + ".part")
    try:
        with httpx.stream("GET", url, timeout=180, follow_redirects=True) as r:
            if r.status_code != 200:
                print(f"[失败] {local} HTTP {r.status_code}", flush=True)
                return False
            total = int(r.headers.get("content-length") or 0)
            done = last = 0
            with part.open("wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
                    done += len(chunk)
                    if done - last > 100 * 1024 * 1024:
                        last = done
                        print(f"  {local}: {done/1e6:.0f}/{total/1e6:.0f}MB", flush=True)
        part.replace(dst)
        print(f"[完成] {local} {dst.stat().st_size/1e6:.0f}MB {time.time()-t0:.0f}s", flush=True)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[异常] {local}: {type(e).__name__}: {str(e)[:90]}", flush=True)
        return False


def main() -> int:
    ok = 0
    for repo, remote, local in JOBS:
        if download(repo, remote, local):
            ok += 1
    print(f"\n完成 {ok}/{len(JOBS)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
