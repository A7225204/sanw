import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\dashboard-web\src\features\memory-viz\types.ts")
s = p.read_text(encoding="utf-8")
if "kind?: 'intra' | 'cross'" not in s:
    s = s.replace("export interface StarfieldEdge {\n  source: string;\n  target: string;\n  weight: number;\n}",
                  "export interface StarfieldEdge {\n  source: string;\n  target: string;\n  weight: number;\n  kind?: 'intra' | 'cross';\n}")
    p.write_text(s, encoding="utf-8")
    print("types: StarfieldEdge 加 kind")

# 渲染器：团内边蓝色、跨团边紫色（同名主题跨星河）
r = pathlib.Path(r"D:\qq-ai-bot\dashboard-web\src\features\memory-viz\StarfieldView\starfieldRenderer.ts")
t = r.read_text(encoding="utf-8")

old = '''  const linePos: number[] = [];
  const flowSeeds: { a: THREE.Vector3; b: THREE.Vector3; phase: number; speed: number }[] = [];
  for (const e of edges) {
    const ia = indexOf.get(e.source) ?? -1;
    const ib = indexOf.get(e.target) ?? -1;
    if (ia < 0 || ib < 0) continue;
    const a = new THREE.Vector3(positions[ia * 3], positions[ia * 3 + 1], positions[ia * 3 + 2]);
    const b = new THREE.Vector3(positions[ib * 3], positions[ib * 3 + 1], positions[ib * 3 + 2]);
    linePos.push(a.x, a.y, a.z, b.x, b.y, b.z);
    flowSeeds.push({ a, b, phase: Math.random(), speed: 0.18 + Math.random() * 0.22 });
  }
  const lineGeo = new THREE.BufferGeometry();
  lineGeo.setAttribute('position', new THREE.Float32BufferAttribute(linePos, 3));
  const lineMat = new THREE.LineBasicMaterial({
    color: 0x3f86bd, transparent: true, opacity: 0.34,
    blending: THREE.AdditiveBlending, depthWrite: false,
  });
  scene.add(new THREE.LineSegments(lineGeo, lineMat));

  const flowPos = new Float32Array(Math.max(1, flowSeeds.length) * 3);
  const flowGeo = new THREE.BufferGeometry();
  flowGeo.setAttribute('position', new THREE.BufferAttribute(flowPos, 3));
  const flowMat = new THREE.PointsMaterial({
    color: 0xeaf6ff, size: 0.03, transparent: true, opacity: 0.9,
    blending: THREE.AdditiveBlending, depthWrite: false, map: makeGlowTexture(),
  });
  scene.add(new THREE.Points(flowGeo, flowMat));'''

new = '''  // 团内边（蓝）与跨团边（紫，同名主题跨星河）分开画
  const intraPos: number[] = [];
  const crossPos: number[] = [];
  const flowSeeds: { a: THREE.Vector3; b: THREE.Vector3; phase: number; speed: number; cross: boolean }[] = [];
  for (const e of edges) {
    const ia = indexOf.get(e.source) ?? -1;
    const ib = indexOf.get(e.target) ?? -1;
    if (ia < 0 || ib < 0) continue;
    const a = new THREE.Vector3(positions[ia * 3], positions[ia * 3 + 1], positions[ia * 3 + 2]);
    const b = new THREE.Vector3(positions[ib * 3], positions[ib * 3 + 1], positions[ib * 3 + 2]);
    const cross = e.kind === 'cross';
    (cross ? crossPos : intraPos).push(a.x, a.y, a.z, b.x, b.y, b.z);
    flowSeeds.push({ a, b, phase: Math.random(), speed: cross ? 0.30 : 0.2, cross });
  }

  const mkLines = (posArr: number[], color: number, opacity: number) => {
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.Float32BufferAttribute(posArr, 3));
    const m = new THREE.LineBasicMaterial({
      color, transparent: true, opacity,
      blending: THREE.AdditiveBlending, depthWrite: false,
    });
    const seg = new THREE.LineSegments(g, m);
    scene.add(seg);
    return { g, m };
  };
  const intraLines = mkLines(intraPos, 0x3f86bd, 0.32);
  const crossLines = mkLines(crossPos, 0xb07bff, 0.6);

  const flowPos = new Float32Array(Math.max(1, flowSeeds.length) * 3);
  const flowColor = new Float32Array(Math.max(1, flowSeeds.length) * 3);
  let fiColor = 0;
  for (const s of flowSeeds) {
    const c = new THREE.Color(s.cross ? 0xd8c4ff : 0xeaf6ff);
    flowColor[fiColor * 3] = c.r;
    flowColor[fiColor * 3 + 1] = c.g;
    flowColor[fiColor * 3 + 2] = c.b;
    fiColor++;
  }
  const flowGeo = new THREE.BufferGeometry();
  flowGeo.setAttribute('position', new THREE.BufferAttribute(flowPos, 3));
  flowGeo.setAttribute('color', new THREE.BufferAttribute(flowColor, 3));
  const flowMat = new THREE.PointsMaterial({
    size: 0.032, transparent: true, opacity: 0.9, vertexColors: true,
    blending: THREE.AdditiveBlending, depthWrite: false, map: makeGlowTexture(),
  });
  scene.add(new THREE.Points(flowGeo, flowMat));'''
if old in t:
    t = t.replace(old, new, 1)

# dispose 里补上新几何体
t = t.replace("      lineGeo.dispose();\n      lineMat.dispose();",
              "      intraLines.g.dispose();\n      intraLines.m.dispose();\n      crossLines.g.dispose();\n      crossLines.m.dispose();")

r.write_text(t, encoding="utf-8")
print("渲染器：团内蓝线 / 跨团紫线 + 流动点分色")
