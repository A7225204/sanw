"""健康灯：定时跑检查，把结果写成一个状态文件，面板右上角亮灯。

**分工**（2026-11-07 定）：

    每 3 小时    deep_check（说话走样 / 记忆召回 / token / 人格）  2.5 秒，0 元
    每天 04:30   verify 的非前端部分（语法 / 链路 / 消气 / 检索 / 自检）  4 秒，0 元
    改完代码     手动跑 tools/verify.py（含前端，33 秒）

**不跑前端**：前端要开浏览器（33 秒），定时任务里太重。
前端那步留给「改完代码手动跑」——那才是它真正能抓到东西的时候。

**灯的颜色**：
    🟢 ok      全过
    🟡 warn    有警告（不影响跑，但该看一眼）
    🔴 fail    有失败（真坏了）
    ⚪ never   还没跑过

状态文件：memory/health.json
    {status, at, elapsed, checks: {名: {status, msg}}, issues: [...]}

对外：
    run(full=False)   跑一次，写文件
    read()            读状态（不跑）
    line()            一句话
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILE = ROOT / "memory" / "health.json"

# tools/ 下的脚本要能被 import
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load() -> dict:
    try:
        return json.loads(FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"status": "never", "at": 0, "checks": {}, "issues": []}


def read() -> dict:
    """读上次的结果（不跑检查，给面板/API 用）。"""
    d = _load()
    age_h = (time.time() - float(d.get("at") or 0)) / 3600 if d.get("at") else -1
    d["age_hours"] = round(age_h, 1)
    # 超过 8 小时没跑，灯变灰（说明定时任务可能挂了）
    if age_h > 8 and d.get("status") not in ("never", None):
        d["stale"] = True
    return d


def _run_deep() -> dict:
    """跑 tools/deep_check.py 的四个检查函数（不调 LLM）。"""
    import importlib
    out: dict = {}
    try:
        dc = importlib.import_module("tools.deep_check")
        out["回复走样"] = dc.check_replies(12)
        out["记忆召回"] = dc.check_recall(10)
        out["token"] = dc.check_token(24)
        out["人格固化"] = dc.check_growth()
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def _run_verify() -> dict:
    """跑 tools/verify.py 的非前端检查（导入函数，不走 subprocess）。"""
    import importlib
    out: dict = {}
    try:
        vf = importlib.import_module("tools.verify")
        for name in ("语法", "链路", "检索", "自检"):
            fn = vf.ALL_CHECKS.get(name)
            if not fn:
                continue
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                vf.rec(name, "FAIL", f"{type(e).__name__}: {e}")
        out["results"] = list(vf.RESULTS)
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def _judge(deep: dict, ver: dict) -> tuple[str, list[str], dict]:
    """把两组结果压成「灯色 + 问题清单 + 每项摘要」。"""
    issues: list[str] = []
    checks: dict[str, dict] = {}

    # ── deep_check 的四项 ──
    r = deep.get("回复走样") or {}
    if r.get("n"):
        checks["回复走样"] = {
            "status": "warn" if r.get("ai_tone") else "ok",
            "msg": f"{r['n']} 条 | 均 {r.get('avg_len')} 字 | 不确定 {r.get('real_uncertain_pct')}%",
        }
        if r.get("ai_tone"):
            issues.append("AI 腔：" + "，".join(f"{k}{v}" for k, v in r["ai_tone"].items()))
    rec = deep.get("记忆召回") or {}
    if rec.get("error"):
        checks["记忆召回"] = {"status": "fail", "msg": rec["error"]}
        issues.append("记忆召回跑不起来：" + rec["error"])
    elif rec.get("rate") is not None:
        bad = rec["rate"] < 70
        checks["记忆召回"] = {
            "status": "fail" if bad else "ok",
            "msg": f"自召回 {rec['self_recall']}/{rec['sample']} = {rec['rate']}%",
        }
        if bad:
            issues.append(f"记忆自召回只有 {rec['rate']}%")
    tk = deep.get("token") or {}
    if tk.get("cost_yuan") is not None:
        _u = tk.get("utility") or {}
        _msg = f"24h 约 {tk['cost_yuan']} 元"
        # 2026-11-07：把 utility 的次数/命中率写进这一行的 msg 里 ——
        # 它是拉低整体命中率的主因（441 次、命中 0.3%，把 47.3% 从 75.8% 拖下来），
        # 不给出来，主人看面板只会觉得「命中率怎么这么低」却不知道是谁干的。
        if _u.get("calls"):
            _msg += f" · utility {_u['calls']}次 命中{_u.get('hit_pct', 0)}%"
        checks["token"] = {"status": "ok", "msg": _msg}
        if tk.get("heavy"):
            checks["token"]["status"] = "warn"
            issues.append("单次输入超 5000 的通道：" + "，".join(tk["heavy"]))
        # utility 暴涨：次数或单次大小明显超标
        if _u.get("spike"):
            checks["token"]["status"] = "warn"
            _sp = _u["spike"]
            if _u.get("by_scope"):
                _sp += "（" + "，".join(_u["by_scope"]) + "）"
            issues.append("utility 异常：" + _sp)
    g = deep.get("人格固化") or {}
    if g.get("total"):
        backlog = g.get("pending_backlog", 0)
        checks["人格固化"] = {
            "status": "warn" if backlog > 25 else "ok",
            "msg": f"{g['total']} 条 | pending {backlog}",
        }
        if backlog > 25:
            issues.append(f"人格 pending 积压 {backlog} 条")

    # ── verify 的四项 ──
    for x in (ver.get("results") or []):
        st = {"PASS": "ok", "WARN": "warn", "FAIL": "fail"}.get(x.get("status"), "warn")
        checks[x["name"]] = {"status": st, "msg": x.get("msg", "")}
        if x.get("status") in ("FAIL", "WARN"):
            issues.append(f"{x['name']}：{x.get('msg', '')}")
    if ver.get("error"):
        issues.append("verify 跑不起来：" + ver["error"])

    if any(v["status"] == "fail" for v in checks.values()):
        status = "fail"
    elif any(v["status"] == "warn" for v in checks.values()):
        status = "warn"
    elif checks:
        status = "ok"
    else:
        status = "never"
    return status, issues[:8], checks


def run(full: bool = False) -> dict:
    """跑一次，写状态文件。

    full=True 时连 verify 的机制检查一起跑；False 只跑 deep_check。
    """
    t0 = time.perf_counter()
    deep = _run_deep()
    ver = _run_verify() if full else {}
    el = round(time.perf_counter() - t0, 2)
    status, issues, checks = _judge(deep, ver)
    d = {
        "status": status,
        "at": int(time.time()),
        "elapsed": el,
        "checks": checks,
        "issues": issues,
        "full": bool(full),
    }
    try:
        FILE.parent.mkdir(parents=True, exist_ok=True)
        FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return d


def line() -> str:
    d = read()
    icon = {"ok": "🟢", "warn": "🟡", "fail": "🔴", "never": "⚪"}.get(d.get("status"), "⚪")
    ago = f"{d['age_hours']}h 前" if d.get("age_hours", -1) >= 0 else "还没跑过"
    head = f"{icon} {d.get('status', 'never')} · {ago} · {d.get('elapsed', 0)}s"
    if d.get("issues"):
        head += " | " + "；".join(d["issues"][:2])
    return head


def _run_full_with_frontend() -> dict:
    """完整版：deep_check + verify（含前端）。前端那步要开浏览器，约 33 秒。"""
    import importlib
    t0 = time.perf_counter()
    deep = _run_deep()
    ver: dict = {}
    try:
        vf = importlib.import_module("tools.verify")
        for name, fn in vf.ALL_CHECKS.items():
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                vf.rec(name, "FAIL", f"{type(e).__name__}: {e}")
        ver = {"results": list(vf.RESULTS)}
    except Exception as e:  # noqa: BLE001
        ver = {"error": f"{type(e).__name__}: {e}"}
    el = round(time.perf_counter() - t0, 2)
    status, issues, checks = _judge(deep, ver)
    d = {"status": status, "at": int(time.time()), "elapsed": el,
         "checks": checks, "issues": issues, "full": True, "with_frontend": True}
    try:
        FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return d


def register_schedule() -> None:
    """每 3 小时 deep_check；每天 04:30 加 verify。"""
    try:
        from nonebot import logger
        from nonebot_plugin_apscheduler import scheduler
    except Exception:  # noqa: BLE001
        return

    @scheduler.scheduled_job("cron", hour="*/3", minute="7", id="health_light_3h",
                             misfire_grace_time=900, coalesce=True)
    async def _every3() -> None:
        import asyncio
        try:
            d = await asyncio.to_thread(run, True)   # 2026-11-07：每 3 小时也带上 verify
            if d["status"] in ("warn", "fail"):
                logger.warning(f"[健康灯] {line()}")
            else:
                logger.info(f"[健康灯] {line()}")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[健康灯] 3h 检查失败：{type(e).__name__}: {e}")

    @scheduler.scheduled_job("cron", hour="0", minute="30", id="health_light_daily",
                             misfire_grace_time=1800, coalesce=True)
    async def _daily() -> None:
        """每天 00:30：跑一次**含前端**的完整检查（开浏览器，33 秒）。

        2026-11-07 从 04:30 挪到 00:30 —— 那时候通常还没睡，出问题能立刻看到。
        """
        import asyncio
        try:
            d = await asyncio.to_thread(_run_full_with_frontend)
            logger.info(f"[健康灯] 每日全查（含前端）：{line()}")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[健康灯] 每日检查失败：{type(e).__name__}: {e}")

    # 启动 5 分钟后先跑一次（不阻塞启动）
    try:
        import datetime as _dt
        boot_at = _dt.datetime.now() + _dt.timedelta(minutes=5)
        scheduler.add_job(_boot_once, "date", run_date=boot_at, id="health_light_boot_real",
                          misfire_grace_time=300)
    except Exception:  # noqa: BLE001
        pass


async def _boot_once() -> None:
    import asyncio
    try:
        from nonebot import logger
        d = await asyncio.to_thread(run, True)
        logger.info(f"[健康灯] 启动首查：{line()}")
    except Exception:  # noqa: BLE001
        pass


try:
    register_schedule()
except Exception:  # noqa: BLE001
    pass
