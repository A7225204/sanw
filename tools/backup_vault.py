#!/usr/bin/env python
"""备份与还原演练（2026-11-05，借鱼神双子星的 restore-drill 思路）。

核心判断：**备份不算数，能读回来才算。**

用法：
    python tools/backup_vault.py backup          # 全量备份到 backups/vault/<时间戳>/
    python tools/backup_vault.py drill           # 拿最新备份还原到临时目录，逐项核对
    python tools/backup_vault.py drill --at 20261105_1310
    python tools/backup_vault.py list            # 列出已有备份

drill 退出码 0 = 能读回来；非 0 = 备份不可用，别等真出事才发现。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VAULT = ROOT / "backups" / "vault"

# 要备份的东西：配置文件、插件代码、记忆、人格、工作流、面板
INCLUDE_FILES = [
    "config.py",
    "persona.md",
    "comfy_workflows/dafeiyu_xl.json",
    "comfy_workflows/qwen_vl_recognize.json",
    "dashboard/index.html",
    "docs/系统全景手册.md",
]
INCLUDE_DIRS = [
    "plugins",
    "memory",
]
# 跳过：缓存/临时/大文件
SKIP_SUFFIX = {".pyc", ".log", ".mp3", ".wav", ".png", ".jpg", ".jpeg", ".mp4", ".db", ".safetensors"}
SKIP_DIRS = {"__pycache__", ".git", "backups"}


def _walk(src: Path):
    for p in src.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.suffix.lower() in SKIP_SUFFIX:
            continue
        yield p


def _manifest(files: list[tuple[Path, str]]) -> dict:
    return {
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(files),
        "files": [{"rel": rel, "size": p.stat().st_size} for p, rel in files],
    }


def do_backup() -> int:
    stamp = time.strftime("%Y%m%d_%H%M")
    out = VAULT / stamp
    if out.exists():
        print(f"✗ 备份目录已存在：{out}")
        return 1
    out.mkdir(parents=True, exist_ok=True)
    copied: list[tuple[Path, str]] = []
    for rel in INCLUDE_FILES:
        src = ROOT / rel
        if src.is_file():
            dst = out / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied.append((src, rel))
    for rel in INCLUDE_DIRS:
        src_dir = ROOT / rel
        if not src_dir.is_dir():
            continue
        for src in _walk(src_dir):
            relp = str(src.relative_to(ROOT))
            dst = out / relp
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(src, dst)
                copied.append((src, relp))
            except Exception as e:  # noqa: BLE001
                print(f"  跳过 {relp}: {type(e).__name__}")
    man = _manifest(copied)
    (out / "MANIFEST.json").write_text(
        json.dumps(man, ensure_ascii=False, indent=2), encoding="utf-8")
    total = sum(f["size"] for f in man["files"])
    print(f"✓ 备份完成：{out}")
    print(f"  {man['count']} 个文件，{total/1024/1024:.2f} MB")
    return 0


def _latest() -> Path | None:
    if not VAULT.is_dir():
        return None
    dirs = sorted([d for d in VAULT.iterdir() if d.is_dir() and (d / "MANIFEST.json").is_file()])
    return dirs[-1] if dirs else None


def do_drill(at: str | None = None) -> int:
    src = (VAULT / at) if at else _latest()
    if not src or not src.is_dir():
        print("✗ 找不到备份。先跑 backup。")
        return 2
    man_path = src / "MANIFEST.json"
    if not man_path.is_file():
        print(f"✗ 备份缺 MANIFEST.json：{src}")
        return 2

    print(f"演练备份：{src.name}")
    try:
        man = json.loads(man_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"✗ MANIFEST 读不了：{type(e).__name__}: {e}")
        return 2

    fails: list[str] = []
    ok = 0
    tmp = Path(tempfile.mkdtemp(prefix="drill_"))
    try:
        # ① 逐文件复制到隔离目录并核对字节数
        for f in man.get("files") or []:
            rel = str(f.get("rel") or "")
            want = int(f.get("size") or 0)
            s = src / rel
            if not s.is_file():
                fails.append(f"缺文件 {rel}")
                continue
            d = tmp / rel
            d.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(s, d)
            except Exception as e:  # noqa: BLE001
                fails.append(f"复制失败 {rel}: {type(e).__name__}")
                continue
            got = d.stat().st_size
            if got != want:
                fails.append(f"大小不符 {rel}: {got} != {want}")
            else:
                ok += 1

        # ② 关键 JSON 能被解析（记忆写坏了在这里就会暴露）
        for key in ("memory/长期记忆.json", "memory/persona_soul.json",
                    "memory/hormones_state.json", "comfy_workflows/dafeiyu_xl.json"):
            p = tmp / key
            if not p.is_file():
                fails.append(f"关键文件缺失 {key}")
                continue
            try:
                d = json.loads(p.read_text(encoding="utf-8-sig"))
                if key.endswith("长期记忆.json"):
                    arch = (d or {}).get("archive") or {}
                    n = sum(len(v or []) for v in arch.values())
                    if n <= 0:
                        fails.append(f"{key} 条目数为 0")
                    else:
                        print(f"  长期记忆可读：{n} 条")
                elif key.endswith("dafeiyu_xl.json"):
                    if "12" not in d:
                        fails.append(f"{key} 缺 KSampler 节点")
                    else:
                        print(f"  工作流可读：cfg={d['12']['inputs'].get('cfg')} "
                              f"lora={d.get('19', {}).get('inputs', {}).get('strength_model', '?')}")
            except Exception as e:  # noqa: BLE001
                fails.append(f"{key} 解析失败：{type(e).__name__}")

        # ③ 插件代码能被编译（语法错的插件还原回去也跑不起来）
        bad = 0
        for p in (tmp / "plugins").glob("*.py"):
            try:
                compile(p.read_text(encoding="utf-8-sig"), str(p), "exec")
            except SyntaxError as e:
                fails.append(f"语法错 {p.name}:{e.lineno}")
                bad += 1
        print(f"  插件语法检查：{len(list((tmp/'plugins').glob('*.py')))} 个，{bad} 个有问题")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print(f"✗ 演练失败 {len(fails)} 项（核对通过 {ok} 个文件）：")
        for f in fails[:12]:
            print(f"  - {f}")
        if len(fails) > 12:
            print(f"  ...还有 {len(fails)-12} 项")
        return 1
    print(f"✓ 演练通过：{ok} 个文件全部可读，关键数据完好")
    return 0


def do_list() -> int:
    if not VAULT.is_dir():
        print("还没有任何备份。")
        return 0
    dirs = sorted([d for d in VAULT.iterdir() if d.is_dir()], reverse=True)
    if not dirs:
        print("还没有任何备份。")
        return 0
    for d in dirs[:20]:
        m = d / "MANIFEST.json"
        if m.is_file():
            try:
                man = json.loads(m.read_text(encoding="utf-8"))
                sz = sum(x.get("size", 0) for x in man.get("files") or [])
                print(f"  {d.name}  {man.get('count')} 文件  {sz/1024/1024:.2f} MB  {man.get('at')}")
            except Exception:  # noqa: BLE001
                print(f"  {d.name}  （MANIFEST 损坏）")
        else:
            print(f"  {d.name}  （无 MANIFEST）")
    return 0


def main() -> int:
    argv = sys.argv[1:]
    cmd = argv[0] if argv else "drill"
    at = None
    if "--at" in argv:
        i = argv.index("--at")
        if i + 1 < len(argv):
            at = argv[i + 1]
    if cmd == "backup":
        return do_backup()
    if cmd == "drill":
        return do_drill(at)
    if cmd == "list":
        return do_list()
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
