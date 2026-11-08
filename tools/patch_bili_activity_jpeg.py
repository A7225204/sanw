import pathlib

# ---------- 1) 看片时把活动状态写出来（网页端就不显示发呆了） ----------
p = pathlib.Path(r"D:\qq-ai-bot\plugins\bilibili_daily.py")
s = p.read_text(encoding="utf-8")
old = '''async def watch(url: str, by: str = "scheduler") -> dict:
    """看一条：下载+分镜+识图（media_fetch 负责），再记进学习档案。"""
    from plugins import media_fetch'''
new = '''async def watch(url: str, by: str = "scheduler") -> dict:
    """看一条：下载+分镜+识图（media_fetch 负责），再记进学习档案。"""
    from plugins import media_fetch
    try:
        from plugins.activity import set_activity
        set_activity("看B站视频中", 1800)
    except Exception:  # noqa: BLE001
        pass'''
if old in s:
    s = s.replace(old, new, 1)
    s = s.replace('''    d["history"].append({"bvid": bvid, "title": title, "summary": summary,
                         "at": time.strftime("%Y-%m-%d %H:%M:%S"), "by": by})''',
'''    d["history"].append({"bvid": bvid, "title": title, "summary": summary,
                         "at": time.strftime("%Y-%m-%d %H:%M:%S"), "by": by})
    try:
        from plugins.activity import set_activity
        set_activity("刚看完B站视频", 180)
    except Exception:  # noqa: BLE001
        pass''')
    p.write_text(s, encoding="utf-8")
    print("bilibili_daily: 看片期间写活动状态")

# ---------- 2) 宫格图先压成 JPEG 再送识图（火山那边 1200px PNG 要 70 秒） ----------
m = pathlib.Path(r"D:\qq-ai-bot\plugins\media_fetch.py")
t = m.read_text(encoding="utf-8")
old2 = '''                    try:
                        raw = Path(sh["png"]).read_bytes()
                        uri = "data:image/png;base64," + _b64mod.b64encode(raw).decode()'''
new2 = '''                    try:
                        # 压成 JPEG 再送：PNG 1200px 火山要 70 秒，JPEG 只要 20 秒左右
                        raw = Path(sh["png"]).read_bytes()
                        try:
                            from PIL import Image as _Img
                            import io as _io
                            _im = _Img.open(_io.BytesIO(raw)).convert("RGB")
                            if _im.width > 1280:
                                _im = _im.resize((1280, int(_im.height * 1280 / _im.width)), _Img.LANCZOS)
                            _buf = _io.BytesIO()
                            _im.save(_buf, format="JPEG", quality=82, optimize=True)
                            raw = _buf.getvalue()
                            _mime = "image/jpeg"
                        except Exception:  # noqa: BLE001
                            _mime = "image/png"
                        uri = f"data:{_mime};base64," + _b64mod.b64encode(raw).decode()'''
if old2 in t:
    t = t.replace(old2, new2, 1)
    m.write_text(t, encoding="utf-8")
    print("media_fetch: 宫格先压 JPEG")
