"""用 ComfyUI 原生 TrainLoraNode 训练鲸鱼娘 LoRA。

流程：
    LoadImageDataSetFromFolder(feiyu_lora)     ← input 下的数据集目录
    MakeTrainingDataset(images, vae, clip, texts)
      → LATENT + CONDITIONING
    TrainLoraNode(UNET z_image_turbo, latents, positive, ...)
      → LORA_MODEL
    SaveLoRA(prefix="feiyu_girl", steps=[...])  → 存到 models/loras

用法：
    python tools\train_lora.py --steps 400 --rank 16
"""
from __future__ import annotations

import argparse
import json
import time

import httpx

BASE = "http://127.0.0.1:8188"
DATASET = "feiyu_lora"
TRIGGER = "feiyu_girl"


def build(steps: int, rank: int, lr: float, batch: int) -> dict:
    return {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": "z_image_turbo_bf16.safetensors",
                         "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": "qwen_3_4b.safetensors", "type": "lumina2",
                         "device": "default"}},
        "3": {"class_type": "VAELoader",
              "inputs": {"vae_name": "diffusion_pytorch_model.safetensors"}},
        "4": {"class_type": "LoadImageDataSetFromFolder", "inputs": {"folder": DATASET}},
        "5": {"class_type": "LoadImageTextDataSetFromFolder", "inputs": {"folder": DATASET}},
        "6": {"class_type": "MakeTrainingDataset",
              "inputs": {"images": ["4", 0], "vae": ["3", 0], "clip": ["2", 0],
                         "texts": ["5", 1]}},
        "7": {"class_type": "TrainLoraNode",
              "inputs": {"model": ["1", 0], "latents": ["6", 0],
                         "positive": ["6", 1], "batch_size": batch,
                         "grad_accumulation_steps": 1, "steps": steps,
                         "learning_rate": lr, "rank": rank, "optimizer": "AdamW",
                         "loss_function": "MSE", "seed": 42,
                         "training_dtype": "bf16", "lora_dtype": "bf16",
                         "quantized_backward": False, "algorithm": "LoRA",
                         "gradient_checkpointing": True, "checkpoint_depth": 1,
                         "offloading": True, "existing_lora": "[None]",
                         "bucket_mode": False, "bypass_mode": False}},
        "8": {"class_type": "SaveLoRA",
              "inputs": {"lora": ["7", 0], "prefix": TRIGGER,
                         "steps": ["7", 2]}},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", type=int, default=1500)
    ap.add_argument("--rank", type=int, default=16)
    ap.add_argument("--lr", type=float, default=4e-4)
    ap.add_argument("--batch", type=int, default=1)
    args = ap.parse_args()

    with httpx.Client(timeout=120) as c:
        r = c.post(f"{BASE}/prompt", json={"prompt": build(args.steps, args.rank, args.lr, args.batch)})
        if r.status_code != 200:
            print("[训练] 提交失败:", r.text[:400])
            return 1
        pid = r.json()["prompt_id"]
        print(f"[训练] 已提交 prompt_id={pid} steps={args.steps} rank={args.rank} lr={args.lr}", flush=True)
        t0 = time.time()
        last_report = 0.0
        while True:
            time.sleep(10)
            h = c.get(f"{BASE}/history/{pid}").json().get(pid) or {}
            st = (h.get("status") or {}).get("status_str")
            if st == "error":
                msgs = (h.get("status") or {}).get("messages") or []
                for m in msgs:
                    if m[0] == "execution_error":
                        print("[训练] 出错:", m[1].get("node_type"),
                              str(m[1].get("exception_message"))[:300], flush=True)
                return 1
            if h.get("outputs"):
                print("[训练] 完成:", json.dumps(h["outputs"], ensure_ascii=False)[:400], flush=True)
                print(f"[训练] 总耗时 {time.time() - t0:.0f}s", flush=True)
                return 0
            # 进度：每 2 分钟报一次
            if time.time() - last_report > 120:
                last_report = time.time()
                q = c.get(f"{BASE}/queue").json()
                running = len(q.get("queue_running") or [])
                print(f"[训练] 进行中 {time.time() - t0:.0f}s（队列 running={running}）", flush=True)
            if time.time() - t0 > 6 * 3600:
                print("[训练] 超过 6 小时，放弃等待", flush=True)
                return 1


if __name__ == "__main__":
    raise SystemExit(main())
