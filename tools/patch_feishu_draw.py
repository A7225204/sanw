import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\plugins\ai_group.py")
s = p.read_text(encoding="utf-8")

if "async def _feishu_draw" in s:
    print("已支持飞书画图")
    raise SystemExit

helper = '''

async def _feishu_draw(prompt: str, mode: str) -> None:
    """飞书里的画图：后台出图，出完直接把图发到飞书。"""
    try:
        from plugins import feishu_bridge
        img, src = await _draw_chain(prompt, mode=mode)
        if not img:
            await asyncio.to_thread(feishu_bridge.notify_owner, "(*甩笔*) 这次没画出来，再喊一次试试。")
            return
        await asyncio.to_thread(feishu_bridge.send_image_bytes, img, "")
        await asyncio.to_thread(feishu_bridge.notify_owner,
                                f"(*擦笔*) 画好了（{src}）。")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[飞书] 画图失败：{type(e).__name__}: {e}")
        try:
            from plugins import feishu_bridge
            await asyncio.to_thread(feishu_bridge.notify_owner, "(*耷拉尾巴*) 画图出错了。")
        except Exception:  # noqa: BLE001
            pass

'''
anchor = "async def feishu_chat(user_id: str, text: str) -> tuple[str, str]:"
i = s.find(anchor)
if i < 0:
    print("feishu_chat 没找到")
    raise SystemExit
s = s[:i] + helper.lstrip("\n") + "\n" + s[i:]

# 在 feishu_chat 里识别画图指令，后台出图并立刻回一句
old = '''    uid = str(user_id or "").strip() or str(YOUR_QQ_ID)
    gid = f"private_{uid}"                 # 长期记忆/好感度：两边共用
    hist_gid = f"private_feishu_{uid}"     # 对话历史：飞书自己一份，不和 QQ 串
    text = (text or "").strip()
    if not text:
        return ""'''
new = '''    uid = str(user_id or "").strip() or str(YOUR_QQ_ID)
    gid = f"private_{uid}"                 # 长期记忆/好感度：两边共用
    hist_gid = f"private_feishu_{uid}"     # 对话历史：飞书自己一份，不和 QQ 串
    text = (text or "").strip()
    if not text:
        return ""

    # 飞书也能画图：后台出图，先把确认回去，画完直接发图
    _draw_prompt = ""
    _draw_mode = ""
    if text.startswith("画图"):
        _draw_prompt = text[2:].strip(" ：:，,")
        _draw_mode = "verbatim"
    elif "画你自己" in text or "画个你" in text:
        _draw_prompt = text
        _draw_mode = "self"
    if _draw_prompt and _draw_mode:
        asyncio.create_task(_feishu_draw(_draw_prompt, _draw_mode))
        return "(*拿起画笔*) 好，本鱼画了，画完直接发你（大概 2~3 分钟）。", ""'''
if old in s:
    s = s.replace(old, new, 1)
    p.write_text(s, encoding="utf-8")
    print("飞书已支持画图（后台出图，不阻塞）")
else:
    print("feishu_chat 开头没匹配上")
