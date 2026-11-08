/**
 * 标签层：引线标签 + 防重叠 + 发光字
 *
 * 为什么单独抽出来：以前是「每 6 帧遍历 DOM 读 style 再改 style」，
 * 读到的是上一帧的值 → 标签在两套布局之间反复横跳，这就是那几像素的抽搐。
 * 现在布局全部在一帧内用纯数据算完，DOM 只写不读，位置只跟投影有关。
 *
 * 组成：
 *   锚点小圆点 + 一根极细引线 + 悬浮在引线顶端的文字
 *   文字不带胶囊底，用 text-shadow 发光
 *   重叠时按优先级隐藏次要标签；鼠标在画布上时全部显示
 */
import * as THREE from 'three';

export interface LabelItem {
  key: string;
  text: string;
  color: string;
  /** 锚点（世界坐标） */
  anchor: THREE.Vector3;
  /** 越重要越先摆，永远不会被隐藏 */
  priority: number;
  /** 字号，默认 12 */
  size?: number;
}

interface Slot {
  el: HTMLDivElement;
  line: SVGLineElement;
  dot: SVGCircleElement;
  w: number;
  h: number;
}

const SVG_NS = 'http://www.w3.org/2000/svg';

export interface LabelLayer {
  /**
   * 每帧调一次。内部只写 DOM、不读 DOM 布局（读布局会强制同步回流，也会引入抖动）
   */
  draw: (items: LabelItem[], camera: THREE.Camera, width: number, height: number) => void;
  dispose: () => void;
}

export function createLabelLayer(container: HTMLElement): LabelLayer {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('width', '100%');
  svg.setAttribute('height', '100%');
  svg.style.cssText = 'position:absolute;inset:0;pointer-events:none;z-index:5;overflow:visible;';
  container.appendChild(svg);

  const slots = new Map<string, Slot>();
  const tmp = new THREE.Vector3();

  // 悬停时全部显示
  let hovering = false;
  const onEnter = () => { hovering = true; };
  const onLeave = () => { hovering = false; };
  container.addEventListener('pointerenter', onEnter);
  container.addEventListener('pointerleave', onLeave);

  function makeSlot(key: string, color: string, size: number): Slot {
    const el = document.createElement('div');
    el.textContent = key;
    // 不要胶囊底：发光字 + 极淡描边，贴在星空上更高级
    el.style.cssText = 'position:absolute;transform:translate(-50%,-100%);white-space:nowrap;'
      + `font-size:${size}px;font-weight:500;letter-spacing:.5px;color:${color};`
      + 'pointer-events:none;user-select:none;will-change:transform;'
      + `text-shadow:0 0 6px ${color}cc,0 0 14px ${color}66,0 1px 2px #000;`;
    container.appendChild(el);

    const line = document.createElementNS(SVG_NS, 'line');
    line.setAttribute('stroke', color);
    line.setAttribute('stroke-width', '1');
    line.setAttribute('stroke-opacity', '0.35');
    svg.appendChild(line);

    const dot = document.createElementNS(SVG_NS, 'circle');
    dot.setAttribute('r', '1.6');
    dot.setAttribute('fill', color);
    dot.setAttribute('fill-opacity', '0.8');
    svg.appendChild(dot);

    return { el, line, dot, w: 80, h: 16 };
  }

  function hide(s: Slot) {
    s.el.style.opacity = '0';
    s.line.setAttribute('stroke-opacity', '0');
    s.dot.setAttribute('fill-opacity', '0');
  }

  return {
    draw(items, camera, width, height) {
      // 1) 投影 + 补齐槽位（布局全在数据里算）
      const placed: Array<{ x: number; y: number; w: number; h: number; primary: boolean }> = [];
      const sorted = items.slice().sort((a, b) => b.priority - a.priority);

      // 先按 key 建/取槽，再逐条摆，读取的尺寸用上一帧缓存（不碰 DOM 布局）
      for (const it of sorted) {
        let s = slots.get(it.key);
        if (!s) {
          s = makeSlot(it.key, it.color, it.size ?? 12);
          slots.set(it.key, s);
        }
        s.el.textContent = it.text;

        tmp.copy(it.anchor).project(camera);
        if (tmp.z > 1 || Math.abs(tmp.x) > 1.4 || Math.abs(tmp.y) > 1.4) { hide(s); continue; }

        const ax = (tmp.x * 0.5 + 0.5) * width;
        const ay = (-tmp.y * 0.5 + 0.5) * height;

        const w = s.w || 80;
        const h = s.h || 16;
        const primary = it.priority >= 100;

        // 文字放在锚点正上方，引线长度固定，避免随距离抖动
        let tx = ax;
        let ty = ay - 18 - h * 0.5;

        // 出画面就夹回来（只夹一次，不形成反馈）
        tx = Math.min(Math.max(tx, w * 0.5 + 4), width - w * 0.5 - 4);
        ty = Math.min(Math.max(ty, h + 6), height - 6);

        // 2) 防重叠：按优先级贪心摆放，撞上就隐藏（确定性算法，不会来回跳）
        const box = { x: tx, y: ty, w, h, primary };
        let collide = false;
        for (const p of placed) {
          if (Math.abs(p.x - tx) < (p.w + w) * 0.5 + 6
            && Math.abs(p.y - ty) < (p.h + h) * 0.5 + 4) { collide = true; break; }
        }
        if (collide && !primary && !hovering) { hide(s); continue; }
        placed.push(box);

        // 3) 写 DOM（只写不读）
        s.el.style.opacity = '1';
        s.el.style.left = `${tx}px`;
        s.el.style.top = `${ty + h * 0.5}px`;

        s.line.setAttribute('x1', `${ax}`);
        s.line.setAttribute('y1', `${ay}`);
        s.line.setAttribute('x2', `${tx}`);
        s.line.setAttribute('y2', `${ty + h * 0.5}`);
        s.line.setAttribute('stroke-opacity', '0.3');
        s.dot.setAttribute('cx', `${ax}`);
        s.dot.setAttribute('cy', `${ay}`);
        s.dot.setAttribute('fill-opacity', '0.75');

        // 尺寸只在文字变了之后测一次
        if (s.el.dataset.measured !== it.text) {
          s.w = s.el.offsetWidth;
          s.h = s.el.offsetHeight;
          s.el.dataset.measured = it.text;
        }
      }
    },
    dispose() {
      container.removeEventListener('pointerenter', onEnter);
      container.removeEventListener('pointerleave', onLeave);
      for (const s of slots.values()) { s.el.remove(); s.line.remove(); s.dot.remove(); }
      slots.clear();
      svg.remove();
    },
  };
}
