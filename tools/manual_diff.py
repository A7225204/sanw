"""比对 系统全景手册.md 与实际代码，输出差异。

用法：
    .venv\\Scripts\\python.exe tools/manual_diff.py
    .venv\\Scripts\\python.exe tools/manual_diff.py --strict   # 有差异时退出码 1

检查三类：
  1. plugins/ 实际 .py 文件 vs 手册 §四 提到的
  2. 定时任务 id vs 手册 §五 表格
  3. 已装 DSH bundle vs 手册附录 C.1
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANUAL = ROOT / "docs" / "系统全景手册.md"


def _section(text: str, start: str, end: str) -> str:
    try:
        return text.split(start, 1)[1].split(end, 1)[0]
    except IndexError:
        return ""


def check_plugins(text: str) -> list[str]:
    # 只取 plugins 表格部分，根目录模块表单独算，避免误判
    sec4 = _section(text, "## 四、功能模块", "### 根目录模块")
    mentioned = set(re.findall(r"`([a-z0-9_]+)\.py`", sec4))
    actual = {p.stem for p in (ROOT / "plugins").glob("*.py") if p.name != "__init__.py"}
    out = []
    missing = sorted(actual - mentioned)
    extra = sorted(mentioned - actual)
    if missing:
        out.append(f"plugins: 实际 {len(actual)} 个，手册 §四 未提 {len(missing)} 个 → {missing}")
    if extra:
        out.append(f"plugins: 手册 §四 提到但文件不存在 → {extra}")
    return out


def check_jobs(text: str) -> list[str]:
    sec5 = _section(text, "## 五、定时任务", "## 六、")
    man_ids = set(re.findall(r"`([a-z_]+)`", sec5))
    code_ids: set[str] = set()
    for p in (ROOT / "plugins").glob("*.py"):
        src = p.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r'scheduled_job\([^)]*?id\s*=\s*"([^"]+)"', src, re.S):
            code_ids.add(m.group(1))
    out = []
    missing = sorted(code_ids - man_ids)
    if missing:
        out.append(f"jobs: 代码有、手册 §五 未提 → {missing}")
    return out


def check_bundles(text: str) -> list[str]:
    pkg_path = Path.home() / ".dsh" / "profiles" / "web" / "package.json"
    if not pkg_path.exists():
        return ["bundle: 找不到 ~/.dsh/profiles/web/package.json"]
    pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
    actual = set(pkg.get("dsh", {}).get("profile", {}).get("bundles") or [])
    sec_c = _section(text, "## C.1", "**装过又卸的**")
    mentioned = set(re.findall(r"`([@A-Za-z0-9_./-]+)`", sec_c))
    out = []
    missing = sorted(actual - mentioned)
    if missing:
        out.append(f"bundle: 实际 {len(actual)} 个，手册 C.1 未提 → {missing}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true", help="有差异时退出码 1")
    args = ap.parse_args()

    if not MANUAL.exists():
        print(f"找不到手册：{MANUAL}")
        return 2
    text = MANUAL.read_text(encoding="utf-8-sig")

    issues: list[str] = []
    for fn in (check_plugins, check_jobs, check_bundles):
        try:
            issues.extend(fn(text))
        except Exception as e:  # noqa: BLE001
            issues.append(f"{fn.__name__}: 检查异常 {type(e).__name__}: {e}")

    if not issues:
        print("[manual_diff] 手册与实际代码一致 ✅")
        return 0
    print(f"[manual_diff] 发现 {len(issues)} 类差异：")
    for i in issues:
        print(" -", i)
    return 1 if args.strict else 0


if __name__ == "__main__":
    sys.exit(main())
