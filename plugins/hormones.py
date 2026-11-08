"""内分泌层：5 个激素量驱动情绪 / 主动性 / 语气。

设计见 docs/内分泌系统-方案A.md。

关键点
------
· 只维护 5 个量：DA 多巴胺 / CORT 皮质醇 / SER 血清素 / OXY 催产素 / MEL 褪黑素
· 7 维情绪降级为派生量（公式固定），对外接口保持不变
· 全部落盘，按"距上次 tick 的时长"补算（关机期间也在变）
· 不调模型、不花 token
"""
from __future__ import annotations

import json
import math
import os
import random
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILE = ROOT / "memory" / "hormones_state.json"
HIST_FILE = ROOT / "memory" / "emotion_history.json"
HIST_MAX = 720  # 12 小时（每分钟 1 点）

ENABLED = os.getenv("HORMONES_ENABLED", "1").lower() in ("1", "true", "yes")

# 名称 / 基线 / 半衰期（分钟）
HORMONES = ["DA", "CORT", "SER", "OXY", "MEL"]

# ===========================================================================
# 通用化学池（2026-11-05，借 digital-life-core 的 genome.json）
#
# 基线、半衰期、温度权重全部搬到 memory/genome.json —— 调性格不用改代码。
# 文件缺失/写坏时回落到下面这份出厂值，保证激素系统永远能起来。
# ===========================================================================
GENOME_FILE = ROOT / "memory" / "genome.json"

_FALLBACK_GENOME = {
    "hormones": {
        "DA":   {"base": 40.0, "half_life_min": 25.0},
        "CORT": {"base": 20.0, "half_life_min": 120.0},
        "SER":  {"base": 55.0, "half_life_min": 240.0},
        "OXY":  {"base": 35.0, "half_life_min": 2880.0},
        "MEL":  {"base": 15.0, "half_life_min": 60.0},
    },
    "temperature": {"cort": 0.60, "da": 0.25, "ser": -0.30, "mel": -0.20,
                    "min": -0.35, "max": 0.35},
}


def _load_genome() -> dict:
    try:
        d = json.loads(GENOME_FILE.read_text(encoding="utf-8-sig"))
        if isinstance(d, dict) and isinstance(d.get("hormones"), dict) and d["hormones"]:
            return d
    except Exception:  # noqa: BLE001
        pass
    return _FALLBACK_GENOME


GENOME = _load_genome()
BASE = {k: float((GENOME["hormones"].get(k) or {}).get("base", 40.0)) for k in HORMONES}
# OXY（依恋/亲密）是关系存量，不是情绪：回落要慢，否则 "聊一下午很黏、睡一觉回原样"。
HALF_LIFE_MIN = {k: float((GENOME["hormones"].get(k) or {}).get("half_life_min", 60.0))
                 for k in HORMONES}

# 热度折扣：同类事件在窗口内重复发生，正向增益按 1/(1+n/HEAT_DIVISOR) 缩水。
# 只压正向；责骂/压力是代价，照旧累加。
HEAT_WINDOW_SEC = float(os.getenv("HORMONES_HEAT_WINDOW_SEC", "600") or "600")
HEAT_DIVISOR = float(os.getenv("HORMONES_HEAT_DIVISOR", "12") or "12")

# 每日正向增量上限（防一天刷爆）。CORT/MEL 是代价维度，不设。
DAILY_CAP = {
    "DA": float(os.getenv("HORMONES_CAP_DA", "40") or "40"),
    "SER": float(os.getenv("HORMONES_CAP_SER", "30") or "30"),
    "OXY": float(os.getenv("HORMONES_CAP_OXY", "40") or "40"),
}

# 只有这三个维度算"对角色有利"的增益；CORT 涨=压力、MEL 涨=困，都是代价，不打折不限额
GAIN_DIMS = {"DA", "SER", "OXY"}

# 亲密事件不跟日常聊天抢额度（2026-11-05）：DA 白天聊几句就用满 40，
# 到了晚上真想聊点什么，一点也涨不上去——那是额度分配错误，不是她冷淡。
INTIMATE_EVENTS = {"intimate", "aroused"}
INTIMATE_CAP = {"DA": 25.0, "SER": 10.0, "OXY": 20.0}

# 交叉影响：每 tick 按 (源偏基线比例) 微调目标
COUPLING = [
    ("SER", "CORT", -0.06),
    ("DA", "CORT", -0.04),
    ("CORT", "OXY", -0.04),
    ("MEL", "DA", -0.05),
]

# 事件增量：(DA, CORT, SER, OXY, MEL)
# 事件增量：原来的值（OXY+12/DA+6）远大于半衰期回落，
# 聊几轮就把 SER/OXY 顶到 100、失去区分度。这里整体调小。
EVENTS = {
    "owner_message": (4, -2, 1, 7, 0),   # 2026-11-04：原来 -10/条，聊几十句就把 CORT 抽到 0
    "group_interact": (1, 0, 1, 1, 0),   # 闲聊不算压力（2026-11-04 修）
    "insult": (-3, 12, -4, -3, 0),
    "praise": (6, -4, 3, 2, 0),
    "meal": (2, -3, 5, 3, 0),
    "sleep": (-5, -8, 5, 0, 20),
    "wake": (4, -2, 2, 0, -20),
    "art_success": (8, -2, 2, 2, 0),
    "repair": (2, -6, 5, 3, 0),
    # 亲密（2026-11-05 加）：专门喂给「情欲」这一维。
    # MEL 是负的——被撩起来的时候不该还犯困，困意会被压下去。
    "intimate": (5, -3, 2, 6, -2),   # 撩 / 调情 / 被撩到
    "aroused": (8, 2, 0, 4, -4),      # 情动：多巴胺冲高，CORT 微升（那点心跳感）
    # 2026-11-06：主人要求加「/喝咖啡」提神。
    # DA 拉高（精神起来）、CORT 压一点（别那么绷）、MEL 大降（不困了）。
    "coffee": (12, -4, 3, 1, -6),
}

# 安抚类事件（降皮质醇时留地板 12）
# 2026-11-05：intimate 也要进来 —— 它的 CORT 是 -3，不设地板的话连撩几句就把皮质醇抽干，
# 触发"异常值域"哨兵并让下次 _load 自愈成 4.0，反而更糟
_COMFORT_EVENTS = {"owner_message", "praise", "meal", "repair", "art_success", "wake", "intimate", "coffee"}


_state: dict | None = None
_mtime = 0.0
_heat: dict[str, list[float]] = {}   # 事件名 -> 窗口内的触发时间戳（内存，不落盘）
_daily: dict[str, float] = {}        # 激素 -> 当天已用的正向增量
_daily_date: str = ""                # 额度所属日期


def _now() -> float:
    return time.time()


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _roll_daily() -> None:
    """跨天清零每日正向额度。"""
    global _daily, _daily_date
    d = _today()
    if _daily_date != d:
        _daily_date = d
        _daily = {}


def _load() -> dict:
    global _state, _mtime, _daily, _daily_date
    try:
        m = FILE.stat().st_mtime
    except Exception:  # noqa: BLE001
        m = 0.0
    if _state is None or (m and m != _mtime):
        try:
            data = json.loads(FILE.read_text(encoding="utf-8-sig"))
        except Exception:  # noqa: BLE001
            data = {}
        vals = data.get("values") or {}
        _state = {k: float(vals.get(k, BASE[k])) for k in HORMONES}
        # 自愈：正常都是 0~100；出现「一个 <1、别的 >10」= 被写进了 0~1 的归一化值，
        # 直接把小值 ×100 拉回量纲（2026-11-04 连续两次 CORT 被写成 0.0x）
        try:
            _small = [k for k in HORMONES if _state[k] < 1.0]
            _big = [k for k in HORMONES if _state[k] > 10.0]
            if _small and _big:
                # 不是量纲错，是被负向事件抽干了：扶回一个保底值（CORT 5 / 其它 10）
                for k in _small:
                    _state[k] = 5.0 if k == "CORT" else 10.0
                print(f"[hormones][自愈] 值域异常 {_small} 被抽干，扶回地板 → "
                      + str({k: _state[k] for k in HORMONES}), flush=True)
        except Exception:  # noqa: BLE001
            pass
        _state["_ts"] = float(data.get("updated_ts") or _now())
        _daily = {k: float(v) for k, v in (data.get("daily_gain") or {}).items()
                  if k in HORMONES or k.endswith("_int")}
        _daily_date = str(data.get("daily_date") or "")
        _mtime = m
    return _state


def _save() -> None:
    global _mtime
    s = _load()
    # 值域哨兵（2026-11-04）：四个维度正常都在 0~100，出现「一个 <1、别的 >10」
    # 就是有路径把 0~1 的归一化值写回来了，打栈方便定位
    try:
        _v = {k: float(s[k]) for k in HORMONES}
        if any(x < 1.0 for x in _v.values()) and any(x > 10.0 for x in _v.values()):
            import traceback as _tb
            print("[hormones][异常值域] " + str(_v) + chr(10) + chr(10).join(_tb.format_stack()[-6:]), flush=True)
    except Exception:  # noqa: BLE001
        pass
    try:
        FILE.parent.mkdir(parents=True, exist_ok=True)
        FILE.write_text(json.dumps({
            "values": {k: round(s[k], 2) for k in HORMONES},
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "updated_ts": s.get("_ts", _now()),
            "daily_date": _daily_date,
            "daily_gain": {k: round(v, 2) for k, v in _daily.items()},
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        _mtime = FILE.stat().st_mtime
    except Exception:  # noqa: BLE001
        pass


def _clamp(v: float) -> float:
    return max(0.0, min(100.0, v))


def _decay_toward_base(s: dict, minutes: float) -> None:
    for k in HORMONES:
        hl = HALF_LIFE_MIN[k]
        if minutes > 0 and hl > 0:
            factor = 0.5 ** (minutes / hl)
            s[k] = BASE[k] + (s[k] - BASE[k]) * factor


_ticking = False


def tick(minutes: float | None = None) -> dict:
    """按经过时间推进激素。minutes=None 时按上次 tick 自动算。"""
    global _ticking
    # 防重入：_append_history → derive_emotions → values → tick 会绕回来
    if _ticking:
        return {k: _load()[k] for k in HORMONES}
    if not ENABLED:
        return dict(_load())
    _ticking = True
    try:
        s = _load()
        now = _now()
        dt_min = minutes if minutes is not None else max(0.0, (now - float(s.get("_ts") or now)) / 60.0)
        dt_min = min(dt_min, 60 * 24 * 7)  # 关机很久也只按一周补
        if dt_min <= 0:
            return {k: s[k] for k in HORMONES}

        _decay_toward_base(s, dt_min)

        # 交叉影响：源偏基线越多，对目标影响越大
        # ⚠️ 2026-11-04 修：原来用完整 dt_min 线性外推，机器人关机一夜后 dt 会是几千分钟，
        #    SER→CORT(-0.06) 一次能算出 -200，把 CORT 直接打到 0（实测 45 → 0.12）。
        #    这里限制耦合只按最多 30 分钟算，且单次最多 ±5，避免"久别重逢就崩溃"。
        _dt_c = min(dt_min, 30.0)
        for src, dst, coef in COUPLING:
            dev = (s[src] - BASE[src]) / 100.0
            _d = dev * coef * 100.0 * (_dt_c / 60.0)
            _d = max(-5.0, min(5.0, _d))
            s[dst] = _clamp(s[dst] + _d)

        # 昼夜：深夜褪黑素涨、动力降
        hour = time.localtime(now).tm_hour
        night = hour >= 23 or hour < 6
        if night:
            s["MEL"] = _clamp(s["MEL"] + 1.2 * dt_min)
            s["DA"] = _clamp(s["DA"] - 0.10 * dt_min)

        s["_ts"] = now
        for k in HORMONES:
            s[k] = _clamp(s[k])
        _save()
        _append_history()
        return {k: s[k] for k in HORMONES}
    finally:
        _ticking = False


def appraise(name: str, ctx: dict | None = None) -> dict:
    """OCC 式认知评价（2026-11-08 移植自 BioCore 的思路）。

    事件不再直接改激素，先过四维评价：
      relevance  相关性  —— 这事跟我有关吗（0~1）
      valence    利害    —— 好还是坏（0=坏 1=好）
      coping     应对    —— 我扛得住吗（0~1，由血清素/多巴胺/皮质醇推）
      agency     归因    —— 怪自己还是怪外面（0=自己 1=外部）

    评价结果用来修正激素变化量：
      · 相关性低 → 效果打折（别人的事，跟我没关系）
      · 坏事件 + 能应对 → 伤害减半；坏事件 + 扛不住 → 伤害加剧
    """
    ctx = ctx or {}
    base = EVENTS.get(name) or (0, 0, 0, 0, 0)
    total = float(sum(base))

    # 相关性：直接冲着她来的最高，群聊闲话最低
    if name in {"owner_message", "praise", "insult", "intimate", "aroused", "repair"}:
        rel = 1.0
    elif name in {"group_interact", "meal", "coffee"}:
        rel = 0.55
    else:
        rel = 0.75

    # 利害：看「正向三维」和「皮质醇」的方向，不能只看总和
    # （insult 的 CORT +12 会把总和顶成正数，但它显然是坏事）
    pos = float(base[0]) + float(base[2]) + float(base[3])   # DA + SER + OXY
    cort_up = float(base[1]) > 0
    if cort_up and pos <= 0:
        val = 0.15                       # 纯坏事
    elif cort_up and pos > 0:
        val = 0.35                       # 又疼又有点爽（aroused）
    elif pos > 0:
        val = min(1.0, 0.55 + pos / 30.0)  # 好事
    else:
        val = 0.45

    # 应对：扛得住的能力，由当前状态推
    coping = ctx.get("coping")
    if coping is None:
        try:
            st = _load()
            coping = (
                float(st.get("SER", 50.0)) * 0.45
                + float(st.get("DA", 50.0)) * 0.25
                + (100.0 - float(st.get("CORT", 20.0))) * 0.30
            ) / 100.0
        except Exception:  # noqa: BLE001
            coping = 0.6

    # 归因：被骂/被修好是外部事件，闲聊是中性
    agency = ctx.get("agency")
    if agency is None:
        agency = 1.0 if name in {"insult", "repair", "praise"} else 0.5

    return {
        "relevance": rel,
        "valence": val,
        "coping": max(0.0, min(1.0, float(coping))),
        "agency": float(agency),
    }


def _appraisal_scale(name: str, base_scale: float, ctx: dict | None) -> float:
    """把评价结果折进 scale（不改 EVENTS 表本身）。"""
    ap = appraise(name, ctx)
    delta = EVENTS.get(name) or (0, 0, 0, 0, 0)
    pos = float(delta[0]) + float(delta[2]) + float(delta[3])
    cort_up = float(delta[1]) > 0
    is_bad = (cort_up and pos <= 0)
    sc = base_scale * (0.45 + 0.55 * ap["relevance"])
    if is_bad:
        # 坏事件：能应对就少疼一点，扛不住就更疼
        sc *= (1.35 - 0.70 * ap["coping"])
    elif pos > 0:
        # 好事：相关性已经折过，这里只做轻微加权
        sc *= (0.85 + 0.30 * ap["valence"])
    return max(0.05, min(3.0, sc))


def apply_event(name: str, scale: float = 1.0, ctx: dict | None = None) -> dict:
    """事件一次性改变激素（会先补算 tick，保证顺序正确）。

    正向增益叠三道闸（参考 fish_heart 的养成节奏）：
    1. 热度折扣：同类事件窗口内重复发生 → 1/(1+n/HEAT_DIVISOR)
    2. 天花板递减：越接近 100 越难涨
    3. 每日额度：DA 40 / SER 30 / OXY 40
    负向（责骂、压力上升）照旧累加，不打折不限额——它们是代价。
    """
    if not ENABLED:
        return dict(_load())
    # 2026-11-07：激素事件同时给"情绪余波"充能。
    # 激素一 tick 就回来了，但被骂一句不该下一秒就没事 —— 那是 emotion_residue 的事。
    try:
        from plugins import emotion_residue as _er
        _er.fire(name)
    except Exception:  # noqa: BLE001
        pass
    tick()
    s = _load()
    # 2026-11-08：先过认知评价，再改激素（BioCore 那套 appraisal）
    scale = _appraisal_scale(name, scale, ctx)
    delta = EVENTS.get(name)
    if delta:
        now = _now()
        lst = [t for t in (_heat.get(name) or []) if now - t <= HEAT_WINDOW_SEC]
        heat_n = len(lst)
        _roll_daily()
        for i, k in enumerate(HORMONES):
            d = delta[i] * scale
            if d > 0 and k in GAIN_DIMS:
                d *= 1.0 / (1.0 + heat_n / HEAT_DIVISOR)   # 热度折扣
                d *= max(0.0, 1.0 - s[k] / 100.0)          # 天花板递减
                # 亲密事件走独立额度池（2026-11-05）：DA 日常 40 聊几句就满，
                # 不分开记的话晚上根本没量可用
                if name in INTIMATE_EVENTS:
                    cap = INTIMATE_CAP.get(k)
                    capkey = k + "_int"
                else:
                    cap = DAILY_CAP.get(k)                      # 每日额度
                    capkey = k
                if cap is not None:
                    used = _daily.get(capkey, 0.0)
                    d = min(d, max(0.0, cap - used))
                    _daily[capkey] = used + d
            # 安抚类事件的"降皮质醇"不能把 CORT 抽干（2026-11-04：
            # owner_message 每条 -10 时，聊一晚上 CORT 从 38 掉到 0.04）
            if k == "CORT" and d < 0 and name in _COMFORT_EVENTS:
                d = max(d, -(max(0.0, s[k] - 12.0)))
            s[k] = _clamp(s[k] + d)
        lst.append(now)
        _heat[name] = lst
        _save()
    return {k: s[k] for k in HORMONES}


def nudge(deltas: dict) -> dict:
    """小幅度直接改激素（2026-11-08，供"再激活"用）。

    和 apply_event 的区别：
      · 不过热度折扣、不吃每日额度（这是"想起来"，不是"发生了"）
      · 每个维度强制封顶 ±1.2，防止每轮召回把激素顶满
    """
    if not ENABLED:
        return dict(_load())
    if not deltas:
        return dict(_load())
    tick()
    s = _load()
    for k in HORMONES:
        d = float(deltas.get(k) or 0.0)
        if not d:
            continue
        d = max(-1.2, min(1.2, d))
        s[k] = _clamp(float(s[k]) + d)
    _save()
    return {k: s[k] for k in HORMONES}


def values() -> dict:
    tick()
    s = _load()
    return {k: round(s[k], 1) for k in HORMONES}


def derive_emotions() -> dict:
    """7 维情绪 = 激素线性组合（含 ±3 噪声，避免太机械）。"""
    v = values()
    da, cort, ser, oxy, mel = v["DA"], v["CORT"], v["SER"], v["OXY"], v["MEL"]
    n = lambda: random.uniform(-3, 3)  # noqa: E731
    out = {
        "开心": 0.55 * da + 0.30 * ser - 0.20 * cort + 12 + n(),
        "害羞": 0.50 * oxy + 0.20 * da - 0.25 * cort + 4 + n(),
        # 情欲（2026-11-05 加，第 8 维，不进 EMOTIONS 列表 → 不改变七维口径和下游映射）：
        # MEL 用 (100-mel) 只做微调而不是直接减满 —— 困和想要可以同时成立，
        # 深夜不该因为 MEL=100 就把这一维压成 0
        "情欲": 0.42 * da + 0.38 * oxy - 0.22 * cort + 0.10 * (100 - mel) + 2 + n(),
        "烦": 0.55 * cort + 0.25 * mel - 0.20 * ser + 8 + n(),
        "怒": 0.70 * cort - 0.25 * ser + 0.15 * da + 5 + n(),
        "丧": 0.45 * (100 - da) + 0.30 * (100 - ser) - 0.20 * oxy + 6 + n(),
        "累": 0.55 * mel + 0.15 * (100 - ser) + n(),
        "害怕": 0.55 * cort - 0.20 * ser + 0.15 * (100 - oxy) + n(),
    }
    res = {k: round(max(0.0, min(100.0, x)), 1) for k, x in out.items()}
    # 2026-11-07：叠上"上一轮还没消的气"。
    # 即时值是"这一秒的样子"，残留是"刚才那件事的余波"，两回事。
    try:
        from plugins import emotion_residue as _er
        res = _er.apply(res)
    except Exception:  # noqa: BLE001
        pass
    return res


def prompt_line() -> str:
    """给 prompt 的一行摘要（token 很小）。

    2026-11-07 改：以前输出「开心48% 害羞61% 烦21%…」七个百分比，
    模型得自己脑补"这算什么状态"。现在换成一句复合情绪（乐+羞+累 → 「挺高兴…
    身子有点沉…嘴上不认」），七个数字不再堆上去。
    来源：抖音那张情绪词库的组合推导法，见 plugins/mood_compound.py 顶部说明。
    """
    v = values()
    e = derive_emotions()
    hs = " ".join(f"{k}{int(v[k])}" for k in HORMONES)
    try:
        from plugins import mood_compound as mc
        ml = mc.mood_line(e)
    except Exception:  # noqa: BLE001
        ml = ""
    if not ml:
        # 兜底：复合情绪算不出来就退回原来的百分比
        es = " ".join(f"{k}{int(e[k])}%"
                      for k in ("开心", "害羞", "烦", "怒", "丧", "累", "害怕"))
        return f"{hs} | {es}"
    return f"{hs} | 此刻：{ml}"


def snapshot() -> dict:
    return {"hormones": values(), "emotions": derive_emotions(), "enabled": ENABLED}


def intimate_line() -> str:
    """私聊专用：情欲强度 + 一句行为提示。低于阈值返回空串，不占 token。"""
    try:
        v = values()
        e = derive_emotions()
        a = float(e.get("情欲") or 0.0)
        if a < 25:
            return ""
        if a >= 70:
            tip = "很浓，可以直接往身上带，别绕、别端"
        elif a >= 45:
            tip = "起来了，会主动靠近、会撩回去，嘴上不认但身体诚实"
        else:
            tip = "有点意思了，别硬装没感觉"
        return f"【情欲 {int(a)}/100】{tip}（这是你自己的身体反应，照它说话，别把数字念出来）"
    except Exception:  # noqa: BLE001
        return ""


def _append_history() -> None:
    """把当前派生情绪追加到历史（面板画趋势用）。"""
    try:
        import json as _j
        try:
            data = _j.loads(HIST_FILE.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict):
                data = {}
        except Exception:  # noqa: BLE001
            data = {}
        pts = data.get("points") or []
        e = derive_emotions()
        pts.append({"ts": time.strftime("%H:%M"), "e": e, "h": {k: round(_load()[k], 1) for k in HORMONES}})
        data["points"] = pts[-HIST_MAX:]
        data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        HIST_FILE.parent.mkdir(parents=True, exist_ok=True)
        HIST_FILE.write_text(_j.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def temperature_delta() -> float:
    """激素对采样温度的修正（约 -0.35 ~ +0.35）。

    2026-11-05（借 digital-life-core 的「情绪劫持」）：以前激素只写进 prompt，
    模型可以不理；这里让它**真的改采样参数** ——

        皮质醇（压力） +0.60 权重 → 温度升 → 回答更急、更冲、更跳
        多巴胺（兴奋） +0.25 权重 → 略升   → 更活泼
        血清素（平稳） -0.30 权重 → 降温   → 更稳、更克制
        褪黑素（困）   -0.20 权重 → 降温   → 更迷糊、更短

    只对聊天生效（调用方按 record_scope 判断），内部任务不受影响。
    """
    try:
        v = values()
    except Exception:  # noqa: BLE001
        return 0.0
    try:
        t = GENOME.get("temperature") or {}
        d = ((float(v.get("CORT", BASE["CORT"])) - BASE["CORT"]) / 100.0 * float(t.get("cort", 0.60))
             + (float(v.get("DA", BASE["DA"])) - BASE["DA"]) / 100.0 * float(t.get("da", 0.25))
             - (float(v.get("SER", BASE["SER"])) - BASE["SER"]) / 100.0 * float(-t.get("ser", -0.30))
             - (float(v.get("MEL", BASE["MEL"])) - BASE["MEL"]) / 100.0 * float(-t.get("mel", -0.20)))
    except Exception:  # noqa: BLE001
        return 0.0
    return max(float(t.get("min", -0.35)), min(float(t.get("max", 0.35)), d))
