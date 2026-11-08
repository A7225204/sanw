import type { MemoryNode, TreeEdge } from '../types';

export interface TreeGraphData {
  nodes: MemoryNode[];
  edges: TreeEdge[];
  /** >200 时隐藏次要标签；>500 时聚合低重要度节点 */
  lod: 'none' | 'compact' | 'cluster';
}

export function buildTreeGraph(
  allNodes: MemoryNode[],
  allEdges: TreeEdge[],
  visible: (n: MemoryNode) => boolean,
  collapsedTopics: string[],
): TreeGraphData {
  const byId = new Map(allNodes.map((n) => [n.id, n]));
  const childrenOf = new Map<string | null, MemoryNode[]>();
  for (const n of allNodes) {
    const key = n.parentId;
    if (!childrenOf.has(key)) childrenOf.set(key, []);
    childrenOf.get(key)!.push(n);
  }

  const keep = new Set<string>();
  const walk = (n: MemoryNode, depth: number) => {
    // 主题层默认展开，事件层按需折叠（spec：初始只渲染到主题层）
    if (n.kind === 'memory' && collapsedTopics.includes(n.parentId ?? '')) return;
    if (!visible(n) && n.kind === 'memory') return;
    keep.add(n.id);
    if (depth > 6) return;
    for (const c of childrenOf.get(n.id) ?? []) walk(c, depth + 1);
  };
  for (const root of childrenOf.get(null) ?? []) walk(root, 0);

  // 祖先补全：选中/搜索命中的节点，父链必须保留
  for (const n of allNodes) {
    if (!keep.has(n.id)) continue;
    let p = n.parentId;
    while (p) {
      keep.add(p);
      p = byId.get(p)?.parentId ?? null;
    }
  }

  // 单主题下记忆过多时只留最重要的 14 条，其余聚成一个 "+N" 节点（LOD）
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

  const nodes = [...allNodes.filter((n) => keep.has(n.id)), ...clusterNodes];
  let edges = allEdges.filter((e) => keep.has(e.source) && keep.has(e.target));
  for (const c of clusterNodes) {
    edges = [...edges, { source: c.parentId as string, target: c.id, kind: 'hierarchy' as const }];
  }
  const lod: TreeGraphData['lod'] =
    nodes.length > 500 ? 'cluster' : nodes.length > 200 ? 'compact' : 'none';
  return { nodes, edges, lod };
}

/** 从选中节点向上找主题，返回需要展开的主题 id 列表（联动用）。 */
export function ancestorsOf(node: MemoryNode, byId: Map<string, MemoryNode>): string[] {
  const out: string[] = [];
  let cur: MemoryNode | undefined = node;
  while (cur) {
    out.push(cur.id);
    cur = cur.parentId ? byId.get(cur.parentId) : undefined;
  }
  return out;
}
