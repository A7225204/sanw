"""用 Seed-VC 把 ACE-Step 唱出来的歌，换成大肥鱼自己的音色。

为什么用它
----------
ACE-Step 只会“唱”，但音色是通用女声；Seed-VC 是零样本音色转换：
给一段她的声音（ref_feiyu.wav，约 30 秒），就能把整首歌换成她的声音唱，
不需要训练、不需要 RVC 那套 fairseq 编译。

调用链
------
    唱歌 → ACE-Step 1.5 生成 mp3 → seedvc.convert() → 她的音色 wav → 发给她

环境
----
    D:\\Seed-VC\\.venv\\Scripts\\python.exe   （复用 ComfyUI 的 torch 2.7+cu128）
    D:\\Seed-VC\\inference.py
    D:\\Seed-VC\\ref_feiyu.wav                 （她的参考音）
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from nonebot.log import logger

VDIR = Path(r"D:\Seed-VC")
VPY = VDIR / ".venv" / "Scripts" / "python.exe"
REF = VDIR / "ref_feiyu.wav"
OUTDIR = VDIR / "converted"


def available() -> tuple[bool, str]:
    if not VPY.exists():
        return False, "没有 Seed-VC 环境"
    if not REF.exists():
        return False, "没有参考音"
    if not (VDIR / "inference.py").exists():
        return False, "没有 inference.py"
    return True, ""


def _free_comfy_vram() -> None:
    """变声前把 ComfyUI 占的显存要回来，否则 GPU 换页会慢 10 倍（实测 12min → 61s）。"""
    try:
        import httpx
        httpx.post("http://127.0.0.1:8188/free",
                   json={"unload_models": True, "free_memory": True},
                   timeout=15)
        logger.info("[变声] 已让 ComfyUI 释放显存")
        time.sleep(3)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[变声] 释放显存失败（继续）：{e}")


def convert(src_audio: str, steps: int = 15, f0: bool = True,
            timeout: float = 900.0) -> tuple[str | None, str]:
    """把 src_audio 换成她音色。返回 (输出 wav, 错误说明)。"""
    ok, why = available()
    if not ok:
        return None, why
    src = Path(src_audio)
    if not src.exists():
        return None, "源音频不存在"
    OUTDIR.mkdir(parents=True, exist_ok=True)
    _free_comfy_vram()
    t0 = time.time()
    cmd = [
        str(VPY), "inference.py",
        "--source", str(src),
        "--target", str(REF),
        "--output", str(OUTDIR),
        "--diffusion-steps", str(steps),
        "--f0-condition", "True" if f0 else "False",
        "--fp16", "True",
    ]
    # HuggingFace 不可达时，huggingface_hub 的在线版本检查会卡死（实测卡在 1% GPU 十几分钟）
    # 模型早就缓存在本地了，强制离线跑，只用缓存
    env = os.environ.copy()
    env["HF_HUB_OFFLINE"] = "1"
    env["TRANSFORMERS_OFFLINE"] = "1"
    env["HF_HUB_DISABLE_TELEMETRY"] = "1"
    try:
        proc = subprocess.run(cmd, cwd=str(VDIR), capture_output=True,
                              timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        logger.warning(f"[变声] 超时 {timeout:.0f}s")
        return None, "变声超时"
    tail = (proc.stdout or b"")[-400:].decode("utf-8", "ignore").replace("\n", " ")
    if proc.returncode != 0:
        err = (proc.stderr or b"")[-400:].decode("utf-8", "ignore")
        logger.warning(f"[变声] 失败 rc={proc.returncode} {err[:200]}")
        return None, err[-160:] or "变声失败"
    # 找最新产物
    files = sorted(OUTDIR.glob("vc_*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return None, "没生成文件"
    newest = files[0]
    if newest.stat().st_mtime < t0 - 5:
        return None, "产物不是本次生成的"
    logger.info(f"[变声] 完成 {time.time() - t0:.1f}s → {newest.name}｜{tail[-80:]}")
    return str(newest), ""
