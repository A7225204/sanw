import pathlib

# ---------- 1) desire.py：冲动改成激素派生（A 方案） ----------
p = pathlib.Path(r"D:\qq-ai-bot\desire.py")
s = p.read_text(encoding="utf-8")

if "def _urge_from_hormones(" not in s:
    helper = '''

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
'''
    s = s.replace('''def urge(tired: float | None = None) -> float:''', helper.lstrip("\n") + "\n\ndef urge(tired: float | None = None) -> float:", 1)

# urge 内部改成先试激素
s = s.replace('''def urge(tired: float | None = None) -> float:
    """想主动说话的冲动 0~1：想念和无聊推高，太累压制（疲劳由身体层传入）。"""
    d = tick()''',
'''def urge(tired: float | None = None) -> float:
    """想主动说话的冲动 0~1。

    A 方案：由激素派生（亲密缺口/多巴胺/压力/血清素/褪黑素）；
    激素不可用时回退到旧的想念/无聊算法。
    """
    _h = _urge_from_hormones(tired)
    if _h is not None:
        return _h
    d = tick()''')

p.write_text(s, encoding="utf-8")
print("desire.py: 冲动改为激素派生（保留旧算法兜底）")

# ---------- 2) hormones.py：压一下事件增量，别让 SER/OXY 顶死 ----------
h = pathlib.Path(r"D:\qq-ai-bot\plugins\hormones.py")
t = h.read_text(encoding="utf-8")
old_events = '''EVENTS = {
    "owner_message": (6, -10, 2, 12, 0),
    "group_interact": (2, 1, 1, 1, 0),
    "insult": (-4, 15, -5, -3, 0),
    "praise": (10, -4, 5, 3, 0),
    "meal": (3, -3, 8, 4, 0),
    "sleep": (-5, -8, 6, 0, 20),
    "wake": (4, -2, 2, 0, -20),
    "art_success": (12, -2, 3, 2, 0),
    "repair": (2, -6, 6, 4, 0),
}'''
new_events = '''# 事件增量：原来的值（OXY+12/DA+6）远大于半衰期回落，
# 聊几轮就把 SER/OXY 顶到 100、失去区分度。这里整体调小。
EVENTS = {
    "owner_message": (4, -10, 1, 7, 0),
    "group_interact": (1, 1, 1, 1, 0),
    "insult": (-3, 12, -4, -3, 0),
    "praise": (6, -4, 3, 2, 0),
    "meal": (2, -3, 5, 3, 0),
    "sleep": (-5, -8, 5, 0, 20),
    "wake": (4, -2, 2, 0, -20),
    "art_success": (8, -2, 2, 2, 0),
    "repair": (2, -6, 5, 3, 0),
}'''
if old_events in t:
    t = t.replace(old_events, new_events, 1)
    h.write_text(t, encoding="utf-8")
    print("hormones.py: 事件增量调小（防顶死）")
