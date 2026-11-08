import { useEffect, useRef, useState } from 'react';
import { createMemoryFlowLayer, type FlowParticle, type PersonaPlanet } from './memoryFlowLayer';
import { useVizStore } from '../store/vizStore';
import { selectNode } from '../store/linkage';

/** 记忆流视图：全新的干净实现，与旧 StarfieldView 完全独立 */
export default function MemoryFlowView({ hiddenCats }: { hiddenCats?: Record<string, boolean> } = {}) {
  const boxRef = useRef<HTMLDivElement>(null);
  const layerRef = useRef<ReturnType<typeof createMemoryFlowLayer> | null>(null);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [tones, setTones] = useState({ pos: 0, neg: 0, neu: 0, trauma: 0, episode: 0 });
  const [nowText, setNowText] = useState('');
  const [rangeTick, setRangeTick] = useState(0);   // 拿到时间范围后强制重跑一次
  const [pollTick, setPollTick] = useState(0);     // 「现在」视图的自动刷新节拍
  const timePct = useVizStore((s) => s.timePct);
  const specificAt = useVizStore((s) => s.specificAt);
  const lastReqRef = useRef(0);
  const rangeRef = useRef<{ oldest: number; newest: number } | null>(null);

  // 建层
  useEffect(() => {
    if (!boxRef.current) return;
    const layer = createMemoryFlowLayer({
      container: boxRef.current,
      onPickNode: (id) => {
        // 右侧详情面板统一读 selectedNodeId，这里只负责选中的节点
        useVizStore.getState().setSelected(id);
        selectNode({ id } as never, 'star');
      },
    });
    layerRef.current = layer;
    layer.start();
    return () => {
      layer.dispose();
      layerRef.current = null;
    };
  }, []);

  // 关系圈层勾选：关掉「人格成长」就不再画成长球
  useEffect(() => {
    const hidden = Object.keys(hiddenCats || {}).filter((k) => hiddenCats?.[k]);
    layerRef.current?.setHiddenKinds(hidden);
  }, [hiddenCats]);

  // 只在「看现在」时轮询：新记忆、新建的行星会自己出现；翻历史时不打扰
  useEffect(() => {
    if (specificAt || timePct < 99.5) return;
    const id = window.setInterval(() => {
      if (document.visibilityState === 'visible') setPollTick((v) => v + 1);
    }, 10_000);
    return () => window.clearInterval(id);
  }, [timePct, specificAt]);

  // 数据：跟着时间指针（防抖 200ms）刷新
  useEffect(() => {
    const run = async () => {
      try {
        // 时间指针：100% = 现在；否则按最近 5 天线性回溯
        // 时间指针：按后端给的真实记忆区间插值（100% = 现在，0% = 最早一条）
        let at = 0;
        const rg = rangeRef.current;
        if (specificAt) {
          // 选了具体日期：直接用绝对时间戳，不受百分比映射影响
          at = specificAt;
        } else if (timePct < 99.5) {
          if (rg) {
            at = rg.oldest + (rg.newest - rg.oldest) * (timePct / 100);
          } else {
            // 还不知道范围：先拉一次「现在」，只为拿到 oldest/newest 建立坐标系，
            // 这一帧不画粒子
            const r0 = await fetch('/api/memory/flow');
            const d0 = await r0.json();
            if (d0.oldest && d0.newest) {
              rangeRef.current = { oldest: Number(d0.oldest), newest: Number(d0.newest) };
              setRangeTick((v) => v + 1);   // 立刻重跑一次，这次就能算出真实的 at
            }
            layerRef.current?.update([]);
            layerRef.current?.setPlanets([]);
            return;
          }
        }
        // ?stress=1 打开压力测试数据（500 条记忆 / 50 事件 / 20 个人格），只在 URL 带参数时生效
        const stress = /[?&]stress=1/.test(window.location.search);
        const qs = new URLSearchParams();
        if (at) qs.set('at', String(Math.round(at)));
        if (stress) qs.set('stress', '1');
        const url = `/api/memory/flow${qs.toString() ? `?${qs.toString()}` : ''}`;
        const r = await fetch(url);
        const d = await r.json();
        layerRef.current?.update((d.particles ?? []) as FlowParticle[]);
        layerRef.current?.updateThoughts((d.thoughts ?? []) as unknown[]);
        {
          const ps = (d.particles ?? []) as FlowParticle[];
          const t = { pos: 0, neg: 0, neu: 0, trauma: 0, episode: 0 };
          for (const p of ps) {
            if (p.tone === 'pos') t.pos++;
            else if (p.tone === 'neg') t.neg++;
            else if (p.tone === 'neu') t.neu++;
            if (p.dest === 'trauma') t.trauma++;
            if (p.dest === 'episode') t.episode++;
          }
          setTones(t);
        }
        layerRef.current?.setPlanets((d.planets ?? []) as PersonaPlanet[]);
        if (d.oldest && d.newest) {
          rangeRef.current = { oldest: Number(d.oldest), newest: Number(d.newest) };
        }
        // 把「当前时间指针」告诉渲染层，行星才知道该不该出现
        (window as unknown as { __flowAt?: number }).__flowAt = Number(d.now) || Date.now();
        setCounts(d.counts ?? {});
        const t = new Date(d.now as number);
        const p2 = (x: number) => String(x).padStart(2, '0');
        setNowText(`${t.getFullYear()}-${p2(t.getMonth() + 1)}-${p2(t.getDate())} ${p2(t.getHours())}:${p2(t.getMinutes())}:${p2(t.getSeconds())}`);
      } catch {
        /* 后端不可达时保持上一帧 */
      }
    };
    // 真节流：同步判断「距上次请求是否够久」，够就直接发；
    // 不用 setTimeout —— effect 的清理函数会把它取消掉，等于防抖。
    const begin = () => {
      lastReqRef.current = performance.now();
      void run();
    };
    if (performance.now() - lastReqRef.current >= 220) {
      begin();
      return () => {};
    }
    // 不够久：安排一次尾部补发（只补一次，不重置）
    const timer = setTimeout(() => {
      begin();
    }, 400 - (performance.now() - lastReqRef.current));
    return () => { clearTimeout(timer); };
  }, [timePct, rangeTick, specificAt, pollTick]);

  return (
    <div style={{ position: 'relative', width: '100%', height: '100%' }}>
      <div ref={boxRef} style={{ position: 'absolute', inset: 0 }} />
      <div style={{
        position: 'absolute', left: 18, top: 18, zIndex: 6,
        background: 'rgba(6,18,32,.72)', border: '1px solid rgba(80,130,180,.28)',
        borderRadius: 10, padding: '10px 12px', minWidth: 128,
        pointerEvents: 'none', backdropFilter: 'blur(4px)',
      }}>
        <div style={{ color: '#6f93ae', fontSize: 12, marginBottom: 6 }}>星系图例</div>
        {[
          ['#ffd77a', '本我（中心）', ''],
          ['#4ab8ff', '自我', ''],
          ['#fff6d8', '超我', ''],
          ['#3ddc84', '正面情绪', tones.pos ? String(tones.pos) : ''],
          ['#ffcc33', '负面情绪', tones.neg ? String(tones.neg) : ''],
          ['#bfe0ff', '中性记忆', tones.neu ? String(tones.neu) : ''],
          ['#ff3b30', '创伤', tones.trauma ? String(tones.trauma) : ''],
          ['#7ac8ff', '长期记忆星尘', ''],
          ['#a8d8ff', '念头气泡', ''],
          ['#9a6aff', '梦境旋涡', ''],
        ].map(([color, name, n]) => (
          <div key={name} style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 3 }}>
            <i style={{ width: 8, height: 8, borderRadius: 4, background: color, display: 'inline-block', boxShadow: `0 0 6px ${color}88` }} />
            <span style={{ color: '#9fc0d6', fontSize: 11 }}>{name}</span>
            {n ? <span style={{ color: '#5d7d96', fontSize: 10, marginLeft: 'auto' }}>{n}</span> : null}
          </div>
        ))}
        <div style={{ marginTop: 6, paddingTop: 6, borderTop: '1px dashed rgba(80,130,170,.2)', fontSize: 10, color: '#4d6a80' }}>
          点星尘/气泡/旋涡可看记忆
        </div>
      </div>
      <div style={{
        position: 'absolute', left: 12, bottom: 12, fontSize: 11, lineHeight: 1.7,
        color: '#8fb8d8', background: 'rgba(6,14,24,.72)', padding: '8px 12px',
        borderRadius: 8, border: '1px solid rgba(120,180,240,.22)', pointerEvents: 'none',
      }}>
        <div style={{ color: '#cfe8ff', marginBottom: 4 }}>{nowText || '—'}</div>
        <div style={{ color: '#6f93ae', marginBottom: 4 }}>指针 {timePct.toFixed(1)}%
          {rangeRef.current ? ` (${new Date(rangeRef.current.oldest).toLocaleString('zh-CN')})` : ' (无范围)'}</div>
        <div>白洞喷出 <b style={{ color: '#fff' }}>{counts.born ?? 0}</b>　
             短期漫游 <b style={{ color: '#9fd8ff' }}>{counts.wander ?? 0}</b></div>
        <div>分流中 <b style={{ color: '#ffd77a' }}>{counts.flow ?? 0}</b>　
             已归位 <b style={{ color: '#6f93ae' }}>{counts.arrived ?? 0}</b></div>
      </div>
    </div>
  );
}
