import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\plugins\game_watch.py")
s = p.read_text(encoding="utf-8")

s = s.replace('''        try:
            txt = await f(buf, _COMMENT_PROMPT)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[看游戏] {fn} 异常：{type(e).__name__}: {e}")
            continue
        txt = str(txt or "").strip()
        if len(txt) >= 6:
            return txt
    return ""''',
'''        try:
            txt = await f(buf, _COMMENT_PROMPT)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[看游戏] {fn} 异常：{type(e).__name__}: {e}")
            continue
        txt = str(txt or "").strip()
        logger.info(f"[看游戏] {fn} 返回 {len(txt)} 字：{txt[:60]}")
        if len(txt) >= 6:
            return txt
    logger.warning("[看游戏] 所有识图通道都没出结果（硅基/本地/火山）")
    return ""''')

s = s.replace('''async def _deliver(text: str) -> None:
    if not text:
        return
    for bot in get_bots().values():
        try:
            await bot.send_private_msg(user_id=OWNER_UID, message=MessageSegment.text(text))
            break''',
'''async def _deliver(text: str) -> None:
    if not text:
        return
    for bot in get_bots().values():
        try:
            await bot.send_private_msg(user_id=OWNER_UID, message=MessageSegment.text(text))
            logger.info(f"[看游戏] 已发私聊：{text[:40]}")
            break''')

s = s.replace('''    if any(k in t for k in ("看我打游戏", "看我玩", "围观我", "开始看游戏")):''',
'''    if any(k in t for k in ("看我打游戏", "看我玩", "围观我", "开始看游戏")):
        logger.info(f"[看游戏] 收到启动指令（{event.get_session_id()}）")''')

p.write_text(s, encoding="utf-8")
print("已加详细日志：启动/识图各通道/投递")
