"""从 ModelScope 下 bge-m3 到本地目录（比 HF 快得多），供 sentence-transformers 直接加载。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx

DEST = Path(r"D:\models\bge-m3")
REPO = "BAAI/bge-m3"
FILES = [
    "config.json",
    "config_sentence_transformers.json",
    "modules.json",
    "sentence_bert_config.json",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "sentencepiece.bpe.model",
    "1_Pooling/config.json",
    "colbert_linear.pt",
    "pytorch_model.bin",
]


def fetch(path: str, tries: int = 8) -> bool:
    dst = DEST / path
    dst.parent.mkdir(parents=True, exist_ok=True)
    url = (f"https://modelscope.cn/api/v1/models/{REPO}/repo"
           f"?Revision=master&FilePath={path}")
    for attempt in range(1, tries + 1):
        have = dst.stat().st_size if dst.exists() else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            with httpx.stream("GET", url, headers=headers, timeout=120,
                              follow_redirects=True) as r:
                if r.status_code not in (200, 206):
                    print(f"[{path}] HTTP {r.status_code}", flush=True)
                    return False
                total = have + int(r.headers.get("content-length") or 0)
                mode = "ab" if (r.status_code == 206 and have) else "wb"
                done = have if mode == "ab" else 0
                with dst.open(mode) as f:
                    for chunk in r.iter_bytes(1 << 20):
                        f.write(chunk)
                        done += len(chunk)
            if total and dst.stat().st_size >= total * 0.99:
                print(f"[完成] {path} {dst.stat().st_size/1e6:.1f}MB", flush=True)
                return True
            print(f"  {path} 未下完 {dst.stat().st_size/1e6:.0f}/{total/1e6:.0f}MB，续传", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"  {path} 断线 {type(e).__name__}，重试 {attempt}/{tries}", flush=True)
            time.sleep(2)
    return (DEST / path).exists() and (DEST / path).stat().st_size > 1024


def main() -> int:
    ok = 0
    for f in FILES:
        if fetch(f):
            ok += 1
    print(f"\n完成 {ok}/{len(FILES)} → {DEST}")
    return 0 if ok == len(FILES) else 1


if __name__ == "__main__":
    sys.exit(main())
