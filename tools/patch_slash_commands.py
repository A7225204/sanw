import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\plugins\ai_group.py")
s = p.read_text(encoding="utf-8")
changed = []

helper = '''

# ---------------------------------------------------------------- 指令前缀（/）
# 主人定：所有"强制指令"必须带 / 前缀，否则按普通聊天处理，
# 避免"画图""唱歌""别看了"这类词在正常对话里被误判。
COMMAND_WORDS = (
    "画图", "画你自己", "画个你", "改图", "编辑图片", "修图", "P图", "p图",
    "唱歌", "点歌", "重唱", "签到", "赛马", "逮捕", "恶搞", "学", "复习",
    "看我打游戏", "看我玩", "围观我", "开始看游戏", "点评一下", "点评我",
    "看我操作", "截个图看看", "别看了", "不看了", "没打游戏",
    "状态", "好感度", "频率", "回复频率", "记忆", "人设", "画图设定", "人设词",
    "出图设定", "测试", "回滚", "重启",
)


def _strip_cmd_prefix(text: str) -> tuple[str, bool]:
    """返回 (去掉 / 之后的正文, 是否是指令)。

    带 / 才算指令；不带的原样返回并标记 False。
    """
    t = str(text or "")
    if t.startswith(("/", "／")):
        return t.lstrip("/／").strip(), True
    return t, False


def _maybe_passthrough(text: str) -> str:
    """不带 / 但长得像指令 → 加零宽前缀，让所有 startswith 判定失效（按聊天走）。"""
    t = str(text or "")
    if t[:1] in ("/", "／"):
        return t
    for w in COMMAND_WORDS:
        if t.startswith(w):
            return "\\u200b" + t
    return t

'''
if "_strip_cmd_prefix" not in s:
    anchor = "def _split_rate_command("
    i = s.find(anchor)
    if i > 0:
        s = s[:i] + helper.lstrip("\n") + "\n" + s[i:]
        changed.append("指令助手")

# 私聊入口：先解析 /，再决定是否当指令
old_priv = '''    user_msg = event.get_plaintext().strip()
    images = [seg.data.get("url") or seg.data.get("file") for seg in event.get_message() if seg.type == "image"'''
new_priv = '''    user_msg = event.get_plaintext().strip()
    # 指令必须带 /；不带的"像指令"内容按普通聊天处理
    user_msg, _is_cmd = _strip_cmd_prefix(user_msg)
    if not _is_cmd:
        user_msg = _maybe_passthrough(user_msg)
    images = [seg.data.get("url") or seg.data.get("file") for seg in event.get_message() if seg.type == "image"'''
if old_priv in s:
    s = s.replace(old_priv, new_priv, 1)
    changed.append("私聊前缀")

# 群聊入口
old_grp = '''    user_msg = re.sub(rf"@{NICKNAME}\\s*", "", raw_text, count=1).strip()
    if not user_msg: user_msg = re.sub(r"^@\\S+\\s*", "", raw_text).strip()'''
new_grp = '''    user_msg = re.sub(rf"@{NICKNAME}\\s*", "", raw_text, count=1).strip()
    if not user_msg: user_msg = re.sub(r"^@\\S+\\s*", "", raw_text).strip()
    # 群里同样：只有 /指令 才算指令
    user_msg, _is_cmd_g = _strip_cmd_prefix(user_msg)
    if not _is_cmd_g:
        user_msg = _maybe_passthrough(user_msg)'''
if old_grp in s:
    s = s.replace(old_grp, new_grp, 1)
    changed.append("群聊前缀")

p.write_text(s, encoding="utf-8")
print("改动:", changed)
