import pathlib

r = pathlib.Path(r"D:\qq-ai-bot\dashboard-web\src\features\memory-viz\StarfieldView\starfieldRenderer.ts")
t = r.read_text(encoding="utf-8")

# 1) tween 同时插值"相机位置 + 旋转中心"
t = t.replace(
"""  let tween: { from: THREE.Vector3; to: THREE.Vector3; t0: number; dur: number } | null = null;""",
"""  let tween: {
    fromCam: THREE.Vector3; toCam: THREE.Vector3;
    fromTarget: THREE.Vector3; toTarget: THREE.Vector3;
    t0: number; dur: number;
  } | null = null;""")

t = t.replace(
"""    if (tween) {
      const k = Math.min(1, (now - tween.t0) / tween.dur);
      const e = 1 - Math.pow(1 - k, 3);
      camera.position.lerpVectors(tween.from, tween.to, e);
      camera.lookAt(0, 0, 0);
      if (k >= 1) tween = null;
    }""",
"""    if (tween) {
      const k = Math.min(1, (now - tween.t0) / tween.dur);
      const e = 1 - Math.pow(1 - k, 3);
      camera.position.lerpVectors(tween.fromCam, tween.toCam, e);
      controls.target.lerpVectors(tween.fromTarget, tween.toTarget, e);
      if (k >= 1) tween = null;
    }""")

# 2) focusPoint：把旋转轴心也移过去
t = t.replace(
"""    focusPoint(x: number, y: number, z: number) {
      const target = new THREE.Vector3(x * SPREAD, y * SPREAD, z * SPREAD);
      const dir = target.clone();
      if (dir.length() < 1e-4) dir.set(0.6, 0.4, 0.6);
      dir.normalize();
      const to = target.clone().add(dir.multiplyScalar(1.15));
      tween = { from: camera.position.clone(), to, t0: performance.now(), dur: 800 };
      lastInteraction = performance.now();
    },""",
"""    focusPoint(x: number, y: number, z: number) {
      const target = new THREE.Vector3(x * SPREAD, y * SPREAD, z * SPREAD);
      const dir = target.clone();
      if (dir.length() < 1e-4) dir.set(0.6, 0.4, 0.6);
      dir.normalize();
      const toCam = target.clone().add(dir.multiplyScalar(1.15));
      tween = {
        fromCam: camera.position.clone(), toCam,
        fromTarget: controls.target.clone(), toTarget: target.clone(),
        t0: performance.now(), dur: 800,
      };
      lastInteraction = performance.now();
    },""")

# 3) focus(nodeId)：以这颗粒子为轴心旋转
t = t.replace(
"""    focus(nodeId: string) {
      const idx = indexOf.get(nodeId);
      if (idx === undefined) return;
      const sf = nodes[idx].starfield!;
      const target = new THREE.Vector3(sf.x * SPREAD, sf.y * SPREAD, sf.z * SPREAD);
      const dir = target.clone().normalize();
      const to = target.clone().add(dir.multiplyScalar(0.9));
      tween = { from: camera.position.clone(), to, t0: performance.now(), dur: 700 };
      lastInteraction = performance.now();
    },""",
"""    focus(nodeId: string) {
      const idx = indexOf.get(nodeId);
      if (idx === undefined) return;
      const sf = nodes[idx].starfield!;
      const target = new THREE.Vector3(sf.x * SPREAD, sf.y * SPREAD, sf.z * SPREAD);
      // 相机离目标 0.9，方向沿用当前视角，避免瞬移感
      const dir = camera.position.clone().sub(controls.target).normalize();
      if (!Number.isFinite(dir.x) || dir.length() < 1e-4) dir.set(0.6, 0.4, 0.6).normalize();
      const toCam = target.clone().add(dir.multiplyScalar(0.9));
      tween = {
        fromCam: camera.position.clone(), toCam,
        fromTarget: controls.target.clone(), toTarget: target.clone(),
        t0: performance.now(), dur: 700,
      };
      lastInteraction = performance.now();
    },""")

r.write_text(t, encoding="utf-8")
print("相机：tween 同时移动位置与旋转轴心（controls.target）")

# 4) 点粒子 → 以该粒子为轴心（不再用星河中心）
v = pathlib.Path(r"D:\qq-ai-bot\dashboard-web\src\features\memory-viz\StarfieldView\StarfieldView.tsx")
u = v.read_text(encoding="utf-8")
old = u[u.find("      onPick: (n) => {"):u.find("      onHover: (n) => {")]
new = """      onPick: (n) => {
        selectNode(n, 'star');
        // 以点中的这颗粒子为轴心：相机围绕它旋转
        handles.current?.focus(n.id);
      },
"""
if old:
    u = u.replace(old, new, 1)
    v.write_text(u, encoding="utf-8")
    print("点击粒子 → 以该粒子为旋转轴心")
