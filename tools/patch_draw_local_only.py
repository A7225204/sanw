import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\plugins\ai_group.py")
s = p.read_text(encoding="utf-8")
changed = []

# 1) 本地出图等待时间 180s → 600s（本地其实能画完，只是之前放弃太早）
n = s.count("deadline = time.time() + 180")
s = s.replace("deadline = time.time() + 180", "deadline = time.time() + 600")
if n:
    changed.append(f"本地等待 {n} 处 → 600s")

# 2) 画图只走本地：_draw_chain 加 local_only 开关，画图调用时开启
old_chain = '''    img = await _local_zimage(prompt, width, height, mode=mode)
    if img:
        return img, "local"
    img = await _sf_image(prompt, f"{width}x{height}")'''
new_chain = '''    img = await _local_zimage(prompt, width, height, mode=mode)
    if img:
        return img, "local"
    if os.getenv("DRAW_LOCAL_ONLY", "1").lower() in ("1", "true", "yes"):
        logger.warning("[出图] 画图只走本地，云端已被关闭（要开云端设 DRAW_LOCAL_ONLY=0）")
        return None, ""
    img = await _sf_image(prompt, f"{width}x{height}")'''
if old_chain in s:
    s = s.replace(old_chain, new_chain, 1)
    changed.append("画图仅本地")

# 3) 干掉画图后面的豆包兜底（那段只有一个用途：本地失败时调 ark 出图）
old_doubao = '''        try:
            r = await http_client.post(
                "https://ark.cn-beijing.volces.com/api/v3/images/generations",
                headers={"Authorization": f"Bearer {DOUBAO_DRAW_API_KEY}"},
                json={"model": DOUBAO_IMAGE_MODEL, "prompt": pure_prompt, "response_format": "url", "size": "1024x1024"}
            )
            res = r.json()
            if "data" in res and len(res["data"]) > 0:
                await private_ai.send(MessageSegment.image(res["data"][0]["url"]))
                await _record_bot_image(gid, "主人", user_id, user_msg, pure_prompt, gid)
                return
            await private_ai.finish(f"豆包画图失败：{res}")
        except Exception as e: await private_ai.finish(f"画图出错：{e}")
        return'''
new_doubao = '''        await private_ai.finish("(*甩笔*) 本地这次没画出来，再喊一次「画图 …」吧，本鱼重画。")
        return'''
if old_doubao in s:
    s = s.replace(old_doubao, new_doubao, 1)
    changed.append("去掉豆包兜底")

p.write_text(s, encoding="utf-8")
print("改动:", changed)
print("DRAW_LOCAL_ONLY 默认:", "1（仅本地）")
