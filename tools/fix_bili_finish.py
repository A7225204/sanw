import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\plugins\ai_group.py")
s = p.read_text(encoding="utf-8")

start = s.find("    # 👇 1.85 B站点看")
end = s.find("    # 👇 1.9 看表", start)
if start < 0 or end < 0:
    raise SystemExit("锚点未找到")

new = '''    # 👇 1.85 B站点看：/B站 看视频 [链接|名字] 👇
    # 注意：进到这里斜杠已经被剥掉了（user_msg 是 "B站 看视频 xxx"），不能只判 "/B站"
    # 只认「看视频」这种明确形式，别把 /B站热搜、/B站登录 这些抢过来
    _bili_m = re.match(r"^/?B站\\s*(?:看视频|看|视频)?\\s*(.*)$", user_msg or "")
    _bili_rest = (_bili_m.group(1).strip() if _bili_m else "")
    _bili_is_watch = (
        bool(_bili_m) and (
            re.match(r"^/?B站\\s*(?:看视频|看|视频)", user_msg or "") is not None
            or _bili_rest == ""
            or _bili_rest.startswith(("http", "BV", "bv"))
        )
    )
    if _bili_is_watch:
        if str(user_id) != YOUR_QQ_ID:
            await private_ai.finish("(*护住屏幕*) B站只有主人能点。")
            return
        from plugins import bilibili_daily as _bd
        await private_ai.send(f"(*搬小板凳*) 我去看{'你给的这条' if _bili_rest else '今天的热门'}，大概一两分钟…")
        _bili_reply = ""
        try:
            _url = await asyncio.to_thread(_bd.resolve_target, _bili_rest)
            if not _url:
                _bili_reply = "(*挠头*) 没找到能看的视频，热门列表也空了。"
            else:
                _r = await _bd.watch(_url, by="owner")
                _bili_reply = (f"🎬 {_r.get('title') or '这条'}\\n\\n{_r.get('summary')}\\n\\n"
                               f"（主人点看，不占今日 {_r.get('limit')} 条额度）")
        except Exception as e:  # noqa: BLE001
            # 注意：这里不能碰 finish，否则会被 finish 抛的 FinishedException 再抓一次
            _bili_reply = f"(*耷拉尾巴*) 没看完：{type(e).__name__}: {str(e)[:80]}"
        await private_ai.finish(_bili_reply or "(*歪头*) 看完了但没啥好说的…")
        return

'''
s = s[:start] + new + s[end:]
p.write_text(s, encoding="utf-8")
print("ai_group: /B站 分支改成 finish 只调一次")
