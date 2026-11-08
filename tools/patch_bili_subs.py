import pathlib

# ---------- 1) bili_dl：拉 B站 CC/AI 字幕 ----------
p = pathlib.Path(r"D:\qq-ai-bot\plugins\bili_dl.py")
s = p.read_text(encoding="utf-8")
if "def fetch_subtitles" not in s:
    add = '''

def fetch_subtitles(url: str) -> list[dict]:
    """拉 B 站自带字幕（CC / AI 字幕），返回 [{"from":秒,"to":秒,"text":...}]。

    字幕是平台给的，比自己跑 ASR 准，而且带时间码、不花钱。
    拿不到就返回空列表，调用方照常出图（只是没有台词）。
    """
    import re as _re
    try:
        import httpx
    except Exception:  # noqa: BLE001
        return []
    m = _re.search(r"(BV[0-9A-Za-z]{10})", str(url or ""))
    if not m:
        return []
    bvid = m.group(1)
    jar = _cookie_dict()
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"}
    try:
        with httpx.Client(timeout=20, headers=headers, cookies=jar or None) as cli:
            v = cli.get("https://api.bilibili.com/x/web-interface/view", params={"bvid": bvid}).json()
            data = v.get("data") or {}
            cid = data.get("cid") or ((data.get("pages") or [{}])[0].get("cid"))
            if not cid:
                return []
            p2 = cli.get("https://api.bilibili.com/x/player/v2",
                         params={"bvid": bvid, "cid": cid}).json()
            subs = (((p2.get("data") or {}).get("subtitle") or {}).get("subtitles") or [])
            if not subs:
                return []
            # 优先中文字幕
            subs.sort(key=lambda x: 0 if "zh" in str(x.get("lan")) else 1)
            su = str(subs[0].get("subtitle_url") or "")
            if not su:
                return []
            if su.startswith("//"):
                su = "https:" + su
            j = cli.get(su).json()
            out = []
            for it in (j.get("body") or []):
                txt = str(it.get("content") or "").strip()
                if not txt:
                    continue
                out.append({"from": float(it.get("from") or 0),
                            "to": float(it.get("to") or 0),
                            "text": txt})
            logger.info(f"[B站字幕] {bvid} 拿到 {len(out)} 条（{subs[0].get('lan')}）")
            return out
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[B站字幕] 拉取失败：{type(e).__name__}: {e}")
        return []
'''
    s = s.rstrip() + add
    p.write_text(s, encoding="utf-8")
    print("bili_dl: 已加 fetch_subtitles")

# ---------- 2) storyboard：格子里带上台词 ----------
q = pathlib.Path(r"D:\qq-ai-bot\plugins\storyboard.py")
t = q.read_text(encoding="utf-8")

t = t.replace(
    '''def build_sheet(path: str, start: float, end: float, cells: int = 9,
                out_path: str | None = None,
                cuts: list[float] | None = None) -> tuple[str, list[dict], list[str]]:''',
    '''def _quotes(subs: list[dict] | None, a: float, b: float, limit: int = 110) -> str:
    """把字幕里落在 [a,b] 的台词拼起来（按时间顺序，超长截断）。"""
    if not subs:
        return ""
    picked = [s for s in subs if float(s.get("to") or 0) > a and float(s.get("from") or 0) < b]
    txt = " ".join(str(s.get("text") or "") for s in picked).strip()
    return txt[:limit]


def build_sheet(path: str, start: float, end: float, cells: int = 9,
                out_path: str | None = None,
                cuts: list[float] | None = None,
                subs: list[dict] | None = None) -> tuple[str, list[dict], list[str]]:''')

t = t.replace(
    '''        meta.append({"index": i + 1, "start": a, "end": b,
                     "frame_at": round(mid, 2), "cuts": n_cuts})
        lines.append(f"cell {i + 1} = {_fmt(a)}-{_fmt(b)}  cuts {n_cuts}")''',
    '''        quote = _quotes(subs, a, b)
        meta.append({"index": i + 1, "start": a, "end": b,
                     "frame_at": round(mid, 2), "cuts": n_cuts, "quote": quote})
        lines.append(f"cell {i + 1} = {_fmt(a)}-{_fmt(b)}  cuts {n_cuts}")
        if quote:
            lines.append(f'    "{quote}"')''')

t = t.replace(
    '''def build_sheets(path: str, window: float = 300.0, cells: int = 9,
                 out_dir: str | None = None, max_sheets: int = 40) -> list[dict]:''',
    '''def build_sheets(path: str, window: float = 300.0, cells: int = 9,
                 out_dir: str | None = None, max_sheets: int = 40,
                 subs: list[dict] | None = None) -> list[dict]:''')

t = t.replace(
    '''        png, meta, lines = build_sheet(path, 0.0, dur, cells,
                                       out_path=os.path.join(out_dir, "sheet_0.png") if out_dir else None)''',
    '''        png, meta, lines = build_sheet(path, 0.0, dur, cells,
                                       out_path=os.path.join(out_dir, "sheet_0.png") if out_dir else None,
                                       subs=subs)''')

t = t.replace(
    '''            png, meta, lines = build_sheet(path, t, e, cells,
                                           out_path=os.path.join(out_dir, f"sheet_{int(t)}.png")
                                           if out_dir else None,
                                           cuts=seg_cuts)''',
    '''            png, meta, lines = build_sheet(path, t, e, cells,
                                           out_path=os.path.join(out_dir, f"sheet_{int(t)}.png")
                                           if out_dir else None,
                                           cuts=seg_cuts, subs=subs)''')

q.write_text(t, encoding="utf-8")
print("storyboard: 格子带台词")
