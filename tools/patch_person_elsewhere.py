import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\server\memory_viz.py")
s = p.read_text(encoding="utf-8")

if "_person_elsewhere" in s:
    print("已支持跨群同一人")
    raise SystemExit

helper = '''

def _person_elsewhere(scope: str, subject: str) -> list[dict]:
    """同一个人（优先按 QQ，其次昵称）在其它 scope 里的主题节点。"""
    me = _speaker_info(scope, subject)
    uid = str(me.get("uid") or "")
    name = str(subject or "").strip()
    out: list[dict] = []
    arch = _archive()
    for other in arch.keys():
        if other == scope:
            continue
        items = arch.get(other) or []
        subjects = {str(i.get("subject") or "").strip()[:20] for i in items}
        if not subjects:
            continue
        hit_subject = ""
        if uid:
            for sub in subjects:
                info = _speaker_info(other, sub)
                if str(info.get("uid") or "") == uid:
                    hit_subject = sub
                    break
        if not hit_subject and name and name in subjects:
            hit_subject = name
        if hit_subject:
            out.append({
                "id": _topic_id(other, hit_subject),
                "scope": other,
                "scopeLabel": _scope_label(other),
                "summary": hit_subject,
                "viaQq": bool(uid),
            })
    return out[:6]

'''
anchor = "def detail(node_id: str) -> dict | None:"
i = s.find(anchor)
s = s[:i] + helper.lstrip("\n") + "\n" + s[i:]

# memory 分支：加 personElsewhere
s = s.replace('''                return {
                    "node": me or {"id": node_id, "scope": scope},
                    "speaker": _speaker_info(scope, _subj),''',
'''                return {
                    "node": me or {"id": node_id, "scope": scope},
                    "speaker": _speaker_info(scope, _subj),
                    "personElsewhere": _person_elsewhere(scope, _subj),''', 1)

# topic 分支
s = s.replace('''    return {
        "node": me,
        "speaker": _speaker_info(scope, str(me.get("summary") or "")),''',
'''    return {
        "node": me,
        "speaker": _speaker_info(scope, str(me.get("summary") or "")),
        "personElsewhere": _person_elsewhere(scope, str(me.get("summary") or "")),''', 1)

p.write_text(s, encoding="utf-8")
print("detail: 已加 personElsewhere（同一人在别的群）")
