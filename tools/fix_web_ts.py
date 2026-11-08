import pathlib

root = pathlib.Path(r"D:\qq-ai-bot\dashboard-web\src")

# 1) store 里 types 路径修正
p = root / "features/memory-viz/store/vizStore.ts"
s = p.read_text(encoding="utf-8")
s = s.replace("from '../features/memory-viz/types'", "from '../types'")
p.write_text(s, encoding="utf-8")

# 2) TreeView：data 字段改成 any 兼容 G6，focusItem 改用 focusElement 兜底
p = root / "features/memory-viz/TreeView/TreeView.tsx"
s = p.read_text(encoding="utf-8")
s = s.replace("data: n,", "data: { ...(n as unknown as Record<string, unknown>) },")
s = s.replace(
"""    try {
      g.focusItem(selectedNodeId);
    } catch {
      /* 节点被过滤掉时忽略 */
    }""",
"""    try {
      const anyG = g as unknown as { focusItem?: (id: string) => void; focusElement?: (id: string) => void };
      if (anyG.focusItem) anyG.focusItem(selectedNodeId);
      else anyG.focusElement?.(selectedNodeId);
    } catch {
      /* 节点被过滤掉时忽略 */
    }""")
p.write_text(s, encoding="utf-8")
print("已修 TS 报错")
