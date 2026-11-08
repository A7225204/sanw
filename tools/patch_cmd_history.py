import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\plugins\ai_group.py")
s = p.read_text(encoding="utf-8")

# ---------- 1) 通用记录器：把指令往返写进私聊/群聊历史 ----------
if "def _install_cmd_history_recorder(" not in s:
    helper = '''

def _msg_text(x) -> str:
    """从 Message / MessageSegment / str 里取纯文本。"""
    try:
        if isinstance(x, str):
            return x
        segs = getattr(x, "__iter__", lambda: [])()
        out = []
        for seg in segs:
            if isinstance(seg, str):
                out.append(seg)
            else:
                d = getattr(seg, "data", None) or {}
                out.append(str(d.get("text") or ""))
        return "".join(out)
    except Exception:  # noqa: BLE001
        return ""


def _install_cmd_history_recorder(matcher, hist_gid: str, user_text: str) -> None:
    """指令类消息：把「主人说的话 + 她回的每一条」写进历史。

    以前指令回复不走聊天流程、不写历史，主人下一句"看到了"她就接不上。
    只记前 80 字，控制 token；同一句不重复记。
    """
    try:
        _hist = _history[hist_gid]
        if not any(h[0] in ("主人",) and h[1] == user_text[:80] and not h[2] for h in _hist[-4:]):
            _hist.append(("主人", user_text[:80], False))

        async def _record_and_call(orig, arg, *rest, **kw):
            txt = _msg_text(arg)[:80]
            if txt and not (txt == user_text[:80]):
                if not any(h[2] and h[1] == txt for h in _hist[-4:]):
                    _hist.append((NICKNAME, txt, True))
            return await orig(arg, *rest, **kw)

        _orig_send = matcher.send
        _orig_finish = matcher.finish

        async def _send(*a, **k):
            return await _record_and_call(_orig_send, *a, **k) if a else await _orig_send(*a, **k)

        async def _finish(*a, **k):
            return await _record_and_call(_orig_finish, *a, **k) if a else await _orig_finish(*a, **k)

        matcher.send = _send          # type: ignore[method-assign]
        matcher.finish = _finish      # type: ignore[method-assign]
    except Exception:  # noqa: BLE001
        pass
'''
    i = s.find("def note_private_exchange(")
    if i > 0:
        s = s[:i] + helper.lstrip("\n") + "\n" + s[i:]
    else:
        raise SystemExit("锚点未找到")

# ---------- 2) 在私聊入口挂上 ----------
old = '''    user_msg = event.get_plaintext().strip()
    # 指令必须带 /；不带的"像指令"内容按普通聊天处理
    user_msg, _is_cmd = _strip_cmd_prefix(user_msg)
    if not _is_cmd:
        user_msg = _maybe_passthrough(user_msg)'''
new = '''    user_msg = event.get_plaintext().strip()
    # 指令必须带 /；不带的"像指令"内容按普通聊天处理
    _raw_cmd_text = user_msg
    user_msg, _is_cmd = _strip_cmd_prefix(user_msg)
    if not _is_cmd:
        user_msg = _maybe_passthrough(user_msg)
    else:
        # 指令往返也写进历史，免得主人下一句"看到了"她接不上
        _install_cmd_history_recorder(private_ai, f"private_{user_id}", user_msg)'''
if old in s:
    s = s.replace(old, new, 1)
else:
    raise SystemExit("私聊入口未匹配")

p.write_text(s, encoding="utf-8")
print("已加：指令往返自动写入私聊历史")
