"""给记忆打「戳心度」：1=纯粹日常，10=极重。

来源：Stanford Generative Agents（Park et al., UIST 2023）。
它让 LLM 直接给每条记忆打 1~10 的 poignancy，prompt 原文：

    On the scale of 1 to 10, where 1 is purely mundane (brushing teeth)
    and 10 is extremely poignant (a break up, college acceptance),
    rate the likely poignancy of the following piece of memory.

实测：「cleaning up the room」→ 2，「asking your crush out on a date」→ 8。

我们原来的 `w`（权重）是「被提起几次」累加，封顶 3.0 ——
结果最常提的琐事权重最高，真正的重大经历只提一次反而低。实测主人 79 条档案里
前 5 条全是 3.0：

    w=3.0  主人主要用3DMAX、CR渲染器和tyflow粒子系统
    w=3.0  主人要求本鱼把日常发QQ空间
    w=3.0  主人修本鱼功能花24亿令牌      ← 这个显然比上面两条重
    w=3.0  主人叫本鱼白饭大王
    w=3.0  主人认可白饭大王外号

打分模型治的就是这个：**重要性由内容决定，不由出现频率决定**。

省钱：**批量打分** —— 一次 prompt 里列 10 条，模型按行输出 10 个数字，
比 Stanford 那种一条一次调用省 10 倍。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "memory" / "importance_cache.json"

BATCH = 10          # 一次调用打几条
DEFAULT_IMP = 5     # 没打过分的按中位数

_RATE_PROMPT = (
    "给下面每条记忆的「戳心程度」打分，1 到 10：\n"
    "1 = 纯粹日常（刷牙、收拾屋子、随便聊两句）\n"
    "10 = 极重（分手、亲人离世、人生转折）\n"
    "中间按分量递增。\n\n"
    "{items}\n\n"
    "只输出数字，一行一个，按顺序，不要解释、不要编号以外的文字。"
)


_CACHE_MEM: dict | None = None
_CACHE_MTIME: float = 0.0


def _load() -> dict:
    """读缓存。带内存副本 + mtime 检查 ——
    检索时会对每条记忆调 get()，不能每次都读 JSON 文件（279 条 × 每条一次太慢）。
    """
    global _CACHE_MEM, _CACHE_MTIME
    try:
        mtime = CACHE.stat().st_mtime
    except Exception:  # noqa: BLE001
        return _CACHE_MEM or {}
    if _CACHE_MEM is not None and mtime == _CACHE_MTIME:
        return _CACHE_MEM
    try:
        d = json.loads(CACHE.read_text(encoding="utf-8"))
        _CACHE_MEM = d if isinstance(d, dict) else {}
        _CACHE_MTIME = mtime
        return _CACHE_MEM
    except Exception:  # noqa: BLE001
        return _CACHE_MEM or {}


def _save(d: dict) -> None:
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(d, ensure_ascii=False, indent=0), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _key(fact: str) -> str:
    return str(fact or "").strip()[:80]


def get(fact: str, default: int = DEFAULT_IMP) -> int:
    """读缓存里的分数，没有返回默认。"""
    v = _load().get(_key(fact))
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _parse(raw: str, n: int) -> list[int] | None:
    """从模型输出里抠出 n 个 1~10 的数字。"""
    nums = [int(x) for x in re.findall(r"(?<!\d)(10|[1-9])(?!\d)", str(raw or ""))]
    if len(nums) < n:
        return None
    return nums[:n]


async def rate_batch(facts: list[str]) -> dict[str, int]:
    """批量打分。返回 {fact: 分}，失败的那批不写缓存。"""
    out: dict[str, int] = {}
    todo = [f for f in facts if str(f or "").strip() and _key(f) not in _load()]
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        body = "\n".join(f"{j + 1}. {str(f)[:70]}" for j, f in enumerate(chunk))
        try:
            from plugins.ai_group import call_ai_raw
            raw = await call_ai_raw(_RATE_PROMPT.format(items=body), "deepseek",
                                    temperature=0.1, max_tokens=120)
        except Exception:  # noqa: BLE001
            continue
        nums = _parse(raw, len(chunk))
        if not nums:
            continue
        cache = _load()
        for f, n in zip(chunk, nums):
            cache[_key(f)] = int(n)
            out[f] = int(n)
        _save(cache)
    return out


def estimate(fact: str) -> int:
    """不打分时的廉价估计（只看内容特征，零成本）。

    用不上模型时（批量跑失败 / 新记忆还没轮到）先给个近似值：
    带数字/专名/时间点的偏高，纯寒暄偏低。
    """
    t = str(fact or "")
    if not t:
        return DEFAULT_IMP
    score = 5.0
    if re.search(r"\d", t):
        score += 1.5                        # 有具体数字
    if re.search(r"(死|走|离开|分手|绝症|住院|手术|失业|破产|去世)", t):
        score += 3.0                        # 生死/重大变故
    if re.search(r"(答应|承诺|保证|约定|发誓)", t):
        score += 1.0
    if re.search(r"(每天|每天都要|习惯|总是|经常)", t):
        score -= 0.5                        # 日常反复的反而轻
    if len(t) <= 8:
        score -= 1.0                        # 太短多半是碎片
    return max(1, min(10, int(round(score))))


def line(fact: str) -> str:
    """调试用一句话。"""
    v = _load().get(_key(fact))
    return f"importance={v if v is not None else '(未打分)'}"


def stats() -> dict:
    d = _load()
    vals = [int(v) for v in d.values() if isinstance(v, int) or str(v).isdigit()]
    if not vals:
        return {"scored": 0}
    return {
        "scored": len(vals),
        "avg": round(sum(vals) / len(vals), 2),
        "min": min(vals),
        "max": max(vals),
    }
