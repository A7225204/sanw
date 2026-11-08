"""自检：不读全量数据，只读元数据，一秒内回答「我坏没坏」。

挪自灵枢（FuRongJun-1999/dsh-memory）的 SelfCheck 契约：

    status() -> {counts, lag, drift, wal_ok}
    **不变量：必须能在不读全部节点的情况下答「我坏没坏」**

对比我们原来的 `check.py` —— 要读 283 条档案 + 304 个向量，跑一次十几秒。
那个适合「定期体检」，不适合「随时问一句」。

这里只查**四个便宜的指标**：

    counts   各层条目数（读 json 头部/文件统计，不解析内容）
    lag      最后一次写入距今多久（看文件 mtime）
    drift    向量索引条目数 vs 档案条目数（只比数字，不比对内容）
    wal_ok   git 工作区有没有未提交的改动

任一项不过 → ok=False，并给出**具体哪项、差多少**。

对外：
    status()     完整状态（约 50~200 ms）
    ok()         只返回 True/False（给它自己用）
    line()       一句话（给日志/面板用）
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MEM = ROOT / "memory"

# 各层的「主文件」——只看 mtime 和大小，不解析
LAYERS = {
    "长期档案": MEM / "长期记忆.json",
    "人格成长": MEM / "persona_growth.json",
    "情绪状态": MEM / "hormones_state.json",
    "执念池": MEM / "执念池.json",
    "行为证据": MEM / "ego_evidence.json",
}

# lag 超过这个就算慢（秒）。一天没写记忆 = 她没在过日子
LAG_WARN = 26 * 3600
LAG_BAD = 72 * 3600


def _mtime_age(p: Path) -> float:
    try:
        return max(0.0, time.time() - p.stat().st_mtime)
    except Exception:  # noqa: BLE001
        return -1.0


def _count_json_items(p: Path) -> int:
    """只数条目，不建完整对象（尽量便宜）。"""
    try:
        raw = p.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return -1
    # 快速路径：archive 里各 scope 的数组长度
    try:
        d = json.loads(raw)
    except Exception:  # noqa: BLE001
        return -1
    if not isinstance(d, dict):
        return len(d) if isinstance(d, list) else -1
    n = 0
    arch = d.get("archive")
    if isinstance(arch, dict):
        for v in arch.values():
            if isinstance(v, list):
                n += len(v)
    elif isinstance(arch, list):
        n += len(arch)
    for k in ("items",):
        v = d.get(k)
        if isinstance(v, list):
            n += len(v)
    return n or len(d)


def _vec_count() -> int:
    """向量条目数（只读索引的 items 键数，不读向量本身）。"""
    for name in ("vec_index.json", "mem_vectors.json"):
        p = MEM / name
        if not p.exists():
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        items = d.get("items") if isinstance(d, dict) else None
        if isinstance(items, dict):
            return len(items)
        if isinstance(d, list):
            return len(d)
    return -1


def _git_clean() -> tuple[bool, int]:
    """记忆仓库健康吗。返回 (健康, 未提交文件数)。

    **2026-11-07 改判据**：原来只要「有未提交文件」就判不干净 ——
    但 `长期记忆.json` 本来就在一直变（每次记忆更新），
    于是灯永远黄。那不是故障，是它在干活。

    真正该报的是「**自动提交停了**」—— 记忆仓库每小时 commit 一次
    （`memory_git_snapshot`），超过 2 小时没 commit 说明定时任务挂了。
    所以现在：未提交文件数照报（给人看），但健不健康看**最后一次 commit 距今多久**。
    """
    try:
        from plugins import memory_git as mg
        if not mg.ready():
            return True, 0
        code, out = mg._run("status", "--porcelain")
        n_dirty = len([x for x in out.splitlines() if x.strip()]) if code == 0 else 0
        # 看最后一次 commit 多久以前
        code2, ts = mg._run("log", "-1", "--format=%ct")
        if code2 == 0 and ts.strip().isdigit():
            age_h = (time.time() - int(ts.strip())) / 3600.0
            # 2 小时内提交过 = 健康（定时任务是每小时一次）
            return (age_h <= 2.0), n_dirty
        return True, n_dirty
    except Exception:  # noqa: BLE001
        return True, 0


def status() -> dict:
    # ① counts + lag：只看文件 stat
    counts: dict[str, int] = {}
    lags: dict[str, float] = {}
    for name, p in LAYERS.items():
        if not p.exists():
            counts[name] = -1
            lags[name] = -1.0
            continue
        counts[name] = _count_json_items(p)
        lags[name] = _mtime_age(p)

    # ② drift：档案数 vs 向量数
    # 2026-11-07 修：原来直接 `向量数 - 档案数`，被判成"多 32 条 → 索引脏了"。
    # 查下来不是脏，是两个口径本来就不一样：
    #   档案 283 条 = 所有 scope 的条目总和（id 是**每个 scope 内自增**的，
    #                全局唯一 id 只有 155 个 —— 同一个 id=50 在 4 个群里都有）
    #   向量 315 条 = 按 `_item_text()` 的内容 hash 去重后存的
    # 所以这两个数不该直接比。真正有意义的只有一种：**向量明显少于档案**（漏建了）。
    # 多于档案的，说明索引里留着已删条目的旧向量 —— 不致命，只是白占地方。
    n_arch = counts.get("长期档案", -1)
    n_vec = _vec_count()
    drift = 0
    drift_note = ""
    if n_arch > 0 and n_vec >= 0:
        if n_vec < n_arch * 0.8:
            drift = n_vec - n_arch          # 负数 = 漏建
        elif n_vec > n_arch * 1.5:
            drift_note = f"向量 {n_vec} 明显多于档案 {n_arch}（可能留着已删内容，不致命）"

    # ③ wal：git 工作区
    clean, dirty = _git_clean()

    # ④ 判定
    issues: list[str] = []
    worst_lag = max((v for v in lags.values() if v >= 0), default=-1)
    if worst_lag < 0:
        issues.append("所有记忆文件都读不到")
    elif worst_lag > LAG_BAD:
        issues.append(f"记忆 {worst_lag/3600:.1f} 小时没写过（超过 {LAG_BAD/3600:.0f}h）")
    elif worst_lag > LAG_WARN:
        issues.append(f"记忆 {worst_lag/3600:.1f} 小时没写过（提醒线 {LAG_WARN/3600:.0f}h）")
    if drift < 0:
        issues.append(f"向量比档案少 {-drift} 条（漏建了，要 sync）")
    if not clean:
        issues.append(f"记忆仓库超过 2 小时没提交（{dirty} 个待提交）—— 定时任务可能挂了")

    return {
        "ok": not issues,
        "counts": counts,
        "lag_hours": {k: (round(v / 3600, 1) if v >= 0 else -1) for k, v in lags.items()},
        "drift": drift,
        "drift_note": drift_note,
        "wal_ok": clean,
        "dirty_files": dirty,
        "issues": issues,
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def ok() -> bool:
    return bool(status().get("ok"))


def line() -> str:
    """一句话（日志/面板用）。"""
    s = status()
    c = s["counts"]
    head = "正常" if s["ok"] else "有 " + str(len(s["issues"])) + " 项异常"
    return (f"[自检] {head} | 档案 {c.get('长期档案', '?')} · "
            f"成长 {c.get('人格成长', '?')} · 向量 {_vec_count()} · "
            f"lag {max((v for v in s['lag_hours'].values() if v >= 0), default=-1):.1f}h | "
            + ("; ".join(s["issues"]) if s["issues"] else "无异常"))
