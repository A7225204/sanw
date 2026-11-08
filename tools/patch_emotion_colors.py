import pathlib

root = pathlib.Path(r"D:\qq-ai-bot\dashboard-web\src\features\memory-viz")

# 1) types：加 emotion
p = root / "types.ts"
s = p.read_text(encoding="utf-8")
if "emotion?" not in s:
    s = s.replace("  sentiment?: number; // -1 负面 ~ 1 正面",
                  "  sentiment?: number; // -1 负面 ~ 1 正面\n  emotion?: string;   // 怒/累/丧/烦/害怕/正面/中性")
    p.write_text(s, encoding="utf-8")
    print("types: 加 emotion")

# 2) 渲染器：核色 + 光晕色分离（丧=黑核白光）
r = root / "StarfieldView/starfieldRenderer.ts"
t = r.read_text(encoding="utf-8")

t = t.replace(
"""  const positions = new Float32Array(nodes.length * 3);
  const colors = new Float32Array(nodes.length * 3);
  const sizes = new Float32Array(nodes.length);""",
"""  const positions = new Float32Array(nodes.length * 3);
  const colors = new Float32Array(nodes.length * 3);   // 光晕颜色
  const cores = new Float32Array(nodes.length * 3);    // 内核颜色
  const sizes = new Float32Array(nodes.length);""")

t = t.replace(
"""    // 白核 + 光晕：正面偏蓝、负面偏红、中性冷白
    const senti = Number(n.sentiment ?? 0);
    const glow = senti > 0.15 ? 0x4fa8ff : senti < -0.15 ? 0xff4d4d : 0xbfe0ff;
    color.set(glow);
    const b = 0.55 + 0.45 * sf.brightness;
    colors[i * 3] = Math.min(1, color.r * b + 0.35);
    colors[i * 3 + 1] = Math.min(1, color.g * b + 0.35);
    colors[i * 3 + 2] = Math.min(1, color.b * b + 0.35);""",
"""    const { glow, core } = emotionColors(n.emotion);
    const b = 0.55 + 0.45 * sf.brightness;
    color.set(glow);
    colors[i * 3] = Math.min(1, color.r * b);
    colors[i * 3 + 1] = Math.min(1, color.g * b);
    colors[i * 3 + 2] = Math.min(1, color.b * b);
    const c2 = new THREE.Color(core);
    cores[i * 3] = c2.r;
    cores[i * 3 + 1] = c2.g;
    cores[i * 3 + 2] = c2.b;""")

t = t.replace(
"""  nodeGeo.setAttribute('color', new THREE.BufferAttribute(colors, 3));
  nodeGeo.setAttribute('aSize', new THREE.BufferAttribute(sizes, 1));""",
"""  nodeGeo.setAttribute('color', new THREE.BufferAttribute(colors, 3));
  nodeGeo.setAttribute('aCore', new THREE.BufferAttribute(cores, 3));
  nodeGeo.setAttribute('aSize', new THREE.BufferAttribute(sizes, 1));""")

t = t.replace(
"""      attribute float aSize;
      attribute vec3 color;
      uniform float uTime;
      varying vec3 vColor;
      void main() {
        vColor = color;""",
"""      attribute float aSize;
      attribute vec3 color;
      attribute vec3 aCore;
      uniform float uTime;
      varying vec3 vColor;
      varying vec3 vCore;
      void main() {
        vColor = color;
        vCore = aCore;""")

t = t.replace(
"""    fragmentShader: `
      uniform sampler2D uTex;
      varying vec3 vColor;
      void main() {
        vec4 t = texture2D(uTex, gl_PointCoord);
        if (t.a < 0.02) discard;
        gl_FragColor = vec4(vColor * t.rgb, t.a);
      }
    `,""",
"""    fragmentShader: `
      uniform sampler2D uTex;
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
      }
    `,""")

# 2b) 情绪 → 颜色表
t = t.replace("const SPREAD = 1.8;", """const SPREAD = 1.8;

/** 情绪 → (光晕色, 内核色)
 *  怒=红 / 累=紫 / 丧=黑核白光 / 害怕=黄 / 烦=橙红 / 正面=蓝 / 中性=冷白
 */
export function emotionColors(emotion?: string): { glow: number; core: number } {
  switch (emotion) {
    case '怒':
      return { glow: 0xff3b30, core: 0xffd9d6 };
    case '累':
      return { glow: 0xa874ff, core: 0xefe6ff };
    case '丧':
      return { glow: 0xffffff, core: 0x0a0a0a };   // 黑核 + 白光
    case '害怕':
      return { glow: 0xffd429, core: 0xfff7d6 };
    case '烦':
      return { glow: 0xff7a3d, core: 0xffe3d2 };
    case '正面':
      return { glow: 0x4fa8ff, core: 0xffffff };
    default:
      return { glow: 0xbfe0ff, core: 0xffffff };
  }
}""")

# 高亮分支也按 emotion 取色
t = t.replace("""        nodes.forEach((n, i) => {
          const sf = n.starfield!;
          const senti = Number(n.sentiment ?? 0);
          const glow = senti > 0.15 ? 0x4fa8ff : senti < -0.15 ? 0xff4d4d : 0xbfe0ff;
          color.set(glow);
          const b = 0.55 + 0.45 * sf.brightness;
          colors[i * 3] = Math.min(1, color.r * b + 0.35);
          colors[i * 3 + 1] = Math.min(1, color.g * b + 0.35);
          colors[i * 3 + 2] = Math.min(1, color.b * b + 0.35);
          sizes[i] = 0.016 + 0.045 * sf.size;
        });""",
"""        nodes.forEach((n, i) => {
          const sf = n.starfield!;
          const { glow } = emotionColors(n.emotion);
          color.set(glow);
          const b = 0.55 + 0.45 * sf.brightness;
          colors[i * 3] = Math.min(1, color.r * b);
          colors[i * 3 + 1] = Math.min(1, color.g * b);
          colors[i * 3 + 2] = Math.min(1, color.b * b);
          sizes[i] = 0.016 + 0.045 * sf.size;
        });""")

t = t.replace("""          const hi = n.id === nodeId || ids.has(n.id);
          const sf = n.starfield!;
          const senti = Number(n.sentiment ?? 0);
          const glow = senti > 0.15 ? 0x4fa8ff : senti < -0.15 ? 0xff4d4d : 0xbfe0ff;
          color.set(glow);
          const b = (0.55 + 0.45 * sf.brightness) * (hi ? 1.35 : 0.3);
          colors[i * 3] = Math.min(1, color.r * b + (hi ? 0.35 : 0.05));
          colors[i * 3 + 1] = Math.min(1, color.g * b + (hi ? 0.35 : 0.05));
          colors[i * 3 + 2] = Math.min(1, color.b * b + (hi ? 0.35 : 0.05));""",
"""          const hi = n.id === nodeId || ids.has(n.id);
          const sf = n.starfield!;
          const { glow } = emotionColors(n.emotion);
          color.set(glow);
          const b = (0.55 + 0.45 * sf.brightness) * (hi ? 1.35 : 0.3);
          colors[i * 3] = Math.min(1, color.r * b);
          colors[i * 3 + 1] = Math.min(1, color.g * b);
          colors[i * 3 + 2] = Math.min(1, color.b * b);""")

r.write_text(t, encoding="utf-8")
print("渲染器：核色/光晕分离 + 情绪色表")
