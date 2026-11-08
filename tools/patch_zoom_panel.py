import pathlib

root = pathlib.Path(r"D:\qq-ai-bot\dashboard-web\src\features\memory-viz")

# ---------- 1) store：glow 默认 4，新增 zoom ----------
p = root / "store/vizStore.ts"
s = p.read_text(encoding="utf-8")
s = s.replace("  glow: number; // 0~100 粒子发光度",
              "  glow: number; // 0~100 粒子发光度\n  zoom: number; // 0~100 镜头远近（100=最近）")
s = s.replace("  setGlow: (v: number) => void;",
              "  setGlow: (v: number) => void;\n  setZoom: (v: number) => void;")
s = s.replace("  glow: 60,", "  glow: 4,\n  zoom: 50,")
s = s.replace("  setGlow: (v) => set({ glow: Math.max(0, Math.min(100, v)) }),",
              "  setGlow: (v) => set({ glow: Math.max(0, Math.min(100, v)) }),\n"
              "  setZoom: (v) => set({ zoom: Math.max(0, Math.min(100, v)) }),")
p.write_text(s, encoding="utf-8")
print("store: glow=4 / zoom")

# ---------- 2) 渲染器：可拉到很近 + setZoom ----------
r = root / "StarfieldView/starfieldRenderer.ts"
t = r.read_text(encoding="utf-8")
t = t.replace("  controls.minDistance = 0.5;\n  controls.maxDistance = 14;",
              "  controls.minDistance = 0.04;\n  controls.maxDistance = 24;")
t = t.replace("  resize: () => void;\n  setGlow: (v01: number) => void;",
              "  resize: () => void;\n  setGlow: (v01: number) => void;\n  setZoom: (v01: number) => void;")
t = t.replace("""    setGlow(v01: number) {
      nodeMat.uniforms.uGlow.value = Math.max(0, Math.min(1, v01));
    },""",
"""    setGlow(v01: number) {
      nodeMat.uniforms.uGlow.value = Math.max(0, Math.min(1, v01));
    },
    setZoom(v01: number) {
      // 0 → 24（看全），1 → 0.05（贴脸）；指数分布，手感均匀
      const lo = 0.05;
      const hi = 24;
      const dist = lo * Math.pow(hi / lo, 1 - Math.max(0, Math.min(1, v01)));
      const dir = camera.position.clone().sub(controls.target);
      if (dir.length() < 1e-6) dir.set(0.6, 0.4, 0.6);
      dir.normalize();
      camera.position.copy(controls.target.clone().add(dir.multiplyScalar(dist)));
      controls.update();
      lastInteraction = performance.now();
    },""")
r.write_text(t, encoding="utf-8")
print("渲染器: 最近 0.04 / setZoom")

# ---------- 3) StarfieldView 下发 zoom ----------
v = root / "StarfieldView/StarfieldView.tsx"
u = v.read_text(encoding="utf-8")
u = u.replace("selectedNodeId, glow } =\n    useVizStore();", "selectedNodeId, glow, zoom } =\n    useVizStore();")
if "setZoom(zoom / 100)" not in u:
    u = u.replace("""  useEffect(() => {
    handles.current?.setGlow(glow / 100);
  }, [glow, nodes]);""",
"""  useEffect(() => {
    handles.current?.setGlow(glow / 100);
  }, [glow, nodes]);

  useEffect(() => {
    handles.current?.setZoom(zoom / 100);
  }, [zoom, nodes]);""")
v.write_text(u, encoding="utf-8")
print("StarfieldView: 跟随 zoom")

# ---------- 4) 顶层面板：加缩放滑杆 ----------
m = root / "MemoryVizPage.tsx"
q = m.read_text(encoding="utf-8")
q = q.replace("selectedTopicId, setTopic, glow, setGlow,\n  } = useVizStore();",
              "selectedTopicId, setTopic, glow, setGlow, zoom, setZoom,\n  } = useVizStore();")
if "缩放" not in q:
    q = q.replace("""        <label style={miniLabel}>
          最低保留度""",
"""        <label style={miniLabel}>
          缩放 {zoom}
          <input
            type="range"
            min={0}
            max={100}
            value={zoom}
            onChange={(e) => setZoom(Number(e.target.value))}
            style={{ width: 90 }}
          />
        </label>

        <label style={miniLabel}>
          最低保留度""")
m.write_text(q, encoding="utf-8")
print("顶层面板: 缩放滑杆")
