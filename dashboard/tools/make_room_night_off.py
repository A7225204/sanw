# -*- coding: utf-8 -*-
"""由 room_night.jpg 生成「熄屏版」room_night_off.jpg。

屏幕是个透视梯形，按多边形遮罩把原画面糊掉再压暗，加一道斜向反光，
看起来像关着的显示器。换底图后重跑即可。

用法：python tools/make_room_night_off.py
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

HERE = Path(__file__).resolve().parent.parent / "assets"
SRC = HERE / "room_night.jpg"
DST = HERE / "room_night_off.jpg"

# 屏幕四角（2048×1152 底图上的坐标，按 room_night.jpg 目测；换图要重标）
QUAD = [(1628, 408), (1914, 390), (1924, 594), (1638, 608)]
SHEEN = [(1662, 566), (1746, 598), (1906, 476), (1886, 436)]


def main() -> int:
    src = Image.open(SRC).convert("RGB")
    w, h = src.size

    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).polygon(QUAD, fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(3))
    m = (np.asarray(mask).astype(float) / 255.0)[..., None]

    base = np.asarray(src).astype(float)
    blur = np.asarray(src.filter(ImageFilter.GaussianBlur(26))).astype(float)
    dark = np.array([15, 20, 36], dtype=float)
    glass = blur * 0.06 + dark * 0.94
    out = base * (1 - m) + glass * m

    sheen = Image.new("L", (w, h), 0)
    ImageDraw.Draw(sheen).polygon(SHEEN, fill=28)
    sheen = sheen.filter(ImageFilter.GaussianBlur(16))
    s = (np.asarray(sheen).astype(float) / 255.0)[..., None]
    out = out + s * np.array([80, 100, 140], dtype=float) * m

    Image.fromarray(np.clip(out, 0, 255).astype(np.uint8)).save(DST, quality=93)
    print("written", DST)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
