import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\plugins\ai_group.py")
s = p.read_text(encoding="utf-8")
changed = []

# ---------- 1) 群聊昵称：按 QQ 号建档，记住首名 + 最新 ----------
old_nick = '''    nickname = event.sender.card or event.sender.nickname or str(event.user_id)'''
new_nick = '''    _raw_nick = event.sender.card or event.sender.nickname or str(event.user_id)
    try:
        from plugins import nick_names as _nn
        nickname = _nn.resolve(event.group_id, event.user_id, _raw_nick)
    except Exception:  # noqa: BLE001
        nickname = _raw_nick'''
if old_nick in s and "nick_names as _nn" not in s:
    s = s.replace(old_nick, new_nick, 1)
    changed.append("群昵称建档")

# ---------- 2) 飞书：对话历史独立，不和 QQ 串 ----------
old_gid = '''    uid = str(user_id or "").strip() or str(YOUR_QQ_ID)
    gid = f"private_{uid}"
    text = (text or "").strip()
    if not text:
        return ""'''
new_gid = '''    uid = str(user_id or "").strip() or str(YOUR_QQ_ID)
    gid = f"private_{uid}"                 # 长期记忆/好感度：两边共用
    hist_gid = f"private_feishu_{uid}"     # 对话历史：飞书自己一份，不和 QQ 串
    text = (text or "").strip()
    if not text:
        return ""'''
if old_gid in s and "private_feishu_" not in s:
    s = s.replace(old_gid, new_gid, 1)
    changed.append("飞书历史独立")

# 历史读写全部换成 hist_gid
s = s.replace('''        if not _history.get(gid):
            rows = await asyncio.to_thread(get_recent_memory, 40, gid)
            for r in (rows or []):
                if r.get("message"):
                    _history[gid].append(("主人", str(r["message"])[:cfg.context.max_len_per_msg], False))
                if r.get("reply"):
                    _history[gid].append((NICKNAME, str(r["reply"])[:cfg.context.max_len_per_msg], True))''',
'''        if not _history.get(hist_gid):
            rows = await asyncio.to_thread(get_recent_memory, 40, hist_gid)
            for r in (rows or []):
                if r.get("message"):
                    _history[hist_gid].append(("主人", str(r["message"])[:cfg.context.max_len_per_msg], False))
                if r.get("reply"):
                    _history[hist_gid].append((NICKNAME, str(r["reply"])[:cfg.context.max_len_per_msg], True))''', 1)

s = s.replace('''            hist_len=len(_history[gid]),
            hist_tok=_history_tokens(_history[gid]),''',
'''            hist_len=len(_history[hist_gid]),
            hist_tok=_history_tokens(_history[hist_gid]),''', 1)
s = s.replace('''        hist = _history_messages(True, list(_history[gid]))''',
'''        hist = _history_messages(True, list(_history[hist_gid]))''', 1)
s = s.replace('''        _history[gid].append(("主人", text[:cfg.context.max_len_per_msg], False))
        _history[gid].append((NICKNAME, reply[:cfg.context.max_len_per_msg], True))''',
'''        _history[hist_gid].append(("主人", text[:cfg.context.max_len_per_msg], False))
        _history[hist_gid].append((NICKNAME, reply[:cfg.context.max_len_per_msg], True))''', 1)

p.write_text(s, encoding="utf-8")
print("改动:", changed)
