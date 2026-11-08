"""她自己打 PvZ（自动开局 → 收阳光 → 种豌豆 → 写战况给机器人解说）。

关键实现（今天实测出来的）：
- 种植 = 选卡 + 调 `GroundOnmousedown({clientX, clientY, which:1})`，
  等价于「鼠标移到格子上按下左键」，不需要模拟拖拽。
- 卡片有冷却：必须等 `CDReady && SunReady` 才能种下一株。
- 格子坐标 = `GetX(col) + EDAlloffsetLeft` / `GetY(row)`（CSS 坐标，不受页面 zoom 影响）。

用法：
  python tools/pvz_live.py            一直玩
  python tools/pvz_live.py --once 60  只跑 60 秒
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
EVENT_FILE = ROOT / "memory" / "pvz_events.jsonl"
URL = "http://127.0.0.1:8091/"
CONTINUE_POS = [(860, 280), (780, 285), (900, 280), (860, 270)]

ST_JS = """() => {
    const o = window.oS || {};
    return {
        level: o.LevelName || '', sun: (typeof o.SunNum === 'number') ? o.SunNum : -1,
        plants: (window.$P || []).length,
        zombies: Object.keys(window.$Z || {}).length,   // $Z 是对象不是数组！
        cards: (window.ArCard || []).length,
        rows: Object.keys(window.$Z || {}).map(k => (window.$Z[k] || {}).R),
    };
}"""

# 一次 tick：收阳光 + 分层决策种植物（照搬 PvzLast 的 分析→阶段→行动 思路）
TICK_JS = """() => {
    const out = {sun: -1, suns: 0, plants: 0, zombies: 0, level: '', planted: null,
                 kind: '', phase: '', err: ''};
    try {
        const o = window.oS || {};
        out.level = o.LevelName || '';

        // 输了 → 重开同一关
        if (document.getElementById('iGameOver')) {
            const lv = o.Lvl || 3;
            out.gameover = true;
            out.level = 'GameOver';
            try {
                if (typeof SelectModal === 'function') SelectModal(lv);
                else if (typeof LoadLvl === 'function') LoadLvl(lv);
            } catch (e) { out.restartErr = e.message; }
            return JSON.stringify(out);
        }

        // 1) 收阳光
        const sunIds = Object.keys(window.ArSun || {}).filter(k => ArSun[k] && ArSun[k].C);
        if (sunIds.length) {
            sunIds.forEach(k => { try { ClickSun(k); } catch (e) {} });
            out.suns = sunIds.length;
        }

        // 2) 分析局面：每行僵尸数量/血量/最靠左位置
        const R = o.R || 5, C = o.C || 9;
        const zrows = {};
        Object.keys(window.$Z || {}).forEach(k => {
            const z = window.$Z[k];
            if (!z || z.PZ === false) return;
            const r = z.R;
            if (typeof r !== 'number') return;
            const hp = (typeof z.HP === 'number') ? z.HP : 200;
            (zrows[r] = zrows[r] || {n: 0, hp: 0, cols: []});
            zrows[r].n += 1;
            zrows[r].hp += hp;
            const col = (typeof z.C === 'number') ? z.C
                      : (typeof z.ZX === 'number') ? Math.floor(z.ZX / 80) : 9;
            zrows[r].cols.push(col);
        });
        const rowList = Object.keys(zrows).map(r => ({
            row: Number(r), n: zrows[r].n, hp: zrows[r].hp,
            minCol: Math.min.apply(null, zrows[r].cols),
        }));
        out.zombies = rowList.reduce((a, b) => a + b.n, 0);

        // 场上植物分布
        const used = {};
        let sunflowers = 0, peas = 0;
        (window.$P || []).forEach(p => {
            used[p.R + '_' + p.C] = 1;
            const pics = ((p && p.PicArr) || []).join(',').toLowerCase();
            if (pics.indexOf('sunflower') >= 0) sunflowers += 1;
            else if (pics.indexOf('peashooter') >= 0) peas += 1;
        });
        out.sunflowers = sunflowers;
        out.peas = peas;

        const cards = window.ArCard || [];
        if (!cards.length) {
            out.sun = o.SunNum; out.plants = (window.$P || []).length;
            return JSON.stringify(out);
        }
        const picOf = (c) => {
            const pr = (c && c.PName && c.PName.prototype) || {};
            return ((pr.PicArr && pr.PicArr[0]) || '').toLowerCase();
        };
        let sunnyIdx = -1, peaIdx = -1;
        cards.forEach((c, i) => {
            const pic = picOf(c);
            if (pic.indexOf('sunflower') >= 0) sunnyIdx = i;
            else if (pic.indexOf('peashooter') >= 0) peaIdx = i;
        });

        // 3) 阶段判断
        //    urgent : 有僵尸进到第 3 列以内
        //    defense: 有僵尸但还没到近处
        //    economy: 没有僵尸 → 攒经济
        const urgent = rowList.some(r => r.minCol <= 3);
        const threat = rowList.length > 0;
        // 每行"火力够不够"：豌豆 20 伤 / 1.4 秒，僵尸速度约 1 格 / 4.5 秒
        // 粗略要求：每行至少 1 株豌豆，血厚的行 2 株
        const weakRows = rowList.filter(r => {
            const need = r.hp > 500 ? 2 : 1;
            // 这行有几株豌豆（按列判断，简化为总数平均）
            return need > (peas / Math.max(1, rowList.length));
        });

        let phase = 'economy';
        let want = -1;
        if (urgent && peaIdx >= 0) { phase = 'urgent'; want = peaIdx; }
        else if (threat && weakRows.length && peaIdx >= 0) { phase = 'defense'; want = peaIdx; }
        else if (sunnyIdx >= 0 && sunflowers < 8) { phase = 'economy'; want = sunnyIdx; }
        else if (peaIdx >= 0) { phase = 'defense'; want = peaIdx; }
        else if (sunnyIdx >= 0) { phase = 'economy'; want = sunnyIdx; }
        out.phase = phase;

        // 4) 行动：选卡 → 找空位 → 调地面处理器
        if (want >= 0 && !o.Chose) {
            const c = cards[want];
            const cost = (c && c.PName && c.PName.prototype && c.PName.prototype.SunNum) || 100;
            if (c && c.CDReady && c.SunReady && o.SunNum >= cost) {
                // 豌豆优先种在"有僵尸的最近行"的中间列；向日葵种最左列
                let targets = [];
                if (want === peaIdx) {
                    const hot = (urgent ? rowList.slice().sort((a, b) => a.minCol - b.minCol)
                                       : rowList.slice().sort((a, b) => b.hp - a.hp));
                    const rows = hot.length ? hot.map(h => h.row) : [2, 1, 3, 0, 4];
                    for (const r of rows) for (const cc of [1, 2, 0, 3, 4]) targets.push([r, cc]);
                } else {
                    for (const cc of [0, 1]) for (const r of [2, 3, 1, 4, 0]) targets.push([r, cc]);
                }
                for (const t of targets) {
                    const r = t[0], cc = t[1];
                    if (r >= R || cc >= C || used[r + '_' + cc]) continue;
                    const before = o.SunNum;
                    o.ChoseCard = want;
                    o.Chose = 1;
                    try {
                        GroundOnmousedown({
                            clientX: GetX(cc) + EDAlloffsetLeft,
                            clientY: GetY(r),
                            which: 1, button: 0,
                            stopPropagation() {}, preventDefault() {},
                        });
                    } catch (e) { o.Chose = 0; }
                    if (o.SunNum < before) {
                        out.planted = {r, c: cc};
                        out.kind = (want === sunnyIdx) ? '向日葵' : '豌豆';
                        break;
                    }
                    o.Chose = 0;
                }
            }
        }
        out.sun = (typeof o.SunNum === 'number') ? o.SunNum : -1;
        out.plants = (window.$P || []).length;
        out.cards = (window.ArCard || []).length;
    } catch (e) { out.err = e.message; }
    return JSON.stringify(out);
}"""


def emit(kind: str, text: str, extra: dict | None = None) -> None:
    try:
        EVENT_FILE.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": time.time(), "at": time.strftime("%H:%M:%S"), "kind": kind, "text": text}
        if extra:
            rec.update(extra)
        with EVENT_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"[事件] {kind}: {text}")
    except Exception as e:  # noqa: BLE001
        print("写事件失败:", e)


async def start_game(page, level: int = 3) -> bool:
    """直接跳关进入战场（level/1.js 起才是真关，0.js 是新手教程）。"""
    jumped = await page.evaluate("""(n) => {
        try {
            if (typeof LoadLvl === 'function') { LoadLvl(n); return 'LoadLvl:' + n; }
            if (typeof StartAdventure === 'function') { StartAdventure(); return 'StartAdventure'; }
            return 'none';
        } catch (e) { return 'err:' + e.message; }
    }""", level)
    print("跳关:", jumped)
    await page.wait_for_timeout(4000)
    for i in range(20):
        st = await page.evaluate(ST_JS)
        if st.get("cards", 0) > 0 or st.get("plants", 0) > 0:
            print("进入战斗:", st)
            return True
        x, y = CONTINUE_POS[i % len(CONTINUE_POS)]
        await page.mouse.click(x, y)
        await page.wait_for_timeout(1500)
    print("开局未完成")
    return False


async def run(seconds: float = 0, level: int = 3) -> None:
    from playwright.async_api import async_playwright

    t0 = time.time()
    last: dict = {}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="msedge", headless=False,
                                          args=["--no-first-run", "--no-default-browser-check"])
        try:
            ctx = await browser.new_context(viewport={"width": 1400, "height": 900})
            page = await ctx.new_page()
            await page.goto(URL, wait_until="load", timeout=30000)
            await page.wait_for_timeout(3500)
            emit("start", "她打开了植物大战僵尸")
            await start_game(page, level)
            emit("enter", "进战场了")

            tick = 0
            while True:
                if seconds and time.time() - t0 > seconds:
                    break
                tick += 1
                try:
                    raw = await page.evaluate(TICK_JS)
                    st = json.loads(raw)
                except Exception as e:  # noqa: BLE001
                    emit("error", f"读不到局面：{type(e).__name__}")
                    await page.wait_for_timeout(2000)
                    continue

                if st.get("gameover"):
                    emit("gameover", "这局输了，自动重开")
                    # 新一局会弹剧情/选卡，连点几下推进
                    for _ in range(10):
                        await page.mouse.click(860, 280)
                        await page.wait_for_timeout(1200)
                        try:
                            _r = await page.evaluate(TICK_JS)
                            _s2 = json.loads(_r)
                        except Exception:  # noqa: BLE001
                            continue
                        if _s2.get("cards", 0) > 0 or _s2.get("plants", 0) > 0:
                            last = {"level": _s2.get("level"), "zombies": 0}
                            break
                    await page.wait_for_timeout(900)
                    continue
                if st.get("level") and st["level"] != last.get("level"):
                    emit("level", f"进入「{st['level']}」")
                if st.get("suns"):
                    emit("sun", f"收了 {st['suns']} 个阳光（现在 {st.get('sun')}）")
                if st.get("planted"):
                    p = st["planted"]
                    emit("plant", f"在第 {p['r']} 行第 {p['c']} 列种了{st.get('kind') or '一株'}",
                         {"row": p["r"], "col": p["c"], "kind": st.get("kind"), "phase": st.get("phase")})
                if st.get("zombies", 0) > last.get("zombies", 0):
                    emit("zombie", f"来了 {st['zombies'] - last.get('zombies', 0)} 只僵尸",
                         {"zombies": st["zombies"], "rows": st.get("rows")})
                if st.get("err"):
                    print("页面报错:", st["err"])

                last = st
                if tick % 5 == 0 or st.get("planted"):
                    print(f"[{time.strftime('%H:%M:%S')}] {st.get('level')} 阳光={st.get('sun')} "
                          f"植物={st.get('plants')} 僵尸={st.get('zombies')} 收+{st.get('suns')}")
                await page.wait_for_timeout(900)
        finally:
            await browser.close()
            emit("stop", "这一局结束了")


if __name__ == "__main__":
    secs = 0.0
    if "--once" in sys.argv:
        i = sys.argv.index("--once")
        secs = float(sys.argv[i + 1]) if len(sys.argv) > i + 1 else 60.0
    lvl = 3
    if "--level" in sys.argv:
        j = sys.argv.index("--level")
        lvl = int(sys.argv[j + 1]) if len(sys.argv) > j + 1 else 1
    asyncio.run(run(secs, lvl))
