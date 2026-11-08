import { useEffect, useMemo, useRef, useState } from 'react';
import { Graph } from '@antv/g6';
import type { MemoryNode, TreeEdge } from '../types';
import { buildTreeGraph } from './treeLayout';
import { useVizStore, isVisible } from '../store/vizStore';
import { selectNode } from '../store/linkage';

interface Props {
  nodes: MemoryNode[];
  edges: TreeEdge[];
}

export default function TreeView({ nodes, edges }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const graphRef = useRef<Graph | null>(null);
  // 逐字段订阅：G6 图重建很贵，不能跟着时间滑块每帧重渲染
  const keyword = useVizStore((st) => st.keyword);
  const filters = useVizStore((st) => st.filters);
  const selectedTopicId = useVizStore((st) => st.selectedTopicId);
  const topicFilterId = useVizStore((st) => st.topicFilterId);
  const selectedNodeId = useVizStore((st) => st.selectedNodeId);
  // 默认只展开到「主题」层；点主题才展开它下面的记忆，避免一屏几百个叶子
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const data = useMemo(() => {
    const visible = (n: MemoryNode) => {
      if (n.kind !== 'memory') return true;
      // 展开状态按「父节点是否展开」判断：
      // 会话树的父是 topic，实体树的父是分类分支（entity:persona 等）。
      // 之前只看 topicId，导致实体树所有子节点永远不显示（树变成死图）。
      if (!expanded.has(n.parentId ?? '') && !expanded.has(n.topicId ?? '')) return false;
      return isVisible(
        {
          retentionScore: n.retentionScore,
          topicId: n.topicId,
          summary: n.summary,
          createdAt: n.createdAt,
        },
        filters,
        keyword,
        topicFilterId,
      );
    };
    return buildTreeGraph(nodes, edges, visible, []);
  }, [nodes, edges, filters, keyword, topicFilterId, expanded]);

  useEffect(() => {
    if (!ref.current) return;
    const graph = new Graph({
      container: ref.current,
      autoFit: 'view',
      data: {
        nodes: data.nodes.map((n) => {
          const isSel = n.id === selectedNodeId;
          return {
            id: n.id,
            data: { ...(n as unknown as Record<string, unknown>) },
            style: {
              // 关键修复：记忆节点以前是 slice(0,0)=空字符串，所以只剩黑点
              labelText:
                n.kind === 'scope' ? n.summary.slice(0, 16)
                  : n.kind === 'topic' ? n.summary.slice(0, 14)
                    : n.summary.slice(0, 12),
              labelPlacement: 'right',
              labelFontSize: 12,
              labelFill: isSel ? '#ffe9b0' : '#e6f2ff',
              labelBackground: true,
              labelBackgroundFill: 'rgba(6,16,28,.85)',
              labelPadding: [2, 4],
              size: n.kind === 'scope' ? 30 : n.kind === 'topic' ? 18 : 12,
              fill: n.kind === 'topic' ? '#4fa3ff' : n.kind === 'scope' ? '#8ef0ff' : '#7fd4ff',
              stroke: isSel ? '#ffd24a' : 'rgba(127,212,255,.35)',
              lineWidth: isSel ? 3 : 1,
              opacity: selectedTopicId && n.topicId && n.topicId !== selectedTopicId ? 0.18 : 1,
            },
          };
        }),
        edges: data.edges.map((e) => ({
          id: `${e.source}->${e.target}`,
          source: e.source,
          target: e.target,
        })),
      },
      // 横向紧凑树；加大垂直间距，避免分支挤成一坨
      layout: {
        type: 'compact-box',
        direction: 'TB',
        getHeight: () => 34,
        getWidth: () => 90,
        getVGap: () => 54,
        getHGap: () => 26,
      },
      behaviors: ['drag-canvas', 'zoom-canvas'],
      node: { type: 'circle' },
      edge: {
        type: 'cubic-vertical',
        // 之前 1px 深蓝线在纯黑底上几乎看不见
        style: { stroke: '#3f6f9f', lineWidth: 1.6 },
      },
    });
    // 关键：render() 是异步的。effect 会因为 data/selectedNodeId 变化而快速重跑，
    // 旧实例在 render 还没结束时就被 destroy()，G6 会在 console.error「graph instance
    // has been destroyed」，随后 this.context.element 已置空 → 未捕获 TypeError。
    // 这里把销毁延后到 render 结束，避免「销毁后继续 draw」。
    let disposed = false;
    const rendered = graph.render()
      .then(() => {
        if (disposed) return;
        try {
          (graph as any).fitView?.();
          (graph as any).zoomTo?.(0.9);
        } catch { /* noop */ }
      })
      .catch(() => { /* render 被销毁打断时忽略 */ });
    graphRef.current = graph;

    const handler = (evt: { targetType?: string; target?: { id?: string } }) => {
      const id = evt?.target?.id;
      if (!id) return;
      const node = nodes.find((n) => n.id === id);
      if (!node) return;
      if (node.kind === 'topic' || node.kind === 'scope') {
        // 分支节点：手风琴式展开——点开一个就收起其它，再点自己收起。
        // 分支点击不触发星海聚焦，否则每点一次镜头就飞一次，很烦。
        setExpanded((prev) => {
          const next = new Set(prev);
          if (next.has(node.id)) {
            next.delete(node.id);
          } else {
            next.clear();
            next.add(node.id);
          }
          return next;
        });
        return;
      }
      // 叶子记忆：右侧详情 + 星海聚焦
      selectNode(node, 'tree');
    };
    graph.on('node:click', handler as never);
    return () => {
      disposed = true;
      graphRef.current = null;
      void rendered.finally(() => {
        try { graph.destroy(); } catch { /* noop */ }
      });
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, selectedNodeId]);

  useEffect(() => {
    const g = graphRef.current;
    if (!g || !selectedNodeId) return;
    try {
      const anyG = g as unknown as {
        focusItem?: (id: string) => void;
        focusElement?: (id: string) => void;
      };
      if (anyG.focusItem) anyG.focusItem(selectedNodeId);
      else anyG.focusElement?.(selectedNodeId);
    } catch {
      /* 节点被过滤掉时忽略 */
    }
  }, [selectedNodeId]);

  return <div ref={ref} style={{ width: '100%', height: '100%', background: '#040a14' }} />;
}
