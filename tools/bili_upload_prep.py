"""B站投稿准备：出封面 + 标题/简介/标签，拷到待上传目录（不真正投稿）。

用法：
    python tools/bili_upload_prep.py data/bili_video/demo.mp4 --title "DAY 16 今天也想摸鱼"
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "bili_upload"
FONT = [r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\msyh.ttc"]


def _ffmpeg() -> str:
    p = ROOT / "data" / "bin" / "ffmpeg.exe"
    return str(p) if p.exists() else "ffmpeg"


def cover(video: Path, title: str, out: Path) -> Path:
    frame = out.parent / "_frame.png"
    subprocess.run([_ffmpeg(), "-y", "-loglevel", "error", "-ss", "1.0",
                    "-i", str(video), "-frames:v", "1", str(frame)], check=False)
    base = Image.open(frame).convert("RGB") if frame.exists() else Image.new("RGB", (1920, 1080), (16, 32, 54))
    base = base.resize((1280, 720), Image.LANCZOS)
    # 顶部压一条半透明黑带，标题放里面（不压画面下方的字幕）
    band = Image.new("RGBA", base.size, (0, 0, 0, 0))
    band.paste(Image.new("RGBA", (base.size[0], 210), (8, 16, 30, 190)), (0, 0))
    base = Image.alpha_composite(base.convert("RGBA"), band.convert("RGBA")).convert("RGB")
    d = ImageDraw.Draw(base)
    font = ImageFont.truetype(FONT[0], 58) if Path(FONT[0]).exists() else ImageFont.load_default()
    lines, cur = [], ""
    for ch in title:
        if font.getlength(cur + ch) > 1140 and cur:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    y = 40
    for ln in lines[:2]:
        d.text((60, y), ln, font=font, fill=(255, 255, 255))
        y += 74
    base.save(out)
    try:
        frame.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--title", default="")
    ap.add_argument("--desc", default="")
    a = ap.parse_args()
    v = Path(a.video)
    if not v.exists():
        raise SystemExit(f"找不到视频：{v}")
    stem = v.stem
    title = a.title or stem
    d = OUT / stem
    d.mkdir(parents=True, exist_ok=True)
    shutil.copy2(v, d / v.name)
    cover(v, title, d / "cover.jpg")
    desc = a.desc or f"大肥鱼日常 · {title}\n\n#大肥鱼 #AI语音 #日常"
    (d / "投稿信息.txt").write_text(
        f"标题：{title}\n分区：生活 / 日常\n标签：大肥鱼,AI语音,日常\n\n简介：\n{desc}\n",
        encoding="utf-8")
    print(f"已准备：{d}")
    for f in sorted(d.iterdir()):
        print("  ", f.name, f.stat().st_size // 1024, "KB")


if __name__ == "__main__":
    main()
