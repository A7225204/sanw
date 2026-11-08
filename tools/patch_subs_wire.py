import pathlib

# ---------- 1) media_fetch：拉字幕并传进分镜 ----------
p = pathlib.Path(r"D:\qq-ai-bot\plugins\media_fetch.py")
s = p.read_text(encoding="utf-8")
old = '''            sheets = await asyncio.to_thread(
                sb.build_sheets, str(video), 300.0, 9, str(tmp), 12
            )'''
new = '''            # B站自带字幕（带时间码、免费），拿不到就只出图
            subs: list[dict] = []
            try:
                subs = await asyncio.to_thread(bili_dl.fetch_subtitles, url)
                out["subtitle_lines"] = len(subs)
            except Exception as e_sub:  # noqa: BLE001
                logger.warning(f"[媒体分析] 拉字幕失败：{type(e_sub).__name__}: {e_sub}")
            sheets = await asyncio.to_thread(
                sb.build_sheets, str(video), 300.0, 9, str(tmp), 12, subs
            )'''
if old in s:
    s = s.replace(old, new, 1)
    p.write_text(s, encoding="utf-8")
    print("media_fetch: 已接字幕")
else:
    print("media_fetch 锚点未匹配")

# ---------- 2) media_parse：分镜里同样带字幕 ----------
q = pathlib.Path(r"D:\qq-ai-bot\plugins\media_parse.py")
t = q.read_text(encoding="utf-8")
t = t.replace(
    '''async def _understand_storyboard(video: Path, info: dict) -> str:''',
    '''async def _understand_storyboard(video: Path, info: dict, url: str = "") -> str:''')
t = t.replace(
    '''        sheets = await asyncio.to_thread(sb.build_sheets, str(video), 300.0, 9, None, 4)''',
    '''        subs: list[dict] = []
        try:
            from plugins import bili_dl
            if bili_dl.is_bili(url):
                subs = await asyncio.to_thread(bili_dl.fetch_subtitles, url)
        except Exception:  # noqa: BLE001
            subs = []
        sheets = await asyncio.to_thread(sb.build_sheets, str(video), 300.0, 9, None, 4, subs)''')
t = t.replace('''        summary = await _understand_storyboard(video, info)''',
              '''        summary = await _understand_storyboard(video, info, url)''')
q.write_text(t, encoding="utf-8")
print("media_parse: 已接字幕（传 url）")
