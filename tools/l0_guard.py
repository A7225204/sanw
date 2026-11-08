"""L0 护栏（2026-11-05，借 digital-life-core 的「L0 物理法则 + 造物主的钥匙」）。

问题：核心源码被改动后，只有等机器人**真的跑起来**、或者等到有人跟她说话没反应，
才发现改坏了。今天已经发生过两次（一次让她彻底哑掉）。

这里给核心文件加三道护栏：

    snapshot   改之前先存一份完整快照（带时间戳，不是散落的 .bak）
    verify     只读检查：语法 + 关键符号是否还在 + 有没有被误删
    rollback   从最近一次快照还原

**校验不通过时拒绝放行**，并打印具体缺了什么。

用法：
    python tools/l0_guard.py snapshot
    python tools/l0_guard.py verify
    python tools/l0_guard.py snapshot --then-edit    # 快照后提示可以动手
    python tools/l0_guard.py rollback [--to 20261105_213000]
    python tools/l0_guard.py status

造物主的钥匙：`L0_GUARD=off` 环境变量可让 verify 直接通过（应急用）。
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SNAP_DIR = ROOT / "backups" / "l0"
MANIFEST = SNAP_DIR / "manifest.json"

# 核心文件 + 必须存在的符号（改了名字/删了函数就会被抓出来）
CORE = {
    "plugins/ai_group.py": [
        "build_prompt_parts", "_local_zimage", "call_ai_raw", "_chat",
        "_fuckds_chat", "_quality_prefix", "_draw_self_triggers", "_strip_fake_state",
    ],
    "plugins/long_memory.py": [
        "archive_text", "archive_related", "consolidate", "add_learned", "add_pending",
    ],
    "plugins/hormones.py": ["values", "temperature_delta", "tick", "prompt_line"],
    "plugins/humanize.py": [
        "strip_cliches", "humanize", "strip_self_repeat", "humanize_add", "add_marks",
    ],
    "plugins/obsessions.py": ["touch", "answered", "active", "decay", "stats"],
    "config.py": ["load_config", "AIConfig"],
}


def _ts() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def _load_manifest() -> dict:
    try:
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"snapshots": []}


def _save_manifest(d: dict) -> None:
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def snapshot(tag: str = "") -> Path:
    """给所有核心文件拍一张完整快照。"""
    stamp = _ts() + (f"_{tag}" if tag else "")
    d = SNAP_DIR / stamp
    d.mkdir(parents=True, exist_ok=True)
    n = 0
    for rel in CORE:
        src = ROOT / rel
        if not src.exists():
            continue
        dst = d / rel.replace("/", "__")
        shutil.copy2(src, dst)
        n += 1
    m = _load_manifest()
    m["snapshots"].append({"id": stamp, "at": time.time(), "files": n,
                           "when": time.strftime("%Y-%m-%d %H:%M:%S")})
    m["snapshots"] = m["snapshots"][-30:]
    _save_manifest(m)
    print(f"✅ 快照 {stamp}（{n} 个文件）→ {d}")
    return d


# ===========================================================================
# 补丁极限安全锁（借 digital-life-core）：护栏自己也要有底线
#
# 它那边的规则是「patch:runtime 只能拦 digital_life_* / organ_*，
# llm/stream、shell 绝无可能被拦」—— 防的是"自保机制把自己锁死"。
# 对应到这里两条：
#   1. l0_guard.py 自己被动过（改判据、缩清单）→ 报警，但不阻断；
#   2. CORE 清单项数变少 → 报警（缩清单等于偷偷关护栏）。
# 唯一明确留的后门是 L0_GUARD=off，而且必须由人手动加。
# ===========================================================================

GUARD_SELF = Path(__file__).resolve()


def _sha(path: Path, n: int = 16) -> str:
    import hashlib
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:n]
    except Exception:  # noqa: BLE001
        return ""


def guard_integrity(verbose: bool = True) -> list[str]:
    """检查护栏自身完整性。返回问题列表（空=正常）。"""
    m = _load_manifest()
    g = m.get("guard") or {}
    probs: list[str] = []
    cur_sha = _sha(GUARD_SELF)
    cur_n = len(CORE)
    base_sha = g.get("sha")
    base_n = int(g.get("core_count") or 0)
    if not base_sha:
        m["guard"] = {"sha": cur_sha, "core_count": cur_n,
                      "when": time.strftime("%Y-%m-%d %H:%M:%S")}
        _save_manifest(m)
        if verbose:
            print(f"  🔐 护栏基线已登记（sha={cur_sha}, {cur_n} 个核心文件）")
        return []
    if cur_sha != base_sha:
        probs.append(f"l0_guard.py 被改过（{base_sha} → {cur_sha}）—— "
                     f"改判据可以，但请确认不是误操作缩了清单")
    if base_n and cur_n < base_n:
        probs.append(f"核心清单从 {base_n} 项缩到 {cur_n} 项 —— 缩清单等于关护栏")
    if verbose:
        if probs:
            for x in probs:
                print(f"  ⚠️ {x}")
        else:
            print(f"  🔐 护栏自身完好（sha={cur_sha}, {cur_n} 个核心文件）")
    return probs


def verify(verbose: bool = True) -> tuple[bool, list[str]]:
    """只读校验：语法 + 关键符号。返回 (是否通过, 问题列表)。"""
    if os.getenv("L0_GUARD", "").lower() in ("off", "0", "false"):
        if verbose:
            print("🔓 L0_GUARD=off —— 跳过校验（造物主的钥匙）")
        return True, []
    problems: list[str] = []
    for rel, symbols in CORE.items():
        p = ROOT / rel
        if not p.exists():
            problems.append(f"{rel}: 文件不存在")
            continue
        raw = p.read_text(encoding="utf-8-sig", errors="replace")
        # ① 编译（不是 ast.parse —— 那个抓不到「global 声明在使用之后」这类编译期错误，
        #    2026-11-05 就是被它放过去一次，导致 ai_group 整个模块导入失败、她不回话）
        try:
            compile(raw, rel, "exec")
            tree = ast.parse(raw)
        except SyntaxError as e:
            problems.append(f"{rel}: 第{e.lineno}行 —— {e.msg}")
            continue
        # ② 关键符号（函数/类/赋值名）
        defined = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.add(node.name)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        defined.add(t.id)
        missing = [s for s in symbols if s not in defined]
        if missing:
            problems.append(f"{rel}: 缺少 {', '.join(missing)}")
        # ③ 明显的截断/乱码
        if len(raw) < 500 and rel.endswith("ai_group.py"):
            problems.append(f"{rel}: 文件只有 {len(raw)} 字节，疑似被截断")
        if verbose and not missing:
            print(f"  ✓ {rel}（{len(raw)} 字节，{len(defined)} 个定义）")
    if problems:
        if verbose:
            print()
            for x in problems:
                print(f"  ❌ {x}")
            print()
            print("🔴 L0 校验未通过 —— 先修好再重启，或用 rollback 还原")
        return False, problems
    if verbose:
        print(f"\n✅ L0 校验通过（{len(CORE)} 个核心文件）")
        guard_integrity(verbose=True)
    return True, []


def rollback(to: str = "", only: str = "") -> bool:
    """从快照还原。不给 to 就用最近一次；only 指定只还原单个文件（相对路径）。"""
    m = _load_manifest()
    snaps = m.get("snapshots") or []
    if not snaps:
        print("没有可用快照")
        return False
    target = None
    if to:
        for s in snaps:
            if s["id"] == to:
                target = s
                break
    else:
        target = snaps[-1]
    if not target:
        print(f"找不到快照 {to}")
        return False
    d = SNAP_DIR / target["id"]
    n = 0
    for f in d.iterdir():
        rel = f.name.replace("__", "/")
        if only and rel != only:
            continue
        dst = ROOT / rel
        shutil.copy2(f, dst)
        n += 1
        print(f"  还原 {rel}")
    print(f"\n✅ 已从 {target['id']} 还原 {n} 个文件")
    return True


def status() -> None:
    m = _load_manifest()
    snaps = m.get("snapshots") or []
    print(f"快照目录：{SNAP_DIR}")
    print(f"快照数量：{len(snaps)}")
    for s in snaps[-5:]:
        print(f"  {s['id']}  {s['when']}  {s['files']} 文件")
    print()
    ok, probs = verify(verbose=True)
    sys.exit(0 if ok else 1)


def main() -> None:
    ap = argparse.ArgumentParser(description="L0 核心文件护栏")
    ap.add_argument("action", choices=["snapshot", "verify", "rollback", "status"])
    ap.add_argument("--tag", default="", help="快照标签")
    ap.add_argument("--to", default="", help="还原到指定快照 id")
    ap.add_argument("--file", default="", help="只还原这一个文件，如 plugins/obsessions.py")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    if a.action == "snapshot":
        snapshot(a.tag)
    elif a.action == "verify":
        ok, _ = verify(verbose=not a.quiet)
        sys.exit(0 if ok else 1)
    elif a.action == "rollback":
        rollback(a.to, a.file)
    else:
        status()


if __name__ == "__main__":
    main()
