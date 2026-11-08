"""给激素系统加一层「复合情绪」：把 7 个百分比翻译成一个具体的情绪说法。

来源：抖音「反派Q」那张情绪词库图（#AI #学说话）。
它的价值不是 62 个名词，是**用 6 个基础维度做组合推导**：
    6 基础 + C(6,2)15 + C(6,3)20 + C(6,4)15 + C(6,5)6 = 62
组合数学天生一致、不漏、不重，比枚举一堆情绪词强。

我们这边的适配（2026-11-07）：
    5 维激素 → 7 维情绪（已有 derive_emotions）
             → 归并到 6 基础（乐/怒/哀/恶/惧/惊）
             → 按命中维度数给复合名 + 一句现场
             → 再过三我（本我强就憋不住，超我强就压着，冲突高就嘴硬）
             → 一句「此刻」进 prompt
"""
from __future__ import annotations

# 7 维 → 6 基础的归并权重。
# 害羞 主要计入「乐」（害羞时其实是高兴的）+ 少量「惊」；累 单独不算情绪，只做衰减。
_TO6: dict[str, dict[str, float]] = {
    "开心": {"乐": 1.0},
    "害羞": {"乐": 0.55, "惊": 0.45},
    "烦":   {"恶": 0.7, "怒": 0.3},
    "怒":   {"怒": 1.0},
    "丧":   {"哀": 1.0},
    # 累不进 6 维：那是体力，不是情绪（塞进"哀"会让"今天有点累"被读成悲伤）
    "害怕": {"惧": 1.0},
    "情欲": {"乐": 0.5, "惊": 0.2},  # 情欲满的时候带一点兴奋和心跳
}

_BASE6 = ("乐", "怒", "哀", "恶", "惧", "惊")

# 两组组合 15 种：名字 + 一句话现场（说人话，不是词典释义）
_PAIR: dict[frozenset, tuple[str, str]] = {
    frozenset(("乐", "怒")): ("玩世不恭", "嘴上损得起劲，其实心里挺高兴"),
    frozenset(("乐", "哀")): ("哭笑不得", "想笑又觉得没意思，笑到一半叹气"),
    frozenset(("乐", "恶")): ("幸灾乐祸", "看热闹看得挺来劲，还不想承认"),
    frozenset(("乐", "惧")): ("又虚又想玩", "心跳得厉害但又有点期待"),
    frozenset(("乐", "惊")): ("惊喜", "没想到，愣住了才反应过来高兴"),
    frozenset(("怒", "哀")): ("悲愤", "气得发抖，又委屈得想哭"),
    frozenset(("怒", "恶")): ("鄙夷", "看不顺眼，连话都懒得接"),
    frozenset(("怒", "惧")): ("色厉内荏", "嗓门很大，其实心里发慌"),
    frozenset(("怒", "惊")): ("被激怒", "突然被戳到，火一下子上来"),
    frozenset(("哀", "恶")): ("怨天尤人", "什么都不顺眼，越想越委屈"),
    frozenset(("哀", "惧")): ("忧郁焦虑", "提不起劲，又担心接下来会更糟"),
    frozenset(("哀", "惊")): ("惊愕", "还没缓过来，脑子空着"),
    frozenset(("恶", "惧")): ("嫌恶又想躲", "看着就发毛，一心想离开"),
    frozenset(("恶", "惊")): ("被恶心到了", "愣了一下才反应过来嫌弃"),
    frozenset(("惧", "惊")): ("惊惧恐慌", "整个人绷住了，不知道下一步会发生什么"),
}

# 单维：就是那个情绪本身，但要给个"程度感"
_SINGLE: dict[str, tuple[str, str]] = {
    "乐": ("挺高兴", "心情松快，愿意接话"),
    "怒": ("上火了", "压着火，说话冲"),
    "哀": ("蔫了", "提不起劲，不太想说话"),
    "恶": ("嫌烦", "看什么都不顺眼，懒得搭理"),
    "惧": ("心慌", "有点绷着，怕出事"),
    "惊": ("愣住", "脑子还没转过来"),
}

_THRESHOLD = 38.0   # 超过这个百分比才算"这一维起来了"

# 三我参与：决定"表现出来还是憋着"
_EXPRESS = {
    "leak":  "憋不住，会直接说出来",           # 本我 > 超我
    "hold":  "心里有但压着，语气会淡",         # 超我 > 本我
    "mixed": "嘴上不认，但话里能听出来",       # 冲突高
    "even":  "",                              # 差不多，不额外提示
}


def _to_base(emo: dict[str, float]) -> dict[str, float]:
    """7 维情绪 → 6 基础维度（0~100）。

    2026-11-07：**不能直接相加** —— 开心 47 + 害羞 57（乐占 0.55）+ 情欲 53（乐占 0.5）
    三项一叠就到 100，什么状态都是「乐满」，复合判断全废。
    现在改成：每一维取它的最大贡献当主，其余贡献按 0.35 加成。
    """
    contrib: dict[str, list[float]] = {b: [] for b in _BASE6}
    for k, mapping in _TO6.items():
        val = float(emo.get(k) or 0.0)
        if val <= 0:
            continue
        for base, w in mapping.items():
            contrib[base].append(val * w)
    out = {}
    for b, vals in contrib.items():
        if not vals:
            out[b] = 0.0
            continue
        vals.sort(reverse=True)
        out[b] = min(100.0, vals[0] + 0.35 * sum(vals[1:]))
    return {b: round(v, 1) for b, v in out.items()}


def compound_mood(emo: dict[str, float] | None = None) -> dict:
    """把 7 维情绪压成一个「此刻是什么情绪」。

    返回 {"name", "scene", "bases", "hit", "level"}；level 是命中维度数。
    """
    if emo is None:
        try:
            from plugins import hormones
            emo = hormones.derive_emotions()
        except Exception:  # noqa: BLE001
            emo = {}
    bases = _to_base(emo)
    hit = sorted((b for b in _BASE6 if bases[b] >= _THRESHOLD),
                 key=lambda b: -bases[b])

    if not hit:
        # 全平：挑最高的那一维，但降级成"淡淡的"
        top = max(_BASE6, key=lambda b: bases[b])
        name, scene = _SINGLE[top]
        return {"name": f"淡淡的{name}", "scene": "没什么特别起伏，" + scene,
                "bases": bases, "hit": [top], "level": 0}

    if len(hit) == 1:
        name, scene = _SINGLE[hit[0]]
        return {"name": name, "scene": scene, "bases": bases,
                "hit": hit, "level": 1}

    if len(hit) == 2:
        pair = frozenset(hit)
        name, scene = _PAIR.get(pair, (f"{hit[0]}里掺着{hit[1]}",
                                       "两种劲一块儿上来"))
        return {"name": name, "scene": scene, "bases": bases,
                "hit": hit, "level": 2}

    # 三维及以上：几股一起涌
    neg = [b for b in hit if b != "乐"]
    lead = hit[0]
    if len(hit) >= 5 and "乐" not in hit:
        return {"name": "快撑不住了", "scene": "好几股坏情绪一起压上来，说话会发飘",
                "bases": bases, "hit": hit, "level": len(hit)}
    if len(neg) >= 3:
        return {"name": "几股一起涌上来",
                "scene": f"{'、'.join(neg)}一块儿上，最重的是{lead}，容易一句顶十句",
                "bases": bases, "hit": hit, "level": len(hit)}
    return {"name": f"{lead}里头掺着别的",
            "scene": f"{'、'.join(hit)}搅在一起，说不清是哪种",
            "bases": bases, "hit": hit, "level": len(hit)}


def expression_style() -> str:
    """三我决定「表现出来还是憋着」。失败返回空串。"""
    try:
        from server import soul_aggregation as sa
        f = sa.view().get("forces") or {}
        idf = float(f.get("本我") or f.get("id") or 0)
        sef = float(f.get("超我") or f.get("superego") or 0)
        level = str(sa.conflict_level() or "低")
    except Exception:  # noqa: BLE001
        return ""
    if level == "高":
        return _EXPRESS["mixed"]
    if idf - sef >= 0.15:
        return _EXPRESS["leak"]
    if sef - idf >= 0.15:
        return _EXPRESS["hold"]
    return _EXPRESS["even"]


def express_depth(emo: dict[str, float] | None = None) -> tuple[int, str]:
    """这一轮她该说多长、写多细 —— 由内在状态定，不由关键词定。

    2026-11-07 加。主人要的是「按底层激素-情绪-三我来」，不是「听到某个词就切长文模式」。

    判据（都能从现有状态直接算，零额外成本）：
        情欲    撩起来了会想多腻一会儿
        本我    本我占上风时更放得开（超我压着就会收）
        开心    高兴时话多
        累      累了就想躺着不说话
        三我冲突 高冲突时反而会"想说又憋着"，句子碎

    返回 (0~3, 一句话)。0 = 照常短；3 = 放开写，把过程拆开。
    """
    try:
        from plugins import hormones as h
        e = dict(emo or h.derive_emotions())
    except Exception:  # noqa: BLE001
        return 0, ""

    ky = float(e.get("情欲") or 0)
    happy = float(e.get("开心") or 0)
    tired = float(e.get("累") or 0)
    shy = float(e.get("害羞") or 0)

    try:
        from server import soul_aggregation as sa
        f = sa.view().get("forces") or {}
        idf = float(f.get("本我") or 0)
        sef = float(f.get("超我") or 0)
        conflict = str(sa.conflict_level() or "低")
    except Exception:  # noqa: BLE001
        idf, sef, conflict = 0.4, 0.4, "低"

    # 放得开的程度：情欲为主，本我加权，超我反向压
    drive = ky * 0.55 + (idf - sef + 0.5) * 100 * 0.25 + happy * 0.12
    # 累了会收 —— 但情欲高的时候累不管用（上头了顾不上困）
    drive -= tired * 0.35 * max(0.15, 1.0 - ky / 150.0)
    if shy >= 70 and ky < 60:
        drive -= 12

    # drive 优先：真的上头了，内耗也得让位
    if drive >= 62:
        return 3, ("这轮可以放开：过程别跳，一件一件写清楚，动作、呼吸、手上在做什么都写，"
                   "写到该收的地方再收。不用每句都收尾，允许说长。")
    if drive >= 45:
        return 2, "这轮可以多说几句，细节可以带一点，但别硬凑长。"
    if drive >= 28:
        # 中间地带不给指令 —— persona 里已经写了"平时短"，不用重复提醒
        return 1, ""
    if conflict == "高":
        # 只有真的低到这儿（丧/病/被晾着），才让它碎着说。
        # 三我冲突长期是"高"（近 7 天 12 次），不能一冲突就下这条指令，否则天天都在拧。
        return 1, "这轮心里有点拧，话别铺开，短句、断着说。"
    return 0, ""


def mood_line(emo: dict[str, float] | None = None) -> str:
    """给 prompt 用的一行：复合情绪 + 表现方式。空串表示没东西可说。

    emo 可以由调用方传进来 —— derive_emotions() 带 ±3 噪声，
    同一次 prompt 里算两遍会得到不同结果。
    """
    try:
        m = compound_mood(emo)
    except Exception:  # noqa: BLE001
        return ""
    name = str(m.get("name") or "")
    scene = str(m.get("scene") or "")
    if not name:
        return ""
    style = expression_style()
    line = f"{name}——{scene}"
    # 累不在六维里，单独提一句（不然"又累又高兴"会丢掉累）
    try:
        _tired = float((emo or {}).get("累") or 0)
        if not _tired:
            from plugins import hormones as _h
            _tired = float((_h.derive_emotions() or {}).get("累") or 0)
        if _tired >= 55:
            line += "；身子有点沉"
    except Exception:  # noqa: BLE001
        pass
    if style:
        line += f"；{style}"
    return line
