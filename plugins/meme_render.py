"""本地表情包模板渲染（借鉴 dsh-nekowhale-stickers 的模板思路）。

不调云端、不花钱：拿她现有的表情包当"底图角色"，叠白板/气泡/对联条。

模板：
    sign     举牌（白板 + 文字，角色在下）
    think    想（思考气泡，角色在下）
    couplet  对联（红条竖排；一句=上联，两句=上下联）
    hug      抱住（文字压在角色胸前）

用法：
    render("sign", "今天也要摸鱼") -> Path
    render("couplet", "代码一遍过")          # 只有上联
    render("couplet", "代码一遍过|上线就崩")  # 上联|下联
"""
from __future__ import annotations

import json
import os
import random
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
STICKER_DIRS = [ROOT / "表情包" / n for n in ("开心", "卖萌", "得意", "无语")]
OUT_DIR = ROOT / "data" / "meme_local"
FONT_PATHS = [
    r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts\simsun.ttc",
]

W = H = 640


def _font(size: int) -> ImageFont.FreeTypeFont:
    for p in FONT_PATHS:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:  # noqa: BLE001
                continue
    return ImageFont.load_default()


def _bg() -> Image.Image:
    """淡粉到浅蓝的斜向渐变底。"""
    img = Image.new("RGB", (W, H), "#f6f7ff")
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / H
        r = int(250 - 20 * t)
        g = int(246 - 10 * t)
        b = int(255 - 8 * t)
        d.line([(0, y), (W, y)], fill=(r, g, b))
    return img


def _pick_sticker() -> Path | None:
    """挑底图：优先没有自带文字的（带字的叠上去会撞字）。"""
    import json as _json
    idx = {}
    try:
        idx = _json.loads((ROOT / "memory" / "sticker_index.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        idx = {}
    cands: list[Path] = []
    for d in STICKER_DIRS:
        if d.is_dir():
            cands += [f for f in d.iterdir()
                      if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif")]
    if not cands:
        return None
    clean = [f for f in cands
             if not any(k in str(idx.get(str(f)) or "") for k in ("文字", "写着", "字幕", "字样", "字条"))]
    return random.choice(clean or cands)


def _sticker_layer(max_w: int, max_h: int) -> Image.Image | None:
    p = _pick_sticker()
    if not p:
        return None
    try:
        im = Image.open(p).convert("RGBA")
        if getattr(im, "n_frames", 1) > 1:
            im.seek(0)
    except Exception:  # noqa: BLE001
        return None
    im.thumbnail((max_w, max_h))
    return im


def _wrap(text: str, font: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    lines, cur = [], ""
    for ch in text:
        if ch == "\n":
            lines.append(cur)
            cur = ""
            continue
        t = cur + ch
        if font.getlength(t) > max_w and cur:
            lines.append(cur)
            cur = ch
        else:
            cur = t
    if cur:
        lines.append(cur)
    return lines


def _paste(base: Image.Image, layer: Image.Image, x: int, y: int) -> None:
    base.paste(layer, (x, y), layer)


def _render_sign(base: Image.Image, text: str) -> None:
    d = ImageDraw.Draw(base)
    board = (70, 60, W - 70, 380)
    d.rounded_rectangle(board, radius=26, fill=(255, 255, 255), outline=(120, 130, 160), width=4)
    font = _font(52)
    lines = _wrap(text, font, board[2] - board[0] - 60)[:4]
    total = len(lines) * 66
    y = (board[1] + board[3]) // 2 - total // 2
    for ln in lines:
        w = font.getlength(ln)
        d.text(((W - w) / 2, y), ln, font=font, fill=(40, 46, 70))
        y += 66
    # 举牌的小手
    d.rectangle([(W // 2 - 14, 380), (W // 2 + 14, 470)], fill=(120, 130, 160))
    st = _sticker_layer(360, 300)
    if st:
        _paste(base, st, (W - st.width) // 2, 430)


def _render_think(base: Image.Image, text: str) -> None:
    d = ImageDraw.Draw(base)
    font = _font(44)
    lines = _wrap(text, font, 420)[:3]
    bw = max(font.getlength(x) for x in lines) + 80 if lines else 200
    bh = len(lines) * 58 + 50
    bx, by = 90, 60
    d.rounded_rectangle((bx, by, bx + bw, by + bh), radius=40,
                        fill=(255, 255, 255), outline=(200, 205, 225), width=3)
    y = by + 24
    for ln in lines:
        d.text((bx + 40, y), ln, font=font, fill=(50, 56, 80))
        y += 58
    # 气泡尾巴
    for i, r in enumerate((16, 10, 6)):
        cx, cy = bx + 90 + i * 34, by + bh + 34 + i * 30
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(255, 255, 255),
                  outline=(200, 205, 225), width=2)
    st = _sticker_layer(420, 380)
    if st:
        _paste(base, st, (W - st.width) // 2, H - st.height - 10)


def _render_couplet(base: Image.Image, text: str) -> None:
    d = ImageDraw.Draw(base)
    parts = [p for p in text.replace("｜", "|").split("|") if p.strip()] or [text]
    font = _font(46)
    strips = [(70, 50, 210, H - 60), (W - 210, 50, W - 70, H - 60)]
    used = parts[:2]
    for idx, (strip, txt) in enumerate(zip(strips, used)):
        x0, y0, x1, y1 = strip
        d.rectangle(strip, fill=(196, 40, 46), outline=(140, 20, 26), width=4)
        n = len(txt)
        step = min(58, (y1 - y0 - 40) // max(1, n))
        y = y0 + 24
        for ch in txt:
            w = font.getlength(ch)
            d.text(((x0 + x1 - w) / 2, y), ch, font=font, fill=(255, 240, 220))
            y += step
    st = _sticker_layer(280, 300)
    if st:
        _paste(base, st, (W - st.width) // 2, H - st.height - 60)


def _render_hug(base: Image.Image, text: str) -> None:
    st = _sticker_layer(460, 460)
    if st:
        _paste(base, st, (W - st.width) // 2, 90)
    d = ImageDraw.Draw(base)
    font = _font(46)
    lines = _wrap(text, font, W - 120)[:2]
    y = H - 60 - len(lines) * 58
    for ln in lines:
        w = font.getlength(ln)
        for dx in (-3, 3):
            for dy in (-3, 3):
                d.text(((W - w) / 2 + dx, y + dy), ln, font=font, fill=(255, 255, 255))
        d.text(((W - w) / 2, y), ln, font=font, fill=(30, 36, 60))
        y += 58


_LAST_FILE = ROOT / "memory" / "meme_last.json"


def _remember(tpl: str, base_name: str) -> None:
    try:
        _LAST_FILE.write_text(json.dumps({"template": tpl, "base": base_name},
                                         ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def last() -> dict:
    try:
        return json.loads(_LAST_FILE.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}


TEMPLATES = {
    "sign": _render_sign, "举牌": _render_sign,
    "think": _render_think, "想": _render_think,
    "couplet": _render_couplet, "对联": _render_couplet,
    "hug": _render_hug, "抱": _render_hug,
}


def render(template: str, text: str, base_name: str = "") -> Path:
    """渲染一张表情包，返回本地路径（PNG）。"""
    key = str(template or "sign").strip().lower()
    fn = TEMPLATES.get(key) or TEMPLATES.get(str(template or "").strip()) or _render_sign
    txt = (text or "").strip() or "?"
    base = _bg()
    fn(base, txt)
    _remember(str(template or "sign"), base_name)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"meme_{key or 'sign'}_{abs(hash(txt)) % 10**8}.png"
    base.save(out)
    return out


def render_temp(template: str, text: str) -> str:
    """给机器人用：渲染到临时文件，返回路径字符串。"""
    return str(render(template, text))
