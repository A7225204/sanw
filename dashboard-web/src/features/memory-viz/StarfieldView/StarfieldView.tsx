import { useEffect, useMemo, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { fetchHormones } from '../../../api/memoryViz';
import type { MemoryNode, StarfieldEdge } from '../types';
import { createStarfieldRenderer, isPositiveEmotion, type RendererHandles } from './starfieldRenderer';
import { useVizStore, isVisible, type Polarity } from '../store/vizStore';
import { selectNode } from '../store/linkage';

interface Props {
  halo?: number;   // 0~200，单独控制核心光晕
  nodes: MemoryNode[];
  edges: StarfieldEdge[];
  computing?: boolean;
}

export default function StarfieldView({ nodes, edges, computing, halo }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const handles = useRef<RendererHandles | null>(null);
  const prevNodesRef = useRef<MemoryNode[]>([]);
  const [hover, setHover] = useState<{ x: number; y: number; text: string } | null>(null);
  const [shock, setShock] = useState(0);   // 创伤应激波纹的时间戳
  const { keyword, filters, topicFilterId, focusRequest, clearFocus, selectedNodeId, glow, zoom, polarity, timePct, colorMode, layerFilter } =
    useVizStore();

  // 历史进度：100 = 现在。按时间分位切，而不是按时间线性切——
  // 记忆大多挤在最近几天，线性切会让前半段几乎没变化。
  const timeCutoff = useMemo(() => {
    if (timePct >= 99.5) return 0;
    const ts = nodes
      .map((n) => n.t)
      .filter((x): x is number => typeof x === 'number' && x > 0)
      .sort((a, b) => a - b);
    if (!ts.length) return 0;
    const idx = Math.max(0, Math.min(ts.length - 1, Math.floor((ts.length - 1) * (timePct / 100))));
    return ts[idx];
  }, [nodes, timePct]);

  const visibleNodes = useMemo(
    () =>
      nodes.filter(
        (n) => {
          if (!n.starfield) return false;
          // 星海过滤层：只看创伤 / 只看高保留 / 只看念头
          if (layerFilter === 'trauma' && !(n as unknown as { trauma?: unknown }).trauma) return false;
          if (layerFilter === 'high' && (n.retentionScore ?? 0) < 0.7) return false;
          if (layerFilter === 'thought' && n.category !== 'thought') return false;
          return (
            n.kind !== 'memory' ||
            isVisible(
              {
                retentionScore: n.retentionScore,
                topicId: n.topicId,
                summary: n.summary,
                createdAt: n.createdAt,
              },
              // minRetention 交给渲染层做（那里才能播「飞进黑洞」动画）
              { ...filters, minRetention: 0 },
              keyword,
              topicFilterId,
            )
          );
        }),
    [nodes, filters, keyword, topicFilterId, layerFilter],
  );

  // ---- 幽灵节点：被过滤掉的球多留 1.2 秒，让渲染层把它们飞进黑洞 ----
  const ghostsRef = useRef<Map<string, { node: MemoryNode; until: number }>>(new Map());
  const [, ghostTick] = useState(0);

  const displayNodes = useMemo(() => {
    const now = Date.now();
    const seen = new Set(visibleNodes.map((n) => n.id));
    // 上一帧在、这一帧没了 → 变成幽灵
    for (const n of prevNodesRef.current) {
      if (!seen.has(n.id) && !ghostsRef.current.has(n.id)) {
        ghostsRef.current.set(n.id, { node: n, until: now + 1200 });
      }
    }
    prevNodesRef.current = visibleNodes;
    // 清过期
    for (const [id, rec] of ghostsRef.current) {
      if (rec.until <= now) ghostsRef.current.delete(id);
    }
    if (ghostsRef.current.size === 0) return { nodes: visibleNodes, ghosts: [] as string[] };
    const ghosts = [...ghostsRef.current.values()].map((r) => r.node);
    return { nodes: [...visibleNodes, ...ghosts], ghosts: ghosts.map((g) => g.id) };
  }, [visibleNodes]);

  // 幽灵在的时候持续重绘（否则动画不会推进）
  useEffect(() => {
    if (ghostsRef.current.size === 0) return;
    const t = setInterval(() => ghostTick((x) => x + 1), 120);
    return () => clearInterval(t);
  });

  const ids = new Set(displayNodes.nodes.map((n) => n.id));
  const visibleEdges = edges.filter((e) => ids.has(e.source) && ids.has(e.target));

  // 场景重建只认「节点集合」本身：拖时间轴、开关分类都会让集合变，必须重建；
  // 但父组件无关重渲染不该重建（否则镜头会被重置）。
  const shapeKey = useMemo(
    () => displayNodes.nodes.map((n) => n.id).join('|') + '#' + visibleEdges.length,
    [displayNodes, visibleEdges],
  );

  // 图例：按情感极性（正面绿 / 负面红 / 中性冷白 / 创伤暗红）
  const legendStats = useMemo(() => {
    let pos = 0, neg = 0, neu = 0, trauma = 0;
    for (const n of nodes) {
      if (n.kind !== 'memory') continue;
      if ((n as unknown as { trauma?: unknown }).trauma) { trauma += 1; continue; }
      const e = n.emotion || '中性';
      if (isPositiveEmotion(e)) pos += 1;
      else if (e === '中性') neu += 1;
      else neg += 1;
    }
    return [
      { name: '正面', color: '#3ddc84', count: pos },
      { name: '负面', color: '#ffcc33', count: neg },
      { name: '中性', color: '#bfe0ff', count: neu },
      { name: '创伤', color: '#b03030', count: trauma },
    ].filter((x) => x.count > 0);
  }, [nodes]);

  useEffect(() => {
    if (!ref.current || displayNodes.nodes.length === 0) return;
    handles.current = createStarfieldRenderer({
      container: ref.current,
      nodes: displayNodes.nodes,
      edges: visibleEdges,
      onPick: (n) => {
        // 点星 = 看单颗，退出图例筛选
        useVizStore.getState().setPolarity(null);
        // 点到创伤节点 → 泛一圈暗红应激波纹
        if (n.category === 'trauma' || (n as unknown as { trauma?: unknown }).trauma) {
          setShock(Date.now());
        }
        selectNode(n, 'star');
        // 以点中的这颗粒子为轴心，并推近到看清星团的距离
        handles.current?.focus(n.id, true);
      },
      onSwallowed: (id) => {
        // 真遗忘：后端登记「已遗忘」，刷新也不会再出现（不是视觉把戏）
        void fetch('/api/memory/forget', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ids: [id] }),
        }).catch(() => { /* 后端不可达时不影响视觉 */ });
      },
      onHover: (n) => {
        if (!n) {
          setHover(null);
          return;
        }
        const rect = ref.current!.getBoundingClientRect();
        setHover({ x: rect.width / 2, y: 18, text: n.summary.slice(0, 60) });
      },
    });
    if (displayNodes.ghosts.length) {
      handles.current?.fadeToBlackhole(displayNodes.ghosts);
    }
    return () => {
      handles.current?.dispose();
      handles.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shapeKey]);



  useEffect(() => {
    handles.current?.setGlow(glow / 100);
  }, [glow, nodes]);

  // 拖历史滑块 / 播放：只更新目标位置（每帧现算 life），不重建场景。
  // timeCutoff 一变就调，所以播放时是连续流动的，不是每 350ms 跳一次。
  useEffect(() => {
    handles.current?.updatePositions(displayNodes.nodes, timeCutoff);
  }, [displayNodes, timeCutoff]);

  // 最低保留度：交给渲染层（低保留度的球会播「飞进黑洞」再隐藏）
  useEffect(() => {
    handles.current?.setMinRetention(filters.minRetention);
  }, [filters.minRetention, shapeKey]);

  useEffect(() => {
    handles.current?.setHalo((halo ?? 100) / 100);
  }, [halo, nodes, shapeKey]);

  // 分类五色 / 情绪四色切换（不重建场景，只重算颜色）
  useEffect(() => {
    handles.current?.setColorMode(colorMode);
  }, [colorMode, shapeKey]);

  // 内分泌染色：皮质醇高偏红、多巴胺高提亮
  const hormones = useQuery({
    queryKey: ['memory-hormones'],
    queryFn: fetchHormones,
    refetchInterval: 15_000,
    staleTime: 5_000,
  });
  useEffect(() => {
    if (hormones.data) handles.current?.setHormones(hormones.data.hormones);
  }, [hormones.data, shapeKey]);

  // 时间轴不重建场景，只改粒子可见性和连线
  useEffect(() => {
    handles.current?.setTimeCutoff(timeCutoff);
  }, [timeCutoff, shapeKey]);

  useEffect(() => {
    handles.current?.setPolarity(polarity);
  }, [polarity, nodes]);

  useEffect(() => {
    handles.current?.setZoom(zoom / 100);
  }, [zoom, nodes]);

  useEffect(() => {
    if (focusRequest?.from === 'tree') {
      handles.current?.focus(focusRequest.nodeId);
      clearFocus();
    }
  }, [focusRequest, clearFocus]);

  useEffect(() => {
    const cur = nodes.find((x) => x.id === selectedNodeId);
    const topicIds = cur?.topicId
      ? nodes.filter((n) => n.topicId === cur.topicId).map((n) => n.id)
      : [];
    handles.current?.setHighlight(selectedNodeId, topicIds);
  }, [selectedNodeId, nodes]);

  return (
    <div style={{ position: 'relative', width: '100%', height: '100%' }}>
      <div ref={ref} style={{ width: '100%', height: '100%' }} />

      {legendStats.length > 0 && (
        <div style={legendStyle}>
          <div style={{ color: '#6f93ae', marginBottom: 6, fontSize: 12 }}>
            情绪光晕
            {polarity && (
              <span
                onClick={() => useVizStore.getState().setPolarity(null)}
                style={{ marginLeft: 8, cursor: 'pointer', color: '#d6eeff', fontSize: 11 }}
                title="清除筛选"
              >
                只看{polarity} ✕
              </span>
            )}
          </div>
          {legendStats.map((v) => {
            const on = polarity === v.name;
            const dim = polarity !== null && !on;
            return (
              <div
                key={v.name}
                onClick={() => useVizStore.getState().setPolarity(on ? null : (v.name as Polarity))}
                title={on ? '再点一次取消' : `只看${v.name}`}
                style={{
                  display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4,
                  cursor: 'pointer', borderRadius: 6, padding: '2px 6px', marginLeft: -6,
                  opacity: dim ? 0.45 : 1,
                  background: on ? 'rgba(120,210,255,.12)' : 'transparent',
                  outline: on ? '1px solid rgba(120,210,255,.35)' : 'none',
                }}
              >
                <i style={{ width: 8, height: 8, borderRadius: 4, background: v.color, display: 'inline-block' }} />
                <span style={{ color: on ? '#d6eeff' : '#9fc0d6', fontSize: 12 }}>{v.name}</span>
                <span style={{ color: '#5d7d96', fontSize: 11 }}>{v.count}</span>
              </div>
            );
          })}
          <div style={{ marginTop: 4, fontSize: 10, color: '#4d6a80' }}>点一下只看该类，再点取消</div>
          <div style={{ marginTop: 8, paddingTop: 6, borderTop: '1px dashed rgba(80,130,170,.2)', fontSize: 11, color: '#5f7e96', lineHeight: 1.8 }}>
            <div><i style={{ width: 14, height: 2, background: '#ffc861', display: 'inline-block', verticalAlign: 'middle', marginRight: 6 }} />金线：你 → 各星河</div>
            <div><i style={{ width: 14, height: 2, background: '#3ddc84', display: 'inline-block', verticalAlign: 'middle', marginRight: 6 }} />绿线：同一个人（按 QQ）跨群</div>
            <div><i style={{ width: 14, height: 2, background: '#3f86bd', display: 'inline-block', verticalAlign: 'middle', marginRight: 6 }} />蓝线：群内记忆关联</div>
          </div>
        </div>
      )}

      {hormones.data && (hormones.data.hormones.MEL ?? 0) > 0.65 && (
        <div
          style={{
            position: 'absolute', left: '50%', top: '50%',
            width: 280, height: 280, marginLeft: -140, marginTop: -140,
            pointerEvents: 'none',
            background: 'radial-gradient(circle, rgba(120,80,200,.35) 0%, rgba(60,30,110,.18) 45%, transparent 70%)',
            borderRadius: '50%',
            animation: 'dreamSwirl 18s linear infinite',
            mixBlendMode: 'screen',
          }}
        />
      )}
      {shock > 0 && (
        <div
          key={shock}
          style={{
            position: 'absolute', inset: 0, pointerEvents: 'none',
            borderRadius: '50%',
            boxShadow: 'inset 0 0 120px 40px rgba(192,48,48,.55)',
            animation: 'traumaShock 900ms ease-out forwards',
          }}
        />
      )}
      {hover && (
        <div style={tipStyle}>{hover.text}…</div>
      )}

      {computing && <div style={overlayStyle}>🌌 星海生成中（后端正在算坐标，算完自动刷新）</div>}
      {visibleNodes.length === 0 && !computing && (
        <div style={overlayStyle}>当前过滤条件下没有可见节点</div>
      )}
    </div>
  );
}

const overlayStyle: React.CSSProperties = {
  position: 'absolute',
  inset: 0,
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  color: '#8fb8d8',
  background: 'rgba(3,6,13,.72)',
  fontSize: 14,
  pointerEvents: 'none',
};

const legendStyle: React.CSSProperties = {
  position: 'absolute',
  left: 16,
  top: 16,
  padding: '10px 12px',
  background: 'rgba(6,14,24,.62)',
  border: '1px solid rgba(70,130,180,.28)',
  borderRadius: 10,
  backdropFilter: 'blur(3px)',
};

const tipStyle: React.CSSProperties = {
  position: 'absolute',
  left: '50%',
  top: 16,
  transform: 'translateX(-50%)',
  padding: '6px 12px',
  background: 'rgba(6,14,24,.85)',
  border: '1px solid rgba(120,210,255,.35)',
  borderRadius: 8,
  color: '#d6eeff',
  fontSize: 12,
  pointerEvents: 'none',
};
