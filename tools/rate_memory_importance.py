"""批量给存量记忆打 importance（1~10），跑完打印分布。

Stanford Generative Agents 的做法：让模型给每条记忆打"戳心度"。

我们原来的 w 是"被提起几次"累加，封顶 3.0 —— 前 5 条全是 3.0 分不出轻重。
这里给现有全部记忆补一次分，之后新记忆在写入时单独打。
"""
import asyncio
import sys

ROOT = r"D:\qq-ai-bot"
sys.path.insert(0, ROOT)

import nonebot
nonebot.init()

from plugins import long_memory as lm
from plugins import memory_importance as mi


async def main() -> None:
    # 收集所有档案里的 fact（跳过已打分的）
    facts: list[str] = []
    seen: set[str] = set()
    for scope, items in lm._archive.items():
        for it in items:
            if not isinstance(it, dict):
                continue
            f = str(it.get("fact") or "").strip()
            if len(f) < 4 or f in seen:
                continue
            seen.add(f)
            facts.append(f)

    print(f"待处理 {len(facts)} 条（缓存里已有的会被跳过）")
    done = await mi.rate_batch(facts)
    print(f"本次新打分 {len(done)} 条")
    print("缓存统计:", mi.stats())

    # 打印分布
    from collections import Counter
    cache = mi._load()
    dist = Counter()
    for f in facts:
        v = cache.get(mi._key(f))
        if v is not None:
            dist[int(v)] += 1
    print("\n分数分布：")
    for k in sorted(dist):
        bar = "█" * min(40, dist[k])
        print(f"  {k:2d} 分: {dist[k]:3d} 条  {bar}")

    # 抽几条样例
    print("\n高分样例（8 分以上）：")
    n = 0
    for f in facts:
        v = cache.get(mi._key(f))
        if v and int(v) >= 8 and n < 8:
            print(f"  {int(v):2d}  {f[:56]}")
            n += 1
    print("\n低分样例（3 分以下）：")
    n = 0
    for f in facts:
        v = cache.get(mi._key(f))
        if v and int(v) <= 3 and n < 8:
            print(f"  {int(v):2d}  {f[:56]}")
            n += 1


asyncio.run(main())
