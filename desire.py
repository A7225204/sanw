"""简化版欲望系统：三个驱动（想念 / 无聊 / 累）。

为什么需要
----------
原来的主动私聊是 `random.random() > 0.07` —— 纯掷骰子，用户感受不到"她为什么突然找我"。
这里用三条会随时间自然变化的驱动值算出一个「想说话的冲动」，冲动够高才开口，并且：

- 主人来消息      → 想念立刻大幅下降（被满足了）
- 任何互动        → 无聊下降
- 清醒越久        → 累上升；累太高会压低冲动（这时候她不想说话）
- 深夜            → 累自动回落（在睡觉）
- 全部落盘，重启不丢；按"距上次 tick 的时间"补算，关机期间也在长

不调模型、不花 token。参考 AstrBot Desire System 的思路，只取最小可用的三个维度。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FILE = ROOT / "memory" / "desire_state.json"

# 每分钟自然变化量
MISS_GAIN = 0.45      # 主人没说话 → 想念上涨
BORED_GAIN = 0.25     # 闲着 → 无聊上涨

# 事件带来的变化
OWNER_TALK_MISS_DROP = 60.0
OWNER_TALK_BORED_DROP = 25.0
INTERACT_BORED_DROP = 18.0

_DEFAULT = {"miss": 20.0, "bored": 30.0}   # 疲劳不在这里存：由 ai_group._body_tired() 传进来


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, float(v)))


def _state() -> dict:
    data = {}
    try:
        raw = json.loads(FILE.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            data = raw
    except Exception:  # noqa: BLE001
        data = {}
    drives = data.get("drives") or {}
    out = {k: _clamp(drives.get(k, _DEFAULT[k])) for k in _DEFAULT}
    data["drives"] = out
    try:
        data["last_tick"] = float(data.get("last_tick") or 0)
    except (TypeError, ValueError):
        data["last_tick"] = 0.0
    return data


def _save(data: dict) -> None:
    try:
        FILE.parent.mkdir(parents=True, exist_ok=True)
        data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def tick() -> dict:
    """按距上次的时间补算三个驱动，返回最新值。幂等：同一时刻多次调用不会重复加。"""
    data = _state()
    drives = data["drives"]
    now = time.time()
    last = data.get("last_tick") or 0.0
    if last <= 0:
        data["last_tick"] = now
        _save(data)
        return drives
    minutes = max(0.0, (now - last) / 60.0)
    if minutes <= 0:
        return drives
    drives["miss"] = _clamp(drives["miss"] + MISS_GAIN * minutes)
    drives["bored"] = _clamp(drives["bored"] + BORED_GAIN * minutes)
    data["last_tick"] = now
    _save(data)
    return drives


def on_owner_talk() -> None:
    """主人说话了：想念被满足，无聊也下去一点。"""
    data = _state()
    d = data["drives"]
    d["miss"] = _clamp(d["miss"] - OWNER_TALK_MISS_DROP)
    d["bored"] = _clamp(d["bored"] - OWNER_TALK_BORED_DROP)
    data["last_owner_talk"] = time.time()
    data["last_tick"] = time.time()
    _save(data)


def on_interact() -> None:
    """任何一次互动：无聊下降。"""
    data = _state()
    data["drives"]["bored"] = _clamp(data["drives"]["bored"] - INTERACT_BORED_DROP)
    data["last_interact"] = time.time()
    _save(data)


# ---------------------------------------------------------------- A 方案：冲动由激素派生
# 旧系统用"想念/无聊"线性累加，和情绪层两套账。
# 现在改成直接读 5 个激素：
#   亲密缺口(OXY) 越缺越想找你；多巴胺(DA) 有劲才想分享；
#   压力(CORT) 高压不想说话；血清素(SER) 稳定更愿意开口；褪黑素(MEL) 高就闭嘴。
_BASE_OXY = 35.0


def _urge_from_hormones(tired: float | None = None) -> float | None:
    try:
        from plugins import hormones
        v = hormones.values()
    except Exception:  # noqa: BLE001
        return None
    if not v:
        return None
    oxy = float(v.get("OXY") or 0)
    da = float(v.get("DA") or 0)
    cort = float(v.get("CORT") or 0)
    ser = float(v.get("SER") or 0)
    mel = float(v.get("MEL") or 0)

    oxy_gap = max(0.0, 100.0 - oxy) / 100.0          # 越不亲密越想找你
    da_n = da / 100.0
    cort_n = max(0.0, min(1.0, cort / 100.0))
    ser_n = max(0.0, min(1.0, ser / 100.0))

    score = 0.45 * oxy_gap + 0.25 * da_n + 0.15 * (1.0 - cort_n) + 0.15 * ser_n
    if mel > 60:
        score *= 0.4
    if tired is not None and tired > 55:             # 身体层还传疲劳就照旧压一压
        score *= 0.6
    return max(0.0, min(1.0, score))


def urge_detail(tired: float | None = None) -> dict:
    """返回可解释的分项（日志/面板用）。"""
    try:
        from plugins import hormones
        v = hormones.values()
    except Exception:  # noqa: BLE001
        v = {}
    oxy = float(v.get("OXY") or 0)
    da = float(v.get("DA") or 0)
    cort = float(v.get("CORT") or 0)
    ser = float(v.get("SER") or 0)
    mel = float(v.get("MEL") or 0)
    return {
        "urge": _urge_from_hormones(tired),
        "oxy_gap": round(max(0.0, 100 - oxy) / 100, 3),
        "da": round(da / 100, 3),
        "cort": round(cort / 100, 3),
        "ser": round(ser / 100, 3),
        "mel": mel,
    }


def urge(tired: float | None = None) -> float:
    """想主动说话的冲动 0~1。

    A 方案：由激素派生（亲密缺口/多巴胺/压力/血清素/褪黑素）；
    激素不可用时回退到旧的想念/无聊算法。
    """
    _h = _urge_from_hormones(tired)
    if _h is not None:
        return _h
    d = tick()
    t = _DEFAULT_TIRED if tired is None else float(tired)
    score = 0.60 * (d["miss"] / 100.0) + 0.40 * (d["bored"] / 100.0)
    if t > 55:
        score -= 0.55 * ((t - 55) / 45.0)
    return _clamp(score, 0.0, 1.0)


def thought(tired: float | None = None) -> str:
    """一句「此刻念头」，给 prompt 易变尾部用；没有明显念头就返回空串。"""
    d = tick()
    t = _DEFAULT_TIRED if tired is None else float(tired)
    if t >= 75:
        return "累得不太想说话，想瘫着"
    if d["miss"] >= 60:
        return "有点想主人了，想找他说句话"
    if d["bored"] >= 65:
        return "无聊得发慌，想找点乐子"
    return ""


_DEFAULT_TIRED = 30.0


def snapshot(tired: float | None = None) -> dict:
    d = tick()
    d["tired"] = _DEFAULT_TIRED if tired is None else float(tired)
    return d
