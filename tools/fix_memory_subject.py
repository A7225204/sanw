import json
import pathlib
import shutil
import time

ROOT = pathlib.Path(r"D:\qq-ai-bot")
LM = ROOT / "plugins" / "long_memory.py"

# ---------- 1) 合并提示词：主语必须是说话人本人，禁止用机器人自己的名字 ----------
s = LM.read_text(encoding="utf-8")
old = '''【操作规则】
- 新增 → op="add"，subject 填群友昵称，fact 写清"谁 + 什么事"；'''
new = '''【主语规则（最重要）】
- subject 必须是**说这句话的那个群友自己的昵称**（聊天记录行首的名字），不许张冠李戴；
- 绝对禁止把 subject 写成机器人自己：「鲸鱼娘」「本鱼」「蓝色大肥鱼」「大肥鱼机器人」等一律不许；
- 描述机器人自己的事（谁蹭她的饭、谁逗她）不要记成事实；
- 如果已有档案里出现以机器人名字为主语的条目，用 op="delete" 删掉。

【操作规则】
- 新增 → op="add"，subject 填群友昵称，fact 写清"谁 + 什么事"；'''
if old in s and "【主语规则（最重要）】" not in s:
    s = s.replace(old, new, 1)
    LM.write_text(s, encoding="utf-8")
    print("MERGE_PROMPT 已加主语规则")
else:
    print("MERGE_PROMPT 未改（已存在或未匹配）")

# ---------- 2) 一次性清理：删掉以机器人名字为主语的档案条目 ----------
BOT_NAMES = {"鲸鱼娘", "本鱼", "蓝色大肥鱼", "蓝鱼", "大肥鱼机器人"}
STORE = ROOT / "memory" / "长期记忆.json"
bak = STORE.with_name(f"长期记忆.json.bak_selfname_{time.strftime('%Y%m%d_%H%M')}")
shutil.copy2(STORE, bak)

data = json.loads(STORE.read_text(encoding="utf-8"))
arch = data.get("archive") or {}
removed = []
for scope, items in list(arch.items()):
    keep = []
    for it in items or []:
        subj = str((it or {}).get("subject") or "").strip()
        if subj in BOT_NAMES:
            removed.append(f"{scope}: [{subj}] {str(it.get('fact'))[:40]}")
        else:
            keep.append(it)
    arch[scope] = keep
if removed:
    STORE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"备份: {bak.name}")
print(f"清掉 {len(removed)} 条以机器人名字为主语的档案：")
for r in removed[:12]:
    print("  -", r)
