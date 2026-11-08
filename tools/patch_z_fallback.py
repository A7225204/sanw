import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\plugins\ai_group.py")
s = p.read_text(encoding="utf-8")

# 1) ComfyUI 健康检查 / 自动拉起
if "async def _comfy_ensure(" not in s:
    helper = '''

_COMFY_PROC = None


async def _comfy_ensure(timeout: float = 120.0) -> bool:
    """ComfyUI 没响应就拉起来（Z-Image 加载崩溃后要重启才能继续画）。"""
    global _COMFY_PROC
    async def _alive() -> bool:
        try:
            r = await http_client.get(f"{COMFY_URL}/system_stats", timeout=5)
            return r.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    if await _alive():
        return True
    try:
        import subprocess
        comfy_py = r"C:\\ComfyUI-aki(2)\\ComfyUI-aki-v1.7\\python\\python.exe"
        comfy_dir = r"C:\\ComfyUI-aki(2)\\ComfyUI-aki-v1.7\\ComfyUI"
        logger.warning("[出图] ComfyUI 没响应，尝试拉起…")
        _COMFY_PROC = subprocess.Popen(
            [comfy_py, "main.py", "--listen", "127.0.0.1", "--port", "8188"],
            cwd=comfy_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[出图] 拉起 ComfyUI 失败：{type(e).__name__}: {e}")
        return False
    t0 = time.time()
    while time.time() - t0 < timeout:
        await asyncio.sleep(4)
        if await _alive():
            logger.info(f"[出图] ComfyUI 已就绪（{time.time() - t0:.0f}s）")
            return True
    return False

'''
    i = s.find("async def _comfy_free(")
    s = s[:i] + helper.lstrip("\n") + "\n" + s[i:]

# 2) Z-Image 失败时回退到 XL（保证还能出图）
old = '''        if mode == "self" and _want_real:'''
new = '''        async def _fallback_xl(reason: str):
            """Z 模型崩了/超时 → 用主人手调的 XL 工作流出图，至少不空白。"""
            try:
                logger.warning(f"[出图] Z 模型失败（{reason}），回退 XL 工作流")
                await _comfy_ensure()
                _wf = json.loads(_xl_path.read_text(encoding="utf-8"))
                _t2 = _find_text_node(_wf) or "10"
                _trig = _draw_self_triggers()
                _en = await _to_english_prompt(str(prompt))
                _txt = f"{_trig}, {_en}" if _trig and _en else (_trig or _en)
                if _t2 in _wf:
                    _wf[_t2]["inputs"]["text"] = _txt[:900]
                _wf = await _fix_missing_ckpt(_wf)
                return await _submit_wf(_wf, t0)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[出图] 回退也失败：{type(e).__name__}: {e}")
                return None

        if mode == "self" and _want_real:'''
if old in s:
    s = s.replace(old, new, 1)
else:
    raise SystemExit("real 分支锚点未匹配")

# real 分支失败回退
old2 = '''            logger.info("[出图] 画你自己（真人风格）→ Z模型人物一致性")
            return await _submit_wf(wf, t0)'''
new2 = '''            logger.info("[出图] 画你自己（真人风格）→ Z模型人物一致性")
            _img_r = await _submit_wf(wf, t0)
            return _img_r or await _fallback_xl("Z 模型无响应")'''
if old2 in s:
    s = s.replace(old2, new2, 1)

# 非 self（画图/画画）失败回退
old3 = '''        # 画图/画画/改图 都走 Z 模型人物一致性工作流（主人 2026-10-29 指定）
        _wf_path = ZIMAGE_WORKFLOW
        wf = json.loads(_wf_path.read_text(encoding="utf-8"))'''
new3 = '''        # 画图/画画 走 Z 模型人物一致性（主人 2026-10-29 指定）；失败自动回退 XL
        _wf_path = ZIMAGE_WORKFLOW
        wf = json.loads(_wf_path.read_text(encoding="utf-8"))'''
if old3 in s:
    s = s.replace(old3, new3, 1)

p.write_text(s, encoding="utf-8")
print("ai_group: 加 ComfyUI 自动拉起 + Z 模型失败回退 XL")
