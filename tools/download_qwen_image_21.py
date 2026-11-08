"""下载「图像编辑 (Qwen Image 2.1)」工作流缺失的三个模型。

来源：ModelScope 镜像（Comfy-Org/Qwen-Image-2.1），实测 54MB/s
目标：ComfyUI 的 models 对应子目录
"""
import pathlib
import sys
import time

import httpx

REPO = "Comfy-Org/Qwen-Image-2.1"
BASE = "https://modelscope.cn/api/v1/models/{repo}/repo?Revision=master&FilePath={path}"
MODELS = pathlib.Path(r"C:\ComfyUI-aki(2)\ComfyUI-aki-v1.7\ComfyUI\models")

FILES = [
    ("diffusion_models/qwen_image_2.1_int8_convrot.safetensors", "diffusion_models", 7.26),
    ("text_encoders/qwen3vl_8b_int8_convrot.safetensors", "text_encoders", 9.35),
    ("vae/qwen_image_2.1_vae_bf16.safetensors", "vae", 0.68),
]


def download(remote: str, subdir: str, size_gb: float) -> bool:
    dst = MODELS / subdir / pathlib.Path(remote).name
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and dst.stat().st_size > size_gb * 1e9 * 0.95:
        print(f"[跳过] {dst.name} 已存在 {dst.stat().st_size / 1e9:.2f}GB", flush=True)
        return True
    part = dst.with_suffix(dst.suffix + ".part")
    url = BASE.format(repo=REPO, path=httpx.URL(path=remote).__str__())
    print(f"[开始] {dst.name} 目标 {size_gb}GB\n       {url}", flush=True)
    t0 = time.time()
    try:
        with httpx.stream("GET", url, timeout=120, follow_redirects=True) as r:
            if r.status_code != 200:
                print(f"[失败] {dst.name} HTTP {r.status_code}", flush=True)
                return False
            total = int(r.headers.get("content-length") or 0)
            done = 0
            last = 0
            with part.open("wb") as f:
                for chunk in r.iter_bytes(1024 * 1024):
                    f.write(chunk)
                    done += len(chunk)
                    if done - last > 200 * 1024 * 1024:
                        last = done
                        sp = done / max(1e-6, time.time() - t0) / 1e6
                        pct = (done / total * 100) if total else 0
                        print(f"  {dst.name}: {done / 1e9:.2f}/{total / 1e9:.2f}GB "
                              f"({pct:.1f}%) {sp:.1f}MB/s", flush=True)
        if part.stat().st_size < size_gb * 1e9 * 0.9:
            print(f"[可疑] {dst.name} 只有 {part.stat().st_size / 1e9:.2f}GB", flush=True)
        part.replace(dst)
        print(f"[完成] {dst.name} {dst.stat().st_size / 1e9:.2f}GB "
              f"用时 {time.time() - t0:.0f}s", flush=True)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[异常] {dst.name}: {type(e).__name__}: {str(e)[:150]}", flush=True)
        return False


def main() -> int:
    ok = 0
    for remote, subdir, size in FILES:
        if download(remote, subdir, size):
            ok += 1
    print(f"\n全部结束：成功 {ok}/{len(FILES)}", flush=True)
    return 0 if ok == len(FILES) else 1


if __name__ == "__main__":
    sys.exit(main())
