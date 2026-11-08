import pathlib
import re

p = pathlib.Path(r"D:\qq-ai-bot\dashboard\index.html")
s = p.read_text(encoding="utf-8")

start = s.find('<div id="memOverlay"')
if start < 0:
    raise SystemExit("找不到 memOverlay，先跑上一版补丁")
end = s.find("</script>", start)
if end < 0:
    raise SystemExit("找不到结束标签")
end += len("</script>")
old_block = s[start:end]

new_block = '''<div id="memOverlay" class="mem-overlay">
  <canvas id="memCanvas"></canvas>
  <div class="mem-head">
    <div class="mem-title">🌌 记忆星河 · 3D</div>
    <div class="mem-stats" id="memStats">加载中…</div>
    <button class="mem-close" id="memClose">关闭</button>
  </div>
  <div class="mem-legend" id="memLegend"></div>
  <div class="mem-tip" id="memTip"></div>
  <div class="mem-hint">拖拽旋转 · 滚轮缩放 · 双击复位 · 悬停看内容</div>
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
  let yaw = 0.6, pitch = -0.35, dist = 1250, autoRotate = true;
  let running = false, dpr = 1;
  let drag = null, last = 0;

  function colorOf(group){
    let h = 0;
    for (let i=0;i<group.length;i++) h = (h*31 + group.charCodeAt(i)) % 360;
    return {h, base:`hsl(${h}, 85%, 62%)`, dim:`hsl(${h}, 80%, 46%)`};
  }

  async function load(){
    const r = await fetch('/api/memory_graph', {cache:'no-store'});
    const data = await r.json();
    if (!data.nodes) { statsEl.textContent = '读取失败'; return; }
    nodes = data.nodes.map(n => {
      const R = 620;
      // 球面均匀撒点，看起来才像星河
      const u = Math.random(), v = Math.random();
      const th = 2*Math.PI*u, ph = Math.acos(2*v-1);
      const rr = R * Math.cbrt(Math.random());
      return {
        ...n,
        x: rr*Math.sin(ph)*Math.cos(th),
        y: rr*Math.sin(ph)*Math.sin(th) * 0.55,
        z: rr*Math.cos(ph),
        vx:0, vy:0, vz:0,
        col: colorOf(n.group || 'x')
      };
    });
    byId = new Map(nodes.map(n => [n.id, n]));
    links = data.links.filter(l => byId.has(l.source) && byId.has(l.target));
    statsEl.textContent = `${data.stats.scopes} 个作用域 · ${data.stats.subjects} 个主题 · ${data.stats.facts} 条事实`;
    const counts = {};
    nodes.forEach(n => { if (n.kind==='fact') counts[n.group]=(counts[n.group]||0)+1; });
    const top = Object.entries(counts).sort((a,b)=>b[1]-a[1]).slice(0,6);
    legendEl.innerHTML = top.map(([g,c])=>{
      const nm = g.replace('group_','群 ').replace('private_','私聊 ');
      return `<div><i style="background:${colorOf(g).base}"></i>${nm}（${c}）</div>`;
    }).join('');
  }

  function resize(){
    dpr = window.devicePixelRatio || 1;
    canvas.width = innerWidth * dpr;
    canvas.height = innerHeight * dpr;
  }

  // ---- 3D 力导向 ----
  function simulate(){
    for (let i=0;i<nodes.length;i++){
      const a = nodes[i];
      for (let j=i+1;j<nodes.length;j++){
        const b = nodes[j];
        let dx=a.x-b.x, dy=a.y-b.y, dz=a.z-b.z;
        const d2 = dx*dx+dy*dy+dz*dz+120;
        if (d2 > 260000) continue;
        const f = 9000/d2;
        dx*=f; dy*=f; dz*=f;
        a.vx+=dx; a.vy+=dy; a.vz+=dz;
        b.vx-=dx; b.vy-=dy; b.vz-=dz;
      }
    }
    for (const l of links){
      const a = byId.get(l.source), b = byId.get(l.target);
      if (!a || !b) continue;
      const dx=b.x-a.x, dy=b.y-a.y, dz=b.z-a.z;
      const d = Math.sqrt(dx*dx+dy*dy+dz*dz)+0.01;
      const target = (a.kind==='scope'||b.kind==='scope') ? 260 : 90;
      const f = (d-target)*0.00022;
      a.vx+=dx/d*f*60; a.vy+=dy/d*f*60; a.vz+=dz/d*f*60;
      b.vx-=dx/d*f*60; b.vy-=dy/d*f*60; b.vz-=dz/d*f*60;
    }
    for (const n of nodes){
      // 向心
      n.vx -= n.x*0.00035; n.vy -= n.y*0.0006; n.vz -= n.z*0.00035;
      n.vx*=0.90; n.vy*=0.90; n.vz*=0.90;
      n.x+=n.vx; n.y+=n.vy; n.z+=n.vz;
    }
  }

  // ---- 投影 ----
  let cosY=1, sinY=0, cosP=1, sinP=0, cx=0, cy=0, fov=0;
  function prepare(){
    cosY = Math.cos(yaw); sinY = Math.sin(yaw);
    cosP = Math.cos(pitch); sinP = Math.sin(pitch);
    cx = innerWidth/2; cy = innerHeight/2;
    fov = Math.min(innerWidth, innerHeight) * 0.9;
  }
  function project(n){
    const x1 = n.x*cosY - n.z*sinY;
    const z1 = n.x*sinY + n.z*cosY;
    const y2 = n.y*cosP - z1*sinP;
    const z2 = n.y*sinP + z1*cosP;
    const zc = z2 + dist;
    if (zc < 60) return null;
    const k = fov / zc;
    return {sx: cx + x1*k, sy: cy + y2*k, k, zc};
  }

  // ---- 渲染 ----
  function draw(){
    ctx.setTransform(dpr,0,0,dpr,0,0);
    ctx.clearRect(0,0,innerWidth,innerHeight);

    // 背景星尘
    ctx.fillStyle = 'rgba(120,200,255,.22)';
    for (let i=0;i<70;i++){
      const sx = (i*197.13 % innerWidth);
      const sy = ((i*i*53.7) % innerHeight);
      ctx.fillRect(sx, sy, 1.2, 1.2);
    }

    prepare();
    const pts = new Map();
    for (const n of nodes){
      const pr = project(n);
      if (pr) pts.set(n.id, pr);
    }

    // 连线（远的先画）
    const ls = links.map(l => {
      const a = pts.get(l.source), b = pts.get(l.target);
      if (!a || !b) return null;
      return {a, b, z: (a.zc + b.zc) / 2, scope: (byId.get(l.source).kind==='scope' || byId.get(l.target).kind==='scope')};
    }).filter(Boolean).sort((p,q)=>q.z-p.z);

    for (const l of ls){
      const depth = Math.max(0, 1 - l.z/2400);
      ctx.strokeStyle = l.scope
        ? `rgba(110,190,240,${0.05 + depth*0.28})`
        : `rgba(80,140,200,${0.03 + depth*0.16})`;
      ctx.lineWidth = l.scope ? 1.1 : 0.7;
      ctx.beginPath(); ctx.moveTo(l.a.sx, l.a.sy); ctx.lineTo(l.b.sx, l.b.sy); ctx.stroke();
    }

    // 节点（远到近）
    const ordered = [...nodes].filter(n => pts.has(n.id)).sort((m,n)=>pts.get(n.id).zc - pts.get(m.id).zc);
    for (const n of ordered){
      const pr = pts.get(n.id);
      const r = Math.max(0.6, n.size * pr.k * (n.kind==='scope' ? 2.0 : 1.0));
      const depth = Math.max(0.12, Math.min(1, 1 - pr.zc/2600));

      // 光晕
      const g = ctx.createRadialGradient(pr.sx, pr.sy, 0, pr.sx, pr.sy, Math.max(2, r*4.5));
      g.addColorStop(0, `hsla(${n.col.h}, 92%, 72%, ${0.85*depth})`);
      g.addColorStop(0.35, `hsla(${n.col.h}, 88%, 60%, ${0.28*depth})`);
      g.addColorStop(1, 'hsla(0,0%,0%,0)');
      ctx.fillStyle = g;
      ctx.beginPath(); ctx.arc(pr.sx, pr.sy, Math.max(2, r*4.5), 0, Math.PI*2); ctx.fill();

      // 核心
      ctx.fillStyle = `hsla(${n.col.h}, 95%, ${72 + depth*18}%, ${0.55 + depth*0.45})`;
      ctx.beginPath(); ctx.arc(pr.sx, pr.sy, Math.max(0.5, r*0.45), 0, Math.PI*2); ctx.fill();

      if (n.kind === 'scope'){
        ctx.font = '12px system-ui';
        ctx.fillStyle = `rgba(215,240,255,${0.55 + depth*0.4})`;
        ctx.fillText(n.label, pr.sx + r*1.4, pr.sy + 4);
      }
    }
    statsEl.dataset.hover = '';
  }

  function loop(t){
    if (!running) return;
    const dt = Math.min(50, t - (last || t)); last = t;
    if (autoRotate && !drag) yaw += dt * 0.00018;
    simulate(); draw();
    requestAnimationFrame(loop);
  }

  function open(){
    overlay.classList.add('show');
    resize();
    yaw = 0.6; pitch = -0.35; dist = 1250; autoRotate = true;
    load().then(()=>{ running = true; last = 0; requestAnimationFrame(loop); });
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

  // 拖拽旋转
  canvas.addEventListener('mousedown', e => {
    drag = {x:e.clientX, y:e.clientY, yaw, pitch};
    canvas.classList.add('dragging');
  });
  window.addEventListener('mouseup', () => { drag = null; canvas.classList.remove('dragging'); });
  canvas.addEventListener('mousemove', e => {
    if (drag){
      yaw = drag.yaw + (e.clientX - drag.x) * 0.006;
      pitch = Math.max(-1.35, Math.min(1.35, drag.pitch + (e.clientY - drag.y) * 0.005));
      return;
    }
    // 悬停
    prepare();
    let best = null, bestD = 20;
    for (const n of nodes){
      const pr = project(n);
      if (!pr) continue;
      const d = Math.hypot(pr.sx - e.clientX, pr.sy - e.clientY);
      if (d < bestD){ bestD = d; best = n; }
    }
    if (best && best.kind !== 'scope'){
      tip.style.display = 'block';
      tip.style.left = (e.clientX + 14) + 'px';
      tip.style.top = (e.clientY + 12) + 'px';
      tip.textContent = best.kind === 'subject' ? ('主题：' + best.label) : best.label;
    } else {
      tip.style.display = 'none';
    }
  });
  canvas.addEventListener('mouseleave', () => { tip.style.display='none'; });
  canvas.addEventListener('wheel', e => {
    e.preventDefault();
    dist = Math.min(2600, Math.max(320, dist * (e.deltaY > 0 ? 1.1 : 0.9)));
  }, {passive:false});
  canvas.addEventListener('dblclick', () => { yaw = 0.6; pitch = -0.35; dist = 1250; });
})();
</script>'''

s = s[:start] + new_block + s[end:]

# 提示条样式
if ".mem-hint" not in s:
    s = s.replace("</style>", '''
  .mem-hint{position:absolute;right:24px;bottom:20px;color:#5d7d96;font-size:12px;z-index:2}
''', 1) if "</style>" in s else s
    s = s.replace("</style>", "</style>", 1)

p.write_text(s, encoding="utf-8")
print("已替换为 3D 星河版本，旧块长度", len(old_block), "新块长度", len(new_block))
