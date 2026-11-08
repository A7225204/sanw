import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\plugins\ai_group.py")
s = p.read_text(encoding="utf-8")

# ---------- 1) 选工作流：画你自己 → 大肥鱼 XL 工作流（原封不动） ----------
old = '''    t0 = time.time()
    try:
        if mode == "edit":
            _wf_path = ZIMAGE_WORKFLOW
        elif ZIMAGE_T2I_USER.exists():
            _wf_path = ZIMAGE_T2I_USER   # 用户导出的 API 工作流，原样使用
        elif ZIMAGE_T2I_WORKFLOW.exists():
            _wf_path = ZIMAGE_T2I_WORKFLOW
        else:
            _wf_path = ZIMAGE_WORKFLOW
        wf = json.loads(_wf_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[出图] 读不到 Z-Image 工作流：{e}")
        return None'''
new = '''    t0 = time.time()
    _xl_path = ROOT / "comfy_workflows" / "dafeiyu_xl.json"
    _keep_params = False   # True = 只塞提示词，尺寸/种子都不动（主人手调的 XL 工作流）
    try:
        if mode == "self" and _xl_path.exists():
            # "画你自己"：走主人新加的 XL 工作流，提示词原封不动
            wf = json.loads(_xl_path.read_text(encoding="utf-8"))
            _keep_params = True
            _tn0 = _find_text_node(wf) or "10"
            if _tn0 in wf:
                wf[_tn0]["inputs"]["text"] = str(prompt)[:1000]
            logger.info(f"[出图] 画你自己 → XL 工作流（提示词原样：{str(prompt)[:40]}）")
        else:
            if mode == "edit":
                _wf_path = ZIMAGE_WORKFLOW
            elif ZIMAGE_T2I_USER.exists():
                _wf_path = ZIMAGE_T2I_USER   # 用户导出的 API 工作流，原样使用
            elif ZIMAGE_T2I_WORKFLOW.exists():
                _wf_path = ZIMAGE_T2I_WORKFLOW
            else:
                _wf_path = ZIMAGE_WORKFLOW
            wf = json.loads(_wf_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[出图] 读不到工作流：{e}")
        return None'''
if old in s:
    s = s.replace(old, new, 1)
else:
    raise SystemExit("工作流选择锚点未匹配")

# ---------- 2) XL 模式：不覆盖尺寸、不随机种子；并把缺失的 ckpt 换成已装的 XL ----------
old2 = '''    try:
        # 人物一致性工作流：提示词在节点 7，采样在节点 12，缩放尺寸在节点 10
        if "7" in wf:'''
new2 = '''    try:
        if _keep_params:
            # 主人手调参数一律不动：只保留他工作流里的尺寸/种子/采样设置
            wf = await _fix_missing_ckpt(wf)
        # 人物一致性工作流：提示词在节点 7，采样在节点 12，缩放尺寸在节点 10
        if (not _keep_params) and "7" in wf:'''
if old2 in s:
    s = s.replace(old2, new2, 1)
else:
    raise SystemExit("提示词节点锚点未匹配")

# 尺寸与种子：XL 模式跳过
s = s.replace('''        # 尺寸：兼容手调工作流里的任意空 latent 节点（EmptyLatentImage / EmptySD3LatentImage …）
        _sized = False''',
'''        # 尺寸：兼容手调工作流里的任意空 latent 节点（EmptyLatentImage / EmptySD3LatentImage …）
        _sized = True if _keep_params else False
        if _sized:
            return await _submit_wf(wf, t0)''')

p.write_text(s, encoding="utf-8")
print("ai_group: XL 工作流分支已插入（还需补 helper）")
