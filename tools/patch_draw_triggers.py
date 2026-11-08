import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\plugins\ai_group.py")
s = p.read_text(encoding="utf-8")

# 1) 读触发词的辅助函数
if "def _draw_self_triggers(" not in s:
    helper = '''

_DRAW_SELF_TRIGGERS_FILE = ROOT / "memory" / "draw_self_triggers.txt"
_DRAW_SELF_TRIGGERS_CACHE: str | None = None


def _draw_self_triggers() -> str:
    """LoRA 触发词（画你自己时垫在最前面，保证是她的形象）。"""
    global _DRAW_SELF_TRIGGERS_CACHE
    if _DRAW_SELF_TRIGGERS_CACHE is not None:
        return _DRAW_SELF_TRIGGERS_CACHE
    try:
        txt = _DRAW_SELF_TRIGGERS_FILE.read_text(encoding="utf-8").strip()
    except Exception:  # noqa: BLE001
        txt = ""
    txt = "，".join(x.strip() for x in txt.replace("\\n", "，").replace("、", "，").split("，") if x.strip())
    _DRAW_SELF_TRIGGERS_CACHE = txt
    return txt


'''
    i = s.find("def _draw_char_desc(")
    s = s[:i] + helper.lstrip("\n") + "\n" + s[i:]

# 2) XL 的 self 分支：触发词 + 主人补的描述
old = '''        if mode == "self" and _xl_path.exists():
            wf = json.loads(_xl_path.read_text(encoding="utf-8"))
            _tn = _find_text_node(wf) or "10"
            if _tn in wf:
                wf[_tn]["inputs"]["text"] = str(prompt)          # 原封不动
            wf = await _fix_missing_ckpt(wf)
            logger.info(f"[出图] 画你自己 → XL 工作流（提示词：{str(prompt)[:40]}）")
            return await _submit_wf(wf, t0)'''
new = '''        if mode == "self" and _xl_path.exists():
            wf = json.loads(_xl_path.read_text(encoding="utf-8"))
            _tn = _find_text_node(wf) or "10"
            _trig = _draw_self_triggers()
            _extra = str(prompt or "").strip(" ，,。")
            if _trig and _extra and _extra not in _trig:
                _text = f"{_trig}，{_extra}"
            elif _trig:
                _text = _trig
            else:
                _text = _extra
            if _tn in wf:
                wf[_tn]["inputs"]["text"] = _text
            wf = await _fix_missing_ckpt(wf)
            logger.info(f"[出图] 画你自己 → XL 工作流（触发词{len(_trig)}字 + 补充：{_extra[:30]}）")
            return await _submit_wf(wf, t0)'''
if old in s:
    s = s.replace(old, new, 1)
else:
    raise SystemExit("self 分支锚点未匹配")

# 3) /画图触发词 查看/修改
old2 = '''    # 👇 1.9 看表：/时间 👇'''
new2 = '''    # 👇 1.88 画你自己 的 LoRA 触发词：/画图触发词 [新内容] 👇
    if user_msg.startswith("画图触发词"):
        _argt = user_msg[len("画图触发词"):].strip(" ：:，,")
        if _argt:
            try:
                _DRAW_SELF_TRIGGERS_FILE.write_text(_argt, encoding="utf-8")
                global _DRAW_SELF_TRIGGERS_CACHE
                _DRAW_SELF_TRIGGERS_CACHE = None
                await private_ai.finish(f"(*点头*) 触发词换成新的了（{len(_argt)} 字）")
            except Exception as e:  # noqa: BLE001
                await private_ai.finish(f"(*耷拉尾巴*) 存不下：{type(e).__name__}")
        else:
            await private_ai.finish(f"(*背书一样念*) 当前触发词：\\n{_draw_self_triggers()[:400]}")
        return

    # 👇 1.9 看表：/时间 👇'''
if old2 in s:
    s = s.replace(old2, new2, 1)
    p.write_text(s, encoding="utf-8")
    print("ai_group: 触发词已接入 + /画图触发词 指令")
