import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\plugins\ai_group.py")
s = p.read_text(encoding="utf-8")

start = s.find("async def _local_zimage(")
if start < 0:
    raise SystemExit("找不到 _local_zimage")
nxt = s.find("\nasync def ", start + 10)
if nxt < 0:
    raise SystemExit("找不到函数结尾")
old_fn = s[start:nxt]

new = '''_COMFY_CKPT_CACHE: list[str] | None = None


async def _comfy_ckpts() -> list[str]:
    """ComfyUI 里实际装了的 checkpoint 列表（缓存一次）。"""
    global _COMFY_CKPT_CACHE
    if _COMFY_CKPT_CACHE is not None:
        return _COMFY_CKPT_CACHE
    try:
        r = await http_client.get(f"{COMFY_URL}/object_info/CheckpointLoaderSimple", timeout=20)
        info = (r.json() or {}).get("CheckpointLoaderSimple") or {}
        opts = ((info.get("input") or {}).get("required") or {}).get("ckpt_name") or [[]]
        _COMFY_CKPT_CACHE = [str(x) for x in (opts[0] or [])]
    except Exception:  # noqa: BLE001
        _COMFY_CKPT_CACHE = []
    return _COMFY_CKPT_CACHE


async def _fix_missing_ckpt(wf: dict) -> dict:
    """工作流里写的 ckpt 没装时，换成实际装了的 XL 模型（只修这一项，其它参数不动）。"""
    ckpts = await _comfy_ckpts()
    if not ckpts:
        return wf
    for _node in wf.values():
        if str(_node.get("class_type") or "") != "CheckpointLoaderSimple":
            continue
        _inp = _node.setdefault("inputs", {})
        cur = str(_inp.get("ckpt_name") or "")
        if cur in ckpts:
            continue
        cand = next((c for c in ckpts if "xl" in c.lower()), ckpts[0])
        logger.info(f"[出图] ckpt '{cur}' 未安装 → 用 '{cand}'")
        _inp["ckpt_name"] = cand
    return wf


async def _submit_wf(wf: dict, t0: float) -> bytes | None:
    """提交工作流并等图片。"""
    q = await http_client.post(f"{COMFY_URL}/prompt", json={"prompt": wf}, timeout=30)
    if q.status_code != 200:
        logger.warning(f"[出图] ComfyUI 拒绝工作流：{q.text[:200]}")
        return None
    pid = (q.json() or {}).get("prompt_id", "")
    deadline = time.time() + 600
    while time.time() < deadline:
        await asyncio.sleep(0.8)
        h = (await http_client.get(f"{COMFY_URL}/history/{pid}", timeout=15)).json()
        item = h.get(pid) or {}
        status = (item.get("status") or {}).get("status_str")
        if status == "error":
            logger.warning(f"[出图] 本地出图执行出错：{str(item.get('status'))[:150]}")
            return None
        for node in (item.get("outputs") or {}).values():
            for img in node.get("images") or []:
                fn = img.get("filename")
                if not fn:
                    continue
                r = await http_client.get(
                    f"{COMFY_URL}/view",
                    params={"filename": fn, "subfolder": img.get("subfolder", ""), "type": "output"},
                    timeout=30,
                )
                if r.status_code == 200 and r.content:
                    try:
                        token_meter.record_local("image", time.time() - t0)
                    except Exception:  # noqa: BLE001
                        pass
                    logger.info(f"[出图] 本地出图用时 {time.time() - t0:.1f}s（{fn}）")
                    return r.content
    logger.warning(f"[出图] 本地出图超时（{time.time() - t0:.0f}s）")
    return None


async def _local_zimage(prompt: str, width: int = 832, height: int = 1216, mode: str = "verbatim") -> bytes | None:
    """本地出图（免费）。

    画你自己 → 主人手调的 XL 工作流（提示词原封不动，尺寸/种子/采样一律不碰）
    其它     → 一致性 / 纯文生图 工作流（沿用原来那套）
    """
    if not LOCAL_IMAGE_ENABLED:
        return None
    t0 = time.time()
    _xl_path = ROOT / "comfy_workflows" / "dafeiyu_xl.json"
    try:
        if mode == "self" and _xl_path.exists():
            wf = json.loads(_xl_path.read_text(encoding="utf-8"))
            _tn = _find_text_node(wf) or "10"
            if _tn in wf:
                wf[_tn]["inputs"]["text"] = str(prompt)          # 原封不动
            wf = await _fix_missing_ckpt(wf)
            logger.info(f"[出图] 画你自己 → XL 工作流（提示词：{str(prompt)[:40]}）")
            return await _submit_wf(wf, t0)

        if mode == "edit":
            _wf_path = ZIMAGE_WORKFLOW
        elif ZIMAGE_T2I_USER.exists():
            _wf_path = ZIMAGE_T2I_USER
        elif ZIMAGE_T2I_WORKFLOW.exists():
            _wf_path = ZIMAGE_T2I_WORKFLOW
        else:
            _wf_path = ZIMAGE_WORKFLOW
        wf = json.loads(_wf_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[出图] 读不到工作流：{e}")
        return None

    try:
        _tn = _find_text_node(wf)
        if _tn in wf:
            wf[_tn]["inputs"]["text"] = str(prompt)[:800]

        # 尺寸：兼容任意空 latent 节点
        _sized = False
        for _node in wf.values():
            _ct = str(_node.get("class_type") or "")
            if _ct.startswith("Empty") and "Latent" in _ct:
                _inp = _node.setdefault("inputs", {})
                _inp["width"] = int(width)
                _inp["height"] = int(height)
                _sized = True
                break
        if not _sized and "10" in wf:
            wf["10"]["inputs"]["width"] = int(width)
            wf["10"]["inputs"]["height"] = int(height)

        # 随机种子
        for _node in wf.values():
            if str(_node.get("class_type") or "") not in ("KSampler", "KSamplerAdvanced"):
                continue
            _inp = _node.setdefault("inputs", {})
            _sd = random.randint(1, 2**31 - 1)
            if "noise_seed" in _inp:
                _inp["noise_seed"] = _sd
            elif "seed" in _inp:
                _inp["seed"] = _sd
        return await _submit_wf(wf, t0)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[出图] 本地出图异常：{type(e).__name__}: {e}")
        return None
'''

s = s[:start] + new + s[nxt:]
p.write_text(s, encoding="utf-8")
print("ai_group: _local_zimage 重写完成")
