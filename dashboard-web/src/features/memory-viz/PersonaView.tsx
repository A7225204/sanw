import { useEffect, useMemo, useRef, useState } from 'react';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { fetchPersona, postGrowthExtract, type PersonaResponse } from '../../api/memoryViz';
import { useVizStore } from './store/vizStore';
import type { MemoryNode, StarfieldEdge } from './types';
import StarfieldView from './StarfieldView/StarfieldView';
import MemoryFlowView from './StarfieldView/MemoryFlowView';

const STORE_KEY = 'dsh-persona-cats';

export default function PersonaView({
  scope, halo, dist, dragMode, onScale,
}: {
  scope: string | null;
  halo?: number;
  dist?: { shortR?: number; planetR?: number; dustR?: number };
  dragMode?: boolean;
  onScale?: (f: number) => void;
}) {
  const dragRef = useRef<{ y: number; s: number } | null>(null);
  const [scale, setScale] = useState(1);
  const [flowMode, setFlowMode] = useState(true);   // 默认用新的记忆流视图
  const applyScale = (v: number) => {
    const f = Math.max(0.1, Math.min(4, v));
    setScale(f);
    onScale?.(f);
  };
  // 时间指针：底部历史滑块决定「现在」是哪一刻。
  // 注意：queryFn 里不能引用 q 自己（自引用会让 TS 推断成 any），
  // 所以已有数据从 queryClient 缓存里取。
  const timePct = useVizStore((st) => st.timePct);
  // 拖动时每秒几十次请求会把场景卡死：停下 350ms 再发。
  const [debouncedPct, setDebouncedPct] = useState(100);
  useEffect(() => {
    const t = setTimeout(() => setDebouncedPct(timePct), 350);
    return () => clearTimeout(t);
  }, [timePct]);
  const q = useQuery({
    queryKey: ['memory-persona', scope, dist?.shortR, dist?.planetR, dist?.dustR, debouncedPct],
    queryFn: () => {
      let atMs: number | undefined;
      if (debouncedPct < 99.5) {
        const prev = qc.getQueryData<PersonaResponse>([
          'memory-persona', scope, dist?.shortR, dist?.planetR, dist?.dustR, 100,
        ]);
        const ts: number[] = (prev?.nodes ?? [])
          .map((n: MemoryNode) => n.t)
          .filter((x: number | undefined): x is number => typeof x === 'number' && x > 0)
          .sort((a: number, b: number) => a - b);
        if (ts.length) {
          atMs = ts[Math.max(0, Math.min(ts.length - 1,
            Math.floor((ts.length - 1) * (debouncedPct / 100))))];
        }
      }
      return fetchPersona(scope, { ...dist, atMs });
    },
    staleTime: 30_000,
    // 时间指针一变就换 queryKey（新 key 没有缓存）→ 不保留旧数据的话，
    // 下面的 isLoading 分支会把整个记忆流画布卸载重建一次，拖滑块就一顿
    placeholderData: keepPreviousData,
  });

  const [off, setOff] = useState<Record<string, boolean>>(() => {
    try {
      return JSON.parse(localStorage.getItem(STORE_KEY) || '{}') as Record<string, boolean>;
    } catch {
      return {};
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(STORE_KEY, JSON.stringify(off));
    } catch {
      /* ignore */
    }
  }, [off]);

  const qc = useQueryClient();
  const extract = useMutation({
    mutationFn: postGrowthExtract,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['memory-persona', scope] });
    },
  });

  const data = q.data;
  const cats = data?.categories ?? [];

  const { nodes, edges } = useMemo(() => {
    if (!data) return { nodes: [], edges: [] };
    const keep = (cat?: string) => {
      if (!cat || cat === 'center') return true;
      return !off[cat];
    };
    const ns = data.nodes.filter((n: MemoryNode) => keep(n.category));
    const ids = new Set(ns.map((n: MemoryNode) => n.id));
    const es = data.edges.filter((e: StarfieldEdge) => ids.has(e.source) && ids.has(e.target));
    return { nodes: ns, edges: es };
  }, [data, off]);

  if (!q.data) return <div style={hint}>加载人格星海…</div>;
  if (q.isError) return <div style={hint}>人格星海加载失败（后端 /api/memory/persona）</div>;

  return (
    <div
      style={{ display: 'flex', width: '100%', height: '100%', touchAction: 'none', cursor: dragMode ? 'ns-resize' : 'default' }}
      onPointerDown={(e) => {
        if (!dragMode) return;
        dragRef.current = { y: e.clientY, s: scale };
      }}
      onPointerMove={(e) => {
        if (!dragRef.current) return;
        const dy = dragRef.current.y - e.clientY;   // 向上拖 = 变远
        applyScale(dragRef.current.s * (1 + dy * 0.006));
      }}
      onPointerUp={() => { dragRef.current = null; }}
      onPointerLeave={() => { dragRef.current = null; }}
    >
      <div style={{ flex: 1, minWidth: 0, position: 'relative' }}>
        {flowMode
          ? <MemoryFlowView hiddenCats={off} />
          : <StarfieldView nodes={nodes} edges={edges} computing={false} halo={halo} />}
      </div>
      <div style={panelStyle}>
        <label style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10, fontSize: 12, color: '#cfe8ff' }}>
          <input type="checkbox" checked={flowMode} onChange={(e) => setFlowMode(e.target.checked)} />
          记忆流视图（新）
        </label>
        <div style={{ fontSize: 12, color: '#6f93ae', marginBottom: 8 }}>关系圈层</div>
        {cats.map((c: { key: string; label?: string; count?: number; status?: string }) => {
          const on = !off[c.key];
          return (
            <label
              key={c.key}
              style={{
                display: 'flex', alignItems: 'center', gap: 8, padding: '5px 6px',
                borderRadius: 6, cursor: 'pointer', marginBottom: 2,
                background: on ? 'rgba(120,210,255,.08)' : 'transparent',
              }}
            >
              <input
                type="checkbox"
                checked={on}
                onChange={() => setOff((s) => ({ ...s, [c.key]: on }))}
              />
              <span style={{ color: on ? '#d6eeff' : '#5d7d96', fontSize: 12 }}>{c.label}</span>
              <span style={{ marginLeft: 'auto', color: c.count ? '#7fd4ff' : '#4d6a80', fontSize: 11 }}>
                {c.status === 'empty' ? '暂无' : c.count}
              </span>
            </label>
          );
        })}
        <div
          style={{
            marginTop: 10, paddingTop: 8, borderTop: '1px dashed rgba(80,130,170,.2)',
            fontSize: 11, color: '#4d6a80', lineHeight: 1.6,
          }}
        >
          关掉某一类 = 从星海移除；勾回来就恢复
        </div>
        <button
          style={{
            marginTop: 10, width: '100%', padding: '6px 0', fontSize: 12, cursor: 'pointer',
            color: '#ffe9b0', background: 'rgba(255,210,74,.12)',
            border: '1px solid rgba(255,210,74,.35)', borderRadius: 8,
          }}
          disabled={extract.isPending}
          onClick={() => extract.mutate()}
          title="从最近记忆里提炼她这段时间的变化"
        >
          {extract.isPending ? '提炼中…' : '✨ 跑一次提炼'}
        </button>
        {extract.isError && (
          <div style={{ marginTop: 6, fontSize: 11, color: '#ff9c9c' }}>提炼失败，看后端日志</div>
        )}
        {extract.data && (
          <div style={{ marginTop: 6, fontSize: 11, color: '#a8ffd0' }}>
            新增 {extract.data.added} 条成长
          </div>
        )}
      </div>
    </div>
  );
}

const hint: React.CSSProperties = { padding: 20, color: '#8fb8d8' };
const panelStyle: React.CSSProperties = {
  width: 168, flexShrink: 0, padding: '10px 10px 10px 4px',
  borderLeft: '1px solid rgba(70,130,180,.2)', overflow: 'auto',
};

