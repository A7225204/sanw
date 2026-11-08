import type {
  MemoryDetailResponse,
  MemoryNode,
  MemoryStarfieldResponse,
  MemoryTreeResponse,
  ScopeSummary,
  StarfieldEdge,
} from '../features/memory-viz/types';

const BASE = '';

async function getJson<T>(url: string): Promise<{ status: number; data: T | null }> {
  const r = await fetch(BASE + url, { cache: 'no-store' });
  if (r.status === 202) return { status: 202, data: null };
  if (!r.ok) throw new Error(`${url} → HTTP ${r.status}`);
  return { status: r.status, data: (await r.json()) as T };
}

export async function fetchScopes(): Promise<ScopeSummary[]> {
  const { data } = await getJson<{ scopes: ScopeSummary[] }>('/api/memory/scopes');
  return data?.scopes ?? [];
}

export async function fetchTree(scope: string): Promise<MemoryTreeResponse> {
  const { data } = await getJson<MemoryTreeResponse>(
    `/api/memory/tree?scope=${encodeURIComponent(scope)}`,
  );
  if (!data) throw new Error('tree 数据为空');
  return data;
}

export async function fetchStarfield(
  scope: string,
): Promise<{ status: number; data: MemoryStarfieldResponse | null }> {
  return getJson<MemoryStarfieldResponse>(
    `/api/memory/starfield?scope=${encodeURIComponent(scope)}`,
  );
}

export async function fetchDetail(nodeId: string): Promise<MemoryDetailResponse> {
  const { data } = await getJson<MemoryDetailResponse>(
    `/api/memory/detail?nodeId=${encodeURIComponent(nodeId)}`,
  );
  if (!data) throw new Error('详情为空');
  return data;
}

export interface SoulHistory {
  updatedAt: string;
  lastRun: string;
  soul: { traits: string[]; rules: string[]; defense: string[] };
  growth: {
    id: string; trait: string; value: string; status: string;
    category: string; at: string; t: number;
  }[];
  pools: {
    bucket: string; label: string; text: string; at: string;
    firstSeen: string; hits: number; solidified: boolean;
  }[];
}

export async function fetchSoulHistory(): Promise<SoulHistory> {
  const { data } = await getJson<SoulHistory>('/api/memory/soul/history');
  if (!data) throw new Error('演化史为空');
  return data;
}

export interface PersonaCategory {
  key: string;
  label: string;
  count: number;
  status: string;
  anchor: { x: number; y: number; z: number };
}

export interface PersonaResponse {
  scope: string;
  center: MemoryNode;
  categories: PersonaCategory[];
  nodes: MemoryNode[];
  edges: StarfieldEdge[];
  generatedAt: string;
}

export async function fetchPersona(
  scope: string | null,
  dist?: { shortR?: number; planetR?: number; dustR?: number; atMs?: number },
): Promise<PersonaResponse> {
  const q = new URLSearchParams({ scope: scope || '' });
  if (dist?.shortR) q.set('shortR', String(dist.shortR));
  if (dist?.planetR) q.set('planetR', String(dist.planetR));
  if (dist?.dustR) q.set('dustR', String(dist.dustR));
  // 时间指针：拖动历史滑块时把它传下去，后端按那一刻算生命进度
  if (dist?.atMs) q.set('at', String(dist.atMs));
  const { data } = await getJson<PersonaResponse>(`/api/memory/persona?${q.toString()}`);
  if (!data) throw new Error('persona 数据为空');
  return data;
}

export async function postGrowthExtract(): Promise<{ added: number; items: unknown[] }> {
  const r = await fetch('/api/memory/growth/extract', { method: 'POST' });
  if (!r.ok) throw new Error(`提炼失败：${r.status}`);
  return (await r.json()) as { added: number; items: unknown[] };
}

export async function postGrowthSolidify(id: string): Promise<void> {
  const r = await fetch(`/api/memory/growth/${encodeURIComponent(id)}/solidify`, { method: 'POST' });
  if (!r.ok) throw new Error(`固化失败：${r.status}`);
}

export async function postCharacterExtract(uid: string): Promise<{ ok: boolean; error?: string }> {
  const r = await fetch(`/api/memory/character/extract?uid=${encodeURIComponent(uid)}`, { method: 'POST' });
  return (await r.json()) as { ok: boolean; error?: string };
}

export async function postGrowthDismiss(id: string): Promise<void> {
  const r = await fetch(`/api/memory/growth/${encodeURIComponent(id)}/dismiss`, { method: 'POST' });
  if (!r.ok) throw new Error(`删除失败：${r.status}`);
}

export async function postRetention(nodeId: string, retentionScore: number): Promise<void> {
  const r = await fetch(
    `/api/memory/node/${encodeURIComponent(nodeId)}/retention`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ retentionScore }),
    },
  );
  if (!r.ok) throw new Error(`retention 失败：${r.status}`);
}

export async function postMerge(nodeId: string, targetId: string): Promise<void> {
  const r = await fetch(`/api/memory/node/${encodeURIComponent(nodeId)}/merge`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ targetId }),
  });
  if (!r.ok) throw new Error(`merge 失败：${r.status}`);
}

export async function postRepair(nodeId: string, mode: 'instant' | 'quick', minutes = 10): Promise<void> {
  const r = await fetch(`/api/memory/node/${encodeURIComponent(nodeId)}/repair`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode, minutes }),
  });
  if (!r.ok) throw new Error(`修复失败：${r.status}`);
}

export async function deleteNode(nodeId: string): Promise<void> {
  const r = await fetch(`/api/memory/node/${encodeURIComponent(nodeId)}`, { method: 'DELETE' });
  if (!r.ok) throw new Error(`删除失败：${r.status}`);
}

export interface HormonesResponse {
  hormones: Record<string, number>;
  raw: Record<string, number>;
  traumaState: string;
  emotions: Record<string, number>;
  updatedAt: string;
}

export async function fetchHormones(): Promise<HormonesResponse> {
  const { data } = await getJson<HormonesResponse>('/api/memory/hormones');
  if (!data) throw new Error('hormones 为空');
  return data;
}

export async function postHormoneInject(name: string, amount = 20): Promise<{ ok: boolean; error?: string }> {
  const r = await fetch(`/api/memory/hormones/inject?name=${encodeURIComponent(name)}&amount=${amount}`, { method: 'POST' });
  return (await r.json()) as { ok: boolean; error?: string };
}

export async function postClearThoughts(): Promise<{ ok: boolean; cleared?: number }> {
  const r = await fetch('/api/memory/thoughts/clear', { method: 'POST' });
  return (await r.json()) as { ok: boolean; cleared?: number };
}

export async function postForceDream(): Promise<{ ok: boolean; note?: string }> {
  const r = await fetch('/api/memory/dream/force', { method: 'POST' });
  return (await r.json()) as { ok: boolean; note?: string };
}

export interface EntityTreeResponse {
  scope: string;
  nodes: MemoryNode[];
  edges: StarfieldEdge[];
  generatedAt: string;
}

export async function fetchEntityTree(scope: string | null): Promise<EntityTreeResponse> {
  const { data } = await getJson<EntityTreeResponse>(
    `/api/memory/entity-tree?scope=${encodeURIComponent(scope || '')}`,
  );
  if (!data) throw new Error('entity tree 为空');
  return data;
}

export async function fetchMemoryPool(): Promise<PersonaResponse> {
  const { data } = await getJson<PersonaResponse>('/api/memory/pool');
  if (!data) throw new Error('pool 为空');
  return data;
}

export async function postSoulAction(
  kind: 'trait' | 'rule' | 'defense',
  index: number,
  action: 'solidify' | 'dismiss',
): Promise<{ ok: boolean; message?: string; error?: string }> {
  const r = await fetch(`/api/memory/soul/${kind}/${index}/${action}`, { method: 'POST' });
  return (await r.json()) as { ok: boolean; message?: string; error?: string };
}

export interface RevRow {
  rev: number;
  value?: string;
  fact?: string;
  trait?: string;
  ts?: string;
}

export async function fetchMemoryHistory(
  kind: 'growth' | 'memory',
  id: string,
): Promise<RevRow[]> {
  const r = await fetch(
    `/api/memory/history?kind=${kind}&id=${encodeURIComponent(id)}`,
  );
  if (!r.ok) throw new Error(`历史读取失败：${r.status}`);
  const d = (await r.json()) as { rows?: RevRow[] };
  return d.rows ?? [];
}

export async function postMemoryRollback(
  kind: 'growth' | 'memory',
  id: string,
): Promise<{ ok?: boolean; error?: string }> {
  const r = await fetch(
    `/api/memory/rollback?kind=${kind}&id=${encodeURIComponent(id)}`,
    { method: 'POST' },
  );
  return (await r.json()) as { ok?: boolean; error?: string };
}
