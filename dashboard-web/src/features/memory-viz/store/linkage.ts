import { useVizStore } from './vizStore';
import type { MemoryNode } from '../types';

/**
 * 双向联动的唯一入口：树和星海都只调这里，避免各写一套逻辑。
 *
 *  · 选中节点：写入 selectedNodeId，并给"另一个视图"发 focusRequest
 *  · 选中主题：写入 selectedTopicId（两边同时高亮）
 *  · 搜索/过滤：只写 store，两个视图各自用 isVisible 过滤，可见集合天然一致
 */
export function selectNode(node: MemoryNode | null, from: 'tree' | 'star'): void {
  const st = useVizStore.getState();
  if (!node) {
    st.setSelected(null);
    return;
  }
  st.setSelected(node.id);
  if (node.topicId) st.setTopic(node.topicId);
  st.requestFocus(node.id, from);
}

export function selectTopic(topicId: string | null): void {
  const st = useVizStore.getState();
  st.setTopic(topicId);
  if (topicId) st.setSelected(topicId);
}

export function onKeyword(keyword: string): void {
  useVizStore.getState().setKeyword(keyword);
}

export function onFilters(patch: Parameters<ReturnType<typeof useVizStore.getState>['setFilters']>[0]): void {
  useVizStore.getState().setFilters(patch);
}
