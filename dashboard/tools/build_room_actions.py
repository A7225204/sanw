# -*- coding: utf-8 -*-
"""把桌宠 frameseq 里每个动作转成网页可播的动图，并生成 manifest。

用法：python tools/build_room_actions.py [--force]
输出：dashboard/assets/pet/all/<动作id>.webp + manifest.json（id/名字/分类/几何）
"""
from __future__ import annotations

import argparse
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SRC = Path(r"C:\Users\Administrator\dsh-pet\_internal\assets\characters\shenshen\frameseq")
HERE = Path(__file__).resolve().parent.parent
OUT = HERE / "assets" / "pet" / "all"
MANIFEST = OUT / "manifest.json"
FF = r"C:\Users\Administrator\AppData\Roaming\npm\ffmpeg"
CROP = "crop=600:360:25:0"
SCALE = "scale=400:240:flags=lanczos"
FPS = "12"
Q = "54"


def convert(job: tuple[str, Path, bool]) -> dict | None:
    cat, folder, force = job
    name, _, gid = folder.name.partition(".")
    src = folder / "f_0001.webp"
    if not src.exists() or not gid:
        return None
    out = OUT / (gid + ".webp")
    if not force and out.exists() and out.stat().st_size > 5000:
        return {"id": gid, "name": name, "cat": cat, "url": "/assets/pet/all/" + gid + ".webp", "skip": True}
    cmd = [
        FF, "-y", "-hide_banner", "-loglevel", "error",
        "-framerate", "24", "-start_number", "1", "-i", str(folder / "f_%04d.webp"),
        "-vf", f"{CROP},fps={FPS},{SCALE}",
        "-c:v", "libwebp_anim", "-lossless", "0", "-q:v", Q, "-loop", "0", "-an", str(out),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return {"id": gid, "name": name, "cat": cat, "url": "/assets/pet/all/" + gid + ".webp"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    jobs: list[tuple[str, Path, bool]] = []
    for cat in sorted(SRC.iterdir()):
        if not cat.is_dir():
            continue
        for folder in sorted(cat.iterdir()):
            if folder.is_dir() and (folder / "f_0001.webp").exists():
                jobs.append((cat.name, folder, args.force))

    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = [r for r in pool.map(convert, jobs) if r]

    rows.sort(key=lambda r: (r["cat"] != "idle", r["cat"], r["name"]))
    MANIFEST.write_text(json.dumps({
        "crop": CROP, "display_width": 34, "display_bottom": 2.5,
        "count": len(rows), "actions": rows,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"done: {len(rows)} actions -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
