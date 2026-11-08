"""长期记忆维护批处理的命令行入口（试运行 / 真跑）。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from plugins import memory_maintain as mm  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真跑（会写盘）")
    a = ap.parse_args()
    print("维护前:", mm.stats())
    row = mm.run(apply=a.apply)
    print("本轮:", row)
    print("维护后:", mm.stats())


if __name__ == "__main__":
    main()
