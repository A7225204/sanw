import { create } from 'zustand';
import type { ScopeId } from '../types';

export interface VizFilters {
  minRetention: number;
  onlyDecaying: boolean;
  topicIds: string[];
  timeRange: [string, string] | null;
}

export type Polarity = '正面' | '负面' | '中性' | '创伤';

interface VizState {
  scope: ScopeId | null;
  selectedNodeId: string | null;
  selectedTopicId: string | null;
  topicFilterId: string | null;  // 下拉框筛选用；点节点只设 selectedTopicId，不过滤
  polarity: Polarity | null; // 图例筛选：点了只高亮这一类，再点取消
  timePct: number;           // 历史进度条 0~100，100=现在
  colorMode: 'category' | 'emotion'; // 分类五色 / 情绪四色
  layerFilter: 'all' | 'trauma' | 'high' | 'thought'; // 星海过滤层
  keyword: string;
  filters: VizFilters;
  focusRequest: { nodeId: string; from: 'tree' | 'star' } | null;
  glow: number; // 0~100 粒子发光度
  zoom: number; // 0~100 镜头远近（100=最近）
  collapsedTopics: string[];
  setScope: (s: ScopeId | null) => void;
  setSelected: (id: string | null) => void;
  setTopic: (id: string | null) => void;
  setTopicFilter: (id: string | null) => void;
  setPolarity: (p: Polarity | null) => void;
  setColorMode: (m: 'category' | 'emotion') => void;
  setLayerFilter: (f: 'all' | 'trauma' | 'high' | 'thought') => void;
  setTimePct: (v: number) => void;
  /** 选具体日期时用绝对时间戳（毫秒），优先于 timePct */
  specificAt: number | null;
  setSpecificAt: (v: number | null) => void;
  setKeyword: (k: string) => void;
  setFilters: (f: Partial<VizFilters>) => void;
  setGlow: (v: number) => void;
  setZoom: (v: number) => void;
  requestFocus: (nodeId: string, from: 'tree' | 'star') => void;
  clearFocus: () => void;
  toggleTopic: (id: string) => void;
}

export const useVizStore = create<VizState>((set) => ({
  scope: null,
  selectedNodeId: null,
  selectedTopicId: null,
  topicFilterId: null,
  polarity: null,
  timePct: 100,
  colorMode: 'emotion',
  layerFilter: 'all',
  keyword: '',
  filters: { minRetention: 0, onlyDecaying: false, topicIds: [], timeRange: null },
  focusRequest: null,
  glow: 50,
  zoom: 50,
  collapsedTopics: [],
  setScope: (scope) =>
    set({ scope, selectedNodeId: null, selectedTopicId: null, topicFilterId: null, focusRequest: null, polarity: null }),
  setSelected: (id) => set({ selectedNodeId: id }),
  setTopic: (id) => set({ selectedTopicId: id }),
  setTopicFilter: (id) => set({ topicFilterId: id }),
  setPolarity: (polarity) => set({ polarity, selectedNodeId: null }),
  setColorMode: (colorMode) => set({ colorMode }),
  setLayerFilter: (layerFilter) => set({ layerFilter }),
  setTimePct: (v) => set({ timePct: Math.max(0, Math.min(100, v)) }),
  specificAt: null,
  setSpecificAt: (v) => set({ specificAt: v }),
  setKeyword: (k) => set({ keyword: k }),
  setFilters: (f) => set((s) => ({ filters: { ...s.filters, ...f } })),
  setGlow: (v) => set({ glow: Math.max(0, Math.min(100, v)) }),
  setZoom: (v) => set({ zoom: Math.max(0, Math.min(100, v)) }),
  requestFocus: (nodeId, from) => set({ focusRequest: { nodeId, from } }),
  clearFocus: () => set({ focusRequest: null }),
  toggleTopic: (id) =>
    set((s) => ({
      collapsedTopics: s.collapsedTopics.includes(id)
        ? s.collapsedTopics.filter((x) => x !== id)
        : [...s.collapsedTopics, id],
    })),
}));

export function isVisible(
  node: { retentionScore: number; topicId: string | null; summary: string; createdAt: string },
  filters: VizFilters,
  keyword: string,
  topicFilterId: string | null,
): boolean {
  if (node.retentionScore < filters.minRetention) return false;
  if (filters.onlyDecaying && node.retentionScore >= 0.35) return false;
  if (topicFilterId && node.topicId && node.topicId !== topicFilterId) return false;
  if (filters.topicIds.length && node.topicId && !filters.topicIds.includes(node.topicId)) {
    return false;
  }
  if (keyword.trim()) {
    const k = keyword.trim().toLowerCase();
    if (!node.summary.toLowerCase().includes(k)) return false;
  }
  return true;
}
