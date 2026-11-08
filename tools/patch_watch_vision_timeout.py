import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\plugins\game_watch.py")
s = p.read_text(encoding="utf-8")
changed = []

# 1) 识图前先释放 ComfyUI 显存（否则 QwenVL 挤不进去，任务会卡住）
helper = '''

async def _free_comfy() -> None:
    """识图前把 ComfyUI 里别的模型卸掉——不然 QwenVL 没显存，任务会挂几分钟。"""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=20) as c:
            await c.post("http://127.0.0.1:8188/free",
                         json={"unload_models": True, "free_memory": True})
        await asyncio.sleep(2)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[看游戏] 释放显存失败：{type(e).__name__}: {e}")

'''
if "_free_comfy" not in s:
    s = s.replace("async def _comment_on_screen()", helper.lstrip("\n") + "\nasync def _comment_on_screen()", 1)
    changed.append("释放显存")

# 2) 本地识图加超时，卡住就跳过，别让用户干等
s = s.replace('''    for fn in ("_local_qwen_vision", "_sf_vision", "_ark_vision"):
        f = getattr(ai_group, fn, None)
        if not f:
            continue
        try:
            txt = await f(buf, _COMMENT_PROMPT)''',
'''    await _free_comfy()
    for fn in ("_local_qwen_vision", "_sf_vision", "_ark_vision"):
        f = getattr(ai_group, fn, None)
        if not f:
            continue
        try:
            txt = await asyncio.wait_for(f(buf, _COMMENT_PROMPT), timeout=100)''')

# 3) 点评/围观先回一句"在看"，别让人以为没反应
s = s.replace('''        _last_comment = time.time()
        txt = await _comment_on_screen()
        await watch_cmd.finish(txt or "(*眯眼*) 屏幕黑乎乎的，看不清。")''',
'''        _last_comment = time.time()
        await watch_cmd.send("(*眯起眼睛*) 让本鱼瞅一眼…")
        txt = await _comment_on_screen()
        await watch_cmd.finish(txt or "(*眯眼*) 屏幕黑乎乎的，看不清。")''')

p.write_text(s, encoding="utf-8")
print("改动:", changed)
