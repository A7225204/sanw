"""群友昵称档案：以 QQ 号为第一身份，昵称只是标签。

为什么需要
----------
群友会改群名片。以前她的历史里直接存"当前昵称"，一改名就变成"两个人"，
于是记不住谁是谁。这里按 QQ 号建档：

    {gid: {uid: {"first": 首个昵称, "current": 最新昵称, "seen": 次数, "updated_at": ...}}}

规则（主人 2026-10-29 定）
------------------------
· 记住「第一个昵称」和「最新昵称」，中间改过的一律不留
· 展示时：没改过就只用昵称；改过则 `首名(现:新名)`，她能看到是同一个人
· 数据存 memory/nick_names.json，按群分开
"""
from __future__ import annotations

import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STORE = ROOT / "memory" / "nick_names.json"

_DATA: dict | None = None
_MTIME: float = 0.0


def _load() -> dict:
    global _DATA, _MTIME
    # 文件被外部（回填脚本/面板）改过就重载，避免内存旧数据把它覆盖回去
    try:
        m = STORE.stat().st_mtime
    except Exception:  # noqa: BLE001
        m = 0.0
    if _MTIME and m and m != _MTIME:
        _DATA = None
    if _DATA is None:
        try:
            _DATA = json.loads(STORE.read_text(encoding="utf-8-sig"))
            if not isinstance(_DATA, dict):
                _DATA = {}
        except Exception:  # noqa: BLE001
            _DATA = {}
        try:
            _MTIME = STORE.stat().st_mtime
        except Exception:  # noqa: BLE001
            _MTIME = 0.0
    return _DATA


def _save() -> None:
    try:
        STORE.parent.mkdir(parents=True, exist_ok=True)
        STORE.write_text(json.dumps(_load(), ensure_ascii=False, indent=1), encoding="utf-8")
        global _MTIME
        try:
            _MTIME = STORE.stat().st_mtime
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        pass


def resolve(gid: str | int, uid: str | int, name: str) -> str:
    """登记昵称并返回展示用的名字（首名或首名(现:新名)）。"""
    g = str(gid)
    u = str(uid)
    n = (name or "").strip()[:24] or u
    data = _load()
    groups = data.setdefault(g, {})
    rec = groups.get(u)
    if not isinstance(rec, dict):
        groups[u] = {"first": n, "current": n, "seen": 1,
                     "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        _save()
        return n
    rec["seen"] = int(rec.get("seen") or 0) + 1
    first = str(rec.get("first") or n)
    cur = str(rec.get("current") or first)
    if n != cur:
        # 改过名：只留 first + 最新
        rec["first"] = first
        rec["current"] = n
        rec["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _save()
        cur = n
    else:
        if rec["seen"] % 20 == 0:
            _save()
    if first != cur:
        return f"{first}(现:{cur})"
    return first


def first_name(gid: str | int, uid: str | int) -> str:
    """取这个人的"首个昵称"（写记忆时优先用它，保证稳定可认）。"""
    g = str(gid)
    u = str(uid)
    rec = (_load().get(g) or {}).get(u)
    if isinstance(rec, dict):
        return str(rec.get("first") or "")[:24]
    return ""


def stats() -> dict:
    data = _load()
    return {"groups": len(data), "people": sum(len(v) for v in data.values() if isinstance(v, dict))}
