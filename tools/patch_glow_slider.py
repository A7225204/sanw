import pathlib

root = pathlib.Path(r"D:\qq-ai-bot\dashboard-web\src\features\memory-viz")

# ---------- 1) store：加 glow ----------
p = root / "store/vizStore.ts"
s = p.read_text(encoding="utf-8")
if "glow" not in s:
    s = s.replace("  focusRequest: { nodeId: string; from: 'tree' | 'star' } | null;",
                  "  focusRequest: { nodeId: string; from: 'tree' | 'star' } | null;\n"
                  "  glow: number; // 0~100 粒子发光度")
    s = s.replace("  requestFocus: (nodeId: string, from: 'tree' | 'star') => void;",
                  "  setGlow: (v: number) => void;\n"
                  "  requestFocus: (nodeId: string, from: 'tree' | 'star') => void;")
    s = s.replace("  focusRequest: null,\n  collapsedTopics: [],",
                  "  focusRequest: null,\n  glow: 60,\n  collapsedTopics: [],")
    s = s.replace("  requestFocus: (nodeId, from) => set({ focusRequest: { nodeId, from } }),",
                  "  setGlow: (v) => set({ glow: Math.max(0, Math.min(100, v)) }),\n"
                  "  requestFocus: (nodeId, from) => set({ focusRequest: { nodeId, from } }),")
    p.write_text(s, encoding="utf-8")
    print("store: 已加 glow")

# ---------- 2) 渲染器：uGlow uniform + setGlow ----------
r = root / "StarfieldView/starfieldRenderer.ts"
t = r.read_text(encoding="utf-8")

t = t.replace("""  resize: () => void;
}""", """  resize: () => void;
  setGlow: (v01: number) => void;
}""", 1)

t = t.replace("""      uTex: { value: makeGlowTexture() },
      uTime: { value: 0 },""",
"""      uTex: { value: makeGlowTexture() },
      uTime: { value: 0 },
      uGlow: { value: 0.6 },""")

t = t.replace("""      uniform sampler2D uTex;
      varying vec3 vColor;
      varying vec3 vCore;
      void main() {
        vec2 uv = gl_PointCoord - 0.5;
        float d = length(uv) * 2.0;
        if (d > 1.0) discard;
        float glowMask = exp(-d * d * 3.2);
        float coreMask = smoothstep(0.26, 0.0, d);
        vec3 col = vColor * glowMask * 0.85 + vCore * coreMask;
        float a = max(glowMask * 0.85, coreMask);
        if (a < 0.02) discard;
        gl_FragColor = vec4(col, a);
      }""",
"""      uniform sampler2D uTex;
      uniform float uGlow;
      varying vec3 vColor;
      varying vec3 vCore;
      void main() {
        vec2 uv = gl_PointCoord - 0.5;
        float d = length(uv) * 2.0;
        if (d > 1.0) discard;
        // uGlow 0~1：同时放大光晕范围和亮度；0 时只剩一点点核
        float soft = 2.4 + (1.0 - uGlow) * 3.2;
        float glowMask = exp(-d * d * soft) * (0.25 + uGlow * 1.5);
        float coreMask = smoothstep(0.26, 0.0, d) * (0.55 + uGlow * 0.75);
        vec3 col = vColor * glowMask + vCore * coreMask;
        float a = max(glowMask, coreMask);
        if (a < 0.02) discard;
        gl_FragColor = vec4(col, a);
      }""")

t = t.replace("""    resize,
    focusPoint(""",
"""    resize,
    setGlow(v01: number) {
      nodeMat.uniforms.uGlow.value = Math.max(0, Math.min(1, v01));
    },
    focusPoint(""")
r.write_text(t, encoding="utf-8")
print("渲染器: uGlow + setGlow")

# ---------- 3) StarfieldView 读 store 并下发 ----------
v = root / "StarfieldView/StarfieldView.tsx"
u = v.read_text(encoding="utf-8")
u = u.replace("  const { keyword, filters, selectedTopicId, focusRequest, clearFocus, selectedNodeId } =\n    useVizStore();",
              "  const { keyword, filters, selectedTopicId, focusRequest, clearFocus, selectedNodeId, glow } =\n    useVizStore();")
if "handles.current?.setGlow" not in u:
    u = u.replace("""  useEffect(() => {
    if (focusRequest?.from === 'tree') {""",
"""  useEffect(() => {
    handles.current?.setGlow(glow / 100);
  }, [glow, nodes]);

  useEffect(() => {
    if (focusRequest?.from === 'tree') {""")
v.write_text(u, encoding="utf-8")
print("StarfieldView: 跟随 glow")

# ---------- 4) 顶层面板加滑杆 ----------
m = root / "MemoryVizPage.tsx"
p2 = m.read_text(encoding="utf-8")
p2 = p2.replace("  const { scope, setScope, keyword, setKeyword, filters, setFilters, selectedTopicId, setTopic } =\n    useVizStore();",
                "  const {\n    scope, setScope, keyword, setKeyword, filters, setFilters, selectedTopicId, setTopic, glow, setGlow,\n  } = useVizStore();")
if "发光" not in p2:
    p2 = p2.replace("""        <label style={miniLabel}>
          最低保留度""",
"""        <label style={miniLabel}>
          发光 {glow}
          <input
            type="range"
            min={0}
            max={100}
            value={glow}
            onChange={(e) => setGlow(Number(e.target.value))}
            style={{ width: 90 }}
          />
        </label>

        <label style={miniLabel}>
          最低保留度""")
    p2 = p2.replace("""  const { scope, setScope, keyword, setKeyword, filters, setFilters, selectedTopicId, setTopic } =
    useVizStore();""",
"""  const {
    scope, setScope, keyword, setKeyword, filters, setFilters, selectedTopicId, setTopic, glow, setGlow,
  } = useVizStore();""")
m.write_text(p2, encoding="utf-8")
print("顶层面板: 发光滑杆")
