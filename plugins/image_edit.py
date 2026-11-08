"""用「图像编辑 (Qwen Image 2.1)」工作流改图。

对应 ComfyUI 里用户那个「图像编辑」工作流的子图，节点关系照搬：

    UNETLoader(qwen_image_2.1_int8_convrot)
      → QwenImage21Cache
      → KSampler ← Condition 来自 TextEncodeQwenImage21（带参考图 + VAE）
                  ← Latent 来自 ComfySwitch（有参考图时用图 latent，纯文生图用空 latent）
      → VAEDecode → SaveImage

对外只有一个入口：edit(image_bytes, instruction) -> bytes | None
"""
from __future__ import annotations

import json
import random
import time
from pathlib import Path

import httpx

from nonebot.log import logger

COMFY_URL = "http://127.0.0.1:8188"
ROOT = Path(__file__).resolve().parent.parent
INPUT_DIR = Path(r"C:\ComfyUI-aki(2)\ComfyUI-aki-v1.7\ComfyUI\input")

UNET = "qwen_image_2.1_int8_convrot.safetensors"
CLIP = "qwen3vl_8b_int8_convrot.safetensors"
VAE = "qwen_image_2.1_vae_bf16.safetensors"


def _workflow(image_name: str, instruction: str, resolution: int = 1024) -> dict:
    return {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": UNET, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": CLIP, "type": "qwen_image", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": VAE}},
        "4": {"class_type": "QwenImage21Cache",
              "inputs": {"model": ["1", 0], "device": "auto", "dtype": "default"}},
        "5": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "6": {"class_type": "TextEncodeQwenImage21",
              "inputs": {"clip": ["2", 0], "vae": ["3", 0], "prompt": instruction,
                         "negative_prompt": "模糊, 变形, 低质量, 水印",
                         "resolution": resolution, "images.image_1": ["5", 0]}},
        "7": {"class_type": "EmptyLatentImage",
              "inputs": {"width": resolution, "height": resolution, "batch_size": 1}},
        "8": {"class_type": "ComfySwitchNode",
              "inputs": {"switch": False, "on_false": ["6", 2], "on_true": ["7", 0]}},
        "9": {"class_type": "KSampler",
              "inputs": {"model": ["4", 0], "positive": ["6", 0], "negative": ["6", 1],
                         "latent_image": ["8", 0], "seed": random.randint(1, 2 ** 31 - 1),
                         "steps": 25, "cfg": 1.0, "sampler_name": "euler",
                         "scheduler": "simple", "denoise": 1.0}},
        "10": {"class_type": "VAEDecode", "inputs": {"samples": ["9", 0], "vae": ["3", 0]}},
        "11": {"class_type": "SaveImage",
               "inputs": {"images": ["10", 0], "filename_prefix": "feiyu_edit"}},
    }


def edit(image_bytes: bytes, instruction: str, timeout: float = 420.0) -> bytes | None:
    """改一张图：把图传给 ComfyUI，跑 Qwen Image 2.1 编辑，返回结果字节。"""
    if not image_bytes:
        return None
    instruction = (instruction or "").strip()[:600]
    if not instruction:
        return None
    from plugins import comfy_boot
    if not comfy_boot.ensure(tag="改图"):
        logger.warning("[改图] ComfyUI 没起来，跳过")
        return None
    name = f"edit_in_{int(time.time())}.png"
    try:
        INPUT_DIR.mkdir(parents=True, exist_ok=True)
        (INPUT_DIR / name).write_bytes(image_bytes)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[改图] 写输入图失败：{e}")
        return None
    t0 = time.time()
    try:
        with httpx.Client(timeout=60) as c:
            q = c.post(f"{COMFY_URL}/prompt",
                       json={"prompt": _workflow(name, instruction)})
            if q.status_code != 200:
                logger.warning(f"[改图] 工作流被拒：{q.text[:200]}")
                return None
            pid = (q.json() or {}).get("prompt_id", "")
            deadline = time.time() + timeout
            while time.time() < deadline:
                time.sleep(1.0)
                h = c.get(f"{COMFY_URL}/history/{pid}").json().get(pid) or {}
                st = (h.get("status") or {}).get("status_str")
                if st == "error":
                    logger.warning(f"[改图] 执行出错：{str(h.get('status'))[:200]}")
                    return None
                for node in (h.get("outputs") or {}).values():
                    for img in node.get("images") or []:
                        fn = img.get("filename")
                        if not fn:
                            continue
                        r = c.get(f"{COMFY_URL}/view",
                                  params={"filename": fn,
                                          "subfolder": img.get("subfolder", ""),
                                          "type": "output"})
                        if r.status_code == 200 and r.content:
                            logger.info(f"[改图] 完成 {time.time() - t0:.1f}s（{fn}）")
                            return r.content
            logger.warning(f"[改图] 超时 {timeout:.0f}s")
            return None
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[改图] 异常：{type(e).__name__}: {e}")
        return None
