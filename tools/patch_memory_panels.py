import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\dashboard\index.html")
s = p.read_text(encoding="utf-8")
changed = []

# ---------- 1) CSS ----------
css = '''
  .mem-left{position:absolute;left:18px;top:70px;bottom:18px;width:216px;z-index:2;
    background:rgba(6,14,24,.72);border:1px solid rgba(70,130,180,.28);border-radius:12px;
    padding:12px 10px;overflow:auto;backdrop-filter:blur(4px)}
  .mem-left h4{margin:0 0 10px;font-size:12px;color:#6f93ae;font-weight:500;letter-spacing:1px}
  .mem-item{display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:8px;cursor:pointer;
    font-size:12px;color:#9fc0d6;transition:.15s}
  .mem-item:hover{background:rgba(90,160,220,.14)}
  .mem-item.active{background:rgba(90,170,255,.2);color:#dff1ff}
  .mem-item i{width:8px;height:8px;border-radius:50%;flex:0 0 auto}
  .mem-item span.c{margin-left:auto;color:#5d7d96;font-size:11px}
  .mem-right{position:absolute;right:18px;top:70px;bottom:18px;width:330px;z-index:2;display:none;
    background:rgba(6,14,24,.78);border:1px solid rgba(70,130,180,.3);border-radius:12px;
    padding:14px;overflow:auto;backdrop-filter:blur(4px);font-size:12px;color:#a9c6da}
  .mem-right.show{display:block}
  .mem-right h3{margin:0 0 4px;font-size:14px;color:#d6eeff;font-weight:600}
  .mem-right .sub{color:#5f7e96;font-size:11px;margin-bottom:12px}
  .mem-sec{margin:14px 0 6px;color:#7fd4ff;font-size:12px;letter-spacing:1px;
    border-left:2px solid #2e7fae;padding-left:8px}
  .mem-row{padding:7px 0;border-bottom:1px dashed rgba(80,130,170,.18);line-height:1.55}
  .mem-row b{color:#cfe9ff;font-weight:500}
  .mem-row .t{color:#54748c;font-size:11px;margin-left:6px}
  .mem-row .w{float:right;color:#f0b85a;font-size:11px}
  .mem-close2{position:absolute;right:10px;top:8px;background:none;border:none;color:#5f7e96;
    cursor:pointer;font-size:14px}
'''
if ".mem-left{" not in s:
    s = s.replace("</style>", css + "</style>", 1)
    changed.append("css")

# ---------- 2) DOM ----------
dom = '''  <div class="mem-left" id="memLeft"><h4>记忆分区</h4></div>
  <div class="mem-right" id="memRight"><button class="mem-close2" id="memDetailClose">×</button><div id="memBody"></div></div>
'''
marker = '<div class="mem-tip" id="memTip"></div>'
if "memLeft" not in s and marker in s:
    s = s.replace(marker, dom + marker, 1)
    changed.append("dom")

# ---------- 3) JS：左侧列表 + 右侧详情 + 高亮 ----------
js = '''
  const memLeft = document.getElementById('memLeft');
  const memRight = document.getElementById('memRight');
  const memBody = document.getElementById('memBody');
  const memDetailClose = document.getElementById('memDetailClose');
  let activeScope = null;
  let scopeNodes = new Map();

  function scopeName(g){
    return g.replace('group_','群 ').replace('private_','私聊 ').replace('global_learn','自学池');
  }

  function renderLeft(){
    if (!memLeft) return;
    const counts = {};
    nodes.forEach(n => { if (n.kind==='fact') counts[n.group]=(counts[n.group]||0)+1; });
    scopeNodes = new Map();
    nodes.forEach(n => { if (n.kind==='scope') scopeNodes.set(n.group, n); });
    const rows = Object.entries(counts).sort((a,b)=>b[1]-a[1]).map(([g,c])=>{
      const col = colorOf(g).base;
      const act = activeScope === g ? ' active' : '';
      return `<div class="mem-item${act}" data-scope="${g}"><i style="background:${col}"></i>${scopeName(g)}<span class="c">${c}</span></div>`;
    }).join('');
    memLeft.innerHTML = '<h4>记忆分区</h4>' + rows;
    memLeft.querySelectorAll('.mem-item').forEach(el => {
      el.addEventListener('click', () => {
        const g = el.dataset.scope;
        activeScope = (activeScope === g) ? null : g;
        renderLeft();
        const sn = scopeNodes.get(g);
        if (sn && activeScope) { focusTarget = {x:sn.x, y:sn.y, z:sn.z}; flying = {dist: 520}; }
        else { focusTarget = {x:0,y:0,z:0}; flying = {dist: 1350}; }
        if (activeScope) loadDetail(g);
        else memRight.classList.remove('show');
      });
    });
  }

  async function loadDetail(scope){
    if (!memRight) return;
    memRight.classList.add('show');
    memBody.innerHTML = '<h3>' + scopeName(scope) + '</h3><div class="sub">加载中…</div>';
    try {
      const r = await fetch('/api/memory_detail?scope=' + encodeURIComponent(scope), {cache:'no-store'});
      const d = await r.json();
      const facts = (d.facts||[]).map(f=>`<div class="mem-row"><span class="w">w=${(f.w||0).toFixed(1)}</span><b>${esc(f.fact)}</b><span class="t">${esc(f.time||'')} · ${esc(f.subject||'')}</span></div>`).join('') || '<div class="mem-row">（暂无长期记忆）</div>';
      const anchors = (d.anchors||[]).map(a=>`<div class="mem-row"><b>${esc(a.subject||'锚点')}</b>：${esc(a.fact)}<span class="t">${esc(a.time||'')}</span></div>`).join('') || '<div class="mem-row">（暂无锚点）</div>';
      const recent = (d.recent||[]).slice(0,10).map(x=>`<div class="mem-row"><span class="t">${esc(x.ts||'')}</span><br>👤 ${esc(x.user||'')}<br>🐟 ${esc(x.bot||'')}</div>`).join('') || '<div class="mem-row">（暂无短期对话）</div>';
      memBody.innerHTML =
        `<h3>${scopeName(scope)}</h3><div class="sub">长期 ${d.facts_total||0} 条 · 短期 ${(d.recent||[]).length} 条 · 更新 ${esc(d.generated_at||'')}</div>` +
        `<div class="mem-sec">🧠 长期记忆</div>${facts}` +
        `<div class="mem-sec">⚓ 记忆锚点</div>${anchors}` +
        `<div class="mem-sec">💬 短期记忆（最近对话）</div>${recent}`;
    } catch(e){
      memBody.innerHTML = '<h3>读取失败</h3><div class="sub">' + esc(String(e)) + '</div>';
    }
  }

  function esc(t){
    return String(t==null?'':t).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  }
'''
anchor_js = "  function open(){"
if "renderLeft()" not in s and anchor_js in s:
    s = s.replace(anchor_js, js + "\n" + anchor_js, 1)
    changed.append("js")

# 打开时渲染左侧
s = s.replace("load().then(()=>{ running = true; last = 0; requestAnimationFrame(loop); });",
              "load().then(()=>{ renderLeft(); running = true; last = 0; requestAnimationFrame(loop); });", 1)

# 点击节点 → 也拉详情（保留原来的飞过去）
s = s.replace("""      if (best){
        focusTarget = {x:best.x, y:best.y, z:best.z};
        flying = {dist: best.kind === 'scope' ? 420 : 240};
        statsEl.textContent = (best.kind === 'subject' ? '主题：' : '') + best.label;
      }""",
"""      if (best){
        focusTarget = {x:best.x, y:best.y, z:best.z};
        flying = {dist: best.kind === 'scope' ? 420 : 240};
        statsEl.textContent = (best.kind === 'subject' ? '主题：' : '') + best.label;
        if (best.group){ activeScope = best.group; renderLeft(); loadDetail(best.group); }
      }""", 1)

# 详情关闭
s = s.replace("  btn.addEventListener('click', open);",
              "  if (memDetailClose) memDetailClose.addEventListener('click', () => { memRight.classList.remove('show'); activeScope = null; renderLeft(); });\n  btn.addEventListener('click', open);", 1)

# 高亮：非选中作用域变暗
s = s.replace("      const depth = Math.max(0.12, Math.min(1, 1 - pr.zc/2600)) * n.reveal;",
              "      const dim = (activeScope && n.group !== activeScope) ? 0.18 : 1;\n      const depth = Math.max(0.12, Math.min(1, 1 - pr.zc/2600)) * n.reveal * dim;", 1)
s = s.replace("      return {a, b, rv, z:(a.zc+b.zc)/2, scope:(na.kind==='scope'||nb.kind==='scope')};",
              "      const dm = (activeScope && na.group !== activeScope) ? 0.15 : 1;\n      return {a, b, rv: rv*dm, z:(a.zc+b.zc)/2, scope:(na.kind==='scope'||nb.kind==='scope')};", 1)

p.write_text(s, encoding="utf-8")
print("改动:", changed)
