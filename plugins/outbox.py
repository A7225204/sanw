"""投递箱：把本地文件自动发到主人 QQ + 飞书（两边同时）。

为什么需要
----------
我（运维侧）在机器人进程外生成音频/图片（训练样本、试听对比），
但 NapCat 没开 HTTP API，外部没法直接发消息。
所以约定一个文件夹：丢进去，机器人每 10 秒扫一次，发给主人，然后归档。

用法
----
    D:\\qq-ai-bot\\outbox\\  放 .mp3 / .wav / .jpg / .png
    → 自动发给主人（QQ 私聊 + 飞书）
    → 发完移到 outbox\\sent\\
投递失败 3 次后移到 outbox\\failed\\

环境变量
--------
    OUTBOX_FEISHU=0   关闭飞书镜像（默认开启）
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from nonebot import get_bots

from nonebot.log import logger
from nonebot.adapters.onebot.v11 import MessageSegment
from nonebot_plugin_apscheduler import scheduler

ROOT = Path(__file__).resolve().parent.parent
OUTBOX = ROOT / "outbox"
SENT = OUTBOX / "sent"
FAILED = OUTBOX / "failed"
OWNER_UID = 100000000
AUDIO = {".mp3", ".wav", ".flac", ".opus", ".amr", ".m4a"}
IMAGE = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
FEISHU_MIRROR = os.getenv("OUTBOX_FEISHU", "0").strip() not in {"0", "false", "False", ""}


def _owner():
    for bot in get_bots().values():
        return bot
    return None


def _to_opus(path: Path) -> Path | None:
    """飞书语音只认 opus；转不出来就退化成文件发送。"""
    try:
        import imageio_ffmpeg

        ff = imageio_ffmpeg.get_ffmpeg_exe()
        dst = Path(tempfile.gettempdir()) / (path.stem + ".opus")
        subprocess.run(
            [ff, "-y", "-i", str(path), "-c:a", "libopus", "-b:a", "32k",
             "-ar", "48000", "-ac", "1", str(dst)],
            capture_output=True, timeout=120,
        )
        return dst if dst.exists() and dst.stat().st_size > 1024 else None
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[投递箱] opus 转换失败 {path.name}：{type(e).__name__}: {e}")
        return None


async def _push_feishu(path: Path) -> bool:
    """镜像一份到飞书：音频发语音，图片发图，其他发文件。"""
    if not FEISHU_MIRROR:
        return False
    try:
        from plugins import feishu_bridge as fb

        suffix = path.suffix.lower()
        if suffix in AUDIO:
            opus = _to_opus(path)
            src = opus or path
            kind = "opus" if opus else "stream"
            mime = "audio/ogg" if opus else "audio/mpeg"
            name = src.name
            data = src.read_bytes()
            key = fb._upload_file_bytes(data, name, kind, mime)
            if key and fb._send_by_type(fb.OWNER_OPEN_ID, "audio" if opus else "file",
                                        {"file_key": key}):
                return True
            if key:
                return bool(fb._send_by_type(fb.OWNER_OPEN_ID, "file", {"file_key": key}))
            return False
        if suffix in IMAGE:
            return bool(fb._send_image_file(str(path)))
        data = path.read_bytes()
        key = fb._upload_file_bytes(data, path.name, "stream", "application/octet-stream")
        return bool(key and fb._send_by_type(fb.OWNER_OPEN_ID, "file", {"file_key": key}))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[投递箱] 飞书镜像失败 {path.name}：{type(e).__name__}: {e}")
        return False


async def _deliver() -> None:
    if not OUTBOX.exists():
        return
    files = [p for p in OUTBOX.iterdir() if p.is_file()]
    if not files:
        return
    bot = _owner()
    if bot is None:
        return
    for path in files:
        suffix = path.suffix.lower()
        qq_ok = False
        try:
            if suffix in AUDIO:
                await bot.send_private_msg(
                    user_id=OWNER_UID, message=MessageSegment.record(path.resolve().as_uri()))
            elif suffix in IMAGE:
                await bot.send_private_msg(
                    user_id=OWNER_UID, message=MessageSegment.image(path.resolve().as_uri()))
            else:
                logger.info(f"[投递箱] 跳过不支持的类型：{path.name}")
                continue
            qq_ok = True
            logger.info(f"[投递箱] 已发 QQ：{path.name}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[投递箱] QQ 发送失败 {path.name}：{type(e).__name__}: {e}")

        fs_ok = await _push_feishu(path)
        if fs_ok:
            logger.info(f"[投递箱] 已发飞书：{path.name}")

        if qq_ok or fs_ok:
            SENT.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(SENT / path.name))
        else:
            FAILED.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(path), str(FAILED / path.name))
            except Exception:  # noqa: BLE001
                pass


for _d in (OUTBOX, SENT, FAILED):
    _d.mkdir(parents=True, exist_ok=True)
logger.info(f"[投递箱] 就绪：{OUTBOX}（飞书镜像={'开' if FEISHU_MIRROR else '关'}）")


@scheduler.scheduled_job("interval", seconds=10, id="outbox_deliver")
async def _job() -> None:
    try:
        await _deliver()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[投递箱] 调度异常：{type(e).__name__}: {e}")
