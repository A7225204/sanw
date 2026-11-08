import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\plugins\ai_group.py")
s = p.read_text(encoding="utf-8")

# ---------- 1) 换模型族群前先让 ComfyUI 卸载旧模型（避免 SDXL↔Z-Image 切换崩） ----------
if "async def _comfy_free(" not in s:
    helper = '''

_LAST_MODEL_FAMILY = ""


async def _comfy_free() -> None:
    """让 ComfyUI 卸载已加载的模型，避免不同模型之间切换时崩（access violation）。"""
    try:
        await http_client.post(f"{COMFY_URL}/free",
                               json={"unload_models": True, "free_memory": True}, timeout=20)
        await asyncio.sleep(1.0)
    except Exception:  # noqa: BLE001
        pass


def _wf_family(wf: dict) -> str:
    for _node in wf.values():
        ct = str(_node.get("class_type") or "")
        if ct in ("CheckpointLoaderSimple", "UNETLoader"):
            name = str((_node.get("inputs") or {}).get("ckpt_name")
                       or (_node.get("inputs") or {}).get("unet_name") or "").lower()
            if "xl" in name or "sdxl" in name:
                return "sdxl"
            if "z_image" in name or "z-image" in name:
                return "zimage"
    return "other"

'''
    i = s.find("async def _submit_wf(")
    s = s[:i] + helper.lstrip("\n") + "\n" + s[i:]

# 提交前判断是否换族群
old_sub = '''async def _submit_wf(wf: dict, t0: float) -> bytes | None:
    """提交工作流并等图片。"""
    q = await http_client.post(f"{COMFY_URL}/prompt", json={"prompt": wf}, timeout=30)'''
new_sub = '''async def _submit_wf(wf: dict, t0: float) -> bytes | None:
    """提交工作流并等图片。换模型家族前先 unload，防止切模型崩。"""
    global _LAST_MODEL_FAMILY
    fam = _wf_family(wf)
    if _LAST_MODEL_FAMILY and fam != _LAST_MODEL_FAMILY and fam != "other":
        await _comfy_free()
    q = await http_client.post(f"{COMFY_URL}/prompt", json={"prompt": wf}, timeout=90)'''
if old_sub in s:
    s = s.replace(old_sub, new_sub, 1)
else:
    raise SystemExit("_submit_wf 锚点未匹配")

# 成功后记录家族
s = s.replace('''                    logger.info(f"[出图] 本地出图用时 {time.time() - t0:.1f}s（{fn}）")
                    return r.content''',
'''                    _LAST_MODEL_FAMILY = fam
                    logger.info(f"[出图] 本地出图用时 {time.time() - t0:.1f}s（{fn}，{fam}）")
                    return r.content''')

# ---------- 2) 路由重排 ----------
old_route = '''    try:
        if mode == "self" and _xl_path.exists():'''
new_route = '''    _REAL_WORDS = ("真人", "写实", "摄影", "照片", "真实", "单反", "胶片")
    _want_real = any(k in str(prompt) for k in _REAL_WORDS)
    try:
        if mode == "self" and _want_real:
            # 画你自己 + 真人风格 → 走 Z 模型人物一致性（摄影质感）
            wf = json.loads(ZIMAGE_WORKFLOW.read_text(encoding="utf-8"))
            _tn = _find_text_node(wf)
            if _tn in wf:
                wf[_tn]["inputs"]["text"] = (f"{_draw_char_desc()}，摄影风格:1.4，真实照片质感，"
                                             f"自然光，85mm 镜头，浅景深，{prompt}"[:800])
            wf = await _fix_missing_ckpt(wf)
            logger.info("[出图] 画你自己（真人风格）→ Z模型人物一致性")
            return await _submit_wf(wf, t0)

        if mode == "self" and _xl_path.exists():'''
if old_route in s:
    s = s.replace(old_route, new_route, 1)
else:
    raise SystemExit("路由锚点未匹配")

# 非 self（画图/画画）→ Z模型人物一致性（不再用 t2i）
old_v = '''        if mode == "edit":
            _wf_path = ZIMAGE_WORKFLOW
        elif ZIMAGE_T2I_USER.exists():
            _wf_path = ZIMAGE_T2I_USER
        elif ZIMAGE_T2I_WORKFLOW.exists():
            _wf_path = ZIMAGE_T2I_WORKFLOW
        else:
            _wf_path = ZIMAGE_WORKFLOW
        wf = json.loads(_wf_path.read_text(encoding="utf-8"))'''
new_v = '''        # 画图/画画/改图 都走 Z 模型人物一致性工作流（主人 2026-10-29 指定）
        _wf_path = ZIMAGE_WORKFLOW
        wf = json.loads(_wf_path.read_text(encoding="utf-8"))'''
if old_v in s:
    s = s.replace(old_v, new_v, 1)
else:
    raise SystemExit("verbatim 路由未匹配")

p.write_text(s, encoding="utf-8")
print("ai_group: 路由改为 画你自己→XL / 真人→一致性 / 画画→一致性 / 换模型先unload")
