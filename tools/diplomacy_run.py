"""跑一局七国外交：7 个座位全用大肥鱼（或指定模型）当玩家。

素材来自 chen-006/meow-ai-arena 第 4 集，裁判是它的 `arena.py`（本地 HTTP 服务）。

**为什么这么跑**：那个仓库让 7 个模型各坐一国互相对打。
我们这边只有一个大肥鱼 —— 让她同时坐 7 个国家，
**每个座位都带同一套三我状态**，就能看「同样的性格在不同位置上会怎么打」。

用法：
    python tools/diplomacy_run.py --rounds 8
    python tools/diplomacy_run.py --powers ITALY,FRANCE   # 只让这两个国家动，其余挂机
"""
from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DJ = ROOT / "data" / "diplomacy"
sys.path.insert(0, str(DJ))

import nonebot  # noqa: E402

try:
    nonebot.get_driver()
except Exception:  # noqa: BLE001
    nonebot.init()

from plugins import diplomacy_player as dp  # noqa: E402

POWERS = ['AUSTRIA', 'ENGLAND', 'FRANCE', 'GERMANY', 'ITALY', 'RUSSIA', 'TURKEY']


def start_referee(state_file: Path, port: int) -> subprocess.Popen:
    """起裁判进程。"""
    p = subprocess.Popen(
        [sys.executable, str(DJ / "arena.py"), "--state", str(state_file), "--port", str(port)],
        cwd=str(DJ), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace")
    # 等它打出 "裁判：http://..." 那行
    for _ in range(40):
        line = p.stdout.readline() if p.stdout else ""
        if "裁判" in line or "http" in line:
            break
        if p.poll() is not None:
            break
        time.sleep(0.15)
    return p


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=6, help="最多跑多少轮（每轮 = 所有国家各行动一次）")
    ap.add_argument("--port", type=int, default=18790)
    ap.add_argument("--powers", default="", help="逗号分隔；留空 = 七国全上")
    ap.add_argument("--tag", default="", help="存档名后缀")
    args = ap.parse_args()

    tag = args.tag or time.strftime("%H%M%S")
    state = DJ / f"state_{tag}.json"
    if state.exists():
        print(f"存档已存在：{state}")
        return

    # ① 创建对局
    from arena import Arena  # noqa: E402
    Arena.create(state, auto_settle=True)
    data = json.loads(state.read_text(encoding="utf-8"))
    tokens = data["tokens"]
    print(f"对局已建：{state.name}（7 国，auto_settle=True）")

    # ② 起裁判
    proc = start_referee(state, args.port)
    base = f"http://127.0.0.1:{args.port}"
    time.sleep(1.0)
    print(f"裁判：{base}")

    want = [x.strip().upper() for x in args.powers.split(",") if x.strip()] or POWERS
    players = {p: dp.Player(base, tokens[p], p) for p in want}

    async def loop() -> None:
        for rnd in range(1, args.rounds + 1):
            print(f"\n========== 第 {rnd} 轮 ==========")
            for power, pl in players.items():
                brief = pl.brief()
                if not brief:
                    print(f"  {power}: 拿不到局面，跳过")
                    continue
                if "已结束" in brief:
                    print(f"  {power}: 对局已结束")
                    return
                head = brief.splitlines()[0][:60] if brief else ""
                print(f"\n  [{power}] {head}")
                acts = await dp.decide(brief, power)
                if not acts:
                    print("     （没决定出动作）")
                    continue
                for a in acts:
                    if a["kind"] == "send":
                        print(f"     → 发给 {a['to']}：{a['text'][:56]}")
                    elif a["kind"] == "orders":
                        print(f"     → 军令：{' | '.join(a['orders'])[:70]}")
                    else:
                        print(f"     → {a['kind']}")
                res = pl.act(acts)
                bad = [x for x in res if not (x.get("r") or {}).get("ok", True)]
                if bad:
                    print(f"     !! {len(bad)} 个动作失败")
            await asyncio.sleep(0.5)

    try:
        asyncio.run(loop())
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()
        print(f"\n对局存档：{state}")
        print(f"查看：python -X utf8 tools/diplomacy_run.py --inspect {state.name}")


if __name__ == "__main__":
    main()
