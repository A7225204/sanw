"""ComfyUI 拉起守护：全家桶里 ComfyUI 默认不自启，谁用到谁负责拉起来。

画图（ai_group）、唱歌（ace_sing / ace_sing15）、改图（image_edit）共用这一份，
启动参数必须和 start_all.ps1 保持一致，尤其 --disable-mmap（绕开 torch mmap 崩溃）。
"""
from __future__ import annotations

import os
import socket
import subprocess
import threading
import time
from pathlib import Path

import httpx

try:  # 允许脱离 nonebot 单独跑测试
    from nonebot.log import logger
except Exception:  # noqa: BLE001
    import logging

    logger = logging.getLogger("comfy_boot")

COMFY_URL = os.getenv("COMFY_URL", "http://127.0.0.1:8188")
COMFY_PY = r"C:\ComfyUI-aki(2)\ComfyUI-aki-v1.7\python\python.exe"
COMFY_DIR = r"C:\ComfyUI-aki(2)\ComfyUI-aki-v1.7\ComfyUI"
COMFY_PORT = 8188
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"

_LOCK = threading.Lock()
_PROC = None


def alive(timeout: float = 3.0) -> bool:
    """HTTP 探活：/system_stats 通才是真的起来了（端口开不代表能跑图）。"""
    try:
        r = httpx.get(f"{COMFY_URL}/system_stats", timeout=timeout)
        return r.status_code == 200
    except Exception:  # noqa: BLE001
        return False


def _port_open() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", COMFY_PORT), timeout=1.5):
            return True
    except Exception:  # noqa: BLE001
        return False


def ensure(timeout: float = 150.0, tag: str = "ComfyUI") -> bool:
    """没起来就拉起它；返回是否可用。线程安全，并发调用只会拉起一个进程。"""
    global _PROC
    if alive():
        return True

    with _LOCK:
        if alive():
            return True
        if not _port_open():
            try:
                LOG_DIR.mkdir(parents=True, exist_ok=True)
                env = dict(os.environ)
                env.setdefault("GIT_PYTHON_GIT_EXECUTABLE", r"C:\Program Files\Git\cmd\git.exe")
                out = open(LOG_DIR / "comfyui.out.log", "ab", buffering=0)
                err = open(LOG_DIR / "comfyui.err.log", "ab", buffering=0)
                logger.warning(f"[{tag}] ComfyUI 没响应，拉起中…（首次约 25~40s）")
                _PROC = subprocess.Popen(
                    [COMFY_PY, "main.py", "--listen", "127.0.0.1",
                     "--port", str(COMFY_PORT), "--disable-mmap"],
                    cwd=COMFY_DIR, env=env, stdout=out, stderr=err,
                    creationflags=(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                        | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)))
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[{tag}] 拉起 ComfyUI 失败：{type(e).__name__}: {e}")
                return False
        else:
            logger.info(f"[{tag}] ComfyUI 端口已开但还没就绪，等它自己起来")

    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(3)
        if alive():
            logger.info(f"[{tag}] ComfyUI 已就绪（{time.time() - t0:.0f}s）")
            return True
        if _PROC is not None and _PROC.poll() is not None:
            logger.warning(f"[{tag}] ComfyUI 启动后退出（code={_PROC.returncode}），见 logs/comfyui.err.log")
            return False
    logger.warning(f"[{tag}] ComfyUI 拉起超时，本次放弃")
    return False


async def ensure_async(timeout: float = 150.0, tag: str = "ComfyUI") -> bool:
    """异步版：丢线程池跑，别卡事件循环。"""
    import asyncio

    return await asyncio.to_thread(ensure, timeout, tag)
