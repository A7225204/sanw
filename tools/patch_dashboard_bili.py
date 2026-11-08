import pathlib

# ---------- 1) 后端：加 bili_daily 聚合 ----------
p = pathlib.Path(r"D:\qq-ai-bot\dashboard\server.py")
s = p.read_text(encoding="utf-8")
if "def aggregate_bili_daily(" not in s:
    helper = '''

def aggregate_bili_daily() -> dict[str, Any]:
    """B站每日刷视频：今日进度 + 最近看过的标题（网页端同步显示）。"""
    data = _read_json(ROOT / "memory" / "bili_daily.json")
    items = (data.get("history") or [])[-20:]
    out_items = []
    for it in reversed(items):
        out_items.append({
            "title": str(it.get("title") or ""),
            "bvid": str(it.get("bvid") or ""),
            "at": str(it.get("at") or ""),
            "by": str(it.get("by") or ""),
            "summary": str(it.get("summary") or "")[:120],
        })
    try:
        limit = int(float((ROOT / ".env").read_text(encoding="utf-8", errors="ignore")
                          .split("BILI_DAILY_LIMIT=")[1].splitlines()[0].strip() or 10))
    except Exception:  # noqa: BLE001
        limit = 10
    count = int(data.get("count") or 0) if str(data.get("date") or "") == time.strftime("%Y-%m-%d") else 0
    return {"date": str(data.get("date") or ""), "count": count,
            "limit": limit, "left": max(0, limit - count), "items": out_items}

'''
    i = s.find("def aggregate_emotion(")
    s = s[:i] + helper.lstrip("\n") + "\n" + s[i:]
    s = s.replace('"emotion": aggregate_emotion(),',
                  '"emotion": aggregate_emotion(),\n        "bili_daily": aggregate_bili_daily(),', 1)
    p.write_text(s, encoding="utf-8")
    print("dashboard: 加 bili_daily 接口字段")

# ---------- 2) 前端：加一张卡片 + 渲染 ----------
h = pathlib.Path(r"D:\qq-ai-bot\dashboard\index.html")
t = h.read_text(encoding="utf-8")

if 'id="biliDailyCard"' not in t:
    # 卡片插在「令牌用量」之前
    anchor = '''  <!-- 令牌用量 -->'''
    card = '''  <!-- B站刷视频 -->
  <section class="card" id="biliDailyCard">
    <h2><span class="ico"><svg viewBox="0 0 24 24"><path d="M4 5h16v12H4z"/><path d="M9 21l3-3 3 3"/></svg></span> B站刷视频 <span class="mc" id="biliDailyCount">—</span></h2>
    <div class="mini" id="biliDailyMeta">—</div>
    <ul class="recent" id="biliDailyList" style="margin-top:8px"></ul>
  </section>

'''
    if anchor in t:
        t = t.replace(anchor, card + anchor, 1)

    # 渲染函数插在 renderEmotion 前
    js = '''  function renderBiliDaily(b){
    b = b || {};
    const cnt = Number(b.count || 0), lim = Number(b.limit || 10);
    $("biliDailyCount").textContent = cnt + " / " + lim;
    const left = Number(b.left != null ? b.left : (lim - cnt));
    $("biliDailyMeta").textContent = left > 0
      ? ("今天还差 " + left + " 条 · 随机时间看")
      : "今天看满啦";
    const items = b.items || [];
    if(!items.length){ $("biliDailyList").innerHTML = "<li>还没看过视频</li>"; return; }
    $("biliDailyList").innerHTML = items.map(function(it){
      const t = (it.at || "").slice(11, 16);
      const href = it.bvid ? ("https://www.bilibili.com/video/" + it.bvid) : "";
      const name = esc(it.title || it.bvid || "（无标题）");
      const who = it.by === "owner" ? "主人点看" : "随机刷";
      const link = href ? '<a href="' + href + '" target="_blank" style="color:#7fd4ff">' + name + '</a>' : name;
      return "<li><span class='t'>" + esc(t) + "</span> " + link +
             " <span style='color:#5f7e96'>· " + who + "</span></li>";
    }).join("");
  }

'''
    t = t.replace("  function renderEmotion(e){", js + "  function renderEmotion(e){", 1)
    # 刷新时调用
    t = t.replace("    renderEmotion(s.emotion || {});",
                  "    renderEmotion(s.emotion || {});\n    renderBiliDaily(s.bili_daily || {});", 1)
    h.write_text(t, encoding="utf-8")
    print("dashboard: 加 B站刷视频 卡片")
