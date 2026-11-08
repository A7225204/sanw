"""给记忆可视化加「全部星海」：所有 scope 的星河画在同一界面、彼此隔离，
同一个人（同名 topic）跨 scope 时用一条连线相连。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from pathlib import Path

from loguru import logger

ROOT = Path(__file__).resolve().parent.parent

ALL_SCOPE = "__all__"


def _scope_entries() -> list[tuple[str, list[dict], dict]]:
    """返回 [(scope, nodes, coords)]，坐标取自缓存（缺失的用确定性坐标兜底）。"""
    from server import memory_viz as mv

    arch = mv._archive()
    cache = mv._star_cache()
    out = []
    for scope in arch.keys():
        nodes, _ = mv.build_nodes(scope)
        if not nodes:
            continue
        entry = (cache.get("scopes") or {}).get(scope) or {}
        coords = entry.get("coords") or {}
        missing = [n for n in nodes if n["id"] not in coords]
        if missing:
            coords = {**coords, **mv._compute_coords(scope, missing).get("coords", {})}
        out.append((scope, nodes, coords))
    return out


OWNER_SCOPE = "private_100000000"
OWNER_UID = "100000000"


def _cluster_layout(entries: list[tuple[str, list[dict], dict]], ring: float = 1.15,
                    shrink: float = 0.5) -> tuple[dict[str, dict], dict[str, tuple[float, float, float]]]:
    """主人星团放正中心，其它（他聊过/拉她去的群）按活跃度由内向外扩散。"""
    centers: dict[str, tuple[float, float, float]] = {}
    others = [e for e in entries if e[0] != OWNER_SCOPE]
    owner = [e for e in entries if e[0] == OWNER_SCOPE]

    if owner:
        centers[OWNER_SCOPE] = (0.0, 0.0, 0.0)

    # 由内向外排序：保留度高的团靠中心（核心内圈 / 衰减外抛），条数做次要权重
    def _scope_score(e: tuple[str, list[dict], dict]) -> float:
        nodes = e[1]
        if not nodes:
            return 0.0
        ret = sum(float(n.get("retentionScore") or 0.5) for n in nodes) / len(nodes)
        return ret * 10.0 + math.log1p(len(nodes))

    others.sort(key=lambda e: -_scope_score(e))
    # ── 对数螺旋悬臂：r = a * e^(b·θ) ──
    # 关键不是「按半径排队」，而是让 θ 连续推进、r 指数增长，
    # 这样才是一条从中心卷出去的手臂（均匀分布在圆上会像钟表盘）。
    ARMS = 2                # 两条主臂（银河系就是两条）
    A = 0.62                # r = A·e^(B·θ) 的 a
    B = 0.46                # 卷曲度：越大越卷
    THETA_MAX = 2.35        # 每条臂扫过的总角度（约 135°）
    per_arm = max(1, int(math.ceil(len(others) / ARMS)))
    for i, (scope, nodes, _coords) in enumerate(others):
        arm = i % ARMS
        slot = i // ARMS                        # 第几个落在这条臂上
        # 沿臂推进：θ 从内到外递增，半径交给指数决定
        t = (slot + 1) / per_arm
        theta = arm * math.pi + t * THETA_MAX   # 两条臂相差 180°
        r = A * math.exp(B * (t * THETA_MAX))   # 0.62 → 1.39，再加一点基础外推
        r += 0.55 * t                           # 让外圈再松开一点，别挤在一起
        y = (0.10 if slot % 2 == 0 else -0.10) * (0.4 + t)
        centers[scope] = (r * math.cos(theta), y, r * math.sin(theta))

    placed: dict[str, dict] = {}
    for scope, nodes, coords in entries:
        cx, cy, cz = centers[scope]
        # 骨架节点（scope/topic）留在团心附近，记忆节点排成扁平土星环
        mems = [n for n in nodes if n.get("kind") == "memory"]
        skeleton = [n for n in nodes if n.get("kind") != "memory"]
        for n in skeleton:
            c = coords.get(n["id"]) or {"x": 0, "y": 0, "z": 0}
            placed[n["id"]] = {
                "x": round(cx + c["x"] * shrink * 0.35, 5),
                "y": round(cy + c["y"] * shrink * 0.35, 5),
                "z": round(cz + c["z"] * shrink * 0.35, 5),
            }
        n_mem = len(mems) or 1
        # 团越大，盘越大：半径上限按 √n 增长（8 条 ~0.22，60 条 ~0.40）
        r_max = min(0.46, 0.17 + 0.030 * math.sqrt(n_mem))
        for i, n in enumerate(mems):
            th = (i / n_mem) * 2 * math.pi + (i % 7) * 0.09
            rr = 0.14 + (r_max - 0.14) * (i / max(1, n_mem - 1))
            yy = math.sin(i * 1.7) * 0.045           # 扁平盘，但留一点厚度
            placed[n["id"]] = {
                "x": round(cx + rr * math.cos(th), 5),
                "y": round(cy + yy, 5),
                "z": round(cz + rr * math.sin(th), 5),
            }
    return placed, centers


def starfield_all() -> tuple[int, dict]:
    """所有 scope 合在一起：团内隔离 + 同名主题跨团连线。"""
    from server import memory_viz as mv

    entries = [e for e in _scope_entries() if not str(e[0]).startswith("__")]
    if not entries:
        return 200, {"scope": ALL_SCOPE, "nodes": [], "edges": [], "generatedAt": ""}

    # 自愈：缺坐标的 scope 就地补一份确定性坐标（hash 球面），
    # 否则只要有一个 scope 没算过，全部视图会永远卡在「星海生成中」。
    # 真实 UMAP 布局仍由 jobs/starfield_layout.py 在后台覆盖。
    cache = mv._star_cache()
    changed = False
    for _sc, _nodes, _c in entries:
        _ent = (cache.get("scopes") or {}).get(_sc) or {}
        _missing = [n for n in _nodes if n["id"] not in (_ent.get("coords") or {})]
        if not _missing:
            continue
        try:
            cache.setdefault("scopes", {})[_sc] = mv._compute_coords(_sc, _nodes)
            changed = True
        except Exception:  # noqa: BLE001
            pass
    if changed:
        try:
            mv._write_json(mv.STAR_FILE, cache)
        except Exception:  # noqa: BLE001
            pass

    placed, centers = _cluster_layout(entries)

    out_nodes: list[dict] = []
    by_id: dict[str, dict] = {}
    for scope, nodes, _coords in entries:
        for n in nodes:
            pos = placed.get(n["id"])
            if not pos:
                continue
            size = round(0.25 + 0.75 * float(n.get("importance") or 0), 4)
            bright = round(0.3 + 0.7 * float(n.get("recency") or 0), 4)
            node = {**n, "starfield": {
                "x": pos["x"], "y": pos["y"], "z": pos["z"],
                "size": size, "brightness": bright,
                "color": mv._color_of(n.get("topicId") or n["id"]),
            }}
            out_nodes.append(node)
            by_id[n["id"]] = node

    # ── 给念头池 / 梦境池找两个空位：离所有星云中心尽量远，
    # 免得压在别的记录上（之前写死坐标，悬臂一改就叠住了）
    def _free_pool_spots(radius: float, want: int) -> list[tuple[float, float]]:
        pts = [(c[0], c[2]) for c in centers.values()]
        cands: list[tuple[float, float, float]] = []
        for deg in range(0, 360, 6):
            th = math.radians(deg)
            x, z = radius * math.cos(th), radius * math.sin(th)
            dmin = min((math.hypot(x - px, z - pz) for px, pz in pts), default=9.0)
            cands.append((dmin, x, z))
        cands.sort(key=lambda t: -t[0])
        picks: list[tuple[float, float]] = []
        for _d, x, z in cands:
            if all(math.hypot(x - p[0], z - p[1]) > radius * 0.85 for p in picks):
                picks.append((x, z))
            if len(picks) >= want:
                break
        return picks

    _pool_spots = _free_pool_spots(1.30, 6)

    # ── 新增三个池子（2026-11-06 主人要求：人物池/事件池/反馈闭环也要在星海里）──
    # 位置从 _pool_spots 里往后取，避开念头池和梦境池。
    _NEW_POOLS = [
        ("pool:people", "人物池", "memory/relation_state.json", "dict", 0x7fd4ff, 0.7),
        ("pool:exp", "事件池", "memory/episodes.json", "items", 0x4ade80, 0.7),
        ("pool:feedback", "反馈闭环", "memory/proactive_log.jsonl", "jsonl", 0xffd166, 0.7),
        ("pool:agency", "行动空间", "memory/agency_state.json", "history", 0x7ee8c9, 0.7),
        ("pool:obsessions", "执念池", "memory/执念池.json", "open", 0xff7a9c, 0.7),
    ]
    for _pi, (_pid2, _lb2, _f2, _mode, _col2, _sz2) in enumerate(_NEW_POOLS):
        try:
            _fp2 = ROOT / _f2
            if not _fp2.exists():
                continue
            if _mode == "jsonl":
                _cnt2 = sum(1 for _ln in _fp2.read_text(encoding="utf-8").splitlines()
                            if _ln.strip() and '"reaction"' in _ln)
            else:
                _d2 = json.loads(_fp2.read_text(encoding="utf-8"))
                if _mode == "dict":
                    _cnt2 = len(_d2) if isinstance(_d2, dict) else 0
                elif _mode == "open":
                    _cnt2 = len([x for x in (_d2.get("items") or [])
                                 if str((x or {}).get("status")) == "open"])
                else:
                    _items2 = _d2.get(_mode) if isinstance(_d2, dict) else _d2
                    _cnt2 = len(_items2 or [])
            if _cnt2 <= 0:
                continue
            _spot = _pool_spots[2 + _pi] if len(_pool_spots) > 2 + _pi else (-0.72 + 0.5 * _pi, 0.5)
            # 先把这个池子的真实条目拉一遍，给小光球做 summary
            _rows2: list[dict] = []
            try:
                _pd2 = pool_detail(_pid2) or {}
                _rows2 = list(_pd2.get("related") or [])
            except Exception:  # noqa: BLE001
                _rows2 = []
            _bx, _bz = float(_spot[0]), float(_spot[1])
            out_nodes.append({
                "id": _pid2, "scope": "__pools__", "parentId": None, "topicId": None,
                "summary": _lb2, "sourceMessageIds": [], "kind": "category",
                "retentionScore": 1.0, "importance": 1.0, "recency": 1.0,
                "frequency": 0.5, "createdAt": "", "lastAccessedAt": "",
                "category": "pool", "t": 0, "emotion": "中性",
                "starfield": {"x": round(_bx, 3), "y": 0.20,
                              "z": round(_bz, 3), "size": _sz2,
                              "brightness": 0.8, "color": f"#{_col2:06x}"},
            })
            # 成员小光球（2026-11-06 修）——
            # 之前 summary 是空串、size 才 0.16，前端筛掉了，所以看不见小光球。
            # 现在：真实文字做 summary、尺寸提到 0.34、给 createdAt 免得被时间轴切掉。
            _n_mem = min(10, max(1, _cnt2))
            for _mi in range(_n_mem):
                _th2 = (_mi / max(1, _n_mem)) * 2 * math.pi
                _rr2 = 0.20 + 0.06 * (_mi % 3)
                _lbl2 = ""
                try:
                    _lbl2 = str(_rows2[_mi].get("summary") or "")[:26] if _mi < len(_rows2) else ""
                except Exception:  # noqa: BLE001
                    _lbl2 = ""
                out_nodes.append({
                    "id": f"{_pid2}:{_mi}", "scope": "__pools__", "parentId": _pid2,
                    "topicId": None, "summary": _lbl2 or _lb2, "sourceMessageIds": [],
                    "kind": "memory",
                    "retentionScore": 0.85, "importance": 0.5, "recency": 1.0,
                    "frequency": 0.3,
                    "createdAt": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "lastAccessedAt": "",
                    "category": "pool", "t": int(time.time() * 1000),
                    "emotion": "中性",
                    "starfield": {
                        "x": round(_bx + _rr2 * math.cos(_th2), 4),
                        "y": 0.20,
                        "z": round(_bz + _rr2 * math.sin(_th2), 4),
                        "size": 0.34, "brightness": 0.75,
                        "color": f"#{_col2:06x}",
                    },
                })
        except Exception:  # noqa: BLE001
            continue

    # ── 念头池：不属于任何群，单独成一团飘在星海一侧 ──
    try:
        td = json.loads((ROOT / "memory" / "thoughts.json").read_text(encoding="utf-8-sig"))
        now_ts = time.time()
        thoughts = [x for x in (td.get("items") or [])
                    if isinstance(x, dict) and float(x.get("created") or 0) > now_ts - 86400 * 7]
        thoughts.sort(key=lambda x: -float(x.get("created") or 0))
        cx, cz = _pool_spots[0] if _pool_spots else (-0.72, -0.62)
        cy = 0.20
        out_nodes.append({
            "id": "pool:thoughts", "scope": "__thoughts__", "parentId": None, "topicId": None,
            "summary": "念头池", "sourceMessageIds": [], "kind": "category",
            "retentionScore": 1.0, "importance": 1.0, "recency": 1.0,
            "frequency": 0.5, "createdAt": "2026-10-31 12:00:00", "lastAccessedAt": "",
            "category": "thought", "t": 0, "emotion": "中性",
            "starfield": {"x": cx, "y": cy, "z": cz, "size": 0.7,
                          "brightness": 0.7, "color": "#f2f7ff"},
        })
        n_t = len(thoughts) or 1
        for i, it in enumerate(thoughts[:20]):
            th = (i / n_t) * 2 * math.pi
            rr = 0.16 + 0.16 * (i / n_t)
            nid = f"thought:{i}"
            out_nodes.append({
                "id": nid, "scope": "__thoughts__", "parentId": None, "topicId": None,
                "summary": "💭 " + str(it.get("text") or "")[:40],
                "sourceMessageIds": [], "kind": "memory",
                "retentionScore": 0.8, "importance": 0.5, "recency": 1.0,
                "frequency": 0.2, "createdAt": "", "lastAccessedAt": "",
                "category": "thought",
                "t": int(float(it.get("created") or 0) * 1000),
                "emotion": "中性",
                "starfield": {
                    "x": round(cx + rr * math.cos(th), 4),
                    "y": round(cy + math.sin(i * 1.7) * 0.03, 4),
                    "z": round(cz + rr * math.sin(th), 4),
                    "size": 0.9, "brightness": 0.55, "color": "#f2f7ff",
                },
            })
    except Exception:  # noqa: BLE001
        pass

    # ── 梦境区：夜间生成的独立星团，飘在星海另一侧 ──
    try:
        dd = json.loads((ROOT / "memory" / "dreams.json").read_text(encoding="utf-8-sig"))
        dreams = [x for x in (dd.get("items") or []) if isinstance(x, dict)][-10:]
        cx, cz = _pool_spots[1] if len(_pool_spots) > 1 else (0.66, -0.70)
        cy = -0.16
        out_nodes.append({
            "id": "pool:dreams", "scope": "__dreams__", "parentId": None, "topicId": None,
            "summary": "梦境池", "sourceMessageIds": [], "kind": "category",
            "retentionScore": 1.0, "importance": 1.0, "recency": 1.0,
            "frequency": 0.5, "createdAt": "2026-10-31 12:00:00", "lastAccessedAt": "",
            "category": "dream", "t": 0, "emotion": "中性",
            "starfield": {"x": cx, "y": cy, "z": cz, "size": 0.7,
                          "brightness": 0.7, "color": "#b48cff"},
        })
        n_d = len(dreams) or 1
        for i, it in enumerate(dreams):
            th = (i / n_d) * 2 * math.pi
            rr = 0.14 + 0.14 * (i / n_d)
            out_nodes.append({
                "id": f"dream:{i}", "scope": "__dreams__", "parentId": None, "topicId": None,
                "summary": "🌙 " + str(it.get("at") or "")[:10] + " " + str(it.get("dream") or "")[:30],
                "sourceMessageIds": [], "kind": "memory",
                "retentionScore": 0.5, "importance": 0.5, "recency": 0.8,
                "frequency": 0.1, "createdAt": str(it.get("at") or ""),
                "category": "dream", "t": 0, "emotion": "中性",
                "starfield": {
                    "x": round(cx + rr * math.cos(th), 4),
                    "y": round(cy + math.sin(i * 1.7) * 0.03, 4),
                    "z": round(cz + rr * math.sin(th), 4),
                    "size": 0.9, "brightness": 0.55, "color": "#b48cff",
                },
            })
    except Exception:  # noqa: BLE001
        pass

    # 团内边（沿用单 scope 的星团连线）
    edges: list[dict] = []
    # 环形成型后每颗星都连线会变成刺猬（主人反馈），团内连线不再生成
    # 只保留结构性连线：主人边 / 跨群同名边（在下面单独追加）

    # 跨团边：同一个人（优先按 QQ 号匹配，昵称兜底）连一条绿线
    topics_by_key: dict[str, list[dict]] = {}
    for n in out_nodes:
        if n.get("kind") != "topic":
            continue
        name = str(n.get("summary") or "").strip()
        if not name:
            continue
        scope = str(n.get("scope") or "")
        info = mv._speaker_info(scope, name)
        # 主人自己跨星河的关系用金线表达（主人 → 各星河），这里跳过，避免绿线重复
        if str(info.get("uid") or "") == OWNER_UID:
            continue
        # 有 QQ 就按 QQ 归并（同名不同人/改名都能对上），没有就按昵称
        key = f"uid:{info['uid']}" if info.get("uid") else f"name:{name}"
        topics_by_key.setdefault(key, []).append(n)

    cross = 0
    for key, group in topics_by_key.items():
        # 只连"不同星河之间"的人，同一个群里的多个主题不连
        scopes = {str(n.get("scope")) for n in group}
        if len(scopes) < 2:
            continue
        group.sort(key=lambda x: (str(x.get("scope")), -float(x.get("importance") or 0)))
        for i in range(len(group) - 1):
            a, b = group[i], group[i + 1]
            if str(a.get("scope")) == str(b.get("scope")):
                continue
            na = str(a.get("summary") or "")
            nb = str(b.get("summary") or "")
            # 同一个人在不同群改了名：把两个名字都标出来，否则看着像连错了
            label = na if na == nb else f"{na} / {nb}"
            edges.append({
                "source": a["id"], "target": b["id"],
                "weight": round(min(1.0, 0.45 + 0.15 * len(group)), 4),
                "kind": "cross",
                "label": label,
                "uid": key.split(":", 1)[1] if key.startswith("uid:") else "",
                "viaQq": key.startswith("uid:"),
            })
            cross += 1

    # 主人 → 其它星河：他聊过的群 / 拉她去的群，用一条金线串起来
    owner_links = 0
    owner_root = f"scope:{OWNER_SCOPE}"
    if owner_root in by_id:
        max_cnt = max((len(nodes) for sc, nodes, _c in entries if sc != OWNER_SCOPE), default=1)
        for scope, nodes, _coords in entries:
            if scope == OWNER_SCOPE:
                continue
            # 直接连到该星河的中心球（scope 节点），再由它往下连主题 / 记忆
            root_id = f"scope:{scope}"
            if root_id not in by_id:
                continue
            edges.append({
                "source": owner_root, "target": root_id,
                "weight": round(min(1.0, 0.35 + 0.65 * len(nodes) / max(1, max_cnt)), 4),
                "kind": "owner", "label": mv._scope_label(scope),
            })
            owner_links += 1

    return 200, {
        "scope": ALL_SCOPE,
        "nodes": out_nodes,
        "edges": edges,
        "ownerLinks": owner_links,
        "generatedAt": mv._star_cache().get("generatedAt") or "",
        "method": "multi-scope",
        "crossLinks": cross,
        "clusters": [
            {"scope": sc, "x": c[0], "y": c[1], "z": c[2], "label": mv._scope_label(sc)}
            for sc, c in centers.items()
        ],
    }


def tree_all() -> dict:
    """全部 scope 的树合成一棵森林：每个 scope 一个根。"""
    from server import memory_viz as mv

    arch = mv._archive()
    nodes: list[dict] = []
    edges: list[dict] = []
    for scope in arch.keys():
        sub = mv.tree(scope)
        nodes.extend(sub["nodes"])
        edges.extend(sub["edges"])
    return {"scope": ALL_SCOPE, "nodes": nodes, "edges": edges,
            "generatedAt": mv.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


# ---------------------------------------------------------------- 纯记忆池
# 和「关系视图」完全不同的坐标算法：打破群边界，按类型/激素聚簇。
#   创伤/负面 → 左上（暗红星云）
#   正面/开心 → 右上（金色星云）
#   长期       → 中心
#   短期       → 边缘下方（随时间消散）
def _hash01(seed: str) -> tuple[float, float, float]:
    h = hashlib.sha256(seed.encode("utf-8")).digest()
    return (h[0] / 255.0, h[1] / 255.0, h[2] / 255.0)


_POOL_POS = {
    "trauma": (-1.45, 1.05, 0.55),
    "joy": (1.45, 1.05, 0.55),
    "short": (0.0, -1.65, 0.0),
    "core": (0.0, 0.0, 0.0),
}


def memory_pool() -> dict:
    """纯记忆池视图：所有群的记忆打散后按激素/类型重新聚簇。"""
    from server import memory_viz as mv

    entries = _scope_entries()
    out: list[dict] = []
    for scope, nodes, _coords in entries:
        for n in nodes:
            if n.get("kind") != "memory":
                continue
            emo = str(n.get("emotion") or "")
            cat = str(n.get("category") or "")
            if n.get("trauma") or emo == "负面":
                key = "trauma"
            elif emo in ("开心", "喜欢", "得意", "感谢", "约定", "期待", "正面"):
                key = "joy"
            elif cat == "short" or str(n.get("id", "")).startswith("short:"):
                key = "short"
            else:
                key = "core"
            bx, by, bz = _POOL_POS[key]
            u, v, w = _hash01(str(n.get("id")))
            # 球状星团：中心密、边缘疏（之前是均匀撒开，看着零散）
            R = 0.55 if key != "core" else 0.85
            rr = R * (u ** 1.7)
            theta = v * 2 * math.pi
            phi = math.acos(2 * w - 1)
            ox = rr * math.sin(phi) * math.cos(theta)
            oy = rr * math.sin(phi) * math.sin(theta) * 0.65
            oz = rr * math.cos(phi)
            size = round((0.25 + 0.75 * float(n.get("importance") or 0)) * 10.0, 4)  # 记忆池：球放大 10 倍
            bright = round(0.3 + 0.7 * float(n.get("recency") or 0), 4)
            out.append({
                **n,
                "cluster": key,
                "scopeKey": scope,
                "starfield": {
                    "x": round(bx + ox, 4),
                    "y": round(by + oy, 4),
                    "z": round(bz + oz, 4),
                    "size": size,
                    "brightness": bright,
                    "color": mv._color_of(n.get("topicId") or n["id"]),
                },
            })
    return {"scope": "__pool__", "nodes": out, "edges": [],
            "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S")}

def pool_detail(node_id: str) -> dict | None:
    """五个自有池子的详情。

    2026-11-06 修：面板 MemoryDetailPanel 要求顶层 `node` 字段（d.node.id / d.node.kind），
    之前返回的是平铺结构 → 面板读 d.node.id 直接抛错，显示"节点错误"。
    这里照 persona_detail 里白洞/黑洞那套结构返回，并补 `related` 行给 CategoryPanel 列。
    """
    _MAP = {
        "pool:people": ("人物池", "memory/relation_state.json", "dict",
                        "她认识的人和好感度"),
        "pool:exp": ("事件池", "memory/episodes.json", "items",
                     "从聊天里提炼出来的事"),
        "pool:feedback": ("反馈闭环", "memory/proactive_log.jsonl", "jsonl",
                          "她主动开口之后有没有人接"),
        "pool:agency": ("行动空间", "memory/agency_state.json", "history",
                        "没人叫她时她自己挑着做的事"),
        "pool:obsessions": ("执念池", "memory/执念池.json", "open",
                            "还放不下的话头"),
    }
    _parent_id = str(node_id)
    spec = _MAP.get(str(node_id))
    if not spec:
        # 2026-11-07：成员节点（pool:feedback:<at> / pool:exp:<i>）也要能点开。
        # 原来只认锚点 id，点成员一路 404 → 前端「详情加载失败」。
        _parts = str(node_id).split(":")
        if len(_parts) >= 3:
            _head = ":".join(_parts[:2])
            if _head in _MAP:
                spec = _MAP[_head]
                _parent_id = _head
    if not spec:
        return None
    lb, rel, mode, desc = spec
    rows: list[dict] = []
    try:
        fp = ROOT / rel
        if fp.exists():
            if mode == "jsonl":
                for ln in fp.read_text(encoding="utf-8").splitlines()[-60:]:
                    if not ln.strip():
                        continue
                    try:
                        r = json.loads(ln)
                    except Exception:  # noqa: BLE001
                        continue
                    if r.get("reaction"):
                        mark = {"warm": "🟢", "answered": "🟡",
                                "ignored": "⚪", "cold": "🔴"}.get(
                                    str(r.get("reaction")), "·")
                        rows.append({
                            "id": f"pool:feedback:{r.get('at')}",
                            "summary": f"{mark} {str(r.get('text') or '')[:60]}",
                        })
            else:
                d = json.loads(fp.read_text(encoding="utf-8"))
                if mode == "dict":
                    _aff: dict[str, tuple[float, str]] = {}
                    try:
                        from memory_manager import _connect as _mc
                        with _mc() as _c:
                            _cur = _c.cursor()
                            _cur.execute(
                                "SELECT user_id, score, tag FROM affinity "
                                "ORDER BY score DESC")
                            for _u, _sc, _tg in _cur.fetchall():
                                _aff[str(_u)] = (float(_sc or 0), str(_tg or ""))
                    except Exception:  # noqa: BLE001
                        pass
                    for k, v in list(d.items()):
                        sc, tg = _aff.get(str(k), (0.0, ""))
                        rows.append({"id": f"person:{k}",
                                     "summary": f"{tg or k}（好感 {sc:.0f}）"})
                    rows.sort(key=lambda x: -float(
                        re.search(r"好感 (-?\d+)", x["summary"]).group(1))
                        if re.search(r"好感 (-?\d+)", x["summary"]) else 0)
                else:
                    data = d.get(mode) if isinstance(d, dict) else d
                    if mode == "open":
                        data = [x for x in (data or [])
                                if str((x or {}).get("status")) == "open"]
                    for i, x in enumerate((data or [])[:60]):
                        if isinstance(x, dict):
                            t = (x.get("text") or x.get("fact") or x.get("out")
                                 or x.get("title") or x.get("dream")
                                 or json.dumps(x, ensure_ascii=False))
                        else:
                            t = str(x)
                        rows.append({"id": f"{_parent_id}:{i}",
                                     "summary": str(t)[:80]})
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[池子详情] {node_id} 读取失败：{type(exc).__name__}: {exc}")

    if _parent_id != str(node_id):
        _key = str(node_id)[len(_parent_id) + 1:]
        _hit = next((r for r in rows if r["id"] == str(node_id)), None)
        if not _hit and _key.isdigit():
            # 2026-11-07：星海图的成员光球 id 是 pool:feedback:<序号>（0~9），
            # 不是真实条目 id。按序号取，别再 404。
            _i = int(_key)
            _hit = rows[_i] if 0 <= _i < len(rows) else None
        if not _hit:
            # 序号越界 / 条目已被清理：给说明，别让面板报「详情加载失败」
            return {
                "node": {"id": node_id, "kind": "memory", "scope": "__pools__",
                         "parentId": _parent_id, "topicId": None,
                         "summary": f"{lb}·这条不在当前列表里", "emotion": "中性",
                         "sourceMessageIds": [], "retentionScore": 1.0,
                         "importance": 0.5, "recency": 1.0, "frequency": 0.3,
                         "category": "pool", "createdAt": "", "lastAccessedAt": ""},
                "speaker": None, "personElsewhere": [],
                "sourceMessages": [], "relatedNodeIds": [], "related": [],
                "pool": {"count": 0, "kind": "pool-member"},
                "note": f"{desc} · 这条已经不在最近列表里（可能被清理或序号越界），点父节点看全部。",
            }
        return {
            "node": {"id": node_id, "kind": "memory", "scope": "__pools__",
                     "parentId": _parent_id, "topicId": None,
                     "summary": _hit["summary"], "emotion": "中性",
                     "sourceMessageIds": [], "retentionScore": 1.0,
                     "importance": 0.8, "recency": 1.0, "frequency": 0.5,
                     "category": "pool", "createdAt": "", "lastAccessedAt": ""},
            "speaker": None,
            "personElsewhere": [],
            "sourceMessages": [{"id": "1", "senderId": lb, "ts": "",
                                "text": _hit["summary"]}],
            "relatedNodeIds": [],
            "related": [{"id": _hit["id"], "summary": _hit["summary"]}],
            "pool": {"count": 1, "kind": "pool-member"},
            "note": desc,
        }

    return {
        "node": {
            "id": node_id, "kind": "category", "scope": "__pools__",
            "parentId": None, "topicId": None,
            "summary": lb, "emotion": "中性",
            "sourceMessageIds": [],
            "retentionScore": 1.0, "importance": 1.0,
            "recency": 1.0, "frequency": 0.5,
            "category": "pool", "createdAt": "", "lastAccessedAt": "",
        },
        "speaker": None,
        "personElsewhere": [],
        "sourceMessages": [],
        "relatedNodeIds": [r["id"] for r in rows],
        "related": rows,
        "pool": {"count": len(rows), "kind": "pool"},
        "note": f"{desc} · 共 {len(rows)} 条。点下面任意一条跳到它。",
    }
