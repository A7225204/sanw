import pathlib, time, httpx
REPO = "Comfy-Org/ACE-Step-1.5"
remote = "split_files/text_encoders/qwen_1.7b_ace15.safetensors"
dst = pathlib.Path(r"C:\ComfyUI-aki(2)\ComfyUI-aki-v1.7\ComfyUI\models\text_encoders\qwen_1.7b_ace15.safetensors")
dst.parent.mkdir(parents=True, exist_ok=True)
if dst.exists() and dst.stat().st_size > 100*1024*1024:
    print("[跳过] 已存在", dst.stat().st_size/1e9, "GB")
else:
    url = f"https://modelscope.cn/api/v1/models/{REPO}/repo?Revision=master&FilePath={remote}"
    part = dst.with_suffix(".safetensors.part")
    t0=time.time(); done=last=0
    with httpx.stream("GET", url, timeout=120, follow_redirects=True) as r:
        total = int(r.headers.get("content-length") or 0)
        print(f"[开始] {total/1e9:.2f}GB", flush=True)
        with part.open("wb") as f:
            for chunk in r.iter_bytes(1024*1024):
                f.write(chunk); done += len(chunk)
                if done - last > 1024**3:
                    last = done
                    print(f"   {done/1e9:.2f}/{total/1e9:.2f}GB {done/max(1e-6,time.time()-t0)/1e6:.0f}MB/s", flush=True)
    part.replace(dst)
    print(f"[完成] {dst.stat().st_size/1e9:.2f}GB {time.time()-t0:.0f}s", flush=True)
