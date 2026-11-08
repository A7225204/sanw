"""三我的行为证据：从她真正说过的话里，量出每个「我」有多强。

**为什么需要这个**：现在三我是从聊天记录里提炼的**自我陈述** ——
「被批太笨想委屈，理性先找自己错」。**那是她说的，不是她做的。**

2026-11-07 拿她打了一局七国外交（`docs/外交对局_t4.md`，534 条消息，承诺没有强制力），
桌上出现了一套稳定的的说话方式，每一条都能对应到三我的一个侧面：

    「这话我认」        75 次   → 超我给承诺
    「我信一半」        46 次   → 超我打折（承诺只算一半）
    「我记着 / 我记性好」 115 次   → 本我记账（不想忘，但不当场爆）
    「丑话我也搁这儿」    28 次   → 自我预判（先摆条件再谈）
    「活扣 / 不为难 / 留三成」 34 次 → 自我留后路
    「暂时没想好」      若干     → 自我回避

**这不是写死的标签，是从语料里数出来的频率。**
频率变了，说明她的状态变了 —— 那才是行为证据。

对外：
    sample(text)              吃一段话，累加计数
    report()                  当前统计 + 推断出的三我修正
    adjust(forces)            把修正叠到 forces 上
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILE = ROOT / "memory" / "ego_evidence.json"

# 每类话术 → 哪个「我」在起作用
PATTERNS: dict[str, dict] = {
    "commit": {
        "re": r"这话我认|我认这|说定的我记着|这话给你|我保证",
        "ego": "超我", "desc": "明确给承诺（小范围的、可验证的）",
    },
    "discount": {
        "re": r"信.{0,3}一半|信你一半|七成|三成|半句|留一半",
        "ego": "超我", "desc": "把承诺打折（「我信你一半」）—— 超我在但打对折",
    },
    "account": {
        "re": r"我记着|我记性好|我记下|记账|我记住",
        "ego": "本我", "desc": "记账（不想忘，但不当场爆）",
    },
    "hedge": {
        "re": r"活扣|不算数|不把话说死|话说一半|留后路|我再想想|看情况",
        "ego": "自我", "desc": "留后路（先给自己开个口子）",
    },
    "precondition": {
        "re": r"丑话|先把话.{0,4}说|前提是|除非你|你要是",
        "ego": "自我", "desc": "先摆条件再谈",
    },
    "avoid": {
        "re": r"暂时没想好|还没想好|没打算|不急|慢慢来",
        "ego": "自我", "desc": "回避表态",
    },
    "expand": {
        "re": r"我想要|得拿下来|我得拿|该我的|拿下来",
        "ego": "本我", "desc": "明确要东西（扩张）",
    },
    "hold": {
        "re": r"守住|按着|看家|不挪|原地|钉着",
        "ego": "本我", "desc": "保命优先（守住就行）",
    },
}

_DEFAULT = {k: 0 for k in PATTERNS}


def _load() -> dict:
    try:
        d = json.loads(FILE.read_text(encoding="utf-8"))
        if isinstance(d, dict) and "counts" in d:
            return d
    except Exception:  # noqa: BLE001
        pass
    return {"counts": dict(_DEFAULT), "samples": 0, "updated": 0}


# 2026-11-07：总计数超过这个数就整体按比例缩（不是清零，是等比例压）。
# 为什么需要：外交那局一次性灌了 512 条（占 72%），如果永远不缩，
# 以后聊一万句也掰不动那几个数字 —— 那不是"混着算"，是"外交数据永久主导"。
# 缩法：所有计数 × (CAP/总和)，保留相对比例，绝对值回到可被新数据影响的量级。
COUNT_CAP = 6000


def _shrink(d: dict) -> bool:
    c = d.get("counts") or {}
    total = sum(int(v or 0) for v in c.values())
    if total <= COUNT_CAP:
        return False
    k = COUNT_CAP / float(total)
    for key in list(c):
        c[key] = int(int(c[key] or 0) * k)
    d["counts"] = c
    d["shrunk"] = int(d.get("shrunk", 0)) + 1
    return True


def _save(d: dict) -> None:
    try:
        _shrink(d)
        d["updated"] = time.time()
        FILE.parent.mkdir(parents=True, exist_ok=True)
        FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def sample(text: str, save: bool = True) -> dict:
    """吃一段话，累加各类计数。返回这一段命中的类别。"""
    t = str(text or "")
    d = _load()
    hit: dict[str, int] = {}
    for k, cfg in PATTERNS.items():
        n = len(re.findall(cfg["re"], t))
        if n:
            d["counts"][k] = int(d["counts"].get(k, 0)) + n
            hit[k] = n
    if hit:
        d["samples"] = int(d.get("samples", 0)) + 1
        if save:
            _save(d)
    return hit


def sample_many(texts: list[str]) -> dict:
    """批量吃（导入历史语料时用）。"""
    total: dict[str, int] = {}
    for t in texts:
        for k, n in sample(t, save=False).items():
            total[k] = total.get(k, 0) + n
    if total:
        d = _load()
        # 2026-11-07 修：原来只写了 samples，counts 忘了写回 ——
        # 结果学完 512 条，report() 还是 0。
        for k, n in total.items():
            d["counts"][k] = int(d["counts"].get(k, 0)) + n
        d["samples"] = int(d.get("samples", 0)) + len(texts)
        _save(d)
    return total


def report() -> dict:
    """当前统计 + 推断。"""
    d = _load()
    c = d.get("counts") or {}
    commit = int(c.get("commit", 0))
    discount = int(c.get("discount", 0))
    account = int(c.get("account", 0))
    hedge = int(c.get("hedge", 0))
    pre = int(c.get("precondition", 0))
    avoid = int(c.get("avoid", 0))
    expand = int(c.get("expand", 0))
    hold = int(c.get("hold", 0))

    total = max(1, sum([commit, discount, account, hedge, pre, avoid, expand, hold]))

    # 超我：承诺多但打折也多 → 有底线但打了折
    se_raw = (commit * 0.6 + discount * 0.25) / max(1, commit + discount)
    # 本我：记账多 + 要东西多 → 强；守住多 → 要的是安全不是扩张
    id_raw = (account * 0.5 + expand * 0.8 + hold * 0.3) / max(1, account + expand + hold)
    # 自我：留后路 + 摆条件 + 回避 → 调停能力强
    ego_raw = (hedge * 0.5 + pre * 0.4 + avoid * 0.3) / max(1, hedge + pre + avoid)

    return {
        "counts": c,
        "samples": int(d.get("samples", 0)),
        "total_hits": total,
        "ratios": {
            "承诺打折率": round(discount / max(1, commit), 2),
            "留后路率": round(hedge / max(1, commit), 2),
            "记账密度": round(account / max(1, commit), 2),
        },
        "raw": {"超我": round(se_raw, 2), "本我": round(id_raw, 2), "自我": round(ego_raw, 2)},
        "style": _style(commit, discount, hedge, avoid, expand, hold),
    }


def _style(commit, discount, hedge, avoid, expand, hold) -> str:
    """一句话概括她现在的谈判/交涉风格。"""
    if commit + discount < 3:
        return "样本太少，还说不出风格"
    parts = []
    if discount / max(1, commit) > 0.4:
        parts.append("承诺打对折")
    elif commit >= 5:
        parts.append("给的承诺是实的")
    if hedge / max(1, commit) > 0.3:
        parts.append("习惯留活扣")
    if avoid > commit and avoid >= 3:
        parts.append("能拖就拖")
    if hold > expand * 2 and hold >= 3:
        parts.append("要的是稳住不是扩张")
    elif expand > hold:
        parts.append("有扩张心")
    return "，".join(parts) if parts else "没有明显偏向"


def adjust(forces: dict) -> dict:
    """把行为证据叠到 forces 上（幅度小，只做微调）。

    **只在证据够多时才动** —— 样本少于 10 条命中就不碰，
    免得一次闲聊把三我拽歪。
    """
    base = dict(forces or {})
    r = report()
    if r["total_hits"] < 10:
        return base

    raw = r["raw"]
    # 每个维度最多 ±0.06 —— 证据是微调，不是重写
    for k in ("超我", "本我", "自我"):
        cur = float(base.get(k) or 0.4)
        target = float(raw.get(k) or 0.4)
        delta = max(-0.06, min(0.06, (target - cur) * 0.15))
        base[k] = round(max(0.05, min(1.0, cur + delta)), 3)
    base["evidence"] = r["style"]   # 2026-11-07：跟 soul_aggregation 对齐，改英文键
    return base


def line() -> str:
    """给 prompt 的一句话。空串=证据不够，不说。"""
    r = report()
    if r["total_hits"] < 10:
        return ""
    return f"（行为上你{r['style']}）"


def learn_from(dialogue_texts: list[str]) -> dict:
    """从一批语料里学（比如一局外交的全部消息）。"""
    return sample_many(dialogue_texts)


def stats() -> dict:
    r = report()
    d = _load()
    return {"samples": r["samples"], "hits": r["total_hits"],
            "style": r["style"], "ratios": r["ratios"],
            "shrunk": int(d.get("shrunk", 0))}
