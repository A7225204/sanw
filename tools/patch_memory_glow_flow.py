import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\dashboard\index.html")
s = p.read_text(encoding="utf-8")

# ---------- 1) 连线绘制后插入"流动光点" ----------
anchor_cmp = """    const ordered = [...nodes].filter(n => pts.has(n.id) && n.reveal > 0.02)"""
flow_code = """    // ---- 连线上的流动光点（记忆在流动的感觉）----
    ctx.globalCompositeOperation = 'lighter';
    const now = performance.now() * 0.001;
    let li = 0;
    for (const l of ls){
      li++;
      const depth = Math.max(0, 1 - l.z/2400);
      const speed = l.scope ? 0.22 : 0.34;
      const phase = ((now * speed) + li * 0.137) % 1;
      // 每条线两个光点，来回流动
      for (const dir of (phase < 0.5 ? [phase*2] : [(phase-0.5)*2])){
        const t = l.scope ? dir : 1 - dir;
        const fx = l.a.sx + (l.b.sx - l.a.sx) * t;
        const fy = l.a.sy + (l.b.sy - l.a.sy) * t;
        const rad = Math.max(1.0, 3.2 * depth);
        const fg = ctx.createRadialGradient(fx, fy, 0, fx, fy, rad * 3.2);
        fg.addColorStop(0, `rgba(200,240,255,${0.75 * depth * l.rv})`);
        fg.addColorStop(0.4, `rgba(120,200,255,${0.32 * depth * l.rv})`);
        fg.addColorStop(1, 'rgba(0,0,0,0)');
        ctx.fillStyle = fg;
        ctx.beginPath(); ctx.arc(fx, fy, rad * 3.2, 0, Math.PI*2); ctx.fill();
      }
    }
    ctx.globalCompositeOperation = 'source-over';

"""
if anchor_cmp in s and "流动光点" not in s:
    s = s.replace(anchor_cmp, flow_code + anchor_cmp, 1)
else:
    print("流量插入锚点没找到或已存在")

# ---------- 2) 节点发光增强（叠加混合 + 脉冲） ----------
old_node = """      const g = ctx.createRadialGradient(pr.sx, pr.sy, 0, pr.sx, pr.sy, Math.max(2, r*4.5));
      g.addColorStop(0, `hsla(${n.col.h}, 92%, 72%, ${0.8*depth})`);
      g.addColorStop(0.35, `hsla(${n.col.h}, 88%, 60%, ${0.25*depth})`);
      g.addColorStop(1, 'hsla(0,0%,0%,0)');
      ctx.fillStyle = g;
      ctx.beginPath(); ctx.arc(pr.sx, pr.sy, Math.max(2, r*4.5), 0, Math.PI*2); ctx.fill();

      ctx.fillStyle = `hsla(${n.col.h}, 95%, ${72 + depth*18}%, ${0.55 + depth*0.4})`;
      ctx.beginPath(); ctx.arc(pr.sx, pr.sy, Math.max(0.5, r*0.45), 0, Math.PI*2); ctx.fill();"""

new_node = """      // 呼吸脉冲：每个节点相位不同
      const pulse = 1 + 0.12 * Math.sin(now * 1.6 + (n.revealDelay || 0) * 0.01);
      const glowR = Math.max(2.4, r * 6.0 * pulse);

      ctx.globalCompositeOperation = 'lighter';
      // 外层大光晕
      const g = ctx.createRadialGradient(pr.sx, pr.sy, 0, pr.sx, pr.sy, glowR);
      g.addColorStop(0, `hsla(${n.col.h}, 95%, 78%, ${0.55*depth})`);
      g.addColorStop(0.28, `hsla(${n.col.h}, 92%, 64%, ${0.22*depth})`);
      g.addColorStop(0.62, `hsla(${n.col.h}, 90%, 52%, ${0.07*depth})`);
      g.addColorStop(1, 'hsla(0,0%,0%,0)');
      ctx.fillStyle = g;
      ctx.beginPath(); ctx.arc(pr.sx, pr.sy, glowR, 0, Math.PI*2); ctx.fill();

      // 内核（偏白，像真的在发亮）
      const core = Math.max(0.7, r * 0.5);
      const cg = ctx.createRadialGradient(pr.sx, pr.sy, 0, pr.sx, pr.sy, core * 2.6);
      cg.addColorStop(0, `hsla(${n.col.h}, 100%, 96%, ${(0.85 + depth*0.15) * n.reveal})`);
      cg.addColorStop(0.45, `hsla(${n.col.h}, 96%, 76%, ${(0.5 + depth*0.4) * n.reveal})`);
      cg.addColorStop(1, 'hsla(0,0%,0%,0)');
      ctx.fillStyle = cg;
      ctx.beginPath(); ctx.arc(pr.sx, pr.sy, core * 2.6, 0, Math.PI*2); ctx.fill();
      ctx.globalCompositeOperation = 'source-over';

      // 实心点
      ctx.fillStyle = `hsla(${n.col.h}, 100%, 92%, ${(0.7 + depth*0.3) * n.reveal})`;
      ctx.beginPath(); ctx.arc(pr.sx, pr.sy, Math.max(0.6, core * 0.55), 0, Math.PI*2); ctx.fill();"""

if old_node in s:
    s = s.replace(old_node, new_node, 1)
else:
    print("节点发光替换没匹配上")

p.write_text(s, encoding="utf-8")
print("已加：粒子自发光(叠加混合+脉冲) + 连线流动光点")
