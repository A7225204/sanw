#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用火山 Ark 的视觉能力给候选表情包打标签（是否表情包 / 主体 / 情绪 / 水印 / 描述）。

用法：
    python tools/vision_tag.py img1 img2 ...

输出：每行一个 JSON。
"""
from __future__ import annotations

import base64
import io
import json
import sys
from pathlib import Path

import httpx
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"


def _env(key: str) -> str:
    for line in ENV.read_text(encoding="utf-8").splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    return ""


KEY = _env("AI_API_KEY")
BASE = _env("AI_BASE_URL").rstrip("/")
MODEL = _env("AI_MODEL")

PROMPT = """你是表情包素材审核员。看这张图，只输出一个 JSON（不要 markdown、不要解释），字段：
{
  "is_sticker": true/false,        // 是不是聊天用的表情包/梗图（不是摄影作品、海报、插画素材预览、拼图合集）
  "subject": "鲸鱼/鱼/猫/狗/人物/卡通角色/文字/其他",  // 画面主体
  "mood": "开心/无语/生气/卖萌/得意/委屈/震惊/其他",  // 主要情绪
  "has_watermark": true/false,     // 是否有明显商业水印、图库 logo、网址、大段版权文字
  "is_collage": true/false,        // 是否是九宫格/多张拼图
  "caption": "20 字以内中文描述"
}"""


def image_data_url(path: Path) -> str:
    img = Image.open(path)
    if getattr(img, "n_frames", 1) > 1:
        img.seek(0)
    img = img.convert("RGB")
    img.thumbnail((640, 640))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def tag(path: Path) -> dict:
    content = [
        {"type": "text", "text": PROMPT},
        {"type": "image_url", "image_url": {"url": image_data_url(path)}},
    ]
    body = {"model": MODEL, "messages": [{"role": "user", "content": content}], "max_tokens": 900}
    resp = httpx.post(
        BASE + "/chat/completions",
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
        json=body,
        timeout=90,
    )
    payload = resp.json()
    msg = payload["choices"][0]["message"]
    text = (msg.get("content") or "").strip()
    if not text:
        raise RuntimeError("空回复：" + json.dumps(payload)[:200])
    start = text.find("{")
    end = text.rfind("}")
    data = json.loads(text[start:end + 1])
    data["file"] = str(path)
    return data


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        p = Path(arg)
        try:
            print(json.dumps(tag(p), ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"file": str(p), "error": str(exc)}, ensure_ascii=False))
