"""情绪的"余波"：离散情绪带残留，按半衰期衰减。

来源：LIA-Assistant Psyche Engine（ADR-068）的分层设计 ——
它把情绪独立成一层（分钟级，**指数衰减**），和心情（小时级）、人格（永久）分开。

我们的问题：`derive_emotions()` 是**即时算**的 ——
激素一 tick 回来，上一轮那口气就"消失"了。
被狠狠骂一句，下一秒就恢复如常，这不是人。

做法：
- 事件发生时给对应情绪**充能**（写进 memory/emotion_residue.json）
- 每次读的时候按半衰期衰减
- 最终情绪 = 即时算的 + 残留（上限 100）

半衰期不是拍脑袋，按心理学常识：
    怒    30 分钟（气得最久）
    丧    40 分钟（低落最难爬出来）
    害怕  20 分钟
    烦    18 分钟
    开心  15 分钟
    害羞   8 分钟（一会儿就过去了）
"""
from __future__ import annotations

import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILE = ROOT / "memory" / "emotion_residue.json"

# 半衰期（分钟）
HALF_LIFE = {
    "怒": 30.0,
    "丧": 40.0,
    "害怕": 20.0,
    "烦": 18.0,
    "开心": 15.0,
    "害羞": 8.0,
    "情欲": 25.0,
    "累": 90.0,     # 累不是情绪是体力，但这里也给个长尾
}

# 事件 → 情绪充能。激素那边有 EVENTS（5 维），这里是 7 维情绪的对应版本。
# 只写"这一下会让什么情绪上来"，数值是强度（0~100 的加分）。
EMO_EVENTS: dict[str, dict[str, float]] = {
    # 被骂 / 被凶：怒上来了，但也会丧
    "insult":     {"怒": 30, "丧": 12, "烦": 10},
    # 被夸：开心，还害臊
    # 2026-11-07 修：被夸要能消气。
    # 原来 praise 只加开心和害羞，结果实测「被骂 3 次 → 怒 100；
    # 接着被夸 3 次，怒还是 99」—— 余波只涨不消，这不合理。
    "praise":     {"开心": 25, "害羞": 18, "怒": -28, "丧": -12, "烦": -18},
    # 被无视 / 没人接：丧
    "ignored":    {"丧": 16, "烦": 8},
    # 被主人凶：害怕比怒更重（对被在意的人）
    "scolded":    {"害怕": 22, "丧": 15, "怒": 8},
    # 亲密 / 撩：开心 + 害羞 + 情欲
    "intimate":   {"情欲": 20, "害羞": 15, "开心": 10, "怒": -20, "烦": -15},
    "aroused":    {"情欲": 28, "害羞": 8},
    # 干活成功：开心
    "art_success": {"开心": 20},
    # 被安慰 / 和好：丧和怒都被压下去（负值表示削减）
    "repair":     {"丧": -15, "怒": -20, "烦": -10},
    # 喝咖啡：开心一点，累压一点
    "coffee":     {"开心": 8, "累": -12},
    # 吃饭 / 睡觉：累下降
    "meal":       {"累": -10, "开心": 5, "怒": -12, "烦": -15},
    "sleep":      {"累": -35, "丧": -8, "怒": -40, "烦": -45},   # 睡一觉气就消了大半
}


def _load() -> dict:
    try:
        d = json.loads(FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save(d: dict) -> None:
    try:
        FILE.parent.mkdir(parents=True, exist_ok=True)
        FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _decayed(d: dict, now: float) -> dict:
    """把每条残留按半衰期衰减到 now。返回新的 {情绪: 强度}。"""
    out: dict[str, float] = {}
    for k, v in (d or {}).items():
        if not isinstance(v, dict):
            continue
        val = float(v.get("v") or 0)
        at = float(v.get("at") or now)
        hl = float(HALF_LIFE.get(k, 20.0)) * 60.0
        dt = max(0.0, now - at)
        if hl > 0:
            val = val * (0.5 ** (dt / hl))
        if val >= 1.0:                      # 低于 1 就当散了
            out[k] = round(val, 2)
    return out


# 2026-11-07：同一件事，不同的人反应不一样。
# 上面 EMO_EVENTS 是"标称值"，真正充多少要按三我调 ——
# 本我强的人被骂当场就炸（怒多）；超我严的人会忍（怒少、丧多、怕多）。
# 这就是主人说的「按底层激素-情绪-三我来」，不是写死一个数。
_OUTWARD = ("怒", "烦")          # 往外放的
_INWARD = ("丧", "害怕")         # 往里收的


def _ego_modifier() -> dict[str, float]:
    """按三我势力算每种情绪的倍率。拿不到状态就返回全 1（不调）。"""
    try:
        from server import soul_aggregation as sa
        f = sa.view().get("forces") or {}
        idf = float(f.get("本我") or 0.4)
        sef = float(f.get("超我") or 0.4)
    except Exception:  # noqa: BLE001
        return {}
    mod: dict[str, float] = {}
    # 本我 0.4 → 外放倍率 ≈1.0；0.8 → ≈1.5；0.2 → ≈0.74
    out_mul = max(0.5, min(1.8, 0.5 + idf * 1.25))
    # 超我 0.4 → 内收倍率 ≈1.0；0.8 → ≈1.5
    in_mul = max(0.5, min(1.8, 0.5 + sef * 1.25))
    for k in _OUTWARD:
        mod[k] = out_mul
    for k in _INWARD:
        mod[k] = in_mul
    return mod


def fire(event: str) -> dict:
    """某个事件发生 → 给对应情绪充能。返回充能后的残留表。"""
    add = EMO_EVENTS.get(str(event or ""))
    if not add:
        return current()
    # 2026-11-07：顺手喂给阿德勒的「自卑 → 补偿」。
    # 阿德勒说自卑感来自「感到不足」，被否定和被无视是最直接的两类。
    # 节流：同一个事件一天最多记 3 次，否则被刷屏会把她压成讨好型。
    try:
        from plugins import adler as _ad
        _k = {"insult": "被否定", "ignored": "被无视"}.get(str(event))
        if _k:
            _ad.hit_throttled(_k)
    except Exception:  # noqa: BLE001
        pass
    mod = _ego_modifier()
    now = time.time()
    d = _decayed(_load(), now)
    for emo, delta in add.items():
        # 三我调：外放/内收各乘各的倍率（正负都乘，削减也按性格调）
        delta = delta * mod.get(emo, 1.0)
        base = float(d.get(emo) or 0)
        # 负值（安抚）走乘法削减，避免把已经很低的值变成负数
        val = base + delta if delta >= 0 else base * (1 + delta / 100.0)
        val = max(0.0, min(100.0, val))
        if val >= 1.0:
            d[emo] = round(val, 2)
        else:
            d.pop(emo, None)
    _save({k: {"v": v, "at": now} for k, v in d.items()})
    return d


def current() -> dict:
    """当前残留强度（已按半衰期衰减）。"""
    return _decayed(_load(), time.time())


def apply(emo: dict[str, float]) -> dict[str, float]:
    """把残留叠加到即时算出来的情绪上（上限 100）。

    残留是"上一轮还没消的气"，即时值是"这一秒的样子"。
    两者取 max 再加一点余量 —— 不能简单相加，否则吵一句就爆表。
    """
    res = current()
    if not res:
        return emo
    out = dict(emo or {})
    for k, v in res.items():
        base = float(out.get(k) or 0)
        # 残留和即时取较大者，再补 30% 的另一方（两者都在时更强烈）
        out[k] = round(min(100.0, max(base, v) + 0.3 * min(base, v)), 1)
    return out


def line() -> str:
    """给 promat 的一行：还有哪些情绪没散。空串表示都散了。"""
    res = current()
    if not res:
        return ""
    items = sorted(res.items(), key=lambda kv: -kv[1])[:3]
    if not items or items[0][1] < 12:
        return ""
    return "、".join(f"{k}还没散" for k, v in items)
