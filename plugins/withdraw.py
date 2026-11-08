"""回避档：被凶后短暂"不接话"（参考 fish_heart 的阶梯式已读不回）。

约定
----
· blame 0~100：被凶 +8（2026-11-03 从 18 降到 8），每 6 小时 -8，止于 40（心结，不会自己好干净）
· 四档：<25 平静 / 25~55 委屈 / 55~80 害怕 / >=80 回避
· 回避档：私聊前 5 条一条不回，第 6 条 50%，第 7 条起必回（写死 SHUT_LADDER）
· 指令、道歉必回——她可以冷着脸，但不能让心结永远解不开
· 被无视的话原文存着（最多 7 条），等她开口时一次性补给她
· 只影响"回不回"，不改记忆、不碰别的插件数据
"""
from __future__ import annotations

import json
import os
import random
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILE = ROOT / "memory" / "withdraw_state.json"

ENABLED = os.getenv("WITHDRAW_ENABLED", "1").lower() in ("1", "true", "yes")
OFFENSE_GAIN = float(os.getenv("WITHDRAW_OFFENSE_GAIN", "8") or "8")   # 2026-11-03 从 18 降到 8
DECAY_PER_6H = float(os.getenv("WITHDRAW_DECAY", "8") or "8")
FLOOR = float(os.getenv("WITHDRAW_FLOOR", "40") or "40")
SHUT_AT = float(os.getenv("WITHDRAW_SHUT_AT", "80") or "80")
MAX_PENDING = 7

# 第 1~7 条的回概率：前 5 条 0，第 6 条 50%，第 7 条必回
SHUT_LADDER = (0.0, 0.0, 0.0, 0.5, 1.0)   # 前 3 条故意不回，第 4 条 50%，第 5 条必回（2026-11-04 主人调）
GROUP_GRACE = 1  # 群聊刚被打进回避时多放行一句

APOLOGY_KWS = ("对不起", "我错了", "抱歉", "原谅", "别生气", "不该", "哄你", "sorry")

_state: dict | None = None
_mtime = 0.0


def _now() -> float:
    return time.time()


def _load() -> dict:
    global _state, _mtime
    try:
        m = FILE.stat().st_mtime
    except Exception:  # noqa: BLE001
        m = 0.0
    if _state is None or (m and m != _mtime):
        try:
            data = json.loads(FILE.read_text(encoding="utf-8-sig"))
        except Exception:  # noqa: BLE001
            data = {}
        _state = {
            "blame": float(data.get("blame") or 0),          # 对主人的心结
            "group_blame": float(data.get("group_blame") or 0),  # 对群友的火气（2026-11-04 拆开）
            "silent_streak": int(data.get("silent_streak") or 0),
            "group_streak": int(data.get("group_streak") or 0),
            "pending": list(data.get("pending") or [])[-MAX_PENDING:],
            "last_decay": float(data.get("last_decay") or _now()),
        }
        _mtime = m
    return _state


def _save() -> None:
    global _mtime
    s = _load()
    try:
        FILE.parent.mkdir(parents=True, exist_ok=True)
        FILE.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
        _mtime = FILE.stat().st_mtime
    except Exception:  # noqa: BLE001
        pass


def _clamp(v: float) -> float:
    return max(0.0, min(100.0, v))


def tick() -> float:
    """每满 6 小时衰减 8 点，止于 FLOOR。"""
    s = _load()
    now = _now()
    hours = (now - float(s.get("last_decay") or now)) / 3600.0
    if hours < 6:
        return s["blame"]
    drops = int(hours / 6.0)
    s["blame"] = _clamp(max(FLOOR, s["blame"] - drops * DECAY_PER_6H))
    s["group_blame"] = _clamp(max(0.0, float(s.get("group_blame") or 0) - drops * DECAY_PER_6H))
    s["last_decay"] = now
    _save()
    return s["blame"]


def take_offense(amount: float | None = None, target: str = "owner") -> float:
    """被凶：记一笔。

    target="owner"（默认）→ 对主人的心结，影响私聊回不回
    target="group"          → 对群友的火气，**不影响**对主人说话
    """
    if not ENABLED:
        return 0.0
    s = _load()
    gain = OFFENSE_GAIN if amount is None else amount
    if target == "group":
        s["group_blame"] = _clamp(float(s.get("group_blame") or 0) + gain)
    else:
        s["blame"] = _clamp(s["blame"] + gain)
    s["last_decay"] = _now()
    _save()
    return s["blame"]


def soothe(amount: float = 18.0) -> float:
    """被哄/道歉：blame 落。"""
    if not ENABLED:
        return 0.0
    s = _load()
    s["blame"] = _clamp(s["blame"] - amount)
    _save()
    return s["blame"]


def _tier(b: float) -> str:
    if b < 25:
        return "平静"
    if b < 55:
        return "委屈"
    if b < SHUT_AT:
        return "害怕"
    return "回避"


def level(target: str = "owner") -> tuple[str, float]:
    """target="owner" 看对主人的心结；target="group" 看对群友的火气。"""
    tick()
    s = _load()
    b = float(s.get("group_blame") or 0) if target == "group" else float(s["blame"])
    return _tier(b), b


def group_level() -> tuple[str, float]:
    return level("group")


def group_line() -> str:
    """群里火气大时给她一句提示；不影响对主人。"""
    name, b = group_level()
    if name == "回避":
        return f"【对这群人】你现在挺烦（{b:.0f}），话少一点，别陪着起哄。"
    if name == "害怕":
        return f"【对这群人】你有点不痛快（{b:.0f}），不必热络。"
    return ""


def is_apology(text: str) -> bool:
    return any(k in (text or "") for k in APOLOGY_KWS)


def _ladder(streak: int) -> float:
    return SHUT_LADDER[min(streak, len(SHUT_LADDER) - 1)]


def should_reply(channel: str, text: str, is_cmd: bool = False) -> bool:
    """回避档下决定回不回。channel: 'private' | 'group'。"""
    if not ENABLED:
        return True
    name, _b = level()
    if name != "回避":
        return True
    if is_cmd or is_apology(text):
        return True
    s = _load()
    key = "group_streak" if channel == "group" else "silent_streak"
    streak = int(s.get(key) or 0)
    if channel == "group" and streak < GROUP_GRACE:
        s[key] = streak + 1
        _save()
        return True
    if random.random() < _ladder(streak):
        s[key] = 0
        _save()
        return True
    s[key] = streak + 1
    _save()
    return False


def note_silent(text: str) -> None:
    """把被无视的话原文存起来（最多 7 条）。"""
    if not text:
        return
    s = _load()
    s["pending"] = (list(s.get("pending") or []) + [str(text)[:200]])[-MAX_PENDING:]
    _save()


def take_pending() -> list[str]:
    """取出被无视的话（取完清空）。"""
    s = _load()
    items = list(s.get("pending") or [])
    if items:
        s["pending"] = []
        _save()
    return items


def reset() -> None:
    global _state
    _state = {"blame": 0.0, "silent_streak": 0, "group_streak": 0,
              "pending": [], "last_decay": _now()}
    _save()


# ───────────────────────── 群聊 → 私聊 前因桥接 ─────────────────────────
# 群聊和私聊是两本账：群里凶完切私聊，她只知道自己"被凶了"，不知道因为哪句。
# 这里在群里维护一条流水（最近 8 条），凶她那一刻快照触发句 + 前 3 条，私聊时补给她。

def note_group_line(who: str, text: str) -> None:
    """群里每来一句就滚动记一条（who: 他 / 你 / 群友）。"""
    if not text:
        return
    s = _load()
    log = list(s.get("group_log") or [])
    log.append({"at": time.strftime("%H:%M"), "who": who, "text": str(text)[:120]})
    s["group_log"] = log[-8:]
    _save()


def snapshot_blame(trigger: str) -> None:
    """被凶那一刻：把触发句 + 前 3 条流水快照下来（12 小时有效）。"""
    s = _load()
    lines = list(s.get("group_log") or [])[-3:]
    lines.append({"at": time.strftime("%H:%M"), "who": "他", "text": str(trigger)[:120]})
    s["blame_snapshot"] = {"at": _now(), "lines": lines}
    _save()


def take_preamble() -> str:
    """私聊时取前因（只在害怕/回避档给，12 小时有效，取一次清空）。"""
    name, _b = level()
    s = _load()
    snap = s.get("blame_snapshot") or {}
    if not snap:
        return ""
    if _now() - float(snap.get("at") or 0) > 12 * 3600 or name not in ("害怕", "回避"):
        if name == "平静":
            s["blame_snapshot"] = None
            _save()
        return ""
    lines = snap.get("lines") or []
    s["blame_snapshot"] = None
    _save()
    if not lines:
        return ""
    out = "## 群里刚才的几句（你因为这些被凶了）\n"
    for ln in lines:
        out += f"- {ln.get('at', '')} {ln.get('who', '')}：{ln.get('text', '')}\n"
    out += "这是背景，不是要你复述。你现在还在生气/委屈，知道自己在气什么、气哪一句，就够了。"
    return out
