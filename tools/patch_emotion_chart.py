import pathlib
import re

p = pathlib.Path(r"D:\qq-ai-bot\dashboard\index.html")
s = p.read_text(encoding="utf-8")
changed = []

# ---------- 1) 结构：圆盘 → 长方形趋势图 ----------
old_block = '''    <div class="emotion-wheel">
      <div class="wheel" id="emotionWheel">
        <div class="wheel-hole"><b id="emoTop">—</b><span>主导情绪</span></div>
      </div>
      <div class="emo-legend" id="emoLegend"></div>
    </div>'''
new_block = '''    <div class="emotion-panel">
      <div class="emo-now">
        <div class="emo-now-main"><b id="emoTop">—</b><span class="emo-now-label">现在情绪</span></div>
        <div class="emo-hormones" id="emoHormones">—</div>
      </div>
      <canvas id="emoChart" height="200"></canvas>
      <div class="emo-legend" id="emoLegend"></div>
    </div>'''
if old_block in s:
    s = s.replace(old_block, new_block, 1)
    changed.append("结构")
else:
    print("情绪轮结构没匹配")

# ---------- 2) CSS ----------
css = '''
  /* ===== 情绪趋势面板（长方形） ===== */
  .emotion-panel{display:flex;flex-direction:column;gap:8px;min-width:0}
  .emo-now{display:flex;align-items:baseline;justify-content:space-between;gap:10px;
    padding:8px 10px;border-radius:10px;background:rgba(20,28,52,.7);
    border:1px solid rgba(70,110,170,.3)}
  .emo-now-main{display:flex;align-items:baseline;gap:8px}
  .emo-now-main b{font-size:22px;color:#ffd166;letter-spacing:1px}
  .emo-now-label{font-size:11px;color:var(--dim);letter-spacing:2px}
  .emo-hormones{font-size:11px;color:#7fb8d8;letter-spacing:.5px;white-space:nowrap}
  #emoChart{width:100%;height:200px;display:block;border-radius:10px;
    background:rgba(12,18,36,.55);border:1px solid rgba(60,90,140,.25)}
  .emo-legend{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:11px;color:var(--dim)}
  .emo-legend i{width:8px;height:8px;border-radius:2px;display:inline-block;margin-right:4px}
'''
if ".emotion-panel{" not in s:
    s = s.replace("</style>", css + "</style>", 1)
    changed.append("css")

# ---------- 3) JS：renderEmotion 改为画折线 ----------
old_js = s[s.find("  function renderEmotion(e){"):]
old_js = old_js[:old_js.find("\n  function chatItems(")]

new_js = '''  const EMO_ORDER = ["开心", "害羞", "烦", "怒", "丧", "累", "害怕"];
  const EMO_COLORS = {开心:"#ffd166", 害羞:"#ff8fb1", 烦:"#f4a261", 怒:"#ef476f",
                      丧:"#6c7a9c", 累:"#4dabf7", 害怕:"#b08968"};

  function drawEmoChart(history, latest){
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

    // 网格 + 纵轴刻度
    ctx.strokeStyle = "rgba(90,120,170,.18)"; ctx.lineWidth = 1;
    ctx.fillStyle = "rgba(150,180,210,.55)"; ctx.font = "10px system-ui";
    for(let i = 0; i <= 4; i++){
      const y = padT + ih * i / 4;
      ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL + iw, y); ctx.stroke();
      ctx.fillText(String(100 - i * 25), 6, y + 3);
    }

    const pts = (history || []).filter(p => p && p.e);
    if(!pts.length){
      ctx.fillStyle = "rgba(160,190,220,.6)"; ctx.font = "12px system-ui";
      ctx.fillText("正在积累情绪数据…", padL + 8, padT + ih / 2);
      return;
    }

    // 横轴时间标签（最多 5 个）
    const n = pts.length;
    const xAt = i => padL + (n <= 1 ? iw / 2 : iw * i / (n - 1));
    ctx.fillStyle = "rgba(150,180,210,.5)"; ctx.font = "10px system-ui";
    const tick = Math.max(1, Math.floor(n / 5));
    for(let i = 0; i < n; i += tick){
      const y = padT + ih + 14;
      ctx.fillText(pts[i].ts || "", xAt(i) - 12, y);
    }

    // 七条曲线
    EMO_ORDER.forEach(k => {
      ctx.beginPath();
      ctx.strokeStyle = EMO_COLORS[k];
      ctx.lineWidth = k === "开心" || k === "丧" ? 2 : 1.4;
      ctx.globalAlpha = 0.9;
      pts.forEach((p, i) => {
        const v = Math.max(0, Math.min(100, Number(p.e[k] || 0)));
        const x = xAt(i), y = padT + ih * (1 - v / 100);
        i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
      });
      ctx.stroke();
      ctx.globalAlpha = 1;
    });
  }

  function renderEmotion(e){
    e = e || {};
    const vals = e.values || {};
    const order = EMO_ORDER, colors = EMO_COLORS;
    let top = order[0], topVal = 0;
    order.forEach(k => { const v = Number(vals[k] || 0); if(v > topVal){ topVal = v; top = k; } });
    $("emoTop").textContent = top + " " + topVal.toFixed(0) + "%";

    const hs = e.hormones || {};
    const hkTxt = ["DA","CORT","SER","OXY","MEL"]
      .filter(k => hs[k] !== undefined)
      .map(k => k + Math.round(hs[k])).join("  ");
    $("emoHormones").textContent = hkTxt ? ("内分泌 " + hkTxt) : "";

    $("emoLegend").innerHTML = order.map(k =>
      '<span><i style="background:' + colors[k] + '"></i>' + k + " " +
      Number(vals[k] || 0).toFixed(0) + "%</span>").join("");

    drawEmoChart(e.history || [], vals);
  }
'''

if old_js and "drawEmoChart" not in s:
    s = s.replace(old_js, new_js, 1)
    changed.append("js")

p.write_text(s, encoding="utf-8")
print("改动:", changed)
