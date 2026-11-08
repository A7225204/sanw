"""天气：让她知道今天下雨/热不热。

设计约束
--------
- **不阻塞事件循环**：prompt 构建时只读缓存；真正的请求由 5 分钟 tick 后台刷新。
- **只在话题相关时才注入**：普通闲聊不该突然冒天气（否则又多一层废话）。
- 数据源 wttr.in（免费、免 key），失败就静默留空，不影响聊天。
- 缓存 memory/weather_cache.json，30 分钟有效。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FILE = ROOT / "memory" / "weather_cache.json"

CITY = (os.getenv("WEATHER_CITY") or "").strip()
# wttr.in 对英文城市名解析更稳（中文名可能落到附近地点），查询用它
QUERY_CITY = (os.getenv("WEATHER_QUERY_CITY") or "").strip()
TTL = 1800
TRIGGER_WORDS = ("天气", "下雨", "下雪", "热", "冷", "降温", "升温", "出门", "伞", "晒", "风", "穿什么")


def _read() -> dict:
    try:
        data = json.loads(FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save(data: dict) -> None:
    try:
        FILE.parent.mkdir(parents=True, exist_ok=True)
        FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _pick(desc: str) -> str:
    """把英文天气描述压成中文短词。"""
    d = (desc or "").lower()
    table = [
        ("thunder", "雷阵雨"), ("rain", "雨"), ("snow", "雪"), ("sleet", "雨夹雪"),
        ("fog", "雾"), ("mist", "薄雾"), ("overcast", "阴"), ("cloud", "多云"),
        ("sunny", "晴"), ("clear", "晴"),
    ]
    for k, v in table:
        if k in d:
            return v
    return ""


async def refresh() -> dict:
    """后台刷新天气缓存（由 5 分钟 tick 调用）。失败保持旧缓存。"""
    now = time.time()
    cache = _read()
    if now - float(cache.get("ts") or 0) < TTL:
        return cache
    q = QUERY_CITY or (CITY if CITY else "")
    url = f"https://wttr.in/{q}?format=j1" if q else "https://wttr.in/?format=j1"
    try:
        import httpx
        async with httpx.AsyncClient(timeout=12.0) as cli:
            resp = await cli.get(url, headers={"User-Agent": "curl/8"})
            resp.raise_for_status()
            data = resp.json()
    except Exception:  # noqa: BLE001
        cache["ts"] = now - TTL + 300  # 失败也退避 5 分钟再试
        _save(cache)
        return cache
    try:
        cur = (data.get("current_condition") or [{}])[0]
        today = (data.get("weather") or [{}])[0]
        # 显示名优先用我们自己配的城市：wttr.in 对中文城市名会解析到附近地点
        # （配「深圳」它返回 nearest_area=Ma Tso Lung），直接显示配置值更靠谱
        area = ""
        if not CITY:
            try:
                area = ((data.get("nearest_area") or [{}])[0].get("areaName") or [{}])[0].get("value") or ""
            except Exception:  # noqa: BLE001
                area = ""
        desc = ""
        try:
            desc = _pick((cur.get("weatherDesc") or [{}])[0].get("value") or "")
        except Exception:  # noqa: BLE001
            desc = ""
        out = {
            "ts": now,
            "city": str(CITY or area)[:12],
            "desc": desc,
            "temp": cur.get("temp_C"),
            "min": today.get("mintempC"),
            "max": today.get("maxtempC"),
            "rain": ((today.get("hourly") or [{}])[0].get("chanceofrain") or ""),
        }
        _save(out)
        return out
    except Exception:  # noqa: BLE001
        cache["ts"] = now - TTL + 300
        _save(cache)
        return cache


def weather_line(user_text: str = "") -> str:
    """只在话题相关时返回一行天气；否则空串。只读缓存，不阻塞。"""
    text = str(user_text or "")
    if text and not any(w in text for w in TRIGGER_WORDS):
        return ""
    cache = _read()
    if not cache or time.time() - float(cache.get("ts") or 0) > 4 * 3600:
        return ""
    desc = str(cache.get("desc") or "").strip()
    if not desc:
        return ""
    city = str(cache.get("city") or "").strip()
    mn, mx, t = cache.get("min"), cache.get("max"), cache.get("temp")
    parts = []
    if city:
        parts.append(city)
    parts.append(desc)
    if mn and mx:
        parts.append(f"{mn}~{mx}℃")
    elif t:
        parts.append(f"{t}℃")
    rain = str(cache.get("rain") or "")
    if rain.isdigit() and int(rain) >= 50:
        parts.append(f"可能下雨({rain}%)")
    return "【天气】" + " ".join(parts) + "。被问到或聊到出门时自然带上，别主动播报。"
