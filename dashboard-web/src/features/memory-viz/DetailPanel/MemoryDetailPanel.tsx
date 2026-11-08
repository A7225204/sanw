import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  deleteNode, fetchDetail, fetchSoulHistory, postCharacterExtract, postGrowthDismiss,
  postGrowthSolidify, postMerge, postRepair, postRetention, postSoulAction,
  fetchMemoryHistory, postMemoryRollback, type RevRow,
} from '../../../api/memoryViz';
import { selectNode } from '../store/linkage';
import { isPositiveEmotion } from '../StarfieldView/starfieldRenderer';
import { useVizStore } from '../store/vizStore';
import type { MemoryDetailResponse, MemoryNode } from '../types';

/** 节点层级：人格中心 / 分类（池、短期记忆锚点）/ 词条（特质、成长条目）/ 单条记忆 */
type NodeLevel = 'root' | 'category' | 'term' | 'memory';

function levelOf(id: string, kind?: string): NodeLevel {
  if (id === 'persona:me' || kind === 'persona') return 'root';
  // 2026-11-07：念头/梦境也是「池里的条目」，不是记忆。
  // 原来没认这两个前缀 → 落到 'memory' → 点一条念头去查记忆详情，
  // 查不到就显示别的东西（主人说「我点了后为啥是别的记忆小球」）。
  if (id.startsWith('soul:') || id.startsWith('growth:')) return 'term';
  if (id.startsWith('thought:') || id.startsWith('dream:')) return 'term';
  if (kind === 'category' || kind === 'topic'
      || id.startsWith('pool:') || id.startsWith('core:') || id.startsWith('pseudo:')) {
    return 'category';
  }
  return 'memory';
}

export default function MemoryDetailPanel() {
  const qc = useQueryClient();
  // 逐字段订阅：整仓订阅会让详情面板跟着时间滑块每像素重渲染一次
  const scope = useVizStore((st) => st.scope);
  const selectedNodeId = useVizStore((st) => st.selectedNodeId);
  const setSelected = useVizStore((st) => st.setSelected);

  const detail = useQuery({
    queryKey: ['memory-detail', selectedNodeId],
    queryFn: () => fetchDetail(selectedNodeId as string),
    enabled: !!selectedNodeId,
    // 失败立即显示原因，不要重试 3 次（重试期间会一直卡在「加载详情...」）
    retry: false,
    // 快速修复期间每 5 秒刷新一次，进度条会动
    refetchInterval: (q) => {
      const tr = (q.state.data as { trauma?: { repairing?: boolean } } | undefined)?.trauma;
      return tr?.repairing ? 5000 : false;
    },
  });

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['memory-tree', scope] });
    qc.invalidateQueries({ queryKey: ['memory-starfield', scope] });
    qc.invalidateQueries({ queryKey: ['memory-detail', selectedNodeId] });
  };

  const retention = useMutation({
    mutationFn: (v: number) => postRetention(selectedNodeId as string, v),
    onSuccess: invalidate,
  });
  const merge = useMutation({
    mutationFn: (targetId: string) => postMerge(selectedNodeId as string, targetId),
    onSuccess: () => {
      setSelected(null);
      invalidate();
    },
  });
  const repair = useMutation({
    mutationFn: ({ mode }: { mode: 'instant' | 'quick' }) =>
      postRepair(selectedNodeId as string, mode, 10),
    onSuccess: invalidate,
  });
  const remove = useMutation({
    mutationFn: () => deleteNode(selectedNodeId as string),
    onSuccess: () => {
      setSelected(null);
      invalidate();
    },
  });
  const growSolid = useMutation({
    mutationFn: (id: string) => postGrowthSolidify(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['memory-detail', selectedNodeId] });
      qc.invalidateQueries({ queryKey: ['memory-persona'] });
    },
  });
  const growDismiss = useMutation({
    mutationFn: (id: string) => postGrowthDismiss(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['memory-detail', selectedNodeId] });
      qc.invalidateQueries({ queryKey: ['memory-persona'] });
    },
  });
  const soulAct = useMutation({
    mutationFn: (v: { kind: 'trait' | 'rule' | 'defense'; index: number; action: 'solidify' | 'dismiss' }) =>
      postSoulAction(v.kind, v.index, v.action),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['memory-detail'] });
      qc.invalidateQueries({ queryKey: ['memory-persona'] });
    },
  });
  const cardExtract = useMutation({
    mutationFn: (uid: string) => postCharacterExtract(uid),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['memory-detail', selectedNodeId] });
      qc.invalidateQueries({ queryKey: ['memory-persona'] });
    },
  });

  // 选中目标节点，并让星海把镜头推过去（focusRequest.from='tree' 会被星海消费）
  const jumpTo = (id: string) => {
    useVizStore.getState().setSelected(id);
    useVizStore.getState().requestFocus(id, 'tree');
  };

  if (!selectedNodeId) {
    return <div style={panelStyle}>未选中节点。点树上的点或星海里的星。</div>;
  }
  if (detail.isLoading) return <div style={panelStyle}>加载详情…</div>;
  if (detail.isError) {
    const msg = (detail.error as Error | null)?.message || '未知错误';
    return (
      <div style={panelStyle}>
        <div style={{ color: '#ff9a9a', fontSize: 13 }}>详情加载失败</div>
        <div style={{ color: '#8fb8d8', fontSize: 11, marginTop: 6 }}>节点：{selectedNodeId}</div>
        <div style={{ color: '#6f93ae', fontSize: 11, marginTop: 4, wordBreak: 'break-all' }}>{msg}</div>
        <button style={{ ...btnStyle, marginTop: 10 }} onClick={() => detail.refetch()}>重试</button>
      </div>
    );
  }

  const d = detail.data!;
  const level = levelOf(d.node.id, d.node.kind);

  if (level === 'root') return <PersonaPanel d={d} onJump={jumpTo} />;
  if (level === 'category') return <CategoryPanel d={d} onJump={jumpTo} />;
  // 2026-11-07：念头有自己的一套字段（why/priority/poked/src/scope），
  // 塞进 MemoryPanel 只会显示成「一条查不到的记忆」。
  if (String(d.node.id).startsWith('thought:')) return <ThoughtPanel d={d} />;

  return (
    <MemoryPanel
      d={d}
      node={d.node}
      onJump={jumpTo}
      soulAct={soulAct.mutate}
      growSolid={growSolid.mutate}
      growDismiss={growDismiss.mutate}
      growPending={growSolid.isPending || growDismiss.isPending}
      cardExtract={cardExtract}
      retention={retention.mutate}
      merge={merge.mutate}
      remove={remove.mutate}
      repair={repair.mutate}
      relatedIds={d.relatedNodeIds}
      opError={retention.isError || merge.isError || remove.isError}
    />
  );
}

/* ───────────────────────── 人格中心（宏观根节点） ───────────────────────── */

function PersonaPanel({ d, onJump }: { d: MemoryDetailResponse; onJump: (id: string) => void }) {
  const [showHistory, setShowHistory] = useState(false);
  const part = d.triadPart;
  const s = d.soul;
  if (!s) return <div style={panelStyle}>人格数据为空。</div>;

  const idItems = s.idItems ?? [];
  const egoItems = s.egoItems ?? [];
  const superItems = s.superegoItems ?? [];
  const defense: {
    text: string; why: string; at: string; index: number;
    traumaNodeId?: string; traumaSubject?: string;
    charge?: number; triggers?: string[];
  }[] = s.defenseItems ?? s.defense.map((t, i) => ({ text: t, why: '', at: '', index: i }));
  const growth = s.growth ?? [];
  const pools = s.pools ?? { traits: [], rules: [] };

  return (
    <div style={panelStyle}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
        <i style={{ width: 9, height: 9, borderRadius: 5, background: part ? '#ff9a3c' : '#ffd77a', boxShadow: `0 0 8px ${part ? '#ff9a3c' : '#ffd77a'}`, display: 'inline-block' }} />
        <span style={{ fontSize: 14, color: '#ffe9b0', fontWeight: 600 }}>{part?.label || '人格中心'}</span>
        <span style={{ marginLeft: 'auto', fontSize: 11, color: '#6f93ae' }}>
          {part ? (part.state || part.want || '') : `基调 ${s.moodKind || '—'}`}
        </span>
      </div>
      <div style={{ fontSize: 12, color: '#8fb8d8', marginBottom: 10 }}>
        蓝色大肥鱼 · 在册 {s.total} · 成长 {growth.length} · 候选 {s.poolTotal ?? 0}
      </div>

      {Array.from(
        idItems.reduce((m, it) => {
          const k = it.need || '其它';
          m.set(k, [...(m.get(k) || []), it]);
          return m;
        }, new Map<string, typeof idItems>()),
      ).map(([need, items]) => (
        <TagGroup key={`id-${need}`} title={`本我 · ${need}`} color="#ffd77a" items={items} />
      ))}
      <TagGroup title="自我 · 现实做法" color="#ffe08a" items={egoItems} />
      <TagGroup title="超我 · 底线（不能碰）" color="#e8b53a"
        total={superItems.length}
        items={superItems.filter((x) => x.sub !== 'ideal')} />
      <TagGroup title="超我 · 向往（想成为）" color="#9ad4ff"
        total={superItems.length}
        items={superItems.filter((x) => x.sub === 'ideal')} />
      <EgoBars fns={s.egoFunctions} />
      <ForcesBar forces={s.forces} />
      {typeof s.superegoSeverity === 'number' && (
        <div style={{ marginTop: 6, fontSize: 11, color: '#8fb8d8' }}>
          超我严厉度
          <b style={{
            marginLeft: 6,
            color: s.superegoSeverity >= 0.72 ? '#ff8a6b' : s.superegoSeverity <= 0.3 ? '#8ad6a0' : '#ffc46b',
          }}>{s.superegoSeverity.toFixed(2)}</b>
          <span style={{ marginLeft: 8, color: '#5f7d93' }}>
            {s.superegoSeverity >= 0.72 ? '过严：容易自责、先道歉' : s.superegoSeverity <= 0.3 ? '松动：可能破例' : '正常'}
          </span>
        </div>
      )}

      <ConflictHistory stats={s.conflictStats} />

      {defense.length > 0 && (
        <div style={{
          marginTop: 10, padding: '8px 10px', borderRadius: 8,
          background: 'rgba(176,48,48,.12)', border: '1px solid rgba(220,70,70,.45)',
        }}>
          <div style={{ color: '#ff9a9a', fontSize: 11, marginBottom: 4 }}>
            ⚠ 防御机制 {defense.length}（点标签跳到对应创伤）
          </div>
          {defense.map((it) => (
            <div key={it.index} style={{ marginBottom: 6 }}>
              <div style={{ color: '#ffc9c9', fontSize: 12 }}>{it.text}</div>
              {it.why ? (
                <div style={{ color: '#d8a0a0', fontSize: 10, lineHeight: 1.5 }}>{it.why}</div>
              ) : null}
              {!!it.triggers?.length && (
                <div style={{ color: '#e0b0b0', fontSize: 10, marginTop: 2 }}>
                  触发词：{it.triggers.join(' · ')}
                </div>
              )}
              {it.traumaNodeId && (
                <button
                  style={{ ...chipStyle, marginTop: 4, borderColor: 'rgba(220,70,70,.6)', color: '#ffd0d0' }}
                  onClick={() => onJump(it.traumaNodeId as string)}
                  title="跳到这条创伤记忆"
                >
                  → 关联创伤 · 群友 {it.traumaSubject || ''}（电荷 {Math.round((it.charge || 0) * 100)}%）
                </button>
              )}
            </div>
          ))}
        </div>
      )}

      <Accordion title={`人格成长 · ${growth.length} 条`}>
        {growth.length === 0 ? (
          <div style={{ color: '#5f7e96', fontSize: 11 }}>还没有提炼出成长条目。</div>
        ) : growth.map((g) => (
          <div
            key={g.id}
            title={g.value}
            onClick={() => selectNode({ id: g.id, summary: `${g.trait}：${g.value}` } as never, 'star')}
            style={{ display: 'flex', gap: 6, alignItems: 'baseline', fontSize: 11, lineHeight: 1.7, cursor: 'pointer' }}
          >
            <span style={{ color: g.status === 'solidified' ? '#3ddc84' : '#ffd24a' }}>
              {g.status === 'solidified' ? '●' : '○'}
            </span>
            <span style={{ flex: 1, minWidth: 0, color: '#ffe9b0', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
              <b>{g.trait}</b> · {g.value}
            </span>
          </div>
        ))}
      </Accordion>

      <Accordion title={`候选池 · ${s.poolTotal ?? 0} 条（未上位）`}>
        {(['traits', 'rules'] as const).map((k) => {
          const arr = pools[k] ?? [];
          const label = k === 'traits' ? '特质' : '准则';
          const color = k === 'traits' ? '#ffd77a' : '#5aa8ff';
          if (!arr.length) return null;
          return (
            <div key={k} style={{ marginBottom: 4 }}>
              <div style={{ color: '#5f7e96', fontSize: 10, marginBottom: 2 }}>{label} {arr.length}</div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 3 }}>
                {arr.map((x) => (
                  <span key={x} style={{ ...tagStyle(color), opacity: 0.55, fontSize: 10 }}>{x}</span>
                ))}
              </div>
            </div>
          );
        })}
      </Accordion>

      <Section title="操作">
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          <button style={btnStyle} onClick={() => setShowHistory((v) => !v)}>
            {showHistory ? '收起演化史' : '查看该人格演化史'}
          </button>
          <a
            href="/api/memory/soul/export"
            download="persona_export.json"
            style={{ ...btnStyle, textDecoration: 'none', display: 'inline-block' }}
          >
            导出人格档案
          </a>
        </div>
      </Section>

      {showHistory && <SoulHistoryBox />}

      <div style={{ color: '#6f93ae', fontSize: 10, marginTop: 10 }}>
        最近聚合：{s.lastRun || '—'}
        {s.updatedAt ? ` · 更新于 ${s.updatedAt}` : ''}
      </div>
    </div>
  );
}

function SoulHistoryBox() {
  const q = useQuery({ queryKey: ['soul-history'], queryFn: fetchSoulHistory, staleTime: 30_000 });
  if (q.isLoading) return <div style={{ ...hintBox, color: '#8fb8d8' }}>读取演化史…</div>;
  if (q.isError) return <div style={{ ...hintBox, color: '#ff9a9a' }}>演化史读取失败</div>;
  const h = q.data!;
  return (
    <div style={{ ...hintBox, lineHeight: 1.7 }}>
      <div style={{ color: '#9fd8ff', fontSize: 11, marginBottom: 4 }}>
        演化史 · 成长 {h.growth.length} 条 · 候选进场 {h.pools.length} 条
      </div>
      {h.growth.length === 0 && h.pools.length === 0 && (
        <div style={{ color: '#5f7e96', fontSize: 11 }}>还没有历史记录。</div>
      )}
      {h.growth.map((g) => (
        <div key={g.id} style={{ fontSize: 11, color: '#ffe9b0' }}>
          <span style={{ color: '#5f7e96' }}>{g.at || '—'}</span>{' '}
          <span style={{ color: g.status === 'solidified' ? '#3ddc84' : g.status === 'dismissed' ? '#6b7280' : '#ffd24a' }}>
            {g.status === 'solidified' ? '固化' : g.status === 'dismissed' ? '删除' : '待确认'}
          </span>{' '}
          <b>{g.trait}</b> · {g.value}
        </div>
      ))}
      {h.pools.length > 0 && (
        <div style={{ marginTop: 4 }}>
          <div style={{ color: '#8fb8d8', fontSize: 10 }}>候选池进场（按首次出现）</div>
          {h.pools.map((p) => (
            <div key={`${p.bucket}:${p.text}`} style={{ fontSize: 10, color: '#7f9fb8' }}>
              <span style={{ color: '#5f7e96' }}>{p.firstSeen || p.at || '—'}</span>{' '}
              [{p.label}] {p.text} · 命中 {p.hits}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/* ───────────────────────── 分类节点（池 / 锚点） ───────────────────────── */

function CategoryPanel({ d, onJump }: { d: MemoryDetailResponse; onJump: (id: string) => void }) {
  const n = d.node;
  const rows = d.related ?? [];
  const count = d.pool?.count ?? rows.length;
  return (
    <div style={panelStyle}>
      <div style={{ fontSize: 13, color: '#7fd4ff', marginBottom: 6 }}>分类 · {n.summary}</div>
      {d.note ? (
        <div style={{ ...hintBox, color: '#9fc0d6', lineHeight: 1.7, marginBottom: 10 }}>{d.note}</div>
      ) : (
        <div style={{ fontSize: 12, color: '#8fb8d8', marginBottom: 10 }}>
          共 {count} 条{d.pool ? ` · ${poolKindLabel(d.pool.kind)}` : ''} · 点任意一条看详情
        </div>
      )}
      {rows.length === 0 && !d.note ? (
        <div style={{ color: '#5f7e96', fontSize: 12 }}>这个池子现在是空的。</div>
      ) : rows.map((r) => (
        <div
          key={r.id}
          onClick={() => onJump(r.id)}
          style={{ ...rowStyle, cursor: 'pointer' }}
        >
          <span style={{ color: '#cfe9ff' }}>{r.summary || r.id}</span>
          <span style={{ color: '#4d6a80', fontSize: 10, marginLeft: 8 }}>{r.id}</span>
        </div>
      ))}
    </div>
  );
}

/* ───────────── 念头（未竟念头池的一条）─────────────
   2026-11-07：主人点「念头池」里的一条，原来跳到别的记忆小球
   （levelOf 不认 thought: 前缀 → 落到 memory 分支）。
   这里给它自己的面板，**视觉沿用 MemoryPanel 那套内联样式**
   （panelStyle / hintBox / rowStyle / chipStyle）——
   第一版用了 Tailwind 类名，但这个项目没装 Tailwind，渲染出来是一堆裸文字，很丑。 */
function ThoughtPanel({ d }: { d: MemoryDetailResponse }) {
  // thought 在**响应顶层**，不在 node 里（第一版写成 node.thought，字段全空）
  const node = d.node;
  const th = (d as unknown as { thought?: Record<string, unknown> }).thought || {};
  const txt = String(th.text ?? node.summary ?? '').replace(/^💭\s*/, '');
  const why = String(th.why ?? '');
  const pr = Number(th.priority ?? 0);
  const poked = Number(th.poked ?? 0);
  const created = Number(th.created ?? 0);
  const src = String(th.src ?? '');
  const scope = String(th.scope ?? '');
  const when = created
    ? new Date(created * 1000).toLocaleString('zh-CN', { hour12: false })
    : '—';
  const WHY_LABEL: Record<string, string> = {
    承诺: '主人答应过的事', 想要: '一直想要', 好奇: '有点好奇',
    分享欲: '想讲给他听', 在意: '有点在意', 杂念: '一闪而过',
  };
  // 分量条的配色跟 MemoryPanel 的重要度条一致（蓝 → 青）
  const prPct = Math.max(0, Math.min(100, pr * 100));
  return (
    <div style={panelStyle}>
      <div style={{ fontSize: 13, color: '#7fd4ff', marginBottom: 8 }}>💭 未竟念头</div>

      <div style={{ ...hintBox, color: '#dcefff', fontSize: 13, lineHeight: 1.75 }}>
        {txt || '（这条没有正文）'}
      </div>

      <div style={{ ...rowStyle, display: 'flex', gap: 18, marginTop: 6 }}>
        <span><span style={{ color: '#6f93ad' }}>为什么想 </span>
          <span style={{ color: '#9fd0ee' }}>{WHY_LABEL[why] || why || '—'}</span></span>
        <span><span style={{ color: '#6f93ad' }}>提过 </span>
          <span style={{ color: '#9fd0ee' }}>{poked} 次</span></span>
      </div>
      <div style={rowStyle}>
        <span style={{ color: '#6f93ad', fontSize: 11 }}>记下时间 </span>
        <span style={{ color: '#9fd0ee', fontSize: 11 }}>{when}</span>
      </div>

      {/* 分量：用和记忆面板一样的细条，别用纯文字 */}
      <div style={{ marginTop: 10 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between',
                      fontSize: 11, color: '#6f93ad', marginBottom: 4 }}>
          <span>分量</span><span style={{ color: '#9fd0ee' }}>{pr.toFixed(2)}</span>
        </div>
        <div style={{ height: 5, borderRadius: 3, background: 'rgba(80,130,170,.18)' }}>
          <div style={{ height: '100%', width: `${prPct}%`, borderRadius: 3,
                        background: 'linear-gradient(90deg,#4aa3e0,#7fd4ff)',
                        transition: 'width .3s' }} />
        </div>
      </div>

      <div style={{ ...hintBox, marginTop: 12,
                    background: src ? 'rgba(240,180,80,.07)' : 'rgba(120,120,120,.07)',
                    borderColor: src ? 'rgba(240,180,80,.28)' : 'rgba(120,120,120,.22)' }}>
        <div style={{ fontSize: 11, color: src ? '#e8c37f' : '#8a9aa8', marginBottom: 5 }}>
          溯源 · 这句话是谁说的
        </div>
        {src ? (
          <>
            <div style={{ fontSize: 13, lineHeight: 1.7, color: '#dcefff' }}>「{src}」</div>
            <div style={{ fontSize: 10.5, color: '#6f93ad', marginTop: 6 }}>
              {scope ? `会话 ${scope}` : '会话未记录'}
            </div>
          </>
        ) : (
          <div style={{ fontSize: 11.5, color: '#8a9aa8', lineHeight: 1.7 }}>
            这条是加溯源字段之前记的，只留了 40 字摘要 —— 原文和会话都没存。
            <br />新的念头会带整句原文 + 会话号。
          </div>
        )}
      </div>
    </div>
  );
}

function poolKindLabel(kind?: string): string {
  if (kind === 'trauma') return '创伤记忆（点进去可看触发词、修复电荷）';
  if (kind === 'thoughts') return '未竟念头';
  if (kind === 'episodes') return '事件';
  if (kind === 'dreams') return '梦境';
  return '';
}

/* ───────────────────────── 词条 / 单条记忆 ───────────────────────── */

function MemoryPanel({
  d, node: n, onJump, soulAct, growSolid, growDismiss, growPending, cardExtract,
  retention, merge, remove, repair, relatedIds, opError,
}: {
  d: MemoryDetailResponse;
  node: MemoryNode;
  onJump: (id: string) => void;
  soulAct: (v: { kind: 'trait' | 'rule' | 'defense'; index: number; action: 'solidify' | 'dismiss' }) => void;
  growSolid: (id: string) => void;
  growDismiss: (id: string) => void;
  growPending: boolean;
  cardExtract: { mutate: (uid: string) => void; isPending: boolean; isError: boolean; error: unknown };
  retention: (v: number) => void;
  merge: (target: string) => void;
  remove: () => void;
  repair: (v: { mode: 'instant' | 'quick' }) => void;
  relatedIds: string[];
  opError: boolean;
}) {
  const isTerm = n.id.startsWith('soul:') || n.id.startsWith('growth:');
  const isCard = n.id.startsWith('cat:card:');
  // 只有「真实的单条聊天记忆」才有保留度/来源消息/可操作参数
  const hasMemoryOps = n.id.startsWith('cat:short:') || n.id.startsWith('mem:');

  return (
    <div style={panelStyle}>
      <div style={{ fontSize: 13, color: '#7fd4ff', marginBottom: 6 }}>
        {isTerm ? '词条' : isCard ? '人物' : n.kind === 'topic' ? '主题' : '记忆'}
      </div>

      {d.speaker && (
        <div style={{
          fontSize: 12, color: '#8fb8d8', marginBottom: 10, padding: '6px 8px',
          background: 'rgba(80,150,210,.10)', border: '1px solid rgba(90,160,220,.22)',
          borderRadius: 8, lineHeight: 1.6,
        }}>
          <div>
            {d.speaker.channel === 'group' ? '群聊' : d.speaker.channel === 'private' ? '私聊' : '来源'}：
            <b style={{ color: '#cfe9ff' }}>{d.speaker.scopeLabel}</b>
            {d.speaker.groupId ? <span style={{ color: '#5f7e96' }}>（{d.speaker.groupId}）</span> : null}
          </div>
          <div>
            发言人：<b style={{ color: '#cfe9ff' }}>{d.speaker.speaker || '（未记录）'}</b>
            {d.speaker.uid ? <span style={{ color: '#7fd4ff' }}> · QQ {d.speaker.uid}</span> : null}
          </div>
        </div>
      )}

      <div style={{ fontSize: isTerm ? 14 : 15, color: isTerm ? '#ffe9b0' : '#d6eeff', lineHeight: 1.5, marginBottom: 12 }}>
        {n.summary}
      </div>

      {d.dream && (
        <div style={{
          padding: '10px 12px', marginBottom: 12,
          background: 'rgba(140,110,220,.10)', border: '1px solid rgba(160,130,240,.25)',
          borderRadius: 10, lineHeight: 1.8,
        }}>
          <div style={{ fontSize: 12, color: '#c4a8ff', marginBottom: 6 }}>
            🌙 {d.dream.at} · {d.dream.kind || '梦'}
          </div>
          <div style={{ fontSize: 14, color: '#e2d6ff' }}>{d.dream.dream}</div>
          {!!d.dream.insights?.length && (
            <div style={{ marginTop: 10, borderTop: '1px dashed rgba(160,130,240,.3)', paddingTop: 8 }}>
              <div style={{ fontSize: 12, color: '#c4a8ff', marginBottom: 4 }}>梦的收获</div>
              {d.dream.insights.map((x, i) => (
                <div key={i} style={{ fontSize: 13, color: '#cfc2f5', lineHeight: 1.7 }}>· {x}</div>
              ))}
            </div>
          )}
        </div>
      )}

      {d.soulItem && (
        <div style={{ paddingTop: 4 }}>
          <div style={{ fontSize: 11, color: '#9fd8ff', marginBottom: 6 }}>
            {d.soulItem.label}（{d.soulItem.index + 1}/{d.soulItem.total}）
          </div>
          {d.soulItem.why ? (
            <div style={{ fontSize: 11, color: '#8fb8d8', marginBottom: 6 }}>依据：{d.soulItem.why}</div>
          ) : null}
          <div style={{ fontSize: 10, color: '#6f93ae', marginBottom: 8 }}>提炼于 {d.soulItem.at || '—'}</div>
          <div style={{ display: 'flex', gap: 6 }}>
            <button
              style={btnStyle}
              disabled={d.soulItem.solidified}
              onClick={() => soulAct({ kind: d.soulItem!.kind, index: d.soulItem!.index, action: 'solidify' })}
            >
              {d.soulItem.solidified ? '✅ 已固化' : '固化这条'}
            </button>
            <button
              style={{ ...btnStyle, color: '#ff9a9a' }}
              onClick={() => soulAct({ kind: d.soulItem!.kind, index: d.soulItem!.index, action: 'dismiss' })}
            >
              从人格球删除
            </button>
          </div>
        </div>
      )}

      {d.growth && (
        <div
          style={{
            padding: '8px 10px', borderRadius: 8, fontSize: 12, lineHeight: 1.8,
            background: 'rgba(255,210,74,.08)', border: '1px solid rgba(255,210,74,.3)',
            color: '#ffe9b0',
          }}
        >
          <div>方面：<b>{d.growth.trait}</b></div>
          <div>变化：{d.growth.value}</div>
          <div>
            状态：
            {d.growth.status === 'solidified' ? '✅ 已固化'
              : d.growth.status === 'dismissed' ? '⭕ 已删除'
              : '🟡 待确认'}
          </div>
          {!!d.growth.evidence?.length && (
            <div style={{ color: '#c9b98a' }}>依据：{d.growth.evidence.join(' / ')}</div>
          )}
          <div style={{ display: 'flex', gap: 8, marginTop: 8, flexWrap: 'wrap' }}>
            <button
              style={{ ...btnStyle, borderColor: '#2a6a4a', color: '#a8ffd0' }}
              disabled={d.growth.status === 'solidified' || growPending}
              onClick={() => growSolid(d.growth!.id)}
            >
              固化
            </button>
            <button
              style={{ ...btnStyle, borderColor: '#7a4a4a', color: '#ffb4b4' }}
              disabled={d.growth.status === 'dismissed' || growPending}
              onClick={() => growDismiss(d.growth!.id)}
            >
              删除
            </button>
          </div>
          <GrowthHistory id={d.growth.id} />
        </div>
      )}

      {hasMemoryOps && <MemoryHistory nodeId={n.id} />}

      {/* 人物卡（静态客观档案）与关系记忆（动态主观感受）分开显示 */}
      {isCard && (
        <Section title="人物档案">
          {d.card ? (
            <div style={{ fontSize: 12, lineHeight: 2, color: '#cfe9ff' }}>
              {d.card['称呼'] ? <div>称呼：<b>{d.card['称呼']}</b></div> : null}
              {d.card['性别'] ? <div>性别：{d.card['性别']}</div> : null}
              {d.card['职业'] ? <div>职业：{d.card['职业']}</div> : null}
              {d.card['作息'] ? <div>作息：{d.card['作息']}</div> : null}
              {!!d.card['兴趣']?.length && <div>兴趣：{d.card['兴趣'].join('、')}</div>}
              {!!d.card['语言习惯']?.length && <div>语言习惯：{d.card['语言习惯'].join('、')}</div>}
              {d.card['性格'] ? <div>性格：{d.card['性格']}</div> : null}
              {d.card.updatedAt ? (
                <div style={{ color: '#5f7e96', fontSize: 11, marginTop: 4 }}>
                  更新于 {d.card.updatedAt}（基于 {d.card.sourceCount ?? '?'} 条发言）
                </div>
              ) : null}
            </div>
          ) : (
            <div style={{ fontSize: 12, color: '#5f7e96', marginBottom: 8 }}>
              还没有档案。点下面按钮，从 TA 的发言里提炼一份。
            </div>
          )}
          <button
            style={{ ...btnStyle, borderColor: '#2a5a7a', color: '#9fd8ff', marginTop: 8 }}
            disabled={cardExtract.isPending || !n.uid}
            onClick={() => n.uid && cardExtract.mutate(String(n.uid))}
          >
            {cardExtract.isPending ? '提炼中…' : d.card ? '重新提炼档案' : '提炼人物档案'}
          </button>
        </Section>
      )}

      {isCard && d.relationMemory && (
        <Section title="关系记忆">
          <div style={{ fontSize: 12, lineHeight: 2, color: '#ffe0c0' }}>
            <div>
              好感：<b>{d.relationMemory['好感'] ?? '—'}</b>
              <span style={{ color: '#8fb8d8', marginLeft: 8 }}>
                {d.relationMemory['互动模式'] || ''}
              </span>
            </div>
            {!!d.relationMemory['恩怨']?.length && (
              <div style={{ marginTop: 4 }}>
                恩怨：
                <ul style={{ margin: '2px 0 0 16px', padding: 0 }}>
                  {d.relationMemory['恩怨'].map((x, i) => <li key={i} style={{ color: '#e8c8a8' }}>{x}</li>)}
                </ul>
              </div>
            )}
          </div>
        </Section>
      )}

      {n.kind === 'memory' && !isTerm && !d.soulItem && (
        <Section title="当时心情">
          <div
            style={{
              display: 'flex', alignItems: 'center', gap: 8, padding: '8px 10px',
              borderRadius: 8, background: 'rgba(80,150,210,.08)',
              border: '1px solid rgba(90,160,220,.25)',
            }}
          >
            <i
              style={{
                width: 10, height: 10, borderRadius: 5, display: 'inline-block',
                background: moodColor(n), boxShadow: `0 0 8px ${moodColor(n)}`,
              }}
            />
            <span style={{ color: moodColor(n), fontSize: 14 }}>{n.emotion || '中性'}</span>
            {n.trauma && <span style={{ color: '#b03030', fontSize: 11, marginLeft: 6 }}>· 创伤</span>}
            <span style={{ marginLeft: 'auto', color: '#7f9fb8', fontSize: 11 }}>
              情感值 {(typeof n.sentiment === 'number' ? n.sentiment : 0).toFixed(2)}
            </span>
          </div>
        </Section>
      )}

      {d.trauma && (
        <Section title="创伤记忆">
          <div style={{
            padding: '8px 10px', borderRadius: 8, fontSize: 12, lineHeight: 1.8,
            background: 'rgba(176,48,48,.12)', border: '1px solid rgba(220,70,70,.4)', color: '#ffc9c9',
          }}>
            电荷：{Math.round((d.trauma.charge || 0) * 100)}%
            <br />
            触发词：{(d.trauma.triggers || []).join('、') || '（无）'}
            <br />
            起于：{d.trauma.since || ''}
            {d.trauma.repairing && (
              <>
                <br />
                <span style={{ color: '#ffd9a0' }}>
                  修复中 {Math.floor(d.trauma.progress * 100)}%（还剩 {Math.ceil(d.trauma.remainingSec / 60)} 分钟）
                </span>
                <div style={{ height: 6, marginTop: 6, background: 'rgba(255,255,255,.12)', borderRadius: 3 }}>
                  <div style={{ width: `${Math.round(d.trauma.progress * 100)}%`, height: '100%', background: 'linear-gradient(90deg,#e0a040,#ffd9a0)', borderRadius: 3 }} />
                </div>
              </>
            )}
          </div>
          <div style={{ display: 'flex', gap: 8, marginTop: 8, flexWrap: 'wrap' }}>
            <button
              style={{ ...btnStyle, borderColor: '#7a5a2a', color: '#ffd9a0' }}
              onClick={() => {
                if (confirm('快速修复会花 10 分钟慢慢淡掉电荷，确定？')) repair({ mode: 'quick' });
              }}
            >
              ⏳ 快速修复（10 分钟）
            </button>
            <button
              style={{ ...btnStyle, borderColor: '#2a6a4a', color: '#a8ffd0' }}
              onClick={() => {
                if (confirm('一键修复会立刻把电荷清零，确定？')) repair({ mode: 'instant' });
              }}
            >
              ⚡ 一键修复
            </button>
          </div>
        </Section>
      )}

      {d.hormones && Object.keys(d.hormones).length > 0 && (
        <Section title="当时的激素">
          <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', fontSize: 11 }}>
            {([
              ['DA', '多巴胺', '#ffd24a'],
              ['CORT', '皮质醇', '#ff6b6b'],
              ['SER', '血清素', '#5aa8ff'],
              ['OXY', '催产素', '#ff7eb6'],
              ['MEL', '褪黑素', '#a78bfa'],
            ] as const).map(([k, label, color]) => {
              const v = Number(d.hormones?.[k] ?? 0);
              return <span key={k} style={{ color, minWidth: 72 }}>{label} {Math.round(v * 100)}%</span>;
            })}
          </div>
        </Section>
      )}

      {!!d.personElsewhere?.length && (
        <Section title="同一人也在（点击可跳过去）">
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
            {d.personElsewhere.map((p) => (
              <button
                key={p.id}
                title={`${p.scopeLabel}${p.viaQq ? ' · 按QQ匹配' : ' · 按昵称匹配'}`}
                style={{ ...chipStyle, borderColor: 'rgba(61,220,132,.5)', color: '#a8ffd0' }}
                onClick={() => onJump(p.id)}
              >
                <i style={{ width: 6, height: 6, borderRadius: 3, background: '#3ddc84', display: 'inline-block', marginRight: 4 }} />
                {p.scopeLabel.slice(0, 8)}·{p.summary.slice(0, 8)}
              </button>
            ))}
          </div>
        </Section>
      )}

      {!!(d.related ?? []).length && (
        <Section title="相关记忆">
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
            {(d.related ?? []).map((r) => (
              <button key={r.id} title={r.id} style={chipStyle} onClick={() => onJump(r.id)}>
                {r.summary ? r.summary.slice(0, 12) : r.id.split(':').pop()}
              </button>
            ))}
          </div>
        </Section>
      )}

      {hasMemoryOps && (
        <>
          <Section title="保留度构成">
            <Bar label="保留分数" value={n.retentionScore} />
            <Bar label="重要度" value={n.importance} />
            <Bar label="新鲜度" value={n.recency} />
            <Bar label="频次" value={n.frequency} />
          </Section>

          <Section title="来源消息">
            {d.sourceMessages.length === 0 ? (
              <div style={{ color: '#5f7e96', fontSize: 12 }}>（暂无关联的原始消息 id）</div>
            ) : d.sourceMessages.map((m) => (
              <div key={m.id} style={rowStyle}>
                <b>{m.senderId}</b> · {m.ts} · {m.text}
              </div>
            ))}
          </Section>

          <Section title="操作">
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
              <button style={btnStyle} onClick={() => retention(1)}>保留度拉满</button>
              <button style={btnStyle} onClick={() => retention(0.05)}>淡出到 5%</button>
              <button
                style={{ ...btnStyle, borderColor: '#7a4a4a', color: '#ffb4b4' }}
                onClick={() => {
                  const target = relatedIds[0];
                  if (target) merge(target);
                }}
              >
                合并到相关第一条
              </button>
              <button
                style={{ ...btnStyle, borderColor: '#7a3a3a', color: '#ff9c9c' }}
                onClick={() => {
                  if (confirm(`确定删除这条记忆？\n\n${n.summary.slice(0, 40)}`)) remove();
                }}
              >
                删除这条记忆
              </button>
            </div>
            {opError && (
              <div style={{ color: '#ff8a8a', fontSize: 12, marginTop: 8 }}>操作失败，看后端日志</div>
            )}
          </Section>
        </>
      )}

      {!hasMemoryOps && !isTerm && !isCard && (
        <div style={{ color: '#5f7e96', fontSize: 11, marginTop: 12 }}>
          这个节点没有单条记忆的可调参数。
        </div>
      )}
    </div>
  );
}

/* ───────────────────────── 共用小块 ───────────────────────── */

/** 三我势力条：本我欲望 / 自我控制 / 超我严厉（0~1） */
function ForcesBar({ forces }: { forces?: Record<string, number | string> }) {
  // 2026-11-07：forces() 里除了三个数字，现在还带一个 "证据" 字符串
  // （ego_evidence 的行为证据摘要）。直接 Object.entries 进来,
  // v.toFixed(2) 对字符串会抛 TypeError，整个面板白屏。
  // 只留数字项 —— 证据文字不走势力条，走别的展示位。
  const rows = Object.entries(forces || {}).filter(
    ([, v]) => typeof v === 'number' && Number.isFinite(v),
  ) as [string, number][];
  if (!rows.length) return null;
  const colors: Record<string, string> = { 本我: '#ff7b6b', 自我: '#8ad6ff', 超我: '#e8b53a' };
  return (
    <div style={{ marginTop: 10 }}>
      <div style={{ color: '#ffe9b0', fontSize: 11, marginBottom: 4 }}>三我势力</div>
      <div style={{ display: 'flex', gap: 6 }}>
        {rows.map(([k, v]) => {
          const pct = Math.max(4, Math.min(100, Math.round(v * 100)));
          const c = colors[k] || '#9ad4ff';
          return (
            <div key={k} style={{ flex: 1 }}>
              <div style={{ fontSize: 10, color: '#8fb8d8', marginBottom: 2 }}>{k} {v.toFixed(2)}</div>
              <span style={{ display: 'block', height: 6, borderRadius: 3, background: 'rgba(255,255,255,.08)' }}>
                <i style={{ display: 'block', width: `${pct}%`, height: '100%', borderRadius: 3, background: c, boxShadow: `0 0 6px ${c}` }} />
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

/** id/ego/superego → 本我/自我/超我（面板只显示中文） */
const TRIAD_LABEL: Record<string, string> = { id: '本我', ego: '自我', superego: '超我' };
function triadLabel(key: string): string {
  return String(key)
    .split('×')
    .map((x) => TRIAD_LABEL[x.trim()] || x.trim())
    .join(' × ');
}

/** 冲突 7 天趋势 + 类型计数 */
function ConflictHistory({ stats }: { stats?: { days?: { date: string; count: number }[]; kinds?: [string, number][]; total?: number } }) {
  const days = stats?.days || [];
  const kinds = stats?.kinds || [];
  if (!days.length && !kinds.length) return null;
  const max = Math.max(1, ...days.map((d) => d.count));
  return (
    <div style={{ marginTop: 10 }}>
      <div style={{ color: '#ffb08a', fontSize: 11, marginBottom: 4 }}>
        冲突近 7 天 · 共 {stats?.total ?? 0} 次
      </div>
      <div style={{ display: 'flex', alignItems: 'flex-end', gap: 4, height: 34 }}>
        {days.map((d) => (
          <span key={d.date} title={`${d.date}：${d.count} 次`}
            style={{ flex: 1, height: `${Math.max(2, (d.count / max) * 32)}px`,
              background: d.count ? 'rgba(255,150,90,.75)' : 'rgba(255,255,255,.10)',
              borderRadius: 2, display: 'block' }} />
        ))}
      </div>
      {kinds.length > 0 && (
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 4 }}>
          {kinds.map(([k, n]) => (
            <span key={k} style={{ fontSize: 10, color: '#ffc9a0' }}>{triadLabel(k)} ×{n}</span>
          ))}
        </div>
      )}
    </div>
  );
}

/** 自我四项功能：现实检验/冲动控制/延迟满足/情绪调节（0~1，越低越吃紧） */
function EgoBars({ fns }: { fns?: Record<string, number> }) {
  const rows = Object.entries(fns || {}).filter(
    ([, v]) => typeof v === 'number' && Number.isFinite(v),
  ) as [string, number][];
  if (!rows.length) return null;
  return (
    <div style={{ marginTop: 10 }}>
      <div style={{ color: '#ffe08a', fontSize: 11, marginBottom: 4 }}>自我功能 · 此刻</div>
      {rows.map(([k, v]) => {
        const pct = Math.max(3, Math.min(100, Math.round(v * 100)));
        const col = v < 0.45 ? '#ff8a6b' : v < 0.65 ? '#ffc46b' : '#8ad6a0';
        return (
          <div key={k} style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 3 }}>
            <span style={{ width: 58, fontSize: 11, color: '#8fb8d8' }}>{k}</span>
            <span style={{ flex: 1, height: 6, borderRadius: 3, background: 'rgba(255,255,255,.08)' }}>
              <i style={{ display: 'block', width: `${pct}%`, height: '100%', borderRadius: 3, background: col, boxShadow: `0 0 6px ${col}` }} />
            </span>
            <span style={{ width: 30, textAlign: 'right', fontSize: 11, color: col }}>{v.toFixed(2)}</span>
          </div>
        );
      })}
      <div style={{ fontSize: 10, color: '#5f7d93', marginTop: 2 }}>低于各自阈值（0.52~0.58）会写进她的 prompt（说话会留余地）</div>
    </div>
  );
}

function TagGroup({
  title, color, items, onPick, total,
}: {
  title: string;
  color: string;
  items: { text: string; why?: string; at?: string }[];
  onPick?: (index: number, text: string) => void;
  /** 同类合计（超我拆成「底线 / 向往」两组，标题上要能看出总共几条） */
  total?: number;
}) {
  if (!items.length) return null;
  const countLabel = total && total !== items.length
    ? `${items.length}/${total}`
    : String(items.length);
  return (
    <div style={{ marginTop: 8 }}>
      <div style={{ color, fontSize: 11, marginBottom: 3 }}>{title} {countLabel}</div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
        {items.map((x, i) => (
          <span
            key={`${x.text}-${i}`}
            style={{ ...tagStyle(color), cursor: onPick ? 'pointer' : 'default' }}
            title={x.why ? `${x.why}${x.at ? `\n提炼于 ${x.at}` : ''}` : '点它会选中对应的行星'}
            onClick={() => onPick?.(i, x.text)}
          >
            {x.text}
          </span>
        ))}
      </div>
    </div>
  );
}

function Accordion({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <details style={{ marginTop: 12, borderTop: '1px solid rgba(90,140,190,.2)', paddingTop: 8 }}>
      <summary style={{ cursor: 'pointer', color: '#cfe8ff', fontSize: 12 }}>
        ▸ {title}
      </summary>
      <div style={{ marginTop: 6 }}>{children}</div>
    </details>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ marginTop: 14 }}>
      <div style={{ color: '#6f93ae', fontSize: 12, marginBottom: 6 }}>{title}</div>
      {children}
    </div>
  );
}

function Bar({ label, value }: { label: string; value?: number }) {
  // 人格成长这类节点没有保留度构成，缺字段时显示「—」而不是 NaN
  const v = typeof value === 'number' && Number.isFinite(value)
    ? Math.max(0, Math.min(1, value))
    : null;
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
      <span style={{ width: 64, color: '#8fb8d8', fontSize: 12 }}>{label}</span>
      <div style={{ flex: 1, height: 6, background: '#0d1c2b', borderRadius: 3 }}>
        {v !== null && (
          <div
            style={{
              width: `${Math.round(v * 100)}%`,
              height: '100%',
              background: 'linear-gradient(90deg,#2f7fae,#7fd4ff)',
              borderRadius: 3,
            }}
          />
        )}
      </div>
      <span style={{ width: 34, textAlign: 'right', color: '#b9d8ef', fontSize: 11 }}>
        {v === null ? '—' : (v * 100).toFixed(0)}
      </span>
    </div>
  );
}

function moodColor(n: { trauma?: unknown; emotion?: string }): string {
  if (isPositiveEmotion(n.emotion)) return '#3ddc84';
  if (!n.emotion || n.emotion === '中性') return '#bfe0ff';
  return '#ffcc33';                         // 负面：黄（创伤另行标注）
}

const panelStyle: React.CSSProperties = {
  padding: 14,
  color: '#a9c6da',
  background: 'rgba(6,14,24,.78)',
  border: '1px solid rgba(70,130,180,.3)',
  borderRadius: 12,
  height: '100%',
  overflow: 'auto',
  fontSize: 13,
};
const hintBox: React.CSSProperties = {
  marginTop: 8,
  padding: '8px 10px',
  borderRadius: 8,
  background: 'rgba(80,150,210,.08)',
  border: '1px solid rgba(90,160,220,.25)',
  fontSize: 11,
};
const rowStyle: React.CSSProperties = {
  padding: '6px 0',
  borderBottom: '1px dashed rgba(80,130,170,.18)',
  lineHeight: 1.5,
};
const chipStyle: React.CSSProperties = {
  padding: '2px 8px',
  fontSize: 11,
  color: '#9fd0ee',
  background: 'rgba(80,150,210,.12)',
  border: '1px solid rgba(90,160,220,.35)',
  borderRadius: 8,
  cursor: 'pointer',
};
const btnStyle: React.CSSProperties = {
  padding: '6px 10px',
  fontSize: 12,
  color: '#cfe9ff',
  background: 'rgba(80,150,210,.12)',
  border: '1px solid rgba(90,160,220,.35)',
  borderRadius: 8,
  cursor: 'pointer',
};

function tagStyle(color: string): React.CSSProperties {
  return {
    fontSize: 11, padding: '2px 7px', borderRadius: 10,
    color, border: `1px solid ${color}55`, background: `${color}14`,
  };
}

/* ───────────────────────── 成长版本链 ───────────────────────── */

function GrowthHistory({ id }: { id: string }) {
  const [rows, setRows] = useState<RevRow[] | null>(null);
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);

  const load = () => {
    fetchMemoryHistory('growth', id)
      .then(setRows)
      .catch((e) => setErr(e instanceof Error ? e.message : '读取失败'));
  };
  useEffect(load, [id]);

  const rollback = async () => {
    setBusy(true);
    try {
      const r = await postMemoryRollback('growth', id);
      if (r.error) setErr(r.error);
      else { setErr(''); load(); }
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ marginTop: 8, borderTop: '1px dashed rgba(255,210,74,.3)', paddingTop: 6 }}>
      <div style={{ fontSize: 11, color: '#c9b98a', marginBottom: 4 }}>
        修订历史{rows ? `（${rows.length}）` : ''}
      </div>
      {err && <div style={{ fontSize: 11, color: '#ff9a9a' }}>{err}</div>}
      {rows === null && <div style={{ fontSize: 11, color: '#5f7e96' }}>读取中…</div>}
      {rows && rows.length === 0 && (
        <div style={{ fontSize: 11, color: '#5f7e96' }}>暂无修订（只有被改写过才会留版本）</div>
      )}
      {(rows ?? []).map((r) => (
        <div key={r.rev} style={{ fontSize: 11, color: '#d9c79a', lineHeight: 1.6 }}>
          <span style={{ color: '#6f93ae' }}>v{r.rev}</span>{' '}
          <span title={r.value || r.fact}>{(r.value || r.fact || '').slice(0, 40)}</span>{' '}
          <span style={{ color: '#5f7e96' }}>{r.ts || ''}</span>
        </div>
      ))}
      {rows && rows.length > 0 && (
        <button
          style={{ ...btnStyle, marginTop: 6, borderColor: '#5a6a8a', color: '#bcd4ff' }}
          disabled={busy}
          onClick={rollback}
        >
          回滚到上一版
        </button>
      )}
    </div>
  );
}

/* ───────────────────────── 记忆修订历史 ───────────────────────── */

function MemoryHistory({ nodeId }: { nodeId: string }) {
  const parts = nodeId.split(':');
  const numId = parts[parts.length - 1];
  const [rows, setRows] = useState<RevRow[] | null>(null);
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);

  const load = () => {
    if (!/^\d+$/.test(numId)) { setRows([]); return; }
    fetchMemoryHistory('memory', numId)
      .then(setRows)
      .catch((e) => setErr(e instanceof Error ? e.message : '读取失败'));
  };
  useEffect(load, [numId]);

  const rollback = async () => {
    setBusy(true);
    try {
      const r = await postMemoryRollback('memory', numId);
      if (r.error) setErr(r.error);
      else { setErr(''); load(); }
    } finally {
      setBusy(false);
    }
  };

  if (!/^\d+$/.test(numId)) return null;
  return (
    <div style={{ marginTop: 10, borderTop: '1px dashed rgba(120,170,220,.3)', paddingTop: 6 }}>
      <div style={{ fontSize: 11, color: '#9fc0d6', marginBottom: 4 }}>
        修订历史{rows ? `（${rows.length}）` : ''}
      </div>
      {err && <div style={{ fontSize: 11, color: '#ff9a9a' }}>{err}</div>}
      {rows === null && <div style={{ fontSize: 11, color: '#5f7e96' }}>读取中…</div>}
      {rows && rows.length === 0 && (
        <div style={{ fontSize: 11, color: '#5f7e96' }}>暂无修订（这条没被改写过）</div>
      )}
      {(rows ?? []).map((r) => (
        <div key={r.rev} style={{ fontSize: 11, color: '#cfe2f5', lineHeight: 1.6 }}>
          <span style={{ color: '#6f93ae' }}>v{r.rev}</span>{' '}
          <span title={r.fact}>{(r.fact || '').slice(0, 40)}</span>{' '}
          <span style={{ color: '#5f7e96' }}>{r.ts || ''}</span>
        </div>
      ))}
      {rows && rows.length > 0 && (
        <button
          style={{ ...btnStyle, marginTop: 6, borderColor: '#5a6a8a', color: '#bcd4ff' }}
          disabled={busy}
          onClick={rollback}
        >
          回滚到上一版
        </button>
      )}
    </div>
  );
}
