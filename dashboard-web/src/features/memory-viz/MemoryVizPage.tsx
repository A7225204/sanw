import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { fetchEntityTree, fetchHormones, fetchScopes, postClearThoughts, postForceDream, postHormoneInject } from '../../api/memoryViz';
import { useVizStore } from './store/vizStore';
import { useTreeData } from './TreeView/useTreeData';
import { useStarfieldData } from './StarfieldView/useStarfieldData';
import TreeView from './TreeView/TreeView';
import StarfieldView from './StarfieldView/StarfieldView';
import MemoryDetailPanel from './DetailPanel/MemoryDetailPanel';
import PersonaView from './PersonaView';

type Tab = 'split' | 'persona';

// 拖时间滑块时父组件每像素重渲染一次：这三个子树重渲染没有意义（各自订阅各自的数据）
const MemoDetailPanel = memo(MemoryDetailPanel);
const MemoTreeView = memo(TreeView);
const MemoPersonaView = memo(PersonaView);


/** 把毫秒时间戳格式化成「2026-10-31 22:15:30」；0 表示「现在」 */
function fmtTime(ms: number): string {
  if (!ms) return '现在';
  const d = new Date(ms);
  const p2 = (x: number) => String(x).padStart(2, '0');
  return `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())} `
    + `${p2(d.getHours())}:${p2(d.getMinutes())}:${p2(d.getSeconds())}`;
}

export default function MemoryVizPage() {

  // 支持 /memory-viz/#persona 直接进人格视图、#persona&at=0 指定时间指针
  const [tab, setTab] = useState<Tab>(() =>
    typeof location !== 'undefined' && location.hash.includes('persona') ? 'persona' : 'split',
  );
  const [halo, setHalo] = useState(55);
  // 人格视图三层距离（手动调，不用改代码）
  const [dragMode, setDragMode] = useState(false);   // 按钮：按住拖动调远近
  const [distScale, setDistScale] = useState(1);     // 拖动产生的倍数
  const [shortR, setShortR] = useState(0.4);
  const [planetR, setPlanetR] = useState(0.053);
  const [dustR, setDustR] = useState(0.194);
  const personaDist = useMemo(
    () => ({ shortR: shortR * distScale, planetR: planetR * distScale, dustR: dustR * distScale }),
    [shortR, planetR, dustR, distScale],
  );
  // 逐字段订阅：整仓订阅会让这个页面在时间滑块每像素都重渲染一次
  const scope = useVizStore((st) => st.scope);
  const setScope = useVizStore((st) => st.setScope);
  const keyword = useVizStore((st) => st.keyword);
  const setKeyword = useVizStore((st) => st.setKeyword);
  const filters = useVizStore((st) => st.filters);
  const setFilters = useVizStore((st) => st.setFilters);
  const topicFilterId = useVizStore((st) => st.topicFilterId);
  const setTopicFilter = useVizStore((st) => st.setTopicFilter);
  const glow = useVizStore((st) => st.glow);
  const setGlow = useVizStore((st) => st.setGlow);
  const zoom = useVizStore((st) => st.zoom);
  const setZoom = useVizStore((st) => st.setZoom);

  useEffect(() => {
    const m = location.hash.match(/at=(\d+(?:\.\d+)?)/);
    if (!m) return;
    const v = Math.max(0, Math.min(100, Number(m[1])));
    if (Number.isFinite(v)) useVizStore.getState().setTimePct(v);
  }, []);

  const scopes = useQuery({ queryKey: ['memory-scopes'], queryFn: fetchScopes, staleTime: 60_000 });
  const hormones = useQuery({
    queryKey: ['memory-hormones'],
    queryFn: fetchHormones,
    refetchInterval: 15_000,
    staleTime: 5_000,
  });
  const tree = useTreeData(scope);
  const star = useStarfieldData(scope);

  const onPersonaScale = useCallback((f: number) => setDistScale(f), []);
  // 时间轴需要的时间戳：memo 住，否则每次父组件重渲染都会把新数组塞给 TimeBar
  const starNodes = useMemo(
    () => (star.data?.data?.nodes ?? []) as Array<{ t?: number }>,
    [star.data],
  );

  // 实体树：按分类组织（人格/人物卡/长期/经历/念头/梦境），优先于会话树
  const entity = useQuery({
    queryKey: ['memory-entity-tree', scope],
    queryFn: () => fetchEntityTree(scope),
    staleTime: 60_000,
  });
  // 必须 useMemo：否则拖发光/光晕滑块引起重渲染，ref 变化会让 TreeView 的
  // useMemo 失效 → G6 图重建（树会闪一下）。
  const treeNodes = useMemo(
    () => (entity.data?.nodes?.length ? entity.data.nodes : tree.data?.nodes) ?? [],
    [entity.data, tree.data],
  );
  const treeEdges = useMemo(
    () => ((entity.data?.nodes?.length ? entity.data.edges : tree.data?.edges) ?? [])
      .map((e) => ({ source: e.source, target: e.target, kind: 'hierarchy' as const })),
    [entity.data, tree.data],
  );

  useEffect(() => {
    if (!scope && scopes.data?.length) setScope(scopes.data[0].scope);
  }, [scopes.data, scope, setScope]);

  const topics = (tree.data?.nodes ?? []).filter((n) => n.kind === 'topic');

  return (
    <div style={pageStyle}>
      <header style={headerStyle}>
        <strong style={{ color: '#bfe9ff', fontSize: 16, letterSpacing: 1 }}>🌌 记忆可视化</strong>
        <span style={{ fontSize: 10, opacity: 0.5 }}>BUILD-2141</span>

        <select
          value={scope ?? ''}
          onChange={(e) => setScope(e.target.value || null)}
          style={inputStyle}
        >
          {(scopes.data ?? []).map((s) => (
            <option key={s.scope} value={s.scope}>
              {s.label}（{s.count} 条 / {s.topics} 主题）
            </option>
          ))}
        </select>

        <input
          placeholder="搜索记忆…"
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
          style={{ ...inputStyle, width: 160 }}
        />

        <label style={miniLabel} title="核心光晕的强度（单独控制，不影响星点亮度）">
          光晕 {halo}
          <input
            type="range"
            min={0}
            max={200}
            value={halo}
            onChange={(e) => setHalo(Number(e.target.value))}
          />
        </label>
        <label style={miniLabel}>
          发光 {glow}
          <input
            type="range"
            min={0}
            max={1000}
            value={glow}
            onChange={(e) => setGlow(Number(e.target.value))}
            style={{ width: 90 }}
          />
        </label>

        <button
          style={tabBtn(dragMode)}
          title="点亮后：按住画布上下拖动，调整各层离金色球的距离"
          onClick={() => setDragMode((v) => !v)}
        >
          调距离 {dragMode ? 'ON' : ''}
        </button>
        <label style={miniLabel}>
          缩放 {zoom}
          <input
            type="range"
            min={0}
            max={1000}
            value={zoom}
            onChange={(e) => setZoom(Number(e.target.value))}
            style={{ width: 90 }}
          />
        </label>

        <label style={miniLabel}>
          最低保留度
          <input
            type="range"
            min={0}
            max={1000}
            value={Math.round(filters.minRetention * 100)}
            onChange={(e) => setFilters({ minRetention: Number(e.target.value) / 100 })}
          />
        </label>

        <label style={miniLabel}>
          <input
            type="checkbox"
            checked={filters.onlyDecaying}
            onChange={(e) => setFilters({ onlyDecaying: e.target.checked })}
          />
          只看快忘的
        </label>

        <select
          value={topicFilterId ?? ''}
          onChange={(e) => setTopicFilter(e.target.value || null)}
          style={inputStyle}
        >
          <option value="">全部主题</option>
          {topics.map((t) => (
            <option key={t.id} value={t.id}>
              {t.summary}
            </option>
          ))}
        </select>

        <div style={{ marginLeft: 'auto', display: 'flex', gap: 6 }}>
          <button style={tabBtn(tab === 'split')} onClick={() => setTab('split')}>
            树+星海
          </button>
          <button style={tabBtn(tab === 'persona')} onClick={() => setTab('persona')}>
            人格视图
          </button>
        </div>
      </header>

      {/* 内分泌仪表盘：五激素实时进度条 + 创伤状态（每 15 秒刷新） */}
      {hormones.data && (
        <div style={hormoneBarStyle}>
          {HORMONE_META.map((h) => {
            const v = hormones.data!.hormones[h.k] ?? 0;
            return (
              <div key={h.k} style={{ display: 'flex', alignItems: 'center', gap: 6, flex: '0 0 150px' }}>
                <span style={{ fontSize: 11, color: h.color, width: 44 }}>{h.label}</span>
                <div style={{ flex: 1, height: 6, background: '#0d1c2b', borderRadius: 3 }}>
                  <div
                    style={{
                      width: `${Math.round(v * 100)}%`,
                      height: '100%',
                      background: h.color,
                      borderRadius: 3,
                    }}
                  />
                </div>
                <span style={{ fontSize: 10, color: '#8fb8d8', width: 24, textAlign: 'right' }}>
                  {Math.round(v * 100)}
                </span>
              </div>
            );
          })}
          <span
            style={{
              fontSize: 11,
              marginLeft: 8,
              color: hormones.data.traumaState === 'calm' ? '#3ddc84' : '#ffb84a',
            }}
          >
            {hormones.data.traumaState === 'calm'
              ? '● 平稳'
              : hormones.data.traumaState === 'anxious'
                ? '● 焦虑'
                : '● 戒备'}
          </span>
          <button
            style={hormoneBtn}
            title="测试用：DA +20"
            onClick={async () => {
              await postHormoneInject('DA', 20);
              await hormones.refetch();
            }}
          >
            注射多巴胺
          </button>
          <button
            style={hormoneBtn}
            title="测试用：写强制入梦标记"
            onClick={async () => {
              await postForceDream();
            }}
          >
            强制入梦
          </button>
          <button
            style={hormoneBtn}
            title="清空未竟念头池"
            onClick={async () => {
              await postClearThoughts();
            }}
          >
            清理念头池
          </button>
        </div>
      )}

      <div style={bodyStyle}>
        <div style={canvasWrap}>
          {tree.isError && <ErrorBox text="树数据加载失败（星海不受影响）" />}
          {tree.isLoading && <Overlay text="加载树…" />}
          {tab === 'split' && (
            <div style={{ display: 'flex', gap: 6, height: '100%', minHeight: 0 }}>
              <div style={{ flex: '0 0 46%', minWidth: 0, height: '100%' }}>
                {treeNodes.length > 0 && <MemoTreeView nodes={treeNodes} edges={treeEdges} />}
              </div>
              <div style={{ flex: 1, minWidth: 0, height: '100%' }}>
                <StarfieldView
                  nodes={star.data?.data?.nodes ?? []}
                  edges={star.data?.data?.edges ?? []}
                  computing={star.data?.status === 202}
                  halo={halo}
                />
              </div>
            </div>
          )}
          {tab === 'persona' && (
            <MemoPersonaView
              scope={scope}
              halo={halo}
              dist={personaDist}
              dragMode={dragMode}
              onScale={onPersonaScale}
            />
          )}
        </div>
        <aside style={asideStyle}>
          <MemoDetailPanel />
        </aside>
      </div>

      <TimeBar tab={tab} starNodes={starNodes}>
        {tab === 'persona' && (
          <>
            <label style={miniLabel} title="短期记忆核心离金色球的距离">
              短距 {shortR.toFixed(2)}
              <input type="range" min={0.02} max={1.5} step={0.01} value={shortR}
                onChange={(e) => setShortR(Number(e.target.value))} />
            </label>
            <label style={miniLabel} title="特质/准则/防御行星的距离">
              行星 {planetR.toFixed(2)}
              <input type="range" min={0.02} max={1.0} step={0.005} value={planetR}
                onChange={(e) => setPlanetR(Number(e.target.value))} />
            </label>
            <label style={miniLabel} title="沉淀星尘环的距离">
              星尘 {dustR.toFixed(2)}
              <input type="range" min={0.05} max={2.0} step={0.01} value={dustR}
                onChange={(e) => setDustR(Number(e.target.value))} />
            </label>
          </>
        )}
      </TimeBar>
    </div>
  );
}

/**
 * 时间轴：自己订阅 timePct / 播放状态，页面本身不再跟着每像素重渲染。
 * 只放跟「时间」有关的控件；人格视图那三条距离滑块由父组件当 children 塞进来。
 */
const TimeBar = memo(function TimeBar({
  tab,
  starNodes,
  children,
}: {
  tab: Tab;
  starNodes: Array<{ t?: number }>;
  children?: React.ReactNode;
}) {
  const timePct = useVizStore((st) => st.timePct);
  const setTimePct = useVizStore((st) => st.setTimePct);
  const setSpecificAt = useVizStore((st) => st.setSpecificAt);
  const colorMode = useVizStore((st) => st.colorMode);
  const setColorMode = useVizStore((st) => st.setColorMode);
  const layerFilter = useVizStore((st) => st.layerFilter);
  const setLayerFilter = useVizStore((st) => st.setLayerFilter);

  const PLAY_SECONDS = 40;   // 12s 太快，5 天数据糊成一片
  const [playing, setPlaying] = useState(false);
  const playRef = useRef<number | null>(null);

  useEffect(() => {
    if (!playing) {
      if (playRef.current) cancelAnimationFrame(playRef.current);
      playRef.current = null;
      return;
    }
    const from = timePct >= 99.5 ? 0 : timePct;
    const t0 = performance.now();
    const tick = () => {
      const el = (performance.now() - t0) / 1000;
      const pct = Math.min(100, from + (100 - from) * (el / PLAY_SECONDS));
      setTimePct(pct);
      if (pct >= 100) { setPlaying(false); return; }
      playRef.current = requestAnimationFrame(tick);
    };
    playRef.current = requestAnimationFrame(tick);
    return () => {
      if (playRef.current) cancelAnimationFrame(playRef.current);
      playRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playing]);

  // 时间是分位映射：指针 37% = 第 37% 条记忆发生的时刻
  const timeCutoffMs = useMemo(() => {
    if (timePct >= 99.5) return 0;
    const ts = starNodes
      .map((n) => n.t)
      .filter((x): x is number => typeof x === 'number' && x > 0)
      .sort((a, b) => a - b);
    if (!ts.length) return 0;
    const idx = Math.max(0, Math.min(ts.length - 1, Math.floor((ts.length - 1) * (timePct / 100))));
    return ts[idx];
  }, [starNodes, timePct]);

  return (
    <div style={timelineStyle}>
      <span style={{ fontSize: 12, color: '#8fb8d8', width: 200, fontVariantNumeric: 'tabular-nums' }}>
        {timeCutoffMs ? fmtTime(timeCutoffMs) : '现在'}
      </span>
      <input
        type="range"
        min={0}
        max={10000}
        step={1}
        value={Math.round(timePct * 100)}
        onChange={(e) => { setSpecificAt(null); setTimePct(Number(e.target.value) / 100); }}
        style={{ flex: 1 }}
      />
      <select
        value=""
        onChange={(e) => {
          const v = e.target.value;
          if (!v) return;
          if (v === 'now') {
            setSpecificAt(null);
            setTimePct(100);
          } else {
            const [m, d] = v.split('-').map(Number);
            const ts = new Date(2026, m - 1, d, 23, 59, 59).getTime();
            setSpecificAt(ts);
            setTimePct(99);   // 滑块回中，避免和日期选择打架
          }
        }}
        title="按天跳转（直接定位到那天 23:59:59）"
        style={{
          background: '#0b1a2a', color: '#9fd0ff', border: '1px solid #24496b',
          borderRadius: 6, fontSize: 12, padding: '4px 8px', cursor: 'pointer',
        }}
      >
        <option value="">按天</option>
        <option value="now">回到现在</option>
        {[
          '10-20', '10-21', '10-22', '10-23', '10-24', '10-25',
          '10-26', '10-27', '10-28', '10-29', '10-30', '10-31', '11-01',
        ].map((d) => (
          <option key={d} value={d}>{d} 23:59</option>
        ))}
      </select>
      <button
        style={tabBtn(playing)}
        title={playing ? '暂停' : `按 ${PLAY_SECONDS} 秒从最早播到现在`}
        onClick={() => setPlaying((v) => !v)}
      >
        {playing ? '暂停' : '播放'}
      </button>
      <button style={tabBtn(timePct >= 99.5)} onClick={() => setTimePct(100)}>
        回到现在
      </button>
      <button
        style={tabBtn(colorMode === 'category')}
        title="分类五色：人格金/人物蓝/长期紫/经历绿/短期灰白"
        onClick={() => setColorMode(colorMode === 'category' ? 'emotion' : 'category')}
      >
        分类色
      </button>
      {tab === 'persona' && children}
      <select
        value={layerFilter}
        onChange={(e) => setLayerFilter(e.target.value as typeof layerFilter)}
        style={{ ...inputStyle, width: 104 }}
        title="星海过滤层"
      >
        <option value="all">全部节点</option>
        <option value="trauma">只看创伤</option>
        <option value="high">高保留(&gt;0.7)</option>
        <option value="thought">只看念头</option>
      </select>
    </div>
  );
});

function Overlay({ text }: { text: string }) {
  return <div style={{ padding: 20, color: '#8fb8d8' }}>{text}</div>;
}
function ErrorBox({ text }: { text: string }) {
  return <div style={{ padding: 12, color: '#ff9c9c' }}>{text}</div>;
}

const pageStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  height: '100vh',
  background: 'radial-gradient(ellipse at 50% 30%, #071322 0%, #040a14 45%, #01040a 100%)',
  color: '#cfe9ff',
  fontFamily: 'system-ui, "Segoe UI", sans-serif',
};
const headerStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 10,
  padding: '10px 14px',
  borderBottom: '1px solid rgba(70,130,180,.25)',
  flexWrap: 'wrap',
};
const bodyStyle: React.CSSProperties = { flex: 1, display: 'flex', minHeight: 0 };
const timelineStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 12,
  padding: '8px 14px',
  borderTop: '1px solid rgba(70,130,180,.25)',
  background: 'rgba(6,14,24,.5)',
};
const canvasWrap: React.CSSProperties = { flex: 1, minWidth: 0, position: 'relative' };
const asideStyle: React.CSSProperties = { width: 340, borderLeft: '1px solid rgba(70,130,180,.25)', minHeight: 0 };
const inputStyle: React.CSSProperties = {
  padding: '6px 8px',
  fontSize: 12,
  color: '#cfe9ff',
  background: 'rgba(10,22,36,.85)',
  border: '1px solid rgba(90,160,220,.35)',
  borderRadius: 8,
};
const miniLabel: React.CSSProperties = { fontSize: 12, color: '#8fb8d8', display: 'flex', alignItems: 'center', gap: 6 };
const tabBtn = (active: boolean): React.CSSProperties => ({
  padding: '6px 12px',
  fontSize: 12,
  borderRadius: 8,
  cursor: 'pointer',
  color: active ? '#04121f' : '#bfe9ff',
  background: active ? '#7fd4ff' : 'rgba(80,150,210,.12)',
  border: '1px solid rgba(90,160,220,.4)',
});

// 内分泌仪表盘：五激素（多巴胺/皮质醇/血清素/催产素/褪黑素）
const HORMONE_META = [
  { k: 'DA', label: '多巴胺', color: '#ffd24a' },
  { k: 'CORT', label: '皮质醇', color: '#ff6b6b' },
  { k: 'SER', label: '血清素', color: '#5aa8ff' },
  { k: 'OXY', label: '催产素', color: '#ff7eb6' },
  { k: 'MEL', label: '褪黑素', color: '#a78bfa' },
] as const;

const hormoneBarStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 12,
  padding: '6px 12px',
  borderBottom: '1px solid rgba(80,130,170,.18)',
  background: 'rgba(6,16,28,.55)',
};

const hormoneBtn: React.CSSProperties = {
  fontSize: 11,
  padding: '2px 8px',
  color: '#9fd8ff',
  background: 'rgba(80,150,210,.12)',
  border: '1px solid rgba(90,160,220,.35)',
  borderRadius: 6,
  cursor: 'pointer',
  marginLeft: 6,
};

// 创伤应激波纹：从中心向外扩散的暗红光环
if (typeof document !== 'undefined' && !document.getElementById('trauma-shock-kf')) {
  const st = document.createElement('style');
  st.id = 'trauma-shock-kf';
  st.textContent = '@keyframes traumaShock{0%{opacity:0;transform:scale(.2)}25%{opacity:1}100%{opacity:0;transform:scale(1.15)}}';
  document.head.appendChild(st);
}

if (typeof document !== 'undefined' && !document.getElementById('dream-swirl-kf')) {
  const st = document.createElement('style');
  st.id = 'dream-swirl-kf';
  st.textContent = '@keyframes dreamSwirl{from{transform:rotate(0deg) scale(1)}50%{transform:rotate(180deg) scale(1.12)}to{transform:rotate(360deg) scale(1)}}';
  document.head.appendChild(st);
}
