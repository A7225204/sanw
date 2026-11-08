import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\plugins\ai_group.py")
s = p.read_text(encoding="utf-8")

if "VISION_PREFER_ARK" in s:
    print("已配置火山优先")
    raise SystemExit

# 1) 开关：VISION_PREFER=ark 时，任何识图都先走火山（里面有余额）
if "VISION_PREFER" not in s:
    s = s.replace(
        'ZIMAGE_WORKFLOW = ROOT / "comfy_workflows" / "z_face_consistency.json"',
        'VISION_PREFER = (os.getenv("VISION_PREFER", "").strip().lower())\n'
        'ZIMAGE_WORKFLOW = ROOT / "comfy_workflows" / "z_face_consistency.json"',
        1,
    )

# 2) 在 Gemini 判断之前插入"火山优先"分支
old = '''        # 谷歌只在「开了 VPN 且没被额度封禁」时才发；否则直接走硅基（快且免费额度）
        _blocked = _gemini_blocked()'''
new = '''        # 主人指定：识图优先走火山方舟（账户里有余额）
        if not desc and VISION_PREFER == "ark" and _ark_vision_can_use():
            _t_ark = time.time()
            desc = await _ark_vision(buf, prompt)
            if desc:
                token_meter.record_vision("ark", time.time() - _t_ark)
                logger.info(f"[识图] 火山优先({time.time() - _t_ark:.1f}s): {desc[:160]}")
        # 谷歌只在「开了 VPN 且没被额度封禁」时才发；否则直接走硅基（快且免费额度）
        _blocked = _gemini_blocked()'''
if old in s:
    s = s.replace(old, new, 1)
    s = s.replace("        if _use_gemini:", "        if _use_gemini and not desc:", 1)

p.write_text(s, encoding="utf-8")
print("已把火山设为识图首选（其余通道保留兜底）")

# 3) .env 打开开关
e = pathlib.Path(r"D:\qq-ai-bot\.env")
t = e.read_text(encoding="utf-8")
if "VISION_PREFER" not in t:
    t = t.rstrip() + "\n\n# 识图优先通道：ark=火山方舟（账户有余额时用这个）\nVISION_PREFER=ark\n"
    e.write_text(t, encoding="utf-8")
    print(".env: 已加 VISION_PREFER=ark")
