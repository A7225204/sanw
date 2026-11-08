import pathlib
import re

# ---------- 1) feishu_bridge：加一个镜像开关 ----------
fb = pathlib.Path(r"D:\qq-ai-bot\plugins\feishu_bridge.py")
s = fb.read_text(encoding="utf-8")
if "def mirror_enabled(" not in s:
    anchor = "def available("
    helper = '''def mirror_enabled() -> bool:
    """QQ 侧的产出要不要镜像到飞书。

    主人的要求：两边不串。QQ 里说的话/画的图只在 QQ，飞书聊的只在飞书。
    默认关；想看镜像再设 FEISHU_MIRROR=1。
    """
    import os as _os
    return str(_os.getenv("FEISHU_MIRROR", "0")).strip().lower() in ("1", "true", "yes", "on")


'''
    i = s.find(anchor)
    s = s[:i] + helper + s[i:]
    fb.write_text(s, encoding="utf-8")
    print("feishu_bridge: 加 mirror_enabled")

# ---------- 2) ai_group：三处 QQ→飞书 图片镜像加开关 ----------
ag = pathlib.Path(r"D:\qq-ai-bot\plugins\ai_group.py")
t = ag.read_text(encoding="utf-8")
count = 0
patterns = [
    ('''                try:
                    from plugins import feishu_bridge
                    await asyncio.to_thread(feishu_bridge.send_image_bytes, _out, "")
                except Exception:  # noqa: BLE001
                    pass''',
     '''                try:
                    from plugins import feishu_bridge
                    if feishu_bridge.mirror_enabled():
                        await asyncio.to_thread(feishu_bridge.send_image_bytes, _out, "")
                except Exception:  # noqa: BLE001
                    pass'''),
    ('''            try:
                from plugins import feishu_bridge
                await asyncio.to_thread(feishu_bridge.send_image_bytes, _img, "")
            except Exception:  # noqa: BLE001
                pass''',
     '''            try:
                from plugins import feishu_bridge
                if feishu_bridge.mirror_enabled():
                    await asyncio.to_thread(feishu_bridge.send_image_bytes, _img, "")
            except Exception:  # noqa: BLE001
                pass'''),
    ('''                try:
                    from plugins import feishu_bridge
                    await asyncio.to_thread(feishu_bridge.send_image_bytes, _gimg, "")
                except Exception:  # noqa: BLE001
                    pass''',
     '''                try:
                    from plugins import feishu_bridge
                    if feishu_bridge.mirror_enabled():
                        await asyncio.to_thread(feishu_bridge.send_image_bytes, _gimg, "")
                except Exception:  # noqa: BLE001
                    pass'''),
]
for old, new in patterns:
    if old in t:
        t = t.replace(old, new, 1)
        count += 1
ag.write_text(t, encoding="utf-8")
print(f"ai_group: 图片镜像加开关（{count} 处）")

# ---------- 3) game_watch：陪游戏截图只在 QQ ----------
gw = pathlib.Path(r"D:\qq-ai-bot\plugins\game_watch.py")
u = gw.read_text(encoding="utf-8")
old_gw = '''    try:
        from plugins import feishu_bridge
        await asyncio.to_thread(feishu_bridge.notify_owner, text)
    except Exception:  # noqa: BLE001
        pass'''
new_gw = '''    try:
        from plugins import feishu_bridge
        if feishu_bridge.mirror_enabled():
            await asyncio.to_thread(feishu_bridge.notify_owner, text)
    except Exception:  # noqa: BLE001
        pass'''
if old_gw in u:
    u = u.replace(old_gw, new_gw, 1)
    gw.write_text(u, encoding="utf-8")
    print("game_watch: 飞书镜像加开关")

# ---------- 4) outbox：默认不镜像 ----------
ob = pathlib.Path(r"D:\qq-ai-bot\plugins\outbox.py")
v = ob.read_text(encoding="utf-8")
v = v.replace('FEISHU_MIRROR = os.getenv("OUTBOX_FEISHU", "1").strip() not in {"0", "false", "False", ""}',
              'FEISHU_MIRROR = os.getenv("OUTBOX_FEISHU", "0").strip() not in {"0", "false", "False", ""}')
ob.write_text(v, encoding="utf-8")
print("outbox: 默认不镜像到飞书")

# ---------- 5) .env 里也写上，避免下次被默认值带回去 ----------
env = pathlib.Path(r"D:\qq-ai-bot\.env")
e = env.read_text(encoding="utf-8", errors="ignore")
changed = []
for k, val in (("FEISHU_MIRROR", "0"), ("OUTBOX_FEISHU", "0")):
    if re.search(rf"^{k}=", e, re.M):
        e = re.sub(rf"^{k}=.*$", f"{k}={val}", e, count=1, flags=re.M)
    else:
        e += f"\n{k}={val}\n"
    changed.append(k)
env.write_text(e, encoding="utf-8")
print("env:", changed)
