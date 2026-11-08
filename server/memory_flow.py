"""记忆流动 —— 多 Flow 架构（照 tyFlow 的 Send Out 模型）

    ┌─ Flow「出生流」───────────────────────────────────────┐
    │ Birth → PositionObject(短期环) → Speed → TimeTest   │
    │                                              ↓      │
    │                                         Split(命运) │
    └──────────────────────────────────────────────┬──────┘
                                                   │ Send Out
         ┌─────────────────┬─────────────────┬─────┴────────┐
         ↓                 ↓                 ↓              ↓
    Flow「长期」      Flow「创伤」      Flow「事件」      Flow「遗忘」
    MoveToTarget      MoveToTarget      MoveToTarget      MoveToTarget
    (中心外围)         (创伤池)           (事件池)          (黑洞)
         ↓                 ↓                 ↓              ↓
        Stop              Stop              Stop          Delete

每个 flow 自己决定粒子的位置、大小、亮度。以后要给某一类加特殊演化，
只改对应 flow 即可。对外只暴露 flow_frame(at_ms)。
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MEM = ROOT / "memory"

WHITE_HOLE = {"x": -4.60, "y": 0.02, "z": 0.0}
BLACK_HOLE = {"x": 4.20, "y": 0.02, "z": 0.0}

# ── 布局：短期记忆在人格球头顶，长期/事件/创伤/黑洞在下方散开 ──
#         短期(0, 0.95)
#            │
#       ┌────┴────┐
#     长期        事件
#       └────┬────┘
#          创伤     黑洞(右侧)
SHORT_POS = {"x": 0.0, "y": 2.35, "z": 0.0}   # 必须等于前端 memoryFlowLayer 的 SHORT_POS
# 长期记忆：围着人格球的一圈（像之前「念头池」那样包住中心）
LONG_RING_R = 0.42
EPISODE_POS = {"x": 1.25, "y": -0.75, "z": 0.0}
TRAUMA_POS = {"x": 0.0, "y": -1.45, "z": 0.0}

# 一天走完一轮：出生 2h → 漫游 4h → 分流完成 24h
T_BORN = 6.0       # 白洞 → 短期环（拉长，白洞附近才常驻粒子）
T_WANDER = 11.0    # 短期环停留
T_FLOW = 18.0      # 到达目标（比一天短一点，保证长期区有落地粒子）

FORGOTTEN_PATH = MEM / "forgotten.json"

TRAUMA_WORDS = (
    "恶心", "烦死", "气死", "难受", "崩溃", "讨厌", "垃圾", "滚",
    "生气", "吵架", "骂", "哭", "疼", "怕",
)
EVENT_WORDS = (
    "第一次", "决定", "答应", "以后", "从此", "开始", "结束",
    "变化", "改成", "不再", "终于", "结果", "考上", "离职", "辞职",
)


# ══════════════════════════ 工具 ══════════════════════════

def _json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        return {}


def _ts_ms(v) -> int:
    txt = str(v or "").strip()
    if not txt:
        return 0
    for fmt, ln in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M", 16), ("%Y-%m-%d", 10)):
        try:
            return int(time.mktime(time.strptime(txt[:ln], fmt)) * 1000)
        except Exception:  # noqa: BLE001
            continue
    return 0


def _chat_rows(limit: int = 3000) -> list[tuple]:
    db = Path(os.environ.get("APPDATA") or str(Path.home())) / "dsh-pet-standalone-webm-chat" / "shared_memory.db"
    try:
        con = sqlite3.connect(str(db), timeout=5)
        rows = con.execute(
            "select id, speaker, message, timestamp from chat_history order by rowid desc limit ?",
            (limit,),
        ).fetchall()
        con.close()
        return rows
    except Exception:  # noqa: BLE001
        return []


def _hash(s: str) -> int:
    return abs(hash(str(s))) % 100000


# ══════════════════════════ 粒子 ══════════════════════════

def _mk(rid, msg, t: int, dest: str) -> dict:
    return {
        "id": f"cat:short:{rid}",
        "rid": rid,
        "msg": msg,
        "t": t,
        "dest": dest,
        "pos": {"x": 0.0, "y": 0.0, "z": 0.0},
        "size": 0.024,
        "bright": 0.45,
        "state": "BORN",
        "life": 0.0,
        "ageH": 0.0,
    }



def _archive_texts() -> list[tuple[str, str]]:
    """读真实的长期记忆档案（208 条）。返回 [(id, text)]。"""
    try:
        d = _json(MEM / "长期记忆.json")
        arch = d.get("archive") or {}
        out: list[tuple[str, str]] = []
        for scope, lst in arch.items():
            if not isinstance(lst, list):
                continue
            for it in lst:
                if not isinstance(it, dict):
                    continue
                txt = str(it.get("fact") or it.get("subject") or "").strip()
                if txt:
                    out.append((f"{scope}:{it.get('id')}", txt))
        return out
    except Exception:  # noqa: BLE001
        return []


def _archive_times() -> list[int]:
    """档案对应的时间戳（用 lu 字段），用来把档案和原始消息对上。

    档案里的 fact 是「提炼过的一句话」，和原文消息做文本匹配几乎对不上；
    但每条档案都记着 lu（最后使用时间），拿它去匹配消息时间才可靠。
    """
    try:
        d = _json(MEM / "长期记忆.json")
        arch = d.get("archive") or {}
        out: list[int] = []
        for scope, lst in arch.items():
            if not isinstance(lst, list):
                continue
            for it in lst:
                if not isinstance(it, dict):
                    continue
                try:
                    lu = float(it.get("lu") or 0)
                except (TypeError, ValueError):
                    lu = 0.0
                if lu > 1e9:
                    out.append(int(lu * 1000))
        return out
    except Exception:  # noqa: BLE001
        return []


def _bigrams(t: str) -> set:
    t = "".join(ch for ch in t if "一" <= ch <= "鿿" or ch.isalnum())
    return {t[i:i + 2] for i in range(len(t) - 1)} if len(t) >= 2 else ({t} if t else set())


def _matched_archive(msg: str, arch: list[tuple[str, str]], thresh: float = 0.20) -> str | None:
    """用字符二元组重叠度，判断这条消息是否对应某条真实长期记忆。"""
    mg = _bigrams(str(msg or ""))
    if not mg:
        return None
    best_id, best = None, 0.0
    for aid, txt in arch:
        ag = _bigrams(txt)
        if not ag:
            continue
        inter = len(mg & ag)
        if not inter:
            continue
        jac = inter / len(mg | ag)
        if jac > best:
            best, best_id = jac, aid
    return best_id if best >= thresh else None


def _matched_any(msg: str, pool: list[tuple[str, str]], thresh: float = 0.18) -> str | None:
    """在给定文本池里找最相似的一条（二元组 Jaccard）。

    池子的 bigram 会缓存住 —— 之前每条消息都重算一遍 11 条池子的 bigram，
    2242 条消息就是 2.7 万次，光这一步就吃了 0.3 秒。
    """
    if not pool:
        return None
    key = id(pool)
    cached = _POOL_BIGRAMS.get(key)
    if cached is None or len(cached) != len(pool):
        cached = [(aid, _bigrams(txt)) for aid, txt in pool]
        _POOL_BIGRAMS[key] = cached
    mg = _bigrams(str(msg or ""))
    if not mg:
        return None
    best_id, best = None, 0.0
    for aid, ag in cached:
        if not ag:
            continue
        inter = len(mg & ag)
        if not inter:
            continue
        jac = inter / len(mg | ag)
        if jac > best:
            best, best_id = jac, aid
    return best_id if best >= thresh else None


_POOL_BIGRAMS: dict = {}

def _event_texts() -> list[tuple[str, str]]:
    items = [x for x in (_json(MEM / "episodes.json").get("items") or []) if isinstance(x, dict)]
    return [(str(it.get("id")), str(it.get("title") or "") + str(it.get("text") or "")) for it in items if (it.get("title") or it.get("text"))]


def _trauma_texts() -> list[tuple[str, str]]:
    items = [x for x in (_json(MEM / "trauma.json").get("items") or []) if isinstance(x, dict)]
    return [(str(i), str(it.get("subject") or "") + str(it.get("fact") or "")) for i, it in enumerate(items) if (it.get("subject") or it.get("fact"))]

# ══════════════════════════ Flow 1：出生流 ══════════════════════════

def flow_birth(now_ms: int, rows: list[tuple], fg: set,
               trauma_ts: list[int], event_ts: list[int],
               arch: list[tuple[str, str]],
               ep_texts: list[tuple[str, str]] | None = None,
               tr_texts: list[tuple[str, str]] | None = None,
               arch_times: list[int] | None = None) -> tuple[list[dict], list[dict]]:
    """Birth → PositionObject → Speed → TimeTest，返回 (还在流里的, 该分流的)。"""
    alive = []
    for row in rows:
        rid, msg, ts_s = row[0], row[2], row[3]
        ts = _ts_ms(ts_s)
        if not ts or ts > now_ms:
            continue
        # 保留度按 12 小时半衰期算（和视图一致），够高才有资格变长期
        _age_h = max(0.0, (now_ms - ts) / 3600000.0)
        _ret = max(0.02, min(1.0, 0.5 ** (_age_h / 12.0)))
        dest = _judge(msg, rid, fg, trauma_ts, event_ts, ts, _ret, arch, ep_texts, tr_texts, arch_times)
        alive.append(_mk(rid, msg, ts, dest))

    n = max(1, len(alive))
    still, split = [], []
    for i, p in enumerate(alive):
        age_h = (now_ms - p["t"]) / 3600000.0
        p["ageH"] = round(age_h, 1)

        # 短期记忆云：以头顶为中心撒开的一小团
        th = (i / n) * 2 * math.pi
        rr = 0.22 + 0.62 * math.sqrt(i / n)   # 星环更大
        ring = {
            "x": SHORT_POS["x"] + rr * math.cos(th),
            "y": SHORT_POS["y"] + math.sin(i * 1.7) * 0.018,   # 上下压窄成盘
            "z": SHORT_POS["z"] + rr * math.sin(th),
        }

        if age_h < T_BORN:
            k = age_h / T_BORN
            e = k * k * (3 - 2 * k)
            hsh = _hash(p["id"])
            ja = (hsh % 360) / 360.0 * math.pi * 2
            # 撒开半径：0.25 ~ 1.6 个世界单位，让粒子铺满整条「出生路径」，
            # 而不是全挤在白洞那一个点上（那样 255 颗看起来只有几个点）
            jr = 0.25 + ((hsh >> 7) % 100) / 100.0 * 1.35
            p["pos"] = {
                "x": WHITE_HOLE["x"] + (ring["x"] - WHITE_HOLE["x"]) * e + math.cos(ja) * jr * (1 - e),
                "y": WHITE_HOLE["y"] + (ring["y"] - WHITE_HOLE["y"]) * e,
                "z": WHITE_HOLE["z"] + (ring["z"] - WHITE_HOLE["z"]) * e + math.sin(ja) * jr * (1 - e),
            }
            p["state"], p["life"] = "BORN", round(k, 3)
            # 刚喷出来是暗淡的（还在白洞里酝酿）
            p["size"], p["bright"] = 0.020, 0.16 + 0.10 * k
            still.append(p)
        elif age_h < T_WANDER:
            p["pos"] = dict(ring)
            p["state"] = "WANDER"
            p["life"] = round((age_h - T_BORN) / max(1e-6, T_WANDER - T_BORN), 3)
            p["size"], p["bright"] = 0.024, 0.34
            still.append(p)
        else:
            p["ring"] = ring
            split.append(p)
    return still, split


# ══════════════════════════ Flow 2~5：分流 ══════════════════════════

def _fly(p: dict, target: dict, now_ms: int) -> dict:
    age_h = (now_ms - p["t"]) / 3600000.0
    k = min(1.0, max(0.0, (age_h - T_WANDER) / max(1e-6, T_FLOW - T_WANDER)))
    e = math.sqrt(k)
    ring = p.get("ring") or {"x": 0.0, "y": 0.0, "z": 0.0}
    p["pos"] = {
        "x": ring["x"] + (target["x"] - ring["x"]) * e,
        "y": ring["y"] + (target["y"] - ring["y"]) * e,
        "z": ring["z"] + (target["z"] - ring["z"]) * e,
    }
    p["life"] = round(k, 3)
    p["state"] = "ARRIVED" if k >= 1.0 else "FLOW"
    # 越接近归宿越亮：落地时最亮
    p["size"], p["bright"] = (0.030, 0.95) if k >= 1.0 else (0.024, 0.40 + 0.45 * k)
    return p


def _cluster_target(base: dict, p: dict, spread: float = 0.30) -> dict:
    """在一个区域中心周围撒开成一团。"""
    hsh = _hash(p["id"])
    a = (hsh * 0.6180339887) % (2 * math.pi)
    r = 0.04 + spread * ((hsh % 11) / 11.0)
    return {
        "x": base["x"] + r * math.cos(a),
        "y": base["y"] + ((hsh % 7) - 3) * 0.025,
        "z": base["z"] + r * math.sin(a),
    }


def flow_longterm(p: dict, now_ms: int) -> list[dict]:
    # 围在人格球周围的一圈：半径 0.34~0.62，均匀分布
    hsh = _hash(p["id"])
    a = (hsh * 0.6180339887) % (2 * math.pi)
    r = 1.45 + ((hsh % 11) / 11.0) * 0.75   # 再往外扩，给行星和黄核让出空间
    target = {
        "x": r * math.cos(a),
        "y": ((hsh % 7) - 3) * 0.035,
        "z": r * math.sin(a),
    }
    return [_fly(p, target, now_ms)]


def flow_trauma(p: dict, now_ms: int) -> list[dict]:
    return [_fly(p, _cluster_target(TRAUMA_POS, p, 0.26), now_ms)]


def flow_episode(p: dict, now_ms: int) -> list[dict]:
    return [_fly(p, _cluster_target(EPISODE_POS, p, 0.28), now_ms)]


def flow_forget(p: dict, now_ms: int) -> list[dict]:
    """飞到黑洞 → Delete（到达后不再返回，等于从场景销毁）。"""
    p = _fly(p, BLACK_HOLE, now_ms)
    if p["state"] == "ARRIVED":
        return []
    return [p]


FLOWS = {
    "longterm": flow_longterm,
    "trauma": flow_trauma,
    "episode": flow_episode,
    "blackhole": flow_forget,
}


# ══════════════════════════ Split 的条件 ══════════════════════════

def _judge(msg: str, rid, fg: set, trauma_ts: list[int], event_ts: list[int], ts: int,
           retention: float = 0.0, arch: list[tuple[str, str]] | None = None,
           ep_texts: list[tuple[str, str]] | None = None,
           tr_texts: list[tuple[str, str]] | None = None,
           arch_times: list[int] | None = None) -> str:
    """短期粒子的归宿：只有「遗忘」。

    长期记忆 / 创伤 / 事件 都有各自真实数据生成的粒子（_archive_particles /
    _trauma_particles / _episode_particles），它们和真实记录 1:1 对应。
    短期粒子再分流一份就会重复（之前创伤显示 16 个就是这么来的）。
    """
    return "blackhole"

def _archive_ids_and_times() -> tuple[list[str], list[int]]:
    """档案的 id 和形成时间（按 time 字段排序），给行星挑「出身」。"""
    try:
        rows = _archive_formed_at(0)
    except Exception:  # noqa: BLE001
        return [], []
    return [r[1] for r in rows], [r[0] for r in rows]


def _persona_planets(now_ms: int = 0) -> list[dict]:
    """人格标签（核心特质 / 行为准则 / 防御机制）围成行星。数据来自 soul_aggregation。"""
    try:
        from server import soul_aggregation as sa
        v = sa.view()
    except Exception:  # noqa: BLE001
        return []

    planets: list[tuple[str, str]] = []
    # 2026-11-03：行星改吃三我桶（本我/自我/超我），和三个金球同一份数据
    for kind in ("id", "ego", "superego"):
        for it in (v.get(kind) or []):
            t = str((it or {}).get("text") or "").strip()
            if t:
                planets.append((kind, t))

    src_ids, src_times = _archive_ids_and_times()
    n = max(1, len(planets))
    out: list[dict] = []
    # 类内索引：详情接口按「该类内第几条」查（soul:trait:0 / soul:rule:0 ...），
    # 用全局索引会越界（rule 只有 6 条，全局 idx 可能到 10）
    kind_count: dict[str, int] = {}
    for i, (kind, text) in enumerate(planets):
        th = (i / n) * 2 * math.pi
        rr = 1.05 + 0.30 * ((i * 7) % 10) / 10.0   # 卡在金球光晕(1.30)外、长期环(1.45)内
        idx_in_kind = kind_count.get(kind, 0)
        kind_count[kind] = idx_in_kind + 1
        out.append({
            "kind": kind,
            "text": text[:24],
            "idxInKind": idx_in_kind,
            "pos": {
                "x": round(rr * math.cos(th), 3),
                "y": round(math.sin(i * 1.7) * 0.018, 3),
                "z": round(rr * math.sin(th), 3),
            },
            # 三类行星各有底色：特质暖金 / 准则天蓝 / 防御暗红（成长条目见下面）
            # 卫星颜色绝对统一：本我金橙 / 自我蓝 / 超我亮白
            "color": {"id": 0xffc44a, "ego": 0x4ab8ff, "superego": 0xffffff}.get(kind, 0xffc44a),
            "size": 0.05,
            # 这颗行星来自长期记忆里哪一条，以及它什么时候才该出现（那一刻记忆已形成）
            "sourceIndex": (i * 13) % max(1, len(src_ids)),
            # 等这条记忆自己先落地（lu + T_FLOW 小时），才轮得到它变行星
            "appearAt": int(src_times[(i * 13) % max(1, len(src_times))] + T_FLOW * 3600_000) if src_times else 0,
        })

    # 人格成长词条：可视化「跑一次提炼」产出的成长条目也做成行星
    # （待确认=亮金 / 固化=绿；已删除的不画）。带上真实 id，详情接口按 growth:<id> 查。
    try:
        from server import memory_growth as mg
        grows = [x for x in mg.list_items()
                 if str(x.get("status") or "pending") in ("pending", "solidified")]
    except Exception:  # noqa: BLE001
        grows = []
    ng = len(grows)
    for j, it in enumerate(grows):
        st = str(it.get("status") or "pending")
        th = (j / max(1, ng)) * 2 * math.pi + 0.35
        rr = 1.40 + 0.18 * ((j * 5) % 10) / 10.0   # 特质行星(1.05~1.35)外一圈
        out.append({
            "kind": "growth",
            "id": str(it.get("id") or f"growth:{j}"),
            "text": f"{it.get('trait')}：{it.get('value')}"[:24],
            "pos": {
                "x": round(rr * math.cos(th), 3),
                "y": round(math.sin(j * 1.7) * 0.020, 3),
                "z": round(rr * math.sin(th), 3),
            },
            "color": 0x3ddc84 if st == "solidified" else 0xffd24a,
            "size": 0.070 if st == "pending" else 0.060,
            "sourceIndex": (j * 7) % max(1, len(src_ids)),
            "appearAt": int(it.get("t") or 0),   # 条目生成的那一刻才出现
        })
    return out



def _region_particles() -> list[dict]:
    """各区域的「自有粒子」——直接从真实数据来，不是短期流过去的。

      长期记忆 = 长期记忆.json 的 208 条档案
      创伤     = trauma.json 的 8 条
      事件     = episodes.json 的 3 条
    这些粒子常驻在自己的区域环上。
    """
    out: list[dict] = []

    def place(base_x: float, base_y: float, base_z: float, radius: float, i: int, n: int) -> dict:
        th = (i / max(1, n)) * 2 * math.pi
        rr = radius * (0.25 + 0.75 * ((i * 7) % 10) / 10.0)
        return {
            "x": round(base_x + rr * math.cos(th), 3),
            "y": round(base_y + math.sin(i * 1.7) * 0.05, 3),
            "z": round(base_z + rr * math.sin(th), 3),
        }

    # ① 长期记忆：208 条档案，撒在金色球外的大环上
    arch = _cached("arch", _archive_texts)
    n = max(1, len(arch))
    for i, (aid, txt) in enumerate(arch):
        pos = place(0, 0, 0, 1.75, i, n)
        out.append({
            "id": f"mem:{aid}", "kind": "longterm", "text": txt[:40],
            "pos": pos, "color": 0x4a9eff, "size": 0.030, "bright": 0.85,
        })

    # ② 创伤：8 条
    tr = [x for x in (_json(MEM / "trauma.json").get("items") or []) if isinstance(x, dict)]
    n2 = max(1, len(tr))
    for i, it in enumerate(tr):
        pos = place(0, TRAUMA_POS["y"], 0, 0.55, i, n2)
        out.append({
            "id": f"trauma:{i}", "kind": "trauma",
            "text": str(it.get("subject") or "")[:40],
            "pos": pos, "color": 0xff3b30, "size": 0.038, "bright": 0.95,
        })

    # ③ 事件：3 条
    ep = [x for x in (_json(MEM / "episodes.json").get("items") or []) if isinstance(x, dict)]
    n3 = max(1, len(ep))
    for i, it in enumerate(ep):
        pos = place(EPISODE_POS["x"], EPISODE_POS["y"], 0, 0.42, i, n3)
        out.append({
            # 事件没有单条详情接口 → 点开显示事件池汇总
            "id": f"pool:episodes", "kind": "episode",
            "text": str(it.get("title") or "")[:40],
            "pos": pos, "color": 0x3ddc84, "size": 0.038, "bright": 0.95,
        })
    return out


def _archive_particles(now_ms: int) -> list[dict]:
    """长期记忆粒子：每条档案按它自己的 lu 时间出现，出现过就常驻。

    这才是「每天都有新的沉淀」——时间轴拖到 10-25，只有那天之前的；
    拖到最后，197 条才全在。
    """
    try:
        d = _json(MEM / "长期记忆.json")
        arch = d.get("archive") or {}
    except Exception:  # noqa: BLE001
        return []

    # 用真实形成时间（time 字段），不是 lu
    items = _archive_formed_at(now_ms)
    out: list[dict] = []
    n_total = max(1, len(items))
    for i, (lu_ms, aid, txt, tone) in enumerate(items):
        if lu_ms > now_ms:
            continue      # 还没形成
        age_h = (now_ms - lu_ms) / 3600000.0
        # 刚形成时从内圈飞出去，之后常驻在大环上
        th = (i / n_total) * 2 * math.pi
        # 目标：外围巨大球壳（和前端 orbitPoint 的长期记忆分支一致）
        rr = 4.4 + ((i * 7) % 10) / 10.0 * 2.2
        flying = age_h < 18.0
        k = min(1.0, age_h / 18.0)
        # 起点是「短期记忆球」，不是人格球旁边
        sx, sy, sz = SHORT_POS["x"], SHORT_POS["y"], SHORT_POS["z"]
        tx, ty, tz = rr * math.cos(th), math.sin(i * 1.7) * 0.6, rr * math.sin(th)
        e = k * k * (3 - 2 * k)
        out.append({
            "id": f"mem:{aid}", "rid": 0, "dest": "longterm", "tone": tone,
            "state": "FLOW" if flying else "ARRIVED",
            "life": round(k, 3), "ageH": round(age_h, 2),
            "msg": txt[:40],
            "pos": {
                "x": round(sx + (tx - sx) * e, 6),
                "y": round(sy + (ty - sy) * e, 6),
                "z": round(sz + (tz - sz) * e, 6),
            },
            "size": 0.030,
            "bright": 0.55 + 0.40 * k,
        })
    return out


# ── 性能：这些数据在一次会话里基本不变，缓存住，别每帧重读+重算 ──
_CACHE: dict = {}
_CACHE_TTL = 60.0     # 秒


def _cached(key: str, fn):
    import time as _t
    hit = _CACHE.get(key)
    now = _t.time()
    if hit and (now - hit[0]) < _CACHE_TTL:
        return hit[1]
    val = fn()
    _CACHE[key] = (now, val)
    return val



_POS_WORDS = ("开心", "喜欢", "笑", "好棒", "可爱", "爱", "夸", "甜", "期待", "约定", "感谢", "得意")
_NEG_WORDS = ("烦", "气", "怕", "哭", "难过", "讨厌", "骂", "累", "丧", "吵", "凶", "委屈", "难受")


def _tone_of(hs: dict, text: str = "") -> str:
    """情绪极性：pos 正面 / neg 负面 / neu 中性。先看记忆自带的激素差异，没有就按文本关键词。"""
    try:
        da = float(hs.get("DA") or 0)
        cort = float(hs.get("CORT") or 0)
        oxy = float(hs.get("OXY") or 0)
        ser = float(hs.get("SER") or 0)
    except (TypeError, ValueError):
        da = cort = oxy = ser = 1.0
    vals = {"da": da, "cort": cort, "oxy": oxy, "ser": ser}
    top = max(vals, key=vals.get)
    second = sorted(vals.values())[-2]
    # 激素有明显差异 → 按激素判
    if vals[top] - second >= 0.1:
        if top == "cort":
            return "neg"
        if top in ("da", "oxy"):
            return "pos"
        return "neu"
    # 全是默认 1.0 → 用文本关键词兜底
    t = str(text or "")
    if any(k in t for k in _POS_WORDS):
        return "pos"
    if any(k in t for k in _NEG_WORDS):
        return "neg"
    return "neu"


def _archive_formed_at(now_ms: int) -> list[tuple[int, str, str, str]]:
    """按档案的真实形成时间排序。

    注意：档案里的 lu 是「最后使用时间」，不是形成时间——用它排序会让所有记忆
    挤到"最近用过"的那一刻。真正的形成日期在 time 字段（如 "10-27"）。
    time 只有日期没有时分，这里按同类条目均匀铺开，避免同一天全堆在一个点。
    """
    try:
        d = _json(MEM / "长期记忆.json")
        arch = d.get("archive") or {}
    except Exception:  # noqa: BLE001
        return []

    import datetime as _dt

    raw: list[tuple[str, str, str, str]] = []     # (date_str, id, text, tone)
    for scope, lst in arch.items():
        if not isinstance(lst, list):
            continue
        for it in lst:
            if not isinstance(it, dict):
                continue
            txt = str(it.get("fact") or it.get("subject") or "").strip()
            if not txt:
                continue
            raw.append((str(it.get("time") or ""), f"{scope}:{it.get('id')}", txt,
                        _tone_of(it.get("hormones") or {}, txt)))

    # 按日期分组，同一天的按序号铺开（09:00 起，每条 +18 分钟）
    from collections import defaultdict
    by_day: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for date_str, aid, txt, tone in raw:
        day = date_str or "10-25"
        by_day[day].append((aid, txt, tone))

    out: list[tuple[int, str, str, str]] = []
    for day, items in by_day.items():
        try:
            m, dd = day.split("-")[:2]
            base = _dt.datetime(2026, int(m), int(dd), 9, 0, 0)
        except Exception:  # noqa: BLE001
            base = _dt.datetime(2026, 10, 25, 9, 0, 0)
        for i, (aid, txt, tone) in enumerate(items):
            t = base + _dt.timedelta(minutes=18 * i)
            out.append((int(t.timestamp() * 1000), aid, txt, tone))
    out.sort(key=lambda x: x[0])
    return out


def _episode_particles(now_ms: int) -> list[dict]:
    """事件粒子：episodes.json 里的每条记录直接变成一颗绿色粒子。

    之前想让短期粒子「飞」到事件区，但事件标题是提炼过的抽象描述
    （如「对鲸鱼娘的态度变化」），和聊天原文文本匹配几乎对不上，
    结果一条都显示不出来。改成和长期档案一样：按记录自己的时间出现。
    """
    try:
        d = _json(MEM / "episodes.json")
        items = [x for x in (d.get("items") or []) if isinstance(x, dict)]
    except Exception:  # noqa: BLE001
        return []

    import datetime as _dt
    out: list[dict] = []
    n = max(1, len(items))
    for i, it in enumerate(items):
        # 时间：at / ts / time 字段，解析不出来就用创建时间兜底
        t_ms = _ts_ms(it.get("at") or it.get("ts") or it.get("time"))
        if not t_ms:
            t_ms = _ts_ms(it.get("createdAt")) or (now_ms - 86400_000)
        if t_ms > now_ms:
            continue
        th = (i / n) * 2 * math.pi
        _ph = (i * 2.399) % (2 * math.pi)
        _th = math.acos(2 * (((i * 37) % 100) / 100.0) - 1)
        rr = 4.4 + ((i * 7) % 10) / 10.0 * 2.0
        out.append({
            # 事件没有单条详情接口 → 点开显示事件池汇总
            "id": "pool:episodes",
            "rid": 0,
            "dest": "episode",
            "state": "ARRIVED",
            "life": 1.0,
            "ageH": round((now_ms - t_ms) / 3600000.0, 1),
            "msg": str(it.get("title") or "")[:40],
            "label": str(it.get("title") or "")[:12],
            "pos": {
                "x": round(rr * math.sin(_th) * math.cos(_ph), 4),
                "y": round(rr * math.cos(_th), 4),
                "z": round(rr * math.sin(_th) * math.sin(_ph), 4),
            },
            "size": 0.045,
            "bright": 0.95,
        })
    return out



def _trauma_particles(now_ms: int) -> list[dict]:
    """创伤粒子：trauma.json 里每条记录直接变成一颗红色粒子。

    之前是拿短期聊天记录做文本匹配，匹配上的算创伤——但那等于随机挑几条聊天
    染成红色，和真实创伤记录无关。改成和事件一样：真实记录 → 真实粒子。
    """
    try:
        d = _json(MEM / "trauma.json")
        items = [x for x in (d.get("items") or []) if isinstance(x, dict)]
    except Exception:  # noqa: BLE001
        return []

    out: list[dict] = []
    n = max(1, len(items))
    for i, it in enumerate(items):
        t_ms = _ts_ms(it.get("since") or it.get("updated_at") or it.get("at") or it.get("time"))
        if not t_ms:
            t_ms = now_ms - 86400_000
        if t_ms > now_ms:
            continue
        th = (i / n) * 2 * math.pi
        _ph = (i * 2.399) % (2 * math.pi)
        _th = math.acos(2 * (((i * 53) % 100) / 100.0) - 1)
        rr = 4.6 + ((i * 7) % 10) / 10.0 * 2.0
        out.append({
            # 详情接口按「数组索引」查（trauma:<idx>），不能用条目自己的 id
            "id": f"trauma:{i}",
            "rid": 0,
            "dest": "trauma",
            "state": "ARRIVED",
            "life": 1.0,
            "ageH": round((now_ms - t_ms) / 3600000.0, 1),
            "msg": str(it.get("subject") or it.get("fact") or "")[:40],
            "label": str(it.get("subject") or "")[:12],
            "pos": {
                "x": round(rr * math.sin(_th) * math.cos(_ph), 4),
                "y": round(rr * math.cos(_th), 4),
                "z": round(rr * math.sin(_th) * math.sin(_ph), 4),
            },
            "size": 0.048,
            "bright": 0.95,
        })
    return out


# ══════════════════════════ 主入口 ══════════════════════════

def flow_frame(at_ms: int | None = None, stress: bool = False) -> dict:
    now_ms = int(at_ms) if at_ms else int(time.time() * 1000)

    rows = _cached("rows", lambda: _chat_rows(3000))
    fg = {str(x) for x in (_json(FORGOTTEN_PATH).get("ids") or [])}
    trauma_ts = [t for t in (_ts_ms(x.get("since") or x.get("updated_at"))
                             for x in (_json(MEM / "trauma.json").get("items") or []) if isinstance(x, dict)) if t]
    event_ts = [t for t in (_ts_ms(x.get("at") or x.get("ts") or x.get("time"))
                            for x in (_json(MEM / "episodes.json").get("items") or []) if isinstance(x, dict)) if t]

    arch = _archive_texts()
    still, to_split = flow_birth(now_ms, rows, fg, trauma_ts, event_ts, arch,
                                  _cached("ep", _event_texts),
                                  _cached("tr", _trauma_texts),
                                  _cached("at", _archive_times))

    out: list[dict] = list(still)
    for p in to_split:
        fn = FLOWS.get(p["dest"])
        if fn:
            out.extend(fn(p, now_ms))

    # 重要：范围取「全部聊天记录」的时间跨度，不能取本帧粒子的 ——
    # 播放到早期时粒子只剩一两个，范围会塌成一个点，时间指针就再也走不动了。
    raw_ts = [t for t in (_ts_ms(r[3]) for r in rows) if t]
    planets = _persona_planets(now_ms)
    particles = out + _archive_particles(now_ms) + _episode_particles(now_ms) + _trauma_particles(now_ms)

    # ── 压力测试：500 条记忆 / 50 个事件 / 20 个人格（只影响这一帧，不写任何文件）──
    if stress:
        for k in range(12):
            planets.append({
                "kind": "trait",
                "id": f"synth:trait:{k}",
                "text": f"合成特质 {k + 1}",
                "idxInKind": 8 + k,
                "pos": {"x": 0.0, "y": 0.0, "z": 0.0},
                "color": 0xffd77a,
                "size": 0.075,
                "sourceIndex": k,
                "appearAt": 0,
            })
        synth: list[dict] = []
        for i in range(500):
            synth.append({"id": f"synth:mem:{i}", "dest": "longterm", "state": "ARRIVED",
                          "pos": {"x": 0.0, "y": 0.0, "z": 0.0}, "size": 0.03,
                          "bright": 0.9, "life": 1.0, "ageH": float(i % 72), "rid": -1})
        for i in range(50):
            synth.append({"id": f"synth:ep:{i}", "dest": "episode", "state": "ARRIVED",
                          "pos": {"x": 0.0, "y": 0.0, "z": 0.0}, "size": 0.03,
                          "bright": 0.9, "life": 1.0, "ageH": float(i % 72), "rid": -1})
        particles = synth + particles

    return {
        "now": now_ms,
        "oldest": min(raw_ts) if raw_ts else now_ms,
        "newest": max(raw_ts) if raw_ts else now_ms,
        "planets": planets,
        "regions": _region_particles(),
        "counts": {
            "born": sum(1 for p in out if p["state"] == "BORN"),
            "wander": sum(1 for p in out if p["state"] == "WANDER"),
            "flow": sum(1 for p in out if p["state"] == "FLOW"),
            "arrived": sum(1 for p in particles if p["state"] == "ARRIVED"),
        },
        "stress": bool(stress),
        "particles": particles,
        # 潜意识模块（移植自树+星海）：念头池 / 梦境池
        "thoughts": [x for x in (_json(MEM / "thoughts.json").get("items") or []) if isinstance(x, dict)][-12:],
        "dreams": [x for x in (_json(MEM / "dreams.json").get("items") or []) if isinstance(x, dict)][-6:],
    }


if __name__ == "__main__":
    import sys

    at = int(sys.argv[1]) if len(sys.argv) > 1 else None
    d = flow_frame(at)
    print("counts:", d["counts"], "| 总粒子:", len(d["particles"]))
    for p in d["particles"][:5]:
        print(f"  {p['id']:<22} {p['state']:<8} dest={p['dest']:<9} "
              f"age={p['ageH']:>5}h pos=({p['pos']['x']:+.2f},{p['pos']['z']:+.2f})")
