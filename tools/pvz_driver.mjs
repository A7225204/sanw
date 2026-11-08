/**
 * PvZ 自动玩驱动：直连 kimi-webbridge daemon 的 WebSocket（10086）。
 *
 * 比 `npx kimi-webbridge evaluate` 快得多：CLI 每次要起进程 + 建连（1~2 秒），
 * 这里保持一条长连接，每一步只要 10ms 级。
 *
 * 用法：
 *   node tools/pvz_driver.mjs            一直跑
 *   node tools/pvz_driver.mjs --once 30  跑 30 秒
 */
import { WebSocket } from "ws";
import { appendFileSync } from "node:fs";
import { resolve } from "node:path";

const WS_URL = process.env.WS_URL || "ws://127.0.0.1:10086/ws";
const EVENT_FILE = resolve(process.cwd(), "memory", "pvz_events.jsonl");
const INTERVAL = 900;

const onceIdx = process.argv.indexOf("--once");
const RUN_SECONDS = onceIdx > 0 ? Number(process.argv[onceIdx + 1] || 30) : 0;

// 一次 tick 干的活：收阳光 → 有牌且冷却好就种到第一个空位
const TICK_JS = `
(function () {
  var o = window.oS || {};
  var out = { sun: o.SunNum, suns: 0, plants: (window.$P || []).length,
              zombies: (window.$Z || []).length, level: o.LevelName || '', planted: null };
  try {
    var ids = Object.keys(window.ArSun || {}).filter(function (k) { return ArSun[k] && ArSun[k].C; });
    if (ids.length) {
      ids.forEach(function (k) { try { ClickSun(k); } catch (e) {} });
      out.suns = ids.length;
    }
    var cards = window.ArCard || [];
    if (cards.length && !o.Chose) {
      var c = cards[0];
      var cost = (c && c.PName && c.PName.prototype && c.PName.prototype.SunNum) || 100;
      if (c && c.CDReady && c.SunReady && o.SunNum >= cost) {
        var u = {};
        (window.$P || []).forEach(function (p) { u[p.R + '_' + p.C] = 1; });
        var sp = null;
        for (var r = 0; r < (o.R || 5) && !sp; r++) {
          for (var cc = 1; cc < 6 && !sp; cc++) {
            if (!u[r + '_' + cc]) sp = { r: r, c: cc };
          }
        }
        if (sp) {
          o.ChoseCard = 0; o.Chose = 1;
          GroundOnmousedown({
            clientX: GetX(sp.c) + EDAlloffsetLeft,
            clientY: GetY(sp.r),
            which: 1, button: 0,
            stopPropagation: function () {}, preventDefault: function () {}
          });
          out.planted = sp;
        }
      }
    }
  } catch (e) { out.err = e.message; }
  return JSON.stringify(out);
})()`;

function logEvent(kind, text, extra = {}) {
  const rec = { ts: Date.now() / 1000, at: new Date().toLocaleTimeString("zh-CN"),
                kind, text, ...extra };
  try {
    appendFileSync(EVENT_FILE, JSON.stringify(rec) + "\n", "utf8");
  } catch (e) { /* 静默 */ }
}

function connect() {
  return new Promise((res, rej) => {
    const ws = new WebSocket(WS_URL);
    ws.on("open", () => res(ws));
    ws.on("error", rej);
  });
}

function call(ws, action, args = {}) {
  return new Promise((res) => {
    const id = Math.random().toString(36).slice(2);
    const onMsg = (raw) => {
      try {
        const m = JSON.parse(raw.toString());
        if (m.id !== id && m.action !== action) return;
        ws.off("message", onMsg);
        res(m);
      } catch (e) { /* 忽略 */ }
    };
    ws.on("message", onMsg);
    ws.send(JSON.stringify({ action, args, id }));
    setTimeout(() => { ws.off("message", onMsg); res(null); }, 8000);
  });
}

async function main() {
  console.log("连 daemon:", WS_URL);
  const ws = await connect();
  console.log("已连接");
  logEvent("start", "她开始打植物大战僵尸");

  const t0 = Date.now();
  let last = {};
  let n = 0;

  const timer = setInterval(async () => {
    if (RUN_SECONDS && (Date.now() - t0) / 1000 > RUN_SECONDS) {
      clearInterval(timer);
      logEvent("stop", "这一局结束了");
      console.log("结束");
      ws.close();
      process.exit(0);
    }
    try {
      const r = await call(ws, "evaluate", { code: TICK_JS });
      const raw = r && r.data && (r.data.value ?? r.data.result);
      if (!raw || typeof raw !== "string") return;
      const st = JSON.parse(raw);
      n += 1;
      if (st.suns > 0) {
        logEvent("sun", `收了 ${st.suns} 个阳光（现在 ${st.sun}）`);
      }
      if (st.planted) {
        logEvent("plant", `在第 ${st.planted.r} 行第 ${st.planted.c} 列种了一株`,
                 { row: st.planted.r, col: st.planted.c });
      }
      if (st.zombies > (last.zombies || 0)) {
        logEvent("zombie", `来了 ${st.zombies - (last.zombies || 0)} 只僵尸`, { zombies: st.zombies });
      }
      if (st.err) console.log("页面报错:", st.err);
      last = st;
      if (n % 5 === 0 || st.planted || st.suns) {
        console.log(`[${new Date().toLocaleTimeString("zh-CN")}] ${st.level} 阳光=${st.sun} ` +
                    `植物=${st.plants} 僵尸=${st.zombies} 收+${st.suns}`);
      }
    } catch (e) {
      console.log("tick 失败:", e.message);
    }
  }, INTERVAL);
}

main().catch((e) => { console.error("失败:", e.message); process.exit(1); });
