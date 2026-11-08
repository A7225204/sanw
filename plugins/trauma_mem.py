"""创伤记忆：高情绪强度的负面事件，长期保留、可被线索触发、可修复。

设计要点（避免变成记仇怪）
--------------------------
· 只有"辱骂/欺骗/当众羞辱"这类高强度事件才记，且必须是群友（主人不算）
· 上限 6 条，超出替换电荷最低的
· 半衰期 1200 天（几乎不忘），但会被善意互动和主人调和削弱
· 触发：消息里出现触发词 → 皮质醇飙升 + 回复切防御语气（30% 才提往事）
· 数据存 memory/trauma.json，和记忆可视化联动
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILE = ROOT / "memory" / "trauma.json"

ENABLED = os.getenv("TRAUMA_ENABLED", "1").lower() in ("1", "true", "yes")
MAX_ITEMS = int(os.getenv("TRAUMA_MAX", "6") or "6")
MIN_CHARGE = float(os.getenv("TRAUMA_MIN_CHARGE", "0.55") or "0.55")
TRIGGER_PROB = float(os.getenv("TRAUMA_TRIGGER_PROB", "0.3") or "0.3")

# 辱骂/欺骗类线索（命中越多越像真的）
SEVERE_WORDS = ("骗子", "假的", "装的", "滚", "垃圾", "废物", "傻", "蠢", "封杀", "举报你",
                "恶心", "恶心人", "骗人", "糊弄", "滚出去", "闭嘴", "恶心死了", "欠揍")
SOFT_WORDS = ("假", "骗", "糊弄", "不行", "废物")

_state: dict | None = None


def _load() -> dict:
    global _state
    if _state is None:
        try:
            _state = json.loads(FILE.read_text(encoding="utf-8-sig"))
            if not isinstance(_state, dict):
                _state = {}
        except Exception:  # noqa: BLE001
            _state = {}
        _state.setdefault("items", [])
    return _state


def _save() -> None:
    try:
        FILE.parent.mkdir(parents=True, exist_ok=True)
        FILE.write_text(json.dumps(_load(), ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def severity(text: str) -> int:
    t = str(text or "")
    return sum(1 for w in SEVERE_WORDS if w in t)


def should_record(text: str, is_owner: bool) -> bool:
    if not ENABLED or is_owner:
        return False
    return severity(text) >= 1 and len(str(text or "")) >= 4


def register(scope: str, subject: str, fact: str, text: str) -> dict | None:
    """记录一条创伤；charge 由严重度决定，1 条/人（同人同群只记最重的一条）。"""
    if not ENABLED:
        return None
    st = _load()
    items = st["items"]
    charge = min(1.0, 0.55 + 0.15 * severity(text))
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    trig = sorted({w for w in SEVERE_WORDS if w in str(text)})
    for it in items:
        if it.get("scope") == scope and it.get("subject") == subject:
            it["charge"] = max(float(it.get("charge") or 0), charge)
            it["fact"] = str(fact)[:120]
            it["triggers"] = sorted(set(it.get("triggers") or []) | set(trig))
            it["updated_at"] = now
            _save()
            return it
    if len(items) >= MAX_ITEMS:
        items.sort(key=lambda x: float(x.get("charge") or 0))
        items.pop(0)
    item = {"scope": scope, "subject": subject, "fact": str(fact)[:120], "charge": charge,
            "triggers": trig, "since": now, "updated_at": now, "repair": 0.0}
    items.append(item)
    _save()
    return item


def match(text: str, scope: str = "") -> dict | None:
    """当前消息是否触发某条创伤；返回最重的那条。"""
    if not ENABLED:
        return None
    t = str(text or "")
    if not t:
        return None
    best = None
    best_score = 0.0
    for it in _load()["items"]:
        if scope and it.get("scope") != scope:
            continue
        charge = float(it.get("charge") or 0)
        if charge < 0.15:
            continue
        hit = sum(1 for w in (it.get("triggers") or []) if w and w in t)
        if not hit:
            continue
        score = hit * charge
        if score > best_score:
            best, best_score = it, score
    if not best:
        return None
    if random_ok():
        return best
    return {"_silent": True, **best}


def random_ok() -> bool:
    import random
    return random.random() < TRIGGER_PROB


def repair(scope: str, subject: str, amount: float = 0.15) -> dict | None:
    """善意/道歉削弱电荷；charge 低于 0.15 视为淡忘。"""
    for it in _load()["items"]:
        if it.get("scope") == scope and it.get("subject") == subject:
            it["charge"] = max(0.0, float(it.get("charge") or 0) - amount)
            it["repair"] = round(float(it.get("repair") or 0) + amount, 3)
            it["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _save()
            return it
    return None


def tick() -> None:
    """每天衰减：半衰期 1200 天（几乎不忘），低于阈值标记淡忘。"""
    if not ENABLED:
        return
    st = _load()
    changed = False
    now = time.time()
    for it in st["items"]:
        try:
            ts = time.mktime(time.strptime(str(it.get("updated_at"))[:19], "%Y-%m-%d %H:%M:%S"))
        except Exception:  # noqa: BLE001
            continue
        days = max(0.0, (now - ts) / 86400.0)
        if days < 0.5:
            continue
        factor = 0.5 ** (days / 1200.0)
        it["charge"] = round(float(it.get("charge") or 0) * factor, 4)
        it["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        changed = True
    if changed:
        _save()


def all_items() -> list[dict]:
    return list(_load()["items"])


def by_subject(scope: str, subject: str) -> dict | None:
    for it in _load()["items"]:
        if it.get("scope") == scope and it.get("subject") == subject:
            return it
    return None

def _now_ts() -> float:
    return time.time()


def effective_charge(it: dict) -> float:
    """带修复计划的电荷：快速修复期间线性下降，完成后归零。"""
    charge = float(it.get("charge") or 0)
    plan = it.get("repair_plan") or {}
    if not plan:
        return charge
    try:
        started = float(plan.get("started") or 0)
        dur = float(plan.get("duration") or 0)
    except (TypeError, ValueError):
        return charge
    if dur <= 0:
        return charge
    prog = max(0.0, min(1.0, (_now_ts() - started) / dur))
    from_charge = float(plan.get("from_charge") or charge)
    return max(0.0, from_charge * (1.0 - prog))


def start_repair(scope: str | None, subject: str, mode: str = "quick",
                 duration_min: float = 10.0, index: int | None = None) -> dict | None:
    """一键修复（instant，立即归零）/ 快速修复（quick，按时间线性掉）。

    index（列表序号）优先：有些条目没有 scope 字段（如「主人」那两条），
    按 (scope, subject) 匹配不到，用序号最可靠。
    """
    items = _load()["items"]
    # ① 先按序号精确命中
    if index is not None and 0 <= index < len(items):
        it = items[index]
        if str(it.get("subject") or "") == subject or not subject:
            return _do_repair(it, mode, duration_min)
    # ② 退回按 (scope, subject) 匹配；scope 为空时匹配同为空的条目
    for it in items:
        same_scope = str(it.get("scope") or "") == str(scope or "")
        if not same_scope or it.get("subject") != subject:
            continue
        return _do_repair(it, mode, duration_min)
    return None


def _do_repair(it: dict, mode: str, duration_min: float) -> dict:
    """真正的修复动作（抽出来给 start_repair 复用）。"""
    now = _now_ts()
    if mode == "instant":
        it["charge"] = 0.0
        it["repair"] = round(float(it.get("repair") or 0) + 1.0, 3)
        it["repair_plan"] = None
        it["repaired_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    else:
        dur = max(1.0, float(duration_min)) * 60.0
        it["repair_plan"] = {
            "started": now,
            "duration": dur,
            "from_charge": effective_charge(it),
        }
    it["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save()
    plan = it.get("repair_plan") or {}
    remain = 0
    if plan:
        remain = int(max(0.0, float(plan.get("started") or 0) + float(plan.get("duration") or 0) - now))
    return {
        "scope": it.get("scope") or "", "subject": it.get("subject") or "", "mode": mode,
        "charge": round(effective_charge(it), 4),
        "remainingSec": remain,
    }


def repair_status(it: dict) -> dict:
    plan = it.get("repair_plan") or {}
    remain = 0
    progress = 0.0
    if plan:
        try:
            start = float(plan.get("started") or 0)
            dur = float(plan.get("duration") or 0)
            progress = max(0.0, min(1.0, (_now_ts() - start) / dur)) if dur > 0 else 0.0
            remain = int(max(0.0, start + dur - _now_ts()))
        except (TypeError, ValueError):
            pass
    return {"charge": round(effective_charge(it), 4), "progress": round(progress, 3),
            "remainingSec": remain, "repairing": bool(plan and remain > 0)}
