import pathlib

# 1) 游戏点评走在线识图，其他场景保持本地优先
p = pathlib.Path(r"D:\qq-ai-bot\plugins\game_watch.py")
s = p.read_text(encoding="utf-8")

old = '''    await _free_comfy()
    for fn in ("_local_qwen_vision", "_sf_vision", "_ark_vision"):'''
new = '''    # 打游戏这条固定走在线（快、不占显存、不耽误出图）；其他场景仍本地优先
    order = os.getenv("GAME_WATCH_VISION", "online").strip().lower()
    if order == "local":
        await _free_comfy()
        channels = ("_local_qwen_vision", "_ark_vision", "_sf_vision")
    else:
        channels = ("_ark_vision", "_sf_vision", "_local_qwen_vision")
    for fn in channels:'''
if old in s:
    s = s.replace(old, new, 1)
    # 本地兜底时再释放显存
    s = s.replace('''        f = getattr(ai_group, fn, None)
        if not f:
            continue
        try:
            txt = await asyncio.wait_for(f(buf, _COMMENT_PROMPT), timeout=100)''',
'''        f = getattr(ai_group, fn, None)
        if not f:
            continue
        if fn == "_local_qwen_vision":
            await _free_comfy()   # 只有要走本地才腾显存
        try:
            txt = await asyncio.wait_for(f(buf, _COMMENT_PROMPT), timeout=100)''', 1)
    p.write_text(s, encoding="utf-8")
    print("game_watch: 游戏时走在线识图")
else:
    print("game_watch: 未匹配")

# 2) 火山识图每日上限 20 → 60（游戏点评会消耗）
e = pathlib.Path(r"D:\qq-ai-bot\.env")
t = e.read_text(encoding="utf-8")
if "ARK_VISION_DAILY_LIMIT=20" in t:
    t = t.replace("ARK_VISION_DAILY_LIMIT=20", "ARK_VISION_DAILY_LIMIT=60")
    e.write_text(t, encoding="utf-8")
    print(".env: 火山识图上限 20 → 60")
else:
    print(".env: 上限已是别的值", [l for l in t.splitlines() if "ARK_VISION_DAILY_LIMIT" in l])
