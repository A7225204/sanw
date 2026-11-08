/* 大肥鱼的 3D 小家 —— 场景与动作库（2026-11-05）
 *
 * 为什么要用代码造卧室：原来的 57MB 3ds Max 导出模型贴图全丢，
 * 材质是纯黑的灰块，还得靠猜颜色补。代码造的房间每个东西都知道是什么颜色。
 *
 * 动作为什么不用外部库：BOOTH 的免费 VRMA 要登录，GitHub 开源 VRMA 极少。
 * 程序化动作可控、无版权问题，还能按情绪状态实时切换。
 */

import * as THREE from 'three';

// ═══════════════════════════════════════════
//  调色板
// ═══════════════════════════════════════════
const C = {
  floor:    0xb08a63,   // 木地板（白天偏暖）
  floorDk:  0x94714f,
  wall:     0xd8dfe8,   // 白墙（白天）
  wallTop:  0xc8d2de,
  bed:      0x6d7f9e,   // 床架
  sheet:    0xf7fafd,   // 床单
  blanket:  0x7f9dc9,   // 被子
  pillow:   0xffffff,
  desk:     0xa8825c,   // 书桌
  chair:    0x6d7a94,
  rug:      0x8a76a8,
  lamp:     0xffd28a,   // 台灯暖光
  shelf:    0x8a6f52,
  screen:   0x2a6fa8,   // 显示器冷光
  sky:      0xa8d4f0,   // 白天天空
  sun:      0xfff2c8,
};

// ═══════════════════════════════════════════
//  卧室：全部用基础几何体拼
// ═══════════════════════════════════════════
export function buildBedroom(scene) {
  const room = new THREE.Group();
  const W = 5.4, D = 5.0, H = 2.7;      // 宽 深 高

  const mat = (color, rough = 0.85, metal = 0.05) =>
    new THREE.MeshStandardMaterial({ color, roughness: rough, metalness: metal });

  // ── 地板（一条条木纹，比纯色板自然）
  for (let i = 0; i < 14; i++) {
    const w = W / 14;
    const plank = new THREE.Mesh(
      new THREE.BoxGeometry(w * 0.97, 0.04, D),
      mat(i % 2 ? C.floor : C.floorDk, 0.9, 0.02)
    );
    plank.position.set(-W / 2 + w * (i + 0.5), -0.02, 0);
    room.add(plank);
  }

  // ── 墙（后 + 左 + 右，前面留空给镜头）
  const wallMat = mat(C.wall, 0.95, 0);
  const back = new THREE.Mesh(new THREE.BoxGeometry(W, H, 0.08), wallMat);
  back.position.set(0, H / 2, -D / 2);
  room.add(back);

  [-1, 1].forEach(s => {
    const side = new THREE.Mesh(new THREE.BoxGeometry(0.08, H, D), wallMat);
    side.position.set(s * W / 2, H / 2, 0);
    room.add(side);
  });

  // 墙裙（一圈深色踢脚，房间立刻"完整"）
  const skirt = mat(0x1e2434, 0.85, 0.05);
  const sk1 = new THREE.Mesh(new THREE.BoxGeometry(W, 0.18, 0.1), skirt);
  sk1.position.set(0, 0.09, -D / 2 + 0.06); room.add(sk1);
  [-1, 1].forEach(s => {
    const sk = new THREE.Mesh(new THREE.BoxGeometry(0.1, 0.18, D), skirt);
    sk.position.set(s * (W / 2 - 0.06), 0.09, 0); room.add(sk);
  });

  // ── 窗户（后墙开一扇，外面是夜空）
  const winW = 1.5, winH = 1.2, winY = 1.75, winX = -1.0;
  const frame = mat(0x2c3448, 0.7, 0.1);
  // 窗框
  [[0, winH / 2, 0, winW + 0.1, 0.06], [0, -winH / 2, 0, winW + 0.1, 0.06]].forEach(([x, y, z, w, h]) => {
    const b = new THREE.Mesh(new THREE.BoxGeometry(w, h, 0.1), frame);
    b.position.set(winX + x, winY + y, -D / 2 + 0.05); room.add(b);
  });
  [[-winW / 2, 0, 0, 0.06, winH], [winW / 2, 0, 0, 0.06, winH]].forEach(([x, y, z, w, h]) => {
    const b = new THREE.Mesh(new THREE.BoxGeometry(w, h, 0.1), frame);
    b.position.set(winX + x, winY + y, -D / 2 + 0.05); room.add(b);
  });
  // 中横档
  const mullion = new THREE.Mesh(new THREE.BoxGeometry(0.05, winH, 0.08), frame);
  mullion.position.set(winX, winY, -D / 2 + 0.05); room.add(mullion);

  // 窗外：白天天空（渐变感靠两层色块叠）
  const skyGeo = new THREE.PlaneGeometry(winW, winH * 0.72);
  const skyMat = new THREE.MeshBasicMaterial({ color: C.sky });
  const sky = new THREE.Mesh(skyGeo, skyMat);
  sky.position.set(winX, winY + winH * 0.14, -D / 2 - 0.02);
  room.add(sky);

  // 远处的地面/树影（下半截深一点，有纵深）
  const groundGeo = new THREE.PlaneGeometry(winW, winH * 0.30);
  const groundMat = new THREE.MeshBasicMaterial({ color: 0x8fae8a });
  const ground = new THREE.Mesh(groundGeo, groundMat);
  ground.position.set(winX, winY - winH * 0.34, -D / 2 - 0.021);
  room.add(ground);

  // 太阳
  const sun = new THREE.Mesh(
    new THREE.CircleGeometry(0.12, 24),
    new THREE.MeshBasicMaterial({ color: C.sun })
  );
  sun.position.set(winX + 0.42, winY + 0.28, -D / 2 - 0.015);
  room.add(sun);
  window.__sun = sun;

  // 云（三朵小白团，慢慢飘）
  window.__clouds = [];
  for (let i = 0; i < 3; i++) {
    const cloud = new THREE.Group();
    for (let k = 0; k < 3; k++) {
      const puff = new THREE.Mesh(
        new THREE.CircleGeometry(0.055 + k * 0.012, 16),
        new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.88 })
      );
      puff.position.set((k - 1) * 0.06, Math.abs(k - 1) * -0.012, 0);
      cloud.add(puff);
    }
    cloud.position.set(winX - 0.45 + i * 0.42, winY + 0.10 + (i % 2) * 0.16, -D / 2 - 0.013);
    room.add(cloud);
    window.__clouds.push(cloud);
  }

  // 阳光从窗户斜照进来（一个亮的平面投在地上）
  const sunPatch = new THREE.Mesh(
    new THREE.PlaneGeometry(1.5, 1.1),
    new THREE.MeshBasicMaterial({ color: 0xfff0d0, transparent: true, opacity: 0.16 })
  );
  sunPatch.rotation.x = -Math.PI / 2;
  sunPatch.rotation.z = 0.22;
  sunPatch.position.set(winX + 0.55, 0.008, -D / 2 + 1.05);
  room.add(sunPatch);

  // ── 床（靠右墙）
  const bedG = new THREE.Group();
  const bedW = 1.5, bedD = 2.1, bedH = 0.42;
  // 床架
  const frameBed = new THREE.Mesh(new THREE.BoxGeometry(bedW, bedH * 0.55, bedD), mat(C.bed, 0.8, 0.1));
  frameBed.position.y = bedH * 0.275;
  bedG.add(frameBed);
  // 床垫
  const mattress = new THREE.Mesh(new THREE.BoxGeometry(bedW * 0.96, bedH * 0.5, bedD * 0.97), mat(0xc9d4e6, 0.95, 0));
  mattress.position.y = bedH * 0.55 + bedH * 0.25;
  bedG.add(mattress);
  // 床单（盖住下半段）
  const sheet = new THREE.Mesh(new THREE.BoxGeometry(bedW * 0.98, 0.06, bedD * 0.62), mat(C.sheet, 0.92, 0));
  sheet.position.set(0, bedH * 0.55 + bedH * 0.5 + 0.02, bedD * 0.16);
  bedG.add(sheet);
  // 被子（有厚度，边角垂下来一点）
  const blanket = new THREE.Mesh(new THREE.BoxGeometry(bedW * 1.02, 0.13, bedD * 0.55), mat(C.blanket, 0.95, 0));
  blanket.position.set(0, bedH * 0.55 + bedH * 0.5 + 0.09, -bedD * 0.18);
  bedG.add(blanket);
  // 枕头
  const pillow = new THREE.Mesh(new THREE.BoxGeometry(bedW * 0.62, 0.13, 0.34), mat(C.pillow, 0.98, 0));
  pillow.position.set(0, bedH * 0.55 + bedH * 0.5 + 0.10, -bedD * 0.40);
  pillow.rotation.x = -0.12;
  bedG.add(pillow);
  // 床头板
  const headboard = new THREE.Mesh(new THREE.BoxGeometry(bedW, 0.72, 0.09), mat(C.bed, 0.75, 0.12));
  headboard.position.set(0, 0.36, -bedD / 2 - 0.04);
  bedG.add(headboard);

  bedG.position.set(W / 2 - bedW / 2 - 0.18, 0, -D / 2 + bedD / 2 + 0.25);
  room.add(bedG);

  // ── 书桌（靠左墙）
  const deskG = new THREE.Group();
  const deskW = 1.9, deskD = 0.72, deskH = 0.74;
  // 桌面
  const top = new THREE.Mesh(new THREE.BoxGeometry(deskW, 0.06, deskD), mat(C.desk, 0.7, 0.08));
  top.position.y = deskH;
  deskG.add(top);
  // 四条腿
  [[-1, -1], [-1, 1], [1, -1], [1, 1]].forEach(([sx, sz]) => {
    const leg = new THREE.Mesh(new THREE.BoxGeometry(0.07, deskH, 0.07), mat(0x4a3828, 0.85, 0.05));
    leg.position.set(sx * (deskW / 2 - 0.08), deskH / 2, sz * (deskD / 2 - 0.08));
    deskG.add(leg);
  });

  // 显示器 + 屏幕光
  const monBase = new THREE.Mesh(new THREE.BoxGeometry(0.34, 0.03, 0.18), mat(0x1a1f2a, 0.6, 0.3));
  monBase.position.set(0, deskH + 0.045, -0.12);
  deskG.add(monBase);
  const monStand = new THREE.Mesh(new THREE.BoxGeometry(0.06, 0.24, 0.05), mat(0x1a1f2a, 0.6, 0.3));
  monStand.position.set(0, deskH + 0.16, -0.12);
  deskG.add(monStand);
  const screenBody = new THREE.Mesh(new THREE.BoxGeometry(0.92, 0.54, 0.04), mat(0x12161e, 0.5, 0.35));
  screenBody.position.set(0, deskH + 0.54, -0.13);
  deskG.add(screenBody);
  const screenFace = new THREE.Mesh(
    new THREE.PlaneGeometry(0.86, 0.48),
    new THREE.MeshBasicMaterial({ color: C.screen })
  );
  screenFace.position.set(0, deskH + 0.54, -0.105);
  deskG.add(screenFace);
  // 屏幕背光（往墙上打一片蓝，这是氛围的关键）
  const screenGlow = new THREE.PointLight(0x4a8fd8, 2.2, 3.2, 2);
  screenGlow.position.set(0, deskH + 0.54, -0.02);
  deskG.add(screenGlow);
  window.__screenGlow = screenGlow;

  // 键盘鼠标
  const kb = new THREE.Mesh(new THREE.BoxGeometry(0.42, 0.02, 0.14), mat(0x252b38, 0.7, 0.15));
  kb.position.set(0, deskH + 0.04, 0.16);
  deskG.add(kb);

  // 台灯（暖光）
  const lampBase = new THREE.Mesh(new THREE.CylinderGeometry(0.09, 0.11, 0.03, 16), mat(0x2c3444, 0.6, 0.3));
  lampBase.position.set(deskW / 2 - 0.22, deskH + 0.045, -0.02);
  deskG.add(lampBase);
  const lampArm = new THREE.Mesh(new THREE.CylinderGeometry(0.014, 0.014, 0.34, 8), mat(0x2c3444, 0.6, 0.3));
  lampArm.position.set(deskW / 2 - 0.22, deskH + 0.22, -0.02);
  deskG.add(lampArm);
  const lampShade = new THREE.Mesh(new THREE.ConeGeometry(0.13, 0.16, 16, 1, true), mat(0x2c3444, 0.6, 0.3, true));
  lampShade.position.set(deskW / 2 - 0.22, deskH + 0.42, -0.02);
  lampShade.rotation.x = Math.PI;
  deskG.add(lampShade);
  const lampLight = new THREE.PointLight(C.lamp, 1.8, 2.8, 2);
  lampLight.position.set(deskW / 2 - 0.22, deskH + 0.36, -0.02);
  deskG.add(lampLight);
  window.__lampLight = lampLight;

  // 一摞书
  for (let i = 0; i < 3; i++) {
    const bk = new THREE.Mesh(
      new THREE.BoxGeometry(0.22, 0.035, 0.16),
      mat([0x8a4a4a, 0x4a6a8a, 0x8a7a4a][i], 0.9, 0)
    );
    bk.position.set(-deskW / 2 + 0.3, deskH + 0.05 + i * 0.035, 0.05);
    bk.rotation.y = (i - 1) * 0.12;
    deskG.add(bk);
  }

  deskG.position.set(-W / 2 + deskW / 2 + 0.2, 0, -D / 2 + 0.75);
  room.add(deskG);

  // ── 椅子（桌子前，背对我们）
  const chairG = new THREE.Group();
  const seat = new THREE.Mesh(new THREE.BoxGeometry(0.46, 0.06, 0.44), mat(C.chair, 0.85, 0.05));
  seat.position.y = 0.45;
  chairG.add(seat);
  const backRest = new THREE.Mesh(new THREE.BoxGeometry(0.44, 0.42, 0.05), mat(C.chair, 0.85, 0.05));
  backRest.position.set(0, 0.68, -0.19);
  chairG.add(backRest);
  [[-1, -1], [-1, 1], [1, -1], [1, 1]].forEach(([sx, sz]) => {
    const leg = new THREE.Mesh(new THREE.BoxGeometry(0.04, 0.45, 0.04), mat(0x2e3546, 0.8, 0.1));
    leg.position.set(sx * 0.19, 0.225, sz * 0.18);
    chairG.add(leg);
  });
  chairG.position.set(-W / 2 + deskW / 2 + 0.2, 0, -D / 2 + 1.55);
  room.add(chairG);

  // ── 书架（左墙，桌子上方）
  const shelfG = new THREE.Group();
  for (let i = 0; i < 3; i++) {
    const board = new THREE.Mesh(new THREE.BoxGeometry(1.2, 0.04, 0.22), mat(C.shelf, 0.9, 0.03));
    board.position.set(0, 1.35 + i * 0.42, 0);
    shelfG.add(board);
    // 书
    for (let b = 0; b < 7; b++) {
      const bw = 0.04 + Math.random() * 0.03;
      const bh = 0.20 + Math.random() * 0.10;
      const col = [0x8a4a4a, 0x4a6a8a, 0x8a7a4a, 0x5a8a6a, 0x7a5a8a][b % 5];
      const book = new THREE.Mesh(new THREE.BoxGeometry(bw, bh, 0.15), mat(col, 0.9, 0));
      book.position.set(-0.5 + b * 0.155 + Math.random() * 0.02, 1.37 + i * 0.42 + bh / 2, 0);
      shelfG.add(book);
    }
  }
  // 侧板
  [-1, 1].forEach(s => {
    const side = new THREE.Mesh(new THREE.BoxGeometry(0.04, 1.3, 0.22), mat(C.shelf, 0.9, 0.03));
    side.position.set(s * 0.6, 1.77, 0);
    shelfG.add(side);
  });
  shelfG.position.set(-W / 2 + 0.16, 0, -D / 2 + 0.7);
  shelfG.rotation.y = Math.PI / 2;
  room.add(shelfG);

  // ── 地毯（桌子前一块，给她的位置一个"锚点"）
  const rug = new THREE.Mesh(
    new THREE.CircleGeometry(0.95, 48),
    mat(C.rug, 0.98, 0)
  );
  rug.rotation.x = -Math.PI / 2;
  rug.position.set(0, 0.005, 0.6);
  room.add(rug);
  // 地毯外圈
  const rugRing = new THREE.Mesh(
    new THREE.RingGeometry(0.80, 0.95, 48),
    mat(0x6e5a84, 0.98, 0)
  );
  rugRing.rotation.x = -Math.PI / 2;
  rugRing.position.set(0, 0.007, 0.6);
  room.add(rugRing);

  // ── 天花板灯（吸顶，整体底光）
  const ceilLight = new THREE.PointLight(0xfff6e8, 2.4, 7, 2);
  ceilLight.position.set(0, H - 0.15, 0);
  room.add(ceilLight);
  window.__ceilLight = ceilLight;

  scene.add(room);
  return { room, W, D, H, bedG, deskG, chairG, screenGlow, lampLight };
}

// ═══════════════════════════════════════════
//  程序化动作库
//  每个动作 = 一组骨骼旋转关键帧，用正弦/缓动合成
// ═══════════════════════════════════════════
export const POSES = {
  // 站立待机：手臂自然下垂、身体微晃
  idle: {
    leftUpperArm:  { z: 0.10, y: 0 },
    rightUpperArm: { z: -0.10, y: 0 },
    leftLowerArm:  { z: 0.06 },
    rightLowerArm: { z: -0.06 },
    spine: { x: 0 },
    head:  { x: 0 },
  },
  // 打招呼：右手抬起摆动
  wave: {
    rightUpperArm: { z: -2.0, x: -0.3 },
    rightLowerArm: { z: -0.5, x: -0.4 },
    head:  { z: 0.06 },
    spine: { z: -0.03 },
  },
  // 伸懒腰：双手举起、身体后仰
  stretch: {
    leftUpperArm:  { z: 2.2, x: -0.2 },
    rightUpperArm: { z: -2.2, x: -0.2 },
    leftLowerArm:  { z: 0.3 },
    rightLowerArm: { z: -0.3 },
    spine: { x: -0.12 },
    head:  { x: -0.10 },
  },
  // 坐着：臀部下移由外部处理，这里只调腿
  sit: {
    leftUpperLeg:  { x: -1.45 },
    rightUpperLeg: { x: -1.45 },
    leftLowerLeg:  { x: 1.5 },
    rightLowerLeg: { x: 1.5 },
    leftUpperArm:  { z: 0.16, x: 0.15 },
    rightUpperArm: { z: -0.16, x: 0.15 },
    spine: { x: 0.05 },
  },
  // 抱膝（坐地上）
  hug: {
    leftUpperLeg:  { x: -1.9 },
    rightUpperLeg: { x: -1.9 },
    leftLowerLeg:  { x: 2.0 },
    rightLowerLeg: { x: 2.0 },
    leftUpperArm:  { z: 0.6, x: 0.9 },
    rightUpperArm: { z: -0.6, x: 0.9 },
    leftLowerArm:  { z: 0.3, x: 0.7 },
    rightLowerArm: { z: -0.3, x: 0.7 },
    spine: { x: 0.18 },
    head:  { x: 0.10 },
  },
  // 双手插兜（酷）
  handsInPockets: {
    leftUpperArm:  { z: 0.42, x: 0.20 },
    rightUpperArm: { z: -0.42, x: 0.20 },
    leftLowerArm:  { z: 0.9, x: 0.55 },
    rightLowerArm: { z: -0.9, x: 0.55 },
    spine: { x: 0.02 },
  },
  // 害羞：双手背在身后、低头
  shy: {
    leftUpperArm:  { z: 0.9, x: 0.7 },
    rightUpperArm: { z: -0.9, x: 0.7 },
    leftLowerArm:  { z: 0.5 },
    rightLowerArm: { z: -0.5 },
    head: { x: 0.16, z: 0.08 },
    spine: { x: 0.06 },
  },
};

// 鼠标/交互用的表情对应动作
export const EMOTION_POSE = {
  happy:      'wave',
  angry:      'handsInPockets',
  sad:        'hug',
  relaxed:    'idle',
  surprised:  'stretch',
  neutral:    'idle',
  shy:        'shy',
};
