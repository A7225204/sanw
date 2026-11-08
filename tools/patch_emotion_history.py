import pathlib

# ---------- 1) hormones：每次 tick 追加一条情绪历史（供面板画曲线） ----------
p = pathlib.Path(r"D:\qq-ai-bot\plugins\hormones.py")
s = p.read_text(encoding="utf-8")

if "emotion_history.json" not in s:
    s = s.replace('FILE = ROOT / "memory" / "hormones_state.json"',
                  'FILE = ROOT / "memory" / "hormones_state.json"\nHIST_FILE = ROOT / "memory" / "emotion_history.json"\nHIST_MAX = 720  # 12 小时（每分钟 1 点）')
    add = '''


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
        pts.append({"ts": time.strftime("%H:%M"), "e": e})
        data["points"] = pts[-HIST_MAX:]
        data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        HIST_FILE.parent.mkdir(parents=True, exist_ok=True)
        HIST_FILE.write_text(_j.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
'''
    s = s.rstrip() + add
    # tick 结束时追加
    s = s.replace('''    s["_ts"] = now
    for k in HORMONES:
        s[k] = _clamp(s[k])
    _save()
    return {k: s[k] for k in HORMONES}''',
'''    s["_ts"] = now
    for k in HORMONES:
        s[k] = _clamp(s[k])
    _save()
    _append_history()
    return {k: s[k] for k in HORMONES}''')
    p.write_text(s, encoding="utf-8")
    print("hormones: 情绪历史落盘")

# ---------- 2) dashboard：aggregate_emotion 带 history + hormones ----------
d = pathlib.Path(r"D:\qq-ai-bot\dashboard\server.py")
u = d.read_text(encoding="utf-8")
if "emotion_history" not in u:
    u = u.replace('''def aggregate_emotion() -> dict[str, Any]:
    data = _read_json(ROOT / "memory" / "emotion_state.json")''',
'''def aggregate_emotion() -> dict[str, Any]:
    data = _read_json(ROOT / "memory" / "emotion_state.json")
    # 趋势曲线 + 当前激素（新面板用）
    hist = _read_json(ROOT / "memory" / "emotion_history.json")
    horm = _read_json(ROOT / "memory" / "hormones_state.json")
    try:
        data["history"] = (hist.get("points") or [])[-240:]
    except Exception:  # noqa: BLE001
        data["history"] = []
    try:
        data["hormones"] = (horm.get("values") or {})
    except Exception:  # noqa: BLE001
        data["hormones"] = {}''', 1)
    d.write_text(u, encoding="utf-8")
    print("dashboard: 情绪接口带 history/hormones")
