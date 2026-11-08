import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { CSS2DRenderer, CSS2DObject } from 'three/examples/jsm/renderers/CSS2DRenderer.js';
import type { MemoryNode, StarfieldEdge } from '../types';
import { createWhiteHole } from './whitehole';
import { createBlackHole as createBlackHoleGPU } from './blackhole';

export interface RendererHandles {
  dispose: () => void;
  focus: (nodeId: string, zoomIn?: boolean) => void;
  focusPoint: (x: number, y: number, z: number) => void;
  setHighlight: (nodeId: string | null, topicNodeIds: string[]) => void;
  setPolarity: (p: string | null) => void;
  setColorMode: (m: 'category' | 'emotion') => void;
  setHormones: (h: Record<string, number>) => void;
  setTimeCutoff: (cutoff: number) => void;
  resize: () => void;
  setGlow: (v01: number) => void;
  setHalo: (v01: number) => void;   // 单独控制核心光晕（sprite）
  setZoom: (v01: number) => void;
  /** 让若干节点飞向黑洞并淡出（渲染层逐帧插值，不触发 React 重建） */
  fadeToBlackhole: (ids: string[]) => void;
  /** 最低保留度：低于它的球播「飞进黑洞」动画后隐藏（不重建场景） */
  setMinRetention: (v: number) => void;
  /** 数据刷新 / 时间指针变化时更新粒子目标位置（每帧现算 life，不重建场景） */
  updatePositions: (nodes: MemoryNode[], timeCutoffMs: number) => void;
}

interface Options {
  container: HTMLElement;
  nodes: MemoryNode[];
  edges: StarfieldEdge[];
  onPick: (node: MemoryNode) => void;
  onHover?: (node: MemoryNode | null) => void;
  /** 粒子被黑洞彻底吞掉的回调（用于通知后端「已遗忘」） */
  onSwallowed?: (id: string) => void;
}

/** 光点贴图：径向渐变 + 加法混合，做出"发光星体"的观感 */
function makeGlowTexture(): THREE.Texture {
  const size = 128;
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = size;
  const ctx = canvas.getContext('2d')!;
  const g = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
  g.addColorStop(0, 'rgba(255,255,255,1)');
  g.addColorStop(0.25, 'rgba(210,240,255,0.85)');
  g.addColorStop(0.55, 'rgba(120,200,255,0.35)');
  g.addColorStop(1, 'rgba(0,0,0,0)');
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, size, size);
  const tex = new THREE.CanvasTexture(canvas);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

/**
 * 遗忘黑洞的「卡冈图雅」纹理（程序化 canvas 生成，不依赖外部图片）。
 *
 * 参考《星际穿越》：中间黑色事件视界 + 水平橙金吸积盘 +
 * 被引力弯折到上下方的透镜光环（多普勒增亮，一侧更亮）。
 */
/**
 * 真实粒子吸积盘黑洞。
 *
 * 组成（全部是几何/粒子，没有位图贴图）：
 *   ① 事件视界：纯黑球体，绝对不反光
 *   ② 焦点白点：中心一颗极小白光，代表被观测/正在吞噬的奇点
 *   ③ 吸积盘：3000 颗粒子绕中心高速旋转，半径由内到外
 *       内圈  刺眼白蓝 #e8f4ff
 *       中圈  橙黄      #ffb347
 *       外圈  暗红渐隐
 *       y 方向压扁成薄盘，开普勒式差速旋转（越靠内转得越快）
 *   ④ 光子环：上下两道极细弧线（引力透镜把盘背面弯到上下方）
 */

/** 只喂预计算坐标/** 只喂预计算坐标：这里没有任何力导向/物理模拟。 */
const SPREAD = 1.8;

/** 点星时的目标视距。星海半径约 0.9，0.42 能看清一簇星团又不至于糊脸。 */
const STAR_FOCUS_DIST = 0.42;

// ---------- 关系星河点选/镜头（t4） ----------
/** 点击命中半径（屏幕像素）；关系星河按当前镜头距离与 fov 换算成世界半径 */
const PICK_PX_CLICK = 12;
/** hover 命中半径（屏幕像素）：比点击更紧，避免扫过密集星尘时闪出无关边 */
const PICK_PX_HOVER = 7;
/** 点核心后的视距：sceneRadius*0.28，并夹在 [1.6, keepDist*0.9] */
const RELATION_FOCUS_RATIO = 0.72;   // 0.28 太近：点一下其他星团就飞出视野
const RELATION_FOCUS_MIN = 1.6;
/** 只有一颗人格金核时（sceneRadius=0）的等效场景半径，用于算默认视距 */
const PERSONA_ONLY_RADIUS = 2.6;

// ---------- 关系星河：核心恒星 / 记忆星尘 的尺寸与亮度层级 ----------
// 后端 persona_view 给 kind=scope/persona 的 starfield.size 是「核心分级」（当前约 8~14，
// 数值会随后端调参变化），kind=memory 的星尘是 0.16~0.49。两套量纲必须分开处理。
// 归一化区间不写死：从本次 nodes 的核心 size 现算 min/max（后端再调参也不用改前端），
// 下面两个常量只作为「拿不到有效区间」时的兜底。
const CORE_SIZE_FALLBACK_MIN = 8;
const CORE_SIZE_FALLBACK_MAX = 14;
/** 关系布局判定：普通记忆星海里的群锚点 size=1，关系星河的核心是 8+ */
const RELATION_CORE_MIN_SIZE = 6;

/** 记忆星尘 aSize：0.012 + 0.018*size（后端当前 size 0.16~0.49 → 0.0149~0.0208），只做核心周围的尘土 */
const MEMORY_A_SIZE_BASE = 0.012;
const MEMORY_A_SIZE_GAIN = 0.018;

/** 群核心 aSize：0.115 + 0.085*t（t 为本次核心 size 区间的归一值 → 0.115~0.200）。
 *  下限 0.115 / 记忆上限 0.0208 ≈ 5.5 倍，远高于「至少 4 倍」的要求。 */
const SCOPE_A_SIZE_BASE = 0.115;
const SCOPE_A_SIZE_GAIN = 0.085;

/** 自我中心（persona:me）与分类锚点的 aSize */
const PERSONA_A_SIZE = 0.075;
const ANCHOR_A_SIZE = 0.05;

/** 群核心光晕 sprite 的世界尺寸：只随核心 size 温和放大（0.68~1.30），
 *  绝不能用 0.6 + size*0.4（size=10 → 4.6，会把半径 0.72 的整片星尘盘吞掉）。 */
const CORE_GLOW_MIN = 0.68;
const CORE_GLOW_GAIN = 0.62;

const clamp01 = (v: number) => (v < 0 ? 0 : v > 1 ? 1 : v);

function isCoreKind(n: MemoryNode): boolean {
  return n.kind === 'scope' || n.kind === 'persona';
}

/** 核心 size → 0~1 分级（区间由本次 nodes 现算，兜底 8~14）。 */
function coreT(size: number, coreMin: number, coreMax: number): number {
  const span = Math.max(0.5, coreMax - coreMin);
  return clamp01((size - coreMin) / span);
}

/** 星体 aSize 基准：核心恒星 >> 记忆星尘，两层量纲在这里彻底分开。
 *  relation=false（普通记忆星海，没有群核心）时保持旧的尺寸公式，不影响另一个视图。 */
function nodeBaseSize(n: MemoryNode, relation: boolean, coreMin: number, coreMax: number): number {
  const s = n.starfield?.size ?? 0;
  if (!relation) return (0.022 + 0.062 * s) * (n.kind === 'persona' ? 1.4 : 1.0);
  if (n.kind === 'scope') return SCOPE_A_SIZE_BASE + SCOPE_A_SIZE_GAIN * coreT(s, coreMin, coreMax);
  if (n.kind === 'persona') {
    // 人核心同样按 size 分级（金色巨核要够大），size 缺失时才回退到固定值
    return s >= RELATION_CORE_MIN_SIZE
      ? SCOPE_A_SIZE_BASE + SCOPE_A_SIZE_GAIN * coreT(s, coreMin, coreMax)
      : PERSONA_A_SIZE;
  }
  if (n.kind === 'category') return ANCHOR_A_SIZE;
  // 人格视图 v9 的 size 是 22000 / 220 / 90 这种量级，clamp01 会把三层压成同一档
  // （诊断报告 t7 实测：行星与星尘都变成 0.03 ≈ 9.3px）。这里按语义分三档。
  const cat = n.category;
  if (cat === 'trait' || cat === 'rule' || cat === 'defense') {
    return MEMORY_A_SIZE_BASE + MEMORY_A_SIZE_GAIN * 0.95;   // 行星：明显可见
  }
  if (cat === 'dust') {
    return MEMORY_A_SIZE_BASE + MEMORY_A_SIZE_GAIN * 0.25;   // 星尘：细小微弱
  }
  if (cat === 'blackhole') {
    // 遗忘黑洞：要比所有球都大一圈，才有"吞掉"的体量感
    return MEMORY_A_SIZE_BASE + MEMORY_A_SIZE_GAIN * 1.55;
  }
  if (cat === 'whitehole') {
    // 记忆白洞：和黑洞对称，同样大
    return MEMORY_A_SIZE_BASE + MEMORY_A_SIZE_GAIN * 1.55;
  }
  if (cat === 'short') {
    // 短期记忆：按 retention 缩小；被黑洞吸走时（sucked）继续收缩
    const ret = Number((n as any).retention ?? 1);
    const suck = Number((n as any).sucked ?? 0);
    return (MEMORY_A_SIZE_BASE + MEMORY_A_SIZE_GAIN * clamp01(ret)) * (1 - 0.55 * suck) * 2.2;
  }
  return MEMORY_A_SIZE_BASE + MEMORY_A_SIZE_GAIN * clamp01(s);
}

/** 星体亮度系数：核心拉满、星尘压暗，保证画面里「最亮最大」的永远是群核心。
 *  仅对关系星河生效；普通记忆星海沿用原来的亮度。 */
function brightnessOf(n: MemoryNode, relation: boolean): number {
  const raw = 0.55 + 0.45 * (n.starfield?.brightness ?? 0.5);
  if (!relation) return raw;
  if (n.kind === 'scope') return Math.min(1, raw * 1.25);
  if (n.kind === 'persona') return Math.min(1, raw * 1.15);
  if (n.kind === 'memory') return raw * 0.68;   // 星尘：退居背景
  return raw;
}

/** 缓存 CSS 颜色 → hex，避免每帧给几百个粒子 new THREE.Color。 */
const cssHexCache = new Map<string, number>();
function cssHex(css: string): number {
  let v = cssHexCache.get(css);
  if (v === undefined) {
    v = new THREE.Color(css).getHex();
    cssHexCache.set(css, v);
  }
  return v;
}

/** 正面情绪细分（开心/喜欢/得意/感谢/约定/期待）+ 兜底“正面”。 */
export const POSITIVE_EMOTIONS = ['正面', '开心', '喜欢', '得意', '感谢', '约定', '期待'] as const;

export function isPositiveEmotion(e?: string): boolean {
  return !!e && (POSITIVE_EMOTIONS as readonly string[]).includes(e);
}

/** 情绪 → (光晕色, 内核色)
 *  按情感极性着色：正面=绿 / 负面=黄 / 中性=冷白；创伤另走暗红。
 */
export function emotionColors(emotion?: string): { glow: number; core: number } {
  if (isPositiveEmotion(emotion)) return { glow: 0x3ddc84, core: 0xffffff };
  if (!emotion || emotion === '中性') return { glow: 0xbfe0ff, core: 0xffffff };
  // 怒/累/丧/烦/害怕 都归负面，统一黄
  return { glow: 0xffcc33, core: 0xffffff };
}

/** 节点光晕色：带 trauma 标记的记忆固定暗红（创伤红），其余按情绪取色。
 *  初始渲染和 setHighlight 必须走同一个函数，否则点一下就会把红色重置成情绪色。
 */
/** 五类记忆 → 星海配色（报告 §三.1）。核心在内圈、短期在最外圈由后端坐标决定。 */
export const CATEGORY_COLORS: Record<string, number> = {
  growth: 0xffd700,  // 人格成长：亮金（灵魂核心）
  card: 0x00bfff,    // 人物卡：天蓝（稳定恒星）
  long: 0x8a2be2,    // 长期记忆：紫（深邃）
  exp: 0x32cd32,     // 经历：绿（生长的故事）
  short: 0xe0e0e0,   // 短期记忆：灰白（快速暗淡）
  thought: 0xf2f7ff, // 念头池：近白虚影（未完成的想法）
  ring0: 0xffd24a,   // 内环·高频
  ring1: 0x5aa8ff,   // 中环·普通
  ring2: 0x8fa6bb,   // 外环·边缘
  trauma: 0xc03030,  // 创伤：暗红脉冲
  people: 0x5aa8ff,  // 人物：天蓝
};

/** 'category' = 按五类上色；'emotion' = 按正负/中性/创伤上色 */
let colorMode: 'category' | 'emotion' = 'category';

export function nodeGlowColor(n: MemoryNode): number {
  if (n.kind === 'persona') return 0xffd77a;   // 人格中心：金
  if (n.kind === 'category') return 0x7fd4ff;  // 分类锚点：青
  // 关系星河核心：默认由激素 trend 决定；切到「分类色」时按分类上色
  if (n.kind === 'scope') {
    if (colorMode === 'category') {
      const cc = CATEGORY_COLORS[n.category || ''];
      if (cc) return cc;
    }
    const trend = (n as unknown as { trend?: string }).trend;
    if (trend === 'cort') return 0xff3b30;   // 心血管张力：暗红
    if (trend === 'oxy') return 0xff7eb6;    // 催产素：粉
    return 0xbfe4ff;                          // 中性：冷蓝白
  }
  // 关系星河的星尘：直接用后端按群 trend 给的暗色（#c9737c / #dfa2c4 / #8fb6d9），
  // 不再走情绪色，避免彩色星尘盖过核心恒星。
  // 只有「关系星河的星尘」才用后端按群 trend 给的暗色（它们带 scopeKey）；
  // 普通星海/记忆池的记忆必须走情绪色（正面绿/负面黄/中性白/创伤红）。
  if (n.kind === 'memory' && n.starfield?.color
      && (n as unknown as { scopeKey?: string }).scopeKey) {
    return cssHex(n.starfield.color);
  }
  if (n.category === 'growth') {
    const st = (n as unknown as { status?: string }).status;
    if (st === 'dismissed') return 0x6b7280;   // 删除：不发光灰球
    if (st === 'solidified') return 0x3ddc84;  // 固化：绿
    return 0xffd24a;                           // 待确认：亮金
  }
  if (n.category === 'thought') return 0xf2f7ff;  // 念头：始终是虚影白
  if (n.category === 'dream') return 0xb48cff;    // 梦境：暗紫
  if (n.category === 'blackhole') return 0x0a0a12;  // 事件视界：近黑，亮靠吸积盘纹理
  if (n.category === 'whitehole') return 0xffffff; // 记忆白洞：纯白
  // 短期记忆：分流阶段用「命运色」（长期蓝/创伤红/事件绿/遗忘暗红），
  // 停留在环上或刚出生时用中性灰白。
  if (n.category === 'short' && n.starfield?.color) {
    return cssHex(n.starfield.color);
  }
  if (n.category === 'defense') return 0xb03030;  // 防御机制：暗红
  if (n.category === 'trait') return 0xffd77a;  // 核心特质：暖金
  if (n.category === 'rule') return 0x5aa8ff;   // 行为准则：天蓝
  if (colorMode === 'category') {
    const c = CATEGORY_COLORS[n.category || ''];
    if (c) return c;
  }
  // 情绪模式：正面绿 / 负面黄 / 中性冷白 / 创伤暗红
  // 创伤的三种形态都要认：带 trauma 对象 / 分类就是 trauma / id 以 trauma: 开头
  const traumatized = !!(n as unknown as { trauma?: { charge?: number } }).trauma
    || n.category === 'trauma'
    || String(n.id || '').startsWith('trauma:');
  return traumatized ? 0xb03030 : emotionColors(n.emotion).glow;
}

/** 节点的情感极性：创伤优先，其次按 emotion 归一。 */
export function polarityOf(n: MemoryNode): '正面' | '负面' | '中性' | '创伤' {
  // 与 nodeGlowColor 保持同一套判定，否则图例筛选和颜色会对不上
  if ((n as unknown as { trauma?: unknown }).trauma
      || n.category === 'trauma'
      || String(n.id || '').startsWith('trauma:')) return '创伤';
  const e = n.emotion || '中性';
  if (isPositiveEmotion(e)) return '正面';
  if (e === '中性') return '中性';
  return '负面';
}

export function createStarfieldRenderer({
  container, nodes, edges, onPick, onHover, onSwallowed,
}: Options): RendererHandles {
  const scene = new THREE.Scene();
  scene.fog = new THREE.FogExp2(0x03060d, 0.04);
  // 背景星在 r=25~55 的天球上，远平面必须放得下

  const camera = new THREE.PerspectiveCamera(55, 1, 0.01, 160);   // 背景天球 r≤55，留余量
  camera.position.set(3.6, 2.3, 3.6);

  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setClearColor(0x03060d, 1);
  container.appendChild(renderer.domElement);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.minDistance = 0.04;
  controls.maxDistance = 24;
  controls.autoRotate = true;
  controls.autoRotateSpeed = 0.22;
  // 鼠标映射：左键旋转 / 中键平移 / 右键平移（滚轮仍是缩放）
  controls.mouseButtons = {
    LEFT: THREE.MOUSE.ROTATE,
    MIDDLE: THREE.MOUSE.PAN,
    RIGHT: THREE.MOUSE.PAN,
  };
  controls.panSpeed = 1.0;
  controls.screenSpacePanning = true;

  // ---- 星尘背景
  const dustGeo = new THREE.BufferGeometry();
  const dustCount = 1500;
  const dust = new Float32Array(dustCount * 3);
  {
    // 两条对数旋臂 + 薄盘：均匀随机撒点没有形态，看着只是噪点
    const ARMS = 2;
    for (let i = 0; i < dustCount; i++) {
      const arm = i % ARMS;
      const t = Math.pow(Math.random(), 0.75);            // 内密外疏
      const r = 1.15 + t * 5.2;
      const theta = arm * (Math.PI * 2 / ARMS) + Math.log(r / 1.6) * 2.1 + (Math.random() - 0.5) * 0.55;
      const y = (Math.random() - 0.5) * (0.35 + 0.9 * (r / 8));   // 盘厚
      dust[i * 3] = Math.cos(theta) * r + (Math.random() - 0.5) * 0.5;
      dust[i * 3 + 1] = y;
      dust[i * 3 + 2] = Math.sin(theta) * r + (Math.random() - 0.5) * 0.5;
    }
  }
  dustGeo.setAttribute('position', new THREE.BufferAttribute(dust, 3));
  const dustTex = makeGlowTexture();
  const dustMat = new THREE.PointsMaterial({
    color: 0x86c8ff, size: 0.028, transparent: true, opacity: 0.5,
    blending: THREE.AdditiveBlending, depthWrite: false, map: dustTex,
    sizeAttenuation: true,
  });
  // 装在 group 里，帧循环里慢慢转 —— 星河才会「流」
  const dustGroup = new THREE.Group();
  dustGroup.add(new THREE.Points(dustGeo, dustMat));
  scene.add(dustGroup);

  // ---- 背景星空：两层星点（远景点 + 亮星），慢慢自转，给纯黑背景铺一层底
  const starFieldGroup = new THREE.Group();
  {
    const tex = makeGlowTexture();

    // ① 远景细星：多、小、极淡，铺满整个天球
    const N_FAR = 3200;
    const farPos = new Float32Array(N_FAR * 3);
    const farCol = new Float32Array(N_FAR * 3);
    const c = new THREE.Color();
    for (let i = 0; i < N_FAR; i++) {
      const r = 25 + Math.random() * 30;               // 大天球：相机永远在球内，任何方向都有
      const u = Math.random() * 2 - 1;
      const th = Math.random() * Math.PI * 2;
      const sp = Math.sqrt(Math.max(0, 1 - u * u));
      farPos[i * 3] = r * sp * Math.cos(th);
      farPos[i * 3 + 1] = r * u * 0.75;
      farPos[i * 3 + 2] = r * sp * Math.sin(th);
      // 冷白 / 淡蓝 / 偶尔一点暖黄
      const t = Math.random();
      c.setRGB(0.72 + t * 0.28, 0.80 + t * 0.18, 1.0);
      if (Math.random() < 0.08) c.setRGB(1.0, 0.92, 0.78);
      const b = 0.25 + Math.random() * 0.55;
      farCol[i * 3] = c.r * b;
      farCol[i * 3 + 1] = c.g * b;
      farCol[i * 3 + 2] = c.b * b;
    }
    const farGeo = new THREE.BufferGeometry();
    farGeo.setAttribute('position', new THREE.BufferAttribute(farPos, 3));
    farGeo.setAttribute('color', new THREE.BufferAttribute(farCol, 3));
    starFieldGroup.add(new THREE.Points(farGeo, new THREE.PointsMaterial({
      size: 2.4, vertexColors: true, transparent: true, opacity: 1.0,
      blending: THREE.AdditiveBlending, depthWrite: false, map: tex,
      sizeAttenuation: false,          // 像素单位：远处星点才不会缩没
      fog: false,                      // 背景星在雾外，别被 FogExp2 吃掉
    })));

    // ② 亮星：少、大、带一点辉光
    const N_NEAR = 140;
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
      nearCol[i * 3] = c.r;
      nearCol[i * 3 + 1] = c.g;
      nearCol[i * 3 + 2] = c.b;
    }
    const nearGeo = new THREE.BufferGeometry();
    nearGeo.setAttribute('position', new THREE.BufferAttribute(nearPos, 3));
    nearGeo.setAttribute('color', new THREE.BufferAttribute(nearCol, 3));
    starFieldGroup.add(new THREE.Points(nearGeo, new THREE.PointsMaterial({
      size: 4.0, vertexColors: true, transparent: true, opacity: 1.0,
      blending: THREE.AdditiveBlending, depthWrite: false, map: tex,
      sizeAttenuation: false,
      fog: false,
    })));
  }
  scene.add(starFieldGroup);

  // ---- 背景星云：两团极淡的雾，给纯黑背景一点体积感
  {
    const nebTex = makeGlowTexture();
    const NEB: Array<[number, number, number, number, number]> = [
      // x, y, z, scale, opacity
      [-3.2, 1.4, -5.0, 12, 0.16],
      [4.0, -1.8, -6.5, 14, 0.13],
      [0.0, 2.6, -8.0, 18, 0.10],
    ];
    for (const [x, y, z, sc, op] of NEB) {
      const sp = new THREE.Sprite(new THREE.SpriteMaterial({
        map: nebTex, color: 0x33559e, transparent: true, opacity: op,
        blending: THREE.AdditiveBlending, depthWrite: false,
      }));
      sp.position.set(x, y, z);
      sp.scale.setScalar(sc);
      scene.add(sp);
    }
  }

  // ---- 星体：Points + 光晕贴图
  const indexOf = new Map<string, number>();
  // 飞向黑洞的节点：index → 起始位置与时间。逐帧插值，只改 BufferAttribute。
  let minRetention = 0;
  let lastCutoff = 0;
  // tyFlow：不可见的锚点（提供坐标）+ 它们的元数据
  const anchorPos = new Map<string, { x: number; y: number; z: number }>();
  const anchorMeta = new Map<string, { t: number; destiny: string; tx: number; ty: number; tz: number }>();
  // 平滑过渡：目标位置 + 每帧 lerp（数据刷新时粒子滑过去，而不是瞬移）
  const tweenTarget = new Map<number, { x: number; y: number; z: number }>();

  // ── 引力吸纳：每条记忆粒子从「自己的原位」出发，沿螺旋加速轨迹飞进黑洞 ──
  interface Flyer {
    id: string;           // 节点 id（吞噬后回调后端）
    idx: number;          // 主粒子索引（复用已有粒子，绝不新建）
    x0: number; y0: number; z0: number;   // 起点：它原本在星环上的位置
    t0: number; dur: number;              // 起止时间
    delay: number;                        // 错峰延迟：让球一颗颗走，而不是齐射
    spin: number;                         // 螺旋方向 ±1
    trail: number[];                      // 尾迹：最近 N 个世界坐标
    slot: number;                         // 尾迹槽位（固定，不随数组增删移动）
  }
  const flyers: Flyer[] = [];
  const FLY_MS = 2200;                    // 飞行时长
  const FLY_MAX = 40;                     // 同屏最多几个（防卡）
  const TRAIL_LEN = 14;                   // 尾迹采样点

  // 尾迹点云（独立于主粒子：主粒子飞，尾迹拖在身后）
  const trailCount = FLY_MAX * TRAIL_LEN;
  const trailPos = new Float32Array(trailCount * 3).fill(9999);
  const trailCol = new Float32Array(trailCount * 3);
  const trailGeo = new THREE.BufferGeometry();
  trailGeo.setAttribute('position', new THREE.BufferAttribute(trailPos, 3));
  trailGeo.setAttribute('color', new THREE.BufferAttribute(trailCol, 3));
  const trailMat = new THREE.PointsMaterial({
    size: 0.028, vertexColors: true, transparent: true, opacity: 0.85,
    blending: THREE.AdditiveBlending, depthWrite: false, map: makeGlowTexture(),
  });
  const trailPoints = new THREE.Points(trailGeo, trailMat);
  trailPoints.frustumCulled = false;

  // 吞噬闪光：黑洞处的扩张圆环
  interface Ripple { sp: THREE.Sprite; t0: number; dur: number }
  const ripples: Ripple[] = [];
  const BH_X = 2.35 * SPREAD, BH_Y = 0.02 * SPREAD, BH_Z = 0 * SPREAD;

  /** 在黑洞中心放一圈扩张的光环（吞噬瞬间的反馈） */
  function spawnRipple() {
    if (ripples.length > 8) return;
    const mat = new THREE.SpriteMaterial({
      map: makeGlowTexture(), color: 0xffd27a, transparent: true,
      opacity: 0.95, blending: THREE.AdditiveBlending, depthWrite: false,
    });
    const sp = new THREE.Sprite(mat);
    sp.position.set(BH_X, BH_Y, BH_Z);
    sp.scale.set(0.4, 0.4, 1);
    worldGroup.add(sp);
    ripples.push({ sp, t0: performance.now(), dur: 700 });
  }

  /** 把若干粒子送上飞向黑洞的路。必须传「已存在的粒子索引」。 */
  function launchFlyers(items: Array<{ idx: number; id: string }>) {
    const now = performance.now();
    // 错峰：已经在飞的球排出的队尾时间，新球依次往后延，最多 2.5 秒铺开
    let tail = 0;
    for (const f of flyers) {
      const end = f.t0 + f.delay + f.dur;
      if (end > tail) tail = end;
    }
    let order = 0;
    for (const it of items) {
      const { idx, id } = it;
      if (flyers.some((f) => f.idx === idx)) continue;
      if (flyers.length >= FLY_MAX) break;
      // 每颗间隔 110ms；如果队列已经排到很后面，就压缩间隔，避免排到很久以后
      const delay = Math.max(0, Math.min(tail - now, 2500)) + order * 110;
      order++;
      // 找一个没被占用的尾迹槽位（用下标做槽位会在 splice 后错位，导致尾迹残留）
      const used = new Set(flyers.map((f) => f.slot));
      let slot = 0;
      while (used.has(slot) && slot < FLY_MAX) slot++;
      if (slot >= FLY_MAX) break;
      for (let ti = 0; ti < TRAIL_LEN; ti++) trailPos[(slot * TRAIL_LEN + ti) * 3] = 9999;
      flyers.push({
        id, idx,
        x0: positions[idx * 3], y0: positions[idx * 3 + 1], z0: positions[idx * 3 + 2],
        t0: now, dur: FLY_MS,
        delay, slot,
        spin: Math.random() > 0.5 ? 1 : -1,
        trail: [],
      });
    }
  }
  const positions = new Float32Array(nodes.length * 3);
  const colors = new Float32Array(nodes.length * 3);   // 光晕颜色
  const cores = new Float32Array(nodes.length * 3);    // 内核颜色
  const sizes = new Float32Array(nodes.length);
  const color = new THREE.Color();
  // 关系星河布局 = 后端 persona_view 产出的「群核心 + 星尘」：scope 节点 size 是 6~10。
  // 普通记忆星海里的 scope 节点 size=1（只是群锚点），必须走回旧公式，不被本文件改动波及。
  const relation = nodes.some((n) => n.kind === 'persona')
    || nodes.some((n) => n.kind === 'scope' && (n.starfield?.size ?? 0) >= RELATION_CORE_MIN_SIZE);
  // 关系星河是「点选主导」的视图：默认不自动旋转，目标静止才好点中；
  // 普通记忆星海保持原来的缓慢自转。
  controls.autoRotate = !relation;

  // 核心分级区间：优先用群核心(scope)的 size 现算（后端从 6~10 调到 8~14 也不用改前端）；
  // 只有一颗人格金核时退回 persona 的 size；都拿不到才用兜底常量。
  let coreMin = Infinity;
  let coreMax = -Infinity;
  const feedCoreSize = (sz: number) => {
    if (sz <= 0) return;
    if (sz < coreMin) coreMin = sz;
    if (sz > coreMax) coreMax = sz;
  };
  for (const n of nodes) {
    if (n.kind === 'scope') feedCoreSize(n.starfield?.size ?? 0);
  }
  if (!Number.isFinite(coreMin)) {
    for (const n of nodes) {
      if (n.kind === 'persona') feedCoreSize(n.starfield?.size ?? 0);
    }
  }
  if (!Number.isFinite(coreMin) || !Number.isFinite(coreMax)) {
    coreMin = CORE_SIZE_FALLBACK_MIN;
    coreMax = CORE_SIZE_FALLBACK_MAX;
  } else if (coreMax - coreMin < 0.5) {
    // 只有一档核心（例如只剩一颗金色人核）：把它当作最高等级，保证够大够亮
    coreMin = Math.max(0.5, coreMax - 6);
  }
  // 记忆生命周期：白洞(诞生) → 短期环(停留) → 归宿(长期/创伤/事件/黑洞)
  // 每个粒子的位置由它的 life(0→1) 决定，不是播放动画。
  const WH_X = -2.35 * SPREAD, WH_Y = 0.02 * SPREAD, WH_Z = 0 * SPREAD;

  nodes.forEach((n, i) => {
    indexOf.set(n.id, i);
    const sf = n.starfield!;
    let _x = sf.x * SPREAD, _y = sf.y * SPREAD, _z = sf.z * SPREAD;

    if (n.category === 'short' && (n as any).life !== undefined) {
      const life = Math.max(0, Math.min(1, Number((n as any).life)));
      const tx = Number((n as any).targetX ?? 0) * SPREAD;
      const ty = Number((n as any).targetY ?? 0) * SPREAD;
      const tz = Number((n as any).targetZ ?? 0) * SPREAD;
      const ringX = sf.x * SPREAD, ringY = sf.y * SPREAD, ringZ = sf.z * SPREAD;

      if (life < 0.28) {
        // ① 刚诞生：白洞 → 短期环（还在路上）
        const k = life / 0.28;
        const e = k * k * (3 - 2 * k);        // smoothstep
        _x = WH_X + (ringX - WH_X) * e;
        _y = WH_Y + (ringY - WH_Y) * e;
        _z = WH_Z + (ringZ - WH_Z) * e;
      } else if (life < 0.62) {
        // ② 停在短期环上绕着核心转
        _x = ringX; _y = ringY; _z = ringZ;
      } else {
        // ③ 开始分流：向自己的归宿加速飞去
        const k = Math.min(1, (life - 0.62) / 0.38);
        const e = k * k * k;                  // 越飞越快（引力感）
        _x = ringX + (tx - ringX) * e;
        _y = ringY + (ty - ringY) * e;
        _z = ringZ + (tz - ringZ) * e;
      }
    }

    positions[i * 3] = _x;
    positions[i * 3 + 1] = _y;
    positions[i * 3 + 2] = _z;
    const traumatized = !!(n as unknown as { trauma?: { charge?: number } }).trauma;
    // 统一走 nodeGlowColor：scope 取 trend 色，memory 取后端暗色星尘色，其余按情绪/分类
    const glow = traumatized ? 0xb03030 : nodeGlowColor(n);   // 创伤：白底 + 暗红光
    const b = brightnessOf(n, relation);
    color.set(glow);
    colors[i * 3] = Math.min(1, color.r * b);
    colors[i * 3 + 1] = Math.min(1, color.g * b);
    colors[i * 3 + 2] = Math.min(1, color.b * b);
    const c2 = new THREE.Color(0xffffff);   // 内核恒为白：光晕决定星体颜色，白核最亮
    cores[i * 3] = c2.r;
    cores[i * 3 + 1] = c2.g;
    cores[i * 3 + 2] = c2.b;
    // sizes 统一在算完密度后填
  });

  // 每个节点归属哪个 scope（沿 parentId 往上找）。
  // 悬臂布局、scope 轨道环、标签权重都用这张表 —— 别各写一套判定
  // （之前用 id 前缀判断，topic/memory 的 id 不带前缀 → 全判错）
  const rootScopeOf: number[] = new Array(nodes.length).fill(-1);
  // scope id → 下标（节点的 scope 字段是 'group_xxx' 这种，scope 节点 id 是 'scope:group_xxx'）
  const scopeById = new Map<string, number>();
  for (let i = 0; i < nodes.length; i++) {
    if (nodes[i].kind === 'scope') scopeById.set(nodes[i].id, i);
  }
  for (let i = 0; i < nodes.length; i++) {
    // ① 沿 parentId 往上找
    let cur: number | undefined = i;
    for (let hop = 0; hop < 8 && cur !== undefined; hop++) {
      if (nodes[cur].kind === 'scope') { rootScopeOf[i] = cur; break; }
      const pid: string | null = nodes[cur].parentId;
      cur = pid ? indexOf.get(pid) : undefined;
    }
    // ② 链断了：用节点自带的 scope 字段兜底（很多 topic/memory 的父节点根本没进图）
    if (rootScopeOf[i] < 0) {
      const sc = (nodes[i] as unknown as { scope?: string }).scope;
      if (sc) {
        const idx = scopeById.get(sc.startsWith('scope:') ? sc : `scope:${sc}`);
        if (idx !== undefined) rootScopeOf[i] = idx;
      }
    }
    if (rootScopeOf[i] < 0) rootScopeOf[i] = i;
  }

  // 近邻排斥：UMAP 会把语义相近的记忆投到几乎同一点，视觉上像重复的球。
  // 这里做一次轻量的局部松弛，只把过近的点推开，不动整体形状。
  if (nodes.length > 1) {
    const MIN_SEP = 0.045;
    for (let iter = 0; iter < 24; iter++) {
      let moved = false;
      for (let i = 0; i < nodes.length; i++) {
        for (let j = i + 1; j < nodes.length; j++) {
          const dx = positions[j * 3] - positions[i * 3];
          const dy = positions[j * 3 + 1] - positions[i * 3 + 1];
          const dz = positions[j * 3 + 2] - positions[i * 3 + 2];
          const d = Math.sqrt(dx * dx + dy * dy + dz * dz);
          if (d >= MIN_SEP) continue;
          let ux = dx, uy = dy, uz = dz;
          if (d < 1e-6) {
            // 完全重合：用序号哈希给一个固定方向，避免两球永远叠死
            const h = (i * 2654435761) >>> 0;
            ux = ((h & 255) / 255 - 0.5);
            uy = (((h >> 8) & 255) / 255 - 0.5);
            uz = (((h >> 16) & 255) / 255 - 0.5);
            const len = Math.sqrt(ux * ux + uy * uy + uz * uz) || 1;
            ux /= len; uy /= len; uz /= len;
          } else {
            ux /= d; uy /= d; uz /= d;
          }
          const push = (MIN_SEP - d) * 0.5;
          positions[i * 3] -= ux * push;
          positions[i * 3 + 1] -= uy * push;
          positions[i * 3 + 2] -= uz * push;
          positions[j * 3] += ux * push;
          positions[j * 3 + 1] += uy * push;
          positions[j * 3 + 2] += uz * push;
          moved = true;
        }
      }
      if (!moved) break;
    }
  }

  // 局部密度：同样明亮的粒子，挤在一起时自动变小，避免发光拉满糊成一团
  const densityScale = new Float32Array(nodes.length).fill(1);
  {
    const total = nodes.length;
    if (total > 4) {
      const k = Math.min(6, total - 1);
      const near = new Float32Array(total);
      for (let i = 0; i < total; i++) {
        const ds: number[] = [];
        for (let j = 0; j < total; j++) {
          if (i === j) continue;
          const dx = positions[j * 3] - positions[i * 3];
          const dy = positions[j * 3 + 1] - positions[i * 3 + 1];
          const dz = positions[j * 3 + 2] - positions[i * 3 + 2];
          ds.push(dx * dx + dy * dy + dz * dz);
        }
        ds.sort((a, b) => a - b);
        let sum = 0;
        for (let t = 0; t < k; t++) sum += Math.sqrt(ds[t]);
        near[i] = sum / k;
      }
      const med = [...near].sort((a, b) => a - b)[Math.floor(total / 2)] || 1;
      for (let i = 0; i < total; i++) {
        // 比中位数近的缩小、远的放大，指数 0.7 让过渡柔一点
        densityScale[i] = Math.max(0.42, Math.min(1.3, Math.pow(near[i] / med, 0.7)));
      }
    }
  }
  // 关系星河的世界尺度由后端布局（RING_RADIUS / 星尘半径）× SPREAD 决定，当前约 7~15；
  // 普通记忆星海只有 ~0.9。镜头映射必须按这个尺度自适应，否则默认 zoom=50 只看得到一个核心。
  let sceneRadius = 0;
  for (let i = 0; i < nodes.length; i++) {
    const r = Math.hypot(positions[i * 3], positions[i * 3 + 1], positions[i * 3 + 2]);
    if (r > sceneRadius) sceneRadius = r;
  }
  // 「只有一颗金核」的人格布局 sceneRadius=0，给一个等效尺度，镜头才不会怼进光晕里
  const layoutRadius = relation ? Math.max(sceneRadius, PERSONA_ONLY_RADIUS) : sceneRadius;
  // 关系星河默认视距：整片星河（半径 sceneRadius）要完整装进 fov，
  // d = sceneRadius / tan(fov/2) * 1.05（fov=55° 时 ≈ sceneRadius*2.01）。
  const fovTan = Math.max(0.05, Math.tan(THREE.MathUtils.degToRad(camera.fov) / 2));
  // 只有一颗球（sceneRadius≈0）时不要把镜头贴脸：给一个固定近距离正对它
  const fitDistance = sceneRadius > 0.6
    ? Math.max(1, (sceneRadius / fovTan) * 1.05)
    : 4.5;
  if (relation) {
    camera.far = Math.max(camera.far, fitDistance * 3);
    camera.updateProjectionMatrix();
    controls.maxDistance = Math.max(controls.maxDistance, fitDistance * 1.8);
  }
  // 悬臂布局把星河撑到半径 ~2.8：默认镜头也要跟着拉远，
  // 否则只看到中心那一坨，根本看不出螺旋
  if (sceneRadius > 1.6) {
    camera.far = Math.max(camera.far, fitDistance * 3);
    camera.updateProjectionMatrix();
    controls.maxDistance = Math.max(controls.maxDistance, fitDistance * 1.8);
    const dir = camera.position.clone().sub(controls.target);
    if (dir.lengthSq() > 1e-6) {
      camera.position.copy(controls.target).add(dir.normalize().multiplyScalar(fitDistance * 1.05));
    }
  }

  /** 视距映射：0 → 看全整片星河，0.5 → 默认构图，1 → 贴脸单核。 */
  const zoomDistance = (v01: number): number => {
    const lo = 0.05;
    const hi = 24;
    const base = lo * Math.pow(hi / lo, 1 - clamp01(v01));
    // 普通星海沿用原曲线（作者按 ~0.9 半径调过手感），不受关系星河影响
    if (!relation || layoutRadius < 1.2) {
      // 但悬臂布局把整片撑到半径 ~2.8：把老曲线整体放大，
      // 使 v01=0.5 正好是「装下整片」。只做等比缩放，曲线形状（手感）不变，
      // 也不会和 setZoom 的调用顺序打架（上次改成 piecewise 直接把画面搞空）
      if (layoutRadius > 1.6) {
        const midOld = 0.05 * Math.pow(24 / 0.05, 0.5);   // 老曲线在 v01=0.5 处的距离 ≈1.095
        return base * (fitDistance / midOld) * 0.96;
      }
      return base;
    }
    const near = Math.max(0.16, layoutRadius * 0.06);
    const fit = fitDistance;                       // 默认：整片星河装进画面
    const far = Math.max(24, fit * 1.5, layoutRadius * 2.4);   // 拉到最远：看清群间排斥结构
    const t = clamp01(v01);
    return t >= 0.5
      ? near * Math.pow(fit / near, (1 - t) / 0.5)   // t=0.5 → fit，t=1 → near
      : fit * Math.pow(far / fit, (0.5 - t) / 0.5);  // t=0.5 → fit，t=0 → far
  };

  // 统一的星体尺寸：关系星河里核心恒星走 SCOPE_A_SIZE（0.115~0.200），记忆星尘走
  // MEMORY_A_SIZE（0.0165~0.0208，约 1/6）。密度缩放只作用于星尘，核心不再被密度系数
  // 压缩/放大；普通记忆星海则保持旧公式（全部走密度缩放）。
  const sizeOf = (n: MemoryNode, i: number, grow = 1): number =>
    nodeBaseSize(n, relation, coreMin, coreMax) * grow *
    (relation && isCoreKind(n) ? 1 : densityScale[i]);
  nodes.forEach((n, i) => {
    sizes[i] = sizeOf(n, i);
  });

  const nodeGeo = new THREE.BufferGeometry();
  nodeGeo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
  nodeGeo.setAttribute('color', new THREE.BufferAttribute(colors, 3));
  nodeGeo.setAttribute('aCore', new THREE.BufferAttribute(cores, 3));
  nodeGeo.setAttribute('aSize', new THREE.BufferAttribute(sizes, 1));
  const visArr = new Float32Array(nodes.length).fill(1);
  nodeGeo.setAttribute('aVis', new THREE.BufferAttribute(visArr, 1));
  // 创伤节点：1 = 暗红脉冲呼吸灯
  const traumaArr = new Float32Array(nodes.length);
  nodes.forEach((n, i) => {
    traumaArr[i] = (n as unknown as { trauma?: unknown }).trauma ? 1 : 0;
  });
  nodeGeo.setAttribute('aTrauma', new THREE.BufferAttribute(traumaArr, 1));

  const nodeMat = new THREE.ShaderMaterial({
    uniforms: {
      uTex: { value: makeGlowTexture() },
      uTime: { value: 0 },
      uGlow: { value: 0.6 },
      uDrift: { value: 1 },
    },
    vertexShader: `
      attribute float aSize;
      attribute float aVis;
      attribute float aTrauma;
      attribute vec3 color;
      attribute vec3 aCore;
      uniform float uTime;
      uniform float uDrift;   // 0 = 不浮动（否则端点与连线对不上）
      varying vec3 vColor;
      varying vec3 vCore;
      varying float vVis;
      varying float vDepth;
      varying float vPx;
      void main() {
        vColor = color;
        vCore = aCore;
        vVis = aVis;
        // 星云呼吸：每个粒子绕自己的位置做微小正弦浮动
        vec3 drift = uDrift * vec3(
          sin(uTime * 0.45 + position.y * 2.7) * 0.012,
          cos(uTime * 0.37 + position.x * 2.3) * 0.012,
          sin(uTime * 0.41 + position.z * 2.1) * 0.010
        );
        vec4 mv = modelViewMatrix * vec4(position + drift, 1.0);
        float pulse = 1.0 + 0.12 * sin(uTime * 1.8 + position.x * 12.0 + position.y * 8.0)
                    + aTrauma * 0.38 * sin(uTime * 3.2 + position.z * 5.0);
        float px = min(aSize * pulse * (1400.0 / -mv.z), 260.0);        // 贴脸时限制最大点尺寸
        gl_PointSize = px;
        vDepth = -mv.z;                                                 // 给片元做大气透视
        vPx = px;                                                       // 星芒只在够大的星上出
        gl_Position = projectionMatrix * mv;
      }
    `,
    fragmentShader: `
      uniform sampler2D uTex;
      uniform float uGlow;
      varying vec3 vColor;
      varying vec3 vCore;
      varying float vVis;
      varying float vDepth;
      varying float vPx;
      void main() {
        if (vVis < 0.5) discard;
        vec2 uv = gl_PointCoord - 0.5;
        float d = length(uv) * 2.0;
        if (d > 1.0) discard;

        // uGlow 0~1：同时放大光晕范围和亮度；0 时只剩一点点核
        float soft = 2.4 + (1.0 - uGlow) * 3.2;
        float glowMask = exp(-d * d * soft) * (0.25 + uGlow * 1.5);
        float coreMask = smoothstep(0.26, 0.0, d) * (0.55 + uGlow * 0.75);

        // 十字星芒：只有亮星（大/近）才明显，远的小点不会被撑成噪点
        float spike = (exp(-abs(uv.y) * 40.0) * exp(-uv.x * uv.x * 22.0)
                     + exp(-abs(uv.x) * 40.0) * exp(-uv.y * uv.y * 22.0));
        // 只在大星上出芒（<22px 的小点保持圆润），否则整片星空变成钻石地
        spike *= smoothstep(30.0, 62.0, vPx) * 0.5;

        vec3 col = vColor * glowMask + vCore * coreMask + mix(vColor, vec3(1.0), 0.6) * spike;

        // 大气透视：越远越偏冷、越暗，纵深立刻出来
        float far = smoothstep(9.0, 30.0, vDepth);
        col = mix(col, vec3(0.34, 0.46, 0.72) * (0.35 + 0.65 * max(col.r, max(col.g, col.b))), far * 0.55);
        col *= 1.0 - far * 0.28;

        float a = max(max(glowMask, coreMask), spike * 0.6);
        if (a < 0.02) discard;
        gl_FragColor = vec4(col, a);
      }
    `,
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
  });

  // 关系/人格视图：关掉粒子浮动，否则球在飘、线是钉死的，端点永远差 ~0.012
  if (relation) nodeMat.uniforms.uDrift.value = 0;
  const nodePoints = new THREE.Points(nodeGeo, nodeMat);
  const worldGroup = new THREE.Group();
  worldGroup.add(trailPoints);

  // ── 记忆流粒子（tyFlow 的可见粒子）：从白洞喷出，飞向各自的锚点 ──
  const FLOW_MAX = 900;
  const mflowPos = new Float32Array(FLOW_MAX * 3).fill(9999);
  const mflowCol = new Float32Array(FLOW_MAX * 3);
  const mflowGeo = new THREE.BufferGeometry();
  mflowGeo.setAttribute('position', new THREE.BufferAttribute(mflowPos, 3));
  mflowGeo.setAttribute('color', new THREE.BufferAttribute(mflowCol, 3));
  const mflowMat = new THREE.PointsMaterial({
    size: 0.020, vertexColors: true, transparent: true, opacity: 0.9,
    blending: THREE.AdditiveBlending, depthWrite: false, map: makeGlowTexture(),
  });
  const mflowPoints = new THREE.Points(mflowGeo, mflowMat);
  mflowPoints.frustumCulled = false;
  worldGroup.add(mflowPoints);

  const FLOW_COLORS: Record<string, number> = {
    longterm: 0x4a9eff, trauma: 0xff3b30, episode: 0x3ddc84, blackhole: 0x8b1a1a,
  };

  /** 每帧按「时间指针」重算所有流动粒子的位置 */
  function updateFlow(cutoffMs: number) {
    const nowMs = cutoffMs > 0 ? cutoffMs : Date.now();
    let n = 0;
    for (const [id, meta] of anchorMeta) {
      if (n >= FLOW_MAX) break;
      const ring = anchorPos.get(id);
      if (!ring) continue;

      // ① 还没出生 → 不画
      if (nowMs < meta.t) continue;

      const ageH = (nowMs - meta.t) / 3600000;
      const life = Math.min(1, ageH / 24);

      // ② 出生：白洞 → 短期锚点（12% 生命周期）
      // ③ 停留：短期锚点上（13%）
      // ④ 分流：短期锚点 → 命运锚点（75%）
      let px: number, py: number, pz: number;
      if (life < 0.12) {
        const k = life / 0.12;
        const e = k * k * (3 - 2 * k);
        let hsh = 0;
        for (let ci = 0; ci < id.length; ci++) hsh = (hsh * 31 + id.charCodeAt(ci)) | 0;
        const jr = 0.08 + ((Math.abs(hsh) % 100) / 100) * 0.20;
        const ja = ((Math.abs(hsh >> 7) % 360) / 360) * Math.PI * 2;
        px = WH_X + (ring.x - WH_X) * e + Math.cos(ja) * jr * (1 - e);
        py = WH_Y + (ring.y - WH_Y) * e;
        pz = WH_Z + (ring.z - WH_Z) * e + Math.sin(ja) * jr * (1 - e);
      } else if (life < 0.25) {
        px = ring.x; py = ring.y; pz = ring.z;
      } else {
        const k = Math.min(1, (life - 0.25) / 0.75);
        const e = Math.sqrt(k);
        const tg = anchorPos.get(`__dest_${meta.destiny}_${id}`) ?? {
          x: meta.tx, y: meta.ty, z: meta.tz,
        };
        px = ring.x + (tg.x - ring.x) * e;
        py = ring.y + (tg.y - ring.y) * e;
        pz = ring.z + (tg.z - ring.z) * e;
      }

      mflowPos[n * 3] = px;
      mflowPos[n * 3 + 1] = py;
      mflowPos[n * 3 + 2] = pz;
      const c = new THREE.Color(FLOW_COLORS[meta.destiny] ?? 0xbfe0ff);
      const fade = life < 0.25 ? 1.0 : Math.max(0.15, 1.0 - (life - 0.25));
      mflowCol[n * 3] = c.r * fade;
      mflowCol[n * 3 + 1] = c.g * fade;
      mflowCol[n * 3 + 2] = c.b * fade;
      n++;
    }
    // 没用的点藏起来
    for (let i = n; i < FLOW_MAX; i++) mflowPos[i * 3] = 9999;
    mflowGeo.attributes.position.needsUpdate = true;
    mflowGeo.attributes.color.needsUpdate = true;
  }

  // ── 白洞：奇点喷流（billboard HDR 光核 + 双螺旋喷流 + 加色预乘）──
  const whNode = nodes.find((n) => n.category === 'whitehole');
  let whiteHole: { group: THREE.Group; update: (t: number, dt?: number) => void } | null = null;
  if (whNode?.starfield) {
    const wh = createWhiteHole({
      position: new THREE.Vector3(
        whNode.starfield.x * SPREAD,
        whNode.starfield.y * SPREAD,
        whNode.starfield.z * SPREAD,
      ),
      size: 0.1,
      jetLength: 0.9,
      jetCone: 0.2,
    });
    // 点大小跟着视口走，否则缩放窗口时喷流会变粗/变细
    wh.setViewport(container.clientHeight || 520, 55, window.devicePixelRatio || 1);
    worldGroup.add(wh.group);
    whiteHole = wh;
  }

  // ── 黑洞：粒子吸积盘系统（位置取后端给的 BLACKHOLE_POS）──
  const bhNode = nodes.find((n) => n.category === 'blackhole');
  let blackHole: ReturnType<typeof createBlackHoleGPU> | null = null;
  if (bhNode?.starfield) {
    blackHole = createBlackHoleGPU({
      position: new THREE.Vector3(
        bhNode.starfield.x * SPREAD,
        bhNode.starfield.y * SPREAD,
        bhNode.starfield.z * SPREAD,
      ),
      radius: 0.13,
      diskOuter: 4.2,
    });
    blackHole.setViewport(container.clientHeight || 520, 55, window.devicePixelRatio || 1);
    worldGroup.add(blackHole.group);
  }
  scene.add(worldGroup);

  worldGroup.add(nodePoints);

  // ---- 星河名标签：scope 节点上方挂一个 HTML 标签（CSS2D 比 Sprite 文字清晰）
  if (getComputedStyle(container).position === 'static') container.style.position = 'relative';
  const labelRenderer = new CSS2DRenderer();
  labelRenderer.setSize(container.clientWidth || 800, container.clientHeight || 520);
  const labelDom = labelRenderer.domElement;
  labelDom.style.position = 'absolute';
  labelDom.style.inset = '0';
  labelDom.style.pointerEvents = 'none';
  container.appendChild(labelDom);

  const scopeLabels: CSS2DObject[] = [];
  const labelWeight: number[] = [];
  nodes.forEach((n, i) => {
    // 特质行星、念头/梦境小球都不挂常驻标签：只显示「池名」节点（kind=category），
    // 点小球后右侧面板看内容。
    if (n.kind !== 'scope' && n.kind !== 'persona' && n.kind !== 'category') return;
    if ((n as unknown as { hideLabel?: boolean }).hideLabel || !n.summary) return;
    const center = n.kind === 'persona';
    const el = document.createElement('div');
    el.textContent = n.summary || (center ? '我' : n.kind === 'category' ? '分类' : '星河');
    el.style.cssText = center
      ? 'color:#ffe6a8;font-size:16px;font-weight:600;line-height:1;padding:4px 10px;border-radius:8px;' +
        'background:rgba(40,28,10,.55);border:1px solid rgba(255,215,120,.45);' +
        'white-space:nowrap;text-shadow:0 0 8px rgba(0,0,0,.9);' +
        'font-family:system-ui,-apple-system,sans-serif;pointer-events:none;'
      : 'color:#a8d4ff;font-size:12px;line-height:1;padding:3px 7px;border-radius:6px;' +
        'background:rgba(6,16,28,.55);border:1px solid rgba(90,160,220,.35);' +
        'white-space:nowrap;text-shadow:0 0 6px rgba(0,0,0,.9);' +
        'font-family:system-ui,-apple-system,sans-serif;pointer-events:none;';
    const obj = new CSS2DObject(el);
    const lift = center ? 0.12 : 0.055;
    obj.position.set(positions[i * 3], positions[i * 3 + 1] + lift, positions[i * 3 + 2]);
    worldGroup.add(obj);
    scopeLabels.push(obj);
    // 权重 = 它下面挂了多少节点（成员多的星云名优先显示）
    let members = 1;
    for (let k = 0; k < nodes.length; k++) if (i !== k && rootScopeOf[k] === i) members++;
    if (n.kind === 'persona') members += 9999;     // 主核永远优先
    labelWeight.push(members);
  });

  // ---- 群核心光晕：核心恒星的「激素光圈」
  // 尺寸与 sf.size(6~10) 解耦：只按核心分级温和放大到 0.68~1.30 世界单位，
  // 否则 size=10 会得到 4.6，把半径 ≤0.72 的整片记忆星尘盘直接吞掉。
  interface ScopeGlow { sp: THREE.Sprite; base: number; amp: number; speed: number; phase: number; trend: string; isBlackhole?: boolean }
  const scopeGlows: ScopeGlow[] = [];
  let haloScale = 1;   // 光晕滑块（0~2）：乘在每帧呼吸之上，否则会被呼吸动画覆盖
  nodes.forEach((n, i) => {
    // 念头池也要光晕（白色虚影），否则只剩几个干瘪的点
    const isThoughtNode = n.category === 'thought' || n.category === 'dream';
    const isBlackhole = n.category === 'blackhole';
    // 黑洞由独立粒子系统负责（createBlackHole），这里跳过它
    if (isBlackhole) return;
    if (!isCoreKind(n) && !isThoughtNode) return;
    const isPersona = n.kind === 'persona';
    const sf = n.starfield!;
    const trend = (isPersona ? 'persona' : ((n as unknown as { trend?: string }).trend || 'neutral'));
    // 激素配色：关系星河 cort 暗红 / oxy 粉 / neutral 蓝白 / persona 金；
    // 普通记忆星海的群锚点沿用旧配色。
    const col = isThoughtNode ? 0xf2f7ff
      : isPersona ? 0xffd77a
      : !relation
        ? (trend === 'cort' ? 0xff4a3a : trend === 'oxy' ? 0xff7eb6 : 0x9fd8ff)
        : (trend === 'cort' ? 0xd0362a : trend === 'oxy' ? 0xff7eb6 : 0xbfe4ff);
    const base = isThoughtNode ? 0.18 : relation ? (isPersona ? 0.5 : 0.7) : (isPersona ? 0.22 : 0.5);
    const mat = new THREE.SpriteMaterial({
      map: makeGlowTexture(),
      color: col,
      transparent: true,
      opacity: base,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
    });
    const sp = new THREE.Sprite(mat);
    sp.position.set(positions[i * 3], positions[i * 3 + 1], positions[i * 3 + 2]);
    const gt = coreT(sf.size || coreMin, coreMin, coreMax);
    const sz = isBlackhole ? 2.2
      : isPersona
        ? (relation ? CORE_GLOW_MIN + CORE_GLOW_GAIN * gt : 0.3)
        : relation ? CORE_GLOW_MIN + CORE_GLOW_GAIN * gt : 0.6 + (sf.size || 1) * 0.4;
    sp.scale.set(sz, sz, 1);
    (sp as unknown as { userData: { baseOpacity: number } }).userData = { baseOpacity: mat.opacity };
    worldGroup.add(sp);
    scopeGlows.push({
      sp, isBlackhole, base: isBlackhole ? 0.55 : base,
      // cort：心血管张力 — 暗红呼吸最深；oxy：粉色轻心跳；neutral：几乎常亮
      amp: isBlackhole ? 0.06
        : !relation || isPersona ? 0 : trend === 'cort' ? 0.30 : trend === 'oxy' ? 0.18 : 0.10,
      speed: trend === 'cort' ? 1.15 : trend === 'oxy' ? 0.85 : 0.55,
      phase: (i * 1.7) % (Math.PI * 2),
      trend,
    });
  });

  // ---- 连线 + 沿线流动光点（时间轴只重建这一层，不重建整个场景）
  /**
   * 沿线流光：每条边一个随机相位，光珠沿 aT 推进，经过处前亮后暗。
   * 关键：几何体在**重建时新建**（属性一开始就齐），不能事后再 setAttribute，
   * 否则程序已经编译完、属性没绑定，整层线不显示（上次就栽在这）。
   */
  const mkFlowLines = (color: number, base: number) => {
    const m = new THREE.ShaderMaterial({
      uniforms: {
        uTime: { value: 0 },
        uColor: { value: new THREE.Color(color) },
        uBase: { value: base },
      },
      vertexShader: `
        attribute float aT;
        attribute float aPhase;
        varying float vT;
        varying float vPhase;
        void main() {
          vT = aT;
          vPhase = aPhase;
          gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
        }
      `,
      fragmentShader: `
        uniform float uTime;
        uniform vec3 uColor;
        uniform float uBase;
        varying float vT;
        varying float vPhase;
        void main() {
          float head = fract(uTime * 0.30 + vPhase);
          float d = fract(vT - head + 0.5) - 0.5;
          float lit = exp(-d * d * 150.0);
          float a = clamp(uBase + lit * 0.5, 0.0, 1.0);
          vec3 col = uColor * (0.55 + lit * 2.0);
          gl_FragColor = vec4(col, a);
        }
      `,
      transparent: true,
      depthWrite: false,
      depthTest: false,
      blending: THREE.AdditiveBlending,
    });
    let seg = new THREE.LineSegments(new THREE.BufferGeometry(), m);
    seg.renderOrder = 10;
    worldGroup.add(seg);
    return {
      m,
      setData(pos: number[], ts: number[], ph: number[]) {
        const g = new THREE.BufferGeometry();
        g.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
        g.setAttribute('aT', new THREE.Float32BufferAttribute(ts, 1));
        g.setAttribute('aPhase', new THREE.Float32BufferAttribute(ph, 1));
        const old = seg.geometry;
        seg.geometry = g;
        old.dispose();
        seg.frustumCulled = false;
      },
      dispose() {
        seg.geometry.dispose();
        m.dispose();
      },
    };
  };

  const mkLines = (posArr: number[], color: number, opacity: number) => {
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.Float32BufferAttribute(posArr, 3));
    const m = new THREE.LineBasicMaterial({
      color, transparent: true, opacity,
      blending: THREE.AdditiveBlending, depthWrite: false,
      depthTest: false,   // 线要能压在大球上，否则端点被球体遮住、看起来没连到球心
    });
    const seg = new THREE.LineSegments(g, m);
    seg.renderOrder = 10;
    worldGroup.add(seg);
    return { g, m };
  };
  // 连线默认状态几乎不可见（全部 ≤0.02）：结构靠核心+星尘成形，
  // 具体某条边只在 hover / 选中时由下面的高亮层短暂闪出。
  const intraLines = mkFlowLines(0x5aa8ff, 0.30);
  const crossLines = mkFlowLines(0x7dffb0, 0.10);   // 同一个人跨星河：绿线（流光·极淡）
  const ownerLines = mkFlowLines(0xffd58a, 0.11);
  const negLines = mkLines([], 0xc03030, 0.35);   // 负面关系：暗红光带

  interface Seed { a: THREE.Vector3; b: THREE.Vector3; phase: number; speed: number; cross: boolean; owner: boolean }
  let flowSeeds: Seed[] = [];
  let flowPos = new Float32Array(3);
  const flowGeo = new THREE.BufferGeometry();
  flowGeo.setAttribute('position', new THREE.BufferAttribute(flowPos, 3));
  flowGeo.setAttribute('color', new THREE.BufferAttribute(new Float32Array(3), 3));

  const setLinePos = (g: THREE.BufferGeometry, arr: number[]) => {
    g.setAttribute('position', new THREE.Float32BufferAttribute(arr, 3));
  };

  const applyCutoff = (cutoff: number) => {
    lastCutoff = cutoff;
    const _fadeOut: number[] = [];
    nodes.forEach((n, i) => {
      const t = (n as { t?: number }).t;
      const ret = (n as { retentionScore?: number }).retentionScore ?? 1;
      const passRet = ret >= minRetention;
      let vis: number;
      if (t) {
        vis = (!cutoff || t <= cutoff) ? 1 : 0;
      } else {
        const always = n.kind === 'persona' || n.kind === 'category';
        vis = (always || !cutoff) ? 1 : 0;
      }
      if (vis && !passRet) {
        vis = 0;
        _fadeOut.push(i);      // 保留度不够 → 播动画
      }
      visArr[i] = vis;
    });
    if (_fadeOut.length) {
      // 已经在飞的不要再重复起飞；其余的保持可见，交给 launchFlyers
      const pending = _fadeOut
        .filter((i) => !flyers.some((f) => f.idx === i))
        .map((i) => ({ idx: i, id: nodes[i]?.id ?? '' }))
        .filter((x) => x.id);
      for (const x of pending) visArr[x.idx] = 1;
      launchFlyers(pending);
    }
    nodeGeo.attributes.aVis.needsUpdate = true;

    const intra: number[] = [];
    const intraT: number[] = [];
    const intraPhase: number[] = [];
    const ownerT: number[] = [];
    const ownerPhase: number[] = [];
    const crossT: number[] = [];
    const crossPhase: number[] = [];
    const cross: number[] = [];
    const owner: number[] = [];
    const neg: number[] = [];
    const seeds: Seed[] = [];
    // 连线裁剪：横跨屏幕的超长内链会糊住画面，内链只保留距离最短的一批；
    // 主人边/跨群边是结构语义，全保留。
    const MAX_INTRA = 900;
    interface Cand { a: THREE.Vector3; b: THREE.Vector3; dist: number; kind: string }
    const cand: Cand[] = [];
    for (const e of edges) {
      const ia = indexOf.get(e.source) ?? -1;
      const ib = indexOf.get(e.target) ?? -1;
      if (ia < 0 || ib < 0 || !visArr[ia] || !visArr[ib]) continue;
      const a = new THREE.Vector3(positions[ia * 3], positions[ia * 3 + 1], positions[ia * 3 + 2]);
      const b = new THREE.Vector3(positions[ib * 3], positions[ib * 3 + 1], positions[ib * 3 + 2]);
      cand.push({ a, b, dist: a.distanceTo(b), kind: e.kind || 'intra' });
    }
    // 客户端补边：接口只回 10 条语义边，整片星海是散的。
    // 用节点的 parentId 连「记忆 → 主题 → 群」，天然是一张跨得开的星云网；
    // 没有 parent 的再退回最近邻（kNN），避免孤岛。
    {
      const key = (a: THREE.Vector3, b: THREE.Vector3) =>
        `${a.x.toFixed(2)},${a.y.toFixed(2)},${a.z.toFixed(2)}|${b.x.toFixed(2)},${b.y.toFixed(2)},${b.z.toFixed(2)}`;
      const has = new Set(cand.map((c) => key(c.a, c.b)));
      const posOf = (i: number) => new THREE.Vector3(positions[i * 3], positions[i * 3 + 1], positions[i * 3 + 2]);
      let added = 0;

      // ① 父子边（结构网）
      for (let i = 0; i < nodes.length; i++) {
        if (!visArr[i]) continue;
        const pid = nodes[i].parentId;
        if (!pid) continue;
        const j = indexOf.get(pid);
        if (j === undefined || !visArr[j] || j === i) continue;
        const a = posOf(i);
        const b = posOf(j);
        const k1 = key(a, b);
        const k2 = key(b, a);
        if (has.has(k1) || has.has(k2)) continue;
        has.add(k1);
        cand.push({ a, b, dist: a.distanceTo(b), kind: 'intra' });
        if (++added > 700) break;
      }

      // ② 兜底 kNN：连到「最近的、但不在自己脸上的」节点（避免全是亚像素短线）
      if (added < 120) {
        const memIdx: number[] = [];
        for (let i = 0; i < nodes.length; i++) if (visArr[i] && nodes[i].kind === 'memory') memIdx.push(i);
        for (const i of memIdx) {
          if (added > 700) break;
          const ai = posOf(i);
          const near: { j: number; d: number }[] = [];
          for (const j of memIdx) {
            if (j === i) continue;
            const d = ai.distanceTo(posOf(j));
            if (d < 0.06) continue;          // 太近的不连：画出来是看不见的短线
            near.push({ j, d });
          }
          near.sort((x, y) => x.d - y.d);
          for (const n2 of near.slice(0, 1)) {
            const bj = posOf(n2.j);
            const k1 = key(ai, bj);
            const k2 = key(bj, ai);
            if (has.has(k1) || has.has(k2)) continue;
            has.add(k1);
            cand.push({ a: ai, b: bj, dist: n2.d, kind: 'intra' });
            added++;
          }
        }
      }
    }

    const structural = cand.filter((x) => x.kind === 'owner' || x.kind === 'cross' || x.kind === 'owner_neg');
    const plain = cand.filter((x) => x.kind !== 'owner' && x.kind !== 'cross')
      .sort((x, y) => x.dist - y.dist)
      .slice(0, MAX_INTRA);
    for (const it of [...structural, ...plain]) {
      const { a, b } = it;
      const isCross = it.kind === 'cross';
      const isOwner = it.kind === 'owner';
      const isNeg = it.kind === 'owner_neg';
      // 曲线化：中点向上拱起一点，采样成折线，视觉上是柔和的弧
      const mid = a.clone().add(b).multiplyScalar(0.5);
      // 主人线 / 跨群线：中点往侧向拱出去一点，变成柔和的弧，不再是一条直绳
      if (isOwner || isCross) {
        const dir = b.clone().sub(a);
        const len = dir.length() || 1;
        const perp = new THREE.Vector3().crossVectors(dir, new THREE.Vector3(0, 1, 0));
        if (perp.lengthSq() > 1e-6) {
          perp.normalize().multiplyScalar(len * 0.35);   // 强弧度：顺着悬臂弯，而不是直挺挺射出去
          mid.add(perp);
        }
      }
      const curve = new THREE.QuadraticBezierCurve3(a, mid, b);   // 端点=球心
      const pts = curve.getPoints(10);
      const target = isNeg ? neg : isOwner ? owner : isCross ? cross : intra;
      const segN = pts.length - 1;
      const phase = (seeds.length * 0.6180339887) % 1;
      for (let pi = 0; pi < segN; pi++) {
        const p0 = pts[pi];
        const p1 = pts[pi + 1];
        target.push(p0.x, p0.y, p0.z, p1.x, p1.y, p1.z);
        const tt0 = pi / segN;
        const tt1 = (pi + 1) / segN;
        if (isOwner) {
          ownerT.push(tt0, tt1);
          ownerPhase.push(phase, phase);
        } else if (isCross) {
          crossT.push(tt0, tt1);
          crossPhase.push(phase, phase);
        } else if (!isNeg) {
          intraT.push(tt0, tt1);
          intraPhase.push(phase, phase);
        }
      }
      seeds.push({
        a, b, phase: Math.random(),
        speed: isOwner ? 0.16 : isCross ? 0.30 : 0.2,
        cross: isCross, owner: isOwner,
      });
    }
    flowSeeds = seeds;
    intraLines.setData(intra, intraT, intraPhase);
    crossLines.setData(cross, crossT, crossPhase);
    ownerLines.setData(owner, ownerT, ownerPhase);
    setLinePos(negLines.g, neg);

    const n = Math.max(1, seeds.length);
    flowPos = new Float32Array(n * 3);
    const col = new Float32Array(n * 3);
    seeds.forEach((s, i) => {
      const c = new THREE.Color(s.owner ? 0xffe0a8 : s.cross ? 0xa8ffd0 : 0xeaf6ff);
      col[i * 3] = c.r;
      col[i * 3 + 1] = c.g;
      col[i * 3 + 2] = c.b;
    });
    flowGeo.setAttribute('position', new THREE.BufferAttribute(flowPos, 3));
    flowGeo.setAttribute('color', new THREE.BufferAttribute(col, 3));
  };
  applyCutoff(0);
  const flowMat = new THREE.PointsMaterial({
    size: 0.012, transparent: true, opacity: 0.35, vertexColors: true,
    blending: THREE.AdditiveBlending, depthWrite: false, map: makeGlowTexture(),
  });
  const flowObj = new THREE.Points(flowGeo, flowMat);
  worldGroup.add(flowObj);

  // ---- 高亮边：常态完全不显示（主连线 opacity ≤0.02），
  // 只在 hover / 选中某个节点时把"与它相关"的边闪出来，随后自动淡出。
  const hiGeo = new THREE.BufferGeometry();
  const hiMat = new THREE.LineBasicMaterial({
    vertexColors: true, transparent: true, opacity: 0,
    blending: THREE.AdditiveBlending, depthWrite: false,
  });
  const hiLines = new THREE.LineSegments(hiGeo, hiMat);
  hiLines.frustumCulled = false;
  hiLines.visible = false;
  worldGroup.add(hiLines);

  const HI_EDGE_MAX = 90;
  const HI_EDGE_ALPHA = 0.55;
  // 固定容量顶点缓冲（90 条边 × 10 段 × 2 端点），重建时只覆写 + setDrawRange，
  // 避免每次 hover 换 BufferAttribute 在 GPU 侧留下没人释放的旧 buffer。
  const HI_VERT_CAP = HI_EDGE_MAX * 20;
  const hiPosAttr = new THREE.Float32BufferAttribute(new Float32Array(HI_VERT_CAP * 3), 3);
  const hiColAttr = new THREE.Float32BufferAttribute(new Float32Array(HI_VERT_CAP * 3), 3);
  hiPosAttr.setUsage(THREE.DynamicDrawUsage);
  hiColAttr.setUsage(THREE.DynamicDrawUsage);
  hiGeo.setAttribute('position', hiPosAttr);
  hiGeo.setAttribute('color', hiColAttr);
  hiGeo.setDrawRange(0, 0);

  let hiNodeId: string | null = null;
  let hiHoldUntil = 0;
  let hiAlpha = 0;
  const _hiA = new THREE.Vector3();
  const _hiB = new THREE.Vector3();

  function rebuildHiEdges(nodeId: string) {
    hiNodeId = nodeId;
    const ia = indexOf.get(nodeId);
    if (ia === undefined) return;
    _hiA.set(positions[ia * 3], positions[ia * 3 + 1], positions[ia * 3 + 2]);
    const cand: { b: THREE.Vector3; dist: number; kind: string }[] = [];
    for (const e of edges) {
      if (e.source !== nodeId && e.target !== nodeId) continue;
      const other = indexOf.get(e.source === nodeId ? e.target : e.source);
      if (other === undefined || other === ia || !visArr[other]) continue;
      _hiB.set(positions[other * 3], positions[other * 3 + 1], positions[other * 3 + 2]);
      cand.push({ b: _hiB.clone(), dist: _hiA.distanceTo(_hiB), kind: e.kind || 'intra' });
    }
    cand.sort((x, y) => x.dist - y.dist);
    const pos = hiPosAttr.array as Float32Array;
    const cols = hiColAttr.array as Float32Array;
    const tint = new THREE.Color();
    let vi = 0;
    for (const it of cand.slice(0, HI_EDGE_MAX)) {
      const mid = _hiA.clone().add(it.b).multiplyScalar(0.5);   // 高亮边同样不做 y 偏移，端点=球心
      const pts = new THREE.QuadraticBezierCurve3(_hiA.clone(), mid, it.b).getPoints(10);
      tint.set(it.kind === 'owner' ? 0xffe0a8 : it.kind === 'cross' ? 0xa8ffd0 : 0x9fd8ff);
      for (let pi = 0; pi < pts.length - 1 && vi < HI_VERT_CAP; pi++) {
        const p0 = pts[pi];
        const p1 = pts[pi + 1];
        pos[vi * 3] = p0.x; pos[vi * 3 + 1] = p0.y; pos[vi * 3 + 2] = p0.z;
        cols[vi * 3] = tint.r; cols[vi * 3 + 1] = tint.g; cols[vi * 3 + 2] = tint.b;
        vi++;
        pos[vi * 3] = p1.x; pos[vi * 3 + 1] = p1.y; pos[vi * 3 + 2] = p1.z;
        cols[vi * 3] = tint.r; cols[vi * 3 + 1] = tint.g; cols[vi * 3 + 2] = tint.b;
        vi++;
      }
      if (vi >= HI_VERT_CAP) break;
    }
    hiPosAttr.needsUpdate = true;
    hiColAttr.needsUpdate = true;
    hiGeo.setDrawRange(0, vi);
  }

  /** 闪一下某节点的相关边；holdMs 后自动淡出（hover 时每次移动续期）。 */
  let hiLastBuild = 0;
  function flashEdges(nodeId: string, holdMs: number) {
    const now = performance.now();
    if (nodeId !== hiNodeId && now - hiLastBuild > 80) {   // 轻节流：扫过密集星尘时不狂重建几何
      rebuildHiEdges(nodeId);
      hiLastBuild = now;
    }
    hiHoldUntil = Math.max(hiHoldUntil, now + holdMs);
  }

  // ---- 拾取
  // 固定的世界阈值在新布局里会失效：同一颗星在屏幕上占几像素随镜头距离变化，
  // 拉远后固定的 0.45 世界单位会盖住整片星尘（点 A 群核心却选中 B 群星尘）。
  // 这里统一改成「屏幕像素半径 → 世界半径」的换算，并且核心优先。
  const ray = new THREE.Raycaster();
  const pointer = new THREE.Vector2();

  /** 距相机 dist 处，1 个屏幕像素对应多少世界单位：world = 2*tan(fov/2)*dist / 画布高度 */

  /**
   * @param pxRadius   屏幕像素命中半径（点选 PICK_PX_CLICK / hover PICK_PX_HOVER）
   * @param preferCore 核心优先：1.5 倍像素阈值内的群核心/persona 优先返回，
   *                   保证「点群核心必得该群」而不是重叠的星尘。
   */
  const pick = (ev: PointerEvent, _pxRadius: number, _preferCore: boolean): number | null => {
    // 屏幕空间优先：按「鼠标到球心的像素距离 / 球自身像素半径」打分，分最小者胜。
    // 这样点在巨球中心的小球上时，小球（score≈0）不会被外面那层巨球（score 很大）抢走。
    const rect = renderer.domElement.getBoundingClientRect();
    const mx = ev.clientX - rect.left;
    const my = ev.clientY - rect.top;
    const W = rect.width || 800;
    const H = rect.height || 500;
    const v = new THREE.Vector3();
    const t = new THREE.Vector3();
    // 两轮：先只找「人格行星」（trait/rule/defense），再找普通节点。
    // 中心球 size 极大，单轮打分永远赢，导致永远点不到行星。
    const isPlanet = (i: number) =>
      nodes[i].category === 'trait' || nodes[i].category === 'rule' || nodes[i].category === 'defense';
    // 预扫：鼠标到「核心恒星」（persona / scope）的最小归一化距离。
    // 行星优先轮只有比中心核心更靠近鼠标时才抢占，否则点金色中心球会选中旁边行星。
    let coreScore = Infinity;
    for (let i = 0; i < nodes.length; i++) {
      if (!isCoreKind(nodes[i])) continue;
      if (!nodes[i].starfield || !visArr[i]) continue;
      t.set(positions[i * 3], positions[i * 3 + 1], positions[i * 3 + 2]);
      t.applyMatrix4(worldGroup.matrixWorld);
      v.copy(t).project(camera);
      if (v.z > 1 || v.z < -1) continue;
      const sx = (v.x * 0.5 + 0.5) * W;
      const sy = (-v.y * 0.5 + 0.5) * H;
      const camDist = camera.position.distanceTo(t);
      if (camDist <= 0) continue;
      const pxDia = Math.min(360, Math.max(12, (sizes[i] || 0.02) * (1400 / camDist)));
      const d = Math.hypot(sx - mx, sy - my);
      if (d > pxDia / 2 + 6) continue;
      coreScore = Math.min(coreScore, d / (pxDia / 2));
    }

    let best: number | null = null;
    let bestScore = Infinity;
    for (const pass of [0, 1]) {
    best = null;
    bestScore = Infinity;
    for (let i = 0; i < nodes.length; i++) {
      if (pass === 0 && !isPlanet(i)) continue;
      const sf = nodes[i].starfield;
      if (!sf || !visArr[i]) continue;
      t.set(positions[i * 3], positions[i * 3 + 1], positions[i * 3 + 2]);
      t.applyMatrix4(worldGroup.matrixWorld);   // 世界组在自转，必须用变换后的坐标
      v.copy(t).project(camera);
      if (v.z > 1 || v.z < -1) continue;
      const sx = (v.x * 0.5 + 0.5) * W;
      const sy = (-v.y * 0.5 + 0.5) * H;
      const camDist = camera.position.distanceTo(t);
      if (camDist <= 0) continue;
      // 命中半径 = 实际渲染出来的点直径（gl_PointSize = aSize * 1400 / 距离）。
      // 旧公式 (0.022+0.062*sf.size) 在新布局里会算出上千像素：行星 size=220 时
      // 封顶后仍覆盖整个中心区域，导致「点金色中心球」总是选中旁边重叠的行星。
      // 下限 12px 保证星尘也点得中（它们实际只有 1~2px）。
      const rawDia = (sizes[i] || 0.02) * (1400 / camDist);
      const cap = isPlanet(i) ? 60 : 360;
      const pxDia = Math.min(cap, Math.max(isPlanet(i) ? 14 : 12, rawDia));
      const d = Math.hypot(sx - mx, sy - my);
      if (d > pxDia / 2 + 6) continue;          // 鼠标不在这个球上
      const score = d / (pxDia / 2);            // 越靠球心越小
      // pass 0 是行星优先轮：必须比中心核心更贴近鼠标（score 更小）才抢占，
      // 否则点在金色中心球上时会被旁边 10px 内的行星抢走。
      if (pass === 0 && (score > 0.85 || score >= coreScore)) continue;
      if (score < bestScore) {
        bestScore = score;
        best = i;
      }
    }
    if (best !== null) return best;
    }
    if (best !== null) return best;

    // 都没按住：退回射线拾取（宽松阈值）
    pointer.x = ((ev.clientX - rect.left) / rect.width) * 2 - 1;
    pointer.y = -((ev.clientY - rect.top) / rect.height) * 2 + 1;
    ray.setFromCamera(pointer, camera);
    (ray as unknown as { params: { Points: { threshold: number } } }).params.Points = { threshold: 0.6 };
    const hits = ray.intersectObject(nodePoints, false);
    if (!hits.length) return null;
    hits.sort((a, b) => (a.distanceToRay ?? a.distance) - (b.distanceToRay ?? b.distance));
    return hits[0].index ?? null;
  };

  // ---- P6：点击涟漪（对象池 6 圈，各自一份材质，靠 uFade 淡出）
  const makeClickRippleMat = () => new THREE.ShaderMaterial({
    uniforms: { uColor: { value: new THREE.Color(0x9fd8ff) }, uFade: { value: 1 } },
    vertexShader: `
      varying vec2 vUv;
      void main() {
        vUv = position.xy;                       // 单位平面 -1..1
        vec4 mv = modelViewMatrix * vec4(0.0, 0.0, 0.0, 1.0);
        mv.xy += position.xy;
        gl_Position = projectionMatrix * mv;
      }
    `,
    fragmentShader: `
      uniform vec3 uColor;
      uniform float uFade;
      varying vec2 vUv;
      void main() {
        float r = length(vUv);
        if (r > 1.0) discard;
        float d = (r - 0.82) * 14.0;
        float ring = exp(-d * d);
        float a = ring * 0.9 * uFade;
        if (a < 0.01) discard;
        gl_FragColor = vec4(uColor * (0.8 + ring * 1.6), a);
      }
    `,
    transparent: true,
    depthWrite: false,
    depthTest: false,
    blending: THREE.AdditiveBlending,
  });
  const clickRipples: { mesh: THREE.Mesh; t0: number; life: number; base: number }[] = [];
  const clickRipplePool: THREE.Mesh[] = [];
  for (let i = 0; i < 6; i++) {
    const m = new THREE.Mesh(new THREE.PlaneGeometry(2, 2), makeClickRippleMat());
    m.visible = false;
    m.frustumCulled = false;
    m.renderOrder = 12;
    scene.add(m);
    clickRipplePool.push(m);
  }
  function spawnClickRipple(x: number, y: number, z: number, base: number) {
    const mesh = clickRipplePool.find((m) => !m.visible) ?? clickRipplePool[0];
    mesh.position.set(x, y, z);
    mesh.visible = true;
    const t0 = performance.now();
    const item = { mesh, t0, life: 900, base };
    const exist = clickRipples.find((r2) => r2.mesh === mesh);
    if (exist) Object.assign(exist, item);
    else clickRipples.push(item);
  }

  let hoverNodeId: string | null = null;
  let selNodeId: string | null = null;
  const onMove = (ev: PointerEvent) => {
    const idx = pick(ev, PICK_PX_HOVER, false);   // hover 用紧阈值：点到哪个就只亮哪个
    hoverNodeId = idx === null ? null : nodes[idx].id;
    renderer.domElement.style.cursor = idx === null ? 'grab' : 'pointer';
    if (idx !== null) flashEdges(nodes[idx].id, 420);   // hover：相关边短暂闪出
    if (onHover) onHover(idx === null ? null : nodes[idx]);
  };
  // 拖动旋转松手时浏览器也会发 click，必须区分「拖拽」和「点选」，
  // 否则每次转完视角镜头都会自动推进一次。
  let downX = 0;
  let downY = 0;
  let dragging = false;
  renderer.domElement.addEventListener('pointerdown', (ev) => {
    downX = ev.clientX;
    downY = ev.clientY;
    dragging = false;
  });
  renderer.domElement.addEventListener('pointermove', (ev) => {
    if (ev.buttons && Math.hypot(ev.clientX - downX, ev.clientY - downY) > 6) dragging = true;
  });
  const onClick = (ev: MouseEvent) => {
    if (dragging) return;                       // 刚转完视角：不算点击，镜头保持不动
    const idx = pick(ev as unknown as PointerEvent, PICK_PX_CLICK, true);
    if (idx === null) return;
    const n = nodes[idx];
    if (n) {
      if (n.starfield) {
        spawnClickRipple(
          n.starfield.x * SPREAD,
          n.starfield.y * SPREAD,
          n.starfield.z * SPREAD,
          Math.max(0.22, (n.starfield.size ?? 1) * 0.3),
        );
      }
      onPick(n);
    }
  };
  renderer.domElement.addEventListener('pointermove', onMove);
  renderer.domElement.addEventListener('click', onClick);
  const onDbl = () => {
    // 双击：回到世界中心（关系星河回到「整片星河」的默认视距）
    const dir = camera.position.clone().sub(controls.target);
    if (dir.length() < 1e-5) dir.set(0.6, 0.4, 0.6);
    dir.normalize();
    const toCam = relation
      ? dir.multiplyScalar(fitDistance)
      : new THREE.Vector3(3.1, 1.9, 3.1);
    tween = {
      fromCam: camera.position.clone(),
      toCam,
      fromTarget: controls.target.clone(),
      toTarget: new THREE.Vector3(0, 0, 0),
      t0: performance.now(), dur: 700,
    };
  };
  renderer.domElement.addEventListener('dblclick', onDbl);

  let tween: {
    fromCam: THREE.Vector3; toCam: THREE.Vector3;
    fromTarget: THREE.Vector3; toTarget: THREE.Vector3;
    t0: number; dur: number;
  } | null = null;
  let lastSpinT = 0;
  let raf = 0;
  let last = performance.now();
  let lastInteraction = performance.now();
  let running = true;
  const clock = new THREE.Clock();

  let labelTick = 0;
  const _v = new THREE.Vector3();
  function updateLabelCollision() {
    // 屏幕投影后按距离去重：两个标签 <46px 视为重叠，后来的直接隐藏
    const rect = renderer.domElement;
    const w = rect.clientWidth || 800;
    const h = rect.clientHeight || 500;
    const shown: { x: number; y: number }[] = [];
    scopeLabels.forEach((obj) => {
      _v.copy(obj.position).project(camera);
      const x = (_v.x * 0.5 + 0.5) * w;
      const y = (-_v.y * 0.5 + 0.5) * h;
      const clash = shown.some((q) => Math.hypot(q.x - x, q.y - y) < 46);
      obj.visible = !clash;
      if (!clash) shown.push({ x, y });
    });
  }

  const loop = (now: number) => {
    if (!running) return;
    raf = requestAnimationFrame(loop);
    if (++labelTick % 12 === 0) updateLabelCollision();
    const idle = now - lastInteraction > 2000;
    const minGap = idle ? 1000 / 30 : 0;
    if (now - last < minGap) return;
    last = now;

    const t = clock.getElapsedTime();
    nodeMat.uniforms.uTime.value = t;
    intraLines.m.uniforms.uTime.value = t;
    crossLines.m.uniforms.uTime.value = t;
    ownerLines.m.uniforms.uTime.value = t;
    dustGroup.rotation.y += 0.0006;
    starFieldGroup.rotation.y += 0.00012;

    // 涟漪：0→1 扩散 + 淡出
    for (let i = clickRipples.length - 1; i >= 0; i--) {
      const rp = clickRipples[i];
      const k = (performance.now() - rp.t0) / rp.life;
      if (k >= 1) {
        rp.mesh.visible = false;
        clickRipples.splice(i, 1);
        continue;
      }
      const e = 1 - Math.pow(1 - k, 2.2);          // 先快后慢
      rp.mesh.scale.setScalar(rp.base * (0.5 + e * 3.4));
      rp.mesh.quaternion.copy(camera.quaternion);  // 永远面向相机
      (rp.mesh.material as THREE.ShaderMaterial).uniforms.uFade.value = 1 - k;
    }

    // ---- 遗忘动画：被过滤的球飞向黑洞，边飞边缩小变暗 ----
    if (flyers.length) {
      const pnow = performance.now();
      let dirty = false;
      let trailDirty = false;
      for (let fi = flyers.length - 1; fi >= 0; fi--) {
        const f = flyers[fi];
        if (pnow < f.t0 + f.delay) {
          // 还没轮到它：留在原位，等前面的球先走
          continue;
        }
        const raw = Math.min(1, (pnow - f.t0 - f.delay) / f.dur);
        // 加速：被引力越拉越快（ease-in）
        const k = raw * raw * raw * 0.85 + raw * 0.15;

        // 螺旋轨迹：半径线性收缩 + 角度随进度旋转（越靠近转得越快）
        const dx0 = f.x0 - BH_X, dy0 = f.y0 - BH_Y, dz0 = f.z0 - BH_Z;
        const r0 = Math.hypot(dx0, dy0, dz0) || 1e-6;
        const ang0 = Math.atan2(dz0, dx0);
        const ang = ang0 + f.spin * (1.4 + 4.0 * k) * k;   // 约 2.5 圈
        const r = r0 * (1 - k) * (1 - 0.25 * k);           // 非线性收缩
        const px = BH_X + r * Math.cos(ang);
        const pz = BH_Z + r * Math.sin(ang);
        const py = BH_Y + (f.y0 - BH_Y) * (1 - k) + Math.sin(k * Math.PI * 3) * r0 * 0.06;

        positions[f.idx * 3] = px;
        positions[f.idx * 3 + 1] = py;
        positions[f.idx * 3 + 2] = pz;
        visArr[f.idx] = Math.max(0, 1 - k * k);            // 越来越暗
        dirty = true;

        // 尾迹：记录最近 N 个位置，越旧越暗
        f.trail.unshift(px, py, pz);
        if (f.trail.length > TRAIL_LEN * 3) f.trail.length = TRAIL_LEN * 3;
        const base = f.slot * TRAIL_LEN;
        for (let ti = 0; ti < TRAIL_LEN; ti++) {
          const o = (base + ti) * 3;
          const src = ti * 3;
          if (src + 2 < f.trail.length) {
            trailPos[o] = f.trail[src];
            trailPos[o + 1] = f.trail[src + 1];
            trailPos[o + 2] = f.trail[src + 2];
            const fade = 1 - ti / TRAIL_LEN;
            trailCol[o] = 1.0 * fade;
            trailCol[o + 1] = 0.82 * fade;
            trailCol[o + 2] = 0.45 * fade;
          } else {
            trailPos[o] = 9999;      // 隐藏未使用点
          }
        }
        trailDirty = true;

        // 抵达黑洞：销毁 + 吞噬闪光
        const dist = Math.hypot(px - BH_X, py - BH_Y, pz - BH_Z);
        if (raw >= 1 || dist < 0.04) {
          visArr[f.idx] = 0;                 // 从场景移除
          for (let ti = 0; ti < TRAIL_LEN; ti++) {
            trailPos[(f.slot * TRAIL_LEN + ti) * 3] = 9999;      // 清掉它自己的槽位
            trailPos[(f.slot * TRAIL_LEN + ti) * 3 + 1] = 9999;
            trailPos[(f.slot * TRAIL_LEN + ti) * 3 + 2] = 9999;
          }
          spawnRipple();                     // 吸积盘吞噬闪光
          try { onSwallowed?.(f.id); } catch { /* 回调失败不影响动画 */ }
          flyers.splice(fi, 1);
          trailDirty = true;
          dirty = true;
        }
      }
      if (dirty) {
        nodeGeo.attributes.position.needsUpdate = true;
        nodeGeo.attributes.aVis.needsUpdate = true;
      }
      if (trailDirty) {
        trailGeo.attributes.position.needsUpdate = true;
        trailGeo.attributes.color.needsUpdate = true;
      }
    }

    // 吞噬光环：扩张 + 淡出
    if (ripples.length) {
      const pnow = performance.now();
      for (let i = ripples.length - 1; i >= 0; i--) {
        const rp = ripples[i];
        const k = Math.min(1, (pnow - rp.t0) / rp.dur);
        const sc = 0.4 + 3.2 * k;
        rp.sp.scale.set(sc, sc, 1);
        (rp.sp.material as THREE.SpriteMaterial).opacity = 0.95 * (1 - k);
        if (k >= 1) {
          worldGroup.remove(rp.sp);
          rp.sp.material.dispose();
          ripples.splice(i, 1);
        }
      }
    }
    // ---- 位置平滑过渡：拖滑块时粒子滑到新位置，不瞬移 ----
    if (tweenTarget.size) {
      let moved = false;
      for (const [i, tgt] of tweenTarget) {
        const cx = positions[i * 3], cy = positions[i * 3 + 1], cz = positions[i * 3 + 2];
        const k = 0.18;                       // 每帧逼近 18%，约 250ms 到位
        const nx = cx + (tgt.x - cx) * k;
        const ny = cy + (tgt.y - cy) * k;
        const nz = cz + (tgt.z - cz) * k;
        positions[i * 3] = nx; positions[i * 3 + 1] = ny; positions[i * 3 + 2] = nz;
        moved = true;
        if (Math.abs(nx - tgt.x) < 0.002 && Math.abs(ny - tgt.y) < 0.002
            && Math.abs(nz - tgt.z) < 0.002) {
          positions[i * 3] = tgt.x; positions[i * 3 + 1] = tgt.y; positions[i * 3 + 2] = tgt.z;
          tweenTarget.delete(i);              // 到位了就停
        }
      }
      if (moved) nodeGeo.attributes.position.needsUpdate = true;
    }

    blackHole?.update(t);                 // 吸积盘高速旋转 + 白点呼吸
    whiteHole?.update(t);                 // 白洞脉冲 + 喷射粒子
    // 自转：选中某个球 / 鼠标悬在球上 / 刚操作过 1.2 秒内 —— 一律停，方便点准
    const spinIdle = performance.now() - lastInteraction > 1200;
    const spinPaused = selNodeId !== null || hoverNodeId !== null || !spinIdle;
    if (!spinPaused) {
      const dtSpin = lastSpinT ? Math.min(0.06, t - lastSpinT) : 0.016;
      worldGroup.rotation.y += dtSpin * 0.025;
    }
    lastSpinT = t;
    // 核心激素脉冲：cort 暗红呼吸 / oxy 粉色轻心跳 / neutral 蓝白近常亮
    for (const g of scopeGlows) {
      const m = g.sp.material as THREE.SpriteMaterial;
      m.opacity = Math.max(0, Math.min(1, g.base * (1 + g.amp * Math.sin(t * g.speed + g.phase)) * haloScale));
      // 黑洞的吸积盘缓慢自转（SpriteMaterial 支持 rotation）
      if (g.isBlackhole) {
        m.rotation = t * 0.35;
        // 呼吸时轻微缩放，像吸积盘在吞吐
        const sc = 2.2 * (1 + 0.04 * Math.sin(t * 0.9));
        g.sp.scale.set(sc, sc, 1);
      }
    }

    // 连线高亮：hover / 选中的相关边短暂出现后自动淡出，回到“默认不可见”
    const hiWant = now < hiHoldUntil ? HI_EDGE_ALPHA : 0;
    hiAlpha += (hiWant - hiAlpha) * 0.14;
    if (hiWant === 0 && hiAlpha < 0.01) hiAlpha = 0;
    if (hiAlpha > 0.004) {
      hiLines.visible = true;
      hiMat.opacity = hiAlpha;
    } else if (hiLines.visible) {
      hiLines.visible = false;
      hiMat.opacity = 0;
    }

    let fi = 0;
    for (const s of flowSeeds) {
      const p = (s.phase + t * s.speed) % 1;
      mflowPos[fi * 3] = s.a.x + (s.b.x - s.a.x) * p;
      mflowPos[fi * 3 + 1] = s.a.y + (s.b.y - s.a.y) * p;
      mflowPos[fi * 3 + 2] = s.a.z + (s.b.z - s.a.z) * p;
      fi++;
    }
    mflowGeo.attributes.position.needsUpdate = true;

    if (tween) {
      const k = Math.min(1, (now - tween.t0) / tween.dur);
      const e = 1 - Math.pow(1 - k, 3);
      camera.position.lerpVectors(tween.fromCam, tween.toCam, e);
      controls.target.lerpVectors(tween.fromTarget, tween.toTarget, e);
      if (k >= 1) tween = null;
    }
    controls.autoRotate = !relation && selNodeId === null && hoverNodeId === null;
    controls.update();
    renderer.render(scene, camera);
    labelRenderer.render(scene, camera);
  };
  raf = requestAnimationFrame(loop);

  controls.addEventListener('start', () => {
    lastInteraction = performance.now();
    controls.autoRotate = false;
  });
  controls.addEventListener('end', () => {
    lastInteraction = performance.now();
  });

  const onVis = () => {
    if (document.visibilityState === 'hidden') {
      running = false;
      cancelAnimationFrame(raf);
    } else if (!running) {
      running = true;
      last = performance.now();
      raf = requestAnimationFrame(loop);
    }
  };
  document.addEventListener('visibilitychange', onVis);

  const resize = () => {
    const w = container.clientWidth || 800;
    const h = container.clientHeight || 520;
    renderer.setSize(w, h, false);
    labelRenderer.setSize(w, h);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  };
  resize();
  const ro = new ResizeObserver(resize);
  ro.observe(container);

  // ---- 高亮状态：选中节点 / 图例极性筛选（互斥：点图例会清空选中）
  let selTopicIds = new Set<string>();
  let polarity: string | null = null;
  // 内分泌染色：CORT 高 → 整体偏红（紧张），DA 高 → 整体提亮（兴奋）
  let hormoneTint = { DA: 0.5, CORT: 0.3 };
  const TINT_RED = new THREE.Color(0xff3b30);

  function applyColors() {
    nodes.forEach((n, i) => {
      color.set(nodeGlowColor(n));
      if (hormoneTint.CORT > 0.5) {
        color.lerp(TINT_RED, Math.min(0.45, (hormoneTint.CORT - 0.5) * 0.9));
      }
      if (hormoneTint.DA > 0.5) {
        color.multiplyScalar(1.0 + Math.min(0.25, (hormoneTint.DA - 0.5) * 0.5));
      }
      const base = brightnessOf(n, relation);
      let mul = 1;
      let grow = 1;
      if (polarity) {
        const on = polarityOf(n) === polarity;
        mul = on ? 1.3 : 0.1;
        grow = on ? 1.7 : 1;
      } else if (selNodeId) {
        const on = n.id === selNodeId || selTopicIds.has(n.id);
        mul = on ? 1.12 : 0.3;
        grow = on ? 1.6 : 1;
      }
      const b = base * mul;
      colors[i * 3] = Math.min(1, color.r * b);
      colors[i * 3 + 1] = Math.min(1, color.g * b);
      colors[i * 3 + 2] = Math.min(1, color.b * b);
      // 尺寸走统一的 sizeOf：核心恒星与记忆星尘保持 5 倍以上差距，
      // 不再出现「setHormones 一调用核心就被打回普通尺寸」的回归。
      sizes[i] = sizeOf(n, i, grow);
    });
    nodeGeo.attributes.color.needsUpdate = true;
    nodeGeo.attributes.aSize.needsUpdate = true;
  }

  return {
    resize,
    setGlow(v01: number) {
      nodeMat.uniforms.uGlow.value = Math.max(0, Math.min(1, v01));
    },
    setHalo(v01: number) {
      haloScale = Math.max(0, Math.min(2, v01));
      // 立即应用一次（不用等下一帧），拖到 0 就立刻消失
      scopeGlows.forEach((g) => {
        (g.sp.material as THREE.SpriteMaterial).opacity = Math.max(0, Math.min(1, g.base * haloScale));
      });
    },
    setZoom(v01: number) {
      // 指数分布，手感均匀；关系星河按自身尺度自适应（见 zoomDistance）
      const dist = zoomDistance(v01);
      const dir = camera.position.clone().sub(controls.target);
      if (dir.length() < 1e-6) dir.set(0.6, 0.4, 0.6);
      dir.normalize();
      camera.position.copy(controls.target.clone().add(dir.multiplyScalar(dist)));
      controls.update();
      lastInteraction = performance.now();
    },
    focusPoint(x: number, y: number, z: number) {
      const target = new THREE.Vector3(x * SPREAD, y * SPREAD, z * SPREAD);
      const keepDist = Math.max(0.06, camera.position.distanceTo(controls.target));
      const dir = camera.position.clone().sub(controls.target);
      if (dir.length() < 1e-5) dir.set(0.6, 0.4, 0.6);
      dir.normalize();
      const toCam = target.clone().add(dir.multiplyScalar(keepDist));
      tween = {
        fromCam: camera.position.clone(), toCam,
        fromTarget: controls.target.clone(), toTarget: target.clone(),
        t0: performance.now(), dur: 800,
      };
      lastInteraction = performance.now();
    },
    focus(nodeId: string, zoomIn = false) {
      const idx = indexOf.get(nodeId);
      if (idx === undefined) return;
      const sf = nodes[idx].starfield!;
      const target = new THREE.Vector3(sf.x * SPREAD, sf.y * SPREAD, sf.z * SPREAD)
        .applyMatrix4(worldGroup.matrixWorld);   // 世界组自转：目标点必须用旋转变换后的坐标
      // 只缩不放：点星时推近到 STAR_FOCUS_DIST；用户已经拉得更近就保持原距离，
      // 避免把刚手动拉近的镜头又推回去。
      const keepDist = Math.max(0.06, camera.position.distanceTo(controls.target));
      // 关系星河半径是旧星海的 ~10 倍，0.42 的世界视距会把整片星河甩出画面（点一次后就「点不中」）。
      // 这里按场景尺度给你一个能看清目标 + 周边星团的视距，并且绝不超过当前视距的 0.9。
      const focusDist = relation
        ? Math.min(Math.max(layoutRadius * RELATION_FOCUS_RATIO, RELATION_FOCUS_MIN), keepDist * 0.9)
        : STAR_FOCUS_DIST;
      const dist = zoomIn ? Math.min(keepDist, focusDist) : keepDist;
      const dir = camera.position.clone().sub(controls.target);
      if (dir.length() < 1e-5) dir.set(0.6, 0.4, 0.6);
      dir.normalize();
      const toCam = target.clone().add(dir.multiplyScalar(dist));
      tween = {
        fromCam: camera.position.clone(), toCam,
        fromTarget: controls.target.clone(), toTarget: target.clone(),
        t0: performance.now(), dur: 700,
      };
      lastInteraction = performance.now();
    },
    setHighlight(nodeId: string | null, topicNodeIds: string[]) {
      selNodeId = nodeId;
      selTopicIds = new Set(topicNodeIds);
      applyColors();
      // 选中时把与它相关的边闪出来，1.4s 后自动淡出
      if (nodeId) flashEdges(nodeId, 1400);
    },
    setPolarity(p: string | null) {
      polarity = p;
      applyColors();
    },
    setColorMode(m: 'category' | 'emotion') {
      colorMode = m;
      applyColors();
    },
    setHormones(h: Record<string, number>) {
      hormoneTint = { DA: h.DA ?? 0.5, CORT: h.CORT ?? 0.3 };
      applyColors();
    },
    setTimeCutoff(cutoff: number) {
      applyCutoff(cutoff);
    },
    setMinRetention(v: number) {
      minRetention = v;
      applyCutoff(lastCutoff);
    },
    updatePositions(next: MemoryNode[], cutoffMs: number) {
      // 位置完全由「时间指针 + 节点创建时间」现算，不用后端返回的 life。
      // 好处：拖滑块/播放时每一帧都能重算，粒子连续流动，而不是等请求回来才跳。
      const nowMs = cutoffMs > 0 ? cutoffMs : Date.now();
      for (const n of next) {
        const i = indexOf.get(n.id);
        if (i === undefined || !n.starfield) continue;

        // tyFlow 思路：这些分类只是「坐标提供者」，本身不可见
        // （长期 dust / 短期 short / 事件 exp / 创伤 trauma 全部 opacity 0）
        if (n.category === 'dust' || n.category === 'exp' || n.category === 'trauma'
            || n.category === 'short') {
          visArr[i] = 0;
          // 但仍然记下它的坐标，供流动粒子取用
          anchorPos.set(n.id, {
            x: n.starfield.x * SPREAD,
            y: n.starfield.y * SPREAD,
            z: n.starfield.z * SPREAD,
          });
          if (n.category === 'short' && n.t) {
            // 短期锚点：把「创建时间 + 命运 + 目标」记下来，流动粒子用
            anchorMeta.set(n.id, {
              t: n.t,
              destiny: String((n as any).destiny ?? 'longterm'),
              tx: Number((n as any).targetX ?? 0) * SPREAD,
              ty: Number((n as any).targetY ?? 0) * SPREAD,
              tz: Number((n as any).targetZ ?? 0) * SPREAD,
            });
          }
          continue;
        }

        // 非短期记忆：直接用它自己的坐标（骨架常显）
        if (n.category !== 'short' || !n.t) {
          visArr[i] = 1;
          tweenTarget.set(i, {
            x: n.starfield.x * SPREAD,
            y: n.starfield.y * SPREAD,
            z: n.starfield.z * SPREAD,
          });
          continue;
        }

        // 短期记忆：还没「出生」的记忆不该出现 ——
        // 时间指针在它的创建时间之前 → 它根本还不存在（初始帧就该是一片空）
        if (nowMs < n.t) {
          visArr[i] = 0;
          continue;
        }
        visArr[i] = 1;

        const ageH = Math.max(0, (nowMs - n.t) / 3600000);
        const life = Math.min(1, ageH / 24);   // 24 小时走完一生（原来 72h 太长，球到不了分流）
        const ringX = n.starfield.x * SPREAD;
        const ringY = n.starfield.y * SPREAD;
        const ringZ = n.starfield.z * SPREAD;
        const tX = Number((n as any).targetX ?? 0) * SPREAD;
        const tY = Number((n as any).targetY ?? 0) * SPREAD;
        const tZ = Number((n as any).targetZ ?? 0) * SPREAD;

        let px: number, py: number, pz: number;
        if (life < 0.12) {
          // ① 刚诞生：白洞 → 短期环（短暂的出生段）
          const k = life / 0.12;
          const e = k * k * (3 - 2 * k);
          // 出生点撒开：用节点 id 的哈希做一个稳定的偏移，
          // 否则几百颗球全叠在白洞那一个点上，看不出「喷射」。
          let hsh = 0;
          for (let ci = 0; ci < n.id.length; ci++) hsh = (hsh * 31 + n.id.charCodeAt(ci)) | 0;
          const jitter = 0.10 + ((Math.abs(hsh) % 100) / 100) * 0.22;
          const jang = ((Math.abs(hsh >> 7) % 360) / 360) * Math.PI * 2;
          const jy = (((Math.abs(hsh >> 13) % 100) / 100) - 0.5) * 0.18;
          px = WH_X + (ringX - WH_X) * e + Math.cos(jang) * jitter * (1 - e);
          py = WH_Y + (ringY - WH_Y) * e + jy * (1 - e);
          pz = WH_Z + (ringZ - WH_Z) * e + Math.sin(jang) * jitter * (1 - e);
        } else if (life < 0.25) {
          // ② 停在短期环上（也短，马上开始分流）
          px = ringX; py = ringY; pz = ringZ;
        } else {
          // ③ 分流：占 75% 生命周期，能清楚看到粒子在路上
          const k = Math.min(1, (life - 0.25) / 0.75);
          // 用 sqrt 而不是立方：粒子一离开环就开始明显位移，
          // 立方会让它们在前 80% 时间里几乎不动，全堆在环上。
          const e = Math.sqrt(k);
          px = ringX + (tX - ringX) * e;
          py = ringY + (tY - ringY) * e;
          pz = ringZ + (tZ - ringZ) * e;
        }
        tweenTarget.set(i, { x: px, y: py, z: pz });
      }
      nodeGeo.attributes.aVis.needsUpdate = true;
      updateFlow(cutoffMs);
    },
    fadeToBlackhole(ids: string[]) {
      const items: Array<{ idx: number; id: string }> = [];
      for (const id of ids) {
        const i = indexOf.get(id);
        if (i === undefined) continue;
        visArr[i] = 1;         // 先保持可见，动画从它自己的位置起飞
        items.push({ idx: i, id });
      }
      nodeGeo.attributes.aVis.needsUpdate = true;
      launchFlyers(items);
    },
    dispose() {
      scopeGlows.forEach((g) => {
        scene.remove(g.sp);
        (g.sp.material as THREE.SpriteMaterial).dispose();
      });
      running = false;
      cancelAnimationFrame(raf);
      document.removeEventListener('visibilitychange', onVis);
      renderer.domElement.removeEventListener('pointermove', onMove);
      renderer.domElement.removeEventListener('click', onClick);
      renderer.domElement.removeEventListener('dblclick', onDbl);
      ro.disconnect();
      controls.dispose();
      nodeGeo.dispose();
      nodeMat.dispose();
      hiGeo.dispose();
      hiMat.dispose();
      intraLines.dispose();
      for (const m of clickRipplePool) {
        m.geometry.dispose();
        (m.material as THREE.Material).dispose();
      }
      crossLines.dispose();
      ownerLines.dispose();
      mflowGeo.dispose();
      mflowMat.dispose();
      dustGeo.dispose();
      dustMat.dispose();
      renderer.dispose();
      scopeLabels.forEach((o) => {
        scene.remove(o);
        o.element.remove();
      });
      labelDom.remove();
      if (renderer.domElement.parentElement === container) {
        container.removeChild(renderer.domElement);
      }
    },
  };
}
