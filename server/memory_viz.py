"""记忆可视化后端：一份数据、两种投影（树 / 星海）。

数据源只有一处：memory/长期记忆.json 的 archive（scope → [{id, subject, fact, time, w, h, lu}]）。
· 树投影：scope 根 → 主题(subject) → 记忆(fact)
· 星海投影：同一批节点 + 离线预计算坐标（memory/memory_starfield.json）

节点 id 规则（两边必须严格一致）：
    scope:<scope>                根节点
    topic:<scope>:<hash(subject)> 主题
    mem:<scope>:<fact-id>        记忆
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LM_FILE = ROOT / "memory" / "长期记忆.json"
STAR_FILE = ROOT / "memory" / "memory_starfield.json"

SCOPE_LABELS = {"global_learn": "自学池", "douyin": "抖音"}


# ---------------------------------------------------------------- 基础

def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8-sig")) or {}
    except Exception:  # noqa: BLE001
        return {}


def _write_json(p: Path, data: dict) -> None:
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _scope_label(scope: str) -> str:
    names = dict(SCOPE_LABELS)
    gp = _read_json(ROOT / "memory" / "group_permissions.json")
    for gid, info in (gp.get("groups") or {}).items():
        nm = str((info or {}).get("name") or "").strip()
        if nm:
            names[f"group_{gid}"] = nm
    if scope in names:
        return names[scope]
    if scope.startswith("private_"):
        who = scope.split("_", 1)[1]
        return "私聊 " + ("主人" if who.isdigit() and len(who) <= 9 else who[-6:])
    if scope.startswith("group_"):
        return "群 " + scope.split("_", 1)[1]
    return scope


def _topic_id(scope: str, subject: str) -> str:
    h = hashlib.sha1(f"{scope}|{subject}".encode("utf-8")).hexdigest()[:10]
    return f"topic:{scope}:{h}"


def _color_of(topic_id: str) -> str:
    h = int(hashlib.sha1(topic_id.encode("utf-8")).hexdigest()[:8], 16)
    hue = h % 360
    return f"hsl({hue}, 78%, 62%)"


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


def _half_life_days(it: dict) -> float:
    """和 long_memory 的艾宾浩斯保持一致的粗版：约定类忘得慢，日常类忘得快。"""
    text = f"{it.get('subject') or ''}{it.get('fact') or ''}"
    base = 60.0
    if any(k in text for k in ("约定", "承诺", "答应", "喜欢", "讨厌", "习惯", "名字", "外号", "忌口")):
        base = 180.0
    elif any(k in text for k in ("今天", "昨天", "吃了", "天气", "心情", "困", "饿")):
        base = 35.0
    hits = int(it.get("h") or 0)
    return min(500.0, base * (1.0 + 0.55 * hits))


# ---------------------------------------------------------------- 情绪倾向（蓝=正面，红=负面）

_POS_WORDS = ("喜欢", "开心", "高兴", "答应", "约定", "夸", "可爱", "萌", "好棒", "谢谢",
              "喜欢", "擅长", "顺利", "成功", "陪", "宠", "温柔", "笑", "靠谱", "在意",
              "认真", "进步", "可爱", "软", "乖", "喜欢")
_NEG_WORDS = ("讨厌", "生气", "烦", "嫌弃", "骂", "难过", "哭", "失败", "吵架", "威胁",
              "不爽", "敷衍", "拒绝", "累", "崩溃", "难受", "挑", "怼", "差", "烂",
              "错", "矛盾", "争议", "吵", "不理", "生气",
              "疼", "痛", "心疼", "惨", "可怜")


_EMOTIONS = (
    ("怒", ("生气", "愤怒", "火大", "怒", "吼", "骂", "怼", "凶", "凶巴巴", "翻脸", "炸")),
    ("累", ("累", "疲惫", "困", "熬", "加班", "歇", "没力气", "不睡", "熬夜", "撑不住", "忙")),
    ("丧", ("丧", "崩溃", "难过", "哭", "失落", "失败", "难受", "抑郁", "绝望", "委屈",
           "疼", "痛", "心疼", "惨", "可怜")),
    ("烦", ("烦", "烦死", "无语", "嫌弃", "敷衍", "挑", "吵", "不爽", "厌倦", "腻",
           "不喜欢", "讨厌", "反感")),
    ("害怕", ("害怕", "怕", "吓", "恐怖", "紧张", "担心", "慌", "不敢")),
)
_POS = ("喜欢", "开心", "高兴", "答应", "约定", "夸", "可爱", "萌", "好棒", "谢谢",
        "擅长", "顺利", "成功", "陪", "宠", "温柔", "笑", "靠谱", "在意", "认真", "进步")

# 正面细分：按优先级从具体到泛化，命中即返回
_POS_EMOTIONS = (
    ("开心", ("开心", "高兴", "快乐", "哈哈", "笑", "愉快", "兴奋", "爽")),
    ("喜欢", ("喜欢", "爱", "心动", "可爱", "萌", "宠", "温柔", "乖")),
    ("得意", ("夸", "棒", "厉害", "优秀", "佩服", "崇拜", "擅长", "靠谱", "进步")),
    ("感谢", ("谢谢", "感谢")),
    ("约定", ("约定", "答应", "承诺")),
    ("期待", ("期待", "希望", "想要")),
)


_NEGATORS = ("不", "没", "别", "无", "未", "非", "莫", "勿", "甭")


def _is_negated(t: str, i: int) -> bool:
    """词前面两个字里出现否定词就算被否定（“不喜欢”“没生气”）。"""
    pre = t[max(0, i - 2):i]
    return any(ch in pre for ch in _NEGATORS)


def _emotion(text: str) -> str:
    """把记忆分成 怒/累/丧/烦/害怕/正面/中性。顺序就是优先级。

    否定词要生效：命中的负面词若前面有“不/没/别”就跳过；
    正面词被否定则判负面（“不喜欢”归到“烦”），不会再算成正面。
    """
    t = str(text or "")
    for name, words in _EMOTIONS:
        for w in words:
            i = t.find(w)
            while i != -1:
                if not _is_negated(t, i):
                    return name
                i = t.find(w, i + 1)
    for name, words in _POS_EMOTIONS:
        for w in words:
            i = t.find(w)
            while i != -1:
                if not _is_negated(t, i):
                    return name
                i = t.find(w, i + 1)
    pos_negated = False
    for w in _POS:
        i = t.find(w)
        while i != -1:
            if _is_negated(t, i):
                pos_negated = True
            else:
                return "正面"
            i = t.find(w, i + 1)
    return "烦" if pos_negated else "中性"


def _ms(v) -> int:
    """epoch 秒 / 毫秒 / 日期字符串 → 毫秒时间戳（0 表示未知）。"""
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        x = float(v)
        if x <= 0:
            return 0
        return int(x * 1000) if x < 1e11 else int(x)
    s = str(v).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            if fmt == "%m-%d":
                dt = dt.replace(year=datetime.now().year)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    return 0


def _sentiment(text: str) -> float:
    """粗粒度情绪：返回 -1~1（正数=正面，负数=负面）。否定词会翻转计数。"""
    t = str(text or "")
    pos = neg = 0
    for w in _POS_WORDS:
        i = t.find(w)
        while i != -1:
            if _is_negated(t, i):
                neg += 1
            else:
                pos += 1
            i = t.find(w, i + 1)
    for w in _NEG_WORDS:
        i = t.find(w)
        while i != -1:
            if _is_negated(t, i):
                pos += 1
            else:
                neg += 1
            i = t.find(w, i + 1)
    if pos == neg:
        return 0.0
    return round((pos - neg) / max(1, pos + neg), 3)


def _metrics(it: dict, now: float) -> dict:
    w = float(it.get("w") or 1.0)
    lu = float(it.get("lu") or 0.0) or (now - 86400 * 7)
    age_days = max(0.0, (now - lu) / 86400.0)
    rank = w * (0.5 ** (age_days / _half_life_days(it)))
    fact = it.get("fact") or ""
    emo = _emotion(fact)
    sent = _sentiment(fact)
    # 情绪标签和极性必须自洽：标签是正面但极性算成 0（词表没覆盖到）时补一个正值，
    # 否则会出现「喜欢 / 0.00」这种自相矛盾的展示（主人反馈过）。
    if emo in ("开心", "喜欢", "得意", "感谢", "约定", "期待", "正面") and sent <= 0:
        sent = 0.3
    elif emo in ("怒", "烦", "丧", "累", "害怕", "负面") and sent >= 0:
        sent = -0.3
    return {
        "sentiment": sent,
        "emotion": emo,
        "retentionScore": round(_clamp01(rank / 3.0), 4),   # 生效权重 / 上限 3
        "importance": round(_clamp01(w / 3.0), 4),
        "recency": round(_clamp01(1.0 - age_days / 120.0), 4),
        "frequency": round(_clamp01(int(it.get("h") or 0) / 5.0), 4),
    }


def _archive() -> dict:
    data = _read_json(LM_FILE)
    arch = data.get("archive") or {}
    return {k: [x for x in (v or []) if isinstance(x, dict) and x.get("fact")] for k, v in arch.items()}


# ---------------------------------------------------------------- 投影

def _trauma_map(scope: str) -> dict:
    """该 scope 下的创伤条目：subject → charge。"""
    out: dict = {}
    try:
        import json as _j
        data = _j.loads((ROOT / "memory" / "trauma.json").read_text(encoding="utf-8-sig"))
        for it in data.get("items") or []:
            if str(it.get("scope")) != str(scope):
                continue
            out[str(it.get("subject") or "")] = {
                "charge": float(it.get("charge") or 0),
                "triggers": it.get("triggers") or [],
                "since": it.get("since") or "",
            }
    except Exception:  # noqa: BLE001
        pass
    return out


def build_nodes(scope: str) -> tuple[list[dict], list[dict]]:
    """返回 (nodes, tree_edges)。树和星海都用这一份 nodes。"""
    arch = _archive()
    items = arch.get(scope) or []
    now = time.time()
    trauma = _trauma_map(scope)
    nodes: list[dict] = []
    edges: list[dict] = []

    root_id = f"scope:{scope}"
    facts = [_metrics(it, now) for it in items]
    nodes.append({
        "id": root_id, "scope": scope, "parentId": None, "topicId": None,
        "summary": _scope_label(scope), "sourceMessageIds": [],
        "kind": "scope",
        "retentionScore": round(sum(f["retentionScore"] for f in facts) / max(1, len(facts)), 4),
        "importance": 1.0, "recency": 1.0, "frequency": 1.0,
        "createdAt": "", "lastAccessedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })

    by_subject: dict[str, list[dict]] = {}
    for it in items:
        by_subject.setdefault(str(it.get("subject") or "杂项")[:20], []).append(it)

    for subject, group in by_subject.items():
        tid = _topic_id(scope, subject)
        mx = [_metrics(it, now) for it in group]
        nodes.append({
            "id": tid, "scope": scope, "parentId": root_id, "topicId": tid,
            "summary": subject, "sourceMessageIds": [], "kind": "topic",
            "retentionScore": round(sum(m["retentionScore"] for m in mx) / len(mx), 4),
            "importance": round(sum(m["importance"] for m in mx) / len(mx), 4),
            "recency": round(max(m["recency"] for m in mx), 4),
            "frequency": round(sum(m["frequency"] for m in mx) / len(mx), 4),
            "createdAt": "", "lastAccessedAt": "",
        })
        edges.append({"source": root_id, "target": tid, "kind": "hierarchy"})
        for it in group:
            mid = f"mem:{scope}:{it.get('id')}"
            m = _metrics(it, now)
            _tr = trauma.get(str(it.get("subject") or "").strip())
            if _tr and m["sentiment"] > -0.3:
                m["sentiment"] = -0.3   # 创伤条目至少记为明显负面
            nodes.append({
                "id": mid, "scope": scope, "parentId": tid, "topicId": tid,
                "summary": str(it.get("fact"))[:120], "sourceMessageIds": [],
                "kind": "memory", **m,
                "trauma": _tr,
                "_trauma_subject": str(it.get("subject") or "").strip(),
                "createdAt": str(it.get("time") or ""),
                "lastAccessedAt": "",
                "t": _ms(it.get("lu")) or _ms(it.get("time")),
            })
            edges.append({"source": tid, "target": mid, "kind": "hierarchy"})
    # 骨架补时间：scope / topic 取自己名下最早的记忆时间，这样历史轴上
    # 每个星河、每个主题都会按「它第一次出现」的时刻依次冒出来，金线也跟着走。
    mem_t = [n for n in nodes if n.get("kind") == "memory" and int(n.get("t") or 0) > 0]
    for n in nodes:
        if n.get("kind") == "scope":
            ts = [int(m["t"]) for m in mem_t if m.get("scope") == n.get("scope")]
        elif n.get("kind") == "topic":
            ts = [int(m["t"]) for m in mem_t if m.get("topicId") == n.get("topicId")]
        else:
            continue
        if ts:
            n["t"] = min(ts)
    return nodes, edges


def _star_cache() -> dict:
    return _read_json(STAR_FILE)


def _ensure_starfield(scope: str, nodes: list[dict]) -> tuple[dict | None, str]:
    """确保这批节点有坐标缓存；缺失返回 (None, 'computing') 并触发离线任务。"""
    cache = _star_cache()
    entry = (cache.get("scopes") or {}).get(scope) or {}
    ids_now = [n["id"] for n in nodes]
    cached = entry.get("coords") or {}
    missing = [i for i in ids_now if i not in cached]
    if entry and not missing:
        return entry, "ready"
    if not entry and len(nodes) <= 400:
        # 小 scope 直接同步算，体感更顺（真实降维任务仍走 jobs/starfield_layout.py）
        entry = _compute_coords(scope, nodes)
        cache.setdefault("scopes", {})[scope] = entry
        _write_json(STAR_FILE, cache)
        return entry, "ready"
    _spawn_layout_job(scope)
    return None, "computing"


def _spawn_layout_job(scope: str) -> None:
    try:
        import subprocess
        import sys
        subprocess.Popen(
            [sys.executable, str(ROOT / "jobs" / "starfield_layout.py"), "--scope", scope],
            cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:  # noqa: BLE001
        pass


def _compute_coords(scope: str, nodes: list[dict]) -> dict:
    """确定性 3D 坐标（单位球内）。有 umap + 向量就用 UMAP，没有就用哈希球面投影。

    这里只给"缓存缺失时立刻可用"的兜底；正式全量仍由 jobs/starfield_layout.py 跑。
    """
    coords: dict[str, dict] = {}
    for n in nodes:
        seed = hashlib.sha1(n["id"].encode("utf-8")).hexdigest()
        u = int(seed[:8], 16) / 0xFFFFFFFF
        v = int(seed[8:16], 16) / 0xFFFFFFFF
        theta = 2 * math.pi * u
        phi = math.acos(2 * v - 1)
        # 主题靠近球心一圈，记忆在外层；越重要越靠内
        r = 0.35 if n.get("kind") == "topic" else (0.55 + 0.45 * (1 - float(n.get("importance") or 0)))
        if n.get("kind") == "scope":
            r = 0.0
        coords[n["id"]] = {
            "x": round(r * math.sin(phi) * math.cos(theta), 5),
            "y": round(r * math.sin(phi) * math.sin(theta), 5),
            "z": round(r * math.cos(phi), 5),
        }
    return {"coords": coords, "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "method": "deterministic", "count": len(coords)}


def _star_edges(nodes: list[dict], top_ratio: float = 0.2) -> list[dict]:
    """星海边：同主题记忆全部串成链，主题中心再连最重要的前 3 条。

    早前只取每主题前 8 条连链，导致 68% 的记忆是孤岛，这里改成全连。
    """
    by_topic: dict[str, list[dict]] = {}
    for n in nodes:
        if n.get("kind") == "memory" and n.get("topicId"):
            by_topic.setdefault(n["topicId"], []).append(n)
    edges: list[dict] = []
    for topic, group in by_topic.items():
        group.sort(key=lambda x: -float(x.get("importance") or 0))
        # 同主题内全部按重要度顺序串起来（相邻两两相连）
        for i in range(len(group) - 1):
            edges.append({
                "source": group[i]["id"], "target": group[i + 1]["id"],
                "weight": round((group[i]["importance"] + group[i + 1]["importance"]) / 2, 4),
            })
        # 主题中心连到最重要的前 3 条，主题星不再是单线
        for core in group[:3]:
            edges.append({"source": topic, "target": core["id"],
                          "weight": round(float(core.get("importance") or 0), 4)})
    # 星河中心（scope）→ 自己的主题球：让金线落下来之后有路可走
    for n in nodes:
        if n.get("kind") == "topic" and n.get("parentId"):
            edges.append({"source": n["parentId"], "target": n["id"], "weight": 0.6})
    # 只在极端规模才截断，避免上千条记忆时画面变毛线团；正常情况全保留
    if len(edges) > 400:
        edges.sort(key=lambda e: -e["weight"])
        return edges[:400]
    return edges


# ---------------------------------------------------------------- 对外接口

def _pseudo_nodes(scope: str, nodes: list[dict]) -> tuple[list[dict], list[dict]]:
    """树图额外的两个实体节点：创伤源树 / 未竟念头池。

    它们是"库"不是记忆条目：只做入口，点开看数量与去向。
    """
    root = next((n for n in nodes if n.get("kind") == "scope" and n.get("scope") == scope), None)
    if not root:
        return [], []
    pid = root["id"]

    def count_json(path, key="items", pred=None) -> int:
        try:
            d = json.loads(Path(path).read_text(encoding="utf-8-sig"))
            rows = [x for x in (d.get(key) or []) if isinstance(x, dict)]
            return len([x for x in rows if pred(x)]) if pred else len(rows)
        except Exception:  # noqa: BLE001
            return 0

    n_trauma = count_json(ROOT / "memory" / "trauma.json")
    n_thought = 0
    try:
        t = json.loads((ROOT / "memory" / "thoughts.json").read_text(encoding="utf-8-sig"))
        now = time.time()
        n_thought = len([x for x in (t.get("items") or [])
                         if isinstance(x, dict) and float(x.get("created") or 0) > now - 86400 * 7])
    except Exception:  # noqa: BLE001
        n_thought = 0

    base = {
        "scope": scope, "parentId": pid, "topicId": None,
        "sourceMessageIds": [], "kind": "topic",
        "retentionScore": 1.0, "importance": 1.0, "recency": 1.0,
        "frequency": 0.0, "createdAt": "", "lastAccessedAt": "",
    }
    out = [
        {**base, "id": "pseudo:trauma", "summary": f"创伤源树 · {n_trauma}"},
        {**base, "id": "pseudo:thoughts", "summary": f"未竟念头池 · {n_thought}"},
    ]
    edges = [{"source": pid, "target": n["id"], "kind": "hierarchy"} for n in out]
    return out, edges


def tree(scope: str) -> dict:
    nodes, edges = build_nodes(scope)
    # 树图多出两个实体节点：创伤源树 / 未竟念头池（主人 2026-10-31 架构要求）
    _pn, _pe = _pseudo_nodes(scope, nodes)
    nodes = nodes + _pn
    edges = edges + _pe
    return {"scope": scope, "nodes": nodes, "edges": edges,
            "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


def starfield(scope: str) -> tuple[int, dict]:
    nodes, _ = build_nodes(scope)
    entry, state = _ensure_starfield(scope, nodes)
    if state != "ready":
        return 202, {"scope": scope, "status": "computing"}
    coords = entry.get("coords") or {}
    out_nodes = []
    for n in nodes:
        c = coords.get(n["id"])
        if not c:
            continue
        size = round(0.25 + 0.75 * float(n.get("importance") or 0), 4)
        bright = round(0.3 + 0.7 * float(n.get("recency") or 0), 4)
        out_nodes.append({**n, "starfield": {
            "x": c["x"], "y": c["y"], "z": c["z"],
            "size": size, "brightness": bright,
            "color": _color_of(n.get("topicId") or n["id"]),
        }})
    # 2026-11-06：主人要求「人物池 / 事件池 / 反馈池 要出现在树+星海的星海里」。
    # 这些池子在 /api/memory/persona 里有，但星海这边走的是 starfield，看不到。
    # 这里补几个 category 节点，和记忆星点一起画。
    _POOLS = [
        ("pool:people", "人物池", "memory/relation_state.json", "count", 0x7fd4ff),
        ("pool:exp", "事件池", "memory/episodes.json", "items", 0x4ade80),
        ("pool:feedback", "反馈闭环", "memory/proactive_log.jsonl", None, 0xffd166),
        ("pool:agency", "行动空间", "memory/agency_state.json", "history", 0x7ee8c9),
        ("pool:obsessions", "执念池", "memory/执念池.json", "items", 0xff7a9c),
    ]
    _extra = []
    for _i, (_pid, _lb, _f, _key, _col) in enumerate(_POOLS):
        try:
            _fp = ROOT / _f
            if not _fp.exists():
                continue
            if _key is None:
                _n = sum(1 for _ln in _fp.read_text(encoding="utf-8").splitlines()
                         if _ln.strip() and '"reaction"' in _ln)
            else:
                _d = json.loads(_fp.read_text(encoding="utf-8"))
                if _pid == "pool:people":
                    # relation_state 是 {uid: {...}} 的平铺字典，没有 count 键
                    _n = len(_d) if isinstance(_d, dict) else 0
                else:
                    _it = _d.get(_key) if isinstance(_d, dict) else _d
                    if _pid == "pool:obsessions":
                        _it = [x for x in (_it or []) if str((x or {}).get("status")) == "open"]
                    _n = len(_it or [])
            if _n <= 0:
                continue
            import math as _m
            _th = (_i / len(_POOLS)) * 2 * _m.pi
            _r = 2.6
            _extra.append({
                "id": _pid, "kind": "category", "category": "pool",
                "summary": f"{_lb}·{_n}", "count": _n,
                # 前端拿这两个字段算"历史进度"和"关系层级"，缺了尺寸会算成 0
                "t": int(time.time() * 1000),
                "relation": 1,
                "retentionScore": 1.0, "importance": 1.0,
                "recency": 1.0, "frequency": 1.0,
                "createdAt": "", "lastAccessedAt": "",
                "topicId": None, "parentId": None, "scope": scope,
                "sourceMessageIds": [],
                "starfield": {
                    "x": round(_r * _m.cos(_th), 3),
                    "y": round(0.15 + 0.05 * (_i % 2), 3),
                    "z": round(_r * _m.sin(_th), 3),
                    "size": 1.05, "brightness": 0.85,
                    "color": f"#{_col:06x}",
                },
            })
        except Exception:  # noqa: BLE001
            continue
    out_nodes = out_nodes + _extra
    return 200, {
        "scope": scope, "nodes": out_nodes,
        "edges": _star_edges(nodes),
        "generatedAt": entry.get("generatedAt") or "",
        "method": entry.get("method") or "",
    }


def scopes() -> dict:
    arch = _archive()
    out = []
    for scope, items in arch.items():
        out.append({"scope": scope, "label": _scope_label(scope),
                    "count": len(items),
                    "topics": len({str(i.get("subject") or "杂项")[:20] for i in items})})
    total = sum(x["count"] for x in out)
    out.sort(key=lambda x: -x["count"])
    out.insert(0, {"scope": "__all__", "label": "全部星海", "count": total,
                   "topics": sum(x["topics"] for x in out)})
    return {"scopes": out, "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


def _speaker_info(scope: str, subject: str) -> dict:
    """这条记忆属于谁：群名 / 昵称 / QQ 号（昵称→QQ 用 nick_names.json 反查）。"""
    info = {
        "channel": "private" if scope.startswith("private_") else "group" if scope.startswith("group_") else "other",
        "scopeLabel": _scope_label(scope),
        "groupId": "",
        "speaker": str(subject or "").strip(),
        "uid": "",
    }
    if scope.startswith("group_"):
        info["groupId"] = scope.split("_", 1)[1]
        try:
            data = _read_json(ROOT / "memory" / "nick_names.json")
            people = (data.get(info["groupId"]) or {})
            for uid, rec in people.items():
                if not isinstance(rec, dict):
                    continue
                first = str(rec.get("first") or "")
                cur = str(rec.get("current") or "")
                if subject and (subject == first or subject == cur or subject in (first, cur)):
                    info["uid"] = str(uid)
                    info["speaker"] = first or cur or subject
                    break
        except Exception:  # noqa: BLE001
            pass
    elif scope.startswith("private_"):
        who = scope.split("_", 1)[1]
        if who.isdigit():
            info["uid"] = who
            info["speaker"] = info["speaker"] or "主人"
    return info


def _person_elsewhere(scope: str, subject: str) -> list[dict]:
    """同一个人（优先按 QQ，其次昵称）在其它 scope 里的主题节点。"""
    me = _speaker_info(scope, subject)
    uid = str(me.get("uid") or "")
    name = str(subject or "").strip()
    out: list[dict] = []
    arch = _archive()
    for other in arch.keys():
        if other == scope:
            continue
        items = arch.get(other) or []
        subjects = {str(i.get("subject") or "").strip()[:20] for i in items}
        if not subjects:
            continue
        hit_subject = ""
        _self_names = ("本鱼", "鲸鱼娘", "蓝色大肥鱼", "大肥鱼")
        if uid:
            for sub in subjects:
                # 跳过"她自己"的主题：私聊里 subject=本鱼 说的是机器人，不是主人
                if sub in _self_names:
                    continue
                if other.startswith("private_") and sub != "主人":
                    continue
                info = _speaker_info(other, sub)
                if str(info.get("uid") or "") == uid:
                    hit_subject = sub
                    break
        if not hit_subject and name and name in subjects:
            hit_subject = name
        if hit_subject:
            out.append({
                "id": _topic_id(other, hit_subject),
                "scope": other,
                "scopeLabel": _scope_label(other),
                "summary": hit_subject,
                "viaQq": bool(uid),
            })
    return out[:6]


def detail(node_id: str) -> dict | None:
    parts = node_id.split(":")
    if len(parts) < 3:
        return None
    kind, scope = parts[0], parts[1]
    if kind == "mem":
        arch = _archive()
        want = parts[2]
        for it in arch.get(scope) or []:
            if str(it.get("id")) == want:
                src = it.get("src") or []
                src_msgs = [
                    {"id": f"src-{i}", "text": str(x.get("t") or "")[:160],
                     "senderId": str(x.get("s") or ""), "ts": str(x.get("ts") or "")}
                    for i, x in enumerate(src) if isinstance(x, dict)
                ]
                nodes, _ = build_nodes(scope)
                me = next((n for n in nodes if n["id"] == node_id), None)
                sib_nodes = [n for n in nodes
                             if n.get("kind") == "memory"
                             and n.get("topicId") and n.get("topicId") == (me or {}).get("topicId")
                             and n["id"] != node_id]
                siblings = [n["id"] for n in sib_nodes]
                _subj = ""
                for it2 in arch.get(scope) or []:
                    if str(it2.get("id")) == want:
                        _subj = str(it2.get("subject") or "")
                        break
                _tr = None
                try:
                    import importlib.util as _iu2
                    _spec = _iu2.spec_from_file_location("trauma_mem", ROOT / "plugins" / "trauma_mem.py")
                    _mod = _iu2.module_from_spec(_spec); _spec.loader.exec_module(_mod)  # type: ignore[union-attr]
                    _item = _mod.by_subject(scope, str((me or {}).get("_trauma_subject") or ""))
                    if _item:
                        _tr = {**_mod.repair_status(_item), "triggers": _item.get("triggers") or [],
                               "since": _item.get("since") or ""}
                except Exception:  # noqa: BLE001
                    _tr = None
                return {
                    "node": me or {"id": node_id, "scope": scope},
                    "trauma": _tr,
                    # 这条记忆固化时的内分泌快照（新固化的条目才有）
                    "hormones": it.get("hormones") or {},
                    "speaker": _speaker_info(scope, _subj),
                    "personElsewhere": _person_elsewhere(scope, _subj),
                    "sourceMessages": src_msgs,
                    "relatedNodeIds": siblings[:8],
                    "related": [{"id": n["id"], "summary": n.get("summary") or "",
                                 "kind": n.get("kind") or "memory"} for n in sib_nodes[:8]],
                }
        return None
    nodes, _ = build_nodes(scope)
    me = next((n for n in nodes if n["id"] == node_id), None)
    if not me:
        return None
    rel_nodes = [n for n in nodes
                 if n.get("kind") == "memory" and n.get("topicId") == me.get("topicId")][:8]
    return {
        "node": me,
        "speaker": _speaker_info(scope, str(me.get("summary") or "")),
        "personElsewhere": _person_elsewhere(scope, str(me.get("summary") or "")),
        "sourceMessages": [],
        "relatedNodeIds": [n["id"] for n in rel_nodes],
        "related": [{"id": n["id"], "summary": n.get("summary") or "",
                     "kind": n.get("kind") or "memory"} for n in rel_nodes],
    }


# ---------------------------------------------------------------- 写操作

def _load_lm() -> dict:
    return _read_json(LM_FILE)


def _save_lm(data: dict) -> None:
    _write_json(LM_FILE, data)


def _find_item(scope: str, mem_id: str) -> tuple[dict, list, int] | None:
    data = _load_lm()
    items = ((data.get("archive") or {}).get(scope) or [])
    for idx, it in enumerate(items):
        if isinstance(it, dict) and str(it.get("id")) == str(mem_id):
            return data, items, idx
    return None


def set_retention(node_id: str, retention: float) -> dict:
    """把 retentionScore(0~1) 映射回 weight(上限 3)，并把 last_used 设为现在。"""
    parts = node_id.split(":")
    if len(parts) < 3 or parts[0] != "mem":
        return {"ok": False, "error": "只支持记忆节点"}
    scope, mem_id = parts[1], parts[2]
    found = _find_item(scope, mem_id)
    if not found:
        return {"ok": False, "error": "节点不存在"}
    data, items, idx = found
    w = round(max(0.05, min(3.0, float(retention) * 3.0)), 3)
    items[idx]["w"] = w
    items[idx]["lu"] = time.time()
    _save_lm(data)
    return {"ok": True, "nodeId": node_id, "retentionScore": round(w / 3.0, 4)}


def delete_node(node_id: str, force: bool = False) -> dict:
    parts = node_id.split(":")
    if len(parts) < 3:
        return {"ok": False, "error": "bad id"}
    kind, scope = parts[0], parts[1]
    data = _load_lm()
    arch = data.setdefault("archive", {})
    if kind == "mem":
        items = arch.get(scope) or []
        keep = [it for it in items if str((it or {}).get("id")) != str(parts[2])]
        removed = len(items) - len(keep)
        arch[scope] = keep
        _save_lm(data)
        return {"ok": removed > 0, "removed": removed}
    if kind == "topic":
        # 删主题会连带删掉它下面所有记忆 —— 默认拒绝，必须显式 force
        if not force:
            nodes, _ = build_nodes(scope)
            cnt = len([n for n in nodes
                       if n.get("topicId") == node_id and n.get("kind") == "memory"])
            return {"ok": False, "error": f"这是主题节点，下面有 {cnt} 条记忆，需逐条删除",
                    "needForce": True, "memoryCount": cnt}
        nodes, _ = build_nodes(scope)
        topic_id = node_id
        mem_ids = {n["id"].split(":")[-1] for n in nodes
                   if n.get("topicId") == topic_id and n.get("kind") == "memory"}
        items = arch.get(scope) or []
        keep = [it for it in items if str((it or {}).get("id")) not in mem_ids]
        removed = len(items) - len(keep)
        arch[scope] = keep
        _save_lm(data)
        return {"ok": removed > 0, "removed": removed}
    return {"ok": False, "error": "只支持记忆/主题节点"}


def merge_nodes(node_id: str, target_id: str) -> dict:
    """把 node_id 并进 target_id：保留 target 事实，删掉 source（简单合并）。"""
    if node_id == target_id:
        return {"ok": False, "error": "不能合并到自己"}
    src = delete_node(node_id)
    if not src.get("ok"):
        return {"ok": False, "error": "源节点删除失败"}
    return {"ok": True, "merged": node_id, "into": target_id}
