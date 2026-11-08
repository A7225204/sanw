import pathlib

# ---------- 1) 后端：历史点同时存激素 ----------
p = pathlib.Path(r"D:\qq-ai-bot\plugins\hormones.py")
s = p.read_text(encoding="utf-8")
old = '''        pts.append({"ts": time.strftime("%H:%M"), "e": e})'''
new = '''        pts.append({"ts": time.strftime("%H:%M"), "e": e, "h": {k: round(_load()[k], 1) for k in HORMONES}})'''
if old in s:
    s = s.replace(old, new, 1)
    p.write_text(s, encoding="utf-8")
    print("hormones: 历史点带激素")

# ---------- 2) 前端：曲线改画 5 条激素 ----------
h = pathlib.Path(r"D:\qq-ai-bot\dashboard\index.html")
t = h.read_text(encoding="utf-8")

old_js_start = t.find("  const EMO_ORDER = [")
old_js_end = t.find("  function chatItems(", old_js_start)
if old_js_start > 0 and old_js_end > old_js_start:
    new_js = '''  const EMO_ORDER = ["开心", "害羞", "烦", "怒", "丧", "累", "害怕"];
  const EMO_COLORS = {开心:"#ffd166", 害羞:"#ff8fb1", 烦:"#f4a261", 怒:"#ef476f",
                      丧:"#6c7a9c", 累:"#4dabf7", 害怕:"#b08968"};

  // 内分泌五条线的颜色（和情绪图例区分）
  const HORMONE_META = [
    ["DA",   "多巴胺", "#ffd166"],
    ["CORT", "皮质醇", "#ef476f"],
    ["SER",  "血清素", "#4dd0a0"],
    ["OXY",  "催产素", "#ff8fb1"],
    ["MEL",  "褪黑素", "#6c7a9c"],
  ];

  let LAST_EMOTION = {};

  function drawHormoneChart(history, latestHormones){
    const cv = $("emoChart");
    if(!cv) return;
    const dpr = window.devicePixelRatio || 1;
    const w = cv.clientWidth || 560, h = 200;
    cv.width = w * dpr; cv.height = h * dpr;
    const ctx = cv.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    const padL = 34, padR = 10, padT = 12, padB = 20;
    const iw = Math.max(10, w - padL - padR), ih = Math.max(10, h - padT - padB);

    ctx.strokeStyle = "rgba(90,120,170,.18)"; ctx.lineWidth = 1;
    ctx.fillStyle = "rgba(150,180,210,.55)"; ctx.font = "10px system-ui";
    for(let i = 0; i <= 4; i++){
      const y = padT + ih * i / 4;
      ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL + iw, y); ctx.stroke();
      ctx.fillText(String(100 - i * 25), 6, y + 3);
    }

    const pts = (history || []).filter(p => p && p.h);
    if(!pts.length){
      ctx.fillStyle = "rgba(160,190,220,.6)"; ctx.font = "12px system-ui";
      ctx.fillText("正在积累内分泌数据…", padL + 8, padT + ih / 2);
      return;
    }

    const n = pts.length;
    const xAt = i => padL + (n <= 1 ? iw / 2 : iw * i / (n - 1));
    ctx.fillStyle = "rgba(150,180,210,.5)"; ctx.font = "10px system-ui";
    const tick = Math.max(1, Math.floor(n / 5));
    for(let i = 0; i < n; i += tick){
      ctx.fillText(pts[i].ts || "", xAt(i) - 12, padT + ih + 14);
    }

    // 基线参考（50）虚线
    ctx.setLineDash([4, 4]);
    ctx.strokeStyle = "rgba(120,150,190,.25)";
    ctx.beginPath();
    const y50 = padT + ih * 0.5;
    ctx.moveTo(padL, y50); ctx.lineTo(padL + iw, y50); ctx.stroke();
    ctx.setLineDash([]);

    HORMONE_META.forEach(([key, _label, color]) => {
      ctx.beginPath();
      ctx.strokeStyle = color;
      ctx.lineWidth = key === "DA" || key === "CORT" ? 2 : 1.5;
      pts.forEach((p, i) => {
        const v = Math.max(0, Math.min(100, Number((p.h || {})[key] || 0)));
        const x = xAt(i), y = padT + ih * (1 - v / 100);
        i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
      });
      ctx.stroke();
      // 末端点 + 数值
      const lastP = pts[n - 1];
      const lv = Math.max(0, Math.min(100, Number((lastP.h || {})[key] || 0)));
      const lx = xAt(n - 1), ly = padT + ih * (1 - lv / 100);
      ctx.fillStyle = color;
      ctx.beginPath(); ctx.arc(lx, ly, 2.6, 0, Math.PI * 2); ctx.fill();
      ctx.font = "10px system-ui";
      ctx.fillText(key + " " + Math.round(lv), Math.min(lx + 4, w - 44), ly - 4);
    });
  }

  function renderEmotion(e){
    e = e || {};
    LAST_EMOTION = e;
    const vals = e.values || {};
    const order = EMO_ORDER, colors = EMO_COLORS;

    // 顶部：现在情绪（主导）+ 百分比
    let top = order[0], topVal = 0;
    order.forEach(k => { const v = Number(vals[k] || 0); if(v > topVal){ topVal = v; top = k; } });
    $("emoTop").textContent = top + " " + topVal.toFixed(0) + "%";

    // 顶部右侧：当前 5 个激素数值
    const hs = e.hormones || {};
    $("emoHormones").textContent = HORMONE_META
      .filter(([k]) => hs[k] !== undefined)
      .map(([k, _l]) => k + Math.round(hs[k]))
      .join("  ");

    // 图例改成内分泌
    $("emoLegend").innerHTML = HORMONE_META.map(([k, label, color]) => {
      const v = hs[k] !== undefined ? Math.round(hs[k]) : "—";
      return '<span><i style="background:' + color + '"></i>' + label + " " + v + "</span>";
    }).join("");

    drawHormoneChart(e.history || [], hs);
  }

'''
    t = t[:old_js_start] + new_js + t[old_js_end:]
    # 顶部标签文案：现在情绪
    t = t.replace('<span class="emo-now-label">现在情绪</span>', '<span class="emo-now-label">现在情绪</span>')
    h.write_text(t, encoding="utf-8")
    print("面板：曲线改为 5 条激素走势")
