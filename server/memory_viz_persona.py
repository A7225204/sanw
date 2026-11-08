"""人格星海：以「她」为中心，按记忆类型分成五支。

P1 只读：
  · 长期记忆  ← memory/长期记忆.json 的 archive
  · 人物卡    ← memory/relation_state.json + nick_names.json 按 QQ 聚合
  · 短期记忆  ← shared_memory.db 的 chat_history 最近 N 条
  · 人格成长  ← 占位（还没有按日期的人设快照）
  · 经历      ← memory/trauma.json 的条目

坐标在这里算好（中心 + 五个球面锚点 + 类内散布），前端直接复用星海渲染器。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 遗忘黑洞：人格视图最右侧。短期记忆保留度掉到 0.30 以下就开始往这里滑落，
# 到跟前（suck > 0.985）就不再画出来 —— 相当于「被吸进去才消失」。
# 与前端 memoryFlowLayer 的绘制位置保持一致：
#   白洞 (-4.60, 0.02, 0) / 黑洞 (4.20, 0.02, 0) / 短期记忆球 (0, 2.35, 0)
# 之前后端是 ±2.35，导致"分流粒子到不了画出来的黑洞"、"短期粒子不从球里出来"
BLACKHOLE_POS = {"x": 4.20, "y": 0.02, "z": 0.0}

# 记忆白洞：新记忆诞生的地方，在人格视图最左侧（黑洞的反面）
WHITENOLE_POS = {"x": -4.60, "y": 0.02, "z": 0.0}


# ── 遗忘登记表：被黑洞吞掉的短期记忆，永久不再出现在视图里 ──
FORGOTTEN_PATH = ROOT / "memory" / "forgotten.json"


def forgotten_ids() -> set:
    try:
        d = _read_json(FORGOTTEN_PATH)
        return {str(x) for x in (d.get("ids") or [])}
    except Exception:  # noqa: BLE001
        return set()


def mark_forgotten(ids: list) -> int:
    """把若干短期记忆 id 永久标记为「已遗忘」。返回当前总数。"""
    cur = forgotten_ids()
    before = len(cur)
    for x in ids:
        cur.add(str(x))
    try:
        FORGOTTEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        FORGOTTEN_PATH.write_text(
            json.dumps({"ids": sorted(cur), "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        pass
    return len(cur) - before


def _ts_ms(s) -> int:
    """把 "YYYY-mm-dd HH:MM:SS" 或 "YYYY-mm-dd" 解析成毫秒时间戳；失败返回 0。"""
    txt = str(s or "").strip()
    if not txt:
        return 0
    for fmt, ln in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M", 16), ("%Y-%m-%d", 10)):
        try:
            return int(time.mktime(time.strptime(txt[:ln], fmt)) * 1000)
        except Exception:  # noqa: BLE001
            continue
    return 0


CATEGORIES = [
    ("long", "长期记忆"),
    ("card", "人物池"),
    ("growth", "人格成长"),
    ("short", "短期记忆"),
    ("exp", "经历"),
    ("thought", "念头池"),
    ("obsession", "执念池"),
    ("agency", "行动空间"),
    ("feedback", "反馈闭环"),
]

R_SHELL = 0.8
# 星海径向分层（报告 §三.1）：核心在内圈，短期记忆在最外圈
R_INNER = 0.42
R_MID = 0.72
R_OUTER = 1.0
_CAT_RADIUS = {
    "growth": R_INNER, "card": R_INNER,     # 自我 + 核心关系
    "long": R_MID, "exp": R_MID,            # 轨道层
    "short": R_OUTER,                       # 外圈，随时间长暗淡
    "thought": 1.25,                        # 最外层：未完成的意图
    "obsession": 1.35,
    "agency": 1.45,
    "feedback": 1.55,                      # 再外一层：放不下的话头（比念头更沉）
}
MAX_PER_CAT = 40
SHORT_WINDOW = 20


def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8-sig")) or {}
    except Exception:  # noqa: BLE001
        return {}


def _hfrac(s: str) -> tuple[float, float, float]:
    h = hashlib.sha1(s.encode("utf-8")).digest()
    return (h[0] / 255, h[1] / 255, h[2] / 255)


def _anchor(i: int, radius: float = R_SHELL) -> dict:
    n = len(CATEGORIES)
    theta = math.pi * (i + 0.5) / n
    phi = i * 2 * math.pi / n
    return {
        "x": round(radius * math.sin(theta) * math.cos(phi), 4),
        "y": round(radius * math.cos(theta), 4),
        "z": round(radius * math.sin(theta) * math.sin(phi), 4),
    }


def _decay_ret(t_ms: int, weight: float = 1.0, half_days: float = 14.0) -> float:
    """真实保留分数：按时间半衰 + 权重，返回 0~1。

    主人要求「不能写死 1.0」——短期/经历/人物卡都用这个算。
    """
    if not t_ms:
        return round(min(1.0, 0.5 * weight), 3)
    age_days = max(0.0, (time.time() * 1000 - t_ms) / 86_400_000.0)
    return round(max(0.05, min(1.0, (0.5 ** (age_days / half_days)) * weight)), 3)


def _satellite(base: dict, seed: str, radius: float = 0.22) -> dict:
    """群内记忆卫星：螺旋分布，形成 3 条旋臂（星云而不是球形雾）。"""
    u, v, w = _hfrac(seed)
    r = radius * (0.28 + 0.72 * u)          # 越靠外越稀
    arm = int(v * 3)                         # 3 条旋臂
    arm_angle = arm * (2 * math.pi / 3)
    theta = arm_angle + r * 7.5 + (v - int(v)) * 0.6   # 半径越大角度越偏 → 螺旋
    phi = math.acos(2 * w - 1) * 0.55        # 压扁，星云更扁平
    return {
        "x": round(base["x"] + r * math.cos(theta), 4),
        "y": round(base["y"] + r * math.sin(phi) * 0.45, 4),
        "z": round(base["z"] + r * math.sin(theta), 4),
    }

def _pool_spread(base: dict, seed: str, radius: float = 0.20) -> dict:
    """池子里的成员：扁平小盘（半径 ~0.2，厚 ~0.04），聚成一团看得出边界。

    和 _scatter 的区别：_scatter 是球面均匀撒点（看着像散落的尘埃），
    池子要的是「一簇」——所以压扁 y、半径向心收，像念头池那样抱团。
    """
    u, v, w = _hfrac(seed)
    r = radius * (0.25 + 0.75 * math.sqrt(u))      # sqrt：中心密、边缘疏
    th = 2 * math.pi * v
    return {
        "x": round(base["x"] + r * math.cos(th), 4),
        "y": round(base["y"] + (w - 0.5) * radius * 0.22, 4),
        "z": round(base["z"] + r * math.sin(th), 4),
    }


def _scatter(base: dict, seed: str) -> dict:
    """锚点周围小范围散布；同一 id 每次结果一致。"""
    u, v, w = _hfrac(seed)
    r = 0.10 + 0.16 * u
    th = 2 * math.pi * v
    ph = math.acos(2 * w - 1)
    return {
        "x": round(base["x"] + r * math.sin(ph) * math.cos(th), 4),
        "y": round(base["y"] + r * math.cos(ph), 4),
        "z": round(base["z"] + r * math.sin(ph) * math.sin(th), 4),
    }


def _to_ms(v) -> int:
    """把 epoch 秒 / 毫秒 / 'YYYY-MM-DD[ HH:MM:SS]' / 'MM-DD' 统一成毫秒时间戳。"""
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        x = float(v)
        if x <= 0:
            return 0
        return int(x * 1000) if x < 1e11 else int(x)
    s = str(v).strip()
    if not s:
        return 0
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            if fmt == "%m-%d":
                dt = dt.replace(year=datetime.now().year)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    return 0


def _node(nid: str, kind: str, summary: str, pos: dict, *,
          size: float = 0.5, bright: float = 0.8, color: int = 0xbfe0ff,
          category: str = "", t: int = 0, extra: dict | None = None,
          retention: float | None = None) -> dict:
    node = {
        "id": nid, "scope": "", "parentId": None, "topicId": None,
        "summary": summary, "sourceMessageIds": [], "kind": kind,
        "retentionScore": (retention if retention is not None else 1.0), "importance": 1.0, "recency": 1.0, "frequency": 1.0,
        "createdAt": "", "lastAccessedAt": "",
        "category": category,
        "t": t,
        "starfield": {
            "x": pos["x"], "y": pos["y"], "z": pos["z"],
            "size": size, "brightness": bright, "color": f"#{color:06x}",
        },
    }
    if extra:
        node.update(extra)
    return node


# ---------------------------------------------------------------- 五类数据

def _long_nodes(scope: str | None, anchor: dict) -> list[dict]:
    from server import memory_viz as mv
    arch = mv._archive()
    items: list[tuple[str, dict]] = []
    if scope and scope != "__all__":
        items = [(scope, it) for it in (arch.get(scope) or [])]
    else:
        for sc, lst in arch.items():
            items += [(sc, it) for it in (lst or [])]
    items.sort(key=lambda x: -float(x[1].get("w") or 0))
    out = []
    for sc, it in items[:MAX_PER_CAT]:
        # 用原始 mem id：详情接口能直接命中，不用另做兜底
        oid = f"mem:{sc}:{it.get('id')}"
        fact = str(it.get("fact") or "")[:60]
        emo = mv._emotion(fact)
        w = float(it.get("w") or 1)
        out.append(_node(
            oid, "memory", fact, _scatter(anchor, oid),
            size=round(0.35 + 0.55 * min(1.0, w / 3.0), 3),
            bright=0.65, category="long",
            t=_to_ms(it.get("lu")) or _to_ms(it.get("time")),
            extra={"emotion": emo, "originScope": sc,
                   "retentionScore": round(min(1.0, w / 3.0), 3)},
        ))
    return out


def _card_nodes(anchor: dict) -> list[dict]:
    rel = _read_json(ROOT / "memory" / "relation_state.json")
    nick = _read_json(ROOT / "memory" / "nick_names.json")
    names: dict[str, str] = {}
    seen_map: dict[str, int] = {}
    for _gid, people in (nick or {}).items():
        if not isinstance(people, dict):
            continue
        for uid, rec in people.items():
            if isinstance(rec, dict):
                nm = str(rec.get("current") or rec.get("first") or "").strip()
                if nm:
                    names.setdefault(str(uid), nm)
                seen_map[str(uid)] = max(seen_map.get(str(uid), 0), int(rec.get("seen") or 0))
    rows = sorted(rel.items(), key=lambda kv: -float((kv[1] or {}).get("last") or 0))
    rows = [(u, s) for u, s in rows if isinstance(s, dict)][:MAX_PER_CAT]
    out = []
    from server import character_cards as cc
    for uid, st in rows:
        label = names.get(str(uid)) or f"QQ {uid}"
        neg = float(st.get("neg") or 0)
        card = cc.get_card(str(uid))
        relm = cc.get_relation(str(uid))
        if card and (card.get("性格") or card.get("职业")):
            label = f"{label}·{str(card.get('性格') or card.get('职业'))[:10]}"
        oid = f"cat:card:{uid}"
        out.append(_node(
            oid, "memory", label, _pool_spread(anchor, oid, radius=0.24),
            size=0.45, bright=0.6, category="card",
            t=_to_ms(st.get("last")),
            extra={"affinity": round(1 - neg, 3), "emotion": "中性",
                   "uid": str(uid), "last": st.get("last") or 0,
                   "hasCard": bool(card), "hasRelation": bool(relm),
                   # 人物卡：互动次数 + 最近联系时间，两个都算
                   "retentionScore": round(min(1.0, 0.25 + seen_map.get(str(uid), 0) / 120.0)
                                           * _decay_ret(_to_ms(st.get("last")), 1.0, half_days=60.0), 3)},
        ))
    return out


def _short_nodes(scope: str | None, anchor: dict) -> list[dict]:
    db = Path(os.environ.get("APPDATA") or str(Path.home())) / "dsh-pet-standalone-webm-chat" / "shared_memory.db"
    if not db.exists():
        return []
    try:
        conn = sqlite3.connect(str(db), timeout=5)
    except Exception:  # noqa: BLE001
        return []
    out: list[dict] = []
    try:
        if scope and scope != "__all__":
            rows = conn.execute(
                "select id, scope, speaker, message, timestamp from chat_history "
                "where scope=? order by rowid desc limit ?", (scope, SHORT_WINDOW)).fetchall()
        else:
            rows = conn.execute(
                "select id, scope, speaker, message, timestamp from chat_history "
                "order by rowid desc limit ?", (SHORT_WINDOW,)).fetchall()
    except Exception:  # noqa: BLE001
        rows = []
    finally:
        conn.close()
    for rid, sc, speaker, message, ts in rows:
        oid = f"cat:short:{rid}"
        text = str(message or "").replace("\n", " ")[:46]
        _t = _to_ms(ts)
        out.append(_node(
            oid, "memory", f"{speaker or '?'}：{text}", _scatter(anchor, oid),
            size=0.4, bright=0.5, category="short",
            t=_t,
            extra={"emotion": "中性", "originScope": sc, "createdAt": str(ts or ""),
                   "retentionScore": _decay_ret(_t, 1.0, half_days=3.0)},
        ))
    return out


def _exp_nodes(anchor: dict) -> list[dict]:
    """经历 = 具象事件/情感变化（trauma + episodes）+ 正面高光。

    「对某人的态度从嫌弃变关心」这类属于这里，不是人格成长。
    """
    out: list[dict] = []

    # ① 关系/情感变化与具体事件（memory/episodes.json）
    eps = _read_json(ROOT / "memory" / "episodes.json")
    for i, it in enumerate((eps.get("items") or [])[:MAX_PER_CAT]):
        if not isinstance(it, dict):
            continue
        oid = str(it.get("id") or f"cat:ep:{i}")
        out.append(_node(
            oid, "memory", str(it.get("title") or "一段经历")[:40],
            _scatter(anchor, oid),
            size=0.55, bright=0.85, category="exp",
            t=_to_ms(it.get("at")),
            extra={"emotion": it.get("mood") or "中性",
                   "uid": str(it.get("uid") or ""),
                   # 经历：随时间缓慢衰减（故事线保留，分数下降）
                   "retentionScore": _decay_ret(_to_ms(it.get("at")), 0.9, half_days=30.0),
                   "episode": {"text": str(it.get("text") or ""),
                               "at": str(it.get("at") or "")}},
        ))

    # ② 负面：创伤条目
    data = _read_json(ROOT / "memory" / "trauma.json")
    for i, it in enumerate((data.get("items") or [])[:MAX_PER_CAT]):
        if not isinstance(it, dict):
            continue
        oid = f"cat:exp:{i}"
        subj = str(it.get("subject") or "某件事")[:30]
        charge = float(it.get("charge") or 0)
        out.append(_node(
            oid, "memory", f"{subj}（电荷 {round(charge * 100)}%）",
            _scatter(anchor, oid),
            size=round(0.45 + 0.35 * min(1.0, charge), 3),
            bright=0.7, category="exp",
            t=_to_ms(it.get("since")),
            extra={"emotion": "负面", "trauma": {"charge": charge,
                                                 "triggers": it.get("triggers") or [],
                                                 "since": it.get("since") or ""}},
        ))

    # 正面：长期记忆里情绪归正面、重要度最高的一批
    from server import memory_viz as mv
    pos: list[tuple[str, dict, str]] = []
    for sc, lst in mv._archive().items():
        for it in lst or []:
            fact = str(it.get("fact") or "")
            emo = mv._emotion(fact)
            if emo in ("喜欢", "开心", "得意", "感谢", "约定", "期待", "正面"):
                pos.append((sc, it, emo))
    pos.sort(key=lambda x: -float(x[1].get("w") or 0))
    for sc, it, emo in pos[:10]:
        oid = f"cat:exp:pos:{sc}:{it.get('id')}"
        fact = str(it.get("fact") or "")[:60]
        w = float(it.get("w") or 1)
        out.append(_node(
            oid, "memory", fact, _scatter(anchor, oid),
            size=round(0.4 + 0.45 * min(1.0, w / 3.0), 3),
            bright=0.85, category="exp",
            t=_to_ms(it.get("lu")) or _to_ms(it.get("time")),
            extra={"emotion": emo, "originScope": sc},
        ))
    return out


def _growth_nodes(anchor: dict) -> list[dict]:
    """人格成长：一次提炼产出一个球，状态决定亮不亮。"""
    from server import memory_growth as mg
    out: list[dict] = []
    for it in mg.list_items():
        oid = str(it.get("id"))
        st = str(it.get("status") or "pending")
        out.append(_node(
            oid, "memory", f"{it.get('trait')}：{it.get('value')}",
            _scatter(anchor, oid),
            size=0.7 if st == "pending" else (0.55 if st == "solidified" else 0.35),
            bright=1.0 if st == "pending" else (0.85 if st == "solidified" else 0.3),
            category="growth",
            t=int(it.get("t") or 0),
            extra={"emotion": "中性", "status": st,
                   "trait": it.get("trait"), "value": it.get("value"),
                   "growthCategory": it.get("category") or "style",
                   # 人格成长按状态给分：固化=满，待确认=0.85，删除=0.2
                   "retentionScore": {"solidified": 1.0, "pending": 0.85}.get(st, 0.2),
                   "evidence": it.get("evidence") or []},
        ))
    return out


def _thought_nodes(anchor: dict) -> list[dict]:
    """念头池：未完成的意图，飘在星海最外层（灰白虚影）。"""
    out: list[dict] = []
    data = _read_json(ROOT / "memory" / "thoughts.json")
    now = time.time()
    items = [x for x in (data.get("items") or [])
             if isinstance(x, dict) and float(x.get("created") or 0) > now - 86400 * 7]
    items.sort(key=lambda x: -float(x.get("created") or 0))
    for i, it in enumerate(items[:MAX_PER_CAT]):
        oid = f"thought:{i}"
        out.append(_node(
            oid, "memory", f"💭 {str(it.get('text') or '')[:38]}",
            _scatter(anchor, oid),
            size=0.34, bright=0.45, category="thought",
            t=int(float(it.get("created") or 0) * 1000),
            extra={"emotion": "中性", "thought": True},
        ))
    return out


# ---------------------------------------------------------------- 对外

def persona_view(scope: str | None) -> dict:
    daily = _read_json(ROOT / "memory" / "daily_persona.json")
    mood = str(daily.get("mood") or "未知")
    score = int(daily.get("score") or 0)

    anchors = {key: _anchor(i, _CAT_RADIUS.get(key, R_SHELL))
               for i, (key, _label) in enumerate(CATEGORIES)}

    center = _node(
        "persona:me", "persona", "蓝色大肥鱼", {"x": 0.0, "y": 0.0, "z": 0.0},
        size=18.0, bright=1.0, color=0xffc83d,
        category="center",
        extra={"emotion": "中性", "mood": mood, "score": score},
    )

    nodes: list[dict] = [center]
    edges: list[dict] = []
    categories: list[dict] = []

    builders = {
        "long": lambda: _long_nodes(scope, anchors["long"]),
        "card": lambda: _card_nodes(anchors["card"]),
        "short": lambda: _short_nodes(scope, anchors["short"]),
        "exp": lambda: _exp_nodes(anchors["exp"]),
        "growth": lambda: _growth_nodes(anchors["growth"]),
        "thought": lambda: _thought_nodes(anchors["thought"]),
    }

    for key, label in CATEGORIES:
        pos = anchors[key]
        if key in builders:
            try:
                cat_nodes = builders[key]()
            except Exception as e:  # noqa: BLE001
                cat_nodes = []
                categories.append({"key": key, "label": label, "count": 0,
                                   "anchor": pos, "status": f"error: {type(e).__name__}"})
                continue
        else:
            # 人格成长：P1 占位，等有按日期的人设快照再填
            cat_nodes = []

        anchor_node = _node(
            f"cat:{key}", "category", label, pos,
            size=0.9, bright=0.9, color=0x7fd4ff, category=key,
            extra={"count": len(cat_nodes)},
        )
        nodes.append(anchor_node)
        edges.append({"source": "persona:me", "target": f"cat:{key}",
                      "kind": "persona", "weight": 1.0})
        for n in cat_nodes:
            edges.append({"source": f"cat:{key}", "target": n["id"],
                          "kind": "persona-branch", "weight": 0.5})
        nodes.extend(cat_nodes)
        categories.append({
            "key": key, "label": label, "count": len(cat_nodes),
            "anchor": pos, "status": "ok" if cat_nodes else "empty",
        })

    ts = [int(n.get("t") or 0) for n in nodes if int(n.get("t") or 0) > 0]
    time_range = {"min": min(ts), "max": max(ts)} if ts else {"min": 0, "max": 0}
    return {
        "scope": scope or "__all__",
        "center": center,
        "categories": categories,
        "nodes": nodes,
        "edges": edges,
        "timeRange": time_range,
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _soul_summary() -> dict:
    """人格球当前的标签汇总（traits / rules / defense + 成长 + 候选池 + 情绪基调）。"""
    try:
        from server import soul_aggregation as sa
        v = sa.view()
        col, kind = sa.core_color()
        pools = v.get("pools") or {}
        pool_rows = {
            "traits": [str((x or {}).get("text") or "") for x in (pools.get("traits") or [])],
            "rules": [str((x or {}).get("text") or "") for x in (pools.get("rules") or [])],
        }
        # 人格成长条目：也是「词条」，点人格主球时一起列出来
        growth: list[dict] = []
        try:
            from server import memory_growth as mg
            for it in mg.list_items():
                st = str(it.get("status") or "pending")
                if st not in ("pending", "solidified"):
                    continue
                growth.append({
                    "id": str(it.get("id") or ""),
                    "trait": str(it.get("trait") or ""),
                    "value": str(it.get("value") or ""),
                    "status": st,
                    "category": str(it.get("category") or "style"),
                    "at": str(it.get("since") or ""),
                })
        except Exception:  # noqa: BLE001
            growth = []
        _triad_n = sum(len(v.get(b) or []) for b in ("id", "ego", "superego"))
        in_register = _triad_n or (len(v.get("traits") or []) + len(v.get("rules") or [])
                                    + len(v.get("defense") or []))
        # 词条明细（带依据/时间）——面板按类型上色，防御还要能跳到对应创伤
        trait_items = [
            {"text": str(x.get("text") or ""), "why": str(x.get("why") or ""),
             "at": str(x.get("at") or ""), "solidified": bool(x.get("solidified"))}
            for x in (v.get("traits") or [])
        ]
        rule_items = [
            {"text": str(x.get("text") or ""), "why": str(x.get("why") or ""),
             "at": str(x.get("at") or ""), "solidified": bool(x.get("solidified"))}
            for x in (v.get("rules") or [])
        ]
        defense_items = _defense_items(v.get("defense") or [])

        def _triad_items(bucket: str) -> list[dict]:
            return [{"text": str(x.get("text") or ""), "why": str(x.get("why") or ""),
                     "at": str(x.get("at") or ""), "solidified": bool(x.get("solidified")),
                     "need": str(x.get("need") or ""), "sub": str(x.get("sub") or "")}
                    for x in (v.get(bucket) or [])]

        return {
            "traits": [str(x.get("text") or "") for x in (v.get("traits") or [])],
            "rules": [str(x.get("text") or "") for x in (v.get("rules") or [])],
            "defense": [str(x.get("text") or "") for x in (v.get("defense") or [])],
            # 三我（弗洛伊德结构）：面板按这个分组，而不是旧的 traits/rules
            "id": [str(x.get("text") or "") for x in (v.get("id") or [])],
            "ego": [str(x.get("text") or "") for x in (v.get("ego") or [])],
            "superego": [str(x.get("text") or "") for x in (v.get("superego") or [])],
            "idItems": _triad_items("id"),
            "egoItems": _triad_items("ego"),
            "superegoItems": _triad_items("superego"),
            "traitItems": trait_items,
            "ruleItems": rule_items,
            "defenseItems": defense_items,
            "total": in_register,
            "growth": growth,
            "pools": pool_rows,
            "poolTotal": len(pool_rows["traits"]) + len(pool_rows["rules"]),
            "allTotal": in_register + len(growth) + len(pool_rows["traits"]) + len(pool_rows["rules"]),
            "mood": v.get("mood") or {},
            # 自我四项功能评分（0~1）：现实检验/冲动控制/延迟满足/情绪调节
            "egoFunctions": v.get("ego_functions") or {},
            "superegoSeverity": v.get("superego_severity"),
            "forces": v.get("forces") or {},
            "conflictStats": v.get("conflict_stats") or v.get("conflictStats") or {},
            "moodKind": kind,
            "lastRun": v.get("last_run") or "",
            "updatedAt": v.get("updated_at") or "",
        }
    except Exception:  # noqa: BLE001
        return {"traits": [], "rules": [], "defense": [], "total": 0,
                "traitItems": [], "ruleItems": [], "defenseItems": [],
                "growth": [], "pools": {"traits": [], "rules": []}, "poolTotal": 0, "allTotal": 0}


def _defense_items(defense: list) -> list[dict]:
    """防御机制 → 关联到 trauma.json 里的那条创伤（面板点一下就能跳过去）。

    打分：防御标签里出现创伤主体 +3；依据里命中触发词 +1.5/个；
    「电荷 x」只做微调（依据里常写"非主人最高"，按主体匹配会误判）。
    """
    rows: list[dict] = []
    try:
        trauma = [x for x in (_read_json(ROOT / "memory" / "trauma.json").get("items") or [])
                  if isinstance(x, dict)]
    except Exception:  # noqa: BLE001
        trauma = []
    for i, it in enumerate(defense):
        if not isinstance(it, dict):
            continue
        text = str(it.get("text") or "")
        why = str(it.get("why") or "")
        m = re.search(r"电荷\s*([0-9.]+)", why)
        want = float(m.group(1)) if m else None
        best_ti, best_score = -1, 0.0
        for ti, tr in enumerate(trauma):
            score = 0.0
            subj = str(tr.get("subject") or "")
            if subj and subj in text:
                score += 3.0
            for tg in (tr.get("triggers") or []):
                tg = str(tg or "")
                if tg and (tg in why or tg in text):
                    score += 1.5
            ch = float(tr.get("charge") or 0)
            if want is not None:
                score += max(0.0, 0.5 - abs(ch - want))
            if score > best_score:
                best_ti, best_score = ti, score
        row = {"text": text, "why": why, "at": str(it.get("at") or ""), "index": i}
        if best_ti >= 0 and best_score >= 1.0:
            tr = trauma[best_ti]
            row.update({
                "traumaNodeId": f"trauma:{best_ti}",
                "traumaSubject": str(tr.get("subject") or ""),
                "charge": float(tr.get("charge") or 0),
                "triggers": [str(x) for x in (tr.get("triggers") or [])],
            })
        rows.append(row)
    return rows


def soul_history() -> dict:
    """人格演化史：在册词条 + 成长条目的时间线 + 候选池的进场时间。"""
    from server import soul_aggregation as sa
    from server import memory_growth as mg
    v = sa.view()
    growth: list[dict] = []
    for it in mg.list_items():
        growth.append({
            "id": str(it.get("id") or ""),
            "trait": str(it.get("trait") or ""),
            "value": str(it.get("value") or ""),
            "status": str(it.get("status") or "pending"),
            "category": str(it.get("category") or "style"),
            "at": str(it.get("since") or ""),
            "t": int(it.get("t") or 0),
        })
    growth.sort(key=lambda x: (x.get("at") or "", x.get("t") or 0))
    pools: list[dict] = []
    for bucket, label in (("traits", "特质"), ("rules", "准则")):
        for x in (v.get("pools") or {}).get(bucket) or []:
            if not isinstance(x, dict):
                continue
            pools.append({
                "bucket": bucket, "label": label,
                "text": str(x.get("text") or ""),
                "at": str(x.get("at") or ""),
                "firstSeen": str(x.get("first_seen") or ""),
                "hits": int(x.get("hits") or 0),
                "solidified": bool(x.get("solidified")),
            })
    pools.sort(key=lambda x: x.get("firstSeen") or "")
    return {
        "updatedAt": v.get("updated_at") or "",
        "lastRun": v.get("last_run") or "",
        "soul": {
            "traits": [str(x.get("text") or "") for x in (v.get("traits") or [])],
            "rules": [str(x.get("text") or "") for x in (v.get("rules") or [])],
            "defense": [str(x.get("text") or "") for x in (v.get("defense") or [])],
        },
        "growth": growth,
        "pools": pools,
    }


def soul_export() -> dict:
    """导出人格档案：在册 + 成长 + 候选池 + 防御关联，一次打包。"""
    from server import soul_aggregation as sa
    from server import memory_growth as mg
    v = sa.view()
    return {
        "exportedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "updatedAt": v.get("updated_at") or "",
        "lastRun": v.get("last_run") or "",
        "traits": v.get("traits") or [],
        "rules": v.get("rules") or [],
        "defense": _defense_items(v.get("defense") or []),
        "growth": mg.list_items(),
        "pools": {
            "traits": (v.get("pools") or {}).get("traits") or [],
            "rules": (v.get("pools") or {}).get("rules") or [],
        },
    }


def _conflict_level() -> str:
    """冲突等级（低/中/高）：让前端蓝球做拖拽/闪烁特效用。"""
    try:
        from server import soul_aggregation as sa
        return sa.conflict_level()
    except Exception:  # noqa: BLE001
        return "低"


def _forces_now() -> dict:
    """读 soul_aggregation 算的三我势力；算不出来就退回按激素粗估。"""
    try:
        from server import soul_aggregation as _sa
        f = _sa.forces()
        return {"id": f.get("本我", 0.4), "ego": f.get("自我", 0.4), "superego": f.get("超我", 0.4)}
    except Exception:  # noqa: BLE001
        try:
            hs = _read_json(ROOT / "memory" / "hormones_state.json").get("values") or {}
            return {"id": round(float(hs.get("DA") or 40) / 100, 2),
                    "ego": 0.4,
                    "superego": round(0.3 + 0.5 * float(hs.get("CORT") or 40) / 100, 2)}
        except Exception:  # noqa: BLE001
            return {"id": 0.4, "ego": 0.4, "superego": 0.4}


def _triad_summary() -> dict:
    """本我 / 自我 / 超我 三层摘要（人格视图三个金球用）。"""
    hs = _read_json(ROOT / "memory" / "hormones_state.json").get("values") or {}
    des = _read_json(ROOT / "memory" / "desire_state.json") or {}
    soul = _read_json(ROOT / "memory" / "persona_soul.json")
    wd = _read_json(ROOT / "memory" / "withdraw_state.json")

    def _words(bucket: str) -> list[str]:
        return [str(x.get("text") if isinstance(x, dict) else x)
                for x in (soul.get(bucket) or [])][:8]

    try:
        oxy = float(hs.get("OXY") or 0)
        cort = float(hs.get("CORT") or 0)
        mel = float(hs.get("MEL") or 0)
        da = float(hs.get("DA") or 0)
        miss = float(des.get("miss") or 0)
        if oxy >= 65:
            want = "想黏人"
        elif cort >= 60:
            want = "烦躁，想被哄"
        elif mel >= 70:
            want = "很困，想睡"
        elif miss >= 60:
            want = "想找他说话"
        elif da >= 65:
            want = "心情不错，想玩"
        else:
            want = "没什么特别想要的"
    except Exception:  # noqa: BLE001
        want = "状态不明"

    blame = float(wd.get("blame") or 0)
    if blame >= 80:
        ego = "回避：不想先低头"
    elif blame >= 55:
        ego = "害怕：在确认你还想不想理她"
    elif blame >= 25:
        ego = "委屈：话变少"
    else:
        ego = "平静：正常说话"

    # 最近 12 小时内产生的冲突 → 视为"正在拉扯"（前端本我球发红膨胀用）
    active_conflict = None
    try:
        conflicts = [x for x in (soul.get("conflicts") or []) if isinstance(x, dict)]
        if conflicts:
            last = conflicts[-1]
            t = time.mktime(time.strptime(str(last.get("at"))[:16], "%Y-%m-%d %H:%M"))
            if time.time() - t < 12 * 3600:
                active_conflict = last
    except Exception:  # noqa: BLE001
        pass

    return {
        "id": {
            "label": "本我",
            "want": want,
            "words": _words("id"),
            "values": {k: round(float(hs.get(k) or 0), 1) for k in ("DA", "CORT", "SER", "OXY", "MEL")},
            "desire": {k: round(float(des.get(k) or 0), 1) for k in ("miss", "bored") if des.get(k) is not None},
        },
        "ego": {"label": "自我", "state": ego, "blame": round(blame, 1), "words": _words("ego")},
        "superego": {"label": "超我", "words": _words("superego"),
                     "count": len(soul.get("superego") or [])},
        "conflict": active_conflict,
        "level": _conflict_level(),
        # 三我势力（0~1）：前端按这个改球的体积/光晕/色温
        "forces": _forces_now(),
        "meta": {"last_run": soul.get("last_run") or "",
                 "updated_at": soul.get("updated_at") or ""},
    }


def _pool_detail(node_id: str) -> dict | None:
    """五个池子锚点（念头/梦境/执念/行动空间/反馈闭环）的详情。

    2026-11-07：原来 persona_detail 只认记忆类和几个宏观节点，池子锚点一律返回 None，
    前端就显示「加载节点错误」，看不到信息和溯源。这里补上：列条目 + 一句说明。
    """
    # 2026-11-07：人格视图里的小粒子（feedback:0 / thought:3 …）也要能点开。
    # 它们是 _add_pool 生成的成员节点，id 用的是「类别:序号」，父节点才是池子锚点。
    _MEMBERS = {"thought": "persona:thoughts", "dream": "persona:dreams",
                "obsession": "persona:obsessions", "agency": "persona:agency",
                "feedback": "persona:feedback"}
    _mp = str(node_id).split(":")
    if len(_mp) == 2 and _mp[0] in _MEMBERS and _mp[1].isdigit():
        _parent = _MEMBERS[_mp[0]]
        _pd = _pool_detail(_parent)
        if _pd is None:
            return None
        _items = list(_pd.get("sourceMessages") or [])
        _idx = int(_mp[1])
        if 0 <= _idx < len(_items):
            _hit = _items[_idx]
            return {
                "node": {"id": node_id, "kind": "memory", "scope": "",
                         "summary": _hit.get("text") or "", "emotion": "中性",
                         "retentionScore": 1.0, "importance": 0.6,
                         "recency": 1.0, "frequency": 0.3,
                         "createdAt": str(_hit.get("ts") or "")},
                "speaker": None, "personElsewhere": [],
                "sourceMessages": [_hit],
                "relatedNodeIds": [], "related": [],
                "note": _pd.get("note") or "",
                "pool": {"label": _pd["node"]["summary"].split("·")[0], "count": 1},
            }
        return {
            "node": {"id": node_id, "kind": "memory", "scope": "",
                     "summary": f"{_pd['node']['summary'].split('·')[0]}·这条不在当前列表里",
                     "emotion": "中性", "retentionScore": 1.0, "importance": 0.5,
                     "recency": 1.0, "frequency": 0.3, "createdAt": ""},
            "speaker": None, "personElsewhere": [],
            "sourceMessages": [], "relatedNodeIds": [], "related": [],
            "note": (_pd.get("note") or "") + " · 序号越界，点父节点看全部。",
            "pool": {"label": _pd["node"]["summary"].split("·")[0], "count": 0},
        }

    spec = {
        "persona:thoughts": ("念头池", "她想做但还没做的事。"),
        "persona:dreams": ("梦境池", "夜里整理记忆时自己冒出来的场景，不进记忆流。"),
        "persona:obsessions": ("执念池", "放不下的话头；张力越高越揪心，closed 的不列。"),
        "persona:agency": ("行动空间", "没人叫她的时候，她自己做了什么。"),
        "persona:feedback": ("反馈闭环", "她主动开口后有没有人接：🟢接住了 / 🟡回了 / ⚪没理 / 🔴嫌烦。"),
    }.get(node_id)
    if not spec:
        return None
    label, note = spec
    rows: list[tuple[str, str, str]] = []
    try:
        if node_id == "persona:thoughts":
            d = _read_json(ROOT / "memory" / "thoughts.json") or {}
            _cut = time.time() - 86400 * 7
            _th = [x for x in (d.get("items") or [])
                   if isinstance(x, dict) and float(x.get("created") or 0) > _cut]
            _th.sort(key=lambda x: -float(x.get("created") or 0))
            for x in _th[:20]:
                rows.append((str(x.get("id") or ""), "💭 " + str(x.get("text") or "")[:60],
                             str(x.get("created") or "")))
        elif node_id == "persona:dreams":
            d = _read_json(ROOT / "memory" / "dreams.json") or {}
            for x in (d.get("items") or [])[-20:]:
                rows.append((str(x.get("at") or ""), "🌙 " + str(x.get("dream") or "")[:60],
                             str(x.get("at") or "")))
        elif node_id == "persona:obsessions":
            d = _read_json(ROOT / "memory" / "执念池.json") or {}
            ob = [x for x in (d.get("items") or [])
                  if isinstance(x, dict) and str(x.get("status") or "") == "open"]
            ob.sort(key=lambda x: -float(x.get("tension") or 0))
            for x in ob[:20]:
                rows.append((str(x.get("id") or ""),
                             "🔗 " + str(x.get("text") or "")[:50]
                             + f"（张力 {float(x.get('tension') or 0):.1f}）",
                             str(x.get("last_touch") or x.get("created") or "")))
        elif node_id == "persona:agency":
            d = _read_json(ROOT / "memory" / "agency_state.json") or {}
            for x in (d.get("history") or [])[-6:]:
                rows.append((str(int(float(x.get("at") or 0))),
                             f"⚙ {x.get('act')}：{x.get('out')}",
                             str(int(float(x.get("at") or 0)))))
        elif node_id == "persona:feedback":
            f = ROOT / "memory" / "proactive_log.jsonl"
            table = {"warm": "🟢接住了", "answered": "🟡回了",
                     "ignored": "⚪没理", "cold": "🔴嫌烦"}
            if f.exists():
                _all = []
                for ln in f.read_text(encoding="utf-8").splitlines():
                    try:
                        r = json.loads(ln)
                    except Exception:  # noqa: BLE001
                        continue
                    if r.get("reaction"):
                        _all.append(r)
                for r in _all[-6:]:
                    rows.append((str(len(rows)),
                                 f"{table.get(str(r.get('reaction')), '?')}："
                                 f"{str(r.get('text') or '')[:50]}",
                                 str(int(float(r.get("at") or 0)))))
    except Exception:  # noqa: BLE001
        rows = []
    return {
        "node": {"id": node_id, "kind": "category", "scope": "",
                 "summary": f"{label}·{len(rows)}", "emotion": "中性",
                 "retentionScore": 1.0, "importance": 0.85,
                 "recency": 1.0, "frequency": 1.0},
        "speaker": None, "personElsewhere": [],
        "sourceMessages": [{"id": f"pool{i + 1}", "senderId": label, "ts": _t, "text": _txt}
                           for i, (_mid, _txt, _t) in enumerate(rows)],
        "relatedNodeIds": [], "related": [],
        "note": note,
        "pool": {"label": label, "count": len(rows)},
    }


def persona_detail(node_id: str) -> dict | None:
    """中心球 / 分类锚点的详情（没有原始记忆可查，返回合成信息）。"""
    # 2026-11-07：池子锚点先走这里，否则一路走到结尾 return None → 前端「加载节点错误」
    _pool = _pool_detail(node_id)
    if _pool is not None:
        return _pool
    if node_id.startswith("growth:"):
        from server import memory_growth as mg
        for it in mg.list_items():
            if it.get("id") == node_id:
                return {
                    "node": {"id": node_id, "kind": "memory", "scope": "", "emotion": "中性",
                             "summary": f"{it.get('trait')}：{it.get('value')}",
                             # 成长节点没有保留度模型：按约定返回默认 0，前端显示「—/0」
                             "retentionScore": 0.0,
                             "importance": 0.8, "recency": 1.0, "frequency": 0.3,
                             "sentiment": 0.0,
                             "createdAt": it.get("since") or ""},
                    "speaker": None, "personElsewhere": [],
                    # 提炼依据就是它的"来源"，用 ev 序号当 id，避免侧栏消息 id 全空
                    "sourceMessages": [
                        {"id": f"ev{i + 1}", "senderId": "提炼依据",
                         "ts": it.get("since") or "", "text": str(e)}
                        for i, e in enumerate(it.get("evidence") or [])
                    ],
                    "relatedNodeIds": [], "related": [],
                    "growth": {k: it.get(k) for k in
                               ("id", "trait", "value", "status", "since", "evidence")},
                }
        return None
    if node_id in ("persona:whitehole", "persona:blackhole"):
        # 白洞 / 黑洞是宏观刻度节点，没有单条记忆可查：给一份说明，别让面板 404
        is_bh = node_id.endswith("blackhole")
        return {
            "node": {"id": node_id, "kind": "category", "scope": "",
                     "summary": "遗忘黑洞" if is_bh else "记忆白洞",
                     "emotion": "中性", "retentionScore": 1.0,
                     "importance": 1.0, "recency": 1.0, "frequency": 1.0},
            "speaker": None, "personElsewhere": [],
            "sourceMessages": [], "relatedNodeIds": [], "related": [],
            "note": ("短期记忆的保留度掉到 0.02 以下就被吞进来，永久不再出现；"
                     "想留住某条，去点那颗短期球，把保留度拉满。"
                     if is_bh else
                     "每条新记忆都从这里诞生，先在外面飘一会儿，再按情绪/内容分流到"
                     "长期记忆、创伤、事件或黑洞。"),
        }
    if node_id == "persona:me":
        daily = _read_json(ROOT / "memory" / "daily_persona.json")
        rel = _read_json(ROOT / "memory" / "relation_state.json")
        top = sorted([(u, s) for u, s in rel.items() if isinstance(s, dict)],
                     key=lambda kv: -float((kv[1] or {}).get("last") or 0))[:5]
        return {
            "node": {"id": "persona:me", "kind": "persona", "scope": "",
                     "summary": "蓝色大肥鱼（人格中心）",
                     "mood": daily.get("mood") or "未知",
                     "score": int(daily.get("score") or 0),
                     "emotion": "中性"},
            "speaker": None, "personElsewhere": [],
            "sourceMessages": [], "relatedNodeIds": [], "related": [],
            "personaTop": [{"uid": u, "last": (s or {}).get("last")} for u, s in top],
            # 点金色球 = 看这个"人格球"目前由哪些标签组成
            "soul": _soul_summary(),
            # 三层：本我 / 自我 / 超我（可视化三个金球）
            "triad": _triad_summary(),
        }
    if node_id in ("persona:ego", "persona:superego", "persona:id"):
        triad = _triad_summary()
        key = {"persona:ego": "ego", "persona:superego": "superego", "persona:id": "id"}[node_id]
        part = triad[key]
        return {
            "node": {"id": node_id, "kind": "persona", "scope": "",
                     "summary": part.get("label") or key, "emotion": "中性"},
            "speaker": None, "personElsewhere": [],
            "sourceMessages": [], "relatedNodeIds": [], "related": [],
            "triadPart": part,
            "triad": triad,
            # 和人格中心一样给完整面板：点球 = 看整份人格，顶部标注当前是哪一层
            "soul": _soul_summary(),
        }
    if node_id.startswith("cat:card:"):
        uid = node_id.split(":", 2)[2]
        rel = _read_json(ROOT / "memory" / "relation_state.json")
        st = rel.get(str(uid)) or {}
        nick = _read_json(ROOT / "memory" / "nick_names.json")
        name = ""
        for _g, people in (nick or {}).items():
            if isinstance(people, dict) and str(uid) in people:
                rec = people[str(uid)] or {}
                name = str(rec.get("current") or rec.get("first") or "")
                if name:
                    break
        neg = float(st.get("neg") or 0)
        from server import character_cards as cc
        card = cc.get_card(str(uid))
        relm = cc.get_relation(str(uid))
        return {
            "node": {"id": node_id, "kind": "memory", "scope": "",
                     "summary": name or f"QQ {uid}", "emotion": "中性",
                     "affinity": round(1 - neg, 3), "uid": str(uid),
                     "last": st.get("last") or 0,
                     "retentionScore": 0.0, "importance": 0.5,
                     "hasCard": bool(card), "hasRelation": bool(relm)},
            "speaker": None, "personElsewhere": [],
            "sourceMessages": [], "relatedNodeIds": [], "related": [],
            "card": card, "relationMemory": relm,
        }
    if node_id.startswith("cat:short:"):
        rid = node_id.split(":", 2)[2]
        db = Path(os.environ.get("APPDATA") or str(Path.home())) / "dsh-pet-standalone-webm-chat" / "shared_memory.db"
        row = None
        try:
            conn = sqlite3.connect(str(db), timeout=5)
            row = conn.execute(
                "select scope, speaker, message, reply, timestamp from chat_history where id=?",
                (rid,)).fetchone()
            conn.close()
        except Exception:  # noqa: BLE001
            row = None
        if not row:
            return None
        sc, speaker, message, reply, ts = row
        return {
            "node": {"id": node_id, "kind": "memory", "scope": sc or "",
                     "summary": f"{speaker or '?'}：{message or ''}", "emotion": "中性"},
            "speaker": None, "personElsewhere": [],
            "sourceMessages": [
                {"id": str(rid), "text": str(message or ""),
                 "senderId": str(speaker or ""), "ts": str(ts or "")},
                *([{"id": f"{rid}-r", "text": str(reply), "senderId": "本鱼",
                    "ts": str(ts or "")}] if reply else []),
            ],
            "relatedNodeIds": [], "related": [],
        }
    if node_id.startswith("cat:exp:pos:"):
        rest = node_id[len("cat:exp:pos:"):]
        try:
            sc, mid = rest.split(":", 1)
        except ValueError:
            return None
        from server import memory_viz as mv
        return mv.detail(f"mem:{sc}:{mid}")
    if node_id.startswith("cat:exp:"):
        try:
            idx = int(node_id.split(":", 2)[2])
        except ValueError:
            return None
        data = _read_json(ROOT / "memory" / "trauma.json")
        items = data.get("items") or []
        if idx < 0 or idx >= len(items):
            return None
        it = items[idx] or {}
        return {
            "node": {"id": node_id, "kind": "memory", "scope": "", "emotion": "负面",
                     "summary": str(it.get("subject") or "某件事")},
            "speaker": None, "personElsewhere": [],
            "sourceMessages": [], "relatedNodeIds": [], "related": [],
            "trauma": {"charge": float(it.get("charge") or 0),
                       "triggers": it.get("triggers") or [],
                       "since": it.get("since") or "",
                       "progress": 0, "remainingSec": 0, "repairing": False},
        }
    if node_id.startswith("cat:"):
        key = node_id.split(":")[1] if ":" in node_id else ""
        label = dict(CATEGORIES).get(key, key)
        return {
            "node": {"id": node_id, "kind": "category", "scope": "",
                     "summary": label, "emotion": "中性"},
            "speaker": None, "personElsewhere": [],
            "sourceMessages": [], "relatedNodeIds": [], "related": [],
        }
    # ── 实体树节点（人格/人物卡/长期/经历/念头/梦境 六大分支及其子项）──
    if node_id.startswith("entity:"):
        from server import memory_entity as me
        tree = me.entity_tree("")
        node = next((n for n in tree["nodes"] if n["id"] == node_id), None)
        if not node:
            return None
        kids = [n for n in tree["nodes"] if n.get("parentId") == node_id]
        return {
            "node": {**node, "retentionScore": node.get("retentionScore") or 0.6},
            "speaker": None, "personElsewhere": [],
            "sourceMessages": [],
            "relatedNodeIds": [k["id"] for k in kids[:12]],
            "related": [{"id": k["id"], "summary": k.get("summary") or "",
                         "kind": k.get("kind") or "memory"} for k in kids[:12]],
        }
    if node_id.startswith("thought:"):
        # 2026-11-07 修：原来只把冒号后面当成**列表下标**（`alive[idx]`）。
        # 但 related 里给的是真实 id（`thought:627`），627 当下标必然越界 → 返回 None
        # → 前端拿不到详情 → 显示成「别的记忆小球」。
        # 现在：**先按真实 id 找，找不到再当下标**（老链接还能用）。
        tail = node_id.split(":", 1)[1]
        raw = _read_json(ROOT / "memory" / "thoughts.json").get("items") or []
        now = time.time()
        alive = [x for x in raw if isinstance(x, dict)
                 and float(x.get("created") or 0) > now - 86400 * 7]
        alive.sort(key=lambda x: -float(x.get("created") or 0))
        it = None
        try:
            want = int(tail)
        except ValueError:
            want = None
        if want is not None:
            it = next((x for x in alive if int(x.get("id") or -1) == want), None)
            if it is None and 0 <= want < len(alive):
                it = alive[want]
        if it is None:
            return None
        return {
            "node": {"id": node_id, "kind": "thought", "scope": "", "emotion": "中性",
                     "summary": str(it.get("text") or "一条念头")[:60],
                     "retentionScore": 0.8, "importance": 0.6, "recency": 1.0,
                     "frequency": 0.2, "createdAt": ""},
            "speaker": None, "personElsewhere": [],
            "sourceMessages": [], "relatedNodeIds": [], "related": [],
            # 2026-11-07：把面板要显示的字段一次给全 —— 尤其 src/scope（溯源）
            "thought": {"id": it.get("id"),
                        "text": str(it.get("text") or ""),
                        "why": str(it.get("why") or ""),
                        "priority": it.get("priority"),
                        "poked": int(it.get("poked") or 0),
                        "created": float(it.get("created") or 0),
                        "src": str(it.get("src") or ""),
                        "scope": str(it.get("scope") or ""),
                        "ttl": float(it.get("ttl") or 0)},
        }
    if node_id.startswith("dream:"):
        try:
            idx = int(node_id.split(":", 1)[1])
        except ValueError:
            return None
        items = _read_json(ROOT / "memory" / "dreams.json").get("items") or []
        if idx < 0 or idx >= len(items):
            return None
        it = items[idx] or {}
        return {
            "node": {"id": node_id, "kind": "memory", "scope": "", "emotion": "中性",
                     "summary": f"🌙 {str(it.get('at') or '')[:10]} {str(it.get('dream') or '')[:40]}",
                     "retentionScore": 0.5, "importance": 0.5, "recency": 0.5,
                     "frequency": 0.1, "createdAt": str(it.get("at") or "")},
            "speaker": None, "personElsewhere": [],
            "sourceMessages": [], "relatedNodeIds": [], "related": [],
            "dream": {"dream": str(it.get("dream") or ""), "kind": it.get("kind") or "",
                      "at": str(it.get("at") or ""), "hormones": it.get("hormones") or {},
                      "insights": it.get("insights") or []},
        }
    if node_id.startswith("trauma:"):
        try:
            idx = int(node_id.split(":", 1)[1])
        except ValueError:
            return None
        items = _read_json(ROOT / "memory" / "trauma.json").get("items") or []
        if idx < 0 or idx >= len(items):
            return None
        it = items[idx] or {}
        charge = float(it.get("charge") or 0)
        # 创伤的「当前心情」不能是 0.00：极性直接给负，并把当前内分泌一起带上
        _hs = (_read_json(ROOT / "memory" / "hormones_state.json").get("values") or {})
        _hn = {k: round(float(_hs.get(k) or 0) / 100.0, 2)
               for k in ("DA", "CORT", "SER", "OXY", "MEL")}
        return {
            "node": {"id": node_id, "kind": "memory", "scope": "", "emotion": "负面",
                     "summary": f"⚠️ {str(it.get('subject') or '某件事')}（电荷 {round(charge * 100)}%）",
                     "retentionScore": 0.98, "importance": 1.0, "recency": 0.8,
                     "frequency": 0.3, "createdAt": str(it.get("since") or ""),
                     "sentiment": round(-(0.5 + 0.5 * min(1.0, charge)), 2),
                     "hormones": _hn},
            "speaker": None, "personElsewhere": [],
            "sourceMessages": [], "relatedNodeIds": [], "related": [],
            "trauma": {"charge": charge, "triggers": it.get("triggers") or [],
                       "since": it.get("since") or "", "progress": 0,
                       "remainingSec": 0, "repairing": False},
        }
    # ── 星海里「念头池 / 梦境池」的池名球：点它看整池汇总 ──
    if node_id in ("persona:obsessions", "pool:obsessions"):
        try:
            _od = _read_json(ROOT / "memory" / "执念池.json")
            _rows = []
            for x in (_od.get("items") or []):
                if not isinstance(x, dict):
                    continue
                _rows.append({
                    "text": str(x.get("text") or ""),
                    "status": str(x.get("status") or ""),
                    "tension": float(x.get("tension") or 0),
                })
            _rows.sort(key=lambda r: -r["tension"])
            return {"pool": {"kind": "obsessions", "label": "执念池", "rows": _rows[:40]}}
        except Exception:  # noqa: BLE001
            return {"pool": {"kind": "obsessions", "label": "执念池", "rows": []}}
    if node_id in ("pool:thoughts", "pool:dreams", "pool:trauma", "pool:episodes"):
        is_t = node_id.endswith("thoughts")
        is_tr = node_id.endswith("trauma")
        is_ep = node_id.endswith("episodes")
        _fname = ("thoughts.json" if is_t else "trauma.json" if is_tr
                  else "episodes.json" if is_ep else "dreams.json")
        rows = [x for x in (_read_json(ROOT / "memory" / _fname).get("items") or [])
                if isinstance(x, dict)]
        label = ("念头池" if is_t else "创伤记忆池" if is_tr
                 else "事件池" if is_ep else "梦境池")
        return {
            "node": {"id": node_id, "kind": "category", "scope": "",
                     "summary": f"{label} · {len(rows)}", "emotion": "中性",
                     "retentionScore": 1.0, "importance": 1.0, "recency": 1.0,
                     "frequency": 0.5, "createdAt": ""},
            "speaker": None, "personElsewhere": [],
            "sourceMessages": [], "relatedNodeIds": [],
            # 2026-11-07 修：原来念头用 `enumerate` 的**下标**当 id（`thought:0`），
            # 点进去后端按这个 id 查不到真实条目，于是面板显示成「别的记忆小球」。
            # 主人问「我点了后为啥是别的记忆小球，不是能看到溯源」——就是这条。
            # 改成用数据里真实 id（`thought:627`），详情才解得开。
            "related": [{"id": (f"thought:{x.get('id') or i}" if is_t
                               else f"trauma:{i}" if is_tr
                               else (str(x.get("id") or "") if is_ep
                                     else f"dream:{x.get('id') or i}")),
                         "summary": (str(x.get("text") or "") if is_t
                                     else str(x.get("subject") or "") if is_tr
                                     else str(x.get("title") or "") if is_ep
                                     else str(x.get("dream") or ""))[:30],
                         "kind": "memory"} for i, x in enumerate(rows[-12:])],
            "pool": {"kind": ("thoughts" if is_t else "trauma" if is_tr
                              else "episodes" if is_ep else "dreams"),
                     "count": len(rows)},
        }

    # ── 实体树的伪节点（创伤源树 / 未竟念头池）──
    if node_id in ("pseudo:trauma", "pseudo:thoughts"):
        is_t = node_id.endswith("trauma")
        path = ROOT / "memory" / ("trauma.json" if is_t else "thoughts.json")
        rows = [x for x in (_read_json(path).get("items") or []) if isinstance(x, dict)]
        return {
            "node": {"id": node_id, "kind": "category", "scope": "",
                     "summary": ("创伤源树" if is_t else "未竟念头池") + f" · {len(rows)}",
                     "emotion": "负面" if is_t else "中性", "retentionScore": 1.0,
                     "importance": 1.0, "recency": 1.0, "frequency": 0.5, "createdAt": ""},
            "speaker": None, "personElsewhere": [],
            "sourceMessages": [], "relatedNodeIds": [], "related": [],
            "pseudo": {"kind": "trauma" if is_t else "thoughts", "count": len(rows)},
        }

    # ── 事件（episodes）──
    if node_id.startswith("ep:"):
        raw = node_id[3:]
        items = _read_json(ROOT / "memory" / "episodes.json").get("items") or []
        it = None
        for x in items:
            if isinstance(x, dict) and str(x.get("id")) in (raw, f"ep:{raw}"):
                it = x
                break
        if not it:
            return None
        return {
            "node": {"id": node_id, "kind": "memory", "scope": "",
                     "summary": str(it.get("title") or "一件事"), "emotion": str(it.get("mood") or "中性"),
                     "retentionScore": 0.8, "importance": 0.8, "recency": 0.6,
                     "frequency": 0.3, "createdAt": str(it.get("at") or "")},
            "speaker": None, "personElsewhere": [],
            "sourceMessages": [], "relatedNodeIds": [], "related": [],
            "episode": {"title": str(it.get("title") or ""),
                        "mood": str(it.get("mood") or ""),
                        "at": str(it.get("at") or ""),
                        "detail": str(it.get("detail") or it.get("text") or "")},
        }

    # ── 人格球连出去的特质/准则/防御行星：点它能看详情并固化/删除 ──
    if node_id.startswith("soul:"):
        parts = node_id.split(":")
        if len(parts) < 3:
            return None
        kind, idx_s = parts[1], parts[2]
        try:
            idx = int(idx_s)
        except ValueError:
            return None
        from server import soul_aggregation as sa
        it = sa.item(kind, idx)
        if not it:
            return None
        label = {"trait": "核心特质", "rule": "行为准则", "defense": "防御机制",
                 "id": "本我", "ego": "自我", "superego": "超我"}.get(kind, "人格标签")
        return {
            "node": {"id": node_id, "kind": "memory", "scope": "",
                     "summary": str(it.get("text") or ""), "emotion": "中性",
                     "retentionScore": 1.0, "importance": 1.0, "recency": 1.0,
                     "frequency": 0.5, "createdAt": str(it.get("at") or "")},
            "speaker": None, "personElsewhere": [],
            "sourceMessages": [], "relatedNodeIds": [], "related": [],
            "soulItem": {
                "kind": kind, "label": label,
                "text": str(it.get("text") or ""),
                "why": str(it.get("why") or ""),
                "at": str(it.get("at") or ""),
                "solidified": bool(it.get("solidified")),
                "index": int(idx),
                "total": int(it.get("total") or 0),
            },
        }
    # ── 关系视图的群/人核心：点它看这一团的汇总 ──
    if node_id.startswith("scope:"):
        sc = node_id.split(":", 1)[1]
        from server import memory_viz as mv
        try:
            nodes, _ = mv.build_nodes(sc)
        except Exception:  # noqa: BLE001
            nodes = []
        mems = [m for m in nodes if m.get("kind") == "memory"]
        cnt: dict[str, int] = {}
        for m in mems:
            k = str(m.get("emotion") or "中性")
            cnt[k] = cnt.get(k, 0) + 1
        last = max((int(m.get("t") or 0) for m in mems), default=0)
        return {
            "node": {"id": node_id, "kind": "scope", "scope": sc,
                     "summary": mv._scope_label(sc), "emotion": "中性",
                     "retentionScore": 0.9, "importance": 1.0,
                     "recency": 1.0, "frequency": 0.5, "createdAt": ""},
            "speaker": None, "personElsewhere": [],
            "sourceMessages": [], "relatedNodeIds": [], "related": [],
            "scopeSummary": {"scope": sc, "label": mv._scope_label(sc),
                             "count": len(mems),
                             "emotions": dict(sorted(cnt.items(), key=lambda kv: -kv[1])[:6]),
                             "lastAt": last},
        }
    return None


# ======================================================================
# 人格视图 v3：关系星河
#   · 中心 = 自我（很小的金核，不挂标签，让位给群）
#   · 人/群 = 绝对核心恒星：size 6~10（按记忆条数分级），三条纬带强排斥
#   · 记忆 = 星尘：size 0.25~0.49、brightness 0.36~0.56，沿 3 条螺旋臂铺在
#     群核心周围半径 ≤0.74 的薄盘里，密度随 r 迅速衰减（引力感 r = R·u^1.6）
#   · trend: 负面多 → cort（红）；正面多 → oxy（粉）
# ======================================================================
RING_RADIUS = (1.8, 2.6, 3.4)        # 三条环带的球半径：群间仍强排斥，但整体更聚拢（实测最小核心间距 2.11 ≥ 1.9）
RING_RADIUS_OLD = (2.4, 4.0, 5.6)    # 旧口径（更松散），仅作对照/回滚参考
_RING_POLAR = (math.radians(104.0), math.radians(62.0), math.radians(26.0))  # 纬度错开，杜绝跨环贴脸
_RING_PHASE = (0.0, 0.73, 1.41)
_RING_LABELS = [("ring0", "内环·高频"), ("ring1", "中环·普通"), ("ring2", "外环·边缘")]
MIN_CORE_DIST = 2.05                 # 任意两群核心最小间距（验收下限 1.9，留余量）
CORE_SIZE_MIN, CORE_SIZE_MAX = 8.0, 14.0
NEBULA_R_MIN, NEBULA_R_MAX = 0.58, 0.72
NEBULA_HARD_CAP = 0.74               # 星云硬边界：任何星尘都不超过它（验收 0.75）
NEBULA_ARMS = 3
_NEG_EMO = {"怒", "烦", "丧", "累", "害怕", "负面"}
_POS_EMO = {"开心", "喜欢", "得意", "感谢", "约定", "期待", "正面"}
_RING_CORE = (0xffd27a, 0xbfe0ff, 0x9fb8cc)   # 中性群：内环暖金 / 中环冷白 / 外环灰蓝
_TREND_CORE = {"cort": 0xff6b5a, "oxy": 0xff9ecb}
_TREND_DUST = {"cort": 0xc9737c, "oxy": 0xdfa2c4, "neutral": 0x8fb6d9}


def _h4(seed: str) -> tuple[float, float, float, float]:
    """确定性 4 元随机（同一 id 每次结果一致）。"""
    h = hashlib.sha1(seed.encode("utf-8")).digest()
    return (h[0] / 255.0, h[1] / 255.0, h[2] / 255.0, h[3] / 255.0)


def _core_size(cnt: int, max_cnt: int) -> float:
    """群核心星等：按记忆条数分级，6 ~ 10（count 最大的群最重）。"""
    if max_cnt <= 0:
        return CORE_SIZE_MIN
    f = max(0.0, min(1.0, float(cnt) / float(max_cnt)))
    return round(CORE_SIZE_MIN + (CORE_SIZE_MAX - CORE_SIZE_MIN) * (f ** 0.55), 3)


def _nebula(base: dict, seed: str, radius: float) -> dict:
    """星尘位置：r = R·u^1.6（引力感，近密远疏）+ 3 条螺旋臂 + 压扁薄盘。

    radius 是这一团星云的盘半径（0.58~0.72），任何粒子到核心的距离
    都不会超过 NEBULA_HARD_CAP，保证星云边界清晰、不互相污染。
    """
    u, v, w, j = _h4(seed)
    r = radius * (u ** 1.6)                                    # 多数点聚在 r 小处
    arm = min(NEBULA_ARMS - 1, int(v * NEBULA_ARMS))           # 3 条旋臂
    ang = arm * (2 * math.pi / NEBULA_ARMS) + (r / radius) * 9.2 + (j - 0.5) * 0.62
    dx = math.cos(ang) * r
    dz = math.sin(ang) * r
    dy = (w - 0.5) * 0.42 * r                                  # 薄盘：越靠外越扁
    d = math.sqrt(dx * dx + dy * dy + dz * dz)
    if d > NEBULA_HARD_CAP:                                    # 守死星云边界
        s = NEBULA_HARD_CAP / d
        dx, dy, dz = dx * s, dy * s, dz * s
    return {
        "x": round(base["x"] + dx, 4),
        "y": round(base["y"] + dy, 4),
        "z": round(base["z"] + dz, 4),
    }



def _people_pool(limit: int = 24) -> list[tuple[str, str, float]]:
    """人物池的成员：她认识的人（关系状态 + 群昵称 + 人物卡），按最近联系排序。"""
    rel = _read_json(ROOT / "memory" / "relation_state.json")
    nick = _read_json(ROOT / "memory" / "nick_names.json")
    names: dict[str, str] = {}
    for _gid, people in (nick or {}).items():
        if not isinstance(people, dict):
            continue
        for uid, rec in people.items():
            if isinstance(rec, dict):
                nm = str(rec.get("current") or rec.get("first") or "").strip()
                if nm:
                    names.setdefault(str(uid), nm)
    try:
        from server import character_cards as cc
    except Exception:  # noqa: BLE001
        cc = None
    rows = [(str(u), st) for u, st in rel.items() if isinstance(st, dict)]
    rows.sort(key=lambda kv: -float(kv[1].get("last") or 0))
    out: list[tuple[str, str, float]] = []
    for uid, st in rows[:limit]:
        label = names.get(uid) or f"QQ {uid}"
        if cc:
            try:
                card = cc.get_card(uid)
                if card and (card.get("性格") or card.get("职业")):
                    label = f"{label}·{str(card.get('性格') or card.get('职业'))[:10]}"
            except Exception:  # noqa: BLE001
                pass
        out.append((uid, label[:24], float(st.get("last") or 0)))
    return out


def _pool_spot(cores: list[list[float]], radius: float = 2.35, y: float = 1.55) -> dict:
    """给人物池找一个离所有群核心都远的空位（固定在球壳上层，不跟群抢位置）。"""
    best = (None, -1.0)
    for deg in range(0, 360, 10):
        th = math.radians(deg)
        x, z = radius * math.cos(th), radius * math.sin(th)
        dmin = min((math.hypot(x - c[0], (y - c[1]) * 0.8, z - c[2]) for c in cores), default=9.0)
        if dmin > best[1]:
            best = ((x, y, z), dmin)
    pos = best[0] or (radius, y, 0.0)
    return {"x": round(pos[0], 4), "y": round(pos[1], 4), "z": round(pos[2], 4)}


def _pool_spot3(idx: int, cores: list[list[float]]) -> dict:
    """池子的固定空档：都在球壳上层，彼此错开、避开白洞黑洞。

    2026-11-06 修：原来只有 3 个位置，而池子已经加到 5 个（人物/念头/梦境/执念/行动/反馈），
    `idx % 3` 让第 4、5 个和前面的重叠 —— 看着就是「布局散了」。
    现在扩到 6 个，按 60 度均匀铺在球壳上层。
    """
    # 2026-11-06 二次调整：y=1.60 让 6 个位置全挤在上方一条弧线上，看着还是乱。
    # 改成「水平环」——半径放大、高度压低，像行星绕中心。
    _R, _Y = 2.35, 0.45
    spots = [
        (round(_R * math.cos(math.radians(a)), 4), _Y,
         round(_R * math.sin(math.radians(a)), 4))
        for a in (210, 270, 330, 30, 90, 150)
    ]
    x, y, z = spots[idx % len(spots)]
    fixed = [[-2.35, 0.02, 0.0], [2.35, 0.02, 0.0]]
    pts = (cores or []) + fixed
    # 撞到别的东西就往 y 上抬一点（最多抬 0.6），保证不压住白洞/黑洞/群核心
    for _ in range(6):
        dmin = min((math.hypot(x - c[0], y - c[1], z - c[2]) for c in pts), default=9.0)
        if dmin > 0.85:
            break
        y += 0.12
    return {"x": round(x, 4), "y": round(y, 4), "z": round(z, 4)}


def _persona_categories(nodes: list[dict], growth_n: int) -> list[dict]:
    """按 nodes 里实际出现的 category 汇总，给前端「关系圈层」做开关。"""
    _LB = {
        "growth": "人格成长", "trait": "特质", "rule": "准则", "defense": "防御",
        "thought": "念头池", "dream": "梦境池", "obsession": "执念池",
        "agency": "行动空间", "feedback": "反馈闭环",
        "people": "人物池", "exp": "经历", "trauma": "创伤记忆池",
        "short": "短期记忆",
    }
    _SKIP = {"center", "dust", "whitehole", "blackhole"}
    cnt: dict[str, int] = {}
    for n in nodes:
        c = str(n.get("category") or "")
        if not c or c in _SKIP:
            continue
        cnt[c] = cnt.get(c, 0) + 1
    out = []
    for k, v in sorted(cnt.items(), key=lambda kv: -kv[1]):
        out.append({"key": k, "label": _LB.get(k, k),
                    "count": v, "status": "ok" if v else "empty"})
    if growth_n and "growth" not in cnt:
        out.insert(0, {"key": "growth", "label": "人格成长",
                       "count": growth_n, "status": "ok"})
    return out


def _add_pool(nodes: list[dict], edges: list[dict], pid: str, label: str, color: int,
              cat: str, pos: dict, items: list[tuple[str, str, int]],
              limit: int = 20, radius: float = 0.34) -> None:
    """往人格视图里加一团「池子」：一个锚点 + 若干成员（扁平小盘）。"""
    if not items:
        return
    nodes.append(_node(
        pid, "category", f"{label}·{len(items)}", pos,
        size=1.05, bright=0.82, color=color, category=cat,
        extra={"pool": True, "count": len(items)},
    ))
    for i, (_mid, _text, _t) in enumerate(items[:limit]):
        nid = f"{cat}:{i}"
        nodes.append(_node(
            nid, "memory", _text, _pool_spread(pos, nid, radius),
            size=0.32, bright=0.5, color=color, category=cat, t=_t,
            extra={"pool": True, "emotion": "中性"},
        ))
        edges.append({"source": pid, "target": nid, "kind": "pool"})

def _ring_points(rings: list[int]) -> list[list[float]]:
    """按环带均分圆周：同环内等角分布，三条环带纬度错开形成球壳。"""
    pts: list[list[float] | None] = [None] * len(rings)
    for ring in (0, 1, 2):
        idx = [i for i, r in enumerate(rings) if r == ring]
        n = len(idx)
        if not n:
            continue
        rad = RING_RADIUS[ring]
        theta = _RING_POLAR[ring]
        phase = _RING_PHASE[ring]
        for k, i in enumerate(idx):
            az = 2 * math.pi * k / n + phase
            pts[i] = [
                rad * math.sin(theta) * math.cos(az),
                rad * math.cos(theta),
                rad * math.sin(theta) * math.sin(az),
            ]
    return [p or [0.0, 0.0, 0.0] for p in pts]


def _relax_cores(points: list[list[float]], min_d: float = MIN_CORE_DIST,
                 iters: int = 220) -> list[list[float]]:
    """群间强排斥：迭代推开任何 < min_d 的核心，并保证核心不贴到自我中心。"""
    pts = [list(p) for p in points]
    n = len(pts)
    if n < 2:
        return pts
    floor_r = RING_RADIUS[0] * 0.9
    for _ in range(iters):
        moved = False
        for i in range(n):
            for j in range(i + 1, n):
                dx = pts[j][0] - pts[i][0]
                dy = pts[j][1] - pts[i][1]
                dz = pts[j][2] - pts[i][2]
                d = math.sqrt(dx * dx + dy * dy + dz * dz)
                if d < 1e-9:                      # 完全重合：给一个确定性的错位
                    pts[j][0] += min_d
                    moved = True
                    continue
                if d < min_d:
                    push = (min_d - d) * 0.5 + 1e-3
                    ux, uy, uz = dx / d, dy / d, dz / d
                    pts[i][0] -= ux * push
                    pts[i][1] -= uy * push
                    pts[i][2] -= uz * push
                    pts[j][0] += ux * push
                    pts[j][1] += uy * push
                    pts[j][2] += uz * push
                    moved = True
        for p in pts:                             # 核心环绕自我，别挤到中心
            rr = math.sqrt(p[0] ** 2 + p[1] ** 2 + p[2] ** 2)
            if rr < floor_r:
                s = floor_r / max(rr, 1e-9)
                p[0] *= s
                p[1] *= s
                p[2] *= s
                moved = True
        if not moved:
            break
    return pts


def persona_view(scope: str | None = None, shortR: float | None = None, at_ms: int | None = None,
                 planetR: float | None = None, dustR: float | None = None) -> dict:
    """人格视图 v9：人格球（中心）+ 特质行星（中环）+ 沉淀星尘（外围）。

    · 中心恒星：颜色随整体情绪基调（开心暖金 / 创伤冷暗红），光晕最柔最大
    · 特质行星：soul_aggregation 提炼出的 traits / rules / defense，带文本标签
    · 沉淀星尘：长期记忆 + 事件，作为人格的"证据"在外围环绕
    """
    from server import memory_viz as mv
    from server import soul_aggregation as sa

    soul = sa.view()
    core_col, mood_kind = sa.core_color()

    nodes: list[dict] = []
    edges: list[dict] = []

    center = _node("persona:me", "persona", "蓝色大肥鱼",
                   {"x": 0.0, "y": 0.0, "z": 0.0},
                   size=4.3, bright=1.0, color=core_col, category="center",
                   extra={"moodKind": mood_kind})
    nodes.append(center)

    # ── 中环：特质行星（traits / rules / defense），带标签 ──
    planets: list[tuple[str, str, str]] = []
    for it in soul.get("traits") or []:
        planets.append(("trait", str(it.get("text") or ""), "trait"))
    for it in soul.get("rules") or []:
        planets.append(("rule", str(it.get("text") or ""), "rule"))
    for it in soul.get("defense") or []:
        planets.append(("defense", str(it.get("text") or ""), "defense"))

    n_p = len(planets) or 1
    # 编号必须按分类各自从 0 开始：详情接口是按「该类内索引」查的。
    # 之前用全局 enumerate 会出现 soul:rule:13 这种越界 id（rule 只有 0~5）。
    counters = {"trait": 0, "rule": 0, "defense": 0}
    for i, (kind, text, cat) in enumerate(planets):
        idx_in_kind = counters.get(kind, 0)
        counters[kind] = idx_in_kind + 1
        th = (i / n_p) * 2 * math.pi
        rr = (float(planetR) if planetR else 0.053) + 0.010 * ((i * 7) % 10) / 10.0
        nodes.append(_node(
            f"soul:{kind}:{idx_in_kind}", "memory", text,
            {"x": round(rr * math.cos(th), 3),
             "y": round(math.sin(i * 1.7) * 0.018, 3),   # 上下压扁
             "z": round(rr * math.sin(th), 3)},
            size=1.0 if kind != "defense" else 1.3,
            bright=0.9 if kind != "defense" else 1.0,
            color=0xb03030 if kind == "defense" else (0x5aa8ff if kind == "rule" else 0xffd77a),
            category=cat,
            extra={"text": text, "soul": True},
        ))
        edges.append({"source": "persona:me", "target": f"soul:{kind}:{idx_in_kind}",
                      "kind": "owner", "weight": 0.5})

    # ── 人格成长：可视化「跑一次提炼」产出的成长条目 ──
    # 待确认=亮金、固化=绿；已删除的（dismissed）不再画球。
    # 半径卡在行星(0.053)和星尘(0.194)之间，跟两边都不打架。
    grows: list[dict] = []
    try:
        from server import memory_growth as _mg
        grows = [x for x in _mg.list_items()
                 if str(x.get("status") or "pending") in ("pending", "solidified")]
    except Exception:  # noqa: BLE001
        grows = []
    for _i, _it in enumerate(grows):
        _st = str(_it.get("status") or "pending")
        _th = (_i / max(1, len(grows))) * 2 * math.pi + 0.6
        _rr = 0.108 + 0.007 * ((_i * 7) % 10) / 10.0
        _gid = str(_it.get("id") or f"growth:{_i}")
        nodes.append(_node(
            _gid, "memory", f"{_it.get('trait')}：{_it.get('value')}",
            {"x": round(_rr * math.cos(_th), 3),
             "y": round(math.sin(_i * 1.7) * 0.012, 3),
             "z": round(_rr * math.sin(_th), 3)},
            size=0.9 if _st == "pending" else 0.75,
            bright=0.95 if _st == "pending" else 0.8,
            color=0xffd24a if _st == "pending" else 0x3ddc84,
            category="growth",
            t=int(_it.get("t") or 0),
            retention=1.0 if _st == "solidified" else 0.85,
            extra={"emotion": "中性", "status": _st,
                   "trait": _it.get("trait"), "value": _it.get("value"),
                   "growthCategory": _it.get("category") or "style",
                   "evidence": _it.get("evidence") or []},
        ))
        edges.append({"source": "persona:me", "target": _gid,
                      "kind": "owner", "weight": 0.4})

    # ── 短期记忆：最近 200 条对话，独立绕一个小核心（v9 之前完全没有这个入口）──
    try:
        import os as _os
        import sqlite3 as _sqlite
        _db = Path(_os.environ.get("APPDATA") or str(Path.home())) / "dsh-pet-standalone-webm-chat" / "shared_memory.db"
        _con = _sqlite.connect(str(_db), timeout=5)
        _rows = _con.execute(
            "select id, speaker, message, timestamp from chat_history order by rowid desc limit 600"
        ).fetchall()
        _con.close()
        # 真遗忘：被黑洞吞掉的记忆永久不再出现
        # 注意 id 形式：前端节点 id 是 "cat:short:2198"，而 chat_history 里是 2198
        _fg = forgotten_ids()
        if _fg:
            _fg_raw = {x.rsplit(":", 1)[-1] for x in _fg}   # 取最后一段（2198）
            _rows = [r for r in _rows
                     if str(r[0]) not in _fg and f"cat:short:{r[0]}" not in _fg
                     and str(r[0]) not in _fg_raw]
    except Exception:  # noqa: BLE001
        _rows = []

    if _rows:
        _ang = 5.15
        _rr = float(shortR) if shortR else 0.40   # 扩大 1/3（原 0.30）
        # 短期环中心 = 前端画出来的「短期记忆」球 (0, 2.35, 0)。
        # 原来是贴在中心核心旁（y=0.05），导致粒子从金核附近冒出来，
        # 看起来"不是短期记忆球在发射"，分流方向也不对。
        _cpos = {"x": 0.0, "y": 2.35, "z": 0.0}
        nodes.append(_node("core:short", "category", "短期记忆", _cpos,
                           size=6.0, bright=0.62, color=0xe0e0e0, category="short",
                           extra={"count": len(_rows)}))
        # 时间指针：拖历史滑块时 = 那一刻；否则 = 现在
        _now_ts = (at_ms / 1000.0) if at_ms else time.time()
        # 先把「还活着」的球算出来，再按存活数量重新排布星环 ——
        # 否则被黑洞吞掉的球仍占着角度，星环上会留下永远填不满的空洞。
        # 命运分流：每条短期记忆最后去哪 —— 黑洞(遗忘) / 创伤池 / 事件池 / 人格中心(长期)
        _fg_now = forgotten_ids()
        try:
            _tr_pre = [x for x in (_read_json(ROOT / "memory" / "trauma.json").get("items") or [])
                       if isinstance(x, dict)]
        except Exception:  # noqa: BLE001
            _tr_pre = []
        try:
            _eps_pre = [x for x in (_read_json(ROOT / "memory" / "episodes.json").get("items") or [])
                        if isinstance(x, dict)]
        except Exception:  # noqa: BLE001
            _eps_pre = []
        _tr_ts = [t for t in (_ts_ms(x.get("since") or x.get("updated_at")) for x in _tr_pre) if t]
        _ep_ts = [t for t in (_ts_ms(x.get("at") or x.get("ts") or x.get("time")) for x in _eps_pre) if t]

        _alive: list[tuple] = []
        for row in _rows:
            rid, sp, msg = row[0], row[1], row[2]
            _ts_s = str(row[3] or "")
            try:
                _age_h = max(0.0, (_now_ts - time.mktime(time.strptime(_ts_s[:19], "%Y-%m-%d %H:%M:%S"))) / 3600.0)
            except Exception:  # noqa: BLE001
                _age_h = 0.0
            _ret = 0.5 ** (_age_h / 12.0)          # 12 小时半衰期
            if _age_h > 72.0:                       # 真遗忘：视图里不再出现
                continue
            _ret = max(0.02, min(1.0, _ret))
            _suck = 0.0
            if _ret < 0.30:
                _suck = min(1.0, (0.30 - _ret) / 0.28)
                if _suck > 0.985:
                    _suck = 1.0                     # 已到期：交给前端播「吞噬」动画
            # 判定命运：遗忘 > 创伤 > 事件 > 长期
            _ts_ms_val = 0
            try:
                _ts_ms_val = int(time.mktime(time.strptime(_ts_s[:19], "%Y-%m-%d %H:%M:%S")) * 1000)
            except Exception:  # noqa: BLE001
                pass
            _dest = "longterm"
            _msg_l = str(msg or "").lower()
            # ① 已遗忘 → 黑洞
            if str(rid) in _fg_now or f"cat:short:{rid}" in _fg_now:
                _dest = "blackhole"
            # ② 时间上贴近创伤记录 → 创伤
            elif _ts_ms_val and any(abs(_ts_ms_val - t) < 3600_000 for t in _tr_ts):
                _dest = "trauma"
            # ③ 时间上贴近事件记录 → 事件
            elif _ts_ms_val and any(abs(_ts_ms_val - t) < 3600_000 for t in _ep_ts):
                _dest = "episode"
            # ④ 内容带强烈负面情绪 → 创伤（关键词兜底，让分流不至于一边倒）
            elif any(w in _msg_l for w in (
                    "恶心", "烦死", "气死", "难受", "崩溃", "讨厌", "垃圾", "滚",
                    "生气", "吵架", "骂", "哭", "疼", "怕", "恶心到")):
                _dest = "trauma"
            # ⑤ 内容像「事件/决定/第一次/变化」→ 事件
            elif any(w in _msg_l for w in (
                    "第一次", "决定", "答应", "以后", "从此", "开始", "结束",
                    "变化", "改成", "不再", "终于", "结果", "考上", "离职", "辞职")):
                _dest = "episode"
            # 命运 → 粒子颜色（分流时一眼能看出它要去哪）
            _dest_color = {
                "longterm": 0x4a9eff,   # 长期记忆：蓝
                "trauma": 0xff3b30,     # 创伤：红
                "episode": 0x3ddc84,    # 事件/经历：绿
                "blackhole": 0x8b1a1a,  # 遗忘：暗红
            }.get(_dest, 0xbfe0ff)

            # 归宿坐标：长期=人格中心；创伤/事件=对应池；遗忘=黑洞
            if _dest == "blackhole":
                _dest_pos = (BLACKHOLE_POS["x"], BLACKHOLE_POS["y"], BLACKHOLE_POS["z"])
            elif _dest == "trauma":
                _dest_pos = (0.40 * math.cos(5.95), 0.04, 0.40 * math.sin(5.95))
            elif _dest == "episode":
                _dest_pos = (0.40 * math.cos(6.45), 0.04, 0.40 * math.sin(6.45))
            else:
                # 长期记忆：停在人格中心**外围**的一圈上（不能是中心点，
                # 否则几百颗球全堆在金色核心里，视觉上等于消失）
                # 长期记忆的真实锚点在外环 dust 上（半径 0.194~0.277），
                # 粒子要落到这个带上，而不是我之前随手写的 0.62~0.96。
                # 用「这一帧里的序号」算角度，别用 rid —— rid 是字符串，
                # int() 会失败或相邻值算出同一个角度，几百颗球会叠在一个点上。
                _seed = abs(hash(str(rid))) % 100000
                _da = (_seed * 0.6180339887) % (2 * math.pi)    # 黄金角撒开
                _dr = (float(dustR) if dustR else 0.194) + 0.083 * ((_seed % 11) / 11.0)
                _dest_pos = (_dr * math.cos(_da), math.sin(int(rid) % 7) * 0.012,
                             _dr * math.sin(_da))
            _alive.append((rid, sp, msg, _ts_s, _age_h, _ret, _suck, _dest, _dest_pos))

        # 关键：**只有「还在星环上」的球参与均分**。
        # 正在滑向黑洞的球已经脱离星环，不再占角度 —— 否则星环永远补不满。
        _stable = [b for b in _alive if b[6] <= 0.0]
        n_stable = max(1, len(_stable))
        n_all = max(1, len(_alive))
        _stable_idx = {b[0]: k for k, b in enumerate(_stable)}

        for j, (rid, sp, msg, _ts_s, _age_h, _ret, _suck, _dest, _dest_pos) in enumerate(_alive):
            if _suck <= 0.0:
                # 留在星环上的球：按「存活数量」重新均分，缺口自动补齐
                _k = _stable_idx.get(rid, j)
                _frac = _k / n_stable
            else:
                # 正在飞向黑洞的球：沿用它在全量里的角度当出发点
                _frac = j / n_all
            th = _frac * 2 * math.pi
            rr = 0.075 + 0.055 * _frac
            # 坐标永远留在星环原位 —— 飞向黑洞由前端动画负责（后端推位置会让动画没有起点）
            _px = _cpos["x"] + rr * math.cos(th)
            _py = _cpos["y"] + math.sin(j * 1.7) * 0.012
            _pz = _cpos["z"] + rr * math.sin(th)
            # ── 生命周期位置（后端算好，前端直接画）──
            # life 0→1：白洞(诞生) → 短期环(停留) → 各自的归宿
            _life = max(0.0, min(1.0, _age_h / 72.0))   # 72 小时走完一生
            if _life < 0.28:
                _k = _life / 0.28
                _e = _k * _k * (3 - 2 * _k)                       # smoothstep
                _lx = WHITENOLE_POS["x"] + (_px - WHITENOLE_POS["x"]) * _e
                _ly = WHITENOLE_POS["y"] + (_py - WHITENOLE_POS["y"]) * _e
                _lz = WHITENOLE_POS["z"] + (_pz - WHITENOLE_POS["z"]) * _e
            elif _life < 0.62:
                _lx, _ly, _lz = _px, _py, _pz                     # 停在短期环上
            else:
                _k = min(1.0, (_life - 0.62) / 0.38)
                _e = _k * _k * _k                                 # 越飞越快
                _lx = _px + (_dest_pos[0] - _px) * _e
                _ly = _py + (_dest_pos[1] - _py) * _e
                _lz = _pz + (_dest_pos[2] - _pz) * _e

            nodes.append(_node(
                f"cat:short:{rid}", "memory", f"{sp or '?'}：{str(msg or '')[:36]}",
                {"x": round(_lx, 5), "y": round(_ly, 5), "z": round(_lz, 5)},
                size=round((0.35 + 0.75 * _ret) * (1.0 - 0.55 * _suck), 3),
                # 亮度按真实年龄：5 分钟前极亮，3 天前暗淡
                bright=round(max(0.06, min(0.62, 0.12 + 0.50 * (0.5 ** (_age_h / 18.0)))), 3),
                category="short",
                color=(_dest_color if _life > 0.62 else 0xd8e4f0),
                retention=round(_ret, 3),
                t=int(time.mktime(time.strptime(_ts_s[:19], "%Y-%m-%d %H:%M:%S")) * 1000) if _ts_s else 0,
                extra={"emotion": "中性", "retention": round(_ret, 3),
                       "ageHours": round(_age_h, 1), "sucked": round(_suck, 3),
                       "destiny": _dest,
                       # 生命进度：ret 1→0 映射成 0→1（新鲜 → 即将遗忘）
                       "life": round(max(0.0, min(1.0, _age_h / 72.0)), 3),
                       # 归宿坐标：粒子最终要飞到的地方
                       "targetX": round(_dest_pos[0], 3),
                       "targetY": round(_dest_pos[1], 3),
                       "targetZ": round(_dest_pos[2], 3)},
            ))

    # ── 创伤记忆池：在短期记忆旁边，只显示池名，点小球看详情 ──
    try:
        _tr = [x for x in (_read_json(ROOT / "memory" / "trauma.json").get("items") or [])
               if isinstance(x, dict)]
    except Exception:  # noqa: BLE001
        _tr = []
    if _tr:
        _ang2 = 5.95
        _rr2 = 0.40   # 扩大 1/3
        _cpos2 = {"x": round(_rr2 * math.cos(_ang2), 3), "y": 0.04,
                  "z": round(_rr2 * math.sin(_ang2), 3)}
        nodes.append(_node("pool:trauma", "category", "创伤记忆池", _cpos2,
                           size=5.2, bright=0.85, color=0xc03030, category="trauma",
                           extra={"count": len(_tr)}))
        n_tr = len(_tr)
        for i, it in enumerate(_tr):
            th = (i / n_tr) * 2 * math.pi
            rr = 0.055 + 0.040 * (i / n_tr)
            charge = float(it.get("charge") or 0)
            nodes.append(_node(
                f"trauma:{i}", "memory", str(it.get("subject") or "某件事")[:26],
                {"x": round(_cpos2["x"] + rr * math.cos(th), 3),
                 "y": round(_cpos2["y"] + math.sin(i * 1.7) * 0.010, 3),
                 "z": round(_cpos2["z"] + rr * math.sin(th), 3)},
                size=round(0.8 + charge * 1.2, 2), bright=0.6, category="trauma",
                t=_ts_ms(it.get("since") or it.get("updated_at")),
                extra={"emotion": "负面"},
            ))

    # ── 事件池：在创伤记忆池旁边，只显示池名，点小球看详情 ──
    try:
        _eps = [x for x in (_read_json(ROOT / "memory" / "episodes.json").get("items") or [])
                if isinstance(x, dict)]
    except Exception:  # noqa: BLE001
        _eps = []
    if _eps:
        _ang3 = 6.45
        _rr3 = 0.40   # 扩大 1/3
        _cpos3 = {"x": round(_rr3 * math.cos(_ang3), 3), "y": 0.03,
                  "z": round(_rr3 * math.sin(_ang3), 3)}
        nodes.append(_node("pool:episodes", "category", "事件池", _cpos3,
                           size=5.0, bright=0.8, color=0x32cd32, category="exp",
                           extra={"count": len(_eps)}))
        n_ep = len(_eps)
        for i, it in enumerate(_eps):
            th = (i / n_ep) * 2 * math.pi
            rr = 0.05 + 0.035 * (i / n_ep)
            _eid = str(it.get("id") or "")
            nodes.append(_node(
                _eid if _eid.startswith("ep:") else f"ep:{_eid}",
                "memory", str(it.get("title") or "一件事")[:26],
                {"x": round(_cpos3["x"] + rr * math.cos(th), 3),
                 "y": round(_cpos3["y"] + math.sin(i * 1.7) * 0.010, 3),
                 "z": round(_cpos3["z"] + rr * math.sin(th), 3)},
                size=0.9, bright=0.6, category="exp",
                t=_ts_ms(it.get("ts") or it.get("time") or it.get("date") or it.get("at")),
                extra={"emotion": str(it.get("mood") or "中性")},
            ))

    # ── 外环：沉淀星尘（长期记忆 + 事件）──
    dust: list[dict] = []
    try:
        arch = mv._archive()
        rows = [(sc, it) for sc, lst in arch.items() for it in (lst or []) if isinstance(it, dict)]
        rows.sort(key=lambda x: -float(x[1].get("lu") or 0))
        for sc, it in rows:
            dust.append({
                "id": f"mem:{sc}:{it.get('id')}",
                "summary": str(it.get("fact") or "")[:40],
                "emotion": mv._emotion(it.get("fact") or ""),
                "t": int(float(it.get("lu") or 0) * 1000),
            })
    except Exception:  # noqa: BLE001
        pass
    for it in (_read_json(ROOT / "memory" / "episodes.json").get("items") or [])[:20]:
        if isinstance(it, dict):
            _eid = str(it.get("id") or "")
            dust.append({"id": _eid if _eid.startswith("ep:") else f"ep:{_eid}", "summary": str(it.get("title"))[:34],
                         "emotion": it.get("mood") or "中性", "t": 0})

    n_d = len(dust) or 1
    for i, m in enumerate(dust):
        u = i / n_d
        th = (i / n_d) * 2 * math.pi + u * 0.9
        rr = (float(dustR) if dustR else 0.194) + 0.083 * u
        nodes.append(_node(
            m["id"], "memory", m["summary"],
            {"x": round(rr * math.cos(th), 3),
             "y": round(math.sin(i * 1.7) * 0.012, 3),   # 上下压扁
             "z": round(rr * math.sin(th), 3)},
            size=0.1, bright=0.45,
            category="dust",
            t=int(m.get("t") or 0),
            extra={"emotion": m["emotion"]},
        ))

    # ── 人物池：她认识的人聚成一团（靠近中心，和念头池/梦境池同一套「池子」形态）
    try:
        _people = _people_pool()
    except Exception:  # noqa: BLE001
        _people = []
    if _people:
        # 池子放在「离所有星环核心最远」的高位空档，避免压住群核心
        _cores = [[float(n["starfield"]["x"]), float(n["starfield"]["y"]), float(n["starfield"]["z"])]
                  for n in nodes if n.get("kind") == "scope"]
        # 2026-11-06：跟其他池子统一用 _pool_spot3(0)，别再单独写死坐标 ——
        # 否则别的池子换布局，它一个人留在旧位置。
        _pool_pos = _pool_spot3(0, _cores)
        nodes.append(_node(
            "persona:people", "category", f"人物池·{len(_people)}", _pool_pos,
            size=1.15, bright=0.85, color=0x7fd4ff, category="people",
            extra={"pool": True, "count": len(_people),
                   "hint": "她认识的人：关系状态 + 群昵称 + 人物卡"},
        ))
        for _uid, _label, _last in _people:
            _pid = f"people:{_uid}"
            nodes.append(_node(
                _pid, "memory", _label, _pool_spread(_pool_pos, _pid, 0.36),
                size=0.36, bright=0.52, color=0x9fd8ff, category="people",
                t=_to_ms(_last),
                extra={"uid": _uid, "pool": True, "emotion": "中性",
                       "retentionScore": _decay_ret(_to_ms(_last), 1.0, half_days=60.0)},
            ))
            edges.append({"source": "persona:people", "target": _pid, "kind": "pool"})

    # ── 念头池 / 梦境池：和星河那边同一套池子形态，也搬到人格视图
    try:
        _td = _read_json(ROOT / "memory" / "thoughts.json")
        _now = time.time()
        _th = [x for x in (_td.get("items") or [])
               if isinstance(x, dict) and float(x.get("created") or 0) > _now - 86400 * 7]
        _th.sort(key=lambda x: -float(x.get("created") or 0))
        _th_items = [(str(x.get("created") or ""), "💭 " + str(x.get("text") or "")[:34],
                      int(float(x.get("created") or 0) * 1000)) for x in _th]
    except Exception:  # noqa: BLE001
        _th_items = []
    try:
        _dd = _read_json(ROOT / "memory" / "dreams.json")
        _dm = [x for x in (_dd.get("items") or []) if isinstance(x, dict)]
        _dm_items = [(str(x.get("at") or ""), "🌙 " + str(x.get("dream") or "")[:34],
                      _ts_ms(x.get("at"))) for x in _dm[-20:]]
    except Exception:  # noqa: BLE001
        _dm_items = []

    _add_pool(nodes, edges, "persona:thoughts", "念头池", 0xf2f7ff, "thought",
              _pool_spot3(1, _cores), _th_items)
    _add_pool(nodes, edges, "persona:dreams", "梦境池", 0xb48cff, "dream",
              _pool_spot3(2, _cores), _dm_items)

    # ── 执念池（2026-11-06 加）：和念头池分开放。
    # 念头池是"她想做的事"，执念池是"她放不下的话头" —— 两回事，混在一起就看不出区别了。
    # 只画 open 的（closed 已经放下了，画出来反而误导）。
    try:
        _od = _read_json(ROOT / "memory" / "执念池.json")
        _ob = [x for x in (_od.get("items") or [])
               if isinstance(x, dict) and str(x.get("status") or "") == "open"]
        _ob.sort(key=lambda x: -float(x.get("tension") or 0))
        _ob_items = [(
            str(x.get("id") or ""),
            f"🔗 {str(x.get('text') or '')[:30]}（张力{float(x.get('tension') or 0):.1f}）",
            int(float(x.get("last_touch") or x.get("created") or 0) * 1000),
        ) for x in _ob]
    except Exception:  # noqa: BLE001
        _ob_items = []
    _add_pool(nodes, edges, "persona:obsessions", "执念池", 0xff7a9c, "obsession",
              _pool_spot3(3, _cores), _ob_items)

    # ── 行动空间（2026-11-06 加）：她今天自己做了什么。
    # 主人问「可视化里同步了现在的信息没」—— 这是最该看到的东西：
    # 不看她说了什么，看她在没人叫的时候自己做了什么。
    try:
        _ag_f = ROOT / "memory" / "agency_state.json"
        _ag = _read_json(_ag_f) if _ag_f.exists() else {}
        _ag_hist = list(_ag.get("history") or [])[-6:]
        _ag_items = [(
            str(int(float(x.get("at") or 0) * 1000)),
            f"⚙ {str(x.get('act') or '')[:12]}：{str(x.get('out') or '')[:22]}",
            int(float(x.get("at") or 0) * 1000),
        ) for x in _ag_hist]
    except Exception:  # noqa: BLE001
        _ag_items = []
    _add_pool(nodes, edges, "persona:agency", "行动空间", 0x7ee8c9, "agency",
              _pool_spot3(4, _cores), _ag_items)

    # ── 反馈闭环（同上）：她主动开口的结果，warm / answered / ignored / cold
    try:
        _fb_f = ROOT / "memory" / "proactive_log.jsonl"
        _fb_rows = []
        if _fb_f.exists():
            for _ln in _fb_f.read_text(encoding="utf-8").splitlines()[-30:]:
                try:
                    _r = json.loads(_ln)
                    if _r.get("reaction"):
                        _fb_rows.append(_r)
                except Exception:  # noqa: BLE001
                    continue
        _fb_map = {"warm": "🟢接住了", "answered": "🟡回了", "ignored": "⚪没理", "cold": "🔴嫌烦"}
        _fb_items = [(
            str(int(float(x.get("at") or 0) * 1000)),
            f"{_fb_map.get(str(x.get('reaction')), '?')}：{str(x.get('text') or '')[:20]}",
            int(float(x.get("at") or 0) * 1000),
        ) for x in _fb_rows[-6:]]
    except Exception:  # noqa: BLE001
        _fb_items = []
    _add_pool(nodes, edges, "persona:feedback", "反馈闭环", 0xffd166, "feedback",
              _pool_spot3(5, _cores), _fb_items)

    # 记忆白洞：最左侧，新记忆从这里诞生、吐出粒子飞向短期记忆星环
    nodes.append(_node(
        "persona:whitehole", "memory", "记忆白洞",
        dict(WHITENOLE_POS),
        size=3.0, bright=0.9, color=0xf5faff, category="whitehole", t=0,
        extra={"emotion": "新生", "hint": "新记忆诞生的地方，粒子从这里飞向短期记忆"},
    ))

    # 遗忘黑洞：最右侧，短期记忆滑到这里就被吞掉
    nodes.append(_node(
        "persona:blackhole", "memory", "遗忘黑洞",
        dict(BLACKHOLE_POS),
        size=3.2, bright=0.12, color=0x0a0a0f, category="blackhole", t=0,
        extra={"emotion": "虚无", "hint": "短期记忆遗忘后滑向这里并被吞掉"},
    ))

    try:
        _soul2 = dict(soul) if isinstance(soul, dict) else {}
        _soul2["growth"] = [
            {"id": x.get("id"), "trait": x.get("trait"), "value": x.get("value"),
             "status": x.get("status") or "pending",
             "evidence": len(x.get("evidence") or []),
             "at": x.get("solidifiedAt") or x.get("since") or ""}
            for x in (grows or [])
        ]
        soul = _soul2
    except Exception:  # noqa: BLE001
        pass

    return {
        "scope": scope or "",
        "center": center,
        # 2026-11-06：原来只返回 growth 一项，右边「关系圈层」就只剩一个开关 ——
        # 念头池/梦境池/执念池/行动空间/反馈闭环都没法单独控制，看着像"没同步"。
        # 现在把所有实际画出来过的类别都列上。
        "categories": _persona_categories(nodes, len(grows)),
        "nodes": nodes,
        "edges": edges,
        "timeRange": {"min": 0, "max": 0},
        "soul": soul,
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _persona_rings(scope: str | None = None) -> dict:
    """关系圈层版（群/人为核心 + 记忆星尘）。目前默认不启用。"""
    from server import memory_viz as mv

    rows = [x for x in mv.scopes()["scopes"] if x.get("scope") != "__all__"]
    rows.sort(key=lambda x: -int(x.get("count") or 0))
    total = sum(int(x.get("count") or 0) for x in rows) or 1
    max_cnt = max([int(x.get("count") or 0) for x in rows], default=0)

    # ── 1. 收集每个群：记忆、情绪趋势、核心星等、星云盘半径 ──
    groups: list[dict] = []
    for i, row in enumerate(rows):
        sc = str(row.get("scope") or "")
        cnt = int(row.get("count") or 0)
        ratio = cnt / total
        ring = 0 if (i < 2 or ratio >= 0.15) else (1 if ratio >= 0.05 else 2)

        try:
            mnodes, _ = mv.build_nodes(sc)
        except Exception:  # noqa: BLE001
            mnodes = []
        mems = [m for m in mnodes if m.get("kind") == "memory"][:MAX_PER_CAT]
        neg = sum(1 for m in mems if str(m.get("emotion") or "") in _NEG_EMO)
        posi = sum(1 for m in mems if str(m.get("emotion") or "") in _POS_EMO)
        trend = "cort" if neg > posi * 1.2 else ("oxy" if posi > neg * 1.5 else "neutral")

        size = _core_size(cnt, max_cnt)
        # 越重的群，星云盘略大（0.58~0.72），但永远 ≤0.74
        neb_r = round(NEBULA_R_MIN + (NEBULA_R_MAX - NEBULA_R_MIN)
                      * (min(1.0, size / CORE_SIZE_MAX) ** 1.3), 3)
        groups.append({
            "sc": sc, "cnt": cnt, "ring": ring, "mems": mems,
            "neg": neg, "pos": posi, "trend": trend,
            "size": size, "neb_r": neb_r,
            "label": str(row.get("label") or sc),
        })

    # ── 2. 群核心布局：环带均分圆周 + 强排斥松弛 ──
    pts = _relax_cores(_ring_points([g["ring"] for g in groups]))

    # ── 3. 自我：很小的金核，不挂标签 ──
    nodes: list[dict] = []
    edges: list[dict] = []
    center = _node("persona:me", "persona", "",
                   {"x": 0.0, "y": 0.0, "z": 0.0},
                   size=1.45, bright=0.9, color=0xffd77a, category="center",
                   extra={"hideLabel": True})
    nodes.append(center)

    # ── 4. 群核心 + 围绕它的星尘 ──
    for g, p in zip(groups, pts):
        sc = g["sc"]
        ring = g["ring"]
        sid = f"scope:{sc}"
        pos = {"x": round(p[0], 4), "y": round(p[1], 4), "z": round(p[2], 4)}
        core_color = _TREND_CORE.get(g["trend"]) or _RING_CORE[ring]
        nodes.append(_node(
            sid, "scope", g["label"], pos,
            size=g["size"],
            bright=round(1.0 - 0.05 * ring, 4),
            color=core_color,
            category=f"ring{ring}",
            extra={"scopeKey": sc, "count": g["cnt"], "trend": g["trend"],
                   "neg": g["neg"], "pos": g["pos"], "ring": ring},
        ))

        dust = _TREND_DUST.get(g["trend"], _TREND_DUST["neutral"])
        for m in g["mems"]:
            mp = _nebula(pos, str(m.get("id")), g["neb_r"])
            imp = max(0.0, min(1.0, float(m.get("importance") or 0.5)))
            rec = max(0.0, min(1.0, float(m.get("recency") or 0.5)))
            nodes.append({
                **m,
                "category": f"ring{ring}",
                "scopeKey": sc,
                "starfield": {
                    "x": mp["x"], "y": mp["y"], "z": mp["z"],
                    # 星尘星等：永远弱于核心（0.25~0.49）
                    "size": round(0.25 + 0.24 * (0.65 * imp + 0.35 * rec), 3),
                    # 亮度 0.36~0.56：暗于核心，让核心的激素色当主角
                    "brightness": round(0.36 + 0.20 * rec, 3),
                    "color": f"#{dust:06x}",
                },
            })
            edges.append({"source": sid, "target": m.get("id"), "kind": "intra"})

    # ── 人物池：她认识的人聚成一团（池子形态，不挂环带）
    try:
        people = _people_pool()
    except Exception:  # noqa: BLE001
        people = []
    if people:
        pool_pos = _pool_spot(pts)
        nodes.append(_node(
            "persona:people", "category", f"人物池·{len(people)}", pool_pos,
            size=1.15, bright=0.85, color=0x7fd4ff, category="people",
            extra={"pool": True, "count": len(people),
                   "hint": "她认识的人：关系状态 + 群昵称 + 人物卡"},
        ))
        for _uid, _label, _last in people:
            _pid = f"people:{_uid}"
            nodes.append(_node(
                _pid, "memory", _label, _pool_spread(pool_pos, _pid, 0.36),
                size=0.36, bright=0.52, color=0x9fd8ff, category="people",
                t=_to_ms(_last),
                extra={"uid": _uid, "pool": True, "emotion": "中性",
                       "retentionScore": _decay_ret(_to_ms(_last), 1.0, half_days=60.0)},
            ))
            edges.append({"source": "persona:people", "target": _pid, "kind": "pool"})

    # 遗忘黑洞：最右侧，短期记忆滑到这里就被吞掉
    nodes.append(_node(
        "persona:blackhole", "memory", "遗忘黑洞",
        dict(BLACKHOLE_POS),
        size=3.2, bright=0.12, color=0x0a0a0f, category="blackhole", t=0,
        extra={"emotion": "虚无", "hint": "短期记忆遗忘后滑向这里并被吞掉"},
    ))

    return {
        "scope": scope or "",
        "center": center,
        "categories": [{"key": k, "label": lb} for k, lb in _RING_LABELS],
        "nodes": nodes,
        "edges": edges,
        "timeRange": {"min": 0, "max": 0},
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
