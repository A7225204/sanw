"""调用本地 ComfyUI 的 QwenVL 节点识别图片（免费、本地）。

用法：
  python tools/comfy_qwen_vl.py 图片路径 [--prompt "自定义提示词"]

依赖：ComfyUI 已启动（默认 http://127.0.0.1:8188），且装好
ComfyUI-QwenVL / comfyui-KJNodes；首次运行会自动下载 Qwen3-VL-2B。
"""
from __future__ import annotations

import argparse
import json
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / "comfy_workflows" / "qwen_vl_recognize.json"
COMFY_OUTPUT = Path(r"C:\ComfyUI-aki(2)\ComfyUI-aki-v1.7\ComfyUI\output")


def upload(server: str, image_path: Path) -> str:
    with image_path.open("rb") as f:
        r = httpx.post(
            f"{server}/upload/image",
            data={"overwrite": "true", "type": "input"},
            files={"image": (image_path.name, f, "image/png")},
            timeout=60,
        )
    r.raise_for_status()
    return r.json()["name"]


def run(server: str, image: Path, prompt: str | None, timeout: int = 300) -> str:
    wf = json.loads(WORKFLOW.read_text(encoding="utf-8"))
    name = upload(server, image)
    wf["1"]["inputs"]["image"] = name
    if prompt:
        wf["2"]["inputs"]["custom_prompt"] = prompt
    payload = {"prompt": wf, "client_id": uuid.uuid4().hex}
    r = httpx.post(f"{server}/prompt", json=payload, timeout=30)
    r.raise_for_status()
    prompt_id = r.json()["prompt_id"]

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(2)
        h = httpx.get(f"{server}/history/{prompt_id}", timeout=20).json()
        item = h.get(prompt_id)
        if not item:
            continue
        outputs = item.get("outputs") or {}
        for node in outputs.values():
            for key in ("text", "string", "filename"):
                val = node.get(key)
                if isinstance(val, list) and val:
                    val = val[0]
                if isinstance(val, str):
                    candidate = COMFY_OUTPUT / "qwen_vl" / val
                    if key == "filename" and candidate.exists():
                        return candidate.read_text(encoding="utf-8", errors="ignore").strip()
                    if key in ("text", "string"):
                        return val.strip()
        # SaveStringKJ 可能只写文件：找最新的 txt
        folder = COMFY_OUTPUT / "qwen_vl"
        if folder.exists():
            files = sorted(folder.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
            if files:
                return files[0].read_text(encoding="utf-8", errors="ignore").strip()
    raise TimeoutError("QwenVL 识别超时")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--prompt", default=None)
    ap.add_argument("--server", default="http://127.0.0.1:8188")
    ap.add_argument("--timeout", type=int, default=300)
    args = ap.parse_args()
    print(run(args.server, Path(args.image), args.prompt, args.timeout))


if __name__ == "__main__":
    main()
