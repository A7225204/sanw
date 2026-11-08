import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\plugins\game_watch.py")
s = p.read_text(encoding="utf-8")
changed = []

# 1) 群里也能触发（只认主人），handler 改成通用 MessageEvent
s = s.replace(
    "from nonebot.adapters.onebot.v11 import MessageSegment, PrivateMessageEvent",
    "from nonebot.adapters.onebot.v11 import MessageEvent, MessageSegment")
s = s.replace("async def _(event: PrivateMessageEvent, text: str = EventPlainText()):",
              "async def _(event: MessageEvent, text: str = EventPlainText()):")
changed.append("支持群聊触发")

# 2) 退出关键词扩展
s = s.replace(
    'if any(k in t for k in ("别看了", "不用看了", "停止看", "关掉围观", "别围观")):',
    'if any(k in t for k in ("别看了", "不用看了", "停止看", "关掉围观", "别围观",\n'
    '                            "没打游戏", "没在打", "不打了", "不玩了", "没玩了", "没玩", "退出了")):')
changed.append("退出关键词")

# 3) 连续跳过就自动退出，别一直空转
s = s.replace('''async def _watch_loop() -> None:
    global _watch_stop
    rounds = 0
    _watch_stop = False''',
'''async def _watch_loop() -> None:
    global _watch_stop
    rounds = 0
    skips = 0
    _watch_stop = False''')

s = s.replace('''        if txt:
            await _deliver(txt)
        else:
            logger.info("[看游戏] 这轮没截到或没识别出来")''',
'''        if txt:
            skips = 0
            await _deliver(txt)
        else:
            skips += 1
            logger.info(f"[看游戏] 这轮没截到或没识别出来（连续 {skips} 次）")
            if skips >= 3:
                logger.info("[看游戏] 连续 3 轮看不到游戏画面，自动退出围观")
                await _deliver("(*眯眼*) 屏幕上找不着游戏，本鱼先不看了。要再叫我看就说一声。")
                _watch_stop = True
                break''')

# 4) 启动/退出回复按当前会话走（群/私聊都能回）
s = s.replace('''    if any(k in t for k in ("看我打游戏", "看我玩", "围观我", "开始看游戏")):
        if _watch_task and not _watch_task.done():
            await watch_cmd.finish("(*早就坐好了*) 本鱼已经在看了。")
        _watch_task = asyncio.create_task(_watch_loop())
        await watch_cmd.finish()''',
'''    if any(k in t for k in ("看我打游戏", "看我玩", "围观我", "开始看游戏")):
        if _watch_task and not _watch_task.done():
            await watch_cmd.finish("(*早就坐好了*) 本鱼已经在看了。")
        _watch_task = asyncio.create_task(_watch_loop())
        await watch_cmd.finish("(*搬小板凳*) 好，本鱼这就盯着你打。不想看了说「没打游戏」。")''')

p.write_text(s, encoding="utf-8")
print("改动:", changed)
