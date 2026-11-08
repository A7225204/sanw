"""ACE-Step 唱歌：在 ComfyUI 里跑原生 ACE-Step 节点，生成带人声的歌曲。

流程（全部用 ComfyUI 自带节点，不依赖第三方包）：

    CheckpointLoaderSimple(ace_step_v1_3.5b)
      → ModelSamplingSD3? 不需要，ACE-Step 用 KSampler 标准流程
    TextEncodeAceStepAudio(tags=风格, lyrics=歌词)   → CONDITIONING
    EmptyAceStepLatentAudio(seconds=时长)            → LATENT
    KSampler → VAEDecodeAudio → SaveAudioMP3

对外：
    sing(lyrics, seconds=30, tags=...) -> (mp3 路径, 错误说明)
"""
from __future__ import annotations

import random
import time
from pathlib import Path

import httpx

from nonebot.log import logger

COMFY_URL = "http://127.0.0.1:8188"
CKPT = "ace_step_v1_3.5b.safetensors"
DEFAULT_TAGS = "pop, female vocal, cheerful, cute, light background music"
OUTPUT_DIR = Path(r"C:\ComfyUI-aki(2)\ComfyUI-aki-v1.7\ComfyUI\output")


def _workflow(lyrics: str, seconds: float, tags: str, steps: int) -> dict:
    """严格照 ComfyUI 官方 audio_ace_step_1_t2a_song 模板搭的图。

    关键点：
      · ModelSamplingSD3(shift=5) 必须加，否则人声出不来
      · 负条件用 ConditioningZeroOut（不是再写一遍同样的文本）
      · LatentApplyOperationCFG + Tonemap 是官方模板里稳定音质的做法
    """
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": CKPT}},
        "2": {"class_type": "TextEncodeAceStepAudio",
              "inputs": {"clip": ["1", 1], "tags": tags, "lyrics": lyrics,
                         "lyrics_strength": 1.0}},
        "3": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["2", 0]}},
        "4": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["1", 0], "shift": 5.0}},
        "5": {"class_type": "EmptyAceStepLatentAudio",
              "inputs": {"seconds": float(seconds), "batch_size": 1}},
        "6": {"class_type": "LatentOperationTonemapReinhard", "inputs": {"multiplier": 1.0}},
        "7": {"class_type": "LatentApplyOperationCFG",
              "inputs": {"model": ["4", 0], "operation": ["6", 0]}},
        "8": {"class_type": "KSampler",
              "inputs": {"model": ["7", 0], "positive": ["2", 0], "negative": ["3", 0],
                         "latent_image": ["5", 0], "seed": random.randint(1, 2 ** 31 - 1),
                         "steps": steps, "cfg": 5.0, "sampler_name": "euler",
                         "scheduler": "simple", "denoise": 1.0}},
        "9": {"class_type": "VAEDecodeAudio",
              "inputs": {"samples": ["8", 0], "vae": ["1", 2]}},
        "10": {"class_type": "SaveAudioMP3",
               "inputs": {"audio": ["9", 0], "filename_prefix": "sing", "quality": "V0"}},
    }


def _release_busy(wait: float = 5.0) -> None:
    """唱完/失败后释放互斥位，留几秒冷却别让画图立刻插进来。"""
    try:
        from plugins import ai_group as _ag
        _ag._COMFY_BUSY_UNTIL = time.time() + wait
    except Exception:  # noqa: BLE001
        pass


def sing(lyrics: str, seconds: float = 30.0, tags: str = DEFAULT_TAGS,
         steps: int = 60, timeout: float = 900.0) -> tuple[str | None, str]:
    """生成一首歌，返回 (mp3 路径, 错误说明)。"""
    lyrics = (lyrics or "").strip()[:1200]
    if not lyrics:
        return None, "歌词为空"
    # 2026-11-07：旧版唱歌也走 ComfyUI，同样要纳入四方互斥（画图/语音/识图/本地LLM）
    try:
        from plugins import ai_group as _ag
        if _ag._comfy_busy():
            return None, "画图/识图正占着显存，等它跑完我再唱"
        _ag._COMFY_BUSY_UNTIL = time.time() + max(120.0, float(timeout))
    except Exception:  # noqa: BLE001
        pass
    from plugins import comfy_boot
    if not comfy_boot.ensure(tag="唱歌"):
        try:
            from plugins import ai_group as _ag2
            _ag2._COMFY_BUSY_UNTIL = time.time() + 5
        except Exception:  # noqa: BLE001
            pass
        return None, "ComfyUI 没起来（唱歌要先有它）"
    try:
        with httpx.Client(timeout=15) as _fc:
            _fc.post(f"{COMFY_URL}/free", json={"unload_models": True, "free_memory": True})
    except Exception:  # noqa: BLE001
        pass
    t0 = time.time()
    try:
        with httpx.Client(timeout=60) as c:
            q = c.post(f"{COMFY_URL}/prompt",
                       json={"prompt": _workflow(lyrics, seconds, tags, steps)})
            if q.status_code != 200:
                logger.warning(f"[唱歌] ACE 工作流被拒：{q.text[:200]}")
                _release_busy()
                return None, f"工作流被拒：{q.text[:120]}"
            pid = (q.json() or {}).get("prompt_id", "")
            deadline = time.time() + timeout
            while time.time() < deadline:
                time.sleep(2.0)
                h = c.get(f"{COMFY_URL}/history/{pid}").json().get(pid) or {}
                st = (h.get("status") or {}).get("status_str")
                if st == "error":
                    for m in (h.get("status") or {}).get("messages") or []:
                        if m[0] == "execution_error":
                            logger.warning(f"[唱歌] ACE 出错：{m[1].get('node_type')} "
                                           f"{str(m[1].get('exception_message'))[:160]}")
                    _release_busy()
                    return None, "ACE 执行出错"
                for node in (h.get("outputs") or {}).values():
                    for item in (node.get("audio") or []) + (node.get("files") or []):
                        name = item.get("filename") if isinstance(item, dict) else None
                        if not name:
                            continue
                        path = OUTPUT_DIR / (item.get("subfolder") or "") / name
                        if path.exists():
                            logger.info(f"[唱歌] ACE 生成完成 {time.time() - t0:.1f}s → {path.name}")
                            _release_busy()
                            return str(path), ""
                        r = c.get(f"{COMFY_URL}/view",
                                  params={"filename": name,
                                          "subfolder": item.get("subfolder", ""),
                                          "type": "output"})
                        if r.status_code == 200 and r.content:
                            tmp = OUTPUT_DIR / name
                            tmp.write_bytes(r.content)
                            logger.info(f"[唱歌] ACE 生成完成 {time.time() - t0:.1f}s → {name}")
                            _release_busy()
                            return str(tmp), ""
            logger.warning(f"[唱歌] ACE 超时 {timeout:.0f}s")
            _release_busy()
            return None, "生成超时"
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[唱歌] ACE 异常：{type(e).__name__}: {e}")
        _release_busy()
        return None, f"{type(e).__name__}: {e}"
