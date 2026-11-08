"""命令行去 AI 味：python tools/humanize_text.py 输入.txt [-o 输出.txt]

只用规则层（离线、零成本）；模型润色走机器人里的 /去AI味。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from plugins.humanize import strip_cliches  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("src", help="输入文件（UTF-8）")
    ap.add_argument("-o", "--out", help="输出文件；不给就打印到屏幕")
    a = ap.parse_args()
    text = Path(a.src).read_text(encoding="utf-8")
    fixed = strip_cliches(text)
    if a.out:
        Path(a.out).write_text(fixed, encoding="utf-8")
        before, after = len(text), len(fixed)
        print(f"写入 {a.out}：{before} → {after} 字（-{before - after}）")
    else:
        print(fixed)


if __name__ == "__main__":
    main()
