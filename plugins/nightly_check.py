"""夜间巡检：每天凌晨跑一遍，有事推飞书卡片，没事闭嘴。

2026-11-06 加（借 agent-autoloop 的思路）：
现在的链路是「主人说话 → 她干 → 她回」，缺一条「她自己发现问题 → 主动找人」。
补的就是这条：服务挂没挂、内存显存还剩多少、昨天日志有没有 ERROR —— 查出来推给主人。

设计原则（跟那边一致）：
  · 正常就一句话不说 —— 天天推「一切正常」等于噪音
  · 有问题才推，且必须说清楚「哪个、现在什么值、大概怎么修」
  · 纯只读，不改任何东西

开关：NIGHTLY_CHECK=off 可整体关闭。
"""
from __future__ import annotations

import asyncio
import os
import re
import socket
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from nonebot import logger

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "logs"

# 要盯的端口（关了不算故障的放 in_tail）
SERVICES = [
    ("机器人 bot",        8080,  True),
    ("NapCat",            None,  True),    # NapCatWinBootMain / NapCatWinBootHook
    ("桌宠气泡",           None,  False),   # 按进程名判
    ("面板",              8090,  False),
    ("ComfyUI",           8188,  False),   # 按需启停，关着正常
    ("本地 TTS",          9881,  False),
    ("IndexTTS2",         7860,  False),   # 按需启停
    ("本地 LLM (ollama)", 11434, False),
    ("记忆向量 bge-m3",    None,  False),
]

# 硬阈值
MAX_MEM_PCT = 88.0          # 内存占用超过这个比例就报
MAX_DISK_PCT = 93.0
MIN_VRAM_FREE_MB = 800      # 显存剩不到这么多就报（12G 卡跑视频会炸）
LOG_WINDOW_HOURS = 6


def _port_open(port: int, timeout: float = 1.5) -> bool:
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        s.close()


def _proc_running(pattern: str) -> bool:
    """按进程名或命令行找进程。

    2026-11-06 修：原来只看 CommandLine，但 NapCat 是 NapCatWinBootMain.exe 起的、
    命令行里没有 "napcat"，于是天天误报「NapCat 挂了」。进程名也一起匹配。
    """
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | "
             "ForEach-Object { '{0} :: {1}' -f $_.Name, $_.CommandLine }"],
            capture_output=True, text=True, timeout=25, errors="replace",
        ).stdout
        return bool(re.search(pattern, out or "", re.I))
    except Exception:  # noqa: BLE001
        return False


def _mem() -> tuple[float, float, float]:
    """返回 (总GB, 可用GB, 已用百分比)。"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "$o=Get-CimInstance Win32_OperatingSystem;"
             "'{0} {1}' -f $o.TotalVisibleMemorySize,$o.FreePhysicalMemory"],
            capture_output=True, text=True, timeout=20,
        ).stdout.split()
        total = float(out[0]) / 1024 / 1024
        free = float(out[1]) / 1024 / 1024
        return total, free, (total - free) / total * 100
    except Exception:  # noqa: BLE001
        return 0.0, 0.0, 0.0


def _vram() -> tuple[int, int]:
    """返回 (已用MB, 总MB)。没有 nvidia-smi 就返回 0,0。"""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=20,
        ).stdout.strip().splitlines()[0]
        used, total = (int(x.strip()) for x in out.split(","))
        return used, total
    except Exception:  # noqa: BLE001
        return 0, 0


def _disks() -> list[tuple[str, float, float]]:
    """返回 [(盘符, 已用%, 剩余GB)]。"""
    res = []
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-PSDrive -PSProvider FileSystem | "
             "Where-Object {$_.Used -ne $null} | "
             "ForEach-Object {'{0}|{1}|{2}' -f $_.Name,$_.Used,$_.Free}"],
            capture_output=True, text=True, timeout=25,
        ).stdout
        for ln in (out or "").splitlines():
            p = ln.strip().split("|")
            if len(p) != 3:
                continue
            name = p[0]
            try:
                used, free = float(p[1]), float(p[2])
            except ValueError:
                continue
            tot = used + free
            if tot <= 0:
                continue
            res.append((name, used / tot * 100, free / 1024 ** 3))
    except Exception:  # noqa: BLE001
        pass
    return res


# 常见异常的「人话」翻译：飞书卡片是给人看的，别丢一串英文类名。
# 用函数式规则（不是 re.sub 模板），因为模板里的反向引用经过多层转义容易失真。
_ERR_ZH_PATTERNS = [
    (r"NameError:\s*name '([^']+)' is not defined",
     lambda m: f"变量「{m.group(1)}」没定义（代码 bug）"),
    (r"AttributeError:\s*'([^']+)' object has no attribute '([^']+)'",
     lambda m: f"「{m.group(1)}」没有 {m.group(2)} 这个属性（版本不匹配或拼错）"),
    (r"KeyError:\s*(.+)",
     lambda m: f"取不到键 {m.group(1)}（数据结构对不上）"),
    (r"FileNotFoundError:\s*(.+)",
     lambda m: f"文件不存在：{m.group(1)}"),
    (r"PermissionError:\s*(.+)",
     lambda m: f"权限不足：{m.group(1)}"),
    (r"ModuleNotFoundError:\s*No module named '([^']+)'",
     lambda m: f"缺模块「{m.group(1)}」（没装）"),
    (r"ImportError:\s*(.+)",
     lambda m: f"导入失败：{m.group(1)}"),
    (r"ConnectionRefusedError", lambda m: "对方拒绝连接（服务没起或端口不对）"),
    (r"ConnectionAbortedError", lambda m: "连接被对面中断（浏览器/客户端提前关了）"),
    (r"ConnectionResetError", lambda m: "连接被重置（网络或对端崩了）"),
    (r"TimeoutError|timed out|Timeout", lambda m: "超时（对方太久没回）"),
    (r"CancelledError", lambda m: "任务被取消（正常现象，重启时常见）"),
    (r"JSONDecodeError|Expecting value",
     lambda m: "返回的不是合法 JSON（接口挂了或返回了错误页）"),
    (r"UnicodeDecodeError", lambda m: "编码错误（文件不是 UTF-8）"),
    (r"SyntaxError:\s*(.+)", lambda m: f"语法错误：{m.group(1)}"),
    (r"CUDA out of memory|(?<![A-Za-z])OOM(?![A-Za-z])", lambda m: "显存不够"),
    (r"^\s*RuntimeError:\s*(.+)", lambda m: f"运行时错误：{m.group(1)}"),
    (r"^\s*ValueError:\s*(.+)", lambda m: f"值不合法：{m.group(1)}"),
    (r"^\s*TypeError:\s*(.+)", lambda m: f"类型不对：{m.group(1)}"),
    # 下面几条是「日志行」不是「异常类名」，但同样常见、同样该翻
    (r'apscheduler .*Job "([^"]+)".*was missed',
     lambda m: f'定时任务「{m.group(1).split(" (trigger")[0]}」错过执行（那时 bot 在重启）'),
    (r'Failed to import "([^"]+)"',
     lambda m: f'插件「{m.group(1)}」加载失败'),
    (r'Job "([^"]+)".*raised an exception',
     lambda m: f'定时任务「{m.group(1).split(" (trigger")[0]}」执行时抛错'),
    (r'Failed to perform initial fetching',
     lambda m: '抓取远程清单失败（网络问题，可忽略）'),
    (r"(?<![0-9])429(?![0-9])", lambda m: "被限流（请求太密）"),
    (r"(?<![0-9])402(?![0-9])|insufficient|余额", lambda m: "余额/额度不足"),
]


def _zh(key: str) -> str:
    """把英文异常翻成一句人话；翻不了就原样返回。"""
    for pat, fn in _ERR_ZH_PATTERNS:
        m = re.search(pat, key, re.I)
        if m:
            try:
                return fn(m)[:130]
            except Exception:  # noqa: BLE001
                return key
    return key


def _log_errors() -> list[str]:
    """扫最近 N 小时日志里的异常行，去重后返回前 12 条。"""
    since = datetime.now() - timedelta(hours=LOG_WINDOW_HOURS)
    pat = re.compile(r"Traceback|\[ERROR\]|\[CRITICAL\]|SyntaxError|"
                     r"Failed to import|ConnectionError|OOM|CUDA out of memory")
    seen: dict[str, int] = {}
    if not LOGS.exists():
        return []
    ansi = re.compile(r"\[[0-9;]*m")
    for f in LOGS.glob("*.log"):
        try:
            if datetime.fromtimestamp(f.stat().st_mtime) < since - timedelta(hours=2):
                continue
            txt = f.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue
        lines = txt.splitlines()[-4000:]
        for i, ln in enumerate(lines):
            ln = ansi.sub("", ln)          # 彩色日志的转义码会让去重失效
            if not pat.search(ln):
                continue
            # "Traceback (most recent call last):" 本身没信息量，
            # 真正有用的是它下面那行（具体异常类型和消息）
            if "Traceback (most recent call last)" in ln:
                # 栈块的最后一行才是真正的异常（SomeError: message），
                # 中间都是 File/代码行，抓第一个会抓到 sys.exit(main()) 这类没用的
                last, blank = "", 0
                for j in range(i + 1, min(i + 40, len(lines))):
                    cand = ansi.sub("", lines[j]).rstrip()
                    if not cand.strip():
                        blank += 1
                        if blank >= 1 and last:
                            break
                        continue
                    blank = 0
                    last = cand.strip()
                # 只认「像异常」的末行：必须带异常类型/失败词，
                # 排除 ^ ~ 这类定位符、[DEBUG]/[INFO] 日志、以及被截断的残行
                looks = re.search(
                    r"(Error|Exception|Warning|Traceback|失败|未定义|refused|denied|"
                    r"timeout|timed out|out of memory|not defined|No such|Permission)",
                    last or "", re.I)
                if not looks or re.fullmatch(r"[\^~\s\-|]+", last or ""):
                    continue
                key = ("🔻 " + last[:110])
            else:
                key = re.sub(r"^\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s*", "", ln).strip()[:180]
            if not key:
                continue
            # 动态数字（shape / 端口 / 时间）抹成占位符，否则每条都算独立一类
            key = re.sub(r"\d{3,}", "#", key)
            # 白名单：这些是「模型不兼容」的正常警告，不是故障
            #   lora ... shape [x, y]  —— 换底模后 LoRA 权重对不上，ComfyUI 会跳过
            #   未找到 adb / 设备离线 —— 手机不在是常态
            if re.search(r"lora .*shape|unet.*shape|"
                         r"未找到 adb|cannot connect to 1#|"
                         r"CancelledError|warnings\.warn|"
                         r"ComfyUI-Manager.*model-list|"
                         r"信号.*超时时间已到", key, re.I):
                continue
            seen[key] = seen.get(key, 0) + 1
    top = sorted(seen.items(), key=lambda x: -x[1])[:10]
    out = []
    for k, n in top:
        zh = _zh(k)
        out.append(f"{zh}（×{n}）" if n > 1 else zh)
    return out


async def _collect() -> dict:
    down = []
    for name, port, required in SERVICES:
        if port is not None:
            ok = _port_open(port)
        elif name.startswith("NapCat"):
            ok = _proc_running(r"napcatwinboot|napcat\.")
        elif "气泡" in name:
            ok = _proc_running(r"pet_bubble\.py")
        else:
            ok = _proc_running(r"embed_server\.py")
        if not ok and required:
            down.append(name)

    total_gb, free_gb, mem_pct = _mem()
    vram_used, vram_total = _vram()
    disks = _disks()
    errs = await asyncio.to_thread(_log_errors)

    return {
        "down": down,
        "mem": (total_gb, free_gb, mem_pct),
        "vram": (vram_used, vram_total),
        "disks": [(n, u, f) for n, u, f in disks if u >= MAX_DISK_PCT],
        "errors": errs,
    }


def _render(d: dict) -> str:
    """把体检结果写成飞书消息。只写有问题的那几条。"""
    lines: list[str] = []
    now = datetime.now().strftime("%m-%d %H:%M")

    if d["down"]:
        lines.append("🔴 服务挂了：" + "、".join(d["down"]))

    total_gb, free_gb, mem_pct = d["mem"]
    if mem_pct >= MAX_MEM_PCT:
        lines.append(f"🟠 内存 {mem_pct:.0f}%（可用 {free_gb:.1f}G / 共 {total_gb:.0f}G）")

    vu, vt = d["vram"]
    if vt and (vt - vu) < MIN_VRAM_FREE_MB:
        lines.append(f"🟠 显存只剩 {vt - vu} MB（已用 {vu} / {vt}）")

    for name, used_pct, free_g in d["disks"]:
        lines.append(f"🟠 {name} 盘 {used_pct:.0f}% 满（剩 {free_g:.0f}G）")

    if d["errors"]:
        lines.append(f"⚠️ 近 {LOG_WINDOW_HOURS} 小时日志异常 {len(d['errors'])} 类：")
        for e in d["errors"][:8]:
            lines.append("  · " + e)

    if not lines:
        return ""
    return f"🌙 **夜间巡检 {now}**\n" + "\n".join(lines)


async def run_once(force: bool = False) -> dict:
    """跑一遍。有问题推飞书；force=True 时无论有没有问题都推（调试用）。"""
    if os.getenv("NIGHTLY_CHECK", "on").lower() in ("0", "false", "off"):
        return {"skipped": True, "reason": "NIGHTLY_CHECK=off"}

    d = await _collect()
    msg = _render(d)
    pushed = False
    if msg:
        try:
            from plugins import feishu_bridge
            pushed = bool(feishu_bridge.notify_owner(msg))
            if pushed:
                logger.warning(f"[巡检] 发现问题，已推飞书：{len(msg)} 字")
            else:
                logger.warning(f"[巡检] 飞书不可用，结果只落日志：\n{msg}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[巡检] 推送失败：{type(e).__name__}: {e}")
    elif force:
        msg = f"🌙 **夜间巡检 {datetime.now():%m-%d %H:%M}**\n✅ 一切正常"
        try:
            from plugins import feishu_bridge
            pushed = bool(feishu_bridge.notify_owner(msg))
        except Exception:  # noqa: BLE001
            pass

    # 结果留档，面板/排查都能看
    try:
        import json
        (ROOT / "memory" / "nightly_check.json").write_text(
            json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "result": {k: (v if not isinstance(v, tuple) else list(v))
                                   for k, v in d.items()},
                        "pushed": pushed}, ensure_ascii=False, indent=1),
            encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    return {"ok": True, "problems": bool(msg and d), "pushed": pushed, "msg": msg}


def register_schedule() -> None:
    """挂到 nonebot 的定时器上：每天 04:10（避开 04:10 的 trauma_decay？它也是 4:10）。"""
    # 2026-11-06：不用 require()——插件加载时它可能还没 ready，
    # 会报 Cannot load plugin "nonebot_plugin_apscheduler"。
    # schedule.py 一直是直接 import 的，这里跟随同样写法。
    try:
        import nonebot  # noqa: F401
        from nonebot_plugin_apscheduler import scheduler
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[巡检] 定时器不可用：{e}")
        return

    hour = int(os.getenv("NIGHTLY_CHECK_HOUR", "4"))
    minute = int(os.getenv("NIGHTLY_CHECK_MINUTE", "40"))

    @scheduler.scheduled_job("cron", hour=hour, minute=minute,
                             id="nightly_check", misfire_grace_time=7200,
                             coalesce=True)
    async def _job() -> None:
        await run_once()

    logger.info(f"[巡检] 已注册：每天 {hour:02d}:{minute:02d}（有问题才推飞书）")


try:
    register_schedule()
except Exception:  # noqa: BLE001
    pass
