import pathlib

root = pathlib.Path(r"D:\qq-ai-bot\dashboard-web\src\features\memory-viz")

# ---------- 1) treeLayout：每个主题最多展开 14 条记忆，其余聚成 "+N" 节点 ----------
p = root / "TreeView/treeLayout.ts"
s = p.read_text(encoding="utf-8")
s = s.replace(
"""  const nodes = allNodes.filter((n) => keep.has(n.id));""",
"""  // 单主题下记忆过多时只留最重要的 14 条，其余聚成一个 "+N" 节点（LOD）
  const SIBLING_CAP = 14;
  const byParent = new Map<string, MemoryNode[]>();
  for (const n of allNodes) {
    if (!keep.has(n.id) || n.kind !== 'memory' || !n.parentId) continue;
    if (!byParent.has(n.parentId)) byParent.set(n.parentId, []);
    byParent.get(n.parentId)!.push(n);
  }
  const extraByParent = new Map<string, number>();
  for (const [pid, kids] of byParent) {
    if (kids.length <= SIBLING_CAP) continue;
    kids.sort((a, b) => b.importance - a.importance);
    for (const k of kids.slice(SIBLING_CAP)) keep.delete(k.id);
    extraByParent.set(pid, kids.length - SIBLING_CAP);
  }
  const clusterNodes: MemoryNode[] = [];
  for (const [pid, extra] of extraByParent) {
    const parent = byId.get(pid);
    clusterNodes.push({
      id: `cluster:${pid}`,
      scope: parent?.scope ?? '',
      parentId: pid,
      topicId: pid,
      summary: `+${extra} 条`,
      sourceMessageIds: [],
      retentionScore: 0,
      importance: 0,
      recency: 0,
      frequency: 0,
      createdAt: '',
      lastAccessedAt: '',
      kind: 'memory',
    });
    keep.add(`cluster:${pid}`);
  }

  const nodes = [...allNodes.filter((n) => keep.has(n.id)), ...clusterNodes];""")

# cluster 节点的边也要补
s = s.replace(
"""  const edges = allEdges.filter((e) => keep.has(e.source) && keep.has(e.target));""",
"""  let edges = allEdges.filter((e) => keep.has(e.source) && keep.has(e.target));
  for (const c of clusterNodes) {
    edges = [...edges, { source: c.parentId as string, target: c.id, kind: 'hierarchy' as const }];
  }""")
p.write_text(s, encoding="utf-8")

# ---------- 2) TreeView：标签颜色 + 更明显的点 ----------
p = root / "TreeView/TreeView.tsx"
s = p.read_text(encoding="utf-8")
s = s.replace("            labelFontSize: 11,",
              "            labelFontSize: 12,\n            labelFill: '#bfe9ff',")
s = s.replace("            labelText: n.kind === 'memory' ? '' : n.summary.slice(0, 12),",
              "            labelText: n.kind === 'memory' ? n.summary.slice(0, 0) : n.summary.slice(0, 14),")
p.write_text(s, encoding="utf-8")
print("已加每条主题 14 条上限 + 聚合节点 + 标签样式")
