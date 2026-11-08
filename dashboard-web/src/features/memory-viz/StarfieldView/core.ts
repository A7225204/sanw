/**
 * 核心球体（人格主球 / 短期记忆球）
 *
 * 两层：
 *   本体 = 着色器球：域扭曲的等离子表面 + 菲涅尔边缘（比纯色球多一层"内部在动"）
 *   光冠 = billboard：径向衰减 + 角向噪声做成的火焰状射线，永远面向相机
 *
 * 两个显式接口：
 *   pulse()  新记忆诞生 / 被点中时闪一下
 *   setMood() 换主色（人格球颜色随情绪基调走）
 */
import * as THREE from 'three';

const ADD_PREMULT = {
  transparent: true,
  depthWrite: false,
  blending: THREE.CustomBlending,
  blendEquation: THREE.AddEquation,
  blendSrc: THREE.OneFactor,
  blendDst: THREE.OneFactor,
  blendSrcAlpha: THREE.ZeroFactor,
  blendDstAlpha: THREE.OneFactor,
} as const;

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
  float fbm(vec3 p) {
    float s = 0.0;
    s += 0.50 * vnoise(p);
    s += 0.25 * vnoise(p * 2.03 + vec3(1.7, 9.2, 4.3));
    s += 0.125 * vnoise(p * 4.10 + vec3(4.7, 2.3, 8.1));
    return s;
  }
`;

export interface CoreOptions {
  radius: number;
  /** 暗部（球体阴影侧） */
  colorA: THREE.ColorRepresentation;
  /** 主色 */
  colorB: THREE.ColorRepresentation;
  /** 高光 / 边缘 / 光冠 */
  colorC: THREE.ColorRepresentation;
  /** 光冠相对球半径的倍数，默认 3.2 */
  corona?: number;
  /** 表面流动速度 */
  flow?: number;
  /** 自转速度 */
  spin?: number;
  /** 点的位置 */
  position?: THREE.Vector3;
}

export interface Core {
  group: THREE.Group;
  update: (t: number, dt?: number) => void;
  /** 闪一下：新记忆诞生 / 被点中 */
  pulse: (strength?: number) => void;
  /** 换主色（情绪基调） */
  setMood: (a: THREE.ColorRepresentation, b: THREE.ColorRepresentation, c: THREE.ColorRepresentation) => void;
  dispose: () => void;
}

export function createCore(opts: CoreOptions): Core {
  const R = opts.radius;
  const coronaK = opts.corona ?? 3.2;
  const flowSpeed = opts.flow ?? 1;
  const spinSpeed = opts.spin ?? 0.12;

  const group = new THREE.Group();
  if (opts.position) group.position.copy(opts.position);

  const uA = new THREE.Color(opts.colorA);
  const uB = new THREE.Color(opts.colorB);
  const uC = new THREE.Color(opts.colorC);

  const uniforms = {
    uTime: { value: 0 },
    uPulse: { value: 0 },
    uFlow: { value: flowSpeed },
    uA: { value: uA.clone() },
    uB: { value: uB.clone() },
    uC: { value: uC.clone() },
  };

  /* ── 本体 ─────────────────────────────────────────────────── */
  const bodyGeo = new THREE.SphereGeometry(1, 64, 48);
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
      uniform float uTime, uPulse, uFlow;
      uniform vec3 uA, uB, uC;
      varying vec3 vN, vV, vObj;
      ${NOISE}
      void main() {
        vec3 n = normalize(vN);
        vec3 v = normalize(vV);
        float t = uTime * uFlow;

        // 域扭曲：先算一层低频 warp，再用它去扭高频，表面才像"在翻滚"
        float warp = fbm(vObj * 2.4 + vec3(0.0, t * 0.30, t * 0.16));
        float flow = fbm(vObj * 5.2 + vec3(warp * 1.6, t * 0.42, -t * 0.30));
        float heat = smoothstep(0.22, 0.92, flow);

        vec3 col = mix(uA, uB, smoothstep(0.12, 0.72, heat));
        col = mix(col, uC, smoothstep(0.55, 1.0, heat) * 0.9);
        col *= 1.22;                     // 整体提亮：这是颗恒星，不是石头

        // 极区更暗一点，球体才有体积感
        col *= 0.82 + 0.18 * smoothstep(-0.9, 0.9, n.y);

        // 边缘：菲涅尔提亮 —— 这一圈是"发光恒星"的关键
        float fres = pow(1.0 - clamp(dot(n, v), 0.0, 1.0), 2.6);
        col += uC * fres * 1.15;

        col *= 1.0 + uPulse * 0.75;
        gl_FragColor = vec4(col, 1.0);
      }
    `,
  });
  const body = new THREE.Mesh(bodyGeo, bodyMat);
  body.scale.setScalar(R);
  group.add(body);

  /* ── 光冠：billboard 火焰射线 ─────────────────────────────── */
  const coronaMat = new THREE.ShaderMaterial({
    uniforms: {
      uTime: { value: 0 },
      uPulse: { value: 0 },
      uC: { value: uC.clone() },
      uCold: { value: uC.clone().multiplyScalar(0.45) },   // 外圈用亮色的暗调，不能用暗部色（会糊成脏盘）
      uSize: { value: R * coronaK },
    },
    vertexShader: /* glsl */ `
      uniform float uSize;
      varying vec2 vUv;
      void main() {
        vec4 mv = modelViewMatrix * vec4(0.0, 0.0, 0.0, 1.0);
        mv.xy += position.xy * uSize;
        vUv = position.xy;
        gl_Position = projectionMatrix * mv;
      }
    `,
    fragmentShader: /* glsl */ `
      uniform float uTime, uPulse;
      uniform vec3 uC, uCold;
      varying vec2 vUv;
      void main() {
        float r = length(vUv);
        if (r > 1.0) discard;
        float ang = atan(vUv.y, vUv.x);

        // 角向噪声：让光晕像火苗而不是一圈规整的渐变
        float r1 = 0.5 + 0.5 * sin(ang * 6.0 + uTime * 0.85);
        float r2 = 0.5 + 0.5 * sin(ang * 11.0 - uTime * 1.25 + 1.7);
        float rays = 0.42 + 0.58 * pow(r1 * r2, 0.7);

        float core = exp(-r * r * 8.0);
        float mid = exp(-r * r * 2.8) * 0.55;
        float outer = exp(-r * r * 0.95) * 0.20;

        float i = (core + (mid + outer) * mix(1.0, rays, 0.6)) * (1.0 + uPulse * 1.3);
        // 关键：平面是方的，到 r=1 必须干净地衰减到 0，否则会看到一个圆盘硬边
        i *= smoothstep(1.0, 0.45, r);
        vec3 col = mix(uCold, uC, clamp(core * 1.6 + 0.25, 0.0, 1.0));
        gl_FragColor = vec4(col * i, 1.0);
      }
    `,
    ...ADD_PREMULT,
  });
  const corona = new THREE.Mesh(new THREE.PlaneGeometry(2, 2), coronaMat);
  corona.frustumCulled = false;
  corona.renderOrder = 1;
  group.add(corona);

  let pulse = 0;
  let spinAcc = 0;

  return {
    group,
    update(t: number, dt = 0.016) {
      spinAcc += spinSpeed * dt;
      body.rotation.y = spinAcc;

      uniforms.uTime.value = t;
      coronaMat.uniforms.uTime.value = t;

      // 脉冲回落（指数衰减，别用线性，否则像开关）
      pulse *= Math.pow(0.12, dt);
      if (pulse < 0.002) pulse = 0;
      uniforms.uPulse.value = pulse;
      coronaMat.uniforms.uPulse.value = pulse;
    },
    pulse(strength = 1) {
      pulse = Math.max(pulse, strength);
    },
    setMood(a, b, c) {
      uA.set(a);
      uB.set(b);
      uC.set(c);
      uniforms.uA.value.copy(uA);
      uniforms.uB.value.copy(uB);
      uniforms.uC.value.copy(uC);
      coronaMat.uniforms.uC.value.copy(uC);
      coronaMat.uniforms.uCold.value.copy(uC).multiplyScalar(0.45);
    },
    dispose() {
      bodyGeo.dispose();
      bodyMat.dispose();
      corona.geometry.dispose();
      coronaMat.dispose();
    },
  };
}
