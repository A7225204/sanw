"""能量体征（2026-11-05，借 digital-life-core 的 energy 0–100）。

以前她烧 token 没有任何代价感 —— 今天 488 万 token、2.5 元，她本人毫不知情。
这里给她一条**自己的体力条**：

    每聊一轮        -0.50
    醒着自然回血    +0.05/分钟（3/小时）
    睡觉回血        +0.12/分钟（7.2/小时）

按这个速率：**连聊一整天大约掉 20 点**。连着一周高强度说话才会明显累。

四档行为（由 prompt_line 注入，越到后面越不爱动）：

    ≥60  正常
    40-59 有点累：话短一些、少追问
    20-39 很累：只回必要的，不主动找话题
    <20   累瘫：能不开口就不开口

设计上刻意**不做死亡**：它那边 energy 跌破 10 留疤，我们这里只降行为强度，
不至于因为体力低就彻底不回话（那是故障，不是设定）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILE = ROOT / "memory" / "energy_state.json"

MAX_ENERGY = 100.0
START_ENERGY = 85.0          # 首次启用时给 85，别一上来就满格

DRAIN_PER_TURN = 0.50        # 每轮聊天扣多少
REGEN_AWAKE_PER_MIN = 0.05
REGEN_SLEEP_PER_MIN = 0.12

_FALLBACK = {"value": START_ENERGY, "updated_ts": 0.0, "date": ""}


def _now() -> float:
    return time.time()


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _load() -> dict:
    try:
        d = json.loads(FILE.read_text(encoding="utf-8-sig"))
        if isinstance(d, dict) and "value" in d:
            return d
    except Exception:  # noqa: BLE001
        pass
    # 文件不存在/写坏 → 落地一份出厂值，面板才有东西可读
    d = dict(_FALLBACK)
    d["updated_ts"] = _now()
    _save(d)
    return d


def _save(d: dict) -> None:
    try:
        FILE.parent.mkdir(parents=True, exist_ok=True)
        FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _clamp(v: float) -> float:
    return max(0.0, min(MAX_ENERGY, float(v)))


def value() -> float:
    """当前能量（会顺带按流逝时间回一次血）。"""
    d = _load()
    now = _now()
    last = float(d.get("updated_ts") or 0)
    if last > 0 and now > last:
        mins = min((now - last) / 60.0, 24 * 60.0)   # 最多补一天，避免久关后暴满
        sleeping = False
        try:
            from plugins import hormones as _h
            sleeping = float(_h.values().get("MEL", 0)) >= 60
        except Exception:  # noqa: BLE001
            pass
        rate = REGEN_SLEEP_PER_MIN if sleeping else REGEN_AWAKE_PER_MIN
        d["value"] = _clamp(float(d.get("value") or 0) + mins * rate)
        d["updated_ts"] = now
        _save(d)
    return float(d.get("value") or 0)


def drain(amount: float = DRAIN_PER_TURN) -> float:
    """扣能量，返回扣完的值。"""
    d = _load()
    d["value"] = _clamp(float(d.get("value") or 0) - float(amount))
    d["updated_ts"] = _now()
    d["date"] = _today()
    d["turns"] = int(d.get("turns") or 0) + 1
    _save(d)
    return float(d["value"])


def set_value(v: float) -> float:
    d = _load()
    d["value"] = _clamp(v)
    d["updated_ts"] = _now()
    _save(d)
    return float(d["value"])


def level() -> str:
    """档位：ok / tired / very_tired / exhausted"""
    v = value()
    if v >= 60:
        return "ok"
    if v >= 40:
        return "tired"
    if v >= 20:
        return "very_tired"
    return "exhausted"


def prompt_line() -> str:
    """给 prompt 用的体力描述（每轮可能变，调用方放进 volatile）。"""
    v = round(value(), 1)
    lv = level()
    if lv == "ok":
        return ""
    if lv == "tired":
        return (f"【体力 {v:.0f}/100 · 有点乏】话短一点，别追问、别主动开新话题；"
                f"回一两句就好。")
    if lv == "very_tired":
        return (f"【体力 {v:.0f}/100 · 很累】只回必要的，能一句说完就别说两句；"
                f"不要起新话头，不要撒娇耍赖。")
    return (f"【体力 {v:.0f}/100 · 快没电了】能不开口就不开口；"
            f"真要回就极短，别解释、别铺垫。")


def allow_proactive() -> bool:
    """累了就别主动找人了（主动私聊前置检查）。"""
    return value() >= 45


def stats() -> dict:
    d = _load()
    return {"value": round(value(), 1), "level": level(),
            "turns": int(d.get("turns") or 0), "date": d.get("date") or ""}
