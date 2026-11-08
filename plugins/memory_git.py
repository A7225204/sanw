"""记忆版本管理：memory/ 的每次变化进 git，能看 diff、能回滚。

来源：Tobs（BotHarness/DeepSeekBot）的核心设计 ——
> 每个 Bot 的记忆就是一个普通的 Git 仓库。你可以在侧栏翻看它记了什么、
> 哪天改了哪几行，觉得不对就直接改；推到 GitHub，就能在几台电脑之间共用同一个 Bot。

**我们为什么不直接在 memory/ 里 init**：
`memory/` 已经被主仓库 `.gitignore` 排掉了（里面是私密数据），
但如果在 `memory/` 里再放一个 `.git`，会和主仓库的目录遍历打架。

所以用**分离式仓库**：

    GIT_DIR   = data/memory.git      （版本库）
    WORK_TREE = memory/              （工作区）

不污染主仓库，也不影响 `memory/` 的目录结构。

**排掉的**：向量索引（12MB，每次全量变化，能重建）、媒体文件、备份。
**保留的**：长期记忆、情绪状态、三我、日记、执念池这些真正的内容。

对外：
    snapshot(msg)     提交一次（没变化就不提交）
    history(n)        最近 n 次提交
    diff(ref)         某次提交改了什么
    show(ref, path)   某次提交里某个文件的全文
    rollback(ref)     回滚到某次提交（只回滚工作区，不动历史）
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GIT_DIR = ROOT / "data" / "memory.git"
WORK_TREE = ROOT / "memory"

ENV = {
    **os.environ,
    "GIT_DIR": str(GIT_DIR),
    "GIT_WORK_TREE": str(WORK_TREE),
    "GIT_AUTHOR_NAME": "feiyu-bot",
    "GIT_AUTHOR_EMAIL": "bot@local",
    "GIT_COMMITTER_NAME": "feiyu-bot",
    "GIT_COMMITTER_EMAIL": "bot@local",
    # 中文文件名不转义，log 里能直接读
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "core.quotepath",
    "GIT_CONFIG_VALUE_0": "false",
}


# 2026-11-08：面板 server.py 跑在 pythonw 下（没有控制台），前端每 5 秒轮询 /api/health，
# 每次都会经 self_check 走这里跑两条 git。如果不带 CREATE_NO_WINDOW，
# Windows 会给每条 git 新建一个控制台 → 屏幕上就是黑框一闪一闪。这里统一压掉。
_NOWIN = {"creationflags": 0x08000000} if os.name == "nt" else {}


def _run(*args: str, timeout: int = 30) -> tuple[int, str]:
    try:
        r = subprocess.run(["git", *args], capture_output=True, env=ENV,
                           timeout=timeout, cwd=str(ROOT), **_NOWIN)
        out = (r.stdout or b"").decode("utf-8", "replace")
        err = (r.stderr or b"").decode("utf-8", "replace")
        return r.returncode, (out or err).strip()
    except Exception as e:  # noqa: BLE001
        return 1, f"{type(e).__name__}: {e}"


def ready() -> bool:
    return (GIT_DIR / "HEAD").exists()


def snapshot(msg: str = "") -> dict:
    """提交一次。没变化返回 {committed: False}。"""
    if not ready():
        return {"ok": False, "error": "记忆仓库还没初始化"}
    code, out = _run("add", "-A")
    if code != 0:
        # 2026-11-07：另一个路径（server/memory_growth 固化时）也在往同一个仓库提交，
        # 撞上 .git/index.lock 就直接非 0 —— 这不是"仓库坏了"，等一秒重试一次。
        # 顺带把 stderr 带出来：以前这里丢弃 out，出错完全查不到原因。
        time.sleep(1.0)
        code, out = _run("add", "-A")
        if code != 0:
            return {"ok": False, "error": f"add 失败：{out[:120]}"}
    code, out = _run("status", "--porcelain")
    if code != 0:
        return {"ok": False, "error": out}
    if not out.strip():
        return {"ok": True, "committed": False, "reason": "没有变化"}

    n_changed = len([x for x in out.splitlines() if x.strip()])
    text = (msg or "").strip() or f"auto: {n_changed} 个文件变化"
    code, out2 = _run("commit", "-m", text)
    if code != 0:
        return {"ok": False, "error": out2[-300:]}
    code, h = _run("rev-parse", "--short", "HEAD")
    return {"ok": True, "committed": True, "hash": h.strip(), "files": n_changed, "msg": text}


def history(n: int = 12, path: str = "") -> list[dict]:
    """最近 n 次提交。path 限定只看某个文件的。"""
    args = ["log", f"-{max(1, min(200, n))}", "--date=format:%m-%d %H:%M",
            "--pretty=format:%h\x1f%ad\x1f%s"]
    if path:
        args += ["--", path]
    code, out = _run(*args)
    if code != 0 or not out:
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) >= 3:
            rows.append({"hash": parts[0], "time": parts[1], "msg": parts[2]})
    return rows


def diff(ref: str = "HEAD", path: str = "") -> str:
    """某次提交改了什么（unified diff）。"""
    args = ["show", "--stat", "--patch", "--format=", ref]
    if path:
        args += ["--", path]
    code, out = _run(*args, timeout=60)
    if code != 0:
        return f"取 diff 失败：{out[:200]}"
    # 只给前 400 行，面板放不下
    lines = out.splitlines()
    if len(lines) > 400:
        lines = lines[:400] + [f"... 还有 {len(lines) - 400} 行"]
    return "\n".join(lines)


def stat(ref: str = "HEAD") -> str:
    """只看统计（快）。"""
    code, out = _run("show", "--stat", "--format=%h %ad %s", "--date=format:%m-%d %H:%M", ref)
    return out if code == 0 else ""


def show(ref: str, path: str) -> str:
    """某次提交里某个文件的全文。"""
    code, out = _run("show", f"{ref}:{path}")
    return out if code == 0 else f"取不到：{out[:150]}"


def rollback(ref: str) -> dict:
    """把工作区恢复成某次提交的样子。

    **只改工作区，不动历史** —— 回滚本身也是一次新提交，
    所以不会有「回滚后又想回去」的问题。
    """
    if not ready():
        return {"ok": False, "error": "仓库没初始化"}
    code, out = _run("checkout", ref, "--", ".")
    if code != 0:
        return {"ok": False, "error": out[-250:]}
    r = snapshot(f"rollback: 回到 {ref}")
    return {"ok": True, "restored": ref, "new_commit": r.get("hash", "")}


def files_tracked() -> int:
    code, out = _run("ls-files")
    return len(out.splitlines()) if code == 0 else 0


def size_mb() -> float:
    try:
        total = sum(f.stat().st_size for f in GIT_DIR.rglob("*") if f.is_file())
        return round(total / 1048576, 1)
    except Exception:  # noqa: BLE001
        return 0.0


def stats() -> dict:
    h = history(1)
    return {
        "ready": ready(),
        "commits": len(history(200)),
        "files": files_tracked(),
        "size_mb": size_mb(),
        "last": h[0] if h else None,
    }


def register_schedule() -> None:
    """每小时自动提交一次（有变化才提交）。"""
    try:
        from nonebot_plugin_apscheduler import scheduler
    except Exception:  # noqa: BLE001
        return

    @scheduler.scheduled_job("interval", minutes=60, id="memory_git_snapshot",
                             misfire_grace_time=600, coalesce=True)
    async def _tick() -> None:
        try:
            r = snapshot()
            if r.get("committed"):
                from nonebot import logger
                logger.info(f"[记忆版本] {r['files']} 个文件变化 → {r.get('hash')}：{r.get('msg')}")
        except Exception as e:  # noqa: BLE001
            from nonebot import logger
            logger.warning(f"[记忆版本] 提交失败：{type(e).__name__}: {e}")

    # 2026-11-07：启动 90 秒后**补一次**。
    # interval 的计时从进程启动算起 —— 今晚 bot 重启了十几次，每次间隔都不到 60 分钟，
    # 于是这个 hourly 任务一次都没跑过，面板上就报「记忆仓库超过 2 小时没提交（25 个待提交）」。
    # 启动即补：有改动就提交，提交完自删任务。
    @scheduler.scheduled_job("interval", seconds=90, id="memory_git_boot",
                             misfire_grace_time=300, coalesce=True, max_instances=1)
    async def _boot_once() -> None:
        from nonebot import logger
        try:
            r = snapshot()
            if r.get("committed"):
                logger.info(f"[记忆版本] 启动补提交 {r['files']} 个文件 → {r.get('hash')}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[记忆版本] 启动补提交失败：{type(e).__name__}: {e}")
        try:
            scheduler.remove_job("memory_git_boot")
        except Exception:  # noqa: BLE001
            pass


try:
    register_schedule()
except Exception:  # noqa: BLE001
    pass
