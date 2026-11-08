import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\plugins\game_watch.py")
s = p.read_text(encoding="utf-8")

# apscheduler 的 interval 任务在这个插件里没跑起来，改成 driver.on_startup 起一个 asyncio 轮询任务，最稳。
old = '''@scheduler.scheduled_job("interval", seconds=5, id="game_watch_cmd")
async def _poll_panel_cmd() -> None:
    cmd = _read_cmd()
    if not cmd:
        return
    if cmd in ("start", "on", "begin"):
        logger.info("[看游戏] 面板指令：开始")
        _start_watch("panel")
    elif cmd in ("stop", "off", "end"):
        logger.info("[看游戏] 面板指令：停止")
        _stop_watch()'''

new = '''async def _poll_panel_cmd() -> None:
    """面板指令轮询：每 5 秒看一次 memory/game_watch_cmd.json。"""
    while True:
        try:
            cmd = _read_cmd()
            if cmd in ("start", "on", "begin"):
                logger.info("[看游戏] 面板指令：开始")
                _start_watch("panel")
            elif cmd in ("stop", "off", "end"):
                logger.info("[看游戏] 面板指令：停止")
                _stop_watch()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[看游戏] 指令轮询异常：{type(e).__name__}: {e}")
        await asyncio.sleep(5)


@get_driver().on_startup
async def _start_panel_poller() -> None:
    asyncio.get_event_loop().create_task(_poll_panel_cmd())
    logger.info("[看游戏] 面板指令轮询已启动（5 秒一次）")'''

if old in s:
    s = s.replace(old, new, 1)
    s = s.replace("from nonebot import get_bots, on_message",
                  "from nonebot import get_bots, get_driver, on_message", 1)
    s = s.replace("from nonebot_plugin_apscheduler import scheduler\n", "")
    p.write_text(s, encoding="utf-8")
    print("已改为 driver.on_startup 轮询")
else:
    print("未匹配")
