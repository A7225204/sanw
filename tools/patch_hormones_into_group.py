import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\plugins\ai_group.py")
s = p.read_text(encoding="utf-8")
changed = []

# ---------- 1) 情绪读取改为激素层派生（对外签名不变） ----------
old_load = '''def _load_emotion() -> dict:
    vals = _read_emotion_state().get("values") or {}
    out = dict(EMOTION_BASE)
    for k in EMOTIONS:
        try:
            out[k] = max(0.0, float(vals.get(k, EMOTION_BASE[k])))
        except (TypeError, ValueError):
            pass
    return out'''
new_load = '''def _load_emotion() -> dict:
    # 内分泌层接管后：七维情绪由 5 个激素派生（见 docs/内分泌系统-方案A.md）
    try:
        from plugins import hormones
        if hormones.ENABLED:
            d = hormones.derive_emotions()
            return {k: float(d.get(k, EMOTION_BASE.get(k, 0))) for k in EMOTIONS}
    except Exception:  # noqa: BLE001
        pass
    vals = _read_emotion_state().get("values") or {}
    out = dict(EMOTION_BASE)
    for k in EMOTIONS:
        try:
            out[k] = max(0.0, float(vals.get(k, EMOTION_BASE[k])))
        except (TypeError, ValueError):
            pass
    return out'''
if old_load in s:
    s = s.replace(old_load, new_load, 1)
    changed.append("情绪读激素")

# ---------- 2) prompt 行带上激素 ----------
old_prompt = '''def emotion_prompt() -> str:
    pct = _load_emotion()
    return " ".join(f"{k}{pct[k]:.0f}%" for k in EMOTIONS)'''
new_prompt = '''def emotion_prompt() -> str:
    try:
        from plugins import hormones
        if hormones.ENABLED:
            return hormones.prompt_line()
    except Exception:  # noqa: BLE001
        pass
    pct = _load_emotion()
    return " ".join(f"{k}{pct[k]:.0f}%" for k in EMOTIONS)'''
if old_prompt in s:
    s = s.replace(old_prompt, new_prompt, 1)
    changed.append("prompt 带激素")

# ---------- 3) 事件钩子：私聊/群聊 ----------
helper = '''

def _hormone_event(name: str, scale: float = 1.0) -> None:
    """内分泌事件（失败静默，不影响主流程）。"""
    try:
        from plugins import hormones
        hormones.apply_event(name, scale)
    except Exception:  # noqa: BLE001
        pass


def _trauma_check(gid, uid: str, text: str, nickname: str) -> None:
    """群聊里检测创伤线索：命中就抬皮质醇，并把这次冒犯记进创伤库。"""
    try:
        from plugins import trauma_mem, hormones
        scope = f"group_{gid}"
        hit = trauma_mem.match(text, scope)
        if hit:
            hormones.apply_event("insult", 0.6)
            logger.info(f"[创伤] 线索命中：{hit.get('subject')} / charge={hit.get('charge')}")
        if trauma_mem.should_record(text, is_owner=False):
            item = trauma_mem.register(scope, nickname, f"{nickname}：{text[:60]}", text)
            if item:
                logger.info(f"[创伤] 记下一条：{nickname}（charge={item.get('charge')}）")
    except Exception:  # noqa: BLE001
        pass

'''
if "_hormone_event" not in s:
    anchor = "def get_relationship_prompt("
    i = s.find(anchor)
    if i > 0:
        s = s[:i] + helper.lstrip("\n") + "\n" + s[i:]
        changed.append("事件助手")

p.write_text(s, encoding="utf-8")
print("改动:", changed)
