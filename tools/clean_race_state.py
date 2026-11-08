import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\plugins\ai_group.py")
s = p.read_text(encoding="utf-8")
removed = []

if "GROUP_RACE_STATE = defaultdict" in s:
    s = s.replace("GROUP_RACE_STATE = defaultdict(lambda: False)\n", "")
    removed.append("定义")
line = "        if gid in GROUP_RACE_STATE: del GROUP_RACE_STATE[gid]\n"
if line in s:
    s = s.replace(line, "")
    removed.append("清理引用")

p.write_text(s, encoding="utf-8")
print("清理:", removed)
