import pathlib

# ---------- 1) bilibili_daily：加一个"正在看视频"的全局标记 ----------
p = pathlib.Path(r"D:\qq-ai-bot\plugins\bilibili_daily.py")
s = p.read_text(encoding="utf-8")
if "def watching(" not in s:
    helper = '''

_WATCH_UNTIL = 0.0


def watching() -> bool:
    """是不是正在看 B 站视频（给别的模块判断要不要抢活动状态用）。"""
    return time.time() < _WATCH_UNTIL

'''
    i = s.find("async def _summarize(")
    s = s[:i] + helper.lstrip("\n") + "\n" + s[i:]

    # watch() 里设置/清除
    s = s.replace('''    from plugins import media_fetch
    try:
        from plugins.activity import set_activity
        set_activity("看B站视频中", 1800)
    except Exception:  # noqa: BLE001
        pass''',
'''    from plugins import media_fetch
    global _WATCH_UNTIL
    _WATCH_UNTIL = time.time() + 1800
    try:
        from plugins.activity import set_activity
        set_activity("看B站视频中", 1800)
    except Exception:  # noqa: BLE001
        pass''')
    s = s.replace('''    try:
        from plugins.activity import set_activity
        set_activity("刚看完B站视频", 180)
    except Exception:  # noqa: BLE001
        pass''',
'''    _WATCH_UNTIL = 0.0
    try:
        from plugins.activity import set_activity
        set_activity("刚看完B站视频", 180)
    except Exception:  # noqa: BLE001
        pass''')
    p.write_text(s, encoding="utf-8")
    print("bilibili_daily: 加 watching() 标记")

# ---------- 2) ai_group：看片期间别让群聊/私聊把"看B站视频中"顶掉 ----------
g = pathlib.Path(r"D:\qq-ai-bot\plugins\ai_group.py")
t = g.read_text(encoding="utf-8")
changed = []

old_group = '''    if not _private_chat_active() and not _ask_where_raw:
        set_activity("QQ群聊天中", 120, f"群 {gid}")'''
new_group = '''    if not _private_chat_active() and not _ask_where_raw and not _bili_watching():
        set_activity("QQ群聊天中", 120, f"群 {gid}")'''
if old_group in t:
    t = t.replace(old_group, new_group, 1)
    changed.append("群聊")

old_priv = '''    set_activity("和主人聊天中", 120)'''
new_priv = '''    if not _bili_watching():
        set_activity("和主人聊天中", 120)'''
if old_priv in t:
    t = t.replace(old_priv, new_priv, 1)
    changed.append("私聊")

# 辅助函数
if "_bili_watching" in t and "def _bili_watching(" not in t:
    helper2 = '''

def _bili_watching() -> bool:
    """她正在看 B 站视频时，别被聊天状态覆盖（看片可能好几分钟）。"""
    try:
        from plugins import bilibili_daily
        return bilibili_daily.watching()
    except Exception:  # noqa: BLE001
        return False

'''
    i = t.find("def get_relationship_prompt(")
    t = t[:i] + helper2.lstrip("\n") + "\n" + t[i:]

g.write_text(t, encoding="utf-8")
print("ai_group: 状态优先级已加", changed)
