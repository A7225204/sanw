"""唱歌完整链路：ACE 生成 → 分离人声/伴奏 → Seed-VC 变声 → 混回伴奏。

为什么要这一步
--------------
Seed-VC 是"人声转换"模型，官方示例输入都是已经分离好的纯人声。
直接把带伴奏的歌喂给它，输出就只剩人声（主人反馈"伴奏没了"）。
正确顺序是：先把伴奏拆出来，只转人声，最后用 ffmpeg 混合回去。

依赖
----
    demucs 4.1（htdemucs，模型约 80MB，首次跑会下到用户缓存）
    D:\\Seed-VC\\.venv 环境里已装
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path

from nonebot.log import logger

ROOT = Path(__file__).resolve().parent.parent
VPY = Path(r"D:\Seed-VC\.venv\Scripts\python.exe")

# ---- 唱歌优先用派蒙 RVC 变声（和平时说话的嗓子一致）----
RVC_URL = os.getenv("RVC_URL", "http://127.0.0.1:8101").rstrip("/")
RVC_PY = Path(r"D:\RVC\.venv\Scripts\python.exe")
RVC_DIR = Path(r"D:\RVC")
RVC_MODEL = "assets/weights/paimeng.pth"
RVC_INDEX = "assets/indices/paimeng.index"


def _rvc_alive() -> bool:
    try:
        import httpx
        return bool((httpx.get(RVC_URL + "/health", timeout=2).json() or {}).get("ok"))
    except Exception:  # noqa: BLE001
        return False


def _ensure_rvc(timeout: float = 120.0) -> bool:
    """确保 RVC 变声服务在跑。唱歌时 ACE 已经跑完，显存/内存都释放了，够用。"""
    if _rvc_alive():
        return True
    if not RVC_PY.is_file() or not (RVC_DIR / RVC_MODEL).is_file():
        return False
    try:
        (ROOT / "logs").mkdir(exist_ok=True)
        logf = open(ROOT / "logs" / "rvc_sing.log", "ab", buffering=0)  # noqa: SIM115
        subprocess.Popen(
            [str(RVC_PY), "rvc_server.py", "--model", RVC_MODEL,
             "--index", RVC_INDEX, "--port", "8101"],
            cwd=str(RVC_DIR), stdout=logf, stderr=logf,
            creationflags=(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                        | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)))
        logger.info("[唱歌] 正在拉起 RVC 变声服务（加载模型约 40 秒）")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[唱歌] 拉起 RVC 失败：{type(e).__name__}: {e}")
        return False
    t0 = time.time()
    while time.time() - t0 < timeout:
        if _rvc_alive():
            return True
        time.sleep(2)
    return False


def _convert_voice(vocals: Path) -> tuple[str | None, str]:
    """人声变声：优先派蒙 RVC，失败再退回 Seed-VC。"""
    if _ensure_rvc():
        try:
            import httpx
            out = Path(tempfile.gettempdir()) / f"sing_rvc_{int(time.time())}.mp3"
            r = httpx.post(RVC_URL + "/convert",
                           json={"input": str(vocals), "output": str(out), "pitch": 0},
                           timeout=300)
            d = r.json() or {}
            if d.get("ok") and d.get("path"):
                logger.info(f"[唱歌] RVC 变声完成 {d.get('ms')}ms")
                return str(d["path"]), ""
            logger.warning(f"[唱歌] RVC 变声失败：{str(d)[:120]}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[唱歌] RVC 变声异常：{type(e).__name__}: {e}")
    from plugins import seedvc
    logger.info("[唱歌] 退回 Seed-VC 变声")
    return seedvc.convert(str(vocals), 15, True)


def _ffmpeg() -> str:
    try:
        from plugins.douyin_fetch import _ffmpeg as ff
        return ff()
    except Exception:  # noqa: BLE001
        return "ffmpeg"


def separate(src: Path, out_dir: Path) -> tuple[Path | None, Path | None, str]:
    """分离人声/伴奏。返回 (vocals, 伴奏, 错误)。"""
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            [str(VPY), "-m", "demucs.separate", "--two-stems=vocals",
             "-n", "htdemucs", "--out", str(out_dir), "--mp3",
             "--mp3-bitrate", "192", str(src)],
            cwd=r"D:\Seed-VC", capture_output=True, timeout=600,
        )
        if proc.returncode != 0:
            err = (proc.stderr or b"")[-300:].decode("utf-8", "ignore")
            logger.warning(f"[唱歌] demucs 失败：{err[:200]}")
            return None, None, err[-160:]
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[唱歌] demucs 异常：{type(e).__name__}: {e}")
        return None, None, str(e)
    # 产物路径：<out>/htdemucs/<曲名>/vocals.mp3
    stem = src.stem
    base = out_dir / "htdemucs" / stem
    vocals = base / "vocals.mp3"
    back = base / "no_vocals.mp3"
    if not vocals.exists():
        cands = list(out_dir.rglob("vocals.mp3"))
        vocals = cands[0] if cands else None
    if not back.exists():
        cands = list(out_dir.rglob("no_vocals.mp3"))
        back = cands[0] if cands else None
    if not vocals or not back:
        return None, None, "分离产物缺失"
    logger.info(f"[唱歌] 分离完成：人声 {vocals.stat().st_size // 1024}KB / 伴奏 {back.stat().st_size // 1024}KB")
    return vocals, back, ""


def mix(vocals: Path, backing: Path, out: Path) -> tuple[Path | None, str]:
    """把变声后的人声和伴奏混回去。"""
    ff = _ffmpeg()
    cmd = [
        ff, "-y", "-i", str(vocals), "-i", str(backing),
        "-filter_complex",
        "[0:a]volume=1.15[a0];[1:a]volume=1.0[a1];[a0][a1]amix=inputs=2:duration=longest:normalize=0[out]",
        "-map", "[out]", "-b:a", "192k", str(out),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=300)
        if proc.returncode != 0 or not out.exists() or out.stat().st_size < 20 * 1024:
            err = (proc.stderr or b"")[-300:].decode("utf-8", "ignore")
            logger.warning(f"[唱歌] 混音失败：{err[:200]}")
            return None, err[-160:]
        logger.info(f"[唱歌] 混音完成：{out.name} {out.stat().st_size // 1024}KB")
        return out, ""
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[唱歌] 混音异常：{type(e).__name__}: {e}")
        return None, str(e)


def sing_with_backing(ace_audio: str) -> tuple[str | None, str]:
    """ACE 出的歌 → 换人声 → 带伴奏的成品。返回 (最终 mp3, 错误)。"""
    from plugins import seedvc

    src = Path(ace_audio)
    if not src.exists():
        return None, "源音频不存在"
    t0 = time.time()
    tmp = Path(tempfile.mkdtemp(prefix="singmix_"))
    vocals, backing, err = separate(src, tmp)
    if not vocals:
        # 分离失败就退回旧行为（只有人声），至少能出声
        logger.warning("[唱歌] 分离失败，退回纯人声转换")
        conv, e2 = _convert_voice(src)
        return (conv, "") if conv else (None, e2 or err)
    conv, err2 = _convert_voice(vocals)
    if not conv:
        return None, err2 or "变声失败"
    out = tmp / f"{src.stem}_sing.mp3"
    final, err3 = mix(Path(conv), backing, out)
    if not final:
        return conv, err3  # 混音失败至少给变声版
    logger.info(f"[唱歌] 全链路完成 {time.time() - t0:.1f}s")
    return str(final), ""
