"""记忆体检：每天给「记忆固化」做一次自检，把结果写进 memory/memory_health.json。

查四件事
--------
1. 向量覆盖率    档案里多少条已经有向量（决定语义召回能不能召回全）
2. 自召回@3     拿档案原文当问题去检索，看自己能不能排进前 3（低于 85% 说明索引/模型有问题）
3. 档案规模      各 scope 条数、平均权重、是否有超长
4. 画像覆盖率    有多少 scope 已经有「对ta的了解」画像

顺手做全量建索引（启动时第一次跑会把没向量的都补上）。

不调大模型、不发消息，只在日志写一行摘要，结果给面板/排查用。
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from nonebot import logger
from nonebot_plugin_apscheduler import scheduler

ROOT = Path(__file__).resolve().parent.parent
HEALTH_FILE = ROOT / "memory" / "memory_health.json"
ARCHIVE_FILE = ROOT / "memory" / "长期记忆.json"
PROFILE_FILE = ROOT / "memory" / "entity_profile.json"


def _load_json(p: Path) -> dict:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _archive() -> dict:
    return (_load_json(ARCHIVE_FILE).get("archive") or {})


def _all_items() -> list[dict]:
    out = []
    for items in _archive().values():
        if isinstance(items, list):
            out.extend([i for i in items if isinstance(i, dict) and i.get("fact")])
    return out


def self_recall(top_n: int = 3, sample: int = 24) -> dict:
    """拿档案自己的原文当查询，看能不能召回自己。衡量索引质量，不需要大模型。"""
    try:
        import memory_vec as mv
    except Exception:  # noqa: BLE001
        return {"checked": 0, "hit": 0, "rate": None}
    if not mv.available():
        return {"checked": 0, "hit": 0, "rate": None}

    # 一次读盘，后面都在这份快照上比（原来每次 _archive() 都重新解析 JSON，
    # 对象不是同一个，用 is 比较永远不相等 → 自召回一直是 0）
    archive = _archive()
    idx = mv._load_index()["items"]  # noqa: SLF001

    def _vec(it):
        t = mv._item_text(it)  # noqa: SLF001
        return idx.get(mv._key(t)) if t else None  # noqa: SLF001

    pools = {sc: [i for i in arr if isinstance(i, dict) and i.get("fact")]
             for sc, arr in archive.items() if isinstance(arr, list)}
    all_items = [i for arr in pools.values() for i in arr]
    if not all_items:
        return {"checked": 0, "hit": 0, "rate": None}

    step = max(1, len(all_items) // sample)
    picked = all_items[::step][:sample]

    hit = checked = 0
    for it in picked:
        v = _vec(it)
        if not v:
            continue
        scope_items = next((arr for arr in pools.values() if any(o is it for o in arr)), None)
        if not scope_items:
            continue
        scored = []
        for other in scope_items:
            ov = _vec(other)
            if ov:
                scored.append((mv._cos(v, ov), id(other)))  # noqa: SLF001
        if not scored:
            continue
        scored.sort(key=lambda x: -x[0])
        checked += 1
        if any(o == id(it) for _, o in scored[:top_n]):
            hit += 1
    rate = round(hit / checked, 3) if checked else None
    return {"checked": checked, "hit": hit, "rate": rate}


async def _report_and_heal() -> None:
    try:
        import memory_vec as mv
    except Exception:  # noqa: BLE001
        mv = None

    # 全量补向量（后台慢慢补，不阻塞）
    built = 0
    if mv is not None and mv.available():
        try:
            built = await asyncio.to_thread(lambda: sum(mv.sync(v) for v in _archive().values() if isinstance(v, list)))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[记忆体检] 补向量失败：{type(e).__name__}: {e}")

    archive = _archive()
    items = _all_items()
    vec_total = 0
    if mv is not None:
        try:
            vec_total = len(mv._load_index()["items"])  # noqa: SLF001
        except Exception:  # noqa: BLE001
            vec_total = 0
    profiles = _load_json(PROFILE_FILE)
    recall = await asyncio.to_thread(self_recall)

    report = {
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "archive_entries": len(items),
        "vectors": vec_total,
        "coverage": round(vec_total / len(items), 3) if items else None,
        "self_recall_at3": recall,
        "profiles": len(profiles),
        "scopes": {k: len(v) for k, v in archive.items() if isinstance(v, list) and v},
        "built_this_run": built,
    }
    try:
        HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        HEALTH_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    rate = recall.get("rate")
    logger.info(
        f"[记忆体检] 档案 {len(items)} 条 · 向量 {vec_total}（覆盖 {report['coverage']}）"
        f" · 自召回@3 {rate if rate is not None else '—'} · 画像 {len(profiles)} 个"
        + (f" · 本轮补 {built} 条" if built else "")
    )


try:
    @scheduler.scheduled_job("cron", hour="4", minute="20", id="memory_health_daily")
    async def _daily() -> None:
        try:
            await _report_and_heal()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[记忆体检] 每日任务异常：{type(e).__name__}: {e}")

    @scheduler.scheduled_job("cron", minute="*/13", id="memory_health_first")
    async def _first() -> None:
        """启动后第一次；写成功就 remove_job，别每 13 分钟重跑同一段报错。"""
        if HEALTH_FILE.exists():
            try:
                if time.time() - HEALTH_FILE.stat().st_mtime < 3600:
                    scheduler.remove_job("memory_health_first")
                    return
            except Exception:  # noqa: BLE001
                pass
        try:
            await _report_and_heal()
            scheduler.remove_job("memory_health_first")   # 成功即停，避免常驻
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[记忆体检] 首次自检失败（下次再试）：{type(e).__name__}: {e}")
except Exception as e:  # noqa: BLE001
    logger.warning(f"[记忆体检] 定时任务注册失败：{e}")
