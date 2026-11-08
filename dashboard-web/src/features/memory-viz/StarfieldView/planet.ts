/**
 * 记忆星球 · 气态巨行星 / 能量星云
 *
 * 分两层画：
 *   本体   = 着色器球：域扭曲条带 + 高频能量纹 + 极向涡流 + 菲涅尔边缘（轮廓干净）
 *   大气壳 = 稍大一圈的加色球，只在边缘出光
 * 这两层是「不发霉」的关键：没有菲涅尔，球就永远糊成一团雾。
 *
 * 自带两个业务钩子：
 *   setPulse(v)         创伤星球红脉冲（挂皮质醇）
 *   ripple(dir, t, amp) 粒子砸中时的球面冲击波（按角距扩散、随时间衰减）
 */
import * as THREE from 'three';

const ADD_PREMULT = {
  transparent: true,
  depthWrite: false,
  depthTest: false,          // 大气壳/尘环是光，不该被球体切边
  blending: THREE.CustomBlending,
  blendEquation: THREE.AddEquation,
  blendSrc: THREE.OneFactor,
  blendDst: THREE.OneFactor,
  blendSrcAlpha: THREE.ZeroFactor,
  blendDstAlpha: THREE.OneFactor,
} as const;

/** iq 的 hash 值噪声：比 8 次 hash3 便宜一半 */
const NOISE = /* glsl */ `
  float hash(vec3 p) {
    p = fract(p * 0.3183099 + vec3(0.71, 0.113, 0.419));
    p *= 17.0;
    return fract(p.x * p.y * p.z * (p.x + p.y + p.z));
  }
  float vnoise(vec3 x) {
    vec3 i = floor(x), f = fract(x);
    f = f * f * (3.0 - 2.0 * f);
    return mix(
      mix(mix(hash(i + vec3(0.0, 0.0, 0.0)), hash(i + vec3(1.0, 0.0, 0.0)), f.x),
          mix(hash(i + vec3(0.0, 1.0, 0.0)), hash(i + vec3(1.0, 1.0, 0.0)), f.x), f.y),
      mix(mix(hash(i + vec3(0.0, 0.0, 1.0)), hash(i + vec3(1.0, 0.0, 1.0)), f.x),
          mix(hash(i + vec3(0.0, 1.0, 1.0)), hash(i + vec3(1.0, 1.0, 1.0)), f.x), f.y),
      f.z);
  }
  // 3 个八度就够：4 个八度在 15 颗球叠加时是纯 GPU 浪费
  float fbm(vec3 p) {
    float s = 0.0;
    s += 0.50 * vnoise(p);
    s += 0.25 * vnoise(p * 2.03 + vec3(1.7, 9.2, 4.3));
    s += 0.125 * vnoise(p * 4.10 + vec3(4.7, 2.3, 8.1));
    return s;
  }
`;

export interface PlanetOptions {
  radius: number;
  /** 暗部（极区 / 背光） */
  colorA: THREE.ColorRepresentation;
  /** 主条带 */
  colorB: THREE.ColorRepresentation;
  /** 能量纹 / 边缘光 */
  colorC: THREE.ColorRepresentation;
  bands?: number;
  turb?: number;
  speed?: number;
  fres?: number;
  /** 创伤星球：内部乱脉 + 红脉冲 */
  trauma?: boolean;
  /** 带一圈尘环 */
  ring?: boolean;
  /** 条带 / 自转轴，默认 (0,1,0) */
  axis?: THREE.Vector3;
  /** 自转速度（弧度/秒） */
  spin?: number;
}

export interface MemoryPlanet {
  group: THREE.Group;
  mesh: THREE.Mesh;
  update: (t: number, dt?: number) => void;
  /** 创伤红脉冲强度 0~1（挂皮质醇） */
  setPulse: (v: number) => void;
  /** 粒子砸中：dir 为球心指向撞击点的方向（世界坐标） */
  ripple: (dir: THREE.Vector3, t: number, amp?: number) => void;
  /** 换配色（本我恒星情绪基调切换用） */
  setColors: (
    a: THREE.ColorRepresentation,
    b: THREE.ColorRepresentation,
    c: THREE.ColorRepresentation,
  ) => void;
  /** 打光：dir 为 «从球心指向光源» 的世界方向（本我恒星） */
  setLight: (dir: THREE.Vector3) => void;
  /** 画质降级：小到看不清时关掉大气壳/尘环（省填充率） */
  setQuality: (full: boolean) => void;
  dispose: () => void;
}

export function createMemoryPlanet(opts: PlanetOptions): MemoryPlanet {
  const {
    radius,
    bands = 7,
    turb = 1.0,
    speed = 1.0,
    fres = 0.9,
    trauma = false,
    ring = false,
    spin = 0.06,
  } = opts;

  const group = new THREE.Group();
  const colorA = new THREE.Color(opts.colorA);
  const colorB = new THREE.Color(opts.colorB);
  const colorC = new THREE.Color(opts.colorC);

  const uniforms = {
    uTime: { value: 0 },
    uBands: { value: bands },
    uTurb: { value: turb },
    uSpeed: { value: speed },
    uFres: { value: fres },
    uAxis: { value: (opts.axis ?? new THREE.Vector3(0, 1, 0)).clone().normalize() },
    uColorA: { value: colorA.clone() },
    uColorB: { value: colorB.clone() },
    uColorC: { value: colorC.clone() },
    uLight: { value: new THREE.Vector3(0.42, 0.46, 0.78).normalize() },
    uLightStrength: { value: 1 },
    uPulse: { value: 0 },
    uPulseColor: { value: new THREE.Color(0xff2d3a) },
    uRippleDir: { value: new THREE.Vector3(0, 1, 0) },
    uRippleT: { value: -1e3 },
    uRippleAmp: { value: 0 },
    uRippleColor: { value: new THREE.Color(0xdff0ff) },
  };

  /* ── 本体 ─────────────────────────────────────────────────── */
  const bodyMat = new THREE.ShaderMaterial({
    uniforms,
    vertexShader: /* glsl */ `
      varying vec3 vN, vV, vObj;
      void main() {
        vObj = position;
        vN = normalize(mat3(modelMatrix) * normal);
        vec4 wp = modelMatrix * vec4(position, 1.0);
        vV = normalize(cameraPosition - wp.xyz);
        gl_Position = projectionMatrix * viewMatrix * wp;
      }
    `,
    fragmentShader: /* glsl */ `
      uniform float uTime, uBands, uTurb, uSpeed, uFres, uPulse, uRippleT, uRippleAmp, uLightStrength;
      uniform vec3 uAxis, uColorA, uColorB, uColorC, uPulseColor, uRippleDir, uRippleColor, uLight;
      varying vec3 vN, vV, vObj;
      ${NOISE}
      void main() {
        vec3 n = normalize(vN);
        vec3 v = normalize(vV);
        float lat = dot(n, normalize(uAxis));
        float t = uTime * uSpeed;

        // ① 域扭曲条带：两层（粗带定结构、细带碎边），warp 让带子互相拉扯
        float warp = fbm(vObj * 2.3 + vec3(t * 0.35, t * 0.10, -t * 0.20));
        float latW = lat * uBands + warp * uTurb * 3.0;
        float b1 = sin(latW);
        float b2 = sin(latW * 2.7 + 1.7);
        float bandMix = 0.5 + 0.5 * b1;

        // ② 高频能量纹（内部能量流动）
        float veins = fbm(vObj * 7.0 + vec3(-t * 0.5, t * 0.28, t * 0.40));
        float vein = smoothstep(0.45, 0.92, veins);

        vec3 col = mix(uColorA, uColorB, bandMix);
        col = mix(col, uColorC, smoothstep(0.72, 1.0, 0.5 + 0.5 * b2) * 0.30);
        col = mix(col, uColorC, vein * 0.35);

        // ③ 极向涡流：能量沿经线被甩出去
        float pole = smoothstep(0.62, 1.0, abs(lat));
        col += uColorC * pole * 0.12 * (0.6 + 0.4 * sin(atan(n.z, n.x) * 4.0 + t * 2.0));

        // ④ 假平行光：没有这一步，球永远是一块平的色斑
        float ndl = dot(n, normalize(uLight));
        float shade = mix(1.0, 0.42 + 0.58 * smoothstep(-0.6, 0.95, ndl), uLightStrength);
        col *= shade;

        // ⑤ 菲涅尔边缘：只加不混，混会把整颗球的颜色扒掉
        float fres = pow(1.0 - clamp(dot(n, v), 0.0, 1.0), 3.0);
        col += uColorC * fres * uFres * 0.45;

        // ⑤ 创伤：高频乱脉 + 边缘泛红
        if (uPulse > 0.001) {
          float chaos = fbm(vObj * 6.5 + vec3(0.0, t * 1.4, 0.0));
          float bite = smoothstep(0.58, 0.98, chaos) * uPulse;
          col = mix(col, uPulseColor, bite * 0.55);
          col += uPulseColor * fres * uPulse * 0.55;
        }

        // ⑥ 冲击波：按角距扩散的环 + 撞击点短暂爆闪
        float age = uTime - uRippleT;
        if (age > 0.0 && uRippleAmp > 0.001) {
          float ang = acos(clamp(dot(n, normalize(uRippleDir)), -1.0, 1.0));
          float front = age * 2.2;
          float dq = (ang - front) * 7.0;   // 不用 pow：底数会变负
          float wave = exp(-dq * dq) * exp(-age * 2.4);
          float splash = exp(-ang * ang * 22.0) * exp(-age * 7.0);
          col += uRippleColor * (wave * 1.30 + splash * 0.90) * uRippleAmp;
        }

        gl_FragColor = vec4(col, 1.0);
      }
    `,
  });

  const bodyGeo = new THREE.SphereGeometry(1, 64, 48);
  const mesh = new THREE.Mesh(bodyGeo, bodyMat);
  mesh.scale.setScalar(radius);
  group.add(mesh);

  /* ── 大气壳：只在边缘出光 ─────────────────────────────────── */
  const atmoMat = new THREE.ShaderMaterial({
    uniforms: {
      uColor: { value: colorC.clone() },
      uPower: { value: 3.6 },
      uStrength: { value: 0.8 },
    },
    vertexShader: /* glsl */ `
      varying vec3 vN, vV;
      void main() {
        vN = normalize(mat3(modelMatrix) * normal);
        vec4 wp = modelMatrix * vec4(position, 1.0);
        vV = normalize(cameraPosition - wp.xyz);
        gl_Position = projectionMatrix * viewMatrix * wp;
      }
    `,
    fragmentShader: /* glsl */ `
      uniform vec3 uColor; uniform float uPower, uStrength;
      varying vec3 vN, vV;
      void main() {
        float f = pow(1.0 - clamp(dot(normalize(vN), normalize(vV)), 0.0, 1.0), uPower);
        gl_FragColor = vec4(uColor * f * uStrength, 1.0);
      }
    `,
    ...ADD_PREMULT,
  });
  const atmoGeo = new THREE.SphereGeometry(1, 64, 48);
  const atmo = new THREE.Mesh(atmoGeo, atmoMat);
  atmo.scale.setScalar(radius * 1.075);
  group.add(atmo);

  /* ── 可选尘环 ─────────────────────────────────────────────── */
  let ringMesh: THREE.Mesh | null = null;
  let ringGeo: THREE.BufferGeometry | null = null;
  let ringMat: THREE.ShaderMaterial | null = null;
  if (ring) {
    ringGeo = new THREE.RingGeometry(1.22, 1.78, 160, 1);
    ringMat = new THREE.ShaderMaterial({
      uniforms: {
        uTime: { value: 0 },
        uColorA: { value: colorB.clone() },
        uColorB: { value: colorC.clone() },
      },
      vertexShader: /* glsl */ `
        varying vec2 vUv; varying float vR;
        void main() {
          vUv = uv;
          vR = length(position.xy);
          gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
        }
      `,
      fragmentShader: /* glsl */ `
        uniform float uTime; uniform vec3 uColorA, uColorB;
        varying vec2 vUv; varying float vR;
        ${NOISE}
        void main() {
          float r = clamp((vR - 1.22) / 0.56, 0.0, 1.0);
          // 用「半径 + 角度」取噪声：沿环走一圈才是细纹，用 uv 会糊成一片烟
          float ang = atan(vUv.y - 0.5, vUv.x - 0.5);
          float n = fbm(vec3(r * 9.0, ang * 1.6, uTime * 0.10));
          float stri = 0.72 + 0.28 * sin(r * 42.0 + n * 11.0);  // 同心细条（别太规则，否则像黑胶）
          float dens = smoothstep(0.30, 0.80, n) * stri
                     * smoothstep(0.0, 0.16, r)
                     * (1.0 - smoothstep(0.86, 1.0, r));
          gl_FragColor = vec4(mix(uColorA, uColorB, r) * dens * 0.28, 1.0);
        }
      `,
      ...ADD_PREMULT,
      side: THREE.DoubleSide,
    });
    ringMesh = new THREE.Mesh(ringGeo, ringMat);
    ringMesh.scale.setScalar(radius);
    ringMesh.rotation.set(Math.PI * 0.34, 0, Math.PI * 0.07); // 斜扣 + 一点点自转平面偏角
    group.add(ringMesh);
  }

  let spinAcc = 0;
  let pulseOverride = 0;   // 激素/外部驱动；创伤星球另有一层自己的心跳

  return {
    group,
    mesh,
    update(t: number, dt = 0.016) {
      uniforms.uTime.value = t;
      if (ringMat) ringMat.uniforms.uTime.value = t;
      spinAcc += spin * dt;
      mesh.rotation.y = spinAcc;

      // 创伤星球的心跳：双峰（扑通-扑通），没有激素数据时也活着
      const beat = trauma
        ? 0.30 * Math.pow(0.5 + 0.5 * Math.sin(t * 3.1), 3) +
          0.16 * Math.pow(0.5 + 0.5 * Math.sin(t * 3.1 + 1.05), 3)
        : 0;
      uniforms.uPulse.value = Math.min(1, Math.max(pulseOverride, beat));
    },
    setPulse(v: number) {
      pulseOverride = Math.max(0, Math.min(1, v));
    },
    setLight(dir: THREE.Vector3) {
      uniforms.uLight.value.copy(dir).normalize();
    },
    setQuality(full: boolean) {
      atmo.visible = full;
      if (ringMesh) ringMesh.visible = full;
    },
    ripple(dir: THREE.Vector3, t: number, amp = 1) {
      uniforms.uRippleDir.value.copy(dir).normalize();
      uniforms.uRippleT.value = t;
      uniforms.uRippleAmp.value = amp;
    },
    setColors(a, b, c) {
      uniforms.uColorA.value.set(a);
      uniforms.uColorB.value.set(b);
      uniforms.uColorC.value.set(c);
      atmoMat.uniforms.uColor.value.set(c);
      if (ringMat) {
        (ringMat.uniforms.uColorA.value as THREE.Color).set(b);
        (ringMat.uniforms.uColorB.value as THREE.Color).set(c);
      }
    },
    dispose() {
      bodyGeo.dispose();
      bodyMat.dispose();
      atmoGeo.dispose();
      atmoMat.dispose();
      ringGeo?.dispose();
      if (ringMat) ringMat.dispose();
    },
  };
}

/* ──────────────────────────────────────────────────────────────
 * 布局工具：把星球「拉开」，这是不发霉的另一半原因
 * 三类各占一圈、同类均分角度、纵向错开
 * ────────────────────────────────────────────────────────────── */
export type PlanetKind = 'long' | 'trauma' | 'event';

export interface PlanetSlot {
  id: string;
  kind: PlanetKind;
  pos: THREE.Vector3;
}

export function layoutPlanets(
  items: { id: string; kind: PlanetKind }[],
  radius: Record<PlanetKind, number> = { long: 1.05, event: 0.8, trauma: 1.32 },
): PlanetSlot[] {
  const byKind: Record<string, { id: string; kind: PlanetKind }[]> = {};
  for (const it of items) {
    (byKind[it.kind] ??= []).push(it);
  }

  const phaseOf: Record<PlanetKind, number> = { long: 0, event: Math.PI, trauma: Math.PI * 0.25 };

  const out: PlanetSlot[] = [];
  for (const kind of Object.keys(byKind) as PlanetKind[]) {
    const list = byKind[kind];
    const R = radius[kind] ?? 1;
    list.forEach((it, i) => {
      const a = phaseOf[kind] + (i / Math.max(1, list.length)) * Math.PI * 2;
      // 纵向错开：避免所有球排在同一个水平面上（看起来像一条线）
      const y = Math.sin(i * 2.399) * 0.5 * 0.32;
      out.push({ id: it.id, kind, pos: new THREE.Vector3(Math.cos(a) * R, y, Math.sin(a) * R) });
    });
  }
  return out;
}
