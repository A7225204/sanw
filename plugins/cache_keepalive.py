# -*- coding: utf-8 -*-
"""缓存保活：定时用「和聊天同一段稳定前缀」发一次极小请求，把服务端前缀缓存的 TTL 续上。

为什么这么做：实测她的调用 warm 时命中 95.7%，但静默 1~1.5 小时后的第一条只有 17%
——那 13k 未命中就是缓存过期。保活把这段重新捂热，代价按命中价算，几毛钱一天。

同时它也是一次实验：每次记录 cached/prompt，就能看出 TTL 到底多长
（cached=0 说明过期，cached≈prompt 说明还热）。

    /保活            看今天保活的统计（群里要 @）
开关（环境变量）：
    CACHE_KEEPALIVE_ENABLED=0   全关
    CACHE_KEEPALIVE_MINUTES=30  间隔分钟
    CACHE_KEEPALIVE_DRY=1       只构造不发送（看前缀多大）
日志：memory/cache_keepalive.jsonl，一行一次
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

from nonebot import on_command
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent

try:
    from nonebot.log import logger
except Exception:  # noqa: BLE001
    logger = logging.getLogger("cache_keepalive")

ROOT = Path(__file__).resolve().parent.parent
LOG_FILE = ROOT / "memory" / "cache_keepalive.jsonl"
OWNER_QQ = os.getenv("OWNER_QQ_ID") or "100000000"


def enabled() -> bool:
    return os.getenv("CACHE_KEEPALIVE_ENABLED", "1").lower() not in ("0", "false", "no")


def interval_min() -> int:
    try:
        return max(5, min(180, int(os.getenv("CACHE_KEEPALIVE_MINUTES", "30"))))
    except Exception:  # noqa: BLE001
        return 30


def _dry() -> bool:
    return os.getenv("CACHE_KEEPALIVE_DRY", "0").lower() in ("1", "true", "yes")


def _log(rec: dict) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + chr(10))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[保活] 写日志失败：{type(e).__name__}: {e}")


def _stats_today() -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    n = hit = 0
    p_sum = c_sum = 0
    try:
        if LOG_FILE.exists():
            for line in LOG_FILE.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if not str(r.get("ts") or "").startswith(today):
                    continue
                n += 1
                p = int(r.get("prompt") or 0)
                c = int(r.get("cached") or 0)
                p_sum += p
                c_sum += c
                if p and c:
                    hit += 1
    except Exception:  # noqa: BLE001
        pass
    return {"runs": n, "alive": hit, "prompt": p_sum, "cached": c_sum}


async def keepalive_once() -> dict:
    """发一次保活。返回本次记录（也写日志）。"""
    rec: dict = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    try:
        from plugins import ai_group as ag

        stable, _vol, _slow = ag.build_prompt_parts(
            0, OWNER_QQ, is_private=True, user_text="", channel="qq",
        )
        rec["prefix_chars"] = len(stable or "")
        if _dry():
            rec["dry"] = True
            logger.info(f"[保活] dry-run：前缀 {len(stable or '')} 字符，未发送")
            _log(rec)
            return rec
        msgs = [
            {"role": "system", "content": stable},
            {"role": "user", "content": "。"},
        ]
        t0 = time.time()
        text, usage = await asyncio.wait_for(
            ag._chat(msgs, temperature=0.0, max_tokens=1,
                     record_scope="keepalive", channel="keepalive"),
            timeout=45,
        )
        usage = usage or {}
        pin = int(usage.get("prompt_tokens") or 0)
        det = usage.get("prompt_tokens_details") or {}
        cached = int(det.get("cached_tokens") or usage.get("prompt_cache_hit_tokens") or 0)
        rec.update({"prompt": pin, "cached": cached,
                    "ratio": round(cached / pin * 100, 1) if pin else 0.0,
                    "alive": bool(cached), "ms": int((time.time() - t0) * 1000)})
        logger.info(f"[保活] prompt {pin} / cached {cached}"
                    f"（{(cached/pin*100 if pin else 0):.0f}%）{rec['ms']}ms")
    except Exception as e:  # noqa: BLE001
        rec["error"] = f"{type(e).__name__}: {e}"[:150]
        logger.warning(f"[保活] 失败：{rec['error']}")
    _log(rec)
    return rec


def register_schedule() -> None:
    if not enabled():
        logger.info("[保活] CACHE_KEEPALIVE_ENABLED=0，未注册")
        return
    try:
        from nonebot_plugin_apscheduler import scheduler

        scheduler.add_job(
            keepalive_once, "interval", minutes=interval_min(),
            id="cache_keepalive", replace_existing=True,
            next_run_time=datetime.now() + timedelta(seconds=90),
            misfire_grace_time=600, coalesce=True, max_instances=1,
        )
        logger.info(f"[保活] 已注册：每 {interval_min()} 分钟一次（90 秒后先跑一次）")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[保活] 定时器注册失败：{type(e).__name__}: {e}")


cmd = on_command("保活", priority=5, block=False)


@cmd.handle()
async def _(event: MessageEvent):
    if isinstance(event, GroupMessageEvent) and not event.is_tome():
        return
    s = _stats_today()
    rate = (s["cached"] / s["prompt"] * 100) if s["prompt"] else 0
    await cmd.finish(
        f"缓存保活（每 {interval_min()} 分钟）：今天跑了 {s['runs']} 次，"
        f"其中 {s['alive']} 次是热的；prompt {s['prompt']} / cached {s['cached']}（{rate:.0f}%）"
    )


try:
    register_schedule()
except Exception as _e:  # noqa: BLE001
    logger.warning(f"[保活] 启动注册失败：{type(_e).__name__}: {_e}")
