import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\plugins\ai_group.py")
s = p.read_text(encoding="utf-8")

# 画图别名：画图 / 画画 / 画一张 / 帮我画 / 给我画
helper = '''

_DRAW_ALIASES = ("画图", "画画", "画一张", "画个图", "帮我画", "给我画", "画幅画")


def _draw_prompt_of(text: str) -> str | None:
    """把「画图/画画 xxx」里的 xxx 拆出来；不是画图指令返回 None。"""
    t = str(text or "").strip()
    for _kw in _DRAW_ALIASES:
        if t.startswith(_kw):
            rest = t[len(_kw):].strip(" ：:，,")
            # 单独一个"画图"没内容时，让调用方当普通聊天
            return rest if rest else ""
    return None

'''
if "_draw_prompt_of" not in s:
    i = s.find("def _self_draw_action(")
    s = s[:i] + helper.lstrip("\n") + "\n" + s[i:]

# 私聊分支
old_priv = '''    elif user_msg.startswith("画图"):
        pure_prompt = user_msg[2:].strip()
        _draw_mode = "verbatim"'''
new_priv = '''    elif _draw_prompt_of(user_msg) is not None:
        pure_prompt = _draw_prompt_of(user_msg)
        _draw_mode = "verbatim"'''
if old_priv in s:
    s = s.replace(old_priv, new_priv, 1)
else:
    raise SystemExit("私聊画图分支未匹配")

# 群聊分支
old_grp = '''        elif user_msg.startswith("画图"):
            pure_prompt = user_msg[2:].strip(); _draw_mode = "verbatim"'''
new_grp = '''        elif _draw_prompt_of(user_msg) is not None:
            pure_prompt = _draw_prompt_of(user_msg); _draw_mode = "verbatim"'''
if old_grp in s:
    s = s.replace(old_grp, new_grp, 1)
else:
    raise SystemExit("群聊画图分支未匹配")

# 飞书画图分支
old_fs = '''    if text.startswith("画图"):
        _draw_prompt = text[2:].strip(" ：:，,")
        _draw_mode = "verbatim"'''
new_fs = '''    if _draw_prompt_of(text) is not None:
        _draw_prompt = _draw_prompt_of(text) or ""
        _draw_mode = "verbatim"'''
if old_fs in s:
    s = s.replace(old_fs, new_fs, 1)

p.write_text(s, encoding="utf-8")
print("ai_group: 画图别名（画画等）已支持")
