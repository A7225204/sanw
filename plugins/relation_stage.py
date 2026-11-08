"""关系的四个阶段：初识 → 试探 → 亲密 → 稳定。只升不降。

来源：LIA-Assistant Psyche Engine（ADR-068）的 Layer 4 ——
> Relationship never regresses: Stages (ORIENTATION → EXPLORATORY → AFFECTIVE → STABLE)
> are one-way. Absence decays `warmth_active` but depth and stage persist.

我们的问题：只有一个 `affinity` 分数（0~100）。
吵一次架掉 20 分，和好后涨回来 —— **没有"经历过就是经历过"的概念**。
在一起半年的人，不该因为一次吵架就回到陌生。

做法：
- `stage`   单向，只升不降
- `depth`   单向，记录"一起经历过多少"（互动次数 + 冲突修复）
- `warmth`  会衰减，很久没说话就变淡（但 stage / depth 不变）

存在 shared_memory.db 的 `relation_stage` 表（新表，不动 affinity）。
"""
from __future__ import annotations

import sqlite3
import time

# (阶段, 互动次数门槛, 好感门槛, 是否有过冲突修复)
# 2026-11-07 调整：原来「稳定」要求必然有过 repair（和好过），
# 结果主人 2186 条互动、好感 72 也升不上去 —— 门槛错在把加分项当必要条件了。
# 一起经历两百次对话本身就是稳定；repair 改成"提前一班车的加速"，不是门票。
STAGES = [
    ("初识", 0, 0, False),
    ("试探", 20, 45, False),
    ("亲密", 80, 60, False),
    ("稳定", 200, 70, False),
]

STAGE_LABEL = {
    "初识": "还不熟，客气里带点小心",
    "试探": "开始熟络，会主动搭话但还留着余地",
    "亲密": "已经很熟了，能开玩笑也能撒娇",
    "稳定": "老关系了，不用刻意也不会散",
}


def _conn() -> sqlite3.Connection:
    from memory_manager import _connect, init_db
    init_db()
    return _connect()


def _ensure_table(c: sqlite3.Cursor) -> None:
    c.execute(
        """CREATE TABLE IF NOT EXISTS relation_stage
           (user_id TEXT PRIMARY KEY,
            stage TEXT DEFAULT '初识',
            depth INTEGER DEFAULT 0,
            warmth REAL DEFAULT 50.0,
            repairs INTEGER DEFAULT 0,
            updated_at REAL DEFAULT 0)"""
    )


def _row(c: sqlite3.Cursor, user_id: str) -> tuple:
    _ensure_table(c)
    c.execute("INSERT OR IGNORE INTO relation_stage (user_id, updated_at) VALUES (?, ?)",
              (str(user_id), time.time()))
    c.execute(
        "SELECT stage, depth, warmth, repairs, updated_at FROM relation_stage "
        "WHERE user_id = ?", (str(user_id),))
    return c.fetchone() or ("初识", 0, 50.0, 0, 0.0)


def _stage_index(name: str) -> int:
    for i, (s, *_rest) in enumerate(STAGES):
        if s == name:
            return i
    return 0


def get(user_id: str) -> dict:
    """读当前关系状态（warmth 会按天数自然衰减）。"""
    uid = str(user_id)
    try:
        with _conn() as conn:
            c = conn.cursor()
            stage, depth, warmth, repairs, updated = _row(c, uid)
            # warmth 衰减：每 3 天没互动掉 8 点，止于 15
            days = max(0.0, (time.time() - float(updated or 0)) / 86400.0)
            warmth = max(15.0, float(warmth or 50.0) - days / 3.0 * 8.0)
            c.execute("UPDATE relation_stage SET warmth = ? WHERE user_id = ?",
                      (warmth, uid))
            conn.commit()
    except Exception:  # noqa: BLE001
        return {"stage": "初识", "depth": 0, "warmth": 50.0, "repairs": 0,
                "label": STAGE_LABEL["初识"]}
    return {
        "stage": stage, "depth": int(depth), "warmth": round(warmth, 1),
        "repairs": int(repairs), "label": STAGE_LABEL.get(stage, ""),
    }


_LAST_TOUCH: dict[str, float] = {}
TOUCH_GAP = 60.0     # 同一个人的两次 touch 至少隔 60 秒 ——
                     # build_prompt_parts 一轮里可能被调好几次，不去重 depth 会虚涨


def touch(user_id: str, delta_depth: int = 1, warmth: float = 2.0,
          force: bool = False) -> dict:
    """一次互动：depth 涨、warmth 涨，然后看能不能升阶段。"""
    uid = str(user_id)
    now = time.time()
    if not force and now - float(_LAST_TOUCH.get(uid) or 0) < TOUCH_GAP:
        return get(uid)
    _LAST_TOUCH[uid] = now
    # 本我强的人一凑近就暖得快
    _gate, _warm = _patience()
    warmth = warmth * _warm
    try:
        with _conn() as conn:
            c = conn.cursor()
            stage, depth, w, repairs, _u = _row(c, uid)
            depth = int(depth) + max(0, int(delta_depth))
            w = max(0.0, min(100.0, float(w) + float(warmth)))
            c.execute(
                "UPDATE relation_stage SET depth = ?, warmth = ?, updated_at = ? "
                "WHERE user_id = ?", (depth, w, time.time(), uid))
            conn.commit()
    except Exception:  # noqa: BLE001
        return get(uid)
    return _maybe_promote(uid)


def note_repair(user_id: str) -> dict:
    """一次冲突被修复 —— 这是「稳定」的必要条件。"""
    uid = str(user_id)
    try:
        with _conn() as conn:
            c = conn.cursor()
            stage, depth, w, repairs, _u = _row(c, uid)
            c.execute(
                "UPDATE relation_stage SET repairs = ?, warmth = ?, updated_at = ? "
                "WHERE user_id = ?",
                (int(repairs) + 1, max(0.0, min(100.0, float(w) + 5.0)),
                 time.time(), uid))
            conn.commit()
    except Exception:  # noqa: BLE001
        return get(uid)
    return _maybe_promote(uid)


def _patience() -> tuple[float, float]:
    """超我越严越慢热；本我越强越容易凑近。

    返回 (门槛倍率, warmth 涨落倍率)。
    拿不到三我就按 1.0（不动）。
    """
    try:
        from server import soul_aggregation as sa
        f = sa.view().get("forces") or {}
        idf = float(f.get("本我") or 0.4)
        sef = float(f.get("超我") or 0.4)
    except Exception:  # noqa: BLE001
        return 1.0, 1.0
    # 超我 0.4 → 1.0；0.8 → 1.3（更难亲近）；0.2 → 0.85
    gate = max(0.7, min(1.5, 0.7 + sef * 0.75))
    # 本我强 → warmth 涨得快
    warm = max(0.6, min(1.6, 0.6 + idf * 1.0))
    return gate, warm


def _maybe_promote(user_id: str) -> dict:
    """看够不够格升阶段。**只升不降**。"""
    cur = get(user_id)
    now_idx = _stage_index(cur["stage"])
    gate, _warm = _patience()
    # 好感从 affinity 表取
    try:
        from memory_manager import get_affinity
        aff = int(get_affinity(user_id))
    except Exception:  # noqa: BLE001
        aff = 50
    best = now_idx
    for i, (_s, min_depth, min_aff, need_repair) in enumerate(STAGES):
        if i <= now_idx:
            continue
        # 门槛按三我调：超我严的人要多相处一阵才认
        if cur["depth"] < min_depth * gate or aff < min_aff * gate:
            continue
        if need_repair and int(cur["repairs"]) <= 0:
            continue
        best = i
    if best == now_idx:
        cur["label"] = STAGE_LABEL.get(cur["stage"], "")
        return cur
    new_stage = STAGES[best][0]
    try:
        with _conn() as conn:
            c = conn.cursor()
            _ensure_table(c)
            # 单向：只在"新阶段更高级"时写
            c.execute(
                "UPDATE relation_stage SET stage = ?, updated_at = ? "
                "WHERE user_id = ? AND stage = ?",
                (new_stage, time.time(), str(user_id), cur["stage"]))
            conn.commit()
    except Exception:  # noqa: BLE001
        pass
    out = get(user_id)
    out["promoted_from"] = cur["stage"]
    return out


def line(user_id: str) -> str:
    """给 prompt 的一行。空串表示没什么特别的（初识阶段不占 token）。"""
    s = get(user_id)
    if s["stage"] == "初识":
        return ""
    return f"你们的关系：{s['stage']}（{s['label']}）"
