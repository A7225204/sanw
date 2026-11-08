import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\plugins\ai_group.py")
s = p.read_text(encoding="utf-8")
changed = []

# ---------- 1) 私聊：分条改成人味整形 + 补一句 ----------
old_private = '''    chunks = split_reply(reply)
    try:
        gap = float(cfg.reply.line_gap) if cfg.reply.line_gap is not None else 0.8
    except Exception:
        gap = 0.8
    gap = max(0.6, gap)

    for i, seg in enumerate(chunks):
        await private_ai.send(MessageSegment.text(seg))
        if i < len(chunks) - 1:
            await asyncio.sleep(random.uniform(gap, gap * 1.5))

    if img: await private_ai.send(MessageSegment.image(img))'''

new_private = '''    try:
        from plugins import human_touch
        chunks = human_touch.shape(reply)
        _followup = human_touch.maybe_followup(reply)
    except Exception:  # noqa: BLE001
        chunks = split_reply(reply)
        _followup = None
    try:
        gap = float(cfg.reply.line_gap) if cfg.reply.line_gap is not None else 0.8
    except Exception:
        gap = 0.8
    gap = max(0.6, gap)

    for i, seg in enumerate(chunks):
        await private_ai.send(MessageSegment.text(seg))
        if i < len(chunks) - 1:
            await asyncio.sleep(random.uniform(gap, gap * 1.5))

    if img: await private_ai.send(MessageSegment.image(img))

    # 偶尔隔一两秒再补一句，像真人聊着聊着想起来
    if _followup:
        await asyncio.sleep(random.uniform(1.2, 2.6))
        try:
            await private_ai.send(MessageSegment.text(_followup))
        except Exception:  # noqa: BLE001
            pass'''

if old_private in s:
    s = s.replace(old_private, new_private, 1)
    changed.append("私聊")
else:
    print("私聊锚点没找到")

# ---------- 2) 群聊：同样整形 + 补一句 ----------
old_group = '''    chunks = build_chunks(split_reply(reply), img, [MessageSegment.at(event.user_id)] if cfg.reply.'''
new_group = '''    try:
        from plugins import human_touch
        _shaped = human_touch.shape(reply)
        _followup = human_touch.maybe_followup(reply, chance=0.08)
    except Exception:  # noqa: BLE001
        _shaped = split_reply(reply)
        _followup = None
    chunks = build_chunks(_shaped, img, [MessageSegment.at(event.user_id)] if cfg.reply.'''
if old_group in s:
    s = s.replace(old_group, new_group, 1)
    changed.append("群聊分条")

# 群聊补一句：在最后的发送循环后追加
old_group_tail = '''    for i, seg in enumerate(chunks):
        await ai.send(Message(seg))
        if i < len(chunks) - 1: await asyncio.sleep(random.uniform(0.8, 1.5))'''
new_group_tail = '''    for i, seg in enumerate(chunks):
        await ai.send(Message(seg))
        if i < len(chunks) - 1: await asyncio.sleep(random.uniform(0.8, 1.5))

    if _followup:
        await asyncio.sleep(random.uniform(1.2, 2.6))
        try:
            await ai.send(Message(MessageSegment.text(_followup)))
        except Exception:  # noqa: BLE001
            pass'''
if old_group_tail in s:
    s = s.replace(old_group_tail, new_group_tail, 1)
    changed.append("群聊补一句")
else:
    print("群聊尾部锚点没找到")

p.write_text(s, encoding="utf-8")
print("改动:", changed)
