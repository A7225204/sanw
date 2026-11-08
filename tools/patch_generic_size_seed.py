import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\plugins\ai_group.py")
s = p.read_text(encoding="utf-8")

old = '''        if "10" in wf:
            wf["10"]["inputs"]["width"] = int(width)
            wf["10"]["inputs"]["height"] = int(height)
        elif "20" in wf:
            wf["20"]["inputs"]["width"] = int(width)
            wf["20"]["inputs"]["height"] = int(height)
        if "12" in wf:
            wf["12"]["inputs"]["seed"] = random.randint(1, 2**31 - 1)
        elif "7" in wf:
            wf["7"]["inputs"]["seed"] = random.randint(1, 2**31 - 1)'''

new = '''        # 尺寸：兼容手调工作流里的任意空 latent 节点（EmptyLatentImage / EmptySD3LatentImage …）
        _sized = False
        for _nid, _node in wf.items():
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
        # 随机种子：KSampler 用 seed，KSamplerAdvanced 用 noise_seed
        for _nid, _node in wf.items():
            if str(_node.get("class_type") or "") not in ("KSampler", "KSamplerAdvanced"):
                continue
            _inp = _node.setdefault("inputs", {})
            _sd = random.randint(1, 2**31 - 1)
            if "noise_seed" in _inp:
                _inp["noise_seed"] = _sd
            elif "seed" in _inp:
                _inp["seed"] = _sd'''

if old in s:
    s = s.replace(old, new, 1)
    p.write_text(s, encoding="utf-8")
    print("尺寸/种子已改为通用处理")
else:
    print("锚点没找到，当前片段：")
    i = s.find('        if "10" in wf:')
    print(s[i:i + 400] if i > 0 else "(没有 '10' 分支)")
