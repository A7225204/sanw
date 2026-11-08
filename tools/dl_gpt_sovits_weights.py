"""下载 GPT-SoVITS v2 预训练权重（国内直连 HF 可达时跑）。

下载目标（相对 GPT_SoVITS 目录）：
  pretrained_models/chinese-roberta-wwm-ext-large/{config.json,pytorch_model.bin,tokenizer.json}
  pretrained_models/chinese-hubert-base/{config.json,preprocessor_config.json,pytorch_model.bin}
  pretrained_models/s1bert25hz-5kh-longer-epoch=12-step=369668.ckpt
  pretrained_models/s2G2333k.pth
  pretrained_models/s2D2333k.pth
"""
import sys
import time
from pathlib import Path

import httpx

REPO = "lj1995/GPT-SoVITS"
DEST = Path(r"D:\GPT-SoVITS\GPT-SoVITS\GPT_SoVITS\pretrained_models")

FILES = [
    ("chinese-roberta-wwm-ext-large/config.json", "chinese-roberta-wwm-ext-large/config.json"),
    ("chinese-roberta-wwm-ext-large/pytorch_model.bin", "chinese-roberta-wwm-ext-large/pytorch_model.bin"),
    ("chinese-roberta-wwm-ext-large/tokenizer.json", "chinese-roberta-wwm-ext-large/tokenizer.json"),
    ("chinese-hubert-base/config.json", "chinese-hubert-base/config.json"),
    ("chinese-hubert-base/preprocessor_config.json", "chinese-hubert-base/preprocessor_config.json"),
    ("chinese-hubert-base/pytorch_model.bin", "chinese-hubert-base/pytorch_model.bin"),
    ("gsv-v2final-pretrained/s1bert25hz-5kh-longer-epoch=12-step=369668.ckpt",
     "s1bert25hz-5kh-longer-epoch=12-step=369668.ckpt"),
    ("gsv-v2final-pretrained/s2G2333k.pth", "s2G2333k.pth"),
    ("gsv-v2final-pretrained/s2D2333k.pth", "s2D2333k.pth"),
]


def fetch(remote: str, local: str) -> bool:
    dst = DEST / local
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and dst.stat().st_size > 1024:
        print(f"[跳过] {local} {dst.stat().st_size/1e6:.1f}MB", flush=True)
        return True
    url = f"https://huggingface.co/{REPO}/resolve/main/{remote}"
    t0 = time.time()
    try:
        with httpx.stream("GET", url, timeout=120, follow_redirects=True) as r:
            if r.status_code != 200:
                print(f"[失败] {local} HTTP {r.status_code}", flush=True)
                return False
            total = int(r.headers.get("content-length") or 0)
            done = 0
            last = 0
            with dst.open("wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
                    done += len(chunk)
                    if done - last > 100 * 1024 * 1024:
                        last = done
                        print(f"  {local}: {done/1e6:.0f}/{total/1e6:.0f}MB", flush=True)
        print(f"[完成] {local} {dst.stat().st_size/1e6:.1f}MB {time.time()-t0:.0f}s", flush=True)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[异常] {local}: {type(e).__name__}: {str(e)[:100]}", flush=True)
        return False


def main() -> int:
    DEST.mkdir(parents=True, exist_ok=True)
    ok = 0
    for remote, local in FILES:
        if fetch(remote, local):
            ok += 1
    print(f"\n完成 {ok}/{len(FILES)}")
    return 0 if ok == len(FILES) else 1


if __name__ == "__main__":
    sys.exit(main())
