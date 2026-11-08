import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\dashboard\index.html")
s = p.read_text(encoding="utf-8")
changed = []

# ---------- 1) 连线改成"流动虚线" ----------
old_link = """    for (const l of ls){
      const depth = Math.max(0, 1 - l.z/2400);
      ctx.strokeStyle = l.scope
        ? `rgba(110,190,240,${(0.05 + depth*0.26)*l.rv})`
        : `rgba(80,140,200,${(0.03 + depth*0.14)*l.rv})`;
      ctx.lineWidth = l.scope ? 1.1 : 0.7;
      ctx.beginPath(); ctx.moveTo(l.a.sx, l.a.sy); ctx.lineTo(l.b.sx, l.b.sy); ctx.stroke();
    }"""

new_link = """    // 画"流动的虚线"：dashOffset 随时间推进，看起来像数据在管子里跑
    const flowNow = performance.now() * 0.001;
    ctx.setLineDash([3, 9]);
    for (const l of ls){
      const depth = Math.max(0, 1 - l.z/2400);
      ctx.strokeStyle = l.scope
        ? `rgba(120,200,250,${(0.07 + depth*0.30)*l.rv})`
        : `rgba(90,150,210,${(0.04 + depth*0.18)*l.rv})`;
      ctx.lineWidth = l.scope ? 1.3 : 0.9;
      ctx.lineDashOffset = -(flowNow * (l.scope ? 26 : 40)) % 12;
      ctx.beginPath(); ctx.moveTo(l.a.sx, l.a.sy); ctx.lineTo(l.b.sx, l.b.sy); ctx.stroke();
    }
    ctx.setLineDash([]);"""
if old_link in s:
    s = s.replace(old_link, new_link, 1)
    changed.append("dashed")

# ---------- 2) 加强流动光点：每条线 3 个光点 + 拖尾 ----------
old_flow_start = "    const now = performance.now() * 0.001;\n    let li = 0;\n    for (const l of ls){"
i = s.find(old_flow_start)
if i > 0:
    # 找到这段 flow 的结尾（下一个 ctx.globalCompositeOperation = 'source-over';）
    j = s.find("ctx.globalCompositeOperation = 'source-over';", i)
    k = s.find("\n", j) + 1
    new_flow = """    const now = performance.now() * 0.001;
    let li = 0;
    for (const l of ls){
      li++;
      const depth = Math.max(0, 1 - l.z/2400);
      if (depth < 0.05) continue;
      const speed = l.scope ? 0.30 : 0.48;
      // 每条线 3 个光点，等距错开，看起来是连续流
      for (let pi = 0; pi < 3; pi++){
        let t = ((now * speed) + li * 0.137 + pi / 3) % 1;
        if (!l.scope) t = 1 - t;
        const fx = l.a.sx + (l.b.sx - l.a.sx) * t;
        const fy = l.a.sy + (l.b.sy - l.a.sy) * t;
        // 拖尾：在后面 3 个位置画渐弱的小点
        for (let tr = 3; tr >= 1; tr--){
          const tt = Math.max(0, t - (l.scope ? 0.03 : 0.025) * tr);
          const tx = l.a.sx + (l.b.sx - l.a.sx) * tt;
          const ty = l.a.sy + (l.b.sy - l.a.sy) * tt;
          const ta = (0.26 / tr) * depth * l.rv;
          const tr2 = Math.max(0.8, 2.0 * depth);
          const tg = ctx.createRadialGradient(tx, ty, 0, tx, ty, tr2 * 2.6);
          tg.addColorStop(0, `rgba(170,225,255,${ta})`);
          tg.addColorStop(1, 'rgba(0,0,0,0)');
          ctx.fillStyle = tg;
          ctx.beginPath(); ctx.arc(tx, ty, tr2 * 2.6, 0, Math.PI*2); ctx.fill();
        }
        // 光点头部
        const rad = Math.max(1.4, 4.2 * depth);
        const fg = ctx.createRadialGradient(fx, fy, 0, fx, fy, rad * 3.6);
        fg.addColorStop(0, `rgba(235,250,255,${0.95 * depth * l.rv})`);
        fg.addColorStop(0.3, `rgba(140,215,255,${0.5 * depth * l.rv})`);
        fg.addColorStop(1, 'rgba(0,0,0,0)');
        ctx.fillStyle = fg;
        ctx.beginPath(); ctx.arc(fx, fy, rad * 3.6, 0, Math.PI*2); ctx.fill();
      }
    }
"""
    s = s[:i] + new_flow + s[k:]
    changed.append("flow")
else:
    print("flow 段没找到")

p.write_text(s, encoding="utf-8")
print("改动:", changed)
