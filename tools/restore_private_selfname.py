import json
import pathlib

ROOT = pathlib.Path(r"D:\qq-ai-bot")
STORE = ROOT / "memory" / "长期记忆.json"
BAK = ROOT / "memory" / "长期记忆.json.bak_selfname_20261029_1722"

cur = json.loads(STORE.read_text(encoding="utf-8"))
old = json.loads(BAK.read_text(encoding="utf-8"))

cur_arch = cur.setdefault("archive", {})
old_arch = old.get("archive") or {}

restored = 0
for scope, items in old_arch.items():
    # 只把私聊里"本鱼自己的承诺/外号"这类合法条目恢复回去
    if not scope.startswith("private_"):
        continue
    keep = [it for it in (items or []) if str((it or {}).get("subject") or "").strip() in ("本鱼",)]
    if not keep:
        continue
    have = {str(x.get("fact")) for x in (cur_arch.get(scope) or [])}
    add = [it for it in keep if str(it.get("fact")) not in have]
    if add:
        cur_arch.setdefault(scope, []).extend(add)
        restored += len(add)

# 重新编号 id，避免重复
for scope, items in cur_arch.items():
    for idx, it in enumerate(items or [], 1):
        it["id"] = str(idx)

STORE.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"已恢复私聊里 {restored} 条「本鱼」条目（她自己的承诺/外号）")

for scope in ("private_100000000", "group_187950638"):
    items = cur_arch.get(scope) or []
    print(f"\n== {scope}（{len(items)} 条）")
    for it in items[-4:]:
        print(f"   [{it.get('subject')}] {str(it.get('fact'))[:50]}")
