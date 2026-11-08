import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\dashboard\index.html")
s = p.read_text(encoding="utf-8")

start = s.find('<div id="memOverlay"')
end = s.find("</script>", start)
if start < 0 or end < 0:
    raise SystemExit("找不到记忆面板块")
end += len("</script>")

new_block = '''<div id="memOverlay" class="mem-overlay">
  <canvas id="memCanvas"></canvas>
  <div class="mem-head">
    <div class="mem-title">🌌 记忆星河 · 3D</div>
    <div class="mem-stats" id="memStats">加载中…</div>
    <button class="mem-close" id="memClose">关闭</button>
  </div>
  <div class="mem-legend" id="memLegend"></div>
  <div class="mem-tip" id="memTip"></div>
  <div class="mem-hint">拖拽旋转 · 滚轮缩放 · 单击节点飞过去 · 双击复位</div>
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
  let yaw = 0.6, pitch = -0.30, dist = 1350;
  let focus = {x:0, y:0, z:0}, focusTarget = {x:0, y:0, z:0};
  let rotSpeed = 0.00006;           // 自转很慢，避免晃动
  let simAlpha = 1, running = false, dpr = 1;
  let drag = null, last = 0, revealClock = 0, flying = null;

  function colorOf(group){
    let h = 0;
    for (let i=0;i<group.length;i++) h = (h*31 + group.charCodeAt(i)) % 360;
    return {h, base:`hsl(${h}, 85%, 62%)`};
  }

  function timeRank(t){
    // "10-22" → 可比较的数值；没有时间的排最后
    const m = /^(\\d{1,2})-(\\d{1,2})$/.exec(String(t||'').trim());
    if (!m) return 9999;
    return parseInt(m[1],10)*100 + parseInt(m[2],10);
  }

  async function load(){
    const r = await fetch('/api/memory_graph', {cache:'no-store'});
    const data = await r.json();
    if (!data.nodes) { statsEl.textContent = '读取失败'; return; }
    nodes = data.nodes.map(n => {
      const R = 700;
      const u = Math.random(), v = Math.random();
      const th = 2*Math.PI*u, ph = Math.acos(2*v-1);
      const rr = R * Math.cbrt(Math.random());
      return {
        ...n,
        x: rr*Math.sin(ph)*Math.cos(th),
        y: rr*Math.sin(ph)*Math.sin(th) * 0.55,
        z: rr*Math.cos(ph),
        vx:0, vy:0, vz:0,
        col: colorOf(n.group || 'x'),
        reveal: 0
      };
    });
    // 按时间分配出场顺序（越早的记忆越先亮）
    const ranked = [...nodes].sort((a,b)=>timeRank(a.time) - timeRank(b.time));
    ranked.forEach((n, i) => { n.revealDelay = (i / Math.max(1, nodes.length)) * 2200; });
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
    simAlpha = 1; revealClock = 0;
  }

  function resize(){
    dpr = window.devicePixelRatio || 1;
    canvas.width = innerWidth * dpr;
    canvas.height = innerHeight * dpr;
  }

  // ---- 力导向：alpha 衰减，稳定后不再抖 ----
  function simulate(dt){
    if (simAlpha < 0.02) return;          // 收敛后冻结，画面就稳了
    simAlpha *= Math.pow(0.985, dt/16.7);
    const A = simAlpha;
    for (let i=0;i<nodes.length;i++){
      const a = nodes[i];
      for (let j=i+1;j<nodes.length;j++){
        const b = nodes[j];
        let dx=a.x-b.x, dy=a.y-b.y, dz=a.z-b.z;
        const d2 = dx*dx+dy*dy+dz*dz+400;
        if (d2 > 300000) continue;
        const f = (7000/d2) * A;
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
      const target = (a.kind==='scope'||b.kind==='scope') ? 300 : 110;
      const f = ((d-target)*0.00016) * A;
      a.vx+=dx/d*f*60; a.vy+=dy/d*f*60; a.vz+=dz/d*f*60;
      b.vx-=dx/d*f*60; b.vy-=dy/d*f*60; b.vz-=dz/d*f*60;
    }
    for (const n of nodes){
      n.vx -= n.x*0.00028*A; n.vy -= n.y*0.0005*A; n.vz -= n.z*0.00028*A;
      n.vx*=0.93; n.vy*=0.93; n.vz*=0.93;   // 阻尼更大 → 不抖
      n.x+=n.vx; n.y+=n.vy; n.z+=n.vz;
    }
  }

  let cosY=1, sinY=0, cosP=1, sinP=0, cx=0, cy=0, fov=0;
  function prepare(){
    cosY = Math.cos(yaw); sinY = Math.sin(yaw);
    cosP = Math.cos(pitch); sinP = Math.sin(pitch);
    cx = innerWidth/2; cy = innerHeight/2;
    fov = Math.min(innerWidth, innerHeight) * 0.9;
  }
  function project(n){
    const x0 = n.x - focus.x, y0 = n.y - focus.y, z0 = n.z - focus.z;
    const x1 = x0*cosY - z0*sinY;
    const z1 = x0*sinY + z0*cosY;
    const y2 = y0*cosP - z1*sinP;
    const z2 = y0*sinP + z1*cosP;
    const zc = z2 + dist;
    if (zc < 60) return null;
    const k = fov / zc;
    return {sx: cx + x1*k, sy: cy + y2*k, k, zc};
  }

  function draw(dt){
    ctx.setTransform(dpr,0,0,dpr,0,0);
    ctx.clearRect(0,0,innerWidth,innerHeight);

    // 背景星尘（缓慢闪烁）
    const t = performance.now()/1000;
    for (let i=0;i<90;i++){
      const sx = (i*197.13) % innerWidth;
      const sy = (i*i*53.7) % innerHeight;
      const a = 0.10 + 0.10*Math.abs(Math.sin(t*0.5 + i));
      ctx.fillStyle = `rgba(140,205,255,${a})`;
      ctx.fillRect(sx, sy, 1.3, 1.3);
    }

    prepare();
    const pts = new Map();
    for (const n of nodes){
      // 按时间淡入
      revealClock += dt;
      if (revealClock > (n.revealDelay||0) && n.reveal < 1) n.reveal = Math.min(1, n.reveal + dt/900);
      const pr = project(n);
      if (pr) pts.set(n.id, pr);
    }

    const ls = links.map(l => {
      const a = pts.get(l.source), b = pts.get(l.target);
      if (!a || !b) return null;
      const na = byId.get(l.source), nb = byId.get(l.target);
      const rv = Math.min(na.reveal||0, nb.reveal||0);
      return {a, b, rv, z:(a.zc+b.zc)/2, scope:(na.kind==='scope'||nb.kind==='scope')};
    }).filter(Boolean).sort((p,q)=>q.z-p.z);

    for (const l of ls){
      const depth = Math.max(0, 1 - l.z/2400);
      ctx.strokeStyle = l.scope
        ? `rgba(110,190,240,${(0.05 + depth*0.26)*l.rv})`
        : `rgba(80,140,200,${(0.03 + depth*0.14)*l.rv})`;
      ctx.lineWidth = l.scope ? 1.1 : 0.7;
      ctx.beginPath(); ctx.moveTo(l.a.sx, l.a.sy); ctx.lineTo(l.b.sx, l.b.sy); ctx.stroke();
    }

    const ordered = [...nodes].filter(n => pts.has(n.id) && n.reveal > 0.02)
      .sort((m,n)=>pts.get(n.id).zc - pts.get(m.id).zc);
    for (const n of ordered){
      const pr = pts.get(n.id);
      const r = Math.max(0.6, n.size * pr.k * (n.kind==='scope' ? 2.0 : 1.0));
      const depth = Math.max(0.12, Math.min(1, 1 - pr.zc/2600)) * n.reveal;

      const g = ctx.createRadialGradient(pr.sx, pr.sy, 0, pr.sx, pr.sy, Math.max(2, r*4.5));
      g.addColorStop(0, `hsla(${n.col.h}, 92%, 72%, ${0.8*depth})`);
      g.addColorStop(0.35, `hsla(${n.col.h}, 88%, 60%, ${0.25*depth})`);
      g.addColorStop(1, 'hsla(0,0%,0%,0)');
      ctx.fillStyle = g;
      ctx.beginPath(); ctx.arc(pr.sx, pr.sy, Math.max(2, r*4.5), 0, Math.PI*2); ctx.fill();

      ctx.fillStyle = `hsla(${n.col.h}, 95%, ${72 + depth*18}%, ${0.55 + depth*0.4})`;
      ctx.beginPath(); ctx.arc(pr.sx, pr.sy, Math.max(0.5, r*0.45), 0, Math.PI*2); ctx.fill();

      if (n.kind === 'scope'){
        ctx.font = '12px system-ui';
        ctx.fillStyle = `rgba(215,240,255,${0.5 + depth*0.4})`;
        ctx.fillText(n.label, pr.sx + r*1.4, pr.sy + 4);
      }
    }
  }

  function loop(t){
    if (!running) return;
    const dt = Math.min(40, t - (last || t)); last = t;

    if (autoRotate && !drag) yaw += dt * rotSpeed;

    // 相机平滑飞向焦点
    focus.x += (focusTarget.x - focus.x) * 0.08;
    focus.y += (focusTarget.y - focus.y) * 0.08;
    focus.z += (focusTarget.z - focus.z) * 0.08;
    if (flying){
      dist += (flying.dist - dist) * 0.08;
      if (Math.abs(flying.dist - dist) < 4) flying = null;
    }

    simulate(dt);
    draw(dt);
    requestAnimationFrame(loop);
  }

  let autoRotate = true;

  function open(){
    overlay.classList.add('show');
    resize();
    yaw = 0.6; pitch = -0.30; dist = 1350;
    focus = {x:0,y:0,z:0}; focusTarget = {x:0,y:0,z:0};
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

  canvas.addEventListener('mousedown', e => {
    drag = {x:e.clientX, y:e.clientY, yaw, pitch, moved:false};
    canvas.classList.add('dragging');
  });
  window.addEventListener('mouseup', e => {
    if (drag && !drag.moved){
      // 单击：找最近节点，飞过去
      prepare();
      let best = null, bestD = 26;
      for (const n of nodes){
        const pr = project(n);
        if (!pr) continue;
        const d = Math.hypot(pr.sx - e.clientX, pr.sy - e.clientY);
        if (d < bestD){ bestD = d; best = n; }
      }
      if (best){
        focusTarget = {x:best.x, y:best.y, z:best.z};
        flying = {dist: best.kind === 'scope' ? 420 : 240};
        statsEl.textContent = (best.kind === 'subject' ? '主题：' : '') + best.label;
      }
    }
    drag = null; canvas.classList.remove('dragging');
  });
  canvas.addEventListener('mousemove', e => {
    if (drag){
      const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
      if (Math.abs(dx) + Math.abs(dy) > 4) drag.moved = true;
      if (drag.moved){
        yaw = drag.yaw + dx * 0.005;
        pitch = Math.max(-1.3, Math.min(1.3, drag.pitch + dy * 0.004));
      }
      return;
    }
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
    dist = Math.min(2600, Math.max(200, dist * (e.deltaY > 0 ? 1.1 : 0.9)));
    flying = null;
  }, {passive:false});
  canvas.addEventListener('dblclick', () => {
    yaw = 0.6; pitch = -0.30; dist = 1350;
    focusTarget = {x:0,y:0,z:0}; flying = null;
    statsEl.textContent = `${nodes.filter(n=>n.kind==='fact').length} 条事实`;
  });
})();
</script>'''

s = s[:start] + new_block + s[end:]
p.write_text(s, encoding="utf-8")
print("已更新：轻力+快速收敛、点击飞行、时间淡入、慢自转")
