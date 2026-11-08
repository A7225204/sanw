import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\dashboard-web\src\features\memory-viz\DetailPanel\MemoryDetailPanel.tsx")
s = p.read_text(encoding="utf-8")

# 1) 引入 repair mutation + 轮询
s = s.replace("import { deleteNode, fetchDetail, postMerge, postRetention } from '../../../api/memoryViz';",
              "import { deleteNode, fetchDetail, postMerge, postRepair, postRetention } from '../../../api/memoryViz';")

s = s.replace('''  const detail = useQuery({
    queryKey: ['memory-detail', selectedNodeId],
    queryFn: () => fetchDetail(selectedNodeId as string),
    enabled: !!selectedNodeId,
  });''',
'''  const detail = useQuery({
    queryKey: ['memory-detail', selectedNodeId],
    queryFn: () => fetchDetail(selectedNodeId as string),
    enabled: !!selectedNodeId,
    // 快速修复期间每 5 秒刷新一次，进度条会动
    refetchInterval: (q) => {
      const tr = (q.state.data as { trauma?: { repairing?: boolean } } | undefined)?.trauma;
      return tr?.repairing ? 5000 : false;
    },
  });''')

# 2) repair mutation
s = s.replace('''  const remove = useMutation({''',
'''  const repair = useMutation({
    mutationFn: ({ mode }: { mode: 'instant' | 'quick' }) =>
      postRepair(selectedNodeId as string, mode, 10),
    onSuccess: invalidate,
  });

  const remove = useMutation({''')

# 3) 创伤区块换成带修复按钮
old_block_start = s.find("      {(d.node as unknown as { trauma?: { charge: number; triggers: string[]; since: string } }).trauma && (")
old_block_end = s.find("      <Section title=\"保留度构成\">")
if old_block_start > 0 and old_block_end > old_block_start:
    new_block = '''      {d.trauma && (
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
                if (confirm('快速修复会花 10 分钟慢慢淡掉电荷，确定？')) repair.mutate({ mode: 'quick' });
              }}
            >
              ⏳ 快速修复（10 分钟）
            </button>
            <button
              style={{ ...btnStyle, borderColor: '#2a6a4a', color: '#a8ffd0' }}
              onClick={() => {
                if (confirm('一键修复会立刻把电荷清零，确定？')) repair.mutate({ mode: 'instant' });
              }}
            >
              ⚡ 一键修复
            </button>
          </div>
          {repair.isError && (
            <div style={{ color: '#ff8a8a', fontSize: 12, marginTop: 6 }}>修复失败，看后端日志</div>
          )}
        </Section>
      )}

'''
    s = s[:old_block_start] + new_block + s[old_block_end:]

p.write_text(s, encoding="utf-8")
print("详情面板：创伤修复按钮（快速/一键）")
