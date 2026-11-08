import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\server\memory_viz.py")
s = p.read_text(encoding="utf-8")

if "_speaker_info" in s:
    print("已有发言人信息")
    raise SystemExit

helper = '''

def _speaker_info(scope: str, subject: str) -> dict:
    """这条记忆属于谁：群名 / 昵称 / QQ 号（昵称→QQ 用 nick_names.json 反查）。"""
    info = {
        "channel": "private" if scope.startswith("private_") else "group" if scope.startswith("group_") else "other",
        "scopeLabel": _scope_label(scope),
        "groupId": "",
        "speaker": str(subject or "").strip(),
        "uid": "",
    }
    if scope.startswith("group_"):
        info["groupId"] = scope.split("_", 1)[1]
        try:
            data = _read_json(ROOT / "memory" / "nick_names.json")
            people = (data.get(info["groupId"]) or {})
            for uid, rec in people.items():
                if not isinstance(rec, dict):
                    continue
                first = str(rec.get("first") or "")
                cur = str(rec.get("current") or "")
                if subject and (subject == first or subject == cur or subject in (first, cur)):
                    info["uid"] = str(uid)
                    info["speaker"] = first or cur or subject
                    break
        except Exception:  # noqa: BLE001
            pass
    elif scope.startswith("private_"):
        who = scope.split("_", 1)[1]
        if who.isdigit():
            info["uid"] = who
            info["speaker"] = info["speaker"] or "主人"
    return info

'''
anchor = "def detail(node_id: str) -> dict | None:"
i = s.find(anchor)
if i < 0:
    raise SystemExit("detail 未找到")
s = s[:i] + helper.lstrip("\n") + "\n" + s[i:]

# memory 分支：带上发言人信息
s = s.replace('''                return {
                    "node": me or {"id": node_id, "scope": scope},
                    "sourceMessages": src_msgs,''',
'''                _subj = ""
                for it2 in arch.get(scope) or []:
                    if str(it2.get("id")) == want:
                        _subj = str(it2.get("subject") or "")
                        break
                return {
                    "node": me or {"id": node_id, "scope": scope},
                    "speaker": _speaker_info(scope, _subj),
                    "sourceMessages": src_msgs,''', 1)

# topic / 其他分支
s = s.replace('''    return {
        "node": me,
        "sourceMessages": [],
        "relatedNodeIds": [n["id"] for n in rel_nodes],''',
'''    return {
        "node": me,
        "speaker": _speaker_info(scope, str(me.get("summary") or "")),
        "sourceMessages": [],
        "relatedNodeIds": [n["id"] for n in rel_nodes],''', 1)

p.write_text(s, encoding="utf-8")
print("detail: 已返回 speaker（群名/昵称/QQ号）")
