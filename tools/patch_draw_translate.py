import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\plugins\ai_group.py")
s = p.read_text(encoding="utf-8")

# 1) 中文补充自动翻英文（SDXL 的 CLIP 只吃英文）
if "async def _to_english_prompt(" not in s:
    helper = '''

async def _to_english_prompt(text: str) -> str:
    """把中文描述翻成英文 danbooru 风格 tags（SDXL 的文本编码器不认中文）。

    只翻"画你自己"的补充描述；翻译失败就原样返回，不阻断出图。
    """
    t = str(text or "").strip()
    if not t:
        return ""
    if not any("\\u4e00" <= ch <= "\\u9fff" for ch in t):
        return t
    try:
        out, _m = await _chat(
            [{"role": "system", "content": "You are a prompt translator for Stable Diffusion XL."},
             {"role": "user", "content":
              f"把这句话翻译成英文的 danbooru 风格 tag，逗号分隔，只输出英文 tag，不要解释：\\n{t}"}],
            temperature=0.2, max_tokens=200, record_scope="draw_translate", channel="system")
        en = str(out or "").strip().strip("。.")
        en = en.replace("，", ", ").replace("\\n", ", ")
        return en[:300] if en else t
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[出图] 提示词翻译失败（用原文）：{type(e).__name__}: {e}")
        return t

'''
    i = s.find("async def _local_zimage(")
    s = s[:i] + helper.lstrip("\n") + "\n" + s[i:]

# 2) XL 的 self 分支：触发词 + 英文补充
old = '''            _trig = _draw_self_triggers()
            _extra = str(prompt or "").strip(" ，,。")
            if _trig and _extra and _extra not in _trig:
                _text = f"{_trig}，{_extra}"
            elif _trig:
                _text = _trig
            else:
                _text = _extra'''
new = '''            _trig = _draw_self_triggers()
            _extra = str(prompt or "").strip(" ，,。")
            # 中文补充先翻成英文再拼（SDXL 不认中文，直接塞等于没写）
            _extra_en = await _to_english_prompt(_extra) if _extra else ""
            if _trig and _extra_en and _extra_en.lower() not in _trig.lower():
                _text = f"{_trig}, {_extra_en}"
            elif _trig:
                _text = _trig
            else:
                _text = _extra_en or _extra'''
if old in s:
    s = s.replace(old, new, 1)
else:
    raise SystemExit("self 分支锚点未匹配")

# 触发词规范化：换行/、→ 逗号，保留大写
old2 = '''    txt = "，".join(x.strip() for x in txt.replace("\\n", "，").replace("、", "，").split("，") if x.strip())'''
new2 = '''    txt = ", ".join(x.strip() for x in txt.replace("\\n", ",").replace("，", ",").replace("、", ",").split(",") if x.strip())'''
if old2 in s:
    s = s.replace(old2, new2, 1)
    p.write_text(s, encoding="utf-8")
    print("ai_group: 触发词(英文) + 补充自动翻译")
