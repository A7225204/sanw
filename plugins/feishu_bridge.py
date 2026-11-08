"""飞书桥接：QQ 掉线/整机重启时，用飞书私聊给她下指令，她也能回话。

为什么用飞书而不是抖音私信
-------------------------
抖音私信风控极严（频率、含 QQ 号/链接直接判引流），发多了禁私信甚至封号，
而她那个号还要发作品。飞书自建应用走官方长连接，不关联任何有封号风险的账号。

配置（.env）
------------
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
FEISHU_OWNER_OPEN_ID=ou_xxx      # 可选：只认这个人的消息（强烈建议填）
FEISHU_ENABLED=true              # 默认 true，缺 ID/SECRET 时自动不启用

飞书后台要做的事（一次性）
--------------------------
1. 开放平台 → 创建企业自建应用
2. 添加「机器人」能力
3. 权限：im:message（收发消息）、im:message:send_as_bot
4. 事件订阅 → 订阅 im.message.receive_v1，接收方式选「长连接」
5. 发布版本 → 管理员审核通过
6. 把她加为好友（或用「单聊」打开会话），把 Open ID 填进 FEISHU_OWNER_OPEN_ID
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import threading
import time
import time
from pathlib import Path

from nonebot.log import logger

def _lazy_env(name: str, default: str = "") -> str:
    """模块级常量是 import 时读的，那时 .env 可能还没加载（要等 get_config()）。

    2026-11-06：夜间巡检在独立进程里跑，APP_ID/APP_SECRET/OWNER_OPEN_ID 全是空串，
    notify_owner 一直 False。这里读不到就主动触发一次 get_config() 再读。
    """
    v = (os.getenv(name) or "").strip()
    if v:
        return v
    try:
        from config import get_config
        get_config()
    except Exception:  # noqa: BLE001
        pass
    return (os.getenv(name) or default).strip()


APP_ID = (os.getenv("FEISHU_APP_ID") or "").strip()
APP_SECRET = (os.getenv("FEISHU_APP_SECRET") or "").strip()
OWNER_OPEN_ID = (os.getenv("FEISHU_OWNER_OPEN_ID") or "").strip()
# 飞书里的你是 open_id，QQ 里是 100000000。链路必须用同一个身份，
# 否则她把你当成陌生人（外号、好感、私聊档案全都对不上）。
OWNER_UID = (os.getenv("FEISHU_OWNER_UID") or "100000000").strip()
ENABLED = (os.getenv("FEISHU_ENABLED", "true").lower() in ("1", "true", "yes"))

_MAIN_LOOP = None
_BOT_TEXT: list[str] = []       # 最近回复，用于日志/排错
_started = False


def mirror_enabled() -> bool:
    """QQ 侧的产出要不要镜像到飞书。

    主人的要求：两边不串。QQ 里说的话/画的图只在 QQ，飞书聊的只在飞书。
    默认关；想看镜像再设 FEISHU_MIRROR=1。
    """
    import os as _os
    return str(_os.getenv("FEISHU_MIRROR", "0")).strip().lower() in ("1", "true", "yes", "on")


def available() -> bool:
    """飞书能不能用。用 _lazy_env 判，别信模块级常量（import 时 .env 可能还没加载）。"""
    return bool(_lazy_env("FEISHU_ENABLED", "true").lower() in ("1", "true", "yes")
                and _lazy_env("FEISHU_APP_ID")
                and _lazy_env("FEISHU_APP_SECRET"))


def _run_async(coro, timeout: float = 90.0):
    """把协程丢回 bot 的主事件循环执行。

    飞书长连接跑在独立线程里，直接 asyncio.run() 会新建一个循环，
    而 ai_group 的共享 httpx.AsyncClient 绑在主循环上 → RuntimeError。
    """
    import asyncio as _a
    loop = _MAIN_LOOP
    if loop is None or loop.is_closed():
        try:
            loop = _a.get_event_loop()
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"主循环不可用：{e}")
    return _a.run_coroutine_threadsafe(coro, loop).result(timeout)



def _reply_full(text: str) -> tuple[str, str]:
    """命令直通 → 否则走完整 QQ 链路；返回 (回复, 情绪标记)。"""
    t = (text or "").strip()
    if not t:
        return "", ""
    cmd = _dispatch_command(t)
    if cmd:
        return cmd, ""
    try:
        from plugins import ai_group
        out = _run_async(ai_group.feishu_chat(OWNER_UID, t), 120)
        if out and out[0]:
            return out[0], (out[1] if len(out) > 1 else "")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[飞书] 完整链路不可用，回退轻量：{type(e).__name__}: {e}")
    return _small_talk(t), ""


def _reply_text(text: str) -> str:
    """生成回复：命令直通优先，其次走人设闲聊。"""
    t = (text or "").strip()
    if not t:
        return ""
    cmd = _dispatch_command(t)
    if cmd:
        return cmd
    return _small_talk(t)


def _dispatch_command(text: str) -> str:
    """飞书里能直接触发的指令（不依赖 QQ 在线）。"""
    low = text.lower()
    # 喂她：和 QQ 私聊同一套意图判断（吃饭/吃点火锅/请你吃烧烤/喂你块蛋糕…）
    try:
        from plugins import ai_group
        if ai_group._is_feed_intent(text) and ai_group._feed_command(text):   # 只认 / 指令
            b, a = ai_group.feed_now()
            return (f"(*尾巴一甩，认真吃起来*) 那我吃了！"
                    f"饿 {b:.0f}% → {a:.0f}%。主人你也记得吃啊。")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[飞书] 喂食失败：{type(e).__name__}: {e}")
    # 改图：先收到图（_PENDING_EDIT），再发「编辑图片 xxx」
    try:
        import re as _re
        m = _re.match(r"^(编辑图片|图像编辑|编辑图|改图|修图|p图|P图)", text.strip())
        if m:
            global _PENDING_EDIT
            if not _PENDING_EDIT:
                return "先把图发我，再说「编辑图片 <怎么改>」。"
            data, ts = _PENDING_EDIT
            if time.time() - ts > 1800:
                _PENDING_EDIT = None
                return "那张图有点久了，重新发一次吧。"
            instr = text.strip()[len(m.group(1)):].strip(" ：:，,")
            if not instr:
                return "想改成啥样？比如「编辑图片 换成红裙子，背景海边」。"
            from plugins import image_edit
            out = _run_async(asyncio.to_thread(image_edit.edit, data, instr), 500)
            if not out:
                return "改图没成功（可能显存不够），过会儿再试。"
            key = _upload_bytes(out)
            if key:
                _send_by_type(OWNER_OPEN_ID, "image", {"image_key": key})
                _PENDING_EDIT = None
                return "改好了。"
            return "改好了但传图失败。"
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[飞书] 改图失败：{type(e).__name__}: {e}")

    # 画图：飞书也要能真出图（否则她只能用嘴说"我发了张图"）
    try:
        from plugins import ai_group
        _mode, _prompt = "", ""
        if text.startswith("画图"):
            _mode, _prompt = "verbatim", text[2:].strip()
        else:
            _act = ai_group._self_draw_action(text)
            if _act is not None:
                _mode, _prompt = "self", _act or ""
        if _mode:
            img, src = _run_async(ai_group._draw_chain(_prompt, mode=_mode), 240)
            if img:
                image_key = _upload_bytes(img)
                if image_key:
                    _send_by_type(OWNER_OPEN_ID, "image", {"image_key": image_key})
                    logger.info(f"[飞书] 出图 来源={src} mode={_mode}")
                    return f"（画好了，来源 {src}）"
                return "图生成了，但上传飞书失败。"
            return "这次没画出来，工作流可能被占用了，过会儿再试。"
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[飞书] 出图失败：{type(e).__name__}: {e}")
    if any(k in text for k in ("状态", "在线吗", "活着吗")):
        return _status_text()
    if any(k in text for k in ("去玩手机", "玩手机", "刷抖音")):
        try:
            import asyncio
            from plugins import phone_play
            pack = _run_async(asyncio.to_thread(phone_play._play_douyin, phone_play.MANUAL_SCREENS), 150)
            items = pack.get("items") or []
            if not items:
                return f"刷是刷了，但没抓到内容：{pack.get('error') or '可能被风控了'}"
            h = pack.get("hottest") or {}
            lines = [f"刷到 {len(items)} 条，最火的是：{str(h.get('title', ''))[:40]}"
                     + (f"（{h['likes']} 赞）" if h.get("likes") else "")]
            if pack.get("tags"):
                lines.append("话题：" + " ".join("#" + x for x in pack["tags"][:5]))
            return "\n".join(lines)
        except Exception as e:  # noqa: BLE001
            return f"玩手机失败了：{type(e).__name__}: {e}"
    if "说说" in text and any(k in text for k in ("发", "写", "来一条")):
        return "发说说现在得等 QQ 侧的定时任务，飞书这边先记下了。"
    if low.startswith("/") or text.startswith("／"):
        return f"这个指令暂时只支持：状态、玩手机、说说。收到的是：{text[:20]}"
    return ""


def _status_text() -> str:
    bits = [f"飞书通道正常（{time.strftime('%m-%d %H:%M')}）"]
    try:
        import json as _j
        from pathlib import Path as _P
        st = _P(r"D:\qq-ai-bot\memory\qzone_auto.json")
        if st.exists():
            d = _j.loads(st.read_text(encoding="utf-8"))
            bits.append(f"说说今天 {sum((d.get('counts') or {}).values())} 条")
    except Exception:  # noqa: BLE001
        pass
    try:
        import socket
        s = socket.socket()
        s.settimeout(1.0)
        ok = s.connect_ex(("127.0.0.1", 8080)) == 0
        s.close()
        bits.append("QQ 侧 8080 在线" if ok else "QQ 侧 8080 已断")
    except Exception:  # noqa: BLE001
        pass
    return "｜".join(bits)


def _small_talk(text: str) -> str:
    """闲聊：优先走和 QQ 私聊完全相同的链路（人设/情绪/档案/召回/记忆写回）。

    只有那条链路失败时才退回轻量版，保证飞书不会因为一个异常就哑掉。
    """
    try:
        from plugins import ai_group
        out = _run_async(ai_group.feishu_chat(OWNER_UID, text), 120)
        if out:
            return out
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[飞书] 完整链路不可用，回退轻量：{type(e).__name__}: {e}")
    try:
        from plugins.ai_group import call_ai_raw
        import asyncio
        persona = ""
        try:
            from config import cfg
            persona = (cfg.persona.prompt or "")[:1200]
        except Exception:  # noqa: BLE001
            pass
        prompt = (
            (persona + "\n\n" if persona else "")
            + "现在是飞书私聊（QQ 可能掉线了）。用「蓝色大肥鱼」的口吻回一句，短、有脾气、"
              "不超过 40 字，不自称 AI。\n对方说：" + text[:300]
        )
        out = _run_async(call_ai_raw(prompt, "deepseek"), 60)
        return (out or "").strip()[:200] or "（本鱼在，但这一句没想好怎么说）"
    except Exception as e:  # noqa: BLE001
        return f"（回复生成失败：{type(e).__name__}）"


def _send(open_id: str, text: str) -> bool:
    try:
        import lark_oapi as lark
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest, CreateMessageRequestBody,
        )
        cli = (lark.Client.builder()
                      .app_id(_lazy_env("FEISHU_APP_ID"))
                      .app_secret(_lazy_env("FEISHU_APP_SECRET"))
                      .build())
        req = (
            CreateMessageRequest.builder()
            .receive_id_type("open_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(open_id)
                .msg_type("text")
                .content(json.dumps({"text": text}, ensure_ascii=False))
                .build()
            )
            .build()
        )
        resp = cli.im.v1.message.create(req)
        if not resp.success():
            logger.warning(f"[飞书] 发送失败：{resp.code} {resp.msg}")
            return False
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[飞书] 发送异常：{type(e).__name__}: {e}")
        return False


def _send_segments(open_id: str, text: str) -> bool:
    """像 QQ 那样把回复拆成 2~3 段发，段间停顿 0.6~1.2 秒。

    复用 ai_group.split_reply（同一套断句规则），所以两边的"说话节奏"一致。
    发之前先过审核（红线：不贴提示词/路径/密钥/配置/余额）。
    """
    try:
        from plugins import moderation
        text = moderation.filter_outbound(text, is_owner=True, channel="feishu")
    except Exception:  # noqa: BLE001
        pass
    import random as _r
    import time as _t
    try:
        from plugins.ai_group import split_reply
        parts = split_reply(text)
    except Exception:  # noqa: BLE001
        parts = [text]
    if not parts:
        return False
    ok = True
    for i, seg in enumerate(parts):
        if not seg.strip():
            continue
        ok = _send(open_id, seg.strip()) and ok
        if i < len(parts) - 1:
            _t.sleep(_r.uniform(0.6, 1.2))
    return ok


def notify_owner(text: str) -> bool:
    """供其它模块调用的主动推送（QQ 掉线告警、夜间巡检等）。"""
    _oid = _lazy_env("FEISHU_OWNER_OPEN_ID")
    _ok = bool(_lazy_env("FEISHU_ENABLED", "true").lower() in ("1", "true", "yes")
               and _lazy_env("FEISHU_APP_ID")
               and _lazy_env("FEISHU_APP_SECRET"))
    if not _ok or not _oid:
        logger.debug(f"[飞书] 推送跳过（enabled={_ok}, open_id={bool(_oid)}）")
        return False
    return _send(_oid, text[:1500])


_SEEN: dict[str, float] = {}          # event_id/message_id -> 首次处理时间
_SEEN_LOCK = threading.Lock()
_SEEN_TTL = 1800.0                    # 30 分钟内的重复推送一律丢弃
_SEEN_MAX = 512


def _is_dup(key: str) -> bool:
    """飞书同名事件重推去重：返回 True 表示已经处理过。"""
    if not key:
        return False
    now = time.time()
    with _SEEN_LOCK:
        for k, ts in list(_SEEN.items())[:64]:
            if now - ts > _SEEN_TTL:
                _SEEN.pop(k, None)
        if key in _SEEN:
            return True
        _SEEN[key] = now
        while len(_SEEN) > _SEEN_MAX:
            try:
                _SEEN.pop(next(iter(_SEEN)))
            except StopIteration:
                break
    return False


def _on_message(data) -> None:
    """im.message.receive_v1 回调：先去重，再立刻返回，耗时工作扔线程。

    飞书长连接对 ack 的时限很短，旧版在这里同步跑 LLM（最长 120s），
    超时后飞书会把同一条消息重推 2~3 次，于是她对着旧消息反复回话。
    """
    try:
        header = getattr(data, "header", None)
        event_id = str(getattr(header, "event_id", "") or "")
        msg = data.event.message
        msg_id = str(getattr(msg, "message_id", "") or "")
        key = event_id or msg_id
        lag = ""
        ct = getattr(header, "create_time", None)
        if ct:
            try:
                lag = f"（事件延迟 {time.time() - int(ct) / 1000:.0f}s）"
            except (TypeError, ValueError):
                lag = ""
        if _is_dup(key):
            logger.info(f"[飞书] 跳过重复事件 event_id={event_id[:16]} msg_id={msg_id[:16]}{lag}")
            return
        logger.info(f"[飞书] 收到事件 im.message.receive_v1{lag}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[飞书] 事件预检异常：{type(e).__name__}: {e}")
    threading.Thread(target=_handle_message, args=(data,), name="feishu-msg", daemon=True).start()


def _handle_message(data) -> None:
    """真正处理一条飞书消息（在线程里跑，长耗时不再阻塞 ack）。"""
    try:
        msg = data.event.message
        sender = data.event.sender
        open_id = getattr(getattr(sender, "sender_id", None), "open_id", "") or ""
        if OWNER_OPEN_ID and open_id != OWNER_OPEN_ID:
            logger.info(f"[飞书] 忽略非主人消息 open_id={open_id[:10]}…")
            return
        mtype = getattr(msg, "message_type", "") or ""
        if mtype == "audio":
            try:
                _c = json.loads(msg.content or "{}")
            except Exception:  # noqa: BLE001
                _c = {}
            fk = str(_c.get("file_key") or "")
            if not fk:
                _send(open_id, "这条语音我拿不到，重发一下？")
                return
            text = _feishu_voice_to_text(str(getattr(msg, "message_id", "") or ""), fk)
            if not text:
                _send(open_id, "语音听不太清，主人再说一遍或者打字？")
                return
            _send(open_id, f"（听到你说：{text[:60]}）")
        elif mtype == "image":
            try:
                _c = json.loads(msg.content or "{}")
            except Exception:  # noqa: BLE001
                _c = {}
            fk = str(_c.get("image_key") or "")
            data = _download_resource(str(getattr(msg, "message_id", "") or ""), fk, "image")
            if not data:
                _send(open_id, "这张图我没拿到，再发一次？")
                return
            global _PENDING_EDIT
            _PENDING_EDIT = (data, time.time())
            _send(open_id, "图收到了。要我怎么改？发「编辑图片 换成红裙子，背景海边」这样就行。")
            return
        elif mtype != "text":
            _send(open_id, "现在只认文字、语音和图片。")
            return
        else:
            try:
                text = json.loads(msg.content or "{}").get("text", "")
            except Exception:  # noqa: BLE001
                text = ""
        text = (text or "").strip()
        if not text:
            return
        logger.info(f"[飞书] 收到：{text[:60]}（open_id={open_id}）")
        want_voice = any(k in text for k in ("语音", "说给我听", "念给我"))
        reply, mood = _reply_full(text)
        if reply:
            _BOT_TEXT.append(reply)
            del _BOT_TEXT[:-20]
            if want_voice:
                # 要语音就不发文字了，直接说
                if not send_voice(open_id, reply):
                    _send_segments(open_id, reply)
            else:
                _send_segments(open_id, reply)
                if mood:
                    send_sticker(open_id, mood, reply)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[飞书] 处理消息异常：{type(e).__name__}: {e}")


def _run() -> None:
    global _started
    try:
        import lark_oapi as lark
        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(_on_message)
            .build()
        )
        cli = lark.ws.Client(APP_ID, APP_SECRET, event_handler=handler, log_level=lark.LogLevel.DEBUG)
        _started = True
        logger.info("[飞书] 长连接已建立，可以用飞书私聊下指令了")
        cli.start()
    except Exception as e:  # noqa: BLE001
        _started = False
        logger.warning(f"[飞书] 长连接启动失败：{type(e).__name__}: {e}")


def start() -> None:
    if not available():
        logger.info("[飞书] 未配置 FEISHU_APP_ID/FEISHU_APP_SECRET，跳过")
        return
    threading.Thread(target=_run, name="feishu-bridge", daemon=True).start()


try:
    from nonebot import get_driver

    @get_driver().on_startup
    async def _boot() -> None:  # noqa: D401
        global _MAIN_LOOP
        import asyncio as _a
        _MAIN_LOOP = _a.get_running_loop()
        start()
        try:
            start_qq_monitor()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[飞书] QQ 监控启动失败：{type(e).__name__}: {e}")
except Exception:  # noqa: BLE001
    start()

# ---------------- 图片 / 语音 ----------------

def _api_base() -> str:
    return "https://open.feishu.cn/open-apis"


def _token() -> str:
    import httpx
    r = httpx.post(f"{_api_base()}/auth/v3/tenant_access_token/internal",
                   json={"app_id": APP_ID, "app_secret": APP_SECRET}, timeout=20)
    return (r.json() or {}).get("tenant_access_token", "")


def _upload_image(path: str) -> str:
    """上传图片，返回 image_key（失败返回空）。"""
    import httpx
    try:
        data = pathlib.Path(path).read_bytes()
    except Exception:  # noqa: BLE001
        return ""
    try:
        r = httpx.post(f"{_api_base()}/im/v1/images",
                       headers={"Authorization": f"Bearer {_token()}"},
                       data={"image_type": "message"},
                       files={"image": (pathlib.Path(path).name, data, "image/jpeg")},
                       timeout=40)
        return ((r.json() or {}).get("data") or {}).get("image_key", "")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[飞书] 传图失败：{type(e).__name__}: {e}")
        return ""


def _upload_opus(opus_path: str) -> str:
    """上传语音（opus），返回 file_key。"""
    import httpx
    try:
        data = pathlib.Path(opus_path).read_bytes()
    except Exception:  # noqa: BLE001
        return ""
    try:
        r = httpx.post(f"{_api_base()}/im/v1/files",
                       headers={"Authorization": f"Bearer {_token()}"},
                       data={"file_type": "opus", "file_name": pathlib.Path(opus_path).name},
                       files={"file": (pathlib.Path(opus_path).name, data, "audio/ogg")},
                       timeout=60)
        return ((r.json() or {}).get("data") or {}).get("file_key", "")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[飞书] 传语音失败：{type(e).__name__}: {e}")
        return ""


def _send_by_type(open_id: str, msg_type: str, content: dict) -> bool:
    import httpx
    try:
        r = httpx.post(f"{_api_base()}/im/v1/messages?receive_id_type=open_id",
                       headers={"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"},
                       json={"receive_id": open_id, "msg_type": msg_type,
                             "content": json.dumps(content, ensure_ascii=False)},
                       timeout=30)
        d = r.json() or {}
        if d.get("code") != 0:
            logger.warning(f"[飞书] 发 {msg_type} 失败：{str(d)[:140]}")
            return False
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[飞书] 发 {msg_type} 异常：{type(e).__name__}: {e}")
        return False


def _upload_file_bytes(data: bytes, filename: str, file_type: str = "stream",
                       mime: str = "application/octet-stream") -> str:
    """通用上传：返回 file_key（语音用 file_type=opus，普通文件用 stream）。"""
    import httpx
    try:
        r = httpx.post(f"{_api_base()}/im/v1/files",
                       headers={"Authorization": f"Bearer {_token()}"},
                       data={"file_type": file_type, "file_name": filename},
                       files={"file": (filename, data, mime)},
                       timeout=120)
        key = ((r.json() or {}).get("data") or {}).get("file_key", "")
        if not key:
            logger.warning(f"[飞书] 传文件失败：{r.status_code} {r.text[:120]}")
        return key
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[飞书] 传文件异常：{type(e).__name__}: {e}")
        return ""


def _send_image_file(path: str) -> bool:
    """本地图片文件 → 飞书图片消息。"""
    key = _upload_image(path)
    return bool(key and _send_by_type(OWNER_OPEN_ID, "image", {"image_key": key}))


def _upload_bytes(data: bytes, filename: str = "draw.jpg") -> str:
    """把图片字节传到飞书，返回 image_key（失败返回空）。"""
    import httpx
    try:
        r = httpx.post(f"{_api_base()}/im/v1/images",
                       headers={"Authorization": f"Bearer {_token()}"},
                       data={"image_type": "message"},
                       files={"image": (filename, data, "image/jpeg")},
                       timeout=60)
        return ((r.json() or {}).get("data") or {}).get("image_key", "")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[飞书] 传图失败：{type(e).__name__}: {e}")
        return ""


def send_sticker(open_id: str, mood: str, text: str = "") -> bool:
    """按情绪从本地表情库挑一张，传上去当图片发（飞书没有 QQ 那种自定义表情包）。"""
    try:
        from plugins.ai_group import pick_emoji
        p = pick_emoji(mood, text) if mood else None
    except Exception:  # noqa: BLE001
        p = None
    if not p:
        return False
    key = _upload_image(str(p))
    if not key:
        return False
    logger.info(f"[飞书] 发表情包 mood={mood} file={pathlib.Path(p).name}")
    return _send_by_type(open_id, "image", {"image_key": key})


def send_voice(open_id: str, text: str) -> bool:
    """把一句话转成语音发过去（mp3 → opus → 上传 → 音频消息）。"""
    import subprocess
    import tempfile
    try:
        import imageio_ffmpeg
    except Exception:  # noqa: BLE001
        imageio_ffmpeg = None
    try:
        from plugins.ai_group import text_to_voice
        mp3 = _run_async(text_to_voice(text), 90)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[飞书] 语音合成失败：{type(e).__name__}: {e}")
        return False
    if not mp3 or not pathlib.Path(mp3).exists():
        return False
    if imageio_ffmpeg is None:
        logger.warning("[飞书] 没有 ffmpeg，语音发不了")
        return False
    try:
        ff = imageio_ffmpeg.get_ffmpeg_exe()
        opus = pathlib.Path(tempfile.gettempdir()) / "fs_voice.opus"
        subprocess.run([ff, "-y", "-i", str(mp3), "-c:a", "libopus", "-b:a", "32k",
                        "-ar", "48000", "-ac", "1", str(opus)], capture_output=True)
        key = _upload_opus(str(opus))
        if not key:
            return False
        dur = 0
        try:
            import wave  # noqa: F401
            dur = int(max(1.0, len(text) / 5.0) * 1000)
        except Exception:  # noqa: BLE001
            dur = 4000
        return _send_by_type(open_id, "audio", {"file_key": key, "duration": dur})
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[飞书] 语音发送失败：{type(e).__name__}: {e}")
        return False

# ---------------- 收语音：下载 → 本地 SenseVoice 转录 ----------------

def _feishu_voice_to_text(message_id: str, file_key: str) -> str:
    """把飞书语音消息下载下来，用本地 SenseVoice 转成文字（失败返回空串）。"""
    import asyncio
    import tempfile
    import httpx
    try:
        r = httpx.get(f"{_api_base()}/im/v1/messages/{message_id}/resources/{file_key}",
                      headers={"Authorization": f"Bearer {_token()}"},
                      params={"type": "file"}, timeout=60)   # 飞书：audio 消息的资源类型也是 file
        if r.status_code != 200:
            logger.warning(f"[飞书] 语音下载失败 HTTP {r.status_code}: {r.text[:120]}")
            return ""
        raw = r.content
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[飞书] 语音下载异常：{type(e).__name__}: {e}")
        return ""
    if not raw or len(raw) < 800:
        logger.warning(f"[飞书] 语音数据太小（{len(raw)} 字节）")
        return ""
    try:
        import asr
        wav = asr._to_wav(raw, ".opus")
        if not wav:
            logger.warning("[飞书] 语音转 wav 失败（ffmpeg 不支持该格式？）")
            return ""
        # 飞书回调跑在 lark 自己的事件循环里，asyncio.run 会报
        # "cannot be called from a running event loop"。
        # 改用 _run_async 丢回 bot 主循环执行。
        txt = _run_async(asr._transcribe_wav(wav), 150)
        logger.info(f"[飞书] 语音转文字：{(txt or '')[:40]}")
        return (txt or "").strip()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[飞书] 语音转录失败：{type(e).__name__}: {e}")
        return ""

# ---------------- QQ 掉线监控（只通知，不干预） ----------------
_QQ_DOWN_SINCE = 0.0
_QQ_NOTICE_AT = 0.0


def _qq_online() -> bool:
    """QQ 侧（NapCat）当前是否还有 bot 在线。"""
    try:
        from nonebot import get_bots
        return bool(get_bots())
    except Exception:  # noqa: BLE001
        return False


def _qq_monitor_loop() -> None:
    """每 2 分钟看一次 QQ 是否在线；掉线超过 5 分钟用飞书通知一次（每小时最多一次）。

    只做通知：绝不重启 NapCat、绝不改 QQ 侧状态，保证"QQ 挂了不影响别的功能"。
    """
    import time as _t
    global _QQ_DOWN_SINCE, _QQ_NOTICE_AT
    _t.sleep(90)
    while True:
        try:
            online = _qq_online()
            now = _t.time()
            if online:
                if _QQ_DOWN_SINCE:
                    mins = int((now - _QQ_DOWN_SINCE) // 60)
                    logger.info(f"[飞书] QQ 已恢复（掉线约 {mins} 分钟）")
                    if OWNER_OPEN_ID:
                        _send_segments(OWNER_OPEN_ID, f"QQ 侧恢复了（掉线约 {mins} 分钟）。")
                _QQ_DOWN_SINCE = 0.0
            else:
                if not _QQ_DOWN_SINCE:
                    _QQ_DOWN_SINCE = now
                elif now - _QQ_DOWN_SINCE > 300 and now - _QQ_NOTICE_AT > 3600:
                    _QQ_NOTICE_AT = now
                    mins = int((now - _QQ_DOWN_SINCE) // 60)
                    logger.warning(f"[飞书] QQ 已掉线 {mins} 分钟，走飞书通知（不干预 NapCat）")
                    if OWNER_OPEN_ID:
                        _send_segments(
                            OWNER_OPEN_ID,
                            f"QQ 侧掉线 {mins} 分钟了。飞书这条照常用：聊天、语音、表情包、刷抖音都行。"
                        )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[飞书] QQ 监控异常：{type(e).__name__}: {e}")
        _t.sleep(120)


def start_qq_monitor() -> None:
    import threading
    threading.Thread(target=_qq_monitor_loop, name="qq-monitor", daemon=True).start()

def send_image_bytes(data: bytes, caption: str = "") -> bool:
    """把一张图推到飞书（供 QQ 侧出图后镜像过来）。"""
    if not data or not OWNER_OPEN_ID:
        return False
    try:
        key = _upload_bytes(data)
        if not key:
            return False
        ok = _send_by_type(OWNER_OPEN_ID, "image", {"image_key": key})
        if caption:
            _send_segments(OWNER_OPEN_ID, caption)
        return ok
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[飞书] 镜像图片失败：{type(e).__name__}: {e}")
        return False
