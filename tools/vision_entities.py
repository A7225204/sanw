"""本地视觉实体包（零 token 读屏）。

借鉴 dsh-game-stack 的 cv_* 思路，但只做「感知 + 缓存」，不做自动点击：

- learn(name, box)   从一个区域截图学一个模板（存 memory/vision_packs/）
- scan()             截当前屏幕，用模板匹配找出所有实体 → JSON（0 token）
- match_image(buf)   对一张给定图片做同样的匹配（不需要截屏权限）

和视觉模型的分工：
  视觉模型  看没见过的图（贵、慢、通用）
  实体包    看学过的固定界面（免费、毫秒级、只认学过的）

用法示例：
    from tools.vision_entities import learn, scan
    learn("发送按钮")                 # 学鼠标当前位置周围 80x80
    scan()                            # → [{"name":"发送按钮","box":[...],"score":0.97}]
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACK_DIR = ROOT / "memory" / "vision_packs"
PACK_FILE = PACK_DIR / "packs.json"

# 模板匹配的最低相似度（0~1）
MIN_SCORE = 0.90
# 模板最大边长（太大了匹配慢）
MAX_TEMPLATE = 160


def _load() -> dict:
    try:
        d = json.loads(PACK_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save(d: dict) -> None:
    PACK_DIR.mkdir(parents=True, exist_ok=True)
    PACK_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def _pil():
    try:
        from PIL import Image  # noqa: F401
        return __import__("PIL.Image", fromlist=["Image"])
    except Exception:  # noqa: BLE001
        return None


def _grab_screen():
    """截全屏（Windows）。需要 Pillow + pyautogui 或 PIL ImageGrab。"""
    try:
        from PIL import ImageGrab
        return ImageGrab.grab()
    except Exception:  # noqa: BLE001
        try:
            import pyautogui
            return pyautogui.screenshot()
        except Exception:  # noqa: BLE001
            return None


def learn(name: str, box: tuple[int, int, int, int] | None = None, image=None) -> dict:
    """从一个区域学模板。box=None 时用鼠标周围 120x120（需要 pyautogui）。"""
    Image = _pil()
    if Image is None:
        return {"ok": False, "error": "缺 Pillow"}
    img = image or _grab_screen()
    if img is None:
        return {"ok": False, "error": "截屏失败（缺 PIL ImageGrab / pyautogui）"}
    if box is None:
        try:
            import pyautogui
            x, y = pyautogui.position()
            box = (max(0, x - 60), max(0, y - 60), x + 60, y + 60)
        except Exception:  # noqa: BLE001
            return {"ok": False, "error": "没给 box 且拿不到鼠标位置"}
    crop = img.crop(box).convert("RGB")
    w, h = crop.size
    if max(w, h) > MAX_TEMPLATE:
        sc = MAX_TEMPLATE / float(max(w, h))
        crop = crop.resize((max(1, int(w * sc)), max(1, int(h * sc))), Image.LANCZOS)
    d = _load()
    fname = f"{hashlib.sha1(name.encode()).hexdigest()[:12]}.png"
    PACK_DIR.mkdir(parents=True, exist_ok=True)
    crop.save(PACK_DIR / fname)
    d[name] = {"file": fname, "w": crop.width, "h": crop.height,
               "learned_at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "src_box": list(box)}
    _save(d)
    return {"ok": True, "name": name, "size": [crop.width, crop.height], "packs": len(d)}


def _templates():
    Image = _pil()
    d = _load()
    out = []
    for name, rec in d.items():
        try:
            img = Image.open(PACK_DIR / rec["file"]).convert("L")
            out.append((name, img))
        except Exception:  # noqa: BLE001
            continue
    return out


def match_image(image, min_score: float = MIN_SCORE) -> list[dict]:
    """在一张图里找所有学过的模板。返回 [{name, box, score}]（按分数降序）。

    用 OpenCV 的 matchTemplate（归一化互相关），全屏 2560x1440 上约 50ms；
    之前手写 numpy 滑窗要 32 秒，已弃用。
    """
    if image is None:
        return []
    try:
        import cv2
        import numpy as np
    except Exception:  # noqa: BLE001
        return []
    screen = cv2.cvtColor(np.asarray(image.convert("RGB")), cv2.COLOR_RGB2GRAY)
    sh, sw = screen.shape[:2]
    found: list[dict] = []
    for name, tpl_img in _templates():
        tarr = np.asarray(tpl_img)
        th, tw = tarr.shape[:2]
        if tw >= sw or th >= sh:
            continue
        res = cv2.matchTemplate(screen, tarr, cv2.TM_CCOEFF_NORMED)
        _amin, amax, _minl, maxl = cv2.minMaxLoc(res)
        if amax >= min_score:
            found.append({"name": name, "box": [int(maxl[0]), int(maxl[1]), tw, th],
                          "score": round(float(amax), 3)})
    found.sort(key=lambda r: -r["score"])
    return found


def scan(min_score: float = MIN_SCORE) -> list[dict]:
    """截全屏 → 找出所有学过的实体（0 token）。"""
    img = _grab_screen()
    if img is None:
        return []
    return match_image(img, min_score)


def packs() -> dict:
    return _load()


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 2 and sys.argv[1] == "scan":
        print(json.dumps(scan(), ensure_ascii=False, indent=1))
    else:
        print(json.dumps(packs(), ensure_ascii=False, indent=1))


# ---------------------------------------------------------------- 动作层
def click_at(x: int, y: int, button: str = "left") -> dict:
    """在屏幕坐标点一下（ctypes 调 user32，不需要 pyautogui）。"""
    try:
        import ctypes
        u = ctypes.windll.user32
        u.SetCursorPos(int(x), int(y))
        time.sleep(0.06)
        down, up = (0x02, 0x04) if button == "left" else (0x08, 0x10)
        u.mouse_event(down, 0, 0, 0, 0)
        time.sleep(0.03)
        u.mouse_event(up, 0, 0, 0, 0)
        return {"ok": True, "x": int(x), "y": int(y), "button": button}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def click(name: str, offset: tuple[int, int] = (0, 0), min_score: float = MIN_SCORE) -> dict:
    """找一个学过的实体并点它（默认点中心）。"""
    hits = scan(min_score)
    for h in hits:
        if h["name"] == name:
            x, y, w, hh = h["box"]
            return click_at(x + w // 2 + offset[0], y + hh // 2 + offset[1])
    return {"ok": False, "error": f"屏幕上没找到「{name}」", "seen": [h["name"] for h in hits]}
