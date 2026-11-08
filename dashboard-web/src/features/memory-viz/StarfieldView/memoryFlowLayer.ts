/**
 * 记忆流视图（人格视图默认层）
 *
 * 层级关系（本我 → 人格特质 → 底层记忆）：
 *   ① 本我（persona:me）：绝对中心的恒星，半径最大、光晕最亮
 *   ② 人格进化球（trait / rule / growth / defense）：每颗沿**带倾角的轨道**绕本我公转，
 *      同类行星变多时轨道半径自动外扩 → 自然长成多层星环
 *   ③ 底层记忆（长期 / 事件 / 创伤）：不再是孤立的外圈，而是被行星引力捕获的**星尘**——
 *      长期记忆绕特质球、事件绕准则球、创伤绕防御球（伴星）；全部用 THREE.Points 画（LOD）
 *
 * 粒子状态：BORN（白洞喷出）→ WANDER（短期球附近）→ FLOW（分流）→ ARRIVED（被行星捕获）
 */
import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { createCore } from './core';
import { createWhiteHole } from './whitehole';
import { createBlackHole } from './blackhole';
import { createMemoryPlanet, type MemoryPlanet } from './planet';
import { createLabelLayer, type LabelItem } from './labels';

export interface PersonaPlanet {
  kind: string;
  /** 后端直接给的详情 id（成长词条用 growth:<id>）；没有就退回 soul:<kind>:<idx> */
  id?: string;
  idxInKind?: number;
  appearAt?: number;
  text: string;
  pos: { x: number; y: number; z: number };
  color: number;
  size: number;
}

export interface FlowParticle {
  id: string;
  label?: string;
  rid: number;
  dest: 'longterm' | 'trauma' | 'episode' | 'blackhole';
  state: 'BORN' | 'WANDER' | 'FLOW' | 'ARRIVED';
  life: number;
  ageH: number;
  pos: { x: number; y: number; z: number };
  size: number;
  bright: number;
  /** 长期记忆的情绪极性：pos 正面 / neg 负面 / neu 中性 */
  tone?: 'pos' | 'neg' | 'neu';
}

// ── 场景常量（白洞 / 短期球 / 黑洞的位置必须和后端 memory_flow 一致）──
const SPREAD = 1;
const WH_POS = new THREE.Vector3(-11.0 * SPREAD, 0.02 * SPREAD, 0);
const BLACKHOLE_POS = new THREE.Vector3(10.2 * SPREAD, 0.02 * SPREAD, 0);
const SHORT_POS = new THREE.Vector3(0, 2.35, 0);
const SHORT_COLOR = new THREE.Color(0xdfefff);
const DEST_COLOR: Record<string, number> = {
  longterm: 0x7ac8ff,
  trauma: 0xff3b30,
  episode: 0x3ddc84,
  blackhole: 0x8b1a1a,
};
// 长期记忆按情绪极性分色（和树+星海一致：正面绿 / 负面黄 / 中性冷白）
const TONE_COLOR: Record<string, number> = { pos: 0x3ddc84, neg: 0xffcc33, neu: 0xbfe0ff };
const BIRTH_COLOR = new THREE.Color(0x4a9eff);

// 行星轨道：半径 / 倾角 / 公转角速度（rad/s）。同类行星变多时半径自动外扩。
// 半径 / 倾角 / 公转角速度（rad/s）/ 星尘自转速率 / 行星自转
// 内圈快、外圈慢：记忆蓝球（内圈特质）转得最快，事件绿球（准则圈）次之
const PLANET_RING: Record<string, {
  radius: number; tilt: number; speed: number; dustSpin: number; bodySpin: number;
}> = {
  id: { radius: 0.95, tilt: 0.3, speed: 0.09, dustSpin: 0.52, bodySpin: 0.20 }, // 本我词条：绕恒星
  ego: { radius: 0.55, tilt: -0.52, speed: -0.06, dustSpin: 0.34, bodySpin: 0.12 }, // 自我词条
  superego: { radius: 0.5, tilt: 0.74, speed: 0.045, dustSpin: 0.24, bodySpin: 0.07 }, // 超我词条
  growth: { radius: 1.35, tilt: -0.92, speed: -0.03, dustSpin: 0.18, bodySpin: 0.04 }, // 人格成长
};

const PLANET_PRESET: Record<string, { bands: number; turb: number; ring: boolean; spin: number }> = {
  id: { bands: 6, turb: 1.0, ring: false, spin: 0.1 },
  ego: { bands: 4, turb: 0.55, ring: false, spin: 0.07 },
  superego: { bands: 9, turb: 1.9, ring: true, spin: 0.16 },
  growth: { bands: 5, turb: 0.75, ring: false, spin: 0.09 },
};

function makeGlowTexture(size = 64): THREE.Texture {
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = size;
  const ctx = canvas.getContext('2d')!;
  const g = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
  g.addColorStop(0, 'rgba(255,255,255,1)');
  g.addColorStop(0.35, 'rgba(255,255,255,0.65)');
  g.addColorStop(1, 'rgba(255,255,255,0)');
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, size, size);
  const tex = new THREE.CanvasTexture(canvas);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

function hash01(k: string): number {
  let h = 2166136261;
  for (let i = 0; i < k.length; i++) {
    h ^= k.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return (h >>> 0) / 4294967296;
}

export function createMemoryFlowLayer(opts: { container: HTMLElement; onPickNode?: (id: string) => void }) {
  const { container, onPickNode } = opts;

  const scene = new THREE.Scene();
  scene.fog = new THREE.FogExp2(0x03060d, 0.035);
  const camera = new THREE.PerspectiveCamera(55, 1, 0.01, 100);
  camera.position.set(5.5, 4.2, 6.2);
  camera.lookAt(0, 0, 0);

  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setClearColor(0x03060d, 1);
  renderer.domElement.style.position = 'absolute';
  renderer.domElement.style.inset = '0';
  renderer.domElement.style.pointerEvents = 'auto';
  container.appendChild(renderer.domElement);

  // ── 粒子池（星尘）：全部用 THREE.Points 画，数量再多也只是一片点云 ──
  const MAX = 1400;
  const positions = new Float32Array(MAX * 3).fill(9999);
  const colors = new Float32Array(MAX * 3);
  const sizes = new Float32Array(MAX).fill(0.02);
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
  geo.setAttribute('aColor', new THREE.BufferAttribute(colors, 3));
  geo.setAttribute('aSize', new THREE.BufferAttribute(sizes, 1));

  const mat = new THREE.ShaderMaterial({
    uniforms: {
      uTex: { value: makeGlowTexture() },
      uTime: { value: 0 },
      uGlow: { value: 0.42 },
      uDrift: { value: 0.4 },
    },
    vertexShader: `
      attribute float aSize;
      attribute vec3 aColor;
      uniform float uTime;
      uniform float uDrift;
      varying vec3 vColor;
      void main() {
        vColor = aColor;
        vec3 drift = uDrift * vec3(
          sin(uTime * 0.45 + position.y * 2.7) * 0.012,
          cos(uTime * 0.37 + position.x * 2.3) * 0.012,
          sin(uTime * 0.41 + position.z * 2.1) * 0.010
        );
        vec4 mv = modelViewMatrix * vec4(position + drift, 1.0);
        gl_PointSize = aSize * (300.0 / max(0.001, -mv.z));
        gl_Position = projectionMatrix * mv;
      }
    `,
    fragmentShader: `
      uniform sampler2D uTex;
      uniform float uGlow;
      varying vec3 vColor;
      void main() {
        float a = texture2D(uTex, gl_PointCoord).a;
        if (a < 0.02) discard;
        vec3 col = vColor * (0.55 + uGlow * 1.2) + vec3(1.0) * pow(a, 6.0) * 0.5;
        gl_FragColor = vec4(col, a);
      }
    `,
    transparent: true,
    depthWrite: false,
    depthTest: false,
    blending: THREE.AdditiveBlending,
  });

  const points = new THREE.Points(geo, mat);
  points.frustumCulled = false;
  scene.add(points);

  // ── 白洞（新记忆的诞生点）──
  const whiteHole = createWhiteHole({
    position: WH_POS,
    size: 0.34,
    jetLength: 1.8,
    jetCount: 2600,
  });
  whiteHole.setViewport(container.clientHeight || 520, 55, window.devicePixelRatio || 1);
  scene.add(whiteHole.group);

  // ── 短期记忆球 ──
  const shortCore = createCore({
    radius: 0.16,
    colorA: '#2a5a9e',
    colorB: '#8fc8ff',
    colorC: '#eaf7ff',
    corona: 3.6,
    flow: 1.3,
    spin: 0.2,
    position: SHORT_POS,
  });
  scene.add(shortCore.group);

  // ── 黑洞（遗忘）──
  const blackHole = createBlackHole({
    position: BLACKHOLE_POS,
    radius: 0.42,
    diskOuter: 5.6,
  });
  blackHole.setViewport(container.clientHeight || 520, 55, window.devicePixelRatio || 1);
  scene.add(blackHole.group);

  // ── 背景星空（移植自树+星海：两层星点铺底，慢慢自转）──
  const bgStars = new THREE.Group();
  {
    const tex = makeGlowTexture();
    const farGeo = new THREE.BufferGeometry();
    const N_FAR = 3200;
    const farPos = new Float32Array(N_FAR * 3);
    const farCol = new Float32Array(N_FAR * 3);
    const c = new THREE.Color();
    for (let i = 0; i < N_FAR; i++) {
      const r = 25 + Math.random() * 30;
      const u = Math.random() * 2 - 1;
      const th = Math.random() * Math.PI * 2;
      const sp = Math.sqrt(Math.max(0, 1 - u * u));
      farPos[i * 3] = r * sp * Math.cos(th);
      farPos[i * 3 + 1] = r * u * 0.75;
      farPos[i * 3 + 2] = r * sp * Math.sin(th);
      const t = Math.random();
      c.setRGB(0.72 + t * 0.28, 0.80 + t * 0.18, 1.0);
      if (Math.random() < 0.08) c.setRGB(1.0, 0.92, 0.78);
      const b = 0.25 + Math.random() * 0.55;
      farCol[i * 3] = c.r * b; farCol[i * 3 + 1] = c.g * b; farCol[i * 3 + 2] = c.b * b;
    }
    farGeo.setAttribute('position', new THREE.BufferAttribute(farPos, 3));
    farGeo.setAttribute('color', new THREE.BufferAttribute(farCol, 3));
    bgStars.add(new THREE.Points(farGeo, new THREE.PointsMaterial({
      size: 3.2, vertexColors: true, transparent: true, opacity: 1.0,
      blending: THREE.AdditiveBlending, depthWrite: false, map: tex,
      sizeAttenuation: false,
      fog: false,          // 背景星在雾外，别被 FogExp2 吃掉
    })));

    const nearGeo = new THREE.BufferGeometry();
    const N_NEAR = 160;
    const nearPos = new Float32Array(N_NEAR * 3);
    const nearCol = new Float32Array(N_NEAR * 3);
    for (let i = 0; i < N_NEAR; i++) {
      const r = 22 + Math.random() * 28;
      const u = Math.random() * 2 - 1;
      const th = Math.random() * Math.PI * 2;
      const sp = Math.sqrt(Math.max(0, 1 - u * u));
      nearPos[i * 3] = r * sp * Math.cos(th);
      nearPos[i * 3 + 1] = r * u * 0.8;
      nearPos[i * 3 + 2] = r * sp * Math.sin(th);
      c.setRGB(1.0, 0.96, 0.88);
      if (Math.random() < 0.25) c.setRGB(0.78, 0.88, 1.0);
      nearCol[i * 3] = c.r; nearCol[i * 3 + 1] = c.g; nearCol[i * 3 + 2] = c.b;
    }
    nearGeo.setAttribute('position', new THREE.BufferAttribute(nearPos, 3));
    nearGeo.setAttribute('color', new THREE.BufferAttribute(nearCol, 3));
    bgStars.add(new THREE.Points(nearGeo, new THREE.PointsMaterial({
      size: 5.5, vertexColors: true, transparent: true, opacity: 1.0,
      blending: THREE.AdditiveBlending, depthWrite: false, map: tex,
      sizeAttenuation: false,
      fog: false,
    })));
  }
  scene.add(bgStars);

  // ── 本我：绝对中心恒星（金色岩浆，只自转不公转）──
  const idCore = createCore({
    radius: 0.55,
    colorA: '#a35f08',
    colorB: '#ffc94a',
    colorC: '#fff0c0',
    corona: 3.0,
    flow: 1.0,
    spin: 0.2,
  });
  scene.add(idCore.group);

  // ── 自我 / 超我：第一级行星，绕本我公转（不同半径 + Y 轴倾角）──
  const egoCore = createCore({
    radius: 0.28,
    colorA: '#0a2a4a',
    colorB: '#2a9ad0',
    colorC: '#b0f0ff',
    corona: 1.9,
    flow: 0.95,
    spin: 0.18,
  });
  scene.add(egoCore.group);
  const superegoCore = createCore({
    radius: 0.26,
    colorA: '#8a7a3a',
    colorB: '#fff6d8',
    colorC: '#ffffff',
    corona: 1.9,
    flow: 0.7,
    spin: 0.1,
  });
  scene.add(superegoCore.group);

  // 第一级公转：自我 / 超我 绕本我（不同半径、不同倾角）
  const TRIAD_ORBIT: Record<string, { r: number; tilt: number; phase: number; speed: number }> = {
    id: { r: 0, tilt: 0, phase: 0, speed: 0 },
    ego: { r: 2.2, tilt: 0.35, phase: 0, speed: 0.07 },
    superego: { r: 3.05, tilt: -0.5, phase: Math.PI, speed: 0.045 },
  };
  function triadPos(kind: string, t: number, out: THREE.Vector3 = new THREE.Vector3()): THREE.Vector3 {
    const o = TRIAD_ORBIT[kind] ?? TRIAD_ORBIT.id;
    if (o.r <= 0) return out.set(0, 0, 0);
    const a = o.phase + t * o.speed;
    return out.set(
      Math.cos(a) * o.r,
      Math.sin(a) * o.r * Math.sin(o.tilt),
      Math.sin(a) * o.r * Math.cos(o.tilt),
    );
  }

  // ── 潜意识暗流：念头池（半透明幽灵泡）+ 梦境池（紫色旋涡）──
  const thoughtGroup = new THREE.Group();
  scene.add(thoughtGroup);
  let thoughtSprites: THREE.Sprite[] = [];
  let thoughtCount = 0;
  function setThoughts(list: unknown[]) {
    for (const sp of thoughtSprites) {
      thoughtGroup.remove(sp);
      (sp.material as THREE.SpriteMaterial).map?.dispose?.();
      (sp.material as THREE.SpriteMaterial).dispose?.();
    }
    thoughtSprites = [];
    thoughtCount = Math.min(12, list?.length || 0);
    const tex = makeGlowTexture();
    for (let i = 0; i < thoughtCount; i++) {
      const mat = new THREE.SpriteMaterial({
        map: tex, color: 0xa8d8ff, transparent: true, opacity: 0.3,
        depthWrite: false, blending: THREE.AdditiveBlending,
      });
      const sp = new THREE.Sprite(mat);
      sp.scale.setScalar(0.24 + (i % 3) * 0.06);
      sp.userData.thoughtIndex = i;
      thoughtGroup.add(sp);
      thoughtSprites.push(sp);
    }
  }

  // 梦境池：紫红色混沌星云旋涡（粒子雾，不是线框）
  const DREAM_POS = new THREE.Vector3(0, 4.2, 0);   // 短期记忆正头顶（世界坐标同轴，顶视图也对齐）
  const dreamGroup = new THREE.Group();
  dreamGroup.position.copy(DREAM_POS);
  scene.add(dreamGroup);
  const DREAM_N = 1600;
  const dreamGeo = new THREE.BufferGeometry();
  const dreamPos = new Float32Array(DREAM_N * 3);
  const dreamCol = new Float32Array(DREAM_N * 3);
  {
    const c = new THREE.Color();
    for (let i = 0; i < DREAM_N; i++) {
      // 中心密、外围散：t 是"离中心多远"
      const t = Math.pow(Math.random(), 0.55);
      const r = 0.12 + t * 2.3;
      const a = Math.random() * Math.PI * 2 + t * 9;
      const y = (Math.random() - 0.5) * 0.9 * (0.25 + t);
      // 随机抖动 → 混沌雾感（不是规整螺旋）
      const jitter = 0.42 * (1 - t * 0.45);
      dreamPos[i * 3] = Math.cos(a) * r + (Math.random() - 0.5) * jitter;
      dreamPos[i * 3 + 1] = y + (Math.random() - 0.5) * jitter * 0.6;
      dreamPos[i * 3 + 2] = Math.sin(a) * r + (Math.random() - 0.5) * jitter;
      // 极暗星空紫打底，只有中心微微发亮
      const k = Math.pow(1 - t, 2.2);
      c.setRGB(0.30 + 0.45 * k, 0.18 + 0.26 * k, 0.55 + 0.40 * k);
      dreamCol[i * 3] = c.r; dreamCol[i * 3 + 1] = c.g; dreamCol[i * 3 + 2] = c.b;
    }
    dreamGeo.setAttribute('position', new THREE.BufferAttribute(dreamPos, 3));
    dreamGeo.setAttribute('color', new THREE.BufferAttribute(dreamCol, 3));
    const dreamMat = new THREE.PointsMaterial({
      size: 0.095, vertexColors: true, transparent: true, opacity: 0.55,
      depthWrite: false, blending: THREE.AdditiveBlending, map: makeGlowTexture(),
      sizeAttenuation: true,
    });
    dreamGroup.add(new THREE.Points(dreamGeo, dreamMat));
    // 中心高亮紫核：一眼看到"这里能点"
    const coreMat = new THREE.SpriteMaterial({
      map: makeGlowTexture(), color: 0xb07cff, transparent: true, opacity: 0.9,
      depthWrite: false, blending: THREE.AdditiveBlending,
    });
    const core = new THREE.Sprite(coreMat);
    core.scale.setScalar(1.25);
    dreamGroup.add(core);
  }

  // ── 三我状态：本我颜色随 DA 波动；冲突时本我发红膨胀、自我球施力 ──
  // ── 撕裂闪电：自我 ↔ 本我 / 自我 ↔ 超我（冲突等级驱动）──
  const BOLT_SEG = 14;
  function makeBolt() {
    const pos = new Float32Array(BOLT_SEG * 3);
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    const mat = new THREE.LineBasicMaterial({
      color: 0x9ad8ff, transparent: true, opacity: 0,
      blending: THREE.AdditiveBlending, depthWrite: false,
    });
    const line = new THREE.Line(geo, mat);
    line.frustumCulled = false;
    scene.add(line);
    return { geo, mat, pos, line };
  }
  const boltId = makeBolt();     // 自我 ↔ 本我（红）
  const boltSuper = makeBolt();  // 自我 ↔ 超我（金）

  function updateBolt(bolt: { geo: THREE.BufferGeometry; mat: THREE.LineBasicMaterial; pos: Float32Array },
                      a: THREE.Vector3, b: THREE.Vector3, strength: number, t: number) {
    const dir = new THREE.Vector3().subVectors(b, a);
    const side = new THREE.Vector3(-dir.z, 0, dir.x).normalize();
    for (let i = 0; i < BOLT_SEG; i++) {
      const k = i / (BOLT_SEG - 1);
      const jitter = (Math.random() - 0.5) * 0.22 * strength;
      const p = a.clone().addScaledVector(dir, k)
        .addScaledVector(side, Math.sin(k * 12 + t * 8) * 0.1 * strength + jitter);
      bolt.pos[i * 3] = p.x;
      bolt.pos[i * 3 + 1] = p.y;
      bolt.pos[i * 3 + 2] = p.z;
    }
    bolt.geo.attributes.position.needsUpdate = true;
    bolt.mat.opacity = 0.15 + 0.75 * strength * (0.6 + 0.4 * Math.sin(t * 9));
    (bolt.mat as THREE.LineBasicMaterial).color.setHex(strength > 0.7 ? 0xff6a4a : 0x9ad8ff);
  }

  let triadState: {
    da: number; conflict: unknown | null; level: string;
    forces: { id: number; ego: number; superego: number };
  } = { da: 40, conflict: null, level: '低', forces: { id: 0.4, ego: 0.4, superego: 0.4 } };
  async function refreshTriad() {
    try {
      const r = await fetch('/api/memory/detail?nodeId=persona:me');
      if (!r.ok) return;
      const d = await r.json();
      const t = (d && d.triad) || {};
      const f = (t.forces || {}) as Record<string, number>;
      triadState = {
        da: Number((((t.id || {}) as Record<string, unknown>).values as Record<string, number> || {}).DA ?? 40),
        conflict: t.conflict || null,
        level: String(t.level || '低'),
        forces: {
          id: Number.isFinite(f.id) ? f.id : 0.4,
          ego: Number.isFinite(f.ego) ? f.ego : 0.4,
          superego: Number.isFinite(f.superego) ? f.superego : 0.4,
        },
      };
    } catch {
      /* 面板没起时静默 */
    }
  }
  void refreshTriad();
  const triadTimer = window.setInterval(refreshTriad, 60000);

  function resize() {
    const w = container.clientWidth || 1;
    const h = container.clientHeight || 1;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  }
  resize();
  const ro = new ResizeObserver(resize);
  ro.observe(container);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = false;
  controls.dampingFactor = 0.08;
  controls.rotateSpeed = 0.6;
  controls.zoomSpeed = 0.8;
  controls.minDistance = 1.2;
  controls.maxDistance = 42;
  controls.target.set(0, 0, 0);
  controls.mouseButtons = {
    LEFT: THREE.MOUSE.ROTATE,
    MIDDLE: THREE.MOUSE.PAN,
    RIGHT: THREE.MOUSE.PAN,
  };

  // ── 文字标签 ──
  const labelLayer = createLabelLayer(container);
  const LABELS: LabelItem[] = [
    { key: 'wh', text: '白洞 · 新记忆', anchor: WH_POS.clone().add(new THREE.Vector3(0, 0.55, 0)), color: '#e8f6ff', priority: 100, size: 13 },
    { key: 'short', text: '短期记忆', anchor: new THREE.Vector3(0, 2.75, 0), color: '#a8e0ff', priority: 100, size: 13 },
    { key: 'me', text: '蓝色大肥鱼 · 本我', anchor: new THREE.Vector3(0, -0.95, 0), color: '#ffd77a', priority: 120, size: 14 },
    
    
    { key: 'bh', text: '黑洞 · 遗忘', anchor: BLACKHOLE_POS.clone().add(new THREE.Vector3(0, 0.55, 0)), color: '#ffb86c', priority: 100, size: 13 },
  ];
  const labelItems: LabelItem[] = [];
  // 三我头顶文字（anchor 每帧跟随公转）
  const triadLabelRefs: Record<string, LabelItem> = {};
  {
    const meLabel = LABELS.find((x) => x.key === 'me');
    if (meLabel) triadLabelRefs.id = meLabel;
    const egoItem: LabelItem = { key: 'ego', text: '自我', anchor: new THREE.Vector3(), color: '#4ab8ff', priority: 118, size: 13 };
    const superItem: LabelItem = { key: 'superego', text: '超我', anchor: new THREE.Vector3(), color: '#fff6d8', priority: 118, size: 13 };
    LABELS.push(egoItem, superItem);
    triadLabelRefs.ego = egoItem;
    triadLabelRefs.superego = superItem;
  }

  // ── 人格行星 ──
  const planetGroup = new THREE.Group();
  scene.add(planetGroup);

  interface PlanetEntry {
    key: string;
    kind: string;
    id: string;
    group: THREE.Group;
    planet: MemoryPlanet;
    glowMat: THREE.SpriteMaterial;
    hit: { pos: THREE.Vector3; id: string; info: string; radius: number };
    phase: number;
    tilt: number;
    rJitter: number;
    appearAt: number;
    from: THREE.Vector3;
    startPal: ReturnType<typeof palette>;
    finalPal: ReturnType<typeof palette>;
    colored: boolean;
    fly: number;
    hitMesh: THREE.Mesh;    // 不可见命中球（Raycaster 用，每帧跟着行星走）
    lodDot: THREE.Sprite;   // 远距离降级点
  }

  const planets: PlanetEntry[] = [];
  const planetLive = new Map<string, THREE.Group>();
  const planetDone = new Set<string>();
  const hiddenKinds = new Set<string>();
  const TMP_A = new THREE.Color();
  const TMP_B = new THREE.Color();
  const TMP_C = new THREE.Color();
  const ORBIT_TMP = new THREE.Vector3();
  const TMP_LIGHT = new THREE.Vector3();
  const SHORT_TMP = new THREE.Vector3();
  let animT = 0;
  let epoch = 0;

  function palette(c: THREE.Color) {
    const white = new THREE.Color(0xffffff);
    return {
      a: c.clone().multiplyScalar(0.22),
      b: c.clone().lerp(white, 0.18),
      c: c.clone().lerp(white, 0.55),
    };
  }

  function finalOf(pl: PersonaPlanet) {
    const c = (pl as { color?: number }).color;
    if (typeof c === 'number' && c > 0) return new THREE.Color(c);
    return new THREE.Color(pl.kind === 'defense' ? 0xb03030 : pl.kind === 'rule' ? 0x5aa8ff : 0xffd77a);
  }

  function ringCount(kind: string): number {
    let n = 0;
    for (const p of planets) if (p.kind === kind) n++;
    return n;
  }

  // ── 轨道尘埃：用极淡的粒子沿轨道铺开，暗示引力；不画任何实体线 ──
  function planetOrbitPos(
    p: { kind: string; phase: number; tilt: number; rJitter: number },
    t: number,
    out: THREE.Vector3,
  ): THREE.Vector3 {
    const spec = PLANET_RING[p.kind] ?? PLANET_RING.id;
    const expand = Math.min(0.22, ringCount(p.kind) * 0.02);
    const radius = spec.radius + expand + p.rJitter;
    const a = p.phase + t * spec.speed;
    const x = Math.cos(a) * radius;
    const z = Math.sin(a) * radius;
    // 认祖归宗：每颗行星绕自己那一层的球转，形成三个独立子星系
    const base = triadPos(p.kind, t);
    out.set(base.x + x, base.y + z * Math.sin(p.tilt), base.z + z * Math.cos(p.tilt));
    return out;
  }

  /** 记忆被哪颗行星捕获：长期→特质球，事件→准则球，创伤→防御球（伴星） */
  function hostPlanetFor(dest: string, id: string): PlanetEntry | null {
    const want = dest === 'longterm' ? 'id' : dest === 'episode' ? 'ego' : dest === 'trauma' ? 'ego' : '';
    if (!want) return null;
    const list = planets.filter((p) => p.kind === want && p.group.visible);
    if (!list.length) return null;
    return list[Math.floor(hash01(id + '#host') * list.length) % list.length];
  }

  /** 记忆粒子的目标点 = 宿主行星位置 + 一圈缓慢自转的星尘偏移 */
  function orbitPoint(dest: string, id: string, out: THREE.Vector3): boolean {
    // 长期记忆：外围巨大球壳，像星尘一样包裹整个三我星系
    if (dest === 'longterm') {
      const h1 = hash01(id);
      const h2 = hash01(id + '#u');
      const ph = h1 * Math.PI * 2;
      const th = Math.acos(2 * h2 - 1);
      const rr = 4.4 + hash01(id + '#r') * 2.2;
      out.set(
        rr * Math.sin(th) * Math.cos(ph + animT * 0.012),
        rr * Math.cos(th),
        rr * Math.sin(th) * Math.sin(ph + animT * 0.012),
      );
      return true;
    }
    // 事件 / 创伤：和长期记忆一样铺在外围球壳，交错分布
    if (dest === 'episode' || dest === 'trauma') {
      const h1 = hash01(id);
      const h2 = hash01(id + '#u');
      const ph = h1 * Math.PI * 2;
      const th = Math.acos(2 * h2 - 1);
      const baseR = dest === 'trauma' ? 4.6 : 4.4;
      const rr = baseR + hash01(id + '#r') * 2.0;
      out.set(
        rr * Math.sin(th) * Math.cos(ph + animT * 0.012),
        rr * Math.cos(th),
        rr * Math.sin(th) * Math.sin(ph + animT * 0.012),
      );
      return true;
    }
    const host = hostPlanetFor(dest, id);
    if (!host) return false;
    planetOrbitPos(host, animT, out);
    const h1 = hash01(id + '#u');
    const h2 = hash01(id + '#t');
    const h3 = hash01(id + '#b');
    // 创伤是"伴星"：壳更大、离防御球更远
    const shell = dest === 'trauma' ? 0.30 + 0.16 * h3 : 0.18 + 0.20 * h3;
    // 两条旋臂：70% 的点落在臂上（±0.5 rad），30% 散开 → 疏密不均的尘埃带
    const arm = h1 < 0.5 ? 0 : Math.PI;
    const spread = h2 < 0.7 ? (h2 - 0.35) * 1.4 : (h2 - 0.5) * Math.PI * 2;
    const ringSpec = PLANET_RING[host.kind] ?? PLANET_RING.id;
    const ang = arm + spread + animT * ringSpec.dustSpin;
    const u = h1 * 2 - 1;
    const sr = Math.sqrt(Math.max(0, 1 - u * u));
    out.x += Math.cos(ang) * sr * shell;
    out.y += u * shell * 0.75;
    out.z += Math.sin(ang) * sr * shell;
    return true;
  }

  /** 后端来一帧行星列表：只补新的，已有的不重建（重建会打断入场动画） */
  function setPlanets(list: PersonaPlanet[]) {
    if (!list?.length) return;
    for (let idx = 0; idx < list.length; idx++) {
      const pl = list[idx];
      if (hiddenKinds.has(pl.kind)) continue;
      const key = `${pl.kind}:${pl.id ?? idx}`;
      if (planetDone.has(key)) continue;
      const ap = Number(pl.appearAt || 0);
      const now = (window as unknown as { __flowAt?: number }).__flowAt || Date.now();
      if (ap && ap > now) continue;

      const ring = PLANET_RING[pl.kind] ?? PLANET_RING.id;
      const idxInKind = Number(pl.idxInKind ?? idx);
      const phase = idxInKind * 2.39996;
      const tilt = ring.tilt + (hash01(key) - 0.5) * 0.22;
      const rJitter = (((idxInKind * 37) % 10) / 10) * 0.1;
      const target = planetOrbitPos({ kind: pl.kind, phase, tilt, rJitter }, animT, new THREE.Vector3());

      // 入场：从外圈记忆环飞进自己的轨道
      const si = (pl as { sourceIndex?: number }).sourceIndex ?? idx;
      const ang = si * 0.6180339887 * Math.PI * 2;
      const r0 = 2.4 + ((si * 13) % 10) / 10;
      const from = new THREE.Vector3(r0 * Math.cos(ang), Math.sin(si * 1.7) * 0.18, r0 * Math.sin(ang));

      const g = new THREE.Group();
      g.position.copy(from);
      const preset = PLANET_PRESET[pl.kind] ?? PLANET_PRESET.id;
      const startPal = palette(BIRTH_COLOR);
      const radius = Math.min(pl.size * 2.0, 0.145);
      const planet = createMemoryPlanet({
        radius,
        colorA: startPal.a,
        colorB: startPal.b,
        colorC: startPal.c,
        bands: preset.bands,
        turb: preset.turb,
        ring: preset.ring,
        spin: (PLANET_RING[pl.kind] ?? PLANET_RING.id).bodySpin,   // 内圈自转快、外圈慢
        axis: new THREE.Vector3(Math.sin(idx * 1.7), 1, Math.cos(idx * 1.7)).normalize(),
      });
      g.add(planet.group);

      const glowMat = new THREE.SpriteMaterial({
        map: makeGlowTexture(128),
        color: BIRTH_COLOR.getHex(),
        transparent: true,
        opacity: 0.14,                     // 收一点：太大太浓会像"涂抹"
        blending: THREE.AdditiveBlending,
        depthWrite: false,
      });
      const glow = new THREE.Sprite(glowMat);
      glow.scale.setScalar(Math.max(0.38, pl.size * 3.6));
      g.add(glow);

      // ── Raycaster 命中球（不可见，比本体大 1.8 倍，好点）──
      const hitMesh = new THREE.Mesh(
        new THREE.SphereGeometry(1, 10, 8),
        new THREE.MeshBasicMaterial({ transparent: true, opacity: 0, depthWrite: false }),
      );
      hitMesh.scale.setScalar(Math.max(radius * 1.8, 0.12));
      g.add(hitMesh);

      // ── LOD 占位点：镜头拉远时用一颗小点代替整个实体球 ──
      const lodMat = new THREE.SpriteMaterial({
        map: makeGlowTexture(64),
        color: finalOf(pl).getHex(),
        transparent: true,
        opacity: 0.9,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
      });
      const lodDot = new THREE.Sprite(lodMat);
      lodDot.scale.setScalar(Math.max(0.14, radius * 2.6));
      lodDot.visible = false;
      g.add(lodDot);


      planetGroup.add(g);
      planetLive.set(key, g);
      planetDone.add(key);

      const id = pl.id || `soul:${pl.kind}:${pl.idxInKind ?? idx}`;
      const hit = { pos: target.clone(), id, info: pl.text, radius: 34 };
      hitMesh.userData.pickId = id;
      planets.push({
        key, kind: pl.kind, id, group: g, planet, glowMat, hit, hitMesh, lodDot,
        phase, tilt, rJitter, appearAt: ap || 0, from, startPal,
        finalPal: palette(finalOf(pl)), colored: false, fly: 0,
      });
    }
  }

  const FLY_SEC = 6; // 入场：从外环飞进轨道（真实秒）

  function updatePlanets(dt: number, t: number) {
    const now = (window as unknown as { __flowAt?: number }).__flowAt || Date.now();
    for (const p of planets) {
      const started = !p.appearAt || now >= p.appearAt;
      p.group.visible = started && !hiddenKinds.has(p.kind);
      if (!started) continue;
      planetOrbitPos(p, t, p.hit.pos);
      // 本我就是光源：朝向中心的一面亮、背面暗，才有球体感
      TMP_LIGHT.copy(p.hit.pos).multiplyScalar(-1);
      if (TMP_LIGHT.lengthSq() < 1e-6) TMP_LIGHT.set(1, 0, 0);
      p.planet.setLight(TMP_LIGHT.normalize());
      if (!p.colored) {
        p.fly += dt;
        const kk = Math.min(1, p.fly / FLY_SEC);
        const ee = kk * kk * (3 - 2 * kk);
        p.group.position.lerpVectors(p.from, p.hit.pos, ee);
        p.planet.setColors(
          TMP_A.copy(p.startPal.a).lerp(p.finalPal.a, ee),
          TMP_B.copy(p.startPal.b).lerp(p.finalPal.b, ee),
          TMP_C.copy(p.startPal.c).lerp(p.finalPal.c, ee),
        );
        p.glowMat.color.copy(p.startPal.c).lerp(p.finalPal.c, ee);
        if (kk >= 1) {
          p.colored = true;
          p.planet.setColors(p.finalPal.a, p.finalPal.b, p.finalPal.c);
          p.glowMat.color.copy(p.finalPal.c);
        }
      } else {
        p.group.position.copy(p.hit.pos);
      }
      p.planet.update(t, dt);
      // LOD：按屏幕上的像素直径判定——球小到看不清细节就降级成一个点（保帧率）
      const dist = Math.max(0.01, camera.position.distanceTo(p.group.position));
      const pxRadius = (0.145 * (container.clientHeight * 0.5)) / (Math.tan((55 * Math.PI) / 360) * dist);
      // 自适应 LOD：帧率掉了就抬高阈值（小球降级成点），回到 60 再慢慢收回
      const forcePx = Number((window as unknown as { __lodForce?: number }).__lodForce ?? 0);
      const lodPx = LOD_DISABLED ? 0 : forcePx > 0 ? forcePx : lodPxAuto;
      const far = pxRadius < lodPx;
      p.planet.group.visible = !far;
      p.glowMat.opacity = far ? 0 : 0.18;
      p.lodDot.visible = far;
      // 球在屏幕上小到 <9px 时关掉大气壳/尘环（填充率大头），形状还在
      p.planet.setQuality(pxRadius >= 20);
    }
  }

  // ── 粒子：目标点 / 弹簧 / 颜色 ──
  const sx = new Float32Array(MAX);
  const sy = new Float32Array(MAX);
  const sz = new Float32Array(MAX);
  const vx = new Float32Array(MAX);
  const vy = new Float32Array(MAX);
  const vz = new Float32Array(MAX);
  const tx = new Float32Array(MAX);
  const ty = new Float32Array(MAX);
  const tz = new Float32Array(MAX);
  const sizeBase = new Float32Array(MAX);
  const fade = new Float32Array(MAX).fill(1);
  const slotId: string[] = [];
  const prevState: string[] = [];

  let particlesNow: FlowParticle[] = [];
  let aliveCount = 0;

  function shortShell(id: string, out: THREE.Vector3) {
    const h = hash01(id);
    const h2 = hash01(id + '#s');
    const a = h * Math.PI * 2;
    const rr = 0.34 + h2 * 0.3;
    out.set(
      SHORT_POS.x + Math.cos(a) * rr,
      SHORT_POS.y + (h2 - 0.5) * 0.34,
      SHORT_POS.z + Math.sin(a) * rr,
    );
    return out;
  }

  /** 一帧数据：只记录粒子和颜色；位置逐帧由 computeTargets 算（行星一直在动） */
  function updateThoughts(list: unknown[]) { setThoughts(list || []); }

  function update(next: FlowParticle[]) {
    particlesNow = next || [];
    aliveCount = Math.min(particlesNow.length, MAX);

    for (let i = 0; i < aliveCount; i++) {
      const p = particlesNow[i];
      if (slotId[i] !== p.id) {
        // 槽位换人：瞬移 + 清速度，避免画出横穿全场的线
        slotId[i] = p.id;
        sx[i] = p.pos.x * SPREAD;
        sy[i] = p.pos.y * SPREAD;
        sz[i] = p.pos.z * SPREAD;
        vx[i] = vy[i] = vz[i] = 0;
        fade[i] = 1;
        prevState[i] = '';
        if (p.state === 'BORN') {
          sx[i] = WH_POS.x;
          sy[i] = WH_POS.y;
          sz[i] = WH_POS.z;
          whiteHole.flare();
        }
      }
      // 颜色：出生/漫游用冷白，分流后按命运色；归位后压暗（星尘）
      const base = p.state === 'BORN' || p.state === 'WANDER'
        ? SHORT_COLOR
        : (p.tone ? new THREE.Color(TONE_COLOR[p.tone] ?? 0xbfe0ff)
                  : new THREE.Color(DEST_COLOR[p.dest] ?? 0xbfe0ff));
      const dim = p.state === 'ARRIVED' ? 1.05 : 1.45;
      colors[i * 3] = base.r * p.bright * dim;
      colors[i * 3 + 1] = base.g * p.bright * dim;
      colors[i * 3 + 2] = base.b * p.bright * dim;
      // 归位的记忆是"星尘"：点更小（LOD：点云不是实体球）
      sizeBase[i] = p.size * (p.state === 'ARRIVED' ? 9.0 : 11.5);
      sizes[i] = sizeBase[i] * fade[i];
      if (p.state === 'ARRIVED' && prevState[i] !== 'ARRIVED') shortCore.pulse(0.15);
      prevState[i] = p.state;
    }
    for (let i = aliveCount; i < MAX; i++) {
      sizes[i] = 0;
      positions[i * 3] = 9999;
      positions[i * 3 + 1] = 9999;
      positions[i * 3 + 2] = 9999;
      slotId[i] = '';
    }
    geo.setDrawRange(0, aliveCount);
    geo.attributes.aColor.needsUpdate = true;
    geo.attributes.aSize.needsUpdate = true;
    points.visible = aliveCount > 0;
  }

  /** 每个粒子的目标点：状态 + 命运 + 宿主行星 */
  function computeTargets() {
    for (let i = 0; i < aliveCount; i++) {
      const p = particlesNow[i];
      if (!p) continue;
      if (p.state === 'BORN' || p.state === 'WANDER') {
        shortShell(p.id, SHORT_TMP);
        tx[i] = SHORT_TMP.x;
        ty[i] = SHORT_TMP.y;
        tz[i] = SHORT_TMP.z;
        continue;
      }
      if (p.dest === 'blackhole') {
        // 遗忘：螺旋沉进视界
        const h = hash01(p.id);
        const a = h * Math.PI * 2 + animT * 0.6;
        const r = 0.5 * (1 - Math.min(1, p.life || 0));
        tx[i] = BLACKHOLE_POS.x + Math.cos(a) * r;
        ty[i] = BLACKHOLE_POS.y + Math.sin(a) * r * 0.35;
        tz[i] = BLACKHOLE_POS.z + Math.sin(a) * r;
        if ((p.life || 0) > 0.92) fade[i] = Math.max(0, fade[i] - 0.01);
        continue;
      }
      if (orbitPoint(p.dest, p.id, ORBIT_TMP)) {
        tx[i] = ORBIT_TMP.x;
        ty[i] = ORBIT_TMP.y;
        tz[i] = ORBIT_TMP.z;
      } else {
        // 还没有宿主行星（刚启动 / 球被关掉）：先在短期球附近等
        shortShell(p.id, SHORT_TMP);
        tx[i] = SHORT_TMP.x;
        ty[i] = SHORT_TMP.y;
        tz[i] = SHORT_TMP.z;
      }
    }
  }

  const FIXED_DT = 1 / 120;
  const STIFF = 55;
  const DAMP = 13;
  let simAcc = 0;

  function stepSim(dt: number) {
    simAcc = Math.min(simAcc + dt, 0.1);
    while (simAcc >= FIXED_DT) {
      computeTargets();
      for (let i = 0; i < aliveCount; i++) {
        vx[i] += ((tx[i] - sx[i]) * STIFF - vx[i] * DAMP) * FIXED_DT;
        vy[i] += ((ty[i] - sy[i]) * STIFF - vy[i] * DAMP) * FIXED_DT;
        vz[i] += ((tz[i] - sz[i]) * STIFF - vz[i] * DAMP) * FIXED_DT;
        sx[i] += vx[i] * FIXED_DT;
        sy[i] += vy[i] * FIXED_DT;
        sz[i] += vz[i] * FIXED_DT;
        const dx = tx[i] - sx[i];
        const dy = ty[i] - sy[i];
        const dz = tz[i] - sz[i];
        if (dx * dx + dy * dy + dz * dz < 1e-7) {
          // 只清速度，不瞬移：目标每帧会微动，硬吸附会反复跳变（抽搐）
          vx[i] = vy[i] = vz[i] = 0;
        }
      }
      simAcc -= FIXED_DT;
    }
    for (let i = 0; i < aliveCount; i++) {
      positions[i * 3] = sx[i];
      positions[i * 3 + 1] = sy[i];
      positions[i * 3 + 2] = sz[i];
    }
    geo.attributes.position.needsUpdate = true;
    geo.boundingSphere = null;   // 粒子每帧动，重算包围球才能点中
  }

  // ── 点击拾取：行星优先，其次宏观节点 ──
  const pickTargets = [
    { pos: WH_POS.clone(), id: 'persona:whitehole', info: '白洞 · 新记忆的诞生点', radius: 64 },
    { pos: SHORT_POS.clone(), id: 'persona:short', info: '短期记忆', radius: 56 },
    { pos: BLACKHOLE_POS.clone(), id: 'persona:blackhole', info: '黑洞 · 遗忘', radius: 64 },
  ];
  const projTmp = new THREE.Vector3();

  function screenOf(w: THREE.Vector3, rect: DOMRect, out: { x: number; y: number }): boolean {
    projTmp.copy(w).project(camera);
    if (projTmp.z > 1) return false;
    out.x = (projTmp.x * 0.5 + 0.5) * rect.width;
    out.y = (-projTmp.y * 0.5 + 0.5) * rect.height;
    return true;
  }

  let lodPxAuto = 5;          // 自动 LOD 阈值（px）：持续掉帧时上调
  let lodStrikes = 0;         // 连续低帧窗口计数
  const LOD_DISABLED = /[?&]lod=off/.test(window.location.search);
  let fpsAcc = 0;
  let fpsFrames = 0;
  let fpsWindowStart = 0;

  const raycaster = new THREE.Raycaster();
  const ndc = new THREE.Vector2();

  function onClick(ev: PointerEvent) {
    const rect = container.getBoundingClientRect();
    const mx = ev.clientX - rect.left;
    const my = ev.clientY - rect.top;
    const s = { x: 0, y: 0 };
    let best: { d: number; id: string } | null = null;

    // 念头气泡（半透明幽灵泡）：点中 → 那条未完成的执念
    {
      const v = new THREE.Vector3();
      for (let ti = 0; ti < thoughtSprites.length; ti++) {
        thoughtSprites[ti].getWorldPosition(v);
        if (!screenOf(v, rect, s)) continue;
        const d = Math.hypot(mx - s.x, my - s.y);
        if (d < 26 && (!best || d < best.d)) best = { d, id: `thought:${ti}` };
      }
      // 梦境旋涡：顶部大范围可点
      const dv = DREAM_POS.clone();
      dreamGroup.getWorldPosition(dv);
      if (screenOf(dv, rect, s)) {
        const d = Math.hypot(mx - s.x, my - s.y);
        if (d < 70 && (!best || d < best.d)) best = { d, id: 'pool:dreams' };
      }
    }
    // 中心球 + 公转中的三我球：动态位置，48px 内优先选中
    const triadNow: Array<[string, THREE.Vector3, number]> = [
      ['persona:me', new THREE.Vector3(0, 0, 0), 48],
      ['persona:id', triadPos('id', animT), 46],
      ['persona:ego', triadPos('ego', animT), 34],
      ['persona:superego', triadPos('superego', animT), 34],
    ];
    for (const [tid, tpos, rad] of triadNow) {
      if (screenOf(tpos, rect, s)) {
        const dCore = Math.hypot(mx - s.x, my - s.y);
        if (dCore < rad && (!best || dCore < best.d)) best = { d: dCore, id: tid };
      }
    }
    // 行星用 Raycaster：命中球挂在行星 group 上，每帧随世界矩阵更新
    if (!best) {
      ndc.set((mx / rect.width) * 2 - 1, -(my / rect.height) * 2 + 1);
      raycaster.setFromCamera(ndc, camera);
      const meshes = planets.filter((p) => p.group.visible).map((p) => p.hitMesh);
      const hits = raycaster.intersectObjects(meshes, false);
      if (hits.length > 0) {
        const id = String((hits[0].object as THREE.Object3D).userData.pickId || '');
        if (id) best = { d: 0, id };
      }
    }
    // 长期记忆星尘（点云）也可点：点中哪颗就查哪条记忆
    if (!best) {
      ndc.set((mx / rect.width) * 2 - 1, -(my / rect.height) * 2 + 1);
      raycaster.setFromCamera(ndc, camera);
      raycaster.params.Points.threshold = 0.18;
      const phits = raycaster.intersectObject(points, false);
      if (phits.length > 0) {
        const idx = phits[0].index;
        if (idx !== undefined && particlesNow[idx]) best = { d: 0, id: particlesNow[idx].id };
      }
    }
    for (const t of pickTargets) {
      if (!screenOf(t.pos, rect, s)) continue;
      const d = Math.hypot(mx - s.x, my - s.y);
      if (d < t.radius && (!best || d < best.d)) best = { d, id: t.id };
    }
    if (best) onPickNode?.(best.id);
  }
  container.addEventListener('pointerdown', onClick);

  // ── 主循环 ──
  let raf = 0;
  let prevT = 0;
  let lastFlare = 0;
  let frameNo = 0;

  function loopBody() {
    const t = performance.now() / 1000;
    const dt = prevT ? Math.min(0.06, t - prevT) : 0.016;
    prevT = t;
    if (!epoch) epoch = t;
    animT = t - epoch;

    bgStars.rotation.y = animT * 0.008;
    // 念头气泡：绕着三我慢慢飘（潜意识暗流）
    for (let ti = 0; ti < thoughtSprites.length; ti++) {
      const a = ti * 2.399 + animT * 0.06;
      const rr = 2.0 + (ti % 5) * 0.35;
      const yy = -0.6 + Math.sin(animT * 0.4 + ti) * 0.6 + (ti % 3) * 0.25;
      thoughtSprites[ti].position.set(Math.cos(a) * rr, yy, Math.sin(a) * rr);
      thoughtSprites[ti].material.opacity = 0.22 + 0.12 * (0.5 + 0.5 * Math.sin(animT * 0.9 + ti));
    }
    dreamGroup.rotation.y = animT * 0.05;
    updatePlanets(dt, t);
    stepSim(dt);

    controls.update();
    mat.uniforms.uTime.value = animT;
    // 三我绕中心公转
    idCore.group.position.copy(triadPos('id', t));
    egoCore.group.position.copy(triadPos('ego', t));
    superegoCore.group.position.copy(triadPos('superego', t));
    // 冲突等级 → 撕裂闪电（自我↔本我 红 / 自我↔超我 金）+ 蓝球过载闪烁
    {
      const lv = triadState.level === '高' ? 1 : triadState.level === '中' ? 0.45 : 0;
      if (lv > 0) {
        const pId = triadPos('id', t, new THREE.Vector3());
        const pEgo = triadPos('ego', t, new THREE.Vector3());
        const pSup = triadPos('superego', t, new THREE.Vector3());
        updateBolt(boltId, pEgo, pId, lv, animT);
        updateBolt(boltSuper, pEgo, pSup, lv * 0.8, animT);
        egoCore.pulse(lv * (0.6 + 0.4 * Math.sin(animT * 6)));
      } else {
        boltId.mat.opacity = 0;
        boltSuper.mat.opacity = 0;
      }
    }
    // 头顶文字跟随各自球
    for (const kind of ['id', 'ego', 'superego'] as const) {
      const bp = triadPos(kind, t, new THREE.Vector3());
      triadLabelRefs[kind]?.anchor.set(bp.x, bp.y + 0.55, bp.z);
    }
    idCore.update(t, dt);
    egoCore.update(t, dt);
    superegoCore.update(t, dt);

    // 三我势力（0~1）→ 球的体积 + 色温：越强越大越亮
    const _clamp01 = (v: number) => Math.max(0, Math.min(1, v));
    const _scaleOf = (v: number) => 0.82 + 0.55 * _clamp01(v);
    const _kId = _scaleOf(triadState.forces.id);
    {
      const F = triadState.forces;
      const kId = _kId, kEgo = _scaleOf(F.ego), kSup = _scaleOf(F.superego);
      idCore.group.scale.setScalar(kId);
      egoCore.group.scale.setScalar(kEgo);
      superegoCore.group.scale.setScalar(kSup);
      // 自我越弱越偏冷白、越强越亮蓝；超我越严越偏金红
      egoCore.setMood(
        new THREE.Color('#0a2a4a').lerp(new THREE.Color('#134a7a'), F.ego),
        new THREE.Color('#2a9ad0').lerp(new THREE.Color('#7fd8ff'), F.ego),
        new THREE.Color('#b0f0ff'),
      );
      superegoCore.setMood(
        new THREE.Color('#8a7a3a').lerp(new THREE.Color('#a8401f'), F.superego),
        new THREE.Color('#fff6d8').lerp(new THREE.Color('#ffd08a'), F.superego),
        new THREE.Color('#ffffff'),
      );
    }

    // 三我视觉：本我随 DA 变亮变暖；冲突时发红膨胀（有上限），自我球脉动施力
    if (triadState.conflict) {
      idCore.setMood('#b02020', '#ff5a3c', '#ffe0d0');
      const k = 1 + 0.10 * (0.5 + 0.5 * Math.sin(t * 2.4));
      idCore.group.scale.setScalar(_kId * k);
      egoCore.pulse(0.5 + 0.3 * Math.sin(t * 2.4));
    } else {
      const q = Math.max(0, Math.min(1, (triadState.da - 20) / 60));
      idCore.setMood(
        new THREE.Color('#a35f08').lerp(new THREE.Color('#e8801a'), q),
        new THREE.Color('#ffc94a').lerp(new THREE.Color('#ff9a3c'), q),
        new THREE.Color('#fff4c9'),
      );
      idCore.group.scale.setScalar(_kId);
    }
    shortCore.update(t, dt);
    whiteHole.update(t, dt);
    if (animT - lastFlare > 2.4) {
      lastFlare = animT;
      whiteHole.flare();
    }
    frameNo++;
    if (frameNo % 3 === 0) blackHole.update(t, dt);

    {
      labelItems.length = 0;
      for (const lb of LABELS) labelItems.push(lb);
      labelLayer.draw(labelItems, camera, container.clientWidth || 1, container.clientHeight || 1);
    }

    let visible = 0;
    for (const p of planets) if (p.group.visible) visible++;
    (window as unknown as { __planets?: number }).__planets = visible;
    if (frameNo % 30 === 0) {
      const rect = container.getBoundingClientRect();
      const sp = { x: 0, y: 0 };
      (window as unknown as { __planetHits?: unknown[] }).__planetHits = planets
        .filter((p) => p.group.visible)
        .map((p) => {
          if (!screenOf(p.hit.pos, rect, sp)) return null;
          return { id: p.hit.id, kind: p.kind, x: Math.round(sp.x + rect.left), y: Math.round(sp.y + rect.top) };
        })
        .filter(Boolean);
      (window as unknown as { __lod?: number }).__lod =
        planets.filter((p) => p.lodDot.visible).length;
    }

    // 帧率窗口（每 2 秒一次）：<50 抬高阈值，>58 缓慢收回
    if (!fpsWindowStart) fpsWindowStart = t;
    fpsAcc += dt;
    fpsFrames++;
    if (t - fpsWindowStart > 2) {
      const f = fpsFrames / Math.max(0.001, t - fpsWindowStart);
      // 连续 3 个窗口 <38 才降级成点（避免偶发卡顿误伤）；>57 缓慢恢复
      if (f < 38) {
        lodStrikes++;
        if (lodStrikes >= 3 && lodPxAuto < 18) lodPxAuto = Math.min(18, lodPxAuto + 2);
      } else {
        lodStrikes = 0;
      }
      if (f > 57 && lodPxAuto > 5) lodPxAuto = Math.max(5, lodPxAuto - 1);
      (window as unknown as { __fps?: number }).__fps = Math.round(f);
      fpsAcc = 0;
      fpsFrames = 0;
      fpsWindowStart = t;
    }

    renderer.render(scene, camera);
    raf = requestAnimationFrame(loop);
  }

  function loop() {
    try {
      loopBody();
    } catch (err) {
      // 渲染循环里出错会让整块画布停住：把原因挂到 title 上，便于排查
      document.title = 'ERR ' + String((err as Error)?.message || err).slice(0, 200);
      raf = requestAnimationFrame(loop);
    }
  }

  return {
    start() {
      if (!raf) raf = requestAnimationFrame(loop);
    },
    stop() {
      if (raf) {
        cancelAnimationFrame(raf);
        raf = 0;
      }
    },
    update,
    updateThoughts,
    setPlanets,
    /** 关系圈层勾选：关掉的 kind 不再出球（已建好的先隐藏，勾回来下一帧自动恢复） */
    setHiddenKinds(kinds: string[]) {
      hiddenKinds.clear();
      for (const k of kinds) hiddenKinds.add(k);
      for (const [key, g] of planetLive) {
        if (hiddenKinds.has(key.split(':')[0])) g.visible = false;
      }
    },
    dispose() {
      if (raf) cancelAnimationFrame(raf);
      ro.disconnect();
      container.removeEventListener('pointerdown', onClick);
      for (const p of planets) p.planet.dispose();
      whiteHole.dispose();
      blackHole.dispose();
      shortCore.dispose();
      idCore.dispose();
      egoCore.dispose();
      superegoCore.dispose();
      window.clearInterval(triadTimer);
      labelLayer.dispose();
      mat.dispose();
      (mat.uniforms.uTex.value as THREE.Texture).dispose();
      geo.dispose();
      controls.dispose();
      renderer.dispose();
      if (renderer.domElement.parentElement === container) container.removeChild(renderer.domElement);
    },
  };
}
