import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\plugins\ai_group.py")
s = p.read_text(encoding="utf-8")

old = '''def pick_emoji(mood: str, text: str = "") -> Path | None:
    emoji = cfg.emoji
    if not emoji.dir.is_dir(): return None'''
new = '''def pick_emoji(mood: str, text: str = "") -> Path | None:
    emoji = cfg.emoji
    if not emoji.dir.is_dir(): return None
    # 先走语义检索（跨情绪分类统一打分）；没命中再走原来的情绪+2-gram 兜底
    if text:
        try:
            from plugins import sticker_vec
            hit = sticker_vec.best(text)
            if hit:
                _p, _sim = hit
                logger.debug(f"[表情] 语义命中 {Path(_p).name} sim={_sim:.2f}")
                return Path(_p)
        except Exception:  # noqa: BLE001
            pass'''
if old in s and "sticker_vec" not in s:
    s = s.replace(old, new, 1)
    p.write_text(s, encoding="utf-8")
    print("pick_emoji 已接入语义检索")
else:
    print("未匹配或已接入")
