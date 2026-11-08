# -*- coding: utf-8 -*-
"""她的一天（回看版）：把今天实际发生过的事总结成时间线日历，不是给她排计划。

    /日历            今天的日历（她做过什么）
    /日历 昨天        昨天
    /日历 生成        立刻重算今天（每天 23:50 自动跑一次）

证据只取 chat_history 里今天的原话（她说的/对方说的），模型只负责归纳，不许编；
模型失败时按两小时桶取她最近一句兜底，保证一定有一份。
存储：memory/day_calendar.json
开关：DAYLOG_ENABLED=0
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

from nonebot import on_command
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent

try:
    from nonebot.log import logger
except Exception:  # noqa: BLE001
    logger = logging.getLogger("daylog")

from memory_manager import DB_PATH

ROOT = Path(__file__).resolve().parent.parent
CAL_FILE = ROOT / "memory" / "day_calendar.json"
KEEP_DAYS = 31
MAX_ROWS = 400


def enabled() -> bool:
    return os.getenv("DAYLOG_ENABLED", "1").lower() not in ("0", "false", "no")


# ---------------------------------------------------------------- 存储
def _load() -> dict:
    try:
        if CAL_FILE.exists():
            d = json.loads(CAL_FILE.read_text(encoding="utf-8-sig"))
            if isinstance(d, dict) and isinstance(d.get("days"), dict):
                return d
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[日历] 读失败：{type(e).__name__}: {e}")
    return {"days": {}}


def _save(d: dict) -> None:
    tmp = CAL_FILE.with_suffix(".json.tmp")
    try:
        CAL_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, CAL_FILE)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[日历] 写失败：{type(e).__name__}: {e}")


def _prune(d: dict) -> None:
    days = d.get("days") or {}
    for k in sorted(days)[:-KEEP_DAYS]:
        days.pop(k, None)


# ---------------------------------------------------------------- 证据
def evidence(day: str) -> list[tuple[str, str, str]]:
    """返回 [(HH:MM, 谁, 内容)]，只取那一天的。"""
    try:
        con = sqlite3.connect(str(DB_PATH), timeout=5)
        try:
            rows = con.execute(
                "SELECT speaker, message, reply, strftime('%H:%M', timestamp) FROM chat_history "
                "WHERE date(timestamp) = date(?) ORDER BY id ASC LIMIT ?",
                (day, MAX_ROWS),
            ).fetchall()
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[日历] 读 chat_history 失败：{type(e).__name__}: {e}")
        return []
    out: list[tuple[str, str, str]] = []
    for speaker, message, reply, hhmm in rows:
        hhmm = str(hhmm or "")[:5]
        if message:
            out.append((hhmm, "对方", str(message)[:80]))
        if reply:
            out.append((hhmm, "她", str(reply)[:120]))
    try:
        out.extend(extra_evidence(day))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[日历] 汇总外部证据失败：{type(e).__name__}: {e}")
    out.sort(key=lambda x: x[0])
    return out


def _read_json(path: Path) -> dict:
    try:
        if path.exists():
            d = json.loads(path.read_text(encoding="utf-8-sig"))
            return d if isinstance(d, dict) else {}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[日历] 读 {path.name} 失败：{type(e).__name__}: {e}")
    return {}


def _hhmm(ts: str) -> str:
    m = re.search(r"(\d{2}):(\d{2})", str(ts or ""))
    return f"{m.group(1)}:{m.group(2)}" if m else ""


def extra_evidence(day: str) -> list[tuple[str, str, str]]:
    """她真干过的事：B站看了什么、发了什么说说、写了什么日记。"""
    out: list[tuple[str, str, str]] = []

    bili = _read_json(ROOT / "memory" / "bili_daily.json")
    if str(bili.get("date") or "") == day:
        for it in (bili.get("history") or [])[-12:]:
            if not isinstance(it, dict):
                continue
            at = str(it.get("at") or "")
            if not at.startswith(day):
                continue
            t = _hhmm(at)
            title = str(it.get("title") or "").strip()
            if t and title:
                out.append((t, "看了B站", title[:60]))

    mood = _read_json(ROOT / "memory" / "daily_mood.json")
    for it in (mood.get("history") or [])[-10:]:
        if not isinstance(it, dict):
            continue
        ts = str(it.get("ts") or "")
        if not ts.startswith(day):
            continue
        t = _hhmm(ts)
        text = re.sub(r"[*（）()]", "", str(it.get("text") or "")).strip()
        if t and text:
            out.append((t, "发了说说", text[:60]))

    diary = ROOT / "memory" / "diary" / f"{day}.json"
    d = _read_json(diary)
    body = str(d.get("body") or "").strip()
    if body:
        created = str(d.get("created_at") or "")
        out.append((_hhmm(created) or "23:30", "写了日记", re.sub(r"\s+", " ", body)[:80]))

    return out


def _fallback(rows: list[tuple[str, str, str]]) -> list[dict]:
    """按两小时桶取她最近一句，作为粗糙但真实的日历。"""
    # 优先用她真干过的事（B站/说说/日记），没有才退回聊天原话
    real = [(h, w, t) for h, w, t in rows if w in ("看了B站", "发了说说", "写了日记")]
    if real:
        return [{"time": h, "what": (w + "：" + re.sub(r"\s+", "", t))[:20]} for h, w, t in real[:8]]
    buckets: dict[int, tuple[str, str]] = {}
    for hhmm, who, text in rows:
        if who != "她":
            continue
        try:
            h = int(hhmm[:2])
        except Exception:  # noqa: BLE001
            continue
        buckets[h // 2 * 2] = (hhmm, text)
    items = []
    for start in sorted(buckets):
        hhmm, text = buckets[start]
        t = re.sub(r"[*（）()\[\]{}]", "", text).strip()
        items.append({"time": hhmm, "what": (t[:18] or "聊天")})
    return items


# ---------------------------------------------------------------- 生成
def _prompt(day: str, rows: list[tuple[str, str, str]]) -> str:
    lines = "\n".join(f"{hhmm} {who}：{text}" for hhmm, who, text in rows[-MAX_ROWS:])
    return (
        f"下面是 {day} 这名角色（蓝色鲸鱼娘，昵称大肥鱼）和主人/群友当天的聊天原文。\n"
        "只根据原文，归纳她这一天实际做了什么（吃、睡、刷B站、写东西、出门、聊天…）。\n"
        "规矩：不许编原文里没有的事；时间只能在原文出现过的时刻里挑；不要写成计划或待办。\n"
        '只输出 JSON，不要代码块：{"summary":"一句话概括她今天","items":[{"time":"07:30","what":"起床做早饭"}]}\n'
        "items 4~8 条，what 不超过 14 字。\n\n聊天原文：\n" + lines
    )


def _parse(text: str, rows: list[tuple[str, str, str]]) -> dict | None:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    items = []
    for it in data.get("items") or []:
        if not isinstance(it, dict):
            continue
        hhmm = str(it.get("time") or "")
        what = str(it.get("what") or "").strip()[:20]
        if re.match(r"^\d{1,2}:\d{2}$", hhmm) and what:
            hhmm = ":".join(f"{int(x):02d}" for x in hhmm.split(":"))
            items.append({"time": hhmm, "what": what})
    if len(items) < 3:
        return None
    items.sort(key=lambda x: x["time"])
    return {"summary": str(data.get("summary") or "").strip()[:80], "items": items[:10]}


async def build_day(day: str) -> bool:
    if not enabled():
        return False
    rows = evidence(day)
    if not rows:
        logger.info(f"[日历] {day} 没有聊天记录，跳过")
        return False

    data = None
    try:
        from plugins import ai_group as ag  # 惰性导入

        msgs = [
            {"role": "system", "content": ag._persona_snippet()},
            {"role": "user", "content": _prompt(day, rows)},
        ]
        text, _ = await asyncio.wait_for(
            ag._chat(msgs, temperature=0.7, max_tokens=600, record_scope="daylog"), timeout=60
        )
        data = _parse(text, rows)
        if data:
            data["source"] = "llm"
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[日历] 模型归纳失败：{type(e).__name__}: {e}")

    if not data:
        items = _fallback(rows)
        data = {"summary": "今天和他/他们聊了这些。", "items": items, "source": "fallback"}

    d = _load()
    d.setdefault("days", {})[day] = {
        "generatedAt": int(time.time()),
        "summary": data.get("summary") or "",
        "items": data.get("items") or [],
        "source": data.get("source") or "fallback",
        "turns": len(rows),
    }
    _prune(d)
    _save(d)
    logger.info(f"[日历] 写入 {day}：{len(data.get('items') or [])} 条（{data.get('source')}）")
    return True


async def build_today() -> None:
    await build_day(datetime.now().strftime("%Y-%m-%d"))


def register_schedule() -> None:
    if not enabled():
        return
    try:
        from nonebot_plugin_apscheduler import scheduler

        scheduler.add_job(
            build_today, "cron", hour=23, minute=50,
            id="daylog_today", replace_existing=True, misfire_grace_time=3600,
        )
        logger.info("[日历] 已注册 23:50 回看今天")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[日历] 定时器注册失败：{type(e).__name__}: {e}")


# ---------------------------------------------------------------- 指令
cal_cmd = on_command("日历", priority=5, block=False)


def day_of(offset: int) -> str:
    return (datetime.now() + timedelta(days=offset)).strftime("%Y-%m-%d")


@cal_cmd.handle()
async def _(event: MessageEvent):
    if isinstance(event, GroupMessageEvent) and not event.is_tome():
        return
    raw = event.get_plaintext().strip().lstrip("/／")
    if raw.startswith("日历"):
        raw = raw[2:]
    if "生成" in raw:
        ok = await build_today()
        await cal_cmd.finish("今天的日历重算好了，发 /日历 看。" if ok else "今天还没什么可记的。")
    off = -1 if "昨天" in raw else 0
    day = day_of(off)
    d = (_load().get("days") or {}).get(day)
    if not d:
        await cal_cmd.finish(f"{day} 还没有日历。（每天 23:50 自动记一次，也可以发 /日历 生成）")
    lines = [f"{day}（{d.get('source')}）｜{d.get('summary') or '—'}"]
    for it in d.get("items") or []:
        lines.append(f"· {it.get('time')} {it.get('what')}")
    await cal_cmd.finish("\n".join(lines[:12]))


try:
    register_schedule()
except Exception as _e:  # noqa: BLE001
    logger.warning(f"[日历] 启动注册失败：{type(_e).__name__}: {_e}")
