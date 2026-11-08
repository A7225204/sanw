# -*- coding: utf-8 -*-
"""Pixiv 挖图：大肥鱼自己去 Pixiv 收图，收完同步进表情库 / 生图参考库。

    /下图片        立刻跑一轮（挖图 → 同步）
    /挖图状态      看进度

定时：每天 12:00 自动挖（schedule.py 调 run_daily）。

链路：大肥鱼(8080) → DSH 插件 pixiv-duty(3080) → 下载到 library
      → tools/sync_pixiv_to_fish.py 识图分级 → 表情包 / 生图参考库 / R-18
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
from nonebot import on_command
from nonebot.adapters.onebot.v11 import MessageEvent

log = logging.getLogger("pixiv_duty")

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
SYNC = str(ROOT / "tools" / "sync_pixiv_to_fish.py")
API = "http://127.0.0.1:3080/pixiv-duty/api"
STATE = ROOT / "memory" / "pixiv_duty_state.json"

_running = {"busy": False}


def _load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_state(d: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


async def _wait_round(timeout: float = 600) -> str:
    """等 pixiv-duty 当前那一轮跑完，返回状态文字。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        await asyncio.sleep(10)
        try:
            r = await asyncio.to_thread(httpx.get, API + "/status", timeout=15)
            d = r.json()
        except Exception:  # noqa: BLE001
            continue
        rnd = d.get("round") or {}
        if not rnd.get("startedAt"):
            return "done"   # round 已结束（startedAt 清空）
    return "timeout"


def _sync() -> dict:
    """跑同步脚本，返回末尾统计。"""
    try:
        p = subprocess.run([PY, SYNC, "--limit", "40"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=900,
                           cwd=str(ROOT))
        out = (p.stdout or "") + (p.stderr or "")
        return {"ok": p.returncode == 0, "tail": out.strip().splitlines()[-3:]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "tail": [f"{type(e).__name__}: {e}"]}


async def run_round() -> dict:
    """跑一轮：触发挖图 → 等完成 → 同步。"""
    if _running["busy"]:
        return {"ok": False, "msg": "上一轮还在跑"}
    _running["busy"] = True
    try:
        try:
            r = await asyncio.to_thread(httpx.post, API + "/run", json={}, timeout=30)
            if r.status_code != 200:
                return {"ok": False, "msg": f"挖图启动失败：{r.status_code}"}
            round_id = (r.json() or {}).get("roundId") or ""
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "msg": f"连不上 DSH：{type(e).__name__}"}
        st = await _wait_round()
        syn = await asyncio.to_thread(_sync)
        _save_state({"last": time.strftime("%Y-%m-%d %H:%M"), "round": round_id,
                     "status": st, "sync": syn})
        return {"ok": True, "round": round_id, "status": st, "sync": syn}
    finally:
        _running["busy"] = False


down_cmd = on_command("下图片", aliases={"挖图片"}, priority=5, block=True)
status_cmd = on_command("挖图状态", aliases={"下图状态", "挖图情况"}, priority=5, block=False)


@down_cmd.handle()
async def _(event: MessageEvent):
    if _running["busy"]:
        await down_cmd.finish("上一轮还在挖，等会儿。")
    asyncio.create_task(run_round())
    await down_cmd.finish("去 Pixiv 挖了，挖完同步进表情库，好了跟你说。")


@status_cmd.handle()
async def _(event: MessageEvent):
    """状态直接问 pixiv-duty（本地 state 只在跑完同步后写，会显得像"没挖过"）。"""
    try:
        r = await asyncio.to_thread(httpx.get, API + "/status", timeout=20)
        d = r.json() or {}
    except Exception:  # noqa: BLE001
        d = {}
    if not d.get("ok"):
        s = _load_state()
        if not s:
            await status_cmd.finish("挖图服务连不上（3080 的 pixiv-duty 没起？）")
            return
        syn = s.get("sync") or {}
        parts = [
            "上次挖图：" + str(s.get("last") or "?"),
            "状态：" + str(s.get("status") or "?"),
            "同步：" + ("成功" if syn.get("ok") else "失败"),
            " | ".join(syn.get("tail") or []),
        ]
        await status_cmd.finish(chr(10).join(parts))
        return
    today = d.get("today") or {}
    rnd = d.get("round") or {}
    last = d.get("last") or {}
    parts = [f"今天：已存 {today.get('saved', 0)} / 配额 {today.get('quota', '?')} 张"]
    if rnd.get("startedAt"):
        parts.append(f"正在跑：{rnd.get('id', '?')} · 阶段 {rnd.get('phase', '?')}")
    else:
        parts.append("当前没有在跑的轮次")
    if last:
        parts.append(f"上一轮：{last.get('saved', 0)} 张（{str(last.get('at') or '')[:16]}）")
        prob = last.get("problems") or []
        if prob:
            parts.append("问题：" + "；".join(str(x) for x in prob[:3]))
    await status_cmd.finish(chr(10).join(parts))


async def run_daily() -> None:
    """定时任务入口（schedule.py 调）。"""
    try:
        res = await run_round()
        log.info(f"[挖图] 定时完成：{res}")
    except Exception as e:  # noqa: BLE001
        log.warning(f"[挖图] 定时失败：{type(e).__name__}: {e}")
