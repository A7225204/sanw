"""执念池（2026-11-05，借 digital-life-core 的"未解执念 + 张力 + 回声消除"）。

治的是这几天反复出现的同一个毛病：她自己起一个话头（"那个欢迎还是自动发的吧…"），
下一轮读到自己说过，又接着展开，越说越具体，停不下来。

以前的做法是在历史里删重复句子 —— 治标。这里改成**有生命周期的执念**：

    提起 → 入池，张力 +0.6
    主人正面回应 → 张力 -2.0（很快闭合）
    连续没人接 → 每轮 -0.4
    超过 6 小时 → 每小时再 -0.5
    张力 ≤ 0 → 闭合，10 分钟内同话题的提起一律作废（回声消除）

只把 **open 且张力最高** 的 1~2 条写进 prompt，作为"你心里挂着的事" ——
而不是让它在历史里无限繁殖。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POOL_FILE = ROOT / "memory" / "执念池.json"

TENSION_IN = 0.6          # 每次提起加多少
TENSION_ANSWERED = 2.0    # 主人正面回应扣多少
TENSION_IGNORED = 0.4     # 每轮没人接扣多少
TENSION_AGE_PER_HOUR = 0.5
ECHO_WINDOW = 600         # 闭合后的回声消除窗口（秒）
MAX_OPEN = 6              # 池子里最多留几条 open

_STOP = {
    "本鱼", "主人", "那个", "这个", "就是", "好像", "有点", "还是", "不过",
    "话说", "回来", "一下", "什么", "怎么", "因为", "所以", "但是", "而且",
    "自己", "现在", "时候", "这样", "那样", "真的", "已经", "可以", "没有",
    "对了", "起来", "顺带", "另外", "其实", "反正",
}


def _shingles(text: str) -> set[str]:
    t = re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9]", "", text or "")
    return {t[i:i + 2] for i in range(max(0, len(t) - 1))} - _STOP


def _shared(a: str, b: str) -> int:
    return len(_shingles(a) & _shingles(b))


def _load() -> dict:
    try:
        d = json.loads(POOL_FILE.read_text(encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("items"), list):
            d.setdefault("seq", 0)
            return d
    except Exception:  # noqa: BLE001
        pass
    return {"items": [], "seq": 0}


def _save(d: dict) -> None:
    try:
        POOL_FILE.parent.mkdir(parents=True, exist_ok=True)
        POOL_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _now() -> float:
    return time.time()


def _short(text: str, n: int = 40) -> str:
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    return t[:n]


def _find(d: dict, text: str, min_shared: int = 4):
    """按 2 字词重叠找同一条执念。"""
    best, best_n = None, 0
    for it in d["items"]:
        n = _shared(text, it.get("text") or "")
        if n > best_n:
            best, best_n = it, n
    return (best, best_n) if best_n >= min_shared else (None, 0)


def touch(text: str) -> dict:
    """记一次「提起」。返回 {action: new|bump|echo|skip, item}。"""
    t = _short(text)
    if len(t) < 6:
        return {"action": "skip"}
    d = _load()
    it, n = _find(d, t)
    now = _now()
    if it is None:
        d["seq"] = int(d.get("seq") or 0) + 1
        item = {"id": d["seq"], "text": t, "tension": TENSION_IN,
                "born": now, "last": now, "status": "open", "closed_at": None}
        d["items"].append(item)
        d["items"] = d["items"][-40:]
        _save(d)
        return {"action": "new", "item": item}
    # 已闭合：回声窗口内直接作废，窗口外复活原条目（不开双胞胎）
    if it.get("status") == "closed":
        if now - float(it.get("closed_at") or 0) < ECHO_WINDOW:
            return {"action": "echo", "item": it}
        it["status"] = "open"
        it["closed_at"] = None
        it["tension"] = TENSION_IN
        it["last"] = now
        _save(d)
        return {"action": "revive", "item": it}
    it["tension"] = round(float(it.get("tension") or 0) + TENSION_IN, 2)
    it["last"] = now
    _save(d)
    return {"action": "bump", "item": it}


def answered(user_text: str) -> list[dict]:
    """主人这句话正面回应了哪些 open 执念 → 大幅降张力，可能直接闭合。"""
    t = str(user_text or "")
    if len(t) < 3:
        return []
    d = _load()
    hit = []
    now = _now()
    changed = False
    for it in d["items"]:
        if it.get("status") != "open":
            continue
        # 主人随口一句「欢迎那个我改了」也算回应 —— 阈值降到 2（误闭合的代价只是不再提，很小）
        if _shared(t, it.get("text") or "") >= 2:
            it["tension"] = round(float(it.get("tension") or 0) - TENSION_ANSWERED, 2)
            it["last"] = now
            changed = True
            if it["tension"] <= 0:
                it["status"] = "closed"
                it["closed_at"] = now
            hit.append(it)
    if changed:
        _save(d)
    return hit


def decay(sleeping: bool = False) -> None:
    """每轮调一次：没人接的执念慢慢淡掉，太老的强制老死。

    sleeping=True（她在深睡）时衰减 ×2.5 —— 借 digital-life-core 的「深睡期间
    执念张力暂停增长」：睡觉本来就是想开的过程，醒来不该比睡前更钻牛角尖。
    """
    d = _load()
    now = _now()
    mult = 2.5 if sleeping else 1.0
    changed = False
    for it in d["items"]:
        if it.get("status") != "open":
            continue
        age_h = max(0.0, now - float(it.get("last") or now)) / 3600.0
        drop = (TENSION_IGNORED + age_h * TENSION_AGE_PER_HOUR) * mult
        it["tension"] = round(float(it.get("tension") or 0) - drop, 2)
        # 2026-11-08：**age_h 的参照必须更新**，否则每次 decay 都按"距最后一次提起"的年龄
        # 重复扣一遍年龄部分 —— 消息密集时张力会成倍往下掉。
        it["last"] = now
        if it["tension"] < 0:
            it["tension"] = 0.0
        changed = True
        if it["tension"] <= 0:
            it["status"] = "closed"
            it["closed_at"] = now
    if changed:
        _save(d)


def active(n: int = 2, min_tension: float = 1.0) -> str:
    """取张力最高的几条 open 执念，拼成能塞进 prompt 的一段。"""
    d = _load()
    rows = [it for it in d["items"]
            if it.get("status") == "open" and float(it.get("tension") or 0) >= min_tension]
    if not rows:
        return ""
    rows.sort(key=lambda x: -float(x.get("tension") or 0))
    lines = [f"- {_short(it.get('text'), 46)}" for it in rows[:n]]
    return ("【你心里还挂着的事】\n" + "\n".join(lines) +
            "\n（这些是你自己放不下的话题。可以自然提一次，但别追问、别逼主人回应；"
            "主人明显不想聊就自己收住。）")


def stats() -> dict:
    d = _load()
    op = [it for it in d["items"] if it.get("status") == "open"]
    return {"open": len(op), "closed": len(d["items"]) - len(op),
            "top": [(_short(it.get("text"), 30), it.get("tension")) for it in
                    sorted(op, key=lambda x: -float(x.get("tension") or 0))[:3]]}

def is_closed(text: str, min_shared: int = 3) -> bool:
    """这句话说的是不是一条**已经闭合**的执念。

    2026-11-05：实测她能把一个已经 closed 的话题再提一遍——
    closed 只挡住了「注入 prompt」，挡不住「她自己从历史里读到又接着说」。
    这里给去重层用：闭合的话题，出现就该删。
    """
    d = _load()
    t = str(text or "")
    if len(t) < 4:
        return False
    for it in d.get("items") or []:
        if it.get("status") != "closed":
            continue
        if _shared(t, it.get("text") or "") >= min_shared:
            return True
    return False

def related(text: str, min_shared: int = 3) -> dict | None:
    """执念池里有没有跟这句话同话题的条目（开/关都算）。

    2026-11-06：念头池和执念池原来各管各的，同一件事会被记两遍 ——
    执念池已经 closed（张力 -5.94），念头池里还是 open，于是她一边"放下了"一边还念叨。
    念头池新增前用这个查一下，有就不要再记。
    """
    d = _load()
    it, n = _find(d, text, min_shared=min_shared)
    if it:
        it = dict(it)
        it["_shared"] = n
        return it
    return None
