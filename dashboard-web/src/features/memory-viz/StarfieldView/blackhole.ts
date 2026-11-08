/**
 * 黑洞 · 遗忘
 *
 * 三层，全部 GPU 驱动（每帧只改一个 uTime）：
 *   ① 视界：纯黑球，不反光、不吃雾，把后面的东西彻底挡住
 *   ② 吸积盘：Points 着色器，差速旋转 + 多普勒增亮（朝镜头那一侧更亮）
 *   ③ 光子环：面向相机的 billboard，极细的主环 + 上下两道透镜弧
 *
 * 没有位图贴图，所以拉近不会糊成一个甜甜圈。
 */
import * as THREE from 'three';

const ADD_PREMULT = {
  transparent: true,
  depthWrite: false,
  depthTest: false,          // 盘和环是光；只有视界球保留深度
  blending: THREE.CustomBlending,
  blendEquation: THREE.AddEquation,
  blendSrc: THREE.OneFactor,
  blendDst: THREE.OneFactor,
  blendSrcAlpha: THREE.ZeroFactor,
  blendDstAlpha: THREE.OneFactor,
} as const;

export interface BlackHoleOptions {
  position: THREE.Vector3;
  /** 视界半径（世界单位），建议 0.10~0.15 */
  radius?: number;
  /** 吸积盘外径（相对视界半径的倍数） */
  diskOuter?: number;
  /** 吸积盘粒子数 */
  count?: number;
}

export interface BlackHole {
  group: THREE.Group;
  update: (t: number, dt?: number) => void;
  /** 吞掉一条记忆时闪一下 */
  pulse: (strength?: number) => void;
  setViewport: (heightPx: number, fovDeg?: number, pixelRatio?: number) => void;
  dispose: () => void;
}

export function createBlackHole(opts: BlackHoleOptions): BlackHole {
  const R = opts.radius ?? 0.13;
  const outerK = opts.diskOuter ?? 4.0;
  const COUNT = opts.count ?? 4200;

  const group = new THREE.Group();
  group.position.copy(opts.position);

  /* ── ① 视界 ─────────────────────────────────────────────── */
  const horizonGeo = new THREE.SphereGeometry(1, 40, 32);
  const horizonMat = new THREE.MeshBasicMaterial({ color: 0x000000, fog: false });
  const horizon = new THREE.Mesh(horizonGeo, horizonMat);
  horizon.scale.setScalar(R);
  horizon.renderOrder = 2;
  group.add(horizon);

  /* ── ② 吸积盘 ───────────────────────────────────────────── */
  const aR = new Float32Array(COUNT); // 轨道半径
  const aA = new Float32Array(COUNT); // 初始角度
  const aW = new Float32Array(COUNT); // 角速度
  const aH = new Float32Array(COUNT); // 盘厚抖动
  const aSeed = new Float32Array(COUNT);
  const rIn = R * 1.15;
  const rOut = R * outerK;
  for (let i = 0; i < COUNT; i++) {
    // 向内更密：吸积盘内侧应该亮成一圈
    const u = Math.pow(Math.random(), 1.7);
    aR[i] = rIn + u * (rOut - rIn);
    aA[i] = Math.random() * Math.PI * 2;
    // 开普勒差速 ω ∝ r^-1.5：内圈飞快
    aW[i] = (0.9 * Math.pow(rIn, 1.5)) / Math.pow(aR[i], 1.5);
    aH[i] = (Math.random() - 0.5) * R * 0.16 * (0.3 + aR[i] / rOut);
    aSeed[i] = Math.random();
  }
  const diskGeo = new THREE.BufferGeometry();
  diskGeo.setAttribute('position', new THREE.BufferAttribute(new Float32Array(COUNT * 3), 3));
  diskGeo.setAttribute('aR', new THREE.BufferAttribute(aR, 1));
  diskGeo.setAttribute('aA', new THREE.BufferAttribute(aA, 1));
  diskGeo.setAttribute('aW', new THREE.BufferAttribute(aW, 1));
  diskGeo.setAttribute('aH', new THREE.BufferAttribute(aH, 1));
  diskGeo.setAttribute('aSeed', new THREE.BufferAttribute(aSeed, 1));

  const diskMat = new THREE.ShaderMaterial({
    uniforms: {
      uTime: { value: 0 },
      uScale: { value: 900 },
      uPt: { value: 0.024 },
      uRIn: { value: rIn },
      uROut: { value: rOut },
      uFlare: { value: 1 },
    },
    vertexShader: /* glsl */ `
      attribute float aR, aA, aW, aH, aSeed;
      uniform float uTime, uScale, uPt, uRIn, uROut;
      varying float vT, vA;

      void main() {
        float a = aA + uTime * aW;
        vec3 p = vec3(cos(a) * aR, aH, sin(a) * aR);
        vec4 mv = modelViewMatrix * vec4(p, 1.0);
        gl_Position = projectionMatrix * mv;

        float t = clamp((aR - uRIn) / (uROut - uRIn), 0.0, 1.0);
        vT = t;

        // 多普勒增亮：切向速度 (-sin a, 0, cos a) 与视线 (0,0,1) 的点积 = cos a
        float dop = pow(clamp(0.45 + 0.55 * cos(a), 0.0, 1.0), 2.0);
        // 内圈更热更亮；边缘渐隐进黑暗
        vA = (0.30 + 1.35 * dop) * (1.0 - t * 0.55) * (0.75 + 0.45 * aSeed);

        float d = max(-mv.z, 0.001);
        gl_PointSize = clamp(uPt * (1.8 - 1.0 * t) * uScale / d, 1.2, 22.0);
      }
    `,
    fragmentShader: /* glsl */ `
      uniform float uFlare;
      varying float vT, vA;
      void main() {
        vec2 q = gl_PointCoord * 2.0 - 1.0;
        float r2 = dot(q, q);
        if (r2 > 1.0) discard;
        float soft = exp(-r2 * 3.0);
        float hot = exp(-r2 * 14.0);

        // 内：刺眼白蓝 → 中：橙黄 → 外：暗红
        vec3 inner = vec3(0.92, 0.96, 1.0);
        vec3 mid   = vec3(1.0, 0.62, 0.22);
        vec3 outer = vec3(0.42, 0.10, 0.03);
        vec3 col = vT < 0.5
          ? mix(inner, mid, vT * 2.0)
          : mix(mid, outer, (vT - 0.5) * 2.0);

        float a = vA * (soft * 0.85 + hot * 0.55) * uFlare;
        if (a < 0.004) discard;
        gl_FragColor = vec4(col * a, a);
      }
    `,
    ...ADD_PREMULT,
  });
  const disk = new THREE.Points(diskGeo, diskMat);
  disk.frustumCulled = false;
  disk.renderOrder = 3;
  group.add(disk);

  /* ── ③ 光子环：billboard，主环 + 上下透镜弧 ────────────────── */
  const ringMat = new THREE.ShaderMaterial({
    uniforms: {
      uTime: { value: 0 },
      uSize: { value: R * 1.95 },
      uFlare: { value: 1 },
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
      uniform float uTime, uFlare;
      varying vec2 vUv;
      void main() {
        float r = length(vUv);
        if (r > 1.0) discard;
        float phi = atan(vUv.y, vUv.x);

        // 主环：紧贴视界边缘（0.56 * uSize ≈ 1.09 R）的一圈极细亮线
        float d1 = (r - 0.56) * 120.0;
        float ring = exp(-d1 * d1) * 0.85;

        // 上下两道弧：引力把盘背面弯到上下方，多普勒让一侧更亮
        float d2 = (r - 0.60) * 70.0;
        float arcMask = pow(abs(sin(phi)), 7.0);
        float asym = 0.55 + 0.45 * (0.5 + 0.5 * cos(phi - 0.5));
        float arcs = exp(-d2 * d2) * arcMask * 0.95 * asym;

        // 环外一圈极淡的散射光，避免环"贴在黑球上"
        float d3 = (r - 0.56) * 9.0;
        float halo = exp(-d3 * d3) * 0.14;

        float i = (ring + arcs + halo) * uFlare;
        vec3 warm = vec3(1.0, 0.80, 0.46);
        vec3 cool = vec3(1.0, 0.97, 0.90);
        vec3 col = mix(warm, cool, clamp(ring / 0.85, 0.0, 1.0));
        gl_FragColor = vec4(col * i, 1.0);
      }
    `,
    ...ADD_PREMULT,
  });
  const ring = new THREE.Mesh(new THREE.PlaneGeometry(2, 2), ringMat);
  ring.frustumCulled = false;
  ring.renderOrder = 4;
  group.add(ring);

  let flare = 1;
  let flareTarget = 1;

  return {
    group,
    update(t: number) {
      diskMat.uniforms.uTime.value = t;
      ringMat.uniforms.uTime.value = t;

      flare += (flareTarget - flare) * 0.08;
      if (Math.abs(flare - flareTarget) < 0.01) flareTarget = 1;
      diskMat.uniforms.uFlare.value = flare;
      // 光子环呼吸：极缓慢，黑洞不该"跳"
      ringMat.uniforms.uFlare.value = flare * (1 + 0.04 * Math.sin(t * 1.1));
    },
    pulse(strength = 1.5) {
      flare = Math.max(flare, strength);
      flareTarget = 1;
    },
    setViewport(heightPx: number, fovDeg = 55, pixelRatio = 1) {
      const k = (heightPx * pixelRatio) / (2 * Math.tan((fovDeg * Math.PI) / 360));
      diskMat.uniforms.uScale.value = k;
    },
    dispose() {
      horizonGeo.dispose();
      horizonMat.dispose();
      diskGeo.dispose();
      diskMat.dispose();
      ring.geometry.dispose();
      ringMat.dispose();
    },
  };
}
