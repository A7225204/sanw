import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\plugins\ai_group.py")
s = p.read_text(encoding="utf-8")
changed = []

# 私聊发送循环：已经发过的内容不再发（防模型自己复读）
old_priv = '''    for i, seg in enumerate(chunks):
        await private_ai.send(MessageSegment.text(seg))
        if i < len(chunks) - 1:
            await asyncio.sleep(random.uniform(gap, gap * 1.5))'''
new_priv = '''    _sent_texts: list[str] = []
    for i, seg in enumerate(chunks):
        _core = str(seg).strip(" 　。！？!?，,、；;~～…")
        if _core and any(_core in _s or _s in _core for _s in _sent_texts if len(_s) >= 6):
            logger.info(f"[去重] 跳过重复段落：{_core[:30]}")
            continue
        await private_ai.send(MessageSegment.text(seg))
        if _core:
            _sent_texts.append(_core)
        if i < len(chunks) - 1:
            await asyncio.sleep(random.uniform(gap, gap * 1.5))'''
if old_priv in s:
    s = s.replace(old_priv, new_priv, 1)
    changed.append("私聊去重")

# 群聊发送循环同样处理
old_grp = '''    for i, seg in enumerate(chunks):
        await ai.send(Message(seg))
        if i < len(chunks) - 1:
            await asyncio.sleep(random.uniform(0.8, 1.5))'''
new_grp = '''    _sent_g: list[str] = []
    for i, seg in enumerate(chunks):
        _txt_g = str(seg).strip()
        _core_g = _txt_g.strip(" 　。！？!?，,、；;~～…")
        if _core_g and any(_core_g in _s or _s in _core_g for _s in _sent_g if len(_s) >= 6):
            logger.info(f"[去重] 群聊跳过重复段落：{_core_g[:30]}")
            continue
        await ai.send(Message(seg))
        if _core_g:
            _sent_g.append(_core_g)
        if i < len(chunks) - 1:
            await asyncio.sleep(random.uniform(0.8, 1.5))'''
if old_grp in s:
    s = s.replace(old_grp, new_grp, 1)
    changed.append("群聊去重")

p.write_text(s, encoding="utf-8")
print("改动:", changed)
