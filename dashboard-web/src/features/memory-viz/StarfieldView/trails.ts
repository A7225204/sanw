/**
 * 记忆粒子的拖尾层
 *
 * 后端每次轮询才给一次快照，粒子会「瞬移」。所以这里做两件事：
 *   ① 平滑：把目标位置做阻尼跟随 —— 粒子是滑过去的，路径自然带弧度（贝塞尔感）
 *   ② 拖尾：每颗粒子留 N 个历史采样，画成一条首亮尾淡的线
 *
 * 一条 LineSegments 画完，加色预乘，不参与深度写入。
 */
import * as THREE from 'three';

const ADD_PREMULT = {
  transparent: true,
  depthWrite: false,
  depthTest: false,          // 拖尾是光，不该被行星/球体切断（会看成虚线）
  blending: THREE.CustomBlending,
  blendEquation: THREE.AddEquation,
  blendSrc: THREE.OneFactor,
  blendDst: THREE.OneFactor,
  blendSrcAlpha: THREE.ZeroFactor,
  blendDstAlpha: THREE.OneFactor,
} as const;

export interface TrailLayer {
  /** 要 add 到场景里的线对象 */
  object: THREE.LineSegments;
  /** 每帧对每颗粒子调用一次：喂平滑后的位置和颜色 */
  push: (i: number, x: number, y: number, z: number, color: THREE.Color, alpha: number) => void;
  /** 一帧写完调用：提交缓冲 */
  flush: (count: number) => void;
  /** 某个槽换了身份（粒子消失后复用了下标）——清掉它的历史，别拖出一条跨场景的线 */
  reset: (i: number) => void;
  /** 这个槽这帧不画：只把透明度清零，历史保留（重见时光滑接上） */
  hide: (i: number) => void;
  dispose: () => void;
}

export function createTrailLayer(max: number, segs = 6): TrailLayer {
  const verts = max * segs * 2; // 每条 trail 有 segs 段线
  const pos = new Float32Array(verts * 3);
  const col = new Float32Array(verts * 3);
  const alp = new Float32Array(verts);
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  geo.setAttribute('aColor', new THREE.BufferAttribute(col, 3));
  geo.setAttribute('aAlpha', new THREE.BufferAttribute(alp, 1));
  geo.setDrawRange(0, 0);

  const mat = new THREE.ShaderMaterial({
    uniforms: {},
    vertexShader: /* glsl */ `
      attribute vec3 aColor;
      attribute float aAlpha;
      varying vec3 vC;
      varying float vA;
      void main() {
        vC = aColor;
        vA = aAlpha;
        gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
      }
    `,
    fragmentShader: /* glsl */ `
      varying vec3 vC;
      varying float vA;
      void main() {
        if (vA <= 0.001) discard;
        gl_FragColor = vec4(vC * vA, vA);
      }
    `,
    ...ADD_PREMULT,
  });

  const lines = new THREE.LineSegments(geo, mat);
  lines.frustumCulled = false;

  // 每颗粒子的历史（环形）
  const hist = new Float32Array(max * segs * 3);
  const head = new Int32Array(max).fill(-1);

  return {
    object: lines,
    push(i, x, y, z, color, alpha) {
      const h = head[i];
      const nh = h < 0 ? 0 : (h + 1) % segs;
      head[i] = nh;
      const base = (i * segs + nh) * 3;
      hist[base] = x;
      hist[base + 1] = y;
      hist[base + 2] = z;

      if (h < 0) return; // 第一帧没有历史，不画

      // 从最新往回连：越旧越淡、越细
      for (let k = 0; k < segs - 1; k++) {
        const a = (nh - k + segs) % segs;
        const b = (nh - k - 1 + segs) % segs;
        const ai = (i * segs + k) * 2;
        const bi = ai + 1;
        const pa = (i * segs + a) * 3;
        const pb = (i * segs + b) * 3;
        pos[ai * 3] = hist[pa];
        pos[ai * 3 + 1] = hist[pa + 1];
        pos[ai * 3 + 2] = hist[pa + 2];
        pos[bi * 3] = hist[pb];
        pos[bi * 3 + 1] = hist[pb + 1];
        pos[bi * 3 + 2] = hist[pb + 2];

        // 头部亮、尾部暗；尾端再额外压一点，形成「加速甩尾」
        const f = 1 - k / (segs - 1);
        const aHead = alpha * Math.pow(f, 1.4) * 0.9;
        const aTail = alpha * Math.pow(1 - (k + 1) / (segs - 1), 1.4) * 0.9;
        for (const [vi, av] of [
          [ai, aHead],
          [bi, aTail],
        ] as const) {
          col[vi * 3] = color.r;
          col[vi * 3 + 1] = color.g;
          col[vi * 3 + 2] = color.b;
          alp[vi] = av;
        }
      }
    },
    hide(i: number) {
      const base = i * segs * 2;
      for (let k = 0; k < segs * 2; k++) alp[base + k] = 0;
    },
    reset(i: number) {
      head[i] = -1;
      const base = i * segs * 2;
      for (let k = 0; k < segs * 2; k++) alp[base + k] = 0;
    },
    flush(count: number) {
      geo.setDrawRange(0, Math.max(0, count) * segs * 2);
      geo.attributes.position.needsUpdate = true;
      geo.attributes.aColor.needsUpdate = true;
      geo.attributes.aAlpha.needsUpdate = true;
    },
    dispose() {
      geo.dispose();
      mat.dispose();
    },
  };
}
