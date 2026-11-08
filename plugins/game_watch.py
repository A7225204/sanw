"""打游戏时让她随机截图点评（陪游戏）。

触发方式
--------
聊天里（只认主人私聊）：
    「看我打游戏」   开始围观
    「点评一下」     立刻截一张点评
    「别看了 / 没打游戏 / 不玩了」  停止

启动器面板：
    「陪游戏(截图点评)」的启动/停止按钮 → 写 memory/game_watch_cmd.json
    本模块每 5 秒读一次指令并执行，状态写回 memory/game_watch_state.json

保护
----
· 只截游戏窗口，且游戏必须在前台（不在前台就不拍，免得拍到桌面）
· 截图压到 1024 长边
· 识图优先本地 QwenVL，失败才外发
· 连续 3 轮看不到画面自动退出
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import random
import time
from datetime import datetime
from pathlib import Path

from nonebot import get_bots, get_driver, on_message
from nonebot.adapters.onebot.v11 import MessageSegment, PrivateMessageEvent
from nonebot.log import logger
from nonebot.params import EventPlainText

ROOT = Path(__file__).resolve().parent.parent
OWNER_UID = 100000000
GAME_HINTS = ("deltaforce", "delta force", "三角洲")
WATCH_MIN_GAP = float(os.getenv("GAME_WATCH_MIN", "120") or "120")
WATCH_MAX_GAP = float(os.getenv("GAME_WATCH_MAX", "300") or "300")
WATCH_MAX_ROUNDS = int(os.getenv("GAME_WATCH_ROUNDS", "40") or "40")

_CMD_FILE = ROOT / "memory" / "game_watch_cmd.json"
_STATE_FILE = ROOT / "memory" / "game_watch_state.json"

_watch_task: asyncio.Task | None = None
_watch_stop = False
_last_comment = 0.0


# ---------------------------------------------------------------- 状态 / 指令

def _write_state(active: bool, note: str = "") -> None:
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(json.dumps({
            "active": bool(active),
            "note": note[:60],
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }, ensure_ascii=False), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[看游戏] 写状态失败：{type(e).__name__}: {e}")


def _read_cmd() -> str:
    try:
        if not _CMD_FILE.exists():
            return ""
        age = time.time() - _CMD_FILE.stat().st_mtime
        data = json.loads(_CMD_FILE.read_text(encoding="utf-8-sig")) or {}
        cmd = str(data.get("cmd") or "").strip().lower()
        _CMD_FILE.unlink(missing_ok=True)
        if age > 90:
            # 面板/上次会话遗留的旧指令：绝不能隔半天还拿来开围观
            logger.info(f"[看游戏] 忽略过期面板指令 {cmd!r}（{age:.0f}s 前写入）")
            return ""
        return cmd
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------- 截图

def _game_pids() -> set[int]:
    try:
        import psutil
    except Exception:  # noqa: BLE001
        return set()
    pids: set[int] = set()
    for p in psutil.process_iter(["name"]):
        nm = str(p.info.get("name") or "").lower()
        if any(h in nm for h in GAME_HINTS):
            pids.add(p.pid)
    return pids


def _foreground_pid() -> int:
    try:
        import ctypes
        from ctypes import wintypes
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        pid = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)
    except Exception:  # noqa: BLE001
        return 0


def _game_in_front() -> bool:
    pids = _game_pids()
    if not pids:
        return False
    return _foreground_pid() in pids


def _game_window_rect() -> tuple[int, int, int, int] | None:
    pids = _game_pids()
    if not pids:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        rects: list[tuple[int, int, int, int]] = []
        wnd_enum = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def _cb(hwnd, _lparam):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids and user32.IsWindowVisible(hwnd):
                r = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(r))
                w, h = r.right - r.left, r.bottom - r.top
                if w > 400 and h > 300:
                    rects.append((r.left, r.top, w, h))
            return True

        user32.EnumWindows(wnd_enum(_cb), 0)
        if rects:
            rects.sort(key=lambda x: x[2] * x[3], reverse=True)
            return rects[0]
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[看游戏] 找窗口失败：{type(e).__name__}: {e}")
    return None


def _grab() -> bytes | None:
    try:
        from PIL import ImageGrab
    except Exception:  # noqa: BLE001
        logger.warning("[看游戏] 没有 PIL，截不了屏")
        return None
    if not _game_in_front():
        logger.info("[看游戏] 游戏不在前台，本轮跳过（不拍桌面）")
        return None
    try:
        rect = _game_window_rect()
        if rect:
            x, y, w, h = rect
            im = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)
            logger.info(f"[看游戏] 截到游戏窗口 {w}×{h}")
        else:
            im = ImageGrab.grab(all_screens=True)
            logger.info("[看游戏] 没找到游戏窗口，截全屏")
        im = im.convert("RGB")
        im.thumbnail((1024, 1024))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=82)
        return buf.getvalue()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[看游戏] 截图失败：{type(e).__name__}: {e}")
        return None


# ---------------------------------------------------------------- 点评与投递

_COMMENT_PROMPT = (
    "这是一张游戏截图。请用中文口语点评主人的操作：\n"
    "1. 先一句话说清画面里在发生什么（人物/场景/界面/数值，看不清就说看不清，别猜）；\n"
    "2. 再用一句傲娇吐槽（像认识很久的女生，别用客服腔、别写“作为AI”）；\n"
    "总字数 60 字以内，不要分点。"
)


async def _free_comfy() -> None:
    """识图前把 ComfyUI 里别的模型卸掉——不然 QwenVL 没显存，任务会挂几分钟。"""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=20) as c:
            await c.post("http://127.0.0.1:8188/free",
                         json={"unload_models": True, "free_memory": True})
        await asyncio.sleep(2)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[看游戏] 释放显存失败：{type(e).__name__}: {e}")


async def _comment_on_screen() -> str:
    buf = await asyncio.to_thread(_grab)
    if not buf:
        return ""
    try:
        from plugins import ai_group
    except Exception:  # noqa: BLE001
        return ""
    # 打游戏这条固定走在线（快、不占显存、不耽误出图）；其他场景仍本地优先
    order = os.getenv("GAME_WATCH_VISION", "online").strip().lower()
    if order == "local":
        await _free_comfy()
        channels = ("_local_qwen_vision", "_ark_vision", "_sf_vision")
    else:
        channels = ("_ark_vision", "_sf_vision", "_local_qwen_vision")
    for fn in channels:
        f = getattr(ai_group, fn, None)
        if not f:
            continue
        if fn == "_local_qwen_vision":
            await _free_comfy()   # 只有要走本地才腾显存
        try:
            txt = await asyncio.wait_for(f(buf, _COMMENT_PROMPT), timeout=100)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[看游戏] {fn} 异常：{type(e).__name__}: {e}")
            continue
        txt = str(txt or "").strip()
        logger.info(f"[看游戏] {fn} 返回 {len(txt)} 字：{txt[:60]}")
        if len(txt) >= 6:
            return txt
    logger.warning("[看游戏] 所有识图通道都没出结果")
    return ""


async def _deliver(text: str) -> None:
    if not text:
        return
    for bot in get_bots().values():
        try:
            await bot.send_private_msg(user_id=OWNER_UID, message=MessageSegment.text(text))
            logger.info(f"[看游戏] 已发私聊：{text[:40]}")
            break
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[看游戏] QQ 发送失败：{type(e).__name__}: {e}")
    try:
        from plugins import feishu_bridge
        if feishu_bridge.mirror_enabled():
            await asyncio.to_thread(feishu_bridge.notify_owner, text)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- 围观循环

async def _watch_loop() -> None:
    global _watch_stop
    rounds = 0
    skips = 0
    _watch_stop = False
    _write_state(True, "watching")
    logger.info("[看游戏] 开始围观")
    await _deliver("(*搬小板凳*) 行吧，本鱼坐这儿看你打，别死太多次啊。")
    while not _watch_stop and rounds < WATCH_MAX_ROUNDS:
        await asyncio.sleep(random.uniform(WATCH_MIN_GAP, WATCH_MAX_GAP))
        if _watch_stop:
            break
        rounds += 1
        try:
            txt = await _comment_on_screen()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[看游戏] 点评失败：{type(e).__name__}: {e}")
            continue
        if txt:
            skips = 0
            await _deliver(txt)
        else:
            skips += 1
            logger.info(f"[看游戏] 这轮没截到或没识别出来（连续 {skips} 次）")
            if skips >= 3:
                logger.info("[看游戏] 连续 3 轮看不到游戏画面，自动退出")
                await _deliver("(*眯眼*) 屏幕上找不着游戏，本鱼先不看了。要再叫我看就说一声。")
                _watch_stop = True
                break
    logger.info(f"[看游戏] 结束（{rounds} 轮）")
    _write_state(False, f"ended after {rounds}")
    if not _watch_stop:
        await _deliver("(*揉眼睛*) 看累了，本鱼先去歇会儿。")


def _start_watch(note: str = "") -> str:
    global _watch_task
    if _watch_task and not _watch_task.done():
        return "already"
    _watch_task = asyncio.get_event_loop().create_task(_watch_loop())
    _write_state(True, note or "start")
    return "started"


def _stop_watch() -> str:
    global _watch_task, _watch_stop
    _watch_stop = True
    if _watch_task and not _watch_task.done():
        _watch_task.cancel()
    _watch_task = None
    _write_state(False, "stopped")
    return "stopped"


# ---------------------------------------------------------------- 面板指令轮询

async def _poll_panel_cmd() -> None:
    """面板指令轮询：每 5 秒看一次 memory/game_watch_cmd.json。"""
    while True:
        try:
            cmd = _read_cmd()
            if cmd in ("start", "on", "begin"):
                logger.info("[看游戏] 面板指令：开始")
                _start_watch("panel")
            elif cmd in ("stop", "off", "end"):
                logger.info("[看游戏] 面板指令：停止")
                _stop_watch()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[看游戏] 指令轮询异常：{type(e).__name__}: {e}")
        await asyncio.sleep(5)


@get_driver().on_startup
async def _start_panel_poller() -> None:
    try:
        if _CMD_FILE.exists():
            _CMD_FILE.unlink(missing_ok=True)
            logger.info("[看游戏] 启动时清掉残留面板指令，避免开机自己开始围观")
    except Exception:  # noqa: BLE001
        pass
    asyncio.get_event_loop().create_task(_poll_panel_cmd())
    logger.info("[看游戏] 面板指令轮询已启动（5 秒一次）")


# ---------------------------------------------------------------- 聊天触发（仅私聊主人）

watch_cmd = on_message(priority=6, block=False)   # block 必须 False，否则会拦住 ai_group(priority=10)


@watch_cmd.handle()
async def _(event: PrivateMessageEvent, text: str = EventPlainText()):
    global _last_comment
    if str(event.get_user_id()) != str(OWNER_UID):
        return
    t = (text or "").strip()
    # 指令必须带 / 前缀（主人 2026-10-29 定），否则按普通聊天处理
    if t[:1] not in ("/", "／"):
        return
    t = t.lstrip("/／").strip()
    if t.startswith("画图"):
        return

    if any(k in t for k in ("别看了", "不看了", "不用看了", "停止看", "关掉围观", "别围观",
                            "没打游戏", "没在打", "不打了", "不玩了", "没玩了", "没玩", "退出了")):
        _stop_watch()
        await watch_cmd.finish("(*收起小板凳*) 好，不看了。")

    if any(k in t for k in ("看我打游戏", "看我玩", "围观我", "开始看游戏")):
        _start_watch("chat")
        await watch_cmd.finish("(*搬小板凳*) 好，本鱼这就盯着你打。不想看了说「没打游戏」。")

    if any(k in t for k in ("点评一下", "点评我", "看我操作", "截个图看看")):
        if time.time() - _last_comment < 8:
            await watch_cmd.finish("(*翻白眼*) 刚点完，喘口气。")
        _last_comment = time.time()
        await watch_cmd.send("(*眯起眼睛*) 让本鱼瞅一眼…")
        txt = await _comment_on_screen()
        await watch_cmd.finish(txt or "(*眯眼*) 屏幕黑乎乎的，看不清。")
