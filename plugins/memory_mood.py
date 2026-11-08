"""记忆的情感指纹：写入时盖章、检索时按「心境一致」倾斜、被想起时再巩固。

来源：npm 包 `dsh-emotion-memory`（小世炎，v0.1.4）的三个设计，搬过来适配我们自己的结构：

  ① 记忆条目带情感指纹 —— 写入时顺手记下当时的 PAD + 一个词 + 显著性
  ② 检索打分含「心境一致」 —— 难过的时候更容易想起难过的事（mood-congruent retrieval）
  ③ 被想起时再巩固 —— 用当前状态微调指纹，系数 0.15（保留编码时的原始味）

差别：它用中科院 PAD 量表 + 14 个离散通道锚点、每轮调轻量模型给评估变量；
我们已经有 5 维激素 → 6 基础情绪（plugins/mood_compound.py），
所以 PAD 直接由那 6 个基础维度算，不额外调模型、不花 token。

    乐 → P+       哀 → P−        恶 → P− / D+（嫌恶带掌控感）
    怒 → A+ / P−  惧 → A+ / P−   惊 → A+
    累 → A−
    D（掌控）≈ 自我势力（0~1 映射到 −1…1）− 冲突惩罚
"""
from __future__ import annotations

import math

# 再巩固的系数：按包里的 0.15，别太大 —— 否则被想起几次就面目全非
RECONSOLIDATE = 0.15
# 检索里「心境一致」的权重：压得很小，只做同分时的倾斜，不能压过语义相关性
MOOD_WEIGHT = 0.12


def _clamp(v: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def pad_now() -> dict:
    """此刻的 PAD（每个轴 −1…1）+ 一个词。

    2026-11-07 校准：P/A 不再用手调系数，改成
    7 维情绪 → 中科院 14 个锚点的凸组合（见 plugins/pad_anchor.py），
    每个坐标都能追到论文里的某个情绪锚点。
    D（掌控）仍由三我势力给 —— 量表里没有"掌控"的人格对应物，
    但我们有现成的自我势力 / 冲突等级，比从情绪反推更直接。
    """
    try:
        from plugins import hormones as h
        from plugins import pad_anchor as pa
        from plugins import mood_compound as mc
        emo = h.derive_emotions()
        base = pa.pad_from_emotions(emo)
        hit = mc.compound_mood(emo)
    except Exception:  # noqa: BLE001
        return {}

    P = float(base.get("P") or 0)
    A = float(base.get("A") or 0)
    # 掌控：自我势力 0~1 → −1…1；冲突高就往下压
    try:
        from server import soul_aggregation as sa
        f = sa.view().get("forces") or {}
        ego = float(f.get("自我") or f.get("ego") or 0.5)
        D = _clamp((ego - 0.5) * 2.0)
        if str(sa.conflict_level() or "低") == "高":
            D = _clamp(D - 0.35)
    except Exception:  # noqa: BLE001
        D = 0.0
    out = {
        "P": round(_clamp(P), 3), "A": round(_clamp(A), 3), "D": round(D, 3),
        "词": str(hit.get("name") or ""),
        "锚": pa.nearest_anchor({"P": P, "A": A, "D": D}),
    }
    # 2026-11-07：融合「她自己刚说的感觉」。
    # 激素不知道"刚才那句话把她逗到了"，但她在回复末尾自评的那一下知道。
    # 自评只覆盖这一轮，所以只给 0.35 的权重（30 分钟内线性衰减）。
    try:
        from plugins import self_report as _sr
        out = _sr.blend(out)
    except Exception:  # noqa: BLE001
        pass
    return out


def salience(pad: dict) -> float:
    """显著性：情绪越强，这条记忆越"重"。下限 0.15（跟包里一致）。

    2026-11-07：激活度按信度 0.68 折算，别让测不准的轴把显著性抬太高。
    """
    p = abs(float(pad.get("P") or 0))
    a = abs(float(pad.get("A") or 0)) * 0.68
    return round(max(0.15, (a + p) / 2.0), 3)


def stamp(item: dict) -> dict:
    """给一条记忆盖情感指纹（就地修改，返回同一条）。"""
    if not isinstance(item, dict) or item.get("mood"):
        return item
    pad = pad_now()
    if not pad:
        return item
    item["mood"] = pad
    item["sal"] = salience(pad)
    return item


def mood_fit(item: dict) -> float:
    """记忆指纹与当前心境的相似度（0~1）。没有指纹的按中性 0.5。"""
    m = (item or {}).get("mood")
    if not isinstance(m, dict):
        return 0.5
    cur = pad_now()
    if not cur:
        return 0.5
    a = (float(m.get("P") or 0), float(m.get("A") or 0), float(m.get("D") or 0))
    b = (float(cur.get("P") or 0), float(cur.get("A") or 0), float(cur.get("D") or 0))
    # 2026-11-07：加权余弦 —— 激活度 α=0.58 测不准，按 0.68 打折，
    # 不让一个不可靠的轴主导「这条记忆和现在的心情像不像」。
    try:
        from plugins import pad_anchor as pa
        w = (pa.RELIABILITY["P"], pa.RELIABILITY["A"], pa.RELIABILITY["D"])
    except Exception:  # noqa: BLE001
        w = (1.0, 0.68, 0.85)
    num = sum(wi * x * y for wi, x, y in zip(w, a, b))
    na = math.sqrt(sum(wi * x * x for wi, x in zip(w, a)))
    nb = math.sqrt(sum(wi * x * x for wi, x in zip(w, b)))
    if na < 1e-6 or nb < 1e-6:
        return 0.5
    cos = num / (na * nb)
    return round((cos + 1.0) / 2.0, 3)


def reconsolidate(item: dict, coef: float = RECONSOLIDATE) -> dict:
    """被想起时再巩固：用当前状态微调指纹。

    保留编码时的词（`mood.词` 不动），另记「最近想起时的情绪」到 `mood.re`。
    这样既能看出这条记忆"本来是当时什么味"，
    又知道它最近一次被想起时她正在什么心情。
    """
    if not isinstance(item, dict):
        return item
    m = item.get("mood")
    if not isinstance(m, dict):
        return stamp(item)
    cur = pad_now()
    if not cur:
        return item
    for ax in ("P", "A", "D"):
        old = float(m.get(ax) or 0)
        new = float(cur.get(ax) or 0)
        m[ax] = round(_clamp(old + (new - old) * coef), 3)
    # 编码时的词保留；最近想起时的情绪单独记一笔
    m["re"] = {"P": cur.get("P"), "A": cur.get("A"), "D": cur.get("D"),
               "词": cur.get("词") or "", "n": int(m.get("n") or 0) + 1}
    item["mood"] = m
    item["sal"] = salience(m)
    return item
