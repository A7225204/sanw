# -*- coding: utf-8 -*-
"""Pixiv 收图 → 大肥鱼：识图分级 + 三路分发。

    python tools/sync_pixiv_to_fish.py            # 处理新图
    python tools/sync_pixiv_to_fish.py --dry      # 只看会怎么分，不落盘
    python tools/sync_pixiv_to_fish.py --force    # 重新处理已处理过的

三路分发：
  · 表情包（safe + 像表情包）→ D:\\qq-ai-bot\\表情包\\<mood>\\
  · 大肥鱼/鲸鱼娘角色图      → ~/.dsh/image-studio/library/characters/<画师>_<id>\\
  · R-18 / 擦边             → 留在 pixiv 库，只打分级标记（发图时按级别放行）

索引：memory/pixiv_sync.json（已处理 id / 分级 / 描述 / 去向）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PIXIV_LIB = Path(os.path.expanduser("~")) / ".dsh" / "pixiv-duty" / "library"
FISH_EMOJI = Path("D:/qq-ai-bot/表情包")
IMAGES = Path(os.path.expanduser("~")) / ".dsh" / "image-studio" / "library"
INDEX = ROOT / "memory" / "pixiv_sync.json"

MOODS = ("卖萌", "开心", "生气", "无语", "撒娇", "害羞", "困倦", "得意")

VISION_PROMPT = """看这张图，只输出 JSON：
{
  "desc": "40字内画面描述",
  "is_fish": true/false（是不是蓝色大肥鱼/鲸鱼娘/蓝鱼拟人）,
  "is_sticker": true/false（适不适合当聊天表情包：Q版/梗图/反应图/带字）,
  "mood": "卖萌/开心/生气/无语/撒娇/害羞/困倦/得意 里选一个最贴的",
  "level": "safe/suggestive/r18"（safe=正常；suggestive=擦边露肤；r18=明确成人内容）
}"""


def _env() -> dict:
    try:
        from server import memory_growth as mg
        return mg._env()
    except Exception:  # noqa: BLE001
        return {}


ENV = _env()
KEY = str(ENV.get("SILICONFLOW_API_KEY") or "")
BASE = "https://api.siliconflow.cn/v1"
VMODEL = "Qwen/Qwen3-VL-32B-Instruct"


def _index() -> dict:
    try:
        return json.loads(INDEX.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"items": {}}


def _save_index(d: dict) -> None:
    INDEX.parent.mkdir(parents=True, exist_ok=True)
    INDEX.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def _iter_images() -> list[tuple[Path, str, str]]:
    """返回 (图片路径, 作品目录名, 分类)。分类：插画 / 漫画 / R-18。"""
    out: list[tuple[Path, str, str]] = []
    for kind_dir in ("插画", "漫画", "R-18", "小说"):
        base = PIXIV_LIB / kind_dir
        if not base.exists():
            continue
        # 2026-11-05 修：原来写死三层 base/画师/作品/文件，
        # 而 R-18 的实际结构是 R-18/插画/画师/作品/文件（深一层，192 张全部漏扫）。
        # 改成递归扫，任意深度都吃。
        for f in base.rglob("*"):
            try:
                if not f.is_file():
                    continue
            except OSError:
                continue
            if f.suffix.lower() in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                out.append((f, f.parent.name, kind_dir))
    return out


def _vision(img: Path) -> dict:
    b = img.read_bytes()
    ext = img.suffix.lower().lstrip(".")
    if ext == "jpg":
        ext = "jpeg"
    data = "data:image/" + ext + ";base64," + __import__("base64").b64encode(b).decode()
    body = {
        "model": VMODEL,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": data}},
            {"type": "text", "text": VISION_PROMPT},
        ]}],
    }
    r = httpx.post(BASE + "/chat/completions",
                   headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
                   json=body, timeout=180)
    if r.status_code != 200:
        return {"error": r.text[:120]}
    txt = ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    s = txt.find("{")
    e = txt.rfind("}")
    try:
        return json.loads(txt[s:e + 1]) if s >= 0 and e > s else {"error": "no json"}
    except Exception:  # noqa: BLE001
        return {"error": "bad json"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--limit", type=int, default=40)
    a = ap.parse_args()

    idx = _index()
    imgs = _iter_images()
    todo = [x for x in imgs if a.force or str(x[0]) not in idx["items"]][: a.limit]
    print(f"库存 {len(imgs)} 张，本次处理 {len(todo)} 张", flush=True)
    if not todo:
        return

    stats = {"emoji": 0, "character": 0, "r18": 0, "skip": 0, "error": 0}
    for i, (p, work, kind) in enumerate(todo, 1):
        key = str(p)
        try:
            # 2026-11-05：R-18 目录的图，识图接口一律拒（50507），等超时白耗几分钟。
            # 直接按目录定级，既快又准（这批本来就不该靠模型判断）。
            if kind == "R-18":
                info = {"level": "r18", "desc": "R-18 目录直判（跳过识图）",
                        "is_fish": False, "is_sticker": False, "mood": "卖萌"}
            else:
                info = _vision(p)
        except Exception as e:          # 单张超时/断连不能打断整批（2026-11-04）
            print(f"[{i}] {p.name} 识图异常：{type(e).__name__}", flush=True)
            stats["error"] += 1
            continue
        if info.get("error"):
            # 识图失败（常见：接口对擦边/18+ 直接 50507）→ 按所在目录兜底分级，别让整批卡死
            _sp = str(p).replace("\\", "/")
            if "/R-18/" in _sp:
                _lv = "r18"
            elif "/漫画/" in _sp:
                _lv = "suggestive"
            else:
                _lv = "safe"
            print(f"[{i}] {p.name} 识图失败，按目录兜底 {_lv}：{str(info['error'])[:60]}", flush=True)
            idx["items"][key] = {"level": _lv, "desc": "识图失败·按目录判定",
                                 "is_fish": False, "is_sticker": False,
                                 "mood": "卖萌", "dest": "skip",
                                 "at": time.strftime("%Y-%m-%d %H:%M")}
            if not a.dry:
                _save_index(idx)
            stats["skip"] += 1
            continue
        level = str(info.get("level") or "safe")
        desc = str(info.get("desc") or "")
        is_fish = bool(info.get("is_fish"))
        is_sticker = bool(info.get("is_sticker"))
        mood = str(info.get("mood") or "卖萌")
        if mood not in MOODS:
            mood = "卖萌"

        dest = ""
        if kind == "R-18" or level == "r18":
            stats["r18"] += 1
            dest = "r18"
        elif is_sticker and level == "safe":
            # 表情包：拷进图库（按 hash 命名，防重）
            md5 = hashlib.md5(p.read_bytes()).hexdigest()[:10]
            target = FISH_EMOJI / mood
            target.mkdir(parents=True, exist_ok=True)
            out = target / f"pixiv_{md5}{p.suffix.lower()}"
            if not a.dry:
                shutil.copy2(p, out)
            stats["emoji"] += 1
            dest = f"表情包/{mood}"
        elif is_fish:
            # 角色图：进生图参考库
            md5 = hashlib.md5(p.read_bytes()).hexdigest()[:10]
            target = IMAGES / "characters" / f"pixiv_{work}"
            target.mkdir(parents=True, exist_ok=True)
            out = target / f"{md5}{p.suffix.lower()}"
            if not a.dry:
                shutil.copy2(p, out)
            stats["character"] += 1
            dest = f"characters/pixiv_{work}"
        else:
            stats["skip"] += 1
            dest = "skip"

        idx["items"][key] = {
            "level": level, "desc": desc, "is_fish": is_fish,
            "is_sticker": is_sticker, "mood": mood, "dest": dest,
            "at": time.strftime("%Y-%m-%d %H:%M"),
        }
        print(f"[{i}] {p.name} → {dest}（{level}，{desc[:20]}）", flush=True)
        if not a.dry:
            _save_index(idx)

    if not a.dry:
        _save_index(idx)
        # 新表情包补向量（发图时语义检索用，避免重新识图）
        try:
            from plugins import sticker_vec
            r = sticker_vec.build()
            print("向量索引：", r, flush=True)
        except Exception as e:  # noqa: BLE001
            print("向量索引构建失败：", type(e).__name__, e, flush=True)
    print("完成：", stats, flush=True)


if __name__ == "__main__":
    main()
