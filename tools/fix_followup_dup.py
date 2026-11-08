import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\plugins\human_touch.py")
s = p.read_text(encoding="utf-8")
changed = []

# 1) 补一句不再复述自己说过的话：只用通用小尾巴，且不重复正文
old_fu = '''    opener = random.choice(FOLLOWUP_OPENERS)
    # 一半：从原话里挑一小段接着说；一半：用固定小尾巴
    if random.random() < 0.5:
        seg = body[: max(4, min(14, len(body) // 2))].strip(_TAIL_PUNCT)
        if len(seg) >= 4:
            return f"{opener}{seg}"
    return random.choice(FOLLOWUP_TAILS)'''
new_fu = '''    # 只补一句"新的话"，绝不复述刚说过的内容（之前会挑原话开头，看起来像重复发送）
    tail = random.choice(FOLLOWUP_TAILS)
    if tail in body or body[:8] in tail:
        return None
    return tail'''
if old_fu in s:
    s = s.replace(old_fu, new_fu, 1)
    changed.append("补一句不再复述")

# 2) 分条时去重：完全一样 / 互相包含的段落只留一条
old_shape_end = '''    # 太整齐了也不好：随机把最后一条并进上一条
    if len(lines) >= 3 and random.random() < 0.2:
        lines = lines[:-2] + [lines[-2] + lines[-1]]
    return lines'''
new_shape_end = '''    # 太整齐了也不好：随机把最后一条并进上一条
    if len(lines) >= 3 and random.random() < 0.2:
        lines = lines[:-2] + [lines[-2] + lines[-1]]
    return _dedupe_lines(lines)


def _dedupe_lines(lines: list[str]) -> list[str]:
    """去掉重复/互相包含的段落（她有时会把自己第一句又说一遍）。"""
    out: list[str] = []
    for ln in lines:
        core = ln.strip(_TAIL_PUNCT)
        if not core:
            continue
        dup = False
        for keep in out:
            kcore = keep.strip(_TAIL_PUNCT)
            if core == kcore or (len(core) >= 6 and core in kcore) or (len(kcore) >= 6 and kcore in core):
                dup = True
                # 保留更长的那条
                if len(core) > len(kcore):
                    out[out.index(keep)] = ln
                break
        if not dup:
            out.append(ln)
    return out'''
if old_shape_end in s:
    s = s.replace(old_shape_end, new_shape_end, 1)
    changed.append("分条去重")

p.write_text(s, encoding="utf-8")
print("改动:", changed)
