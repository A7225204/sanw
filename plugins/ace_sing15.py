"""ACE-Step 1.5 唱歌（支持中文）：走 ComfyUI 原生 1.5 节点。

为什么换 1.5
------------
1.0（ace_step_v1_3.5b）只会唱英文，中文歌词也会被唱成英文。
1.5 的 TextEncodeAceStepAudio1.5 带 language 参数，支持 zh / yue / ja 等 50 多种。

用的模型（split 版，省显存）
    diffusion_models\\acestep_v1.5_turbo.safetensors   4.46 GB
    text_encoders\\qwen_0.6b_ace15.safetensors         1.11 GB
    vae\\ace_1.5_vae.safetensors                       0.31 GB

对外：
    sing(lyrics, seconds=30, tags=..., language="zh") -> (mp3 路径, 错误说明)
"""
from __future__ import annotations

import random
import re
import time
from pathlib import Path

import httpx

from nonebot.log import logger

COMFY_URL = "http://127.0.0.1:8188"
UNET = "acestep_v1.5_turbo.safetensors"
CLIP_SMALL = "qwen_0.6b_ace15.safetensors"
CLIP_BIG = "qwen_1.7b_ace15.safetensors"
VAE = "ace_1.5_vae.safetensors"
OUTPUT_DIR = Path(r"C:\ComfyUI-aki(2)\ComfyUI-aki-v1.7\ComfyUI\output")

DEFAULT_TAGS = ("anime, soft female vocals, kawaii pop, cheerful, cute, piano, light, "
                "clear pronunciation, crisp diction, clean vocals")
CJK = re.compile(r"[\u4e00-\u9fff]")


def _guess_language(lyrics: str) -> str:
    return "zh" if CJK.search(lyrics or "") else "en"


def _workflow(lyrics: str, seconds: float, tags: str, language: str,
              bpm: int, steps: int, seed: int) -> dict:
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": UNET, "weight_dtype": "default"}},
        # 1.5 必须用 DualCLIPLoader（0.6b + 1.7b），单 CLIPLoader 会报 state_dict 尺寸不匹配
        "2": {"class_type": "DualCLIPLoader",
              "inputs": {"clip_name1": CLIP_SMALL, "clip_name2": CLIP_BIG,
                         "type": "ace", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": VAE}},
        "4": {"class_type": "TextEncodeAceStepAudio1.5",
              "inputs": {"clip": ["2", 0], "tags": tags, "lyrics": lyrics,
                         "seed": seed, "bpm": int(bpm), "duration": float(seconds),
                         "timesignature": "4", "language": language,
                         "keyscale": "C major",
                         # 下面这些是 1.5 节点必填的采样参数（模板里就是这个值）
                         "generate_audio_codes": True, "cfg_scale": 2.0,
                         "temperature": 0.85, "top_p": 0.9, "top_k": 0, "min_p": 0.0}},
        "5": {"class_type": "EmptyAceStep1.5LatentAudio",
              "inputs": {"seconds": float(seconds), "batch_size": 1}},
        "6": {"class_type": "KSampler",
              "inputs": {"model": ["1", 0], "positive": ["4", 0], "negative": ["4", 0],
                         "latent_image": ["5", 0], "seed": seed,
                         "steps": steps, "cfg": 1.0, "sampler_name": "euler",
                         "scheduler": "simple", "denoise": 1.0}},
        "7": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["6", 0], "vae": ["3", 0]}},
        "8": {"class_type": "SaveAudioMP3",
              "inputs": {"audio": ["7", 0], "filename_prefix": "sing15", "quality": "V0"}},
    }


def sing(lyrics: str, seconds: float = 30.0, tags: str = DEFAULT_TAGS,
         language: str = "", bpm: int = 120, steps: int = 40,
         timeout: float = 900.0) -> tuple[str | None, str]:
    """生成一首歌（1.5 版），返回 (mp3 路径, 错误说明)。turbo 默认 8 步。"""
    lyrics = (lyrics or "").strip()[:1500]
    if not lyrics:
        return None, "歌词为空"
    lyrics = _format_lyrics(lyrics)
    lang = language or _guess_language(lyrics)
    seed = random.randint(1, 2 ** 31 - 1)
    # 2026-11-07：把唱歌纳入四方互斥（画图 / 语音 / 识图 / 本地LLM）。
    # 唱歌走的是同一个 ComfyUI，和画图抢同一块显存；以前它一声不吭就提交 workflow，
    # 画图跑到一半显存被挤爆，直接 access violation 崩掉，日志里还看不出是谁干的。
    try:
        from plugins import ai_group as _ag
        if _ag._comfy_busy():
            return None, "画图/识图正占着显存，等它跑完我再唱"
        # 占住位置：唱歌最长跑 timeout 秒，中间别让画图/识图插进来
        _ag._COMFY_BUSY_UNTIL = time.time() + max(120.0, float(timeout))
    except Exception:  # noqa: BLE001
        pass
    from plugins import comfy_boot
    if not comfy_boot.ensure(tag="唱歌1.5"):
        try:
            from plugins import ai_group as _ag2
            _ag2._COMFY_BUSY_UNTIL = 0.0
        except Exception:  # noqa: BLE001
            pass
        _release_busy()
        return None, "ComfyUI 没起来（唱歌要先有它）"
    # 唱歌前先把画图的模型卸了，腾显存
    try:
        with httpx.Client(timeout=15) as _fc:
            _fc.post(f"{COMFY_URL}/free", json={"unload_models": True, "free_memory": True})
    except Exception:  # noqa: BLE001
        pass
    t0 = time.time()
    try:
        with httpx.Client(timeout=60) as c:
            q = c.post(f"{COMFY_URL}/prompt",
                       json={"prompt": _workflow(lyrics, seconds, tags, lang, bpm, steps, seed)})
            if q.status_code != 200:
                logger.warning(f"[唱歌1.5] 工作流被拒：{q.text[:200]}")
                _release_busy()
                return None, f"工作流被拒：{q.text[:120]}"
            pid = (q.json() or {}).get("prompt_id", "")
            deadline = time.time() + timeout
            while time.time() < deadline:
                time.sleep(1.5)
                h = c.get(f"{COMFY_URL}/history/{pid}").json().get(pid) or {}
                st = (h.get("status") or {}).get("status_str")
                if st == "error":
                    for m in (h.get("status") or {}).get("messages") or []:
                        if m[0] == "execution_error":
                            logger.warning(f"[唱歌1.5] 出错：{m[1].get('node_type')} "
                                           f"{str(m[1].get('exception_message'))[:160]}")
                    _release_busy()
                    return None, "执行出错"
                for node in (h.get("outputs") or {}).values():
                    for item in (node.get("audio") or []) + (node.get("files") or []):
                        name = item.get("filename") if isinstance(item, dict) else None
                        if not name:
                            continue
                        path = OUTPUT_DIR / (item.get("subfolder") or "") / name
                        if path.exists():
                            logger.info(f"[唱歌1.5] 完成 {time.time() - t0:.1f}s "
                                        f"{seconds:.0f}s 音频 语言={lang} → {path.name}")
                            _release_busy()
                            return str(path), ""
                if time.time() - t0 > timeout:
                    break
            logger.warning(f"[唱歌1.5] 超时 {timeout:.0f}s")
            _release_busy()
            return None, "生成超时"
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[唱歌1.5] 异常：{type(e).__name__}: {e}")
        _release_busy()
        return None, f"{type(e).__name__}: {e}"

def _release_busy(wait: float = 5.0) -> None:
    """唱完/失败后释放互斥位，留几秒冷却别让画图立刻插进来。"""
    try:
        from plugins import ai_group as _ag
        _ag._COMFY_BUSY_UNTIL = time.time() + wait
    except Exception:  # noqa: BLE001
        pass


def _format_lyrics(lyrics: str) -> str:
    """把歌词整理成 ACE 好唱的格式：短句 + [Verse]/[Chorus] 段落标记。

    实测 ACE-Step 1.5 对长句（>14 字）咬字会糊，拆成短句后清楚很多。
    """
    text = str(lyrics or "").strip()
    if not text:
        return ""
    has_section = "[verse" in text.lower() or "[chorus" in text.lower() or "主歌" in text or "副歌" in text
    lines = []
    for raw in text.splitlines():
        line = raw.strip().strip("，,。.！!？?；;")
        if not line:
            continue
        low = line.lower()
        if any(k in low for k in ("[verse", "[chorus", "[bridge", "[intro", "[outro")) or line in ("主歌", "副歌"):
            lines.append(line.upper() if low.startswith("[") else ("[Verse]" if line == "主歌" else "[Chorus]"))
            continue
        # 按标点拆成短句
        for seg in re.split(r"[，,。.！!？?；;、]", line):
            seg = seg.strip()
            if not seg:
                continue
            while len(seg) > 16:
                lines.append(seg[:14])
                seg = seg[14:]
            if seg:
                lines.append(seg)
    if not has_section:
        # 没有段落标记就自动分两段
        mid = max(1, len(lines) // 2)
        lines = ["[Verse]"] + lines[:mid] + ["[Chorus]"] + lines[mid:]
    return "\n".join(lines)
