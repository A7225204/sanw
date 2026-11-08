/**
 * 白洞 · 新记忆诞生的奇点
 *
 * 白洞不是黑洞的反色版：黑洞是「往里吸 + 两极喷流」，
 * 白洞应该是「从里面往外炸」——所以这里没有任何喷流柱，只有：
 *   ① 刺眼的白色奇点（billboard：两级高斯核 + 缓慢旋转的星芒）
 *   ② 全向爆发：粒子朝球面各个方向抛出，带切向涡旋，越远越慢越淡
 *   ③ 冲击波环：一圈圈向外扩的细环，扩到边缘就消失
 */
import * as THREE from 'three';

const ADD_PREMULT = {
  transparent: true,
  depthWrite: false,
  depthTest: false,
  blending: THREE.CustomBlending,
  blendEquation: THREE.AddEquation,
  blendSrc: THREE.OneFactor,
  blendDst: THREE.OneFactor,
  blendSrcAlpha: THREE.ZeroFactor,
  blendDstAlpha: THREE.OneFactor,
} as const;

export interface WhiteHoleOptions {
  position: THREE.Vector3;
  /** 奇点视觉尺寸（世界单位），建议 0.09~0.14 */
  size?: number;
  /** 爆发半径：粒子能炸到多远 */
  jetLength?: number;
  /** 爆发扁度（0 = 完全球状，>0 压扁成盘），默认 0 */
  jetCone?: number;
  jetCount?: number;
}

export interface WhiteHole {
  group: THREE.Group;
  update: (t: number, dt?: number) => void;
  /** 新记忆诞生时闪一下 */
  flare: (strength?: number) => void;
  setViewport: (heightPx: number, fovDeg?: number, pixelRatio?: number) => void;
  dispose: () => void;
}

export function createWhiteHole(opts: WhiteHoleOptions): WhiteHole {
  const sizeBase = opts.size ?? 0.11;
  const burstR = opts.jetLength ?? 0.9;
  const flat = opts.jetCone ?? 0;
  const count = opts.jetCount ?? 3200;

  const group = new THREE.Group();
  group.position.copy(opts.position);

  /* ── ① 奇点 ───────────────────────────────────────────────── */
  const coreMat = new THREE.ShaderMaterial({
    uniforms: {
      uTime: { value: 0 },
      uSize: { value: sizeBase * 1.7 },
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

        float core = exp(-r * r * 120.0) * 3.2;   // 死白的核
        float halo = exp(-r * r * 7.0) * 0.75;

        // 缓慢旋转的星芒：白洞的"能量线"，不是黑洞的光子环
        float a = atan(vUv.y, vUv.x) + uTime * 0.6;
        vec2 p = vec2(cos(a), sin(a)) * r;
        float sx = exp(-abs(p.y) * 34.0) * exp(-p.x * p.x * 3.0);
        float sy = exp(-abs(p.x) * 34.0) * exp(-p.y * p.y * 3.0);
        float spikes = (sx + sy) * 0.55;

        float i = (core + halo + spikes) * uFlare;
        vec3 col = mix(vec3(1.0), vec3(0.72, 0.86, 1.0), smoothstep(0.0, 0.55, r));
        col = mix(col, vec3(0.35, 0.62, 1.0), smoothstep(0.45, 1.0, r) * 0.7);
        gl_FragColor = vec4(col * i, 1.0);
      }
    `,
    ...ADD_PREMULT,
  });
  const core = new THREE.Mesh(new THREE.PlaneGeometry(2, 2), coreMat);
  core.frustumCulled = false;
  core.renderOrder = 8;
  group.add(core);

  /* ── ② 全向爆发 ───────────────────────────────────────────── */
  const N = count;
  const aDir = new Float32Array(N * 3);
  const aSeed = new Float32Array(N);
  const aPhase = new Float32Array(N);
  const aSpeed = new Float32Array(N);
  const v = new THREE.Vector3();
  for (let i = 0; i < N; i++) {
    // 球面均匀采样 —— 白洞是全向的，不能只往两个方向甩
    const u = Math.random() * 2 - 1;
    const th = Math.random() * Math.PI * 2;
    const s = Math.sqrt(Math.max(0, 1 - u * u));
    v.set(s * Math.cos(th), u * (1 - flat), s * Math.sin(th)).normalize();
    aDir[i * 3] = v.x;
    aDir[i * 3 + 1] = v.y;
    aDir[i * 3 + 2] = v.z;
    aSeed[i] = Math.random();
    aPhase[i] = Math.random();
    aSpeed[i] = 0.13 + Math.random() * 0.15;
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.BufferAttribute(new Float32Array(N * 3), 3));
  geo.setAttribute('aDir', new THREE.BufferAttribute(aDir, 3));
  geo.setAttribute('aSeed', new THREE.BufferAttribute(aSeed, 1));
  geo.setAttribute('aPhase', new THREE.BufferAttribute(aPhase, 1));
  geo.setAttribute('aSpeed', new THREE.BufferAttribute(aSpeed, 1));

  const burstMat = new THREE.ShaderMaterial({
    uniforms: {
      uTime: { value: 0 },
      uScale: { value: 900 },
      uPt: { value: 0.017 },
      uR: { value: burstR },
      uSwirl: { value: 3.6 },
      uFlare: { value: 1 },
    },
    vertexShader: /* glsl */ `
      attribute vec3 aDir;
      attribute float aSeed, aPhase, aSpeed;
      uniform float uTime, uScale, uPt, uR, uSwirl, uFlare;
      varying float vLife, vAlpha;

      void main() {
        float life = fract(aPhase + uTime * aSpeed);   // 0→1：从奇点炸到边缘
        vLife = life;

        // pow(life,0.62)：刚炸出来飞快、远处减速 —— 爆发的惯性感
        float r = uR * pow(life, 0.62);
        vec3 p = aDir * r;

        // 切向涡旋：越往外转得越多，像被奇点甩出去的旋臂
        float ang = uSwirl * life * (0.5 + aSeed);
        float c = cos(ang), sn = sin(ang);
        p.xz = mat2(c, -sn, sn, c) * p.xz;

        // 一点点抖动，别让球壳太规整
        p += vec3(
          sin(uTime * 2.1 + aSeed * 41.0),
          cos(uTime * 1.7 + aSeed * 27.0),
          sin(uTime * 1.3 + aSeed * 19.0)
        ) * 0.02 * life;

        vec4 mv = modelViewMatrix * vec4(p, 1.0);
        gl_Position = projectionMatrix * mv;

        float fadeIn = smoothstep(0.0, 0.10, life);
        float fadeOut = 1.0 - smoothstep(0.62, 1.0, life);
        vAlpha = fadeIn * fadeOut * (0.45 + 0.55 * aSeed) * uFlare;

        float d = max(-mv.z, 0.001);
        gl_PointSize = clamp(uPt * (1.6 - 1.0 * life) * uScale / d, 1.0, 22.0);
      }
    `,
    fragmentShader: /* glsl */ `
      varying float vLife, vAlpha;
      void main() {
        vec2 q = gl_PointCoord * 2.0 - 1.0;
        float r2 = dot(q, q);
        if (r2 > 1.0) discard;
        float soft = exp(-r2 * 3.4);
        float hot = exp(-r2 * 16.0);

        // 近处白热 → 远处冷蓝（能量散掉的温度）
        vec3 nearC = vec3(1.0, 0.99, 0.96);
        vec3 farC = vec3(0.45, 0.70, 1.0);
        vec3 col = mix(nearC, farC, smoothstep(0.05, 0.85, vLife));
        col = mix(col, vec3(1.0), hot * 0.6);

        float a = vAlpha * (soft * 0.85 + hot * 0.5);
        if (a < 0.004) discard;
        gl_FragColor = vec4(col * a, a);
      }
    `,
    ...ADD_PREMULT,
  });
  const burst = new THREE.Points(geo, burstMat);
  burst.frustumCulled = false;
  burst.renderOrder = 6;
  group.add(burst);

  /* ── ③ 冲击波环（一圈圈往外扩）───────────────────────────── */
  const ringMat = new THREE.ShaderMaterial({
    uniforms: {
      uTime: { value: 0 },
      uSize: { value: burstR * 1.15 },
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
        float acc = 0.0;

        // 三个错相位的环：各自从中心扩到 r≈0.95 时消失
        for (int i = 0; i < 3; i++) {
          float ph = fract(uTime * 0.22 + float(i) * 0.3333);
          float rr = 0.06 + ph * 0.92;
          float d = (r - rr) * 90.0;
          float band = exp(-d * d);
          float fade = 1.0 - ph;
          acc += band * fade * fade * 0.5;
        }

        // 边界收干净，否则能看出正方形平面的硬边
        acc *= smoothstep(0.98, 0.55, r);
        gl_FragColor = vec4(vec3(0.62, 0.84, 1.0) * acc * uFlare, 1.0);
      }
    `,
    ...ADD_PREMULT,
  });
  const rings = new THREE.Mesh(new THREE.PlaneGeometry(2, 2), ringMat);
  rings.frustumCulled = false;
  rings.renderOrder = 5;
  group.add(rings);

  let flare = 1;
  let flareTarget = 1;

  return {
    group,
    update(t: number) {
      coreMat.uniforms.uTime.value = t;
      burstMat.uniforms.uTime.value = t;
      ringMat.uniforms.uTime.value = t;

      flare += (flareTarget - flare) * 0.08;
      if (Math.abs(flare - flareTarget) < 0.01) flareTarget = 1;
      coreMat.uniforms.uFlare.value = flare;
      burstMat.uniforms.uFlare.value = flare;
      ringMat.uniforms.uFlare.value = flare;

      // 奇点缓慢呼吸
      const breath = 1 + 0.06 * Math.sin(t * 2.2);
      coreMat.uniforms.uSize.value = sizeBase * 1.7 * breath;
    },
    flare(strength = 1.6) {
      flare = Math.max(flare, strength);
      flareTarget = 1;
    },
    setViewport(heightPx: number, fovDeg = 55, pixelRatio = 1) {
      burstMat.uniforms.uScale.value =
        (heightPx * pixelRatio) / (2 * Math.tan((fovDeg * Math.PI) / 360));
    },
    dispose() {
      geo.dispose();
      burstMat.dispose();
      core.geometry.dispose();
      coreMat.dispose();
      rings.geometry.dispose();
      ringMat.dispose();
    },
  };
}
