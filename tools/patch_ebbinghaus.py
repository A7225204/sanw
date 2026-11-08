import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\plugins\long_memory.py")
s = p.read_text(encoding="utf-8")

if "_half_life_days" in s:
    print("已经加过")
    raise SystemExit

# ---------- 1) 常量：分类半衰期 + 复习增益 + 遗忘阈值 ----------
old_const = """MEM_HALF_LIFE_DAYS = 60.0"""
new_const = """MEM_HALF_LIFE_DAYS = 60.0          # 默认半衰期（没命中过的普通事实）

# ---- 艾宾浩斯层（2026-10-28 追加）----
# 1) 分类半衰期：约定/偏好/人名这类"长期有效"的忘得慢，吃喝天气这类忘得快
# 2) 复习增益：被用得越多半衰期越长（hits 越多越难忘）
# 3) 遗忘阈值：生效权重低于门槛就不再进 prompt（档案里还留着，只是"想不起来"）
MEM_HALF_LIFE_CLASSES = (
    ("long", 180.0, r"约定|承诺|答应|说好|偏好|喜欢|讨厌|不爱|生日|习惯|怕|过敏|名字|外号|叫|住|上班|工作|职业|忌口|素食"),
    ("short", 35.0, r"今天|昨天|刚才|这周|上周|明天|吃了|喝了|天气|心情|困|饿|累|下雨|热|冷"),
)
MEM_HALF_LIFE_HITS_GAIN = 0.55     # 每命中一次，半衰期 ×(1+0.55)，封顶
MEM_HALF_LIFE_MAX = 500.0          # 半衰期上限（天）
MEM_DECAY_FLOOR = 0.12             # 生效权重低于这个 → 不进 prompt"""
if old_const in s:
    s = s.replace(old_const, new_const, 1)
else:
    print("常量锚点没找到")

# ---------- 2) 半衰期计算 + 分类 ----------
old_rank = '''def _item_rank(it: dict, now: float | None = None) -> float:
    """生效权重：自身权重 × 时间衰减。被用过越近、越常被强化 → 越靠前。"""
    now = now or _now_ts()
    try:
        w = float(it.get("w") or MEM_WEIGHT_START)
    except (TypeError, ValueError):
        w = MEM_WEIGHT_START
    try:
        lu = float(it.get("lu") or 0.0)
    except (TypeError, ValueError):
        lu = 0.0
    if lu <= 0:
        lu = now - 86400.0 * 7  # 老数据没有 last_used：当成一周前用过，给个中性起点
    age_days = max(0.0, (now - lu) / 86400.0)
    return w * (0.5 ** (age_days / MEM_HALF_LIFE_DAYS))'''
new_rank = '''def _half_life_days(it: dict) -> float:
    """这条记忆的半衰期：分类基准 × 复习增益，封顶 MEM_HALF_LIFE_MAX。

    · 约定/偏好/人名 → 180 天起（忘得慢）
    · 今天吃了啥/天气 → 35 天起（忘得快）
    · 其余 → 默认 60 天
    · 每被真正用到一次（hits+1），半衰期 ×(1+GAIN)
    """
    text = f"{it.get('subject') or ''}{it.get('fact') or ''}"
    base = MEM_HALF_LIFE_DAYS
    for _name, days, pattern in MEM_HALF_LIFE_CLASSES:
        try:
            if re.search(pattern, text):
                base = days
                break
        except re.error:
            continue
    try:
        hits = int(it.get("h") or 0)
    except (TypeError, ValueError):
        hits = 0
    hl = base * (1.0 + MEM_HALF_LIFE_HITS_GAIN * hits)
    return min(MEM_HALF_LIFE_MAX, hl)


def _item_rank(it: dict, now: float | None = None) -> float:
    """生效权重：自身权重 × 时间衰减。被用过越近、越常被强化 → 越靠前。"""
    now = now or _now_ts()
    try:
        w = float(it.get("w") or MEM_WEIGHT_START)
    except (TypeError, ValueError):
        w = MEM_WEIGHT_START
    try:
        lu = float(it.get("lu") or 0.0)
    except (TypeError, ValueError):
        lu = 0.0
    if lu <= 0:
        lu = now - 86400.0 * 7  # 老数据没有 last_used：当成一周前用过，给个中性起点
    age_days = max(0.0, (now - lu) / 86400.0)
    return w * (0.5 ** (age_days / _half_life_days(it)))'''
if old_rank in s:
    s = s.replace(old_rank, new_rank, 1)
else:
    print("_item_rank 锚点没找到")

# ---------- 3) archive_text：遗忘阈值过滤（按天量化，别每轮都变） ----------
old_filter = '''    _now2 = _now_ts()
    items = [i for i in items
             if not float(i.get("exp") or 0) or float(i.get("exp")) > _now2]'''
new_filter = '''    _now2 = _now_ts()
    items = [i for i in items
             if not float(i.get("exp") or 0) or float(i.get("exp")) > _now2]
    # 艾宾浩斯遗忘：生效权重低于门槛的不再进 prompt（档案里还留着）。
    # 按"天"量化 now，保证同一天内这段文本不变（否则稳定前缀每轮都断）。
    _day = math.floor(_now2 / 86400.0) * 86400.0
    _kept, _forgotten = [], 0
    for _it in items:
        if _item_rank(_it, _day) >= MEM_DECAY_FLOOR:
            _kept.append(_it)
        else:
            _forgotten += 1
    if _forgotten:
        logger.debug(f"[记忆] {key} 有 {_forgotten} 条低于遗忘阈值，本轮不注入")
    items = _kept'''
if old_filter in s:
    s = s.replace(old_filter, new_filter, 1)
else:
    print("archive_text 过滤锚点没找到")

# 需要 math
if "\nimport math\n" not in s:
    s = s.replace("import re\n", "import math\nimport re\n", 1)

p.write_text(s, encoding="utf-8")
print("已加：分类半衰期 + 复习增益 + 遗忘阈值")
