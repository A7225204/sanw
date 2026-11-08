import pathlib

root = pathlib.Path(r"D:\qq-ai-bot\dashboard-web\src\features\memory-viz")

# ---------- 1) 树：布局更松、只给主题/分组加标签、渲染后自适应 ----------
p = root / "TreeView/TreeView.tsx"
s = p.read_text(encoding="utf-8")
s = s.replace(
"            labelText: data.lod === 'compact' && n.kind === 'memory' ? '' : n.summary.slice(0, 16),",
"            labelText: n.kind === 'memory' ? '' : n.summary.slice(0, 12),\n"
"            labelPlacement: 'right',\n"
"            labelFontSize: 11,")
s = s.replace("            size: n.kind === 'scope' ? 26 : n.kind === 'topic' ? 18 : 10,",
              "            size: n.kind === 'scope' ? 30 : n.kind === 'topic' ? 20 : 8,")
s = s.replace(
"      layout: { type: 'compact-box', direction: 'LR', getHeight: () => 26, getWidth: () => 100, getVGap: () => 8 },",
"      layout: { type: 'compact-box', direction: 'LR', getHeight: () => 30, getWidth: () => 90,\n"
"                getVGap: () => 34, getHGap: () => 140 },")
s = s.replace("    graph.render();", "    graph.render().then(() => { try { (graph as any).fitView(); } catch {} });")
p.write_text(s, encoding="utf-8")

# ---------- 2) 星海：颜色改用 setColorAt，体积放大 ----------
p = root / "StarfieldView/starfieldRenderer.ts"
s = p.read_text(encoding="utf-8")
s = s.replace(
"""  const mesh = new THREE.InstancedMesh(geo, mat, Math.max(1, nodes.length));
  mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
  const colorAttr = new THREE.InstancedBufferAttribute(new Float32Array(nodes.length * 3), 3);
  geo.setAttribute('instanceColor', colorAttr);
""",
"""  const mesh = new THREE.InstancedMesh(geo, mat, Math.max(1, nodes.length));
  mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
  mesh.instanceColor = new THREE.InstancedBufferAttribute(new Float32Array(nodes.length * 3), 3);
""")
s = s.replace("    const s = 0.006 + 0.02 * sf.size;", "    const s = 0.012 + 0.03 * sf.size;")
s = s.replace(
"""    color.set(sf.color);
    const b = 0.35 + 0.65 * sf.brightness;
    colorAttr.setXYZ(i, color.r * b, color.g * b, color.b * b);""",
"""    color.set(sf.color);
    const b = 0.55 + 0.45 * sf.brightness;
    mesh.setColorAt(i, color.clone().multiplyScalar(b));""")
s = s.replace("  colorAttr.needsUpdate = true;", "  if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;")
s = s.replace("        const s = (0.006 + 0.02 * sf.size) * (hi ? 1.6 : 1);",
              "        const s = (0.012 + 0.03 * sf.size) * (hi ? 1.6 : 1);")
p.write_text(s, encoding="utf-8")
print("已调整树的布局/标签 与 星海颜色/尺寸")
