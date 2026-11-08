"""模拟：内分泌如何改变记忆保留率。

对比三种固化的记忆，在同样的时间流逝下谁活得久：
  A 普通（CORT 0.2 / OXY 0.3 / DA 0.3）
  B 刻骨铭心（OXY 0.9）
  C 极度恐惧（CORT 0.95，is_traumatic）

保留率 = 当前生效权重 / 固化时的权重（即时间衰减的剩余比例）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, r"D:\qq-ai-bot")

from plugins import long_memory as lm


def make(kind: str, hormones: dict, traumatic: bool = False) -> dict:
    now = lm._now_ts()
    return {
        "id": 1,
        "subject": kind,
        "fact": f"{kind} 的一条记忆",
        "time": "10-30",
        "w": lm.MEM_WEIGHT_START,
        "h": 0,
        "lu": now,
        "src": [],
        "hormones": hormones,
        "is_traumatic": traumatic,
    }


BASE_DAY = 86400.0
SCENARIOS = {
    "A 普通": ({"DA": 0.3, "CORT": 0.2, "SER": 0.6, "OXY": 0.3, "MEL": 0.2}, False),
    "B 刻骨铭心": ({"DA": 0.6, "CORT": 0.3, "SER": 0.5, "OXY": 0.9, "MEL": 0.2}, False),
    "C 极度恐惧": ({"DA": 0.5, "CORT": 0.95, "SER": 0.2, "OXY": 0.4, "MEL": 0.3}, True),
}


def retention(it: dict, days: float) -> float:
    """按 days 天后重算 rank，再除以固化时的原始权重。"""
    now = lm._now_ts()
    it2 = dict(it)
    it2["lu"] = now - days * BASE_DAY
    return lm._item_rank(it2, now=now) / lm.MEM_WEIGHT_START


def main() -> None:
    now = lm._now_ts()
    print("=" * 74)
    print("内分泌 × 记忆保留率模拟（保留率 = 当前权重 / 固化时权重）")
    print("=" * 74)

    print(f"\n{'天数':>6}", end="")
    for name in SCENARIOS:
        print(f"{name:>14}", end="")
    print()
    print("-" * 74)
    for days in (0, 1, 3, 7, 14, 30, 90, 180):
        print(f"{days:>6}", end="")
        for name, (h, tr) in SCENARIOS.items():
            it = make(name, h, tr)
            it["lu"] = now - days * BASE_DAY
            r = retention(it, 0) if days == 0 else lm._item_rank(it, now=now) / lm.MEM_WEIGHT_START
            print(f"{r * 100:>13.1f}%", end="")
        print()

    # 半年后对比：恐惧记忆 vs 普通记忆
    it_c = make("C", SCENARIOS["C 极度恐惧"][0], True)
    it_a = make("A", SCENARIOS["A 普通"][0], False)
    it_c["lu"] = now - 180 * BASE_DAY
    it_a["lu"] = now - 180 * BASE_DAY
    r_c = lm._item_rank(it_c, now=now) / lm.MEM_WEIGHT_START
    r_a = lm._item_rank(it_a, now=now) / lm.MEM_WEIGHT_START
    print("-" * 74)
    print(f"180 天后：恐惧记忆 {r_c * 100:.1f}%  vs  普通记忆 {r_a * 100:.1f}%"
          f"   →  碾压 {(r_c / max(r_a, 1e-9)):.0f} 倍")


if __name__ == "__main__":
    main()
