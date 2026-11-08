"""Google Gemini 免费额度通道：识图 / 出图 / 文本（失败由调用方回退火山）。"""
from __future__ import annotations

import asyncio
import base64
import json
import os
from typing import Any

import httpx

API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
TIMEOUT = float(os.getenv("GEMINI_TIMEOUT", "45") or "45")
VISION_MODEL = os.getenv("GEMINI_VISION_MODEL") or os.getenv("GEMINI_CHAT_MODEL") or "gemini-2.5-flash"
IMAGE_MODEL = os.getenv("GEMINI_IMAGE_MODEL") or "gemini-2.5-flash-image"


def _key() -> str:
    return (os.getenv("GEMINI_API_KEY") or "").strip()


# 最近一次失败原因（"no-key" / HTTP 状态 / 异常摘要）。原实现把所有错误都吞了，
# 调用方无法区分"没网"和"额度用尽"，这里补上，便于决定要不要停用谷歌通道。
LAST_ERROR = ""


async def _post(model: str, payload: dict[str, Any], timeout: float | None = None) -> dict[str, Any] | None:
    global LAST_ERROR
    key = _key()
    if not key:
        LAST_ERROR = "no-key"
        return None
    url = f"{API_BASE}/{model}:generateContent?key={key}"
    try:
        async with httpx.AsyncClient(timeout=timeout or TIMEOUT) as client:
            r = await client.post(url, json=payload)
            try:
                data = r.json()
            except Exception:  # noqa: BLE001
                LAST_ERROR = f"http-{r.status_code}-nonjson"
                return None
        if isinstance(data, dict) and data.get("candidates"):
            LAST_ERROR = ""
            return data
        LAST_ERROR = (json.dumps(data, ensure_ascii=False)
                      if isinstance(data, dict) else "no-candidates")[:300]
        return None
    except Exception as e:  # noqa: BLE001
        LAST_ERROR = f"{type(e).__name__}: {e}"[:300]
        return None


def _parts_text(data: dict[str, Any]) -> str:
    try:
        parts = data["candidates"][0]["content"]["parts"]
    except Exception:
        return ""
    out = []
    for p in parts:
        if isinstance(p, dict) and p.get("text"):
            out.append(str(p["text"]))
    return "\n".join(out).strip()


async def gemini_text(prompt: str, model: str | None = None) -> str:
    data = await _post(model or VISION_MODEL, {"contents": [{"parts": [{"text": prompt}]}]})
    return _parts_text(data) if data else ""


async def gemini_search(prompt: str, model: str | None = None) -> str:
    data = await _post(
        model or VISION_MODEL,
        {"contents": [{"parts": [{"text": prompt}]}], "tools": [{"google_search": {}}]},
        timeout=60,
    )
    return _parts_text(data) if data else ""


async def gemini_vision(prompt: str, image: bytes, mime: str = "image/jpeg",
                        model: str | None = None) -> str:
    b64 = base64.b64encode(image).decode()
    payload = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": mime, "data": b64}},
            ]
        }]
    }
    data = await _post(model or VISION_MODEL, payload)
    return _parts_text(data) if data else ""


async def gemini_image(prompt: str, reference: bytes | None = None,
                       mime: str = "image/jpeg") -> bytes | None:
    parts: list[dict[str, Any]] = [{"text": prompt}]
    if reference:
        parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(reference).decode()}})
    data = await _post(IMAGE_MODEL, {"contents": [{"parts": parts}]}, timeout=90)
    if not data:
        return None
    try:
        for p in data["candidates"][0]["content"]["parts"]:
            inline = p.get("inline_data") or p.get("inlineData")
            if inline and inline.get("data"):
                return base64.b64decode(inline["data"])
    except Exception:
        pass
    return None
