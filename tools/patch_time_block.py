import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\plugins\ai_group.py")
s = p.read_text(encoding="utf-8")

# ---------- 1) 手表助手 ----------
if "def _bj_now(" not in s:
    helper = '''

# ---------------------------------------------------------------- 手表（北京时间）
def _bj_now() -> "datetime":
    """当前北京时间（Asia/Shanghai）；没有 tzdata 时退回本机时间。"""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Shanghai"))
    except Exception:  # noqa: BLE001
        return datetime.now()


def _period_cn(hour: int) -> str:
    if hour < 5:
        return "凌晨"
    if hour < 9:
        return "早上"
    if hour < 12:
        return "上午"
    if hour < 14:
        return "中午"
    if hour < 18:
        return "下午"
    if hour < 23:
        return "晚上"
    return "深夜"


def _time_block() -> str:
    """给模型看的时间块：只认北京时间，不许自己换算/瞎猜。"""
    n = _bj_now()
    wd = "一二三四五六日"[n.weekday()]
    return (
        f"【手表】现在是北京时间 {n:%Y-%m-%d} {n:%H:%M}，星期{wd}，{_period_cn(n.hour)}。"
        "只用这个时间回答；别人问几点/今天几号/星期几就照这个说，不要猜、不要换算别的时区，"
        "也不要说自己没表。"
    )
'''
    i = s.find("def _history_messages(")
    s = s[:i] + helper.lstrip("\n") + "\n" + s[i:]

# ---------- 2) 拼消息时把时间块放最后（不打乱前面缓存） ----------
old = '''    if volatile_prompt:
        messages.append({"role": "system", "content": volatile_prompt})
    messages.append({"role": "user", "content": current_content})
    return messages'''
new = '''    if volatile_prompt:
        messages.append({"role": "system", "content": volatile_prompt})
    # 时间块放最尾巴：每轮都会变，放前面会把整段历史拖出缓存
    messages.append({"role": "system", "content": _time_block()})
    messages.append({"role": "user", "content": current_content})
    return messages'''
if old in s:
    s = s.replace(old, new, 1)

p.write_text(s, encoding="utf-8")
print("ai_group: 时间块已注入 prompt 尾部")
