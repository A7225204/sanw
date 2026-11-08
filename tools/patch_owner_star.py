import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\dashboard\index.html")
s = p.read_text(encoding="utf-8")
changed = []

# ---------- 1) load(): 主人系球固定到中心并标记 owner ----------
old_load = """    // 按时间分配出场顺序（越早的记忆越先亮）"""
new_load = """    // 主人的私聊（QQ / 飞书）放在中心当主星，并标记为 owner（会往外喷粒子）
    const OWNER_SCOPES = ['private_100000000'];
    nodes.forEach(n => {
      if (n.kind === 'scope' && (OWNER_SCOPES.includes(n.group) || (n.group||'').startsWith('private_ou_'))) {
        n.owner = true;
        n.x = 0; n.y = 0; n.z = 0;
        n.vx = 0; n.vy = 0; n.vz = 0;
        n.size = Math.max(n.size, 30);
      }
    });
    // 飞书主人的球稍微偏一点，避免和 QQ 完全重叠
    nodes.forEach(n => {
      if (n.kind === 'scope' && (n.group||'').startsWith('private_ou_')) { n.x = 210; n.y = -60; n.z = 120; }
    });

    // 按时间分配出场顺序（越早的记忆越先亮）"""
if old_load in s:
    s = s.replace(old_load, new_load, 1)
    changed.append("load")

# ---------- 2) simulate(): owner 球不参与斥力位移（钉在中心） ----------
s = s.replace("""    for (const n of nodes){
      n.vx -= n.x*0.00028*A; n.vy -= n.y*0.0005*A; n.vz -= n.z*0.00028*A;""",
"""    for (const n of nodes){
      if (n.owner && n.kind === 'scope') { n.vx = n.vy = n.vz = 0; continue; }
      n.vx -= n.x*0.00028*A; n.vy -= n.y*0.0005*A; n.vz -= n.z*0.00028*A;""", 1)

# ---------- 3) 流动光点：连到主人球的线加强 ----------
s = s.replace("""      const speed = l.scope ? 0.30 : 0.48;
      // 每条线 3 个光点，等距错开，看起来是连续流
      for (let pi = 0; pi < 3; pi++){""",
"""      const speed = l.scope ? 0.30 : 0.48;
      const isOwnerLink = (l.ownerLink === true);
      const pcount = isOwnerLink ? 5 : 3;   // 主人的线粒子上更多
      for (let pi = 0; pi < pcount; pi++){""", 1)
s = s.replace("""        let t = ((now * speed) + li * 0.137 + pi / 3) % 1;""",
"""        let t = ((now * speed) + li * 0.137 + pi / pcount) % 1;""", 1)

# ls 计算时标记 ownerLink
s = s.replace("""      const dm = (activeScope && na.group !== activeScope) ? 0.15 : 1;
      return {a, b, rv: rv*dm, z:(a.zc+b.zc)/2, scope:(na.kind==='scope'||nb.kind==='scope')};""",
"""      const dm = (activeScope && na.group !== activeScope) ? 0.15 : 1;
      return {a, b, rv: rv*dm, ownerLink: !!(na.owner || nb.owner),
              z:(a.zc+b.zc)/2, scope:(na.kind==='scope'||nb.kind==='scope')};""", 1)

# ---------- 4) 主人球：额外光环 + 向外扩散的粒子 ----------
owner_draw = """
      // === 主人球：额外光环 + 向外扩散的粒子（主星效果）===
      if (n.owner){
        ctx.globalCompositeOperation = 'lighter';
        // 两个呼吸光环
        for (let ring = 0; ring < 2; ring++){
          const ph = ((now * 0.45) + ring * 0.5) % 1;
          const rr = (r * 2.2) + ph * r * 7;
          const ra = (1 - ph) * 0.28 * depth;
          ctx.strokeStyle = `hsla(${n.col.h}, 95%, 75%, ${ra})`;
          ctx.lineWidth = 1.4;
          ctx.beginPath(); ctx.arc(pr.sx, pr.sy, rr, 0, Math.PI*2); ctx.stroke();
        }
        // 向外飞散的小粒子（每帧按相位取位置，形成持续喷射）
        const PN = 26;
        for (let pi = 0; pi < PN; pi++){
          const seed = pi * 12.9898;
          const ang = (seed % (Math.PI*2));
          const ph = ((now * 0.35) + (pi / PN)) % 1;
          const rr = r * 1.6 + ph * r * 9;
          const px = pr.sx + Math.cos(ang) * rr;
          const py = pr.sy + Math.sin(ang) * rr * 0.75;
          const pa = (1 - ph) * 0.5 * depth;
          const pg = ctx.createRadialGradient(px, py, 0, px, py, 3.2);
          pg.addColorStop(0, `hsla(${n.col.h}, 98%, 82%, ${pa})`);
          pg.addColorStop(1, 'hsla(0,0%,0%,0)');
          ctx.fillStyle = pg;
          ctx.beginPath(); ctx.arc(px, py, 3.2, 0, Math.PI*2); ctx.fill();
        }
        ctx.globalCompositeOperation = 'source-over';
      }
"""
marker = """      if (n.kind === 'scope'){
        ctx.font = '12px system-ui';"""
if "主人球：额外光环" not in s and marker in s:
    s = s.replace(marker, owner_draw + marker, 1)
    changed.append("owner-draw")

# 主人球的光晕更大
s = s.replace("      const glowR = Math.max(2.4, r * 6.0 * pulse);",
              "      const glowR = Math.max(2.4, r * (n.owner ? 9.5 : 6.0) * pulse);", 1)

p.write_text(s, encoding="utf-8")
print("改动:", changed)
