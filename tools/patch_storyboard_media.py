import pathlib

# ---------- 1) media_fetch：B站改成「分镜宫格 + 文本清单」 ----------
p = pathlib.Path(r"D:\qq-ai-bot\plugins\media_fetch.py")
s = p.read_text(encoding="utf-8")

old = '''        frames = await asyncio.to_thread(df._frames, video, 3)
        if frames:
            try:
                from plugins.ai_group import _describe_images
                descs = await _describe_images([f"data:image/jpeg;base64,{df._b64(f)}" for f in frames])
                out["desc"] = " ｜ ".join(d for d in (descs or []) if d)[:200]
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[媒体分析] B站抽帧识图失败：{type(e).__name__}: {e}")'''
new = '''        # 分镜：把整段视频压成几张宫格图（每张 9 格 + 时间码），比抽 3 帧信息多、比逐帧便宜
        desc_parts: list[str] = []
        try:
            from plugins import storyboard as sb
            info_sb = await asyncio.to_thread(sb.probe, str(video))
            dur = float(info_sb.get("duration") or 0)
            out["duration"] = dur
            out["cuts"] = 0
            sheets = await asyncio.to_thread(
                sb.build_sheets, str(video), 300.0, 9, str(tmp), 12
            )
            out["sheets"] = len(sheets)
            if sheets:
                from plugins.ai_group import _describe_images
                import base64 as _b64mod
                budget = min(len(sheets), 12 if dur > 600 else 4)
                for sh in sheets[:budget]:
                    try:
                        raw = Path(sh["png"]).read_bytes()
                        uri = "data:image/png;base64," + _b64mod.b64encode(raw).decode()
                        ds = await _describe_images(
                            [uri],
                            hint=(f"这是一段视频的分镜宫格（每格左上角有格号和时间码），"
                                  f"覆盖 {sh['start']:.0f}~{sh['end']:.0f} 秒。"
                                  "用 60 字概括这段在讲什么/画面在干什么，不要逐格罗列。"),
                        )
                        txt = " ".join(d for d in (ds or []) if d).strip()
                        if txt:
                            desc_parts.append(f"[{sh['start']:.0f}-{sh['end']:.0f}s] {txt}")
                    except Exception as e2:  # noqa: BLE001
                        logger.warning(f"[媒体分析] 分镜识图失败：{type(e2).__name__}: {e2}")
            out["storyboard"] = await asyncio.to_thread(sb.sheet_text, sheets, dur) if sheets else ""
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[媒体分析] 分镜失败，回退抽帧：{type(e).__name__}: {e}")
            frames = await asyncio.to_thread(df._frames, video, 3)
            if frames:
                try:
                    from plugins.ai_group import _describe_images
                    descs = await _describe_images([f"data:image/jpeg;base64,{df._b64(f)}" for f in frames])
                    desc_parts = [d for d in (descs or []) if d]
                except Exception as e3:  # noqa: BLE001
                    logger.warning(f"[媒体分析] 回退抽帧识图也失败：{type(e3).__name__}: {e3}")
        if desc_parts:
            out["desc"] = " ｜ ".join(desc_parts)[:900]'''
if old in s:
    s = s.replace(old, new, 1)
else:
    raise SystemExit("media_fetch 锚点未匹配")
p.write_text(s, encoding="utf-8")
print("media_fetch: B站改分镜")

# ---------- 2) _describe_images 支持 hint 参数（可选，不传就走原逻辑） ----------
g = pathlib.Path(r"D:\qq-ai-bot\plugins\ai_group.py")
t = g.read_text(encoding="utf-8")
if "hint: str = \"\"" not in t:
    import re
    m = re.search(r"async def _describe_images\(([^)]*)\)", t)
    if m:
        old_sig = m.group(0)
        new_sig = old_sig.replace(")", ', hint: str = "")') if old_sig.rstrip().endswith(")") else old_sig
        # 更稳：直接替换第一个 )
        new_sig = old_sig[:-1] + (", " if m.group(1).strip() else "") + 'hint: str = "")'
        t = t.replace(old_sig, new_sig, 1)
        g.write_text(t, encoding="utf-8")
        print("ai_group: _describe_images 支持 hint")
    else:
        print("未找到 _describe_images 签名")
