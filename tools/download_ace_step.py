import pathlib, time, httpx
REPO = "Comfy-Org/ACE-Step_ComfyUI_repackaged"
PATH = "all_in_one/ace_step_v1_3.5b.safetensors"
BASE = f"https://modelscope.cn/api/v1/models/{REPO}/repo?Revision=master&FilePath={PATH}"
dst = pathlib.Path(r"C:\ComfyUI-aki(2)\ComfyUI-aki-v1.7\ComfyUI\models\checkpoints\ace_step_v1_3.5b.safetensors")
dst.parent.mkdir(parents=True, exist_ok=True)
part = dst.with_suffix(".safetensors.part")
t0 = time.time()
with httpx.stream("GET", BASE, timeout=120, follow_redirects=True) as r:
    print("HTTP", r.status_code, flush=True)
    total = int(r.headers.get("content-length") or 0)
    print(f"总大小 {total/1e9:.2f}GB", flush=True)
    done = last = 0
    with part.open("wb") as f:
        for chunk in r.iter_bytes(1024*1024):
            f.write(chunk); done += len(chunk)
            if done - last > 1024**3:
                last = done
                print(f"  {done/1e9:.2f}/{total/1e9:.2f}GB {done/max(1e-6,time.time()-t0)/1e6:.1f}MB/s", flush=True)
part.replace(dst)
print(f"完成 {dst.stat().st_size/1e9:.2f}GB 用时 {time.time()-t0:.0f}s → {dst}", flush=True)
