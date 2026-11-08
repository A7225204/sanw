"""纪念日 / 生日：让她记得"今天是什么日子"。

和 daily_mood / daily_diary 的区别
----------------------------------
那两个是"今天发生了什么"，这里是"今天是什么日子"——生日、认识第几天、约定日。
数据落在 memory/dates.json，纯本地，不调模型。

用法
----
    from dates import today_line, add, list_all
    line = today_line()        # 有值得提的日子才返回文字，否则空串

只在这些情况往 prompt 里加一行（避免天天念叨）：
- 生日（每年同月同日）
- 纪念日当天 / 整百天 / 满年
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FILE = ROOT / "memory" / "dates.json"

MILESTONES = (7, 30, 50, 100, 200, 300, 365, 500, 730, 1000, 1460)


def _load() -> dict:
    try:
        data = json.loads(FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("items", [])
            data.setdefault("next_id", 1)
            return data
    except Exception:  # noqa: BLE001
        pass
    return {"items": [], "next_id": 1}


def _save(data: dict) -> None:
    try:
        data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        FILE.parent.mkdir(parents=True, exist_ok=True)
        FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _parse(d: str) -> date | None:
    for fmt in ("%Y-%m-%d", "%m-%d", "%Y/%m/%d", "%m/%d"):
        try:
            v = datetime.strptime(d.strip(), fmt)
            return v.date() if fmt.startswith("%Y") else v.replace(year=2000).date()
        except Exception:  # noqa: BLE001
            continue
    return None


def add(name: str, day: str, kind: str = "anniversary", note: str = "") -> tuple[bool, str]:
    """加一条。kind=birthday 按每年同月同日；kind=anniversary 算"第几天"。"""
    parsed = _parse(str(day))
    if not parsed:
        return False, "日期格式看不懂，用 2026-10-20 或 10-20"
    name = str(name or "").strip()[:20]
    if not name:
        return False, "名字不能为空"
    data = _load()
    if any(str(it.get("name")) == name for it in data["items"]):
        return False, f"已经有「{name}」了"
    data["items"].append({
        "id": int(data["next_id"]),
        "name": name,
        "date": parsed.strftime("%Y-%m-%d") if parsed.year != 2000 else parsed.strftime("%m-%d"),
        "kind": "birthday" if kind == "birthday" else "anniversary",
        "note": str(note or "")[:40],
        "created": time.time(),
    })
    data["next_id"] = int(data["next_id"]) + 1
    _save(data)
    return True, f"记住了：{name} = {day}"


def list_all() -> list[dict]:
    return list(_load()["items"])


def remove(tid: int) -> bool:
    data = _load()
    before = len(data["items"])
    data["items"] = [it for it in data["items"] if int(it.get("id") or 0) != int(tid)]
    if len(data["items"]) != before:
        _save(data)
        return True
    return False


def _days_since(start: date, ref: date) -> int:
    return (ref - start).days


def today_line(ref: date | None = None) -> str:
    """今天值得提一句的日子；没有就返回空串。"""
    today = ref or date.today()
    hits: list[str] = []
    for it in list_all():
        parsed = _parse(str(it.get("date") or ""))
        if not parsed:
            continue
        name = str(it.get("name") or "")
        if it.get("kind") == "birthday":
            if (parsed.month, parsed.day) == (today.month, today.day):
                hits.append(f"今天是{name}")
            continue
        # 纪念日：算天数
        start = parsed if parsed.year != 2000 else parsed.replace(year=today.year)
        if start > today:
            continue
        n = _days_since(start, today)
        if n == 0:
            hits.append(f"今天是{name}（就今天）")
        elif (parsed.month, parsed.day) == (today.month, today.day):
            hits.append(f"今天是你和主人{name}满 {max(1, today.year - start.year)} 周年")
        elif n in MILESTONES:
            hits.append(f"今天是你和主人{name}第 {n} 天")
    if not hits:
        return ""
    return "【今天】" + "；".join(hits[:2]) + "。可以自然提一句，别刻意煽情。"


def upcoming(days: int = 7, ref: date | None = None) -> list[str]:
    """未来 N 天内的日子（给主动说话用，避免当天才想起）。"""
    today = ref or date.today()
    out: list[str] = []
    for it in list_all():
        parsed = _parse(str(it.get("date") or ""))
        if not parsed:
            continue
        name = str(it.get("name") or "")
        try:
            nxt = parsed.replace(year=today.year)
        except ValueError:  # 2-29
            continue
        if nxt < today:
            nxt = nxt.replace(year=today.year + 1)
        delta = (nxt - today).days
        if 0 < delta <= days:
            out.append(f"{delta} 天后是{name}")
    return out
