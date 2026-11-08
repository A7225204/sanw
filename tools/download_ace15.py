import pathlib, time, httpx
REPO = "Comfy-Org/ACE-Step-1.5"
FILES = [
    ("split_files/diffusion_models/acestep_v1.5_turbo.safetensors", r"C:\ComfyUI-aki(2)\ComfyUI-aki-v1.7\ComfyUI\models\diffusion_models\acestep_v1.5_turbo.safetensors"),
    ("split_files/text_encoders/qwen_0.6b_ace15.safetensors",       r"C:\ComfyUI-aki(2)\ComfyUI-aki-v1.7\ComfyUI\models\text_encoders\qwen_0.6b_ace15.safetensors"),
    ("split_files/vae/ace_1.5_vae.safetensors",                     r"C:\ComfyUI-aki(2)\ComfyUI-aki-v1.7\ComfyUI\models\vae\ace_1.5_vae.safetensors"),
]
t_all = time.time()
for remote, dsts in FILES:
    dst = pathlib.Path(dsts)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and dst.stat().st_size > 100 * 1024 * 1024:
        print(f"[跳过] {dst.name}", flush=True); continue
    url = f"https://modelscope.cn/api/v1/models/{REPO}/repo?Revision=master&FilePath={remote}"
    part = dst.with_suffix(dst.suffix + ".part")
    t0 = time.time(); done = last = 0
    with httpx.stream("GET", url, timeout=120, follow_redirects=True) as r:
        total = int(r.headers.get("content-length") or 0)
        print(f"[开始] {dst.name} {total/1e9:.2f}GB", flush=True)
        with part.open("wb") as f:
            for chunk in r.iter_bytes(1024*1024):
                f.write(chunk); done += len(chunk)
                if done - last > 1024**3:
                    last = done
                    print(f"   {done/1e9:.2f}/{total/1e9:.2f}GB {done/max(1e-6,time.time()-t0)/1e6:.0f}MB/s", flush=True)
    part.replace(dst)
    print(f"[完成] {dst.name} {dst.stat().st_size/1e9:.2f}GB {time.time()-t0:.0f}s", flush=True)
print(f"全部完成 {time.time()-t_all:.0f}s", flush=True)
