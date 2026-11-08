import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\plugins\ai_group.py")
s = p.read_text(encoding="utf-8")
changed = []

# 1) 私聊入口：主人来消息 → 催产素/多巴胺变化
old_priv = '''    img = pick_emoji(mood_tag, reply) if mood_tag else None
    logger.info(f"[TTS] 私聊回复就绪 reply长度={len(reply)} is_voice={is_voice_request} mood={mood_tag!r}")'''
new_priv = '''    _hormone_event("owner_message")

    img = pick_emoji(mood_tag, reply) if mood_tag else None
    logger.info(f"[TTS] 私聊回复就绪 reply长度={len(reply)} is_voice={is_voice_request} mood={mood_tag!r}")'''
if old_priv in s and '_hormone_event("owner_message")' not in s:
    s = s.replace(old_priv, new_priv, 1)
    changed.append("私聊事件")

# 2) 群聊入口：普通互动 + 创伤线索
old_grp = '''    nickname = _nn.resolve(event.group_id, event.user_id, _raw_nick)
    except Exception:  # noqa: BLE001
        nickname = _raw_nick'''
# 上面这段实际拼写以文件为准，改用更稳的锚点
anchor = '''    if len(RECALL_CACHE) > 500: RECALL_CACHE.popitem(last=False)
    _raw_nick = event.sender.card or event.sender.nickname or str(event.user_id)'''
new_anchor = '''    if len(RECALL_CACHE) > 500: RECALL_CACHE.popitem(last=False)
    _hormone_event("group_interact")
    _trauma_check(gid, str(event.user_id), event.get_plaintext(), str(event.sender.card or event.sender.nickname or ""))
    _raw_nick = event.sender.card or event.sender.nickname or str(event.user_id)'''
if anchor in s and '_trauma_check(gid' not in s:
    s = s.replace(anchor, new_anchor, 1)
    changed.append("群聊事件+创伤检查")

p.write_text(s, encoding="utf-8")
print("改动:", changed)
