import pathlib

# ---------- 1) 后端：金线改连该星河"最亮的那颗主题星"，而不是孤立根节点 ----------
p = pathlib.Path(r"D:\qq-ai-bot\server\memory_viz_all.py")
s = p.read_text(encoding="utf-8")
old = '''            root_id = f"scope:{scope}"
            if root_id not in by_id:
                continue
            edges.append({
                "source": owner_root, "target": root_id,'''
new = '''            # 连到该星河里最重要的一颗主题星（根节点常常孤零零一颗，看着像没连上）
            cand = [n for n in out_nodes
                    if n.get("kind") == "topic" and n.get("scope") == scope]
            cand.sort(key=lambda x: -float(x.get("importance") or 0))
            root_id = cand[0]["id"] if cand else f"scope:{scope}"
            if root_id not in by_id:
                continue
            edges.append({
                "source": owner_root, "target": root_id,'''
if old in s:
    s = s.replace(old, new, 1)
    p.write_text(s, encoding="utf-8")
    print("金线改连各星河最亮的主题星")

# ---------- 2) 前端：金/紫线更淡；选中不要那么亮；点星河推镜头 ----------
r = pathlib.Path(r"D:\qq-ai-bot\dashboard-web\src\features\memory-viz\StarfieldView\starfieldRenderer.ts")
t = r.read_text(encoding="utf-8")

t = t.replace("const crossLines = mkLines(crossPos, 0xb07bff, 0.6);",
              "const crossLines = mkLines(crossPos, 0xb07bff, 0.34);")
t = t.replace("const ownerLines = mkLines(ownerPos, 0xffc861, 0.72);",
              "const ownerLines = mkLines(ownerPos, 0xffc861, 0.40);")

# 高亮柔和一点：1.35 → 1.12，且不再额外加白
t = t.replace("const b = (0.55 + 0.45 * sf.brightness) * (hi ? 1.35 : 0.3);",
              "const b = (0.55 + 0.45 * sf.brightness) * (hi ? 1.12 : 0.3);")

# 增加 focusPoint（推镜头到任意坐标）
t = t.replace("""    focus(nodeId: string) {""",
"""    focusPoint(x: number, y: number, z: number) {
      const target = new THREE.Vector3(x * SPREAD, y * SPREAD, z * SPREAD);
      const dir = target.clone();
      if (dir.length() < 1e-4) dir.set(0.6, 0.4, 0.6);
      dir.normalize();
      const to = target.clone().add(dir.multiplyScalar(1.15));
      tween = { from: camera.position.clone(), to, t0: performance.now(), dur: 800 };
      lastInteraction = performance.now();
    },
    focus(nodeId: string) {""")

# 接口声明
t = t.replace("""export interface RendererHandles {
  dispose: () => void;
  focus: (nodeId: string) => void;""",
"""export interface RendererHandles {
  dispose: () => void;
  focus: (nodeId: string) => void;
  focusPoint: (x: number, y: number, z: number) => void;""")
r.write_text(t, encoding="utf-8")
print("渲染器：线条更淡 + focusPoint")

# ---------- 3) StarfieldView：点任意星 → 镜头推到它所在星河中心 ----------
v = pathlib.Path(r"D:\qq-ai-bot\dashboard-web\src\features\memory-viz\StarfieldView\StarfieldView.tsx")
u = v.read_text(encoding="utf-8")
u = u.replace("""      onPick: (n) => selectNode(n, 'star'),""",
"""      onPick: (n) => {
        selectNode(n, 'star');
        // 点到哪颗星，镜头就推到它所在星河的中心
        const same = visibleNodes.filter((x) => x.scope === n.scope && x.starfield);
        if (same.length) {
          const cx = same.reduce((s2, x) => s2 + (x.starfield!.x), 0) / same.length;
          const cy = same.reduce((s2, x) => s2 + (x.starfield!.y), 0) / same.length;
          const cz = same.reduce((s2, x) => s2 + (x.starfield!.z), 0) / same.length;
          handles.current?.focusPoint(cx, cy, cz);
        }
      },""")
v.write_text(u, encoding="utf-8")
print("点击星河自动推镜头")
