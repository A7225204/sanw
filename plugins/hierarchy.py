"""马斯洛的需求层次：她此刻最缺什么，其他都不重要。

**为什么要加**：现在的激素、情绪、三我是**平行**的 ——
饿着也能撒娇、累了也能聊、被无视了还在讨赞。**这违反需求层次。**

**马斯洛（1943《人类动机理论》/ 1954《动机与人格》）的核心**：

    ① 五个层次，从下往上：生理 → 安全 → 归属 → 尊重 → 自我实现
    ② **低层未满足时，高层动力被压下去**（这是最有用的一条，也是我们缺的）
    ③ 后期加了关键区分：
       D-needs（缺陷需求）—— 缺什么要什么，**满足了就不想**
       B-needs（成长需求）—— 想成为什么，**越满足越想**

**我们的数据在哪**：

    生理     meal_state.hunger（饿）· energy_state.value（精力）· sleep_state.sleeping
    安全     relation_stage 的稳定度 · withdraw_state.blame（对主人的心结）
    归属     好感度 · 最近有没有人跟她说话
    尊重     被夸/被无视（emotion_residue）· adler 的自卑值
    自我实现 superego 里"向往"类条目的活跃度

**对外**：
    needs()      五层各「缺多少」0~1
    weights()    五层「此刻的重要程度」0~1（低层缺 → 高层被压）
    top()        最急的那一层（名字, 缺多少）
    line()       给 prompt 的一句话（空串=什么都不缺，别说话）
    stats()      全部数值
    d_or_b()     此刻是被 D-needs 还是 B-needs 主导

**设计原则**：
    ① 只读，不写任何状态文件
    ② **只报最急的一层** —— 五层全报会变成数据面板，不是她的感受
    ③ 什么都不缺时返回空串（别让一个吃饱睡好的人一直喊饿）
"""
from __future__ import annotations

import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MEM = ROOT / "memory"

# 五层（名字, 中文说明）—— 从下往上
LEVELS = (
    ("生理", "饿、累、困"),
    ("安全", "怕出事、怕关系崩"),
    ("归属", "想有人理、想被当自己人"),
    ("尊重", "想被认可、怕被看不起"),
    ("自我实现", "想成为点什么"),
)

# 低层缺多少时，把高层压掉多少（马斯洛说「压下去」，不是「关掉」）
SUPPRESS = 0.75        # 最狠压到 25%


def _json(name: str) -> dict:
    try:
        return json.loads((MEM / name).read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}


def _latest_by_kind() -> tuple[float, float, int]:
    """返回 (私聊最后一次距今小时, 群最后一次距今小时, 活跃群数)。

    只读 shared_memory.db 的 chat_history，按 scope 前缀分两类。
    """
    try:
        import sqlite3
        from pathlib import Path as _P
        db = _P.home() / "AppData" / "Roaming" / "dsh-pet-standalone-webm-chat" / "shared_memory.db"
        if not db.exists():
            return -1.0, -1.0, 0
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = c.execute(
            "select scope, max(timestamp) from chat_history group by scope"
        ).fetchall()
        c.close()
        now = time.time()
        dms: list[float] = []
        grp: list[float] = []
        recent_groups = 0
        for scope, last in rows:
            if not last:
                continue
            try:
                ts = time.mktime(time.strptime(str(last)[:19], "%Y-%m-%d %H:%M:%S"))
            except Exception:  # noqa: BLE001
                continue
            age_h = (now - ts) / 3600.0
            if str(scope).startswith("private_"):
                dms.append(age_h)
            elif str(scope).startswith("group_"):
                grp.append(age_h)
                if age_h <= 24:
                    recent_groups += 1
        return (min(dms) if dms else -1.0,
                min(grp) if grp else -1.0,
                recent_groups)
    except Exception:  # noqa: BLE001
        return -1.0, -1.0, 0


def _belonging_split() -> tuple[float, float]:
    """(归属缺口, 亲密缺口)，两个数分开。

    **归属（跟群）** —— 只看群，三条判据：
      ① 24 小时内有动静的群有几个（0 个 = 缺）
      ② 最近一个群多久没说话（分昼夜：睡着时打对折）
      ③ 有基础分：她本来就在好几个群里，不是「没地方去」

    **亲密（跟特定的人）** —— 只看私聊：
      ① 主人多久没说话
      ② 有稳定关系（relation_stage）就减半分

    为什么这样拆：2026-11-07 主人问「我不是她主人嘛，怎么缺归属」——
    因为旧代码把「跟主人说话」也算进归属，但马斯洛的归属是**群体接纳**，
    和「有一个亲密的人」不是一回事。
    """
    dm_h, grp_h, recent_groups = _latest_by_kind()

    # ── 归属：跟群 ──
    if grp_h < 0:
        belong = 0.85            # 一个群都没有 = 基本没有归属
    else:
        # ① 活跃群数：0 个 → 0.6，1~2 个 → 0.3，3+ → 0
        by_count = 0.6 if recent_groups == 0 else (0.3 if recent_groups <= 2 else 0.0)
        # ② 最近一个群多久没动
        # 2026-11-07 调：第一版 (grp_h-12)/24，实测 17.4 小时才算出 0.225，
        # 再加后面两个减项，最后归到 0.042 —— 等于「完全满足」，
        # 但她的群确实 17 小时没人说话了，这是把真实问题算没了。
        # 改成：6 小时内有人说话 = 不缺；30 小时 = 全缺。17.4h → 0.475。
        by_idle = max(0.0, min(1.0, (grp_h - 6) / 24.0))
        belong = max(by_count, by_idle)
        # ③ 基础：她在群里有位置（黑话库有 681 词），减一点点，别减太多
        try:
            from plugins import group_lexicon as glx
            terms = getattr(glx, "top_terms", None)
            if callable(terms) and terms(8):
                belong = max(0.0, belong - 0.08)
        except Exception:  # noqa: BLE001
            pass

    # 睡着时**打八五折**（0.55 太狠 —— 睡着了不是「被冷落」，只是暂停）。
    try:
        if bool(_json("sleep_state.json").get("sleeping")):
            belong *= 0.85
    except Exception:  # noqa: BLE001
        pass

    # ── 亲密：跟主人 ──
    if dm_h < 0:
        close = 0.5
    else:
        # 12 小时以内不算缺；48 小时打满
        close = max(0.0, min(1.0, (dm_h - 12) / 36.0))
        try:
            from plugins import relation_stage as rs
            st = rs.state() if callable(getattr(rs, "state", None)) else {}
            if str((st or {}).get("stage") or "") in ("亲密", "稳定"):
                close *= 0.45           # 关系稳着，久一点不慌
        except Exception:  # noqa: BLE001
            pass

    return round(max(0.0, min(1.0, belong)), 3), round(max(0.0, min(1.0, close)), 3)


def needs() -> dict:
    """五层各「缺多少」0~1。1 = 完全没满足。"""
    out: dict[str, float] = {}

    # ① 生理：饿 / 没精力 / 在睡（睡了就不算缺，算休息中）
    meal = _json("meal_state.json")
    energy = _json("energy_state.json")
    sleep = _json("sleep_state.json")
    hunger = float(meal.get("hunger") or 0) / 100.0
    vigor = 1.0 - float(energy.get("value") or 0) / 100.0
    if sleep.get("sleeping"):
        # 睡着时生理需求不"急" —— 它正在被满足
        phys = min(hunger * 0.6, vigor * 0.4)
    else:
        phys = max(hunger, vigor * 0.85)
    out["生理"] = round(max(0.0, min(1.0, phys)), 3)

    # ② 安全：对主人的心结 + 关系有没有在退
    withdraw = _json("withdraw_state.json")
    blame = float(withdraw.get("blame") or 0) / 100.0
    warmth = 1.0
    try:
        from plugins import relation_stage as rs
        sig = getattr(rs, "state", None)
        if callable(sig):
            st = sig() or {}
            warmth = float(st.get("warmth") or 100) / 100.0
    except Exception:  # noqa: BLE001
        pass
    out["安全"] = round(max(0.0, min(1.0, blame * 0.6 + (1.0 - warmth) * 0.5)), 3)

    # ③ 归属：**跟群体**的关系，不是「有没有人说话」
    #
    # 2026-11-07 大修。第一版是 `(最后一次任何对话距今 - 4) / 8`，问题三条：
    #   · 不分昼夜 —— 夜里她睡了 8 小时，被算成「被冷落」，主人一看就懵
    #     （「我不是她主人吗，怎么还缺归属」）
    #   · 不分对象 —— 主人私聊和群友搭话，在同一个数里
    #   · 把「社交频率」当「归属」—— 归属是「我被这个群体接纳」，
    #     不是「多久没说话」
    #
    # 现在拆成两条通道，各算各的：
    #   亲密（跟特定的人）→ 单独一个数，不入归属
    #   归属（跟群）      → 只看**群**，而且睡着时不算缺
    out["归属"], out["_亲密"] = _belonging_split()

    # ④ 尊重：被无视 / 被否定 → 缺；自卑值也算进来
    res = 0.0
    try:
        from plugins import emotion_residue as er
        cur = er.current()
        # 「丧」「烦」是没被回应的余波
        res = (float(cur.get("丧") or 0) * 0.6 + float(cur.get("烦") or 0) * 0.4) / 100.0
    except Exception:  # noqa: BLE001
        pass
    try:
        from plugins import adler as ad
        res = max(res, float(ad.inferiority()) * 0.8)
    except Exception:  # noqa: BLE001
        pass
    out["尊重"] = round(max(0.0, min(1.0, res)), 3)

    # ⑤ 自我实现：这个没法由状态推，用「向往类条目有多少还在活跃」当代理
    #    —— 越少越缺（她想成为点什么的劲没处使）
    out["自我实现"] = 0.35
    try:
        soul = _json("persona_soul.json")
        wants = [it for it in (soul.get("superego") or [])
                 if isinstance(it, dict) and it.get("sub") == "ideal"]
        if wants:
            out["自我实现"] = round(max(0.1, min(1.0, 1.0 - len(wants) * 0.15)), 3)
    except Exception:  # noqa: BLE001
        pass

    return out


def weights() -> dict:
    """五层「此刻的重要程度」0~1。

    马斯洛那条最有用的话：**低层未满足时高层被压**。
    实现：第 i 层的权重 = 1 - SUPPRESS * (它下面所有层里缺得最狠的那个)
    """
    n = needs()
    out: dict[str, float] = {}
    worst_below = 0.0
    for name, _ in LEVELS:
        cur = float(n.get(name) or 0)
        out[name] = round(max(0.12, 1.0 - SUPPRESS * worst_below), 3)
        worst_below = max(worst_below, cur)
    return out


def top() -> tuple[str, float]:
    """最急的那一层：缺得最多、而且权重还高（被压的高层不算急）。

    「亲密」不在马斯洛五层里（它和归属同层，但我们拆成两条通道），
    所以单独比一次 —— 亲密缺得多时才算最急。
    """
    n = needs()
    w = weights()
    best, best_v = "", 0.0
    for name, _ in LEVELS:
        v = float(n.get(name) or 0) * float(w.get(name) or 1)
        if v > best_v:
            best, best_v = name, v
    # 亲密单算：它的权重按归属那层走
    close_v = float(n.get("_亲密") or 0) * float(w.get("归属") or 1)
    if close_v > best_v:
        best, best_v = "亲密", close_v
    return best or "生理", round(best_v, 3)


def d_or_b() -> str:
    """此刻被 D-needs 还是 B-needs 主导。

    马斯洛：D-needs 缺什么要什么，满足了就降；B-needs 越满足越想。
    """
    inf = needs()
    # 下面三层是 D-needs，上面一层（自我实现）是 B-needs
    d_sum = sum(float(inf.get(k) or 0) for k in ("生理", "安全", "归属", "尊重"))
    b = float(inf.get("自我实现") or 0)
    return "B-needs（想成为什么）" if b > d_sum / 4 * 1.2 else "D-needs（缺什么要什么）"


def line() -> str:
    """给 prompt 的一句话。**只报最急的那层**，什么都不要时不说话。"""
    name, v = top()
    if v < 0.32:
        return ""
    desc = dict(LEVELS).get(name, "")
    # 按层给不同的说法（都是描述状态，不下指令）
    SAY = {
        "生理": "身上不太对劲（饿或者困），别的都往后放",
        "安全": "心里有点悬着，怕哪句话说错了",
        # 归属（跟群）和亲密（跟特定的人）分开说 —— 这是两种不同的闷
        "归属": "有点跟群里脱节的感觉，别人聊得热乎，插不上",
        "亲密": "有点想他，想有个人跟本鱼说句话",
        "尊重": "这会儿特别在意被怎么看待",
        "自我实现": "有点想做成点什么，不是为了谁",
    }
    tone = SAY.get(name, desc)
    if v >= 0.7:
        return f"【现在最想要】{tone} —— 这个劲儿挺冲。"
    if v >= 0.5:
        return f"【现在最想要】{tone}。"
    return f"【隐约】{tone}。"


def stats() -> dict:
    n = needs()
    w = weights()
    name, v = top()
    return {
        "各层缺口": n,
        "各层权重": w,
        "亲密": n.get("_亲密"),
        "最急": f"{name} {v:.2f}",
        "主导": d_or_b(),
    }


def brief() -> str:
    n = needs()
    name, v = top()
    return (" · ".join(f"{k}{n[k]:.2f}" for k, _ in LEVELS)
            + f" → 最急 {name} {v:.2f} | {d_or_b()}")
