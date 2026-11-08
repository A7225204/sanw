import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\plugins\ai_group.py")
s = p.read_text(encoding="utf-8")

# 1) 标点回复改成现场生成
if "def _punct_reply(" not in s:
    helper = '''

_PUNCT_LAST: dict = defaultdict(list)      # gid -> [最近几句标点回复]
_PUNCT_COOLDOWN: dict = defaultdict(float) # gid -> 下次允许时间
_PUNCT_FALLBACK = (
    "(*尾巴一歪*) 光甩个问号，本鱼又不是你肚子里的蛔虫。",
    "这符号打得挺有气势的，可惜本鱼读不出来。",
    "(*) 你这一下比群里刷屏还神秘。",
    "嗯？有想法就直说，别拿标点糊弄本鱼。",
    "本鱼猜你在打哑谜，可惜今天没心情解谜。",
)


async def _punct_reply(gid, text: str, uid: str) -> str:
    """群里有人只发标点时的回复：现场生成，不重复固定句。"""
    hint = random.choice((
        "可以吐槽他没头没尾", "可以反问他想干嘛", "可以装傻说看不出来",
        "可以阴阳他一句，但别脏话", "可以顺口提一句你现在在干嘛",
    ))
    recent = "；".join(m[1][:20] for m in list(_history[gid])[-3:]) or "（没上下文）"
    last = "；".join(_PUNCT_LAST[gid][-3:]) or "（无）"
    prompt = (
        "你在 QQ 群里，是「蓝色大肥鱼」——圆滚滚的蓝色鲸鱼拟人少女，傲娇嘴损但心软，"
        "说话短、口语、有脾气，不自称 AI。"
        f"群里有人（不是主人）只发了一个标点：{text!r}，没有任何文字内容。"
        f"最近群里说过：{recent}\\n"
        f"你之前已经说过这些了，别再重复：{last}\\n"
        f"这次{hint}。"
        "要求：10~25 字，一句话，不要括号里的动作描写也可以有 1 处，"
        "不要总是问『什么意思』，不要解释规则，只输出正文。"
    )
    try:
        out = await call_ai_raw(prompt, GROUP_AI_STATE.get(gid, "deepseek"), temperature=1.0, max_tokens=120)
        out = str(out or "").strip().strip('"').replace("\\n", " ")
        if out and "AI 调用失败" not in out:
            for old in _PUNCT_LAST[gid]:
                if _bigram_sim(old, out) > 0.75:   # 太像上一句就换掉
                    out = ""
                    break
            if out:
                _PUNCT_LAST[gid] = (_PUNCT_LAST[gid] + [out])[-5:]
                return out[:60]
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[群聊] 标点回复生成失败：{type(e).__name__}: {e}")
    out = random.choice(_PUNCT_FALLBACK)
    _PUNCT_LAST[gid] = (_PUNCT_LAST[gid] + [out])[-5:]
    return out


def _bigram_sim(a: str, b: str) -> float:
    ga = {a[i:i + 2] for i in range(max(0, len(a) - 1))}
    gb = {b[i:i + 2] for i in range(max(0, len(b) - 1))}
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / max(1, min(len(ga), len(gb)))

'''
    i = s.find("def _install_cmd_history_recorder(")
    s = s[:i] + helper.lstrip("\n") + "\n" + s[i:]

# 2) 替换固定回复
old = '''        if text in ["？", "?", "。", "。。。", "……"]:
            await get_bot().send_group_msg(group_id=gid, message="发个标点符号是什么意思？看不起本鱼？")
            return'''
new = '''        if text in ("？", "?", "。", "。。。", "……", "！", "!", "？？", "??", "…"):
            # 只发标点：现场按她的想法回，不再用固定一句；同群 45 秒内只接一次
            if time.time() >= _PUNCT_COOLDOWN[gid]:
                _PUNCT_COOLDOWN[gid] = time.time() + 45
                await get_bot().send_group_msg(group_id=gid, message=await _punct_reply(gid, text, str(user_id)))
            return'''
if old in s:
    s = s.replace(old, new, 1)
else:
    raise SystemExit("标点分支锚点未匹配")

p.write_text(s, encoding="utf-8")
print("ai_group: 标点回复改为现场生成")
