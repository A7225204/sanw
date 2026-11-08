"""只出一帧，用来校准人物/嘴/眼的位置（不生成视频）。

用法：
    python tools/bili_frame.py -o data/bili_video/preview.png            # 带坐标网格
    python tools/bili_frame.py --mx 0.455 --my 0.555 -o x.png            # 画一个嘴的标记
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.make_bili_video import (  # noqa: E402
    CHAR, EYE_FRACS, H, MOUTH_FRAC, W, _bg, _cut_white, _font, _header, _day_index,
)


def frame(out: Path, mx: float | None, my: float | None, grid: bool,
          full: bool = False, zoom: bool = False, blink: bool = True) -> Path:
    base = _bg()
    char = _cut_white(Image.open(CHAR))
    bb = char.getbbox()
    if bb:
        char = char.crop(bb)
    cw, ch = char.size
    scale = int(H * 0.98)
    char = char.resize((scale, int(scale * ch / max(1, cw))), Image.LANCZOS)
    cw, ch = char.size
    cx = W - cw + int(cw * 0.10)
    cy = H - ch + int(ch * 0.07)
    base.paste(char, (cx, cy), char)
    d = ImageDraw.Draw(base)
    _header(d, _day_index())

    if grid:
        font = _font(26)
        for p in range(5, 100, 5):
            x = cx + cw * p / 100
            y = cy + ch * p / 100
            col = (90, 120, 150) if p % 10 else (140, 180, 220)
            d.line([(x, cy), (x, cy + ch)], fill=col, width=1)
            d.line([(cx, y), (cx + cw, y)], fill=col, width=1)
            if p % 10 == 0:
                d.text((cx - 46, y - 12), f"{p}", font=font, fill=(180, 210, 240))
                d.text((x - 12, cy - 34), f"{p}", font=font, fill=(180, 210, 240))

    if zoom:                     # 只留人物框放大，方便读百分比
        crop = base.crop((cx, cy, cx + cw, cy + ch))
        crop = crop.resize((int(cw * 1.5), int(ch * 1.5)), Image.LANCZOS)
        d2 = ImageDraw.Draw(crop)
        font2 = _font(22)
        for p in range(5, 100, 5):
            gx = crop.size[0] * p / 100
            gy = crop.size[1] * p / 100
            col = (80, 110, 140) if p % 10 else (170, 205, 240)
            d2.line([(gx, 0), (gx, crop.size[1])], fill=col, width=1)
            d2.line([(0, gy), (crop.size[0], gy)], fill=col, width=1)
            if p % 10 == 0:
                d2.text((gx + 3, 4), str(p), font=font2, fill=(200, 225, 250))
                d2.text((4, gy + 2), str(p), font=font2, fill=(200, 225, 250))
        crop.save(out)
        print(f"写好了：{out}  (人物框放大 1.5x)")
        return out

    if full:                     # 和视频里一样的画法（嘴 + 睁眼/眨眼框），用来对位
        _chh = ch
        mh = 0.045 * _chh * 0.55
        mw = 0.052 * _chh
        pxx = cx + cw * MOUTH_FRAC[0]
        pyy = cy + _chh * MOUTH_FRAC[1]
        d.ellipse((pxx - mw / 2, pyy - mh / 2, pxx + mw / 2, pyy + mh / 2),
                  fill=(150, 46, 60))
        for _fx, _fy in (EYE_FRACS if blink else ()):
            x0 = cx + cw * (_fx - 0.030)
            y0 = cy + _chh * _fy
            d.line([(x0, y0), (x0 + cw * 0.060, y0)], fill=(255, 90, 90), width=4)

    if mx is not None and my is not None:
        px = cx + cw * mx
        py = cy + ch * my
        r = 18
        d.ellipse((px - r, py - r, px + r, py + r), outline=(255, 90, 90), width=4)
        d.line([(px - 46, py), (px + 46, py)], fill=(255, 140, 140), width=1)
        d.line([(px, py - 46), (px, py + 46)], fill=(255, 140, 140), width=1)

    out.parent.mkdir(parents=True, exist_ok=True)
    base.save(out)
    print(f"写好了：{out}   人物框 {cw}x{ch} @ ({cx},{cy})")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default="data/bili_video/preview.png")
    ap.add_argument("--mx", type=float, default=None)
    ap.add_argument("--my", type=float, default=None)
    ap.add_argument("--no-grid", action="store_true")
    ap.add_argument("--full", action="store_true", help="按视频一样的画法画嘴和眼")
    ap.add_argument("--zoom", action="store_true", help="只裁人物框放大，带 5% 网格")
    ap.add_argument("--no-blink", action="store_true", help="不画眨眼线")
    a = ap.parse_args()
    frame(Path(a.out), a.mx, a.my, not a.no_grid, full=a.full, zoom=a.zoom,
          blink=not a.no_blink)


if __name__ == "__main__":
    main()
