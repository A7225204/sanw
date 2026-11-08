import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\dashboard\index.html")
s = p.read_text(encoding="utf-8")

if "memOverlay" in s:
    print("已经加过，跳过")
    raise SystemExit

# ---------- 1) 按钮：放进 header 的 brain 区 ----------
btn_anchor = '<div class="brain">'
btn = '''<button id="memBtn" class="mem-btn" title="查看记忆星系">🕸 记忆</button>
    <div class="brain">'''
if btn_anchor in s:
    s = s.replace(btn_anchor, btn, 1)
else:
    print("header 锚点没找到")

# ---------- 2) CSS ----------
css = '''
  /* ===== 记忆星系 ===== */
  .mem-btn{margin-left:auto;margin-right:14px;padding:8px 16px;border-radius:10px;cursor:pointer;
    background:rgba(80,190,255,.12);border:1px solid rgba(120,210,255,.45);color:#bfe9ff;
    font-size:13px;letter-spacing:1px;transition:.2s}
  .mem-btn:hover{background:rgba(80,190,255,.25);box-shadow:0 0 16px rgba(90,200,255,.35)}
  .mem-overlay{position:fixed;inset:0;background:rgba(2,6,14,.92);backdrop-filter:blur(3px);
    display:none;z-index:9999}
  .mem-overlay.show{display:block}
  .mem-head{position:absolute;top:18px;left:24px;right:24px;display:flex;align-items:center;gap:16px;z-index:2}
  .mem-title{color:#cfeaff;font-size:15px;letter-spacing:2px}
  .mem-stats{color:#6e8ba6;font-size:12px}
  .mem-close{margin-left:auto;background:none;border:1px solid rgba(120,210,255,.4);color:#bfe9ff;
    border-radius:8px;padding:6px 14px;cursor:pointer;font-size:13px}
  .mem-close:hover{background:rgba(120,210,255,.15)}
  #memCanvas{position:absolute;inset:0;width:100%;height:100%;display:block;cursor:grab}
  #memCanvas.dragging{cursor:grabbing}
  .mem-legend{position:absolute;left:24px;bottom:20px;color:#7e9db6;font-size:12px;line-height:1.9;z-index:2}
  .mem-legend i{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
  .mem-tip{position:absolute;pointer-events:none;padding:6px 10px;border-radius:8px;font-size:12px;
    background:rgba(8,18,30,.94);border:1px solid rgba(120,210,255,.35);color:#d6efff;max-width:320px;
    display:none;z-index:3;line-height:1.5}
'''
s = s.replace("</style>", css + "</style>", 1)

# ---------- 3) DOM + JS ----------
overlay = '''
<div id="memOverlay" class="mem-overlay">
  <canvas id="memCanvas"></canvas>
  <div class="mem-head">
    <div class="mem-title">🕸 记忆星系</div>
    <div class="mem-stats" id="memStats">加载中…</div>
    <button class="mem-close" id="memClose">关闭</button>
  </div>
  <div class="mem-legend" id="memLegend"></div>
  <div class="mem-tip" id="memTip"></div>
</div>
<script>
(function(){
  const overlay = document.getElementById('memOverlay');
  const btn = document.getElementById('memBtn');
  const closeBtn = document.getElementById('memClose');
  const canvas = document.getElementById('memCanvas');
  const tip = document.getElementById('memTip');
  const statsEl = document.getElementById('memStats');
  const legendEl = document.getElementById('memLegend');
  if (!overlay || !btn) return;
  const ctx = canvas.getContext('2d');
  let nodes = [], links = [], byId = new Map();
  let scale = 1, ox = 0, oy = 0, running = false, alpha = 1;
  let drag = null, hover = null;

  function colorOf(group){
    let h = 0;
    for (let i=0;i<group.length;i++) h = (h*31 + group.charCodeAt(i)) % 360;
    return `hsl(${h}, 78%, 62%)`;
  }

  async function load(){
    statsEl.textContent = '加载中…';
    const r = await fetch('/api/memory_graph', {cache:'no-store'});
    const data = await r.json();
    if (!data.nodes) { statsEl.textContent = '读取失败'; return; }
    nodes = data.nodes.map(n => ({...n, x:(Math.random()-.5)*600, y:(Math.random()-.5)*600, vx:0, vy:0, color: colorOf(n.group||'x')}));
    byId = new Map(nodes.map(n => [n.id, n]));
    links = data.links.filter(l => byId.has(l.source) && byId.has(l.target));
    statsEl.textContent = `${data.stats.scopes} 个作用域 · ${data.stats.subjects} 个主题 · ${data.stats.facts} 条事实`;
    // 图例：按节点数排前 6 个作用域
    const counts = {};
    nodes.forEach(n => { if (n.kind==='fact') counts[n.group]=(counts[n.group]||0)+1; });
    const top = Object.entries(counts).sort((a,b)=>b[1]-a[1]).slice(0,6);
    legendEl.innerHTML = top.map(([g,c])=>`<div><i style="background:${colorOf(g)}"></i>${g.replace('group_','群 ').replace('private_','私聊 ')}（${c}）</div>`).join('');
  }

  function resize(){
    const dpr = window.devicePixelRatio || 1;
    canvas.width = innerWidth * dpr;
    canvas.height = innerHeight * dpr;
    ctx.setTransform(dpr,0,0,dpr,0,0);
  }

  function tick(){
    alpha *= 0.995;
    const cx = innerWidth/2, cy = innerHeight/2;
    // 斥力（用平方近似，节点数少够用）
    for (let i=0;i<nodes.length;i++){
      const a = nodes[i];
      for (let j=i+1;j<nodes.length;j++){
        const b = nodes[j];
        let dx = a.x-b.x, dy = a.y-b.y;
        let d2 = dx*dx+dy*dy+0.01;
        if (d2 > 90000) continue;
        const f = 320 / d2;
        dx*=f; dy*=f;
        a.vx += dx; a.vy += dy;
        b.vx -= dx; b.vy -= dy;
      }
    }
    // 弹簧
    for (const l of links){
      const a = byId.get(l.source), b = byId.get(l.target);
      if (!a || !b) continue;
      const dx = b.x-a.x, dy = b.y-a.y;
      const d = Math.sqrt(dx*dx+dy*dy)+0.01;
      const target = a.kind==='scope'||b.kind==='scope' ? 150 : 46;
      const f = (d-target) * 0.0016;
      a.vx += dx/d*f*10; a.vy += dy/d*f*10;
      b.vx -= dx/d*f*10; b.vy -= dy/d*f*10;
    }
    // 向心 + 阻尼
    for (const n of nodes){
      n.vx += (cx-n.x)*0.0007; n.vy += (cy-n.y)*0.0007;
      n.vx *= 0.86; n.vy *= 0.86;
      n.x += n.vx; n.y += n.vy;
    }
  }

  function draw(){
    ctx.clearRect(0,0,innerWidth,innerHeight);
    ctx.save();
    ctx.translate(ox, oy);
    ctx.scale(scale, scale);
    // 连线
    ctx.lineWidth = 1/Math.max(1,scale*0.9);
    for (const l of links){
      const a = byId.get(l.source), b = byId.get(l.target);
      if (!a || !b) continue;
      const isScope = a.kind==='scope'||b.kind==='scope';
      ctx.strokeStyle = isScope ? 'rgba(90,170,220,.28)' : 'rgba(70,120,170,.16)';
      ctx.beginPath(); ctx.moveTo(a.x,a.y); ctx.lineTo(b.x,b.y); ctx.stroke();
    }
    // 节点
    for (const n of nodes){
      const r = n.size * (n.kind==='scope'?1.5:1);
      const g = ctx.createRadialGradient(n.x,n.y,0,n.x,n.y,r*3.2);
      g.addColorStop(0, n.color);
      g.addColorStop(1, 'rgba(0,0,0,0)');
      ctx.fillStyle = g;
      ctx.beginPath(); ctx.arc(n.x,n.y,r*3.2,0,Math.PI*2); ctx.fill();
      ctx.fillStyle = n.color;
      ctx.beginPath(); ctx.arc(n.x,n.y,r*0.42,0,Math.PI*2); ctx.fill();
      if (n.kind==='scope'){
        ctx.fillStyle = 'rgba(210,240,255,.85)';
        ctx.font = '12px system-ui';
        ctx.fillText(n.label, n.x + 10, n.y + 4);
      }
    }
    ctx.restore();
    if (alpha > 0.02 && running) requestAnimationFrame(frame);
  }

  function frame(){ tick(); draw(); }

  function open(){
    overlay.classList.add('show');
    resize();
    ox = 0; oy = 0; scale = 1; alpha = 1;
    load().then(()=>{ running = true; requestAnimationFrame(frame); });
  }
  function close(){
    running = false;
    overlay.classList.remove('show');
    tip.style.display = 'none';
  }

  btn.addEventListener('click', open);
  closeBtn.addEventListener('click', close);
  window.addEventListener('keydown', e => { if (e.key === 'Escape' && overlay.classList.contains('show')) close(); });
  window.addEventListener('resize', () => { if (overlay.classList.contains('show')) resize(); });

  // 拖拽 / 缩放 / 悬停
  canvas.addEventListener('mousedown', e => { drag = {x:e.clientX-ox, y:e.clientY-oy}; canvas.classList.add('dragging'); });
  canvas.addEventListener('mouseup', () => { drag = null; canvas.classList.remove('dragging'); });
  canvas.addEventListener('mouseleave', () => { drag = null; tip.style.display='none'; });
  canvas.addEventListener('mousemove', e => {
    if (drag){ ox = e.clientX-drag.x; oy = e.clientY-drag.y; alpha = 0.35; if(!running){running=true; requestAnimationFrame(frame);} return; }
    // 最近节点
    const wx = (e.clientX-ox)/scale, wy = (e.clientY-oy)/scale;
    let best = null, bestD = 18/scale+8;
    for (const n of nodes){
      const d = Math.hypot(n.x-wx, n.y-wy);
      if (d < bestD){ bestD = d; best = n; }
    }
    if (best && best.kind !== 'scope'){
      tip.style.display = 'block';
      tip.style.left = (e.clientX+14)+'px';
      tip.style.top = (e.clientY+12)+'px';
      tip.textContent = best.kind === 'subject' ? ('主题：'+best.label) : best.label;
    } else {
      tip.style.display = 'none';
    }
  });
  canvas.addEventListener('wheel', e => {
    e.preventDefault();
    const k = e.deltaY > 0 ? 0.92 : 1.08;
    scale = Math.min(3.2, Math.max(0.25, scale*k));
    alpha = 0.3; if (!running){ running = true; requestAnimationFrame(frame); }
  }, {passive:false});
})();
</script>
'''
s = s.replace("</body>", overlay + "</body>", 1)

p.write_text(s, encoding="utf-8")
print("已注入按钮 + 记忆星系面板")
