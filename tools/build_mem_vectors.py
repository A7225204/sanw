"""离线补齐长期记忆的向量缓存（plugins/long_memory.py 的 _local_embed 用）。

跑一次即可；之后 archive_text 会按「语义 0.65 + 权重近因 0.35」重排。
"""
import sys
import time

sys.path.insert(0, r"D:\qq-ai-bot")

from plugins import long_memory as ltm   # noqa: E402


def main() -> None:
    t0 = time.time()
    n = ltm.build_vectors(force="--force" in sys.argv)
    print(f"新建向量 {n} 条，用时 {time.time() - t0:.1f}s，缓存条数 {len(ltm._load_vectors())}")


if __name__ == "__main__":
    main()
