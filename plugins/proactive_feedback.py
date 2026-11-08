"""主动发言的反馈闭环：她说出去之后，到底有没有人接。

2026-11-06 加（借「能动性四件套」的第 ④ 条）：
以前的主动发言是纯概率 —— `if random.random() < 0.2` 就发一句。
说了 10 句没人理，第 11 句还是同样的概率。那是「随机发言」，不是「能动性」。

这个模块做三件事：
  1. log_sent()    她说出去时记一行（谁说的、说了什么、什么时候）
  2. note_reply()  有人在她之后开口时，判断算不算「接住了」
  3. adjust()      下次决策时读回来：被冷落 → 拉长冷却、压低概率；被接住 → 反过来

反应判定（简单但够用）：
  · 主人/群友在她之后 5 分钟内开口 → answered
  · 话里出现她那条的关键词 → warm（真的接了这个话头）
  · 超过 30 分钟没人开口 → ignored
  · 被骂/被凶 → cold

存 memory/proactive_log.jsonl，一行一条，只留最近 200 条。
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

from nonebot import logger

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "memory" / "proactive_log.jsonl"
MAX_LINES = 200

ANSWER_WINDOW = 300.0      # 5 分钟内有人开口 = 接住了
IGNORE_WINDOW = 1800.0     # 30 分钟没人开口 = 被无视

# 被凶的信号（他心情不好，别再凑上去）
_NEGATIVE = ("烦", "吵", "闭嘴", "别说了", "别叫", "滚", "安静", "闭嘴吧", "别闹",
             "烦人", "不喜欢", "恶心", "别烦")
# 被接住的信号
_POSITIVE = ("哈哈", "笑死", "嗯嗯", "然后呢", "真的", "继续", "不错", "可爱", "喜欢",
             "有意思", "再说说", "啥意思", "怎么")


def _bigrams(text: str) -> set[str]:
    t = "".join(ch for ch in str(text or "") if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")
    return {t[i:i + 2] for i in range(len(t) - 1)} if len(t) >= 2 else set()


def _load() -> list[dict]:
    if not LOG.exists():
        return []
    out = []
    try:
        for ln in LOG.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        return []
    return out[-MAX_LINES:]


def _save(rows: list[dict]) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        LOG.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows[-MAX_LINES:]) + "\n",
            encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[主动反馈] 写日志失败：{type(e).__name__}: {e}")


def log_sent(gid: str, text: str, kind: str = "proactive") -> None:
    """她说了一句主动的话，记下来等结果。"""
    if not str(text or "").strip():
        return
    rows = _load()
    rows.append({
        "at": time.time(),
        "gid": str(gid),
        "text": str(text)[:120],
        "kind": kind,
        "reaction": None,       # None=还没结果
        "delay": None,
        "by": None,
    })
    _save(rows)


def note_reply(gid: str, user_text: str, by: str = "owner") -> dict | None:
    """有人开口了 —— 看他接没接住她最近那条主动发言。返回更新后的那条记录。"""
    now = time.time()
    rows = _load()
    hit = None
    for r in reversed(rows):
        if str(r.get("gid")) != str(gid):
            continue
        if r.get("reaction") is not None:
            continue
        dt = now - float(r.get("at") or 0)
        if dt > IGNORE_WINDOW:
            break
        hit = r
        break
    if not hit:
        return None
    if hit.get("reaction") is not None:
        return None

    dt = now - float(hit.get("at") or 0)
    ut = str(user_text or "")
    hit["delay"] = round(dt, 1)
    hit["by"] = by

    if any(w in ut for w in _NEGATIVE):
        hit["reaction"] = "cold"
    elif dt <= ANSWER_WINDOW:
        # 时间上算接住了，再看是不是真的接了这个话头
        shared = _bigrams(ut) & _bigrams(hit.get("text"))
        if shared or any(w in ut for w in _POSITIVE):
            hit["reaction"] = "warm"
        else:
            hit["reaction"] = "answered"
    else:
        hit["reaction"] = "answered" if dt <= IGNORE_WINDOW else "ignored"
    _save(rows)
    logger.info(f"[主动反馈] {hit.get('reaction')}（{hit.get('delay')}s，{by}）")
    return hit


def settle() -> None:
    """把超时没人理的标记成 ignored。每小时跑一次就够。"""
    now = time.time()
    rows = _load()
    changed = 0
    for r in rows:
        if r.get("reaction") is not None:
            continue
        if now - float(r.get("at") or 0) > IGNORE_WINDOW:
            r["reaction"] = "ignored"
            changed += 1
    if changed:
        _save(rows)
        logger.info(f"[主动反馈] {changed} 条判定为被无视")


def adjust(gid: str) -> dict:
    """读最近 20 条主动发言的结果，给下次决策三个倍数。

    返回 {cooldown_mul, chance_mul, tone, hits}
      cooldown_mul  冷却时间倍率（>1 = 拉长，别再叨叨）
      chance_mul    主动开口概率倍率
      tone          给 prompt 的一句话提示（空=不加）
    """
    rows = [r for r in _load()
            if str(r.get("gid")) == str(gid) and r.get("reaction") is not None][-20:]
    if not rows:
        return {"cooldown_mul": 1.0, "chance_mul": 1.0, "tone": "", "hits": 0}

    warm = sum(1 for r in rows if r.get("reaction") == "warm")
    answered = sum(1 for r in rows if r.get("reaction") == "answered")
    ignored = sum(1 for r in rows if r.get("reaction") == "ignored")
    cold = sum(1 for r in rows if r.get("reaction") == "cold")
    n = len(rows)

    good = warm + answered
    hit_rate = good / n if n else 0.0

    # 被无视得多 → 拉长冷却、压低概率
    if hit_rate < 0.3:
        mul = 2.5
        chance = 0.5
        tone = "最近你主动开口基本没人接，这次别又自说自话，短一点、试探一点。"
    elif hit_rate < 0.5:
        mul = 1.6
        chance = 0.75
        tone = "你最近主动说话有一半没人应，悠着点。"
    elif hit_rate < 0.75:
        mul = 1.0
        chance = 1.0
        tone = ""
    else:
        mul = 0.7
        chance = 1.3
        tone = "你最近主动开口都有人接，可以更自在一点。"

    if cold >= 2:
        mul = max(mul, 3.0)
        chance = min(chance, 0.4)
        tone = "上次开口主人嫌吵了，这次务必短，能不开口就不开。"

    return {"cooldown_mul": mul, "chance_mul": chance, "tone": tone,
            "hits": n, "warm": warm, "answered": answered,
            "ignored": ignored, "cold": cold, "hit_rate": round(hit_rate, 2)}


def prompt_line(gid: str) -> str:
    """给 prompt 用的一句话（没历史就返回空）。"""
    a = adjust(gid)
    if not a.get("tone"):
        return ""
    return "【主动发言反馈】" + a["tone"]


def stats() -> dict:
    rows = _load()
    out: dict = {"total": len(rows)}
    for r in rows:
        k = str(r.get("reaction") or "pending")
        out[k] = out.get(k, 0) + 1
    return out


def register_schedule() -> None:
    """每小时把超时没回音的判成被无视。"""
    try:
        import nonebot  # noqa: F401
        from nonebot_plugin_apscheduler import scheduler
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[主动反馈] 定时器不可用：{e}")
        return

    @scheduler.scheduled_job("cron", minute=17, id="proactive_settle",
                             misfire_grace_time=600, coalesce=True)
    async def _job() -> None:
        settle()

    logger.info("[主动反馈] 已注册：每小时 17 分把超时没回音的判成被无视")


try:
    register_schedule()
except Exception:  # noqa: BLE001
    pass
