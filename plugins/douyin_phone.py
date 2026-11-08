"""抖音手机自动化：监听「互动消息」里的 @，读视频文案，用大肥鱼人设回复。

依赖：
- 手机已开无线 ADB，且 `adb devices` 能看到；
- 手机停在抖音「互动消息」页（或视频详情页），屏幕常亮；
- 已装 ADBKeyboard（中文输入）。

限制：默认每天最多 2 条、间隔 ≥60 分钟、睡眠时段不回。
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import random
import re
import subprocess
import time
import traceback
import xml.etree.ElementTree as ET
from pathlib import Path

from nonebot import get_bot, logger, on_message
from nonebot.adapters.onebot.v11 import MessageEvent, PrivateMessageEvent
from nonebot_plugin_apscheduler import scheduler

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "memory" / "douyin_phone.json"
ADB = os.getenv("ADB_PATH") or r"C:\Users\Administrator\AppData\Local\Microsoft\WinGet\Packages\Google.PlatformTools_Microsoft.Winget.Source_8wekyb3d8bbwe\platform-tools\adb.exe"
SERIAL = os.getenv("DOUYIN_ADB_SERIAL") or "192.168.1.4:37217"
_SERIAL_CACHE: tuple[str, float] = ("", 0.0)


def _active_serial() -> str:
    """配置的无线调试端口失效时，自动用当前已连接的第一台设备。"""
    global _SERIAL_CACHE
    now = time.time()
    if _SERIAL_CACHE[0] and now - _SERIAL_CACHE[1] < 30:
        return _SERIAL_CACHE[0]
    try:
        out = subprocess.run([ADB, "devices"], capture_output=True, text=True, timeout=10).stdout
        if re.search(re.escape(SERIAL) + r"\s+device", out):
            _SERIAL_CACHE = (SERIAL, now)
            return SERIAL
        m = re.search(r"(?m)^(.+?)\s+device$", out)
        if m:
            _SERIAL_CACHE = (m.group(1).strip(), now)
            return _SERIAL_CACHE[0]
    except Exception as e:  # noqa: BLE001
        _exc("读 adb devices", e)
    return SERIAL


def _device_online() -> bool:
    """adb devices 里有没有一台处于 device 状态的设备。

    2026-11-07：手机没连的时候，原来的 _active_serial() 照样返回配置里的 SERIAL，
    于是整轮流程一路 adb 失败 —— 一天刷 409 条告警，
    而且每一步都是同步 subprocess，把 event loop 冻住，
    连累 apscheduler 24h 错过 781 次任务（心跳/投递队列/B站轮询全被卡）。
    这个函数就是那道闸：没设备就别往下走。
    """
    try:
        out = subprocess.run([ADB, "devices"], capture_output=True, text=True, timeout=8).stdout
        return bool(re.search(r"(?m)^\S+\s+device$", out))
    except Exception as e:  # noqa: BLE001
        _exc("检查设备在线", e)
        return False


OWNER_QQ = "100000000"
OWNER_NAMES = {"枷锁内的示例", "摸西瓜的示例", "示例主人"}
DAILY_LIMIT = int(os.getenv("DOUYIN_PHONE_DAILY", "2"))
MIN_GAP = int(os.getenv("DOUYIN_PHONE_GAP_MIN", "60")) * 60
IME_ADB = "com.android.adbkeyboard/.AdbIME"
# 百度输入法：自动流程已改用 ADB_CLEAR_TEXT 清空，这个常量只留作手工排障（adb shell ime set）用
IME_BAIDU = "com.baidu.input_hihonor/com.baidu.input_honor.ImeService"

# 手势滑动坐标（不是点击锚点）：uiautomator 没有「滚到某节点」的接口，只能用手势。
# 来源 = 1080x2400 屏幕的上下安全区（避开通话栏/导航栏）。
# 方向语义（用户实测）：手指 700→1700 = 往下 = 画面往上（下拉刷新）；手指 1700→700 = 往上 = 画面往下翻。
# 所有手势都必须经过 _swipe()，由它把 direction/y1→y2/触发判据写进 events（t5++ 要求 3）。
SWIPE_REFRESH = (540, 700, 540, 1700, 600)      # 下拉刷新互动消息列表（direction=down）
SWIPE_UNLOCK = (540, 2000, 540, 700, 300)       # 兜底解锁：上滑（direction=up，一轮最多一次）
SWIPE_ALBUM_SCROLL = (540, 1700, 540, 700, 400)  # 相册文件夹列表往下翻（direction=up，只允许相册页内）

# 插表情的概率闸门（0 = 关闭，便于排障时复现「纯文字」路径）
STICKER_RATE = float(os.getenv("DOUYIN_STICKER_RATE", "0.3"))

DOUYIN_PKG = "com.ss.android.ugc.aweme"
# 从 mCurrentFocus / mFocusedApp 行里抽 component（pkg/activity），用于精确判断前台应用
FOCUS_PKG_RE = re.compile(r"\b([A-Za-z][\w.]*)/([\w.$]+)")

RUNS_FILE = ROOT / "logs" / "douyin_runs.jsonl"
ARTIFACT_ROOT = ROOT / "logs" / "douyin_runs"

# ---- 失败码枚举：每轮结束写进 logs/douyin_runs.jsonl 的 failure_code ----
# 一个码只对应一种可判定原因，禁止用一句中文同时盖住
# 「IME 没生效 / 坐标失效 / OCR 没命中 / adb 失败」四种原因（t2 审计 336 行）。
FC_DUMP_FAILED = "dump_failed"                          # uiautomator dump 连续失败，没有节点可判定
FC_NOT_IN_DOUYIN = "not_in_douyin"                      # 前台不是抖音（被其他应用/锁屏顶走）
FC_INTERACTION_PAGE_FAILED = "interaction_page_failed"  # 在抖音里但没导航到「互动消息」列表
FC_NO_TARGET = "no_target"                              # 没有可回条目，或原条目在列表里消失
FC_REPLY_BTN_NOT_FOUND = "reply_btn_not_found"          # 找不到「回复评论」锚点，放弃本轮不盲点
FC_INPUT_NOT_FOUND = "input_not_found"                  # 回复/评论输入框（EditText 节点）没出现
FC_TEXT_NOT_ENTERED = "text_not_entered"                # 清空/输入后截图核对不通过 → 不点发送
FC_SEND_BTN_NOT_FOUND = "send_btn_not_found"            # 找不到「发送」锚点
FC_SEND_UNCONFIRMED = "send_unconfirmed"                # 点了发送但确认不到已发出 → 不写 seen
FC_HIJACKED = "hijacked"                                # 操作途中界面被顶走/页面被切
FC_QUOTA_OR_NIGHT = "quota_or_night"                    # 额度用完、间隔未到、夜间 23-8
FC_CONCURRENT_SKIP = "concurrent_skip"                  # 上一轮还没结束
FC_DEVICE_OFFLINE = "device_offline"                    # 手机没连（没插线 / 没开无线调试）
FC_ROUND_BUDGET = "round_budget"                        # 本轮阻塞超过预算，主动中止
FC_REPLY_GENERATE_FAILED = "reply_generate_failed"      # 生成回复正文失败
FC_DISABLED = "disabled"                                # 插件开关是关的
FC_CANCELLED = "cancelled"                              # 本轮被取消
FC_UNHANDLED = "unhandled_exception"                    # 兜底：未预期异常
# ---- 表情包插入链路的子步骤失败码（t5）：这些码出现在 events 里，插表情失败不拖垮整轮 ----
FC_ALBUM_OPEN_FAILED = "album_open_failed"              # 相册没打开/没进到「所有照片」文件夹列表
FC_STICKER_FOLDER_NOT_FOUND = "sticker_folder_not_found"  # 文件夹列表里找不到目标情绪文件夹（含滚动后）
FC_STICKER_PICK_FAILED = "sticker_pick_failed"          # 进了网格但没选中图片/没回到评论编辑页
FC_STICKER_ATTACH_FAILED = "sticker_attach_failed"      # 插表情失败后连相册都退不出去 → 中止本轮
FC_TARGET_UNAVAILABLE = "target_unavailable"            # 目标 @ 正文不可见/已删（点回复也展不开）

# 目标不可回的判据（t8）：正文为空或命中这些「已删除/不可见」文案就不点回复、不白等
UNAVAILABLE_MARKERS = ("此内容不可见", "内容不可见", "该评论已删除", "评论已删除",
                       "内容已删除", "暂时无法查看", "该内容已失效", "评论已被删除")
# 同一 key 连续失败多少次后放弃（写 seen，避免每轮白烧 60~200s）
RETRY_LIMIT = 3
# 哪几类失败按 key 计入重试（t10 加入 interaction_page_failed）
RETRY_CODES = ("input_not_found", "target_unavailable", "interaction_page_failed")

# 评论输入框的占位文案（F6：只做「整串相等」匹配，不再用宽前缀，避免把真实短文本判成提示）
INPUT_PLACEHOLDERS = ("期待你的评论", "留下你的精彩评论", "留下你的", "说点什么", "善语结善缘",
                      "有爱评论", "写下你的评论", "写下你的", "抢沙发", "友善评论",
                      "有什么想法，展开说说", "有什么想法")


def _looks_like_hint(text: str) -> bool:
    """输入框里读到的是不是「空框提示语」而不是真实文字。

    F6 要求：不用「回复/评论/说点…」这种宽前缀（会把「评论区笑死」这种真实短文本误判成空框），
    改成三种窄判据：
    1) 整串等于已知提示文案（INPUT_PLACEHOLDERS）；
    2) 「回复 @某人」/「回复@某人」形式（回复框专属，带 @）；
    3) 「回复 某人：」这种以冒号结尾、长度 ≤30 的回复框提示。
    """
    t = (text or "").strip()
    if not t:
        return True
    if t in INPUT_PLACEHOLDERS:
        return True
    if t.startswith(("回复 @", "回复@")):
        return True
    return t.startswith("回复") and t.endswith(("：", ":")) and len(t) <= 30

# 同步 adb / 截图 / OCR 累计阻塞秒数：这些调用会冻结 event loop，必须能单独量出来
_BLOCK_S = 0.0
_CUR: "_Round | None" = None
_LAST_XML = ""
# 按 tag 留最近一次 dump 的 XML（F8：插表情失败时把当时的相册 xml 落盘取证）
_XML_BY_TAG: dict[str, str] = {}
# screenrecord 能力探测结果缓存（本机 19:46 那轮报 '/system/bin/sh: screenrecord: inaccessible or not found'）
_SCREENRECORD_OK: "bool | None" = None
# 本轮主动按返回键的累计次数（t9：用于判断「发送后页面是自己回到列表」而不是被我们按回去的）
_BACK_PRESSES = 0


def _add_block(dt: float) -> None:
    global _BLOCK_S
    if dt > 0:
        _BLOCK_S += dt


# 2026-11-07：本轮同步阻塞预算（纯保险，不是优化）。
# 真机实测：一轮 no_target 也要冻 event loop 36 秒（navigate 阶段 29 秒 = 9 次 uiautomator dump），
# 真回一条会更久。这个预算只在「卡死了」时救命，不剪断正常流程。
_ROUND_BLOCK_BUDGET_S = 120.0


class _RoundBudgetExceeded(Exception):
    """本轮同步阻塞超过预算，主动中止，别继续占着 event loop。"""


class _Round:
    """一轮「看抖音」的可观测上下文：阶段耗时、失败码、目标条目、尝试次数、失败留档。"""

    def __init__(self, trigger: str) -> None:
        self.rid = time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid() % 1000:03d}"
        self.trigger = trigger
        self.t0 = time.time()
        self.wall = time.strftime("%Y-%m-%d %H:%M:%S")
        self.phases: list[dict] = []
        self.events: list[dict] = []
        self.outcome: str | None = None      # replied / failed / skipped
        self.failure_code: str | None = None
        self.failure_detail = ""
        self.sender = ""
        self.kind = ""
        self.comment = ""
        self.reply = ""                      # 本轮实际发出的回复正文（成功时）
        self.sticker_reason = ""              # 插表情决策：ok / random_gate / no_sticker_file / …
        self.sticker_file = ""                # 选中的表情文件名（没插则为空）
        self.attempt = 0                     # 文字输入尝试次数
        self.block0 = _BLOCK_S
        self.dump_attempts = 0
        self.dump_ok = 0
        self.last_xml = ""
        self.last_png = b""
        self.artifact_dir = ARTIFACT_ROOT / self.rid
        self.artifacts: list[str] = []


def _note(msg: str, code: str | None = None, level: str = "info") -> None:
    """带 round_id 的日志，同时进本轮 events（最终写进 douyin_runs.jsonl）。"""
    r = _CUR
    rid = r.rid if r is not None else "-"
    head = f"[抖音手机][{rid}]" + (f"[{code}]" if code else "")
    fn = {"info": logger.info, "warning": logger.warning,
          "error": logger.error}.get(level, logger.info)
    fn(f"{head} {msg}")
    if r is not None:
        r.events.append({"t": round(time.time() - r.t0, 1), "lvl": level,
                         "code": code or "", "msg": msg[:300]})


def _exc(stage: str, e: BaseException) -> None:
    """异常不再静默：至少记录阶段 + 异常摘要（合同要求，替掉所有 except: pass）。"""
    _note(f"{stage} 异常：{type(e).__name__}: {e}", level="warning")


def _fail(code: str, msg: str = "") -> None:
    """终局失败：写唯一失败码（本轮不再继续）。"""
    r = _CUR
    if r is not None:
        r.outcome = "failed"
        r.failure_code = code
        r.failure_detail = (msg or code)[:300]
    _note(msg or code, code=code, level="warning")


def _skip(code: str, msg: str) -> None:
    """按边界跳过本轮（额度/夜间/无目标/并发）：结局 skipped + 唯一失败码，不算错误。"""
    r = _CUR
    if r is not None:
        if r.outcome != "replied":
            r.outcome = "skipped"
        if r.failure_code is None:
            r.failure_code = code
        if not r.failure_detail:
            r.failure_detail = msg[:300]
    _note(msg, code=code)


@contextlib.contextmanager
def _phase(name: str):
    """阶段计时：记录本阶段墙钟耗时 dur_ms，以及其中同步 adb/截图/OCR 的阻塞 block_ms。"""
    r = _CUR
    t = time.time()
    b = _BLOCK_S
    try:
        yield
    finally:
        if r is not None:
            r.phases.append({"name": name, "ms": int((time.time() - t) * 1000),
                             "block_ms": int((_BLOCK_S - b) * 1000)})
            used = _BLOCK_S - float(getattr(r, "block0", 0.0) or 0.0)
            if used >= _ROUND_BLOCK_BUDGET_S:
                raise _RoundBudgetExceeded(
                    f"本轮同步阻塞已 {used:.0f}s（预算 {_ROUND_BLOCK_BUDGET_S:.0f}s），中止")


def _write_artifact(r: _Round, name: str, data: bytes) -> str:
    """把失败轮的证据（ui.xml / 截图）落到 logs/douyin_runs/<round_id>/ 下。"""
    if not data:
        return ""
    try:
        r.artifact_dir.mkdir(parents=True, exist_ok=True)
        p = r.artifact_dir / name
        p.write_bytes(data)
        r.artifacts.append(str(p))
        return str(p)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[抖音手机][{r.rid}] 落盘 {name} 失败：{type(e).__name__}: {e}")
        return ""


def _write_cur_artifact(name: str, data: bytes) -> None:
    r = _CUR
    if r is not None and data:
        _write_artifact(r, name, data)


def _write_xml_artifact(name: str, tag: str) -> None:
    """把某个 tag 的最近一次 dump XML 落盘（F8：相册流程失败时留当时的节点树）。"""
    xml = _XML_BY_TAG.get(tag, "")
    if xml:
        _write_cur_artifact(name, xml.encode("utf-8"))


def _finish_round(r: _Round) -> None:
    """收尾：补默认结局、失败轮留 ui.xml/截图、写 logs/douyin_runs.jsonl 一行、打一行摘要。"""
    if r.outcome is None:
        r.outcome = "failed"
        r.failure_code = r.failure_code or FC_UNHANDLED
        r.failure_detail = r.failure_detail or "本轮没有显式结局（有返回路径没写结局）"
    if r.outcome == "failed":
        _write_artifact(r, "ui.xml", r.last_xml.encode("utf-8"))
        _write_artifact(r, "screenshot.png", r.last_png)
    dur_ms = int((time.time() - r.t0) * 1000)
    rec = {
        "ts": r.wall,
        "round_id": r.rid,
        "trigger": r.trigger,
        "serial": _active_serial(),
        "outcome": r.outcome,
        "failure_code": r.failure_code,
        "failure_detail": r.failure_detail,
        "duration_ms": dur_ms,
        "block_ms": int((_BLOCK_S - r.block0) * 1000),
        "target": {"sender": r.sender, "kind": r.kind, "comment": r.comment[:20]},
        "reply": r.reply[:120],
        "sticker": {"reason": r.sticker_reason, "file": r.sticker_file},
        "attempt": r.attempt,
        "dump": {"attempts": r.dump_attempts, "ok": r.dump_ok},
        "phases": r.phases,
        "artifacts": r.artifacts,
        "events": r.events[-80:],
    }
    try:
        RUNS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with RUNS_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[抖音手机][{r.rid}] 写 {RUNS_FILE.name} 失败：{type(e).__name__}: {e}")
    phases = ",".join(f"{p['name']}={p['ms']}ms" for p in r.phases)
    logger.info(
        f"[抖音手机][{r.rid}] 本轮结束 trigger={r.trigger} outcome={r.outcome} "
        f"failure_code={r.failure_code or '-'} dur={dur_ms}ms "
        f"block={int((_BLOCK_S - r.block0) * 1000)}ms sticker={r.sticker_reason or '-'}"
        f"{('/' + r.sticker_file) if r.sticker_file else ''} "
        f"target={r.sender or '-'}｜{r.comment[:20] or '-'} attempt={r.attempt} phases[{phases}]"
    )


def _load() -> dict:
    try:
        d = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except FileNotFoundError:
        # 首次运行还没有 state 文件：正常情况，不是错误
        return {}
    except Exception as e:  # noqa: BLE001
        _exc("读 state 文件", e)
        return {}


def _save(d: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        _exc("写 state 文件", e)


def _today(d: dict) -> dict:
    if d.get("date") != time.strftime("%Y-%m-%d"):
        d["date"] = time.strftime("%Y-%m-%d")
        d["count"] = 0
        d["last_at"] = 0
    d.setdefault("seen", [])
    return d


def _retry_bump(st: dict, key: str) -> int:
    """同一个 key 的连续失败计数 +1（t8：state 里 retry[key]=n，防无限空转）。"""
    retry = st.get("retry")
    if not isinstance(retry, dict):
        retry = {}
    n = int(retry.get(key) or 0) + 1
    retry[key] = n
    if len(retry) > 60:                       # 只留最近 60 个 key，避免状态文件膨胀
        for k in list(retry)[:-50]:
            retry.pop(k, None)
    st["retry"] = retry
    return n


def _retry_clear(st: dict, key: str) -> None:
    """成功后清掉该 key 的失败计数。"""
    retry = st.get("retry")
    if isinstance(retry, dict) and key in retry:
        retry.pop(key, None)
        st["retry"] = retry


def _give_up(st: dict, seen: set, key: str, code: str, n: int) -> None:
    """连续失败到上限：把这条写进 seen，避免下一轮又从头白烧一遍（t8）。"""
    st["seen"] = (list(seen) + [key])[-200:]
    st["force"] = False
    _retry_clear(st, key)
    _save(st)
    _note(f"give_up reason=retry_exhausted key={key} retry={n}/{RETRY_LIMIT} last_code={code}",
          code="retry_exhausted", level="warning")


def _count_failure(st: dict, seen: set, key: str, code: str) -> None:
    """可重试类失败按 key 计数，到上限写 seen 放弃（t8 + t10）。

    t10：把 interaction_page_failed 也纳入 —— 20:51/20:52 两轮就是同一条因为退不回互动消息
    白跑了两次；现在连续 3 次同样失败就放弃该条，不再无限空转。
    """
    if code not in RETRY_CODES:
        return
    n = _retry_bump(st, key)
    _note(f"retry={n}/{RETRY_LIMIT} key={key} last_code={code}", code="retry_count")
    if n >= RETRY_LIMIT:
        _give_up(st, seen, key, code, n)


def _target_unavailable_reason(it: dict) -> str:
    """目标是否「不可回」（t8）：正文为空，或显示已删除/不可见。

    实跑证据（round 20261025-203250）：comment='此内容不可见'，点「回复评论」后评论区根本不展开 →
    白等 8s 还判 input_not_found，然后不写 seen，下一轮又重来一遍。
    """
    text = (it.get("text") or "").strip()
    desc = (it.get("desc") or "").strip()
    for marker in UNAVAILABLE_MARKERS:
        if marker in text or marker in desc:
            return f"marker:{marker}"
    if not text:
        return "empty_comment"
    return ""


def _adb(*args: str, timeout: int = 10, tag: str = "") -> str:
    """跑一条 adb 命令，返回 stdout。

    失败不再静默：异常（超时/路径错）记 tag + 异常摘要；非 0 退出码记 tag + stderr 摘要
    （uiautomator dump 拿不到 idle state 就是靠这个才看得见）。返回值仍为空串，
    调用方必须按结果判定，不许把空返回值当成功。
    """
    t = time.time()
    name = tag or (args[0] if args else "adb")
    try:
        p = subprocess.run([ADB, "-s", _active_serial(), *args], capture_output=True, text=True,
                           encoding="utf-8", errors="ignore", timeout=timeout)
        out = (p.stdout or "").strip()
        err = (p.stderr or "").strip()
        if p.returncode != 0:
            _note(f"adb {name} exit={p.returncode} err={err[:160]!r}")
        return out
    except Exception as e:  # noqa: BLE001
        _note(f"adb {name} 异常：{type(e).__name__}: {e}", level="warning")
        return ""
    finally:
        _add_block(time.time() - t)


def _dump(tag: str = "") -> ET.Element | None:
    """uiautomator dump + 解析节点树，最多 3 次（每次失败等 1.2s 让界面静下来）。

    返回 None = 三次都没读到；调用方必须按 dump_failed 放弃本轮。
    旧实现失败时会盲点 (540,700) 想暂停视频 —— 盲点坐标已按合同删除（t2 根因），
    改成「重试 + 明确失败码」。
    """
    global _LAST_XML
    r = _CUR
    last_err = ""
    for attempt in range(3):
        if r is not None:
            r.dump_attempts += 1
        _adb("shell", "uiautomator", "dump", "--compressed", "/sdcard/dsh_ui.xml",
             timeout=15, tag="uiautomator_dump")
        xml = _adb("exec-out", "cat", "/sdcard/dsh_ui.xml", timeout=15, tag="dump_pull")
        try:
            root = ET.fromstring(xml)
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e} xml_len={len(xml)}"
            _note(f"dump 第 {attempt + 1}/3 次解析失败：{last_err}")
            if attempt < 2:
                time.sleep(1.2)
            continue
        _LAST_XML = xml
        if r is not None:
            r.dump_ok += 1
            r.last_xml = xml
        if tag:
            # F8：按 tag 留一份 XML，相册流程失败时能落盘取证
            _XML_BY_TAG[tag] = xml
            if len(_XML_BY_TAG) > 40:
                for k in list(_XML_BY_TAG)[:-30]:
                    _XML_BY_TAG.pop(k, None)
            _note(f"dump ok tag={tag} nodes={sum(1 for _ in _nodes(root))}")
        return root
    if r is not None and not r.last_xml:
        r.last_xml = _LAST_XML
    _note(f"dump 连续 3 次失败：{last_err}", level="warning")
    return None


def _find_node(root: ET.Element, pred) -> ET.Element | None:
    """按谓词找第一个节点（只读，不做任何点击）。"""
    for n in _nodes(root):
        try:
            if pred(n):
                return n
        except Exception as e:  # noqa: BLE001
            _note(f"节点判定异常：{type(e).__name__}: {e}", level="warning")
    return None


def _wait_node(pred, timeout: float = 10.0, interval: float = 0.7,
               desc: str = "") -> ET.Element | None:
    """轮询 dump 直到出现满足 pred 的节点。

    超时值写在调用处（每个调用点的注释里标了 timeout 和放弃条件）。
    超时返回 None，调用方必须按失败码放弃本轮，绝不拿兜底坐标硬点。
    """
    deadline = time.time() + timeout
    while True:
        root = _dump(desc)
        if root is not None:
            node = _find_node(root, pred)
            if node is not None:
                return node
        if time.time() >= deadline:
            return None
        time.sleep(interval)


def _wait_ocr(pred, timeout: float = 6.0, interval: float = 1.0,
              desc: str = "") -> tuple[int, int] | None:
    """OCR 兜底等待：只在整屏 OCR 文字里找锚点，超时返回 None（调用方放弃本轮）。

    OCR 只做节点找不到时的兜底，且必须按文字精确锚定，禁止「取最近的一个」乱点。
    """
    deadline = time.time() + timeout
    while True:
        for t, c in _ocr_screen():
            try:
                if pred(t):
                    return c
            except Exception as e:  # noqa: BLE001
                _note(f"等待 {desc} 判定异常：{type(e).__name__}: {e}", level="warning")
        if time.time() >= deadline:
            return None
        time.sleep(interval)


def _screen_h(root: ET.Element) -> int:
    """屏幕高度：优先用根节点 bounds，取子节点最大 y 兜底。"""
    c = _center(root.get("bounds") or "")
    if c:
        return max(c[1] * 2, 1)
    hy = 0
    for n in _nodes(root):
        m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", n.get("bounds") or "")
        if m:
            hy = max(hy, int(m.group(4)))
    return hy or 2400


def _is_interaction_text(text: str) -> bool:
    return any(k in text for k in ("提到了你", "评论了你", "回复:", "回复："))


def _is_interaction_list(root: ET.Element) -> bool:
    """当前页是不是「互动消息」列表（按 content-desc 判定，不看标题文字）。"""
    for n in _nodes(root):
        if _is_interaction_text(n.get("content-desc") or ""):
            return True
    return False


def _is_interaction_node(n: ET.Element) -> bool:
    return _is_interaction_text((n.get("content-desc") or "") + (n.get("text") or ""))


def _nodes(root: ET.Element):
    for n in root.iter("node"):
        yield n


def _center(bounds: str) -> tuple[int, int] | None:
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds or "")
    if not m:
        return None
    x1, y1, x2, y2 = map(int, m.groups())
    return (x1 + x2) // 2, (y1 + y2) // 2


def _tap(x: int, y: int, source: str = "uiautomator 节点 bounds") -> None:
    """点一个坐标。坐标只能来自 uiautomator 节点 bounds，或 OCR 文字锚点（source 注明来源）。"""
    _note(f"tap {x},{y} 来源={source}")
    _adb("shell", "input", "tap", str(x), str(y), tag="tap")


def _foreground_package() -> str:
    """当前前台包名：只解析 dumpsys window 的 mCurrentFocus / mFocusedApp 行里的 component。

    实机格式：`mCurrentFocus=Window{1e82656 u0 com.ss.android.ugc.aweme/com.ss.android.ugc.aweme
    .socialnotice.view.activity.IntegratedInteractionActivity}`。
    旧实现拿整份 dumpsys 做子串匹配，会让「SplashActivity」这种常驻字样误判（t5++ 要求 4）。
    """
    raw = _adb("shell", "dumpsys", "window", timeout=10, tag="dumpsys_window")
    for ln in raw.splitlines():
        if "mCurrentFocus" in ln or "mFocusedApp" in ln:
            m = FOCUS_PKG_RE.search(ln)
            if m:
                return m.group(1)
    return ""


def _foreground_activity() -> str:
    """当前前台 activity（同一行里的 component 后段），用于区分三种页面（t10）：

    - `...detail.ui.DetailActivity` → 视频详情页（评论面板可能展开，要连按返回才能退）；
    - `...socialnotice.view.activity.IntegratedInteractionActivity` → 互动消息（目标页）；
    - `...main.MainActivity` → 抖音首页。
    """
    raw = _adb("shell", "dumpsys", "window", timeout=10, tag="dumpsys_window")
    for ln in raw.splitlines():
        if "mCurrentFocus" in ln or "mFocusedApp" in ln:
            m = FOCUS_PKG_RE.search(ln)
            if m:
                return m.group(2)
    return ""


def _is_interaction_page(root: ET.Element) -> bool:
    """导航用判据：已经是「互动消息」页（t10）。

    `_is_interaction_list` 只认条目 desc（提到了你/评论了你…）；空列表或条目文案不同时会漏判，
    所以这里并上 `_is_returned_to_list`（≥2 个「回复评论」按钮 / 「互动消息」标题）。
    """
    return _is_interaction_list(root) or _is_returned_to_list(root)


def _is_detail_page() -> bool:
    """当前前台是不是视频详情页（t10：DetailActivity，评论面板可能展开）。"""
    act = _foreground_activity().lower()
    return "detailactivity" in act or "detail.ui" in act


def _is_comment_panel_open(root: ET.Element) -> bool:
    """详情页底部评论面板是否展开（t10）：输入框提示是「有什么想法，展开说说」/「说点什么」。

    注意与「回复 @某人：」的回复编辑器区分——后者不算详情页信号，
    否则 _ensure_interaction_page 会把正在编辑的回复框当成详情页按返回按掉。
    """
    node = _find_reply_input(root)
    if node is None:
        return False
    t = (node.get("text") or "").strip()
    return t.startswith(("有什么想法", "说点什么"))


def _back_out_of_detail() -> bool:
    """从「视频详情页 + 评论面板」连按返回，直到出现互动消息列表（t10）。

    实跑证据（20:51/20:52 两轮）：点开条目进 DetailActivity 且底部评论面板展开时，
    第 1、2 次 KEYCODE_BACK 仍在 DetailActivity（先关面板、再退视频页），第 3 次才回到
    IntegratedInteractionActivity；而旧导航把它当「未知页面」直接放弃 → interaction_page_failed。
    规则：只按返回键（最多 4 次，每次 1.2s 后重新 dump 判定），**绝不用上滑/下滑手势去猜**。
    """
    for i in range(1, 5):
        root = _dump(f"back_detail#{i}")
        if root is not None and _is_interaction_page(root):
            _note(f"导航：连按返回 #{i - 1} 后已回到互动消息")
            return True
        act = _foreground_activity()
        _note(f"导航：检测到视频详情页/评论面板（activity={act or '-'}）→ 连按返回 #{i}")
        _back_gesture()
        time.sleep(1.2)
    root = _dump("back_detail_final")
    if root is not None and _is_interaction_page(root):
        _note("导航：连按返回 #4 后已回到互动消息")
        return True
    _note("导航：连按返回 4 次仍未回到互动消息", level="warning")
    return False


def _swipe(name: str, reason: str) -> None:
    """执行一次手势并把 direction / y1→y2 / 触发判据写进 events（t5++ 要求 3）。

    以前手势是静默的：用户看到「列表被划走」但日志里什么都没有，无法自证是谁划的。
    name 取 refresh / unlock / album。
    """
    coords = {"refresh": SWIPE_REFRESH, "unlock": SWIPE_UNLOCK, "album": SWIPE_ALBUM_SCROLL}.get(name)
    if coords is None:
        _note(f"未知手势 {name}，拒绝执行", level="warning")
        return
    x1, y1, x2, y2, dur = coords
    direction = "down" if y2 > y1 else "up"
    _note(f"gesture swipe_{name} direction={direction} {x1},{y1}→{x2},{y2} dur={dur}ms "
          f"reason={reason}")
    _adb("shell", "input", "swipe", *(str(v) for v in coords), tag=f"swipe_{name}")


def _needs_unlock() -> tuple[bool, str]:
    """是否真的需要解锁（t5++ 要求 1）。

    只认两种硬证据：
    1) `dumpsys power` 的 `mWakefulness=Asleep/Doze`（屏幕确实熄了）；
    2) `dumpsys window policy` 里 KeyguardServiceDelegate 的 `showing=true`，或
       KeyguardStateMonitor 的 `mIsShowing=true`（锁屏确实盖着）。
    荣耀机型未锁屏时也可能出现 `isStatusBarKeyguard=true`，所以这个字段不再参与判断——
    之前就是它导致在互动消息列表中间误上滑，把列表划走。
    """
    power = _adb("shell", "dumpsys", "power", timeout=10, tag="dumpsys_power")
    for line in power.splitlines():
        s = line.strip()
        if s.startswith("mWakefulness=") and s.split("=", 1)[1].strip() in ("Asleep", "Doze", "Dreaming"):
            return True, f"power {s}"
    policy = _adb("shell", "dumpsys", "window", "policy", timeout=10, tag="dumpsys_window_policy")
    section = ""
    for line in policy.splitlines():
        s = line.strip()
        if s in ("KeyguardServiceDelegate", "KeyguardStateMonitor"):
            section = s
            continue
        if section == "KeyguardServiceDelegate" and s in ("showing=true",):
            return True, f"policy {section} {s}"
        if section == "KeyguardStateMonitor" and s in ("mIsShowing=true",):
            return True, f"policy {section} {s}"
        if s in ("showing=false", "mIsShowing=false"):
            section = ""
    return False, "power=awake & keyguard showing=false"


def _wait_pkg(pkg: str, timeout: float = 5.0) -> str:
    """轮询前台包名直到等于 pkg；超时返回最后一次读到的包名。"""
    deadline = time.time() + timeout
    last = ""
    while True:
        last = _foreground_package()
        if last == pkg:
            return last
        if time.time() >= deadline:
            return last
        time.sleep(0.7)


def _in_douyin() -> bool:
    """前台是不是抖音：只解析 mCurrentFocus 里的 component 包名，不拿整份 dumpsys 文本猜。"""
    pkg = _foreground_package()
    ok = pkg == DOUYIN_PKG
    if not ok:
        _note(f"当前前台不是抖音：pkg={pkg!r}")
    return ok


def _back_gesture() -> None:
    """系统返回键（三键导航/手势导航都适用）。

    t9：累计按下次数，供「发送后页面自己回到互动列表」这条正面证据判断
    「本轮有没有主动按过返回键」（按过就不算页面自己退出的）。
    """
    global _BACK_PRESSES
    _BACK_PRESSES += 1
    _adb("shell", "input", "keyevent", "4", tag="back")


def _clear_activity() -> None:
    """手机没连上 / 没进抖音时，把状态栏那条「在玩手机上抖音」撤掉，别挂着假状态。"""
    try:
        from plugins.activity import set_activity

        set_activity("", 0)
    except Exception as e:  # noqa: BLE001
        _exc("清理活动状态", e)


def _wake() -> None:
    _adb("shell", "input", "keyevent", "224", tag="wakeup")  # WAKEUP
    _adb("shell", "wm", "dismiss-keyguard", tag="dismiss_keyguard")


def _set_ime(name: str) -> bool:
    """切换输入法并回读校验（settings get secure default_input_method，最多等 6s）。

    返回 False = 没切过去。旧实现忽略 _set_ime 返回值，导致「AdbIME 没生效」在日志里
    和「坐标失效/adb 失败」混成同一句（t2 根因 312-321），现在这一跳可判定。
    """
    _adb("shell", "ime", "set", name, timeout=10, tag="ime_set")
    pkg = name.split("/")[0]
    deadline = time.time() + 6
    cur = ""
    while True:
        cur = _adb("shell", "settings", "get", "secure", "default_input_method",
                   timeout=10, tag="ime_get")
        if pkg in cur:
            _note(f"输入法已切到 {name}")
            return True
        if time.time() >= deadline:
            break
        time.sleep(0.7)
    _note(f"输入法没切到 {name}（回读 {cur[:60]!r}）", level="warning")
    return False


def _find_reply_input(root: ET.Element) -> ET.Element | None:
    """回复输入框（F2 严格判据）：EditText 且不是搜索/相册挑选框。

    相册预览页、相册搜索页也有输入框和「发送」按钮，所以不能只认「任意 EditText」
    或「任意 text/desc==发送」；这里统一由本函数判定，供 _is_comment_edit_page /
    _read_input_text / _reply_body_present 共用。
    """
    for n in _nodes(root):
        if (n.get("class") or "") != "android.widget.EditText":
            continue
        blob = ((n.get("content-desc") or "") + " " + (n.get("text") or "")).strip()
        if "搜索" in blob or "搜一搜" in blob:
            continue
        return n
    return None


def _read_input_text() -> str | None:
    """读回复输入框的 text（F5）。返回 "" = 空框；文本 = 真实内容；None = **未知**。

    None 的两种来源：dump 拿不到节点 / 页面上没有回复用 EditText。
    调用方绝不能把 None 当成「空框」（否则会误判清空成功 → 旧文字+新文字拼成两句）。
    """
    root = _dump("edittext")
    if root is None:
        _note("读输入框：dump 拿不到节点（未知，不当空框）",
              code="input_text_unknown_dump_failed", level="warning")
        return None
    node = _find_reply_input(root)
    if node is None:
        _note("读输入框：页面上没有回复 EditText（未知，不当空框）",
              code="input_text_unknown_no_edittext", level="warning")
        return None
    return (node.get("text") or "").strip()


def _wait_input_empty(timeout: float = 3.0) -> tuple[bool, str]:
    """轮询输入框文本直到空/只剩提示语；读到「未知」不算清空（F5）。"""
    deadline = time.time() + timeout
    text: str | None = ""
    while True:
        text = _read_input_text()
        if text is not None and _looks_like_hint(text):
            return True, f"text={text[:20]!r}"
        if time.time() >= deadline:
            return False, ("text=未知（dump/EditText 读不到）" if text is None
                           else f"text={text[:40]!r}")
        time.sleep(0.5)


def _reply_body_present(text: str) -> tuple[bool, str]:
    """F3：确认回复正文仍在输入框里（退相册/插表情之后、点发送之前必须过这一关）。

    要求 EditText 文本非空、不是提示语、且包含正文前 6 字（归一化后比较）。
    任何一条不满足都返回 False —— 调用方按 text_not_entered 中止，不点发送。
    """
    root = _dump("body_check")
    if root is None:
        return False, "dump 失败，无法确认正文"
    node = _find_reply_input(root)
    if node is None:
        return False, "页面上没有回复输入框（可能已不在编辑页）"
    cur = (node.get("text") or "").strip()
    if not cur or _looks_like_hint(cur):
        return False, f"输入框为空/只有提示语 text={cur[:30]!r}"
    probe = re.sub(r"[^\w\u4e00-\u9fff]", "", text)[:6]
    if probe and probe not in re.sub(r"[^\w\u4e00-\u9fff]", "", cur):
        return False, f"输入框内容和正文对不上 text={cur[:30]!r} probe={probe!r}"
    return True, f"text={cur[:30]!r}"


def _input_edge_point(bounds: str) -> tuple[int, int] | None:
    """EditText 右边缘内缩 20px、纵向居中（F7：把光标送到文本末尾）。"""
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds or "")
    if not m:
        return None
    x1, y1, x2, y2 = map(int, m.groups())
    return max(x1, x2 - 20), (y1 + y2) // 2


def _clear_input(bounds: str) -> tuple[bool, str]:
    """清空回复输入框（F7）：点 EditText **右边缘**（光标到末尾）→ ADB_CLEAR_TEXT → 回读确认空；
    不行再点右边缘 + 最多 120 次 DEL（一次 adb 带多个 keycode）→ 再回读；
    最后再补一次 ADB_CLEAR_TEXT。

    为什么点右边缘而不是中心：点中心时光标可能落在文字中段，DEL 只删前半截，
    剩下的后半截再拼上新正文 → 重复两句话（F7 原问题）。
    不发 MOVE_END(123)：123 是全局按键，输入框丢焦点时作用在列表上会「滚到底」（用户实测的列表被划走）。
    """
    if not _set_ime(IME_ADB):
        return False, "ADBKeyboard 未生效，ADB_CLEAR_TEXT 不可用"
    edge = _input_edge_point(bounds)
    if edge is None:
        return False, f"EditText bounds 解析失败：{bounds!r}"
    _tap(*edge, "uiautomator EditText 右边缘（光标送到末尾）")
    _adb("shell", "am", "broadcast", "-p", "com.android.adbkeyboard",
         "-a", "ADB_CLEAR_TEXT", timeout=15, tag="adb_clear_text")
    ok, how = _wait_input_empty(3.0)
    if ok:
        return True, f"ADB_CLEAR_TEXT {how}"
    # 兜底（F7）：再点右边缘确保光标在末尾，再连按最多 120 次退格
    _tap(*edge, "清空兜底：再点 EditText 右边缘（光标到末尾）")
    _adb("shell", "input", "keyevent", *(["67"] * 120), timeout=40, tag="del_x120")
    ok2, how2 = _wait_input_empty(3.0)
    if ok2:
        return True, f"del_x120 {how2}"
    # 最后再补一次 ADB_CLEAR_TEXT（F7：DEL 兜底之后回读 ADB_CLEAR_TEXT 结果）
    _adb("shell", "am", "broadcast", "-p", "com.android.adbkeyboard",
         "-a", "ADB_CLEAR_TEXT", timeout=15, tag="adb_clear_text_retry")
    ok3, how3 = _wait_input_empty(2.0)
    if ok3:
        return True, f"ADB_CLEAR_TEXT(retry) {how3}"
    _note(f"输入框清不干净：{how} / {how2} / {how3}", level="warning")
    return False, f"{how}; {how2}; {how3}"


def _shquote(s: str) -> str:
    """POSIX 单引号转义：把任意文本变成**一个** shell 单词。

    为什么需要：`adb shell <a> <b> ...` 会把参数拼成一条命令行交给远端 /system/bin/sh，
    不是独立 argv —— 回复里的 ASCII 括号/空格/&/;/|/$ 会被 sh 解析
    （20:10 实跑：`/system/bin/sh: syntax error: unexpected '('`）。F11 根因。
    """
    return "'" + str(s).replace("'", "'\\''") + "'"


def _type_text(text: str) -> bool:
    """把整句写进已聚焦的输入框（F11 blocker）。

    主通道 ADB_INPUT_B64：AdbIME 原生支持，载荷是 base64（只含 [A-Za-z0-9+/=]），
    连空格/括号/引号/`&;|$` 都不进 shell 解析。
    兜底 ADB_INPUT_TEXT + `_shquote`（单引号转义），保证文本只被 sh 解析一次。
    两条通道都查 "Broadcast completed" 回执，events 里能看到用的是哪条。
    """
    payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
    out = _adb("shell", "am", "broadcast", "-p", "com.android.adbkeyboard",
               "-a", "ADB_INPUT_B64", "--es", "msg", payload, timeout=20, tag="adb_input_b64")
    delivered = "Broadcast completed" in out
    _note(f"输入通道=ADB_INPUT_B64 b64_len={len(payload)} delivered={delivered} out={out[-80:]!r}")
    if delivered:
        return True
    quoted = _shquote(text)
    out2 = _adb("shell", "am", "broadcast", "-p", "com.android.adbkeyboard",
                "-a", "ADB_INPUT_TEXT", "--es", "msg", quoted, timeout=20,
                tag="adb_input_text_quoted")
    delivered2 = "Broadcast completed" in out2
    _note(f"输入通道=ADB_INPUT_TEXT(单引号转义) delivered={delivered2} out={out2[-100:]!r}")
    return delivered2


def _screencap() -> bytes:
    """截图 PNG（失败轮留档用；同时累计同步阻塞时间）。"""
    t = time.time()
    try:
        p = subprocess.run([ADB, "-s", _active_serial(), "exec-out", "screencap", "-p"],
                           capture_output=True, timeout=20)
        data = p.stdout or b""
        if data.startswith(b"\x89PNG"):
            if _CUR is not None:
                _CUR.last_png = data
            return data
        _note(f"截图不是 PNG（len={len(data)}）", level="warning")
        return b""
    except Exception as e:  # noqa: BLE001
        _exc("截图", e)
        return b""
    finally:
        _add_block(time.time() - t)


def _ocr_from_png(img: bytes) -> list[tuple[str, tuple[int, int]]]:
    """对一张 PNG 做本地 OCR，返回 [(文字, 中心坐标)]。OCR 不可用时返回 []。"""
    if not img:
        return []
    try:
        import io

        import numpy as np
        from PIL import Image
        from rapidocr_onnxruntime import RapidOCR
    except Exception as e:  # noqa: BLE001
        _exc("加载 OCR 依赖", e)
        return []
    t = time.time()
    try:
        arr = np.array(Image.open(io.BytesIO(img)).convert("RGB"))
        res, _ = RapidOCR()(arr)
        out = []
        for box, text, _score in (res or []):
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            out.append((str(text), (int((min(xs) + max(xs)) / 2),
                                    int((min(ys) + max(ys)) / 2))))
        return out
    except Exception as e:  # noqa: BLE001
        _exc("OCR 识别", e)
        return []
    finally:
        _add_block(time.time() - t)


def _ocr_screen() -> list[tuple[str, tuple[int, int]]]:
    """截屏 + OCR。候选数为 0 时记日志（t1 缺口：OCR 候选数/坐标/probe 命中要能追溯）。"""
    found = _ocr_from_png(_screencap())
    if not found:
        _note("OCR 没有候选文字（截图失败或 rapidocr 不可用）", level="warning")
    return found


def _verify_typed(text: str, attempt: int = 0) -> tuple[bool, str]:
    """截图核对：输入框里确实是这句回复（OCR 文字锚点，不点任何坐标）。

    回复正文可能比输入框宽，输入后输入框会滚到尾部，所以头部/尾部各取 6 字做 probe。
    输入法是异步提交的，所以最多截两次（第二次前等 1.2s），避免「刚广播完就判失败」。
    OCR 不可用（没装 rapidocr / 截图失败）时退回读 EditText 节点文本，并在日志里标明用了兜底判定。
    核对失败不点发送、不记 seen，保留到下一轮重试（合同要求）。
    """
    norm = re.sub(r"[^\w\u4e00-\u9fff]", "", text)
    probes = [p for p in (norm[:6], norm[-6:]) if p]
    png = b""
    found: list[tuple[str, tuple[int, int]]] = []
    for shot in (1, 2):
        png = _screencap()
        found = _ocr_from_png(png) if png else []
        if found:
            joined = "".join(re.sub(r"[^\w\u4e00-\u9fff]", "", t) for t, _ in found)
            hit = next((p for p in probes if p in joined), "")
            if hit:
                return True, f"ocr probes={probes} hit={hit!r} cands={len(found)} shot={shot}"
        if shot == 1:
            time.sleep(1.2)
    if found:
        _write_cur_artifact(f"input_verify_{attempt or 1}.png", png)
        return False, f"ocr probes={probes} 两次截图均未命中 cands={len(found)}"
    node_text = _read_input_text()
    if node_text is None:
        _write_cur_artifact(f"input_verify_{attempt or 1}.png", png)
        return False, "OCR 不可用且输入框文本读不到（未知，不当成功）"
    node_norm = re.sub(r"[^\w\u4e00-\u9fff]", "", node_text)
    hit = next((p for p in probes if p in node_norm), "")
    _write_cur_artifact(f"input_verify_{attempt or 1}.png", png)
    return bool(hit), f"OCR 不可用，退回节点文本 text={node_text[:40]!r} hit={hit!r}"


def _enter_text(text: str) -> bool:
    """清空 → 点输入框（节点 bounds）→ 输入 → 截图核对，最多 2 次尝试。

    每一次尝试都重新定位 EditText 节点：键盘弹出后布局会变，旧坐标会失效（t2 根因 312-321）。
    核对不通过就清空重试；两次都不通过 → text_not_entered，本轮不点发送、不记 seen。
    """
    r = _CUR
    for attempt in (1, 2):
        if r is not None:
            r.attempt = attempt
        # 等输入框出现：timeout=6s，超时即 input_not_found（不盲点任何坐标）
        node = _wait_node(lambda n: n.get("class") == "android.widget.EditText",
                          timeout=6.0, desc="评论输入框")
        if node is None:
            _fail(FC_INPUT_NOT_FOUND, f"第 {attempt} 次尝试：dump 里没有 EditText 输入框节点")
            return False
        c = _center(node.get("bounds"))
        if not c:
            _fail(FC_INPUT_NOT_FOUND, "输入框节点的 bounds 解析失败")
            return False
        if not _set_ime(IME_ADB):
            _note("输入法没切到 ADBKeyboard，先继续，靠截图核对判定", level="warning")
        cleared, how = _clear_input(node.get("bounds") or "")
        _note(f"清空输入框 attempt={attempt} ok={cleared} ({how})")
        if not cleared:
            if attempt == 2:
                _fail(FC_TEXT_NOT_ENTERED, f"输入框清不干净，本轮不发送：{how}")
                return False
            continue
        # 清空后重新取一次输入框 bounds（键盘弹出/面板上移后旧坐标可能失效）
        node2 = _wait_node(lambda n: n.get("class") == "android.widget.EditText",
                           timeout=4.0, desc="输入框(键盘弹出后)") or node
        c2 = _center(node2.get("bounds")) or c
        _tap(*c2, source="uiautomator EditText 节点 bounds（键盘弹出后重新取）")
        delivered = _type_text(text)
        ok, how2 = _verify_typed(text, attempt)
        _note(f"输入核对 attempt={attempt} ok={ok} delivered={delivered} {how2}")
        if ok:
            return True
        # 核对失败：先清空，绝不点发送
        _clear_input(node2.get("bounds") or "")
    _fail(FC_TEXT_NOT_ENTERED, "两次输入后截图核对都没看到回复正文，本轮不发送")
    return False


def _is_returned_to_list(root: ET.Element) -> bool:
    """页面是否已经回到「互动消息」列表（t9 正面证据用）。

    判据：出现「互动消息」标题，或者出现 ≥2 个「回复评论」按钮（列表每条都有一个）。
    """
    reply_btns = 0
    for n in _nodes(root):
        label = (n.get("text") or "").strip() or (n.get("content-desc") or "").strip()
        if label == "回复评论":
            reply_btns += 1
        blob = (n.get("text") or "") + " " + (n.get("content-desc") or "")
        if "互动消息" in blob:
            return True
    return reply_btns >= 2


def _confirm_sent(text: str, timeout: float = 12.0, box_top: int | None = None,
                  from_edit_page: bool = False,
                  back_presses_at_send: int | None = None) -> tuple[bool, str, str]:
    """发送后轮询确认真的发出去了（最多 12s）——**只认正面证据**（F4 + t9）。

    正面证据（任一命中即成功，返回 (True, source, detail)）：
    1) source=list_node_text：回复正文出现在「非输入框」的节点文本里（评论区/互动列表）；
    2) source=toast：「评论成功/发布成功」这类系统成功回执；
    3) source=returned_to_interaction_list（t9，四条件同时满足）：
       a. 点发送前确实在编辑页（有回复 EditText，由调用方传入 from_edit_page）；
       b. 点发送后 12s 内页面变成互动消息列表（_is_returned_to_list）；
       c. 输入框消失；
       d. 本轮自点发送起没有再按过返回键（比对 _BACK_PRESSES 快照）。
       实跑证据：20:35 那轮点发送 3s 后页面已回到互动列表（after_send.png），
       但旧判据只认「正文上屏/toast/输入框上方 OCR」→ 判 send_unconfirmed → 不写 seen → 下轮重复回复。
    4) source=ocr_above_box：截屏 OCR 在输入框上方（y < box_top-30）识别到正文。
    仍然**不**使用「无 EditText 且无发送按钮=成功」这种否定式判据（F4 已堵掉）。
    拿不到任何正面证据 → (False, "", detail)，调用方记 send_unconfirmed，不写 seen。
    """
    norm = re.sub(r"[^\w\u4e00-\u9fff]", "", text)
    probes = [p for p in (norm[:6], norm[-6:]) if p]
    deadline = time.time() + timeout
    edit_text = ""
    ocr_seen = 0
    back_at_send = _BACK_PRESSES if back_presses_at_send is None else back_presses_at_send
    while True:
        root = _dump("confirm_sent")
        if root is not None:
            page = ""
            edit_text = ""
            for n in _nodes(root):
                if (n.get("class") or "") == "android.widget.EditText":
                    edit_text = (n.get("text") or "").strip()
                    continue
                page += re.sub(r"[^\w\u4e00-\u9fff]", "", n.get("text") or "")
                page += re.sub(r"[^\w\u4e00-\u9fff]", "", n.get("content-desc") or "")
            hit = next((p for p in probes if p in page), "")
            if hit:
                where = "互动消息列表" if _is_interaction_list(root) else "评论区节点"
                return True, "list_node_text", f"正面证据：{where}里出现回复正文 hit={hit!r}"
            # 证据 2：抖音「评论成功/发布成功」这类成功 toast（正面的系统回执）
            toast = next((k for k in ("评论成功", "发布成功", "回复成功", "评论已发布")
                          if k in page), "")
            if toast:
                return True, "toast", f"正面证据：出现成功提示「{toast}」"
            # 证据 3（t9）：点发送后页面自己回到互动消息列表（四条件）
            if from_edit_page and _BACK_PRESSES == back_at_send \
                    and _find_reply_input(root) is None and _is_returned_to_list(root):
                return True, "returned_to_interaction_list", (
                    "正面证据：输入框已消失且页面回到互动消息列表"
                    f"（发送前在编辑页=True，发送后返回键次数={_BACK_PRESSES - back_at_send}）")
            # 证据 4：OCR 在输入框上方识别到正文（输入框内自己也会被 OCR 到，所以按 y 过滤）
            found = _ocr_screen()
            ocr_seen = len(found)
            for t, c in found:
                tn = re.sub(r"[^\w\u4e00-\u9fff]", "", t)
                if box_top is not None and c[1] >= box_top - 30:
                    continue
                h2 = next((p for p in probes if p in tn), "")
                if h2:
                    return True, "ocr_above_box", (f"正面证据：输入框上方 OCR 命中 hit={h2!r} "
                                                   f"y={c[1]} box_top={box_top}")
        if time.time() >= deadline:
            return False, "", (f"12s 内没有任何正面证据（正文没上屏、没 toast、也没回列表；"
                               f"back_delta={_BACK_PRESSES - back_at_send}）"
                               f" edit={edit_text[:20]!r} ocr_cands={ocr_seen}")
        time.sleep(1.0)


def _send_and_confirm(text: str) -> bool:
    """点「发送」并确认发出。

    点击来源：uiautomator「发送」节点 bounds；节点找不到才用 OCR「发送」文字锚点兜底；
    两处都找不到 → send_btn_not_found，本轮放弃（不记 seen）。
    点了但确认不到 → send_unconfirmed，同样不记 seen。
    t9：发送前记下「确实在编辑页 + 当时的返回键计数」，供「页面自己回到列表」这条正面证据使用。
    """
    node = _wait_node(
        lambda n: (n.get("text") or "").strip() == "发送"
        or (n.get("content-desc") or "").strip() == "发送",
        timeout=6.0, desc="发送按钮")
    # 记下输入框上沿（OCR 证据用）+ 是否在编辑页（回列表证据用）+ 返回键快照
    box_top = None
    from_edit_page = False
    probe_root = _dump("send_precheck")
    if probe_root is not None:
        inp = _find_reply_input(probe_root)
        from_edit_page = inp is not None
        m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", (inp.get("bounds") if inp is not None else "") or "")
        if m:
            box_top = int(m.group(2))
    back0 = _BACK_PRESSES
    if node is not None:
        c = _center(node.get("bounds"))
        if not c:
            _fail(FC_SEND_BTN_NOT_FOUND, "「发送」节点的 bounds 解析失败")
            return False
        _tap(*c, source="uiautomator「发送」节点 bounds")
    else:
        cand = _wait_ocr(lambda t: t.strip() == "发送", timeout=5.0, desc="发送按钮")
        if cand is None:
            _fail(FC_SEND_BTN_NOT_FOUND, "节点与 OCR 都没有「发送」锚点，本轮不发送")
            return False
        _tap(*cand, source="OCR「发送」文字锚点（兜底）")
    confirmed, source, how = _confirm_sent(text, box_top=box_top, from_edit_page=from_edit_page,
                                           back_presses_at_send=back0)
    _note(f"发送确认 confirmed={confirmed} source={source or '-'} box_top={box_top} {how}")
    if not confirmed:
        _fail(FC_SEND_UNCONFIRMED, f"点了发送但确认不到已发出：{how}")
        _write_cur_artifact("after_send.png", _screencap())
        return False
    return True


def _compose_and_send(text: str, sticker: Path | None = None) -> bool:
    """统一的回复链路（取代旧的 _post_reply / _ocr_reply 两套语义）：
    清空 → 点输入框 → 输入 → 截图核对 → [可选表情，插进同一条评论] → 点发送 → 确认发出。

    只有「确认发出」才返回 True，调用方据此写 seen。
    t2 的两条相反错误（发送后核对失败静默 False 不记 seen / 点完发送无条件 True 记 seen）
    都收敛到这里：成功必须可确认，失败必须带失败码。
    t5 必改 2：插表情失败后必须先退出相册并确认回到评论编辑页——
    退得回来就继续发纯文字；退不回来就 sticker_attach_failed 中止本轮，
    绝不在相册页里去找「发送」按钮（那必然 send_btn_not_found，白丢一条回复）。
    """
    if not _in_douyin():
        _fail(FC_HIJACKED, "准备输入时界面已不在抖音")
        return False
    with _phase("input"):
        if not _enter_text(text):
            return False
    if sticker is not None:
        with _phase("sticker"):
            ok = _attach_sticker(sticker)
        _note(f"插表情 ok={ok}")
        if not ok:
            escaped = _escape_album()
            if not escaped:
                _note("sticker_failed reason=album_stuck 插表情失败后回不到评论编辑页，本轮中止",
                      code="sticker_failed", level="warning")
                _fail(FC_STICKER_ATTACH_FAILED,
                      "插表情失败后回不到评论编辑页，本轮中止（不去找发送按钮）")
                _write_cur_artifact("after_sticker_fail.png", _screencap())
                return False
            _note("sticker_failed reason=aborted_after_escape 已退回评论编辑页，本轮改为只发文字",
                  code="sticker_failed", level="warning")
            # F3：退相册后必须复核正文还在，否则不能接着发（否则可能发出空评论/旧内容）
            body_ok, body_how = _reply_body_present(text)
            _note(f"退相册后复核正文 ok={body_ok} {body_how}")
            if not body_ok:
                _write_cur_artifact("after_escape_body_missing.png", _screencap())
                _fail(FC_TEXT_NOT_ENTERED, f"退相册后正文不在输入框（{body_how}），本轮不发送")
                return False
        else:
            # 表情已插进输入框：文字与表情是同一条评论，一起发送
            ok2, how = _verify_typed(text, 9)
            _note(f"sticker_ok file=<inserted> 插表情后回读正文 ok={ok2} {how}", code="sticker_ok")
            if not ok2:
                _write_cur_artifact("after_sticker.png", _screencap())
                if not _in_douyin():
                    _fail(FC_HIJACKED, "插表情后前台已不是抖音")
                else:
                    _fail(FC_TEXT_NOT_ENTERED, "插表情后正文被清掉/界面离开编辑页，本轮不发送")
                return False
    with _phase("send"):
        return _send_and_confirm(text)


def _row_window(it: dict) -> tuple[int, int]:
    """这条 @ 在屏幕上的纵坐标窗口：条目节点 bounds 上下各留余量，用于行对齐。"""
    cy = it.get("center", (540, 1200))[1]
    y1 = y2 = int(cy)
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", str(it.get("bounds") or ""))
    if m:
        y1, y2 = int(m.group(2)), int(m.group(4))
    return y1 - 40, y2 + 320


def _page_contains_any(needles: list[str]) -> tuple[bool, bool]:
    """当前页（节点文本 + OCR 文字）里是否出现任一 needle。

    返回 (是否命中, 是否拿到了可判定的页面文字)。第二个值为 False 表示 dump+OCR 都没取到文字，
    调用方必须按「读不到界面」保守放弃，不能当成「目标不对」。
    """
    hay = ""
    root = _dump("page_check")
    if root is not None:
        for n in _nodes(root):
            hay += (n.get("text") or "") + "\x00" + (n.get("content-desc") or "") + "\x00"
    if not any(x and x in hay for x in needles):
        hay += "\x00".join(t for t, _ in _ocr_screen())
    checked = bool(hay.strip("\x00 \t\r\n"))
    return any(x and x in hay for x in needles), checked


def _try_open_reply_second(it: dict) -> bool:
    """第 2 次尝试（t8）：先按返回清场，再点条目正文区域进详情页 → 点「评论」按钮 → 等输入框。

    为什么：20:32 那轮点「回复评论」后评论区压根没展开（目标不可见），
    死等 8s 只会白烧时间；先清场再换一条进入路径，两次都不行才算 input_not_found。
    返回是否出现回复输入框。
    """
    _back_gesture()
    time.sleep(0.8)
    c = it.get("center")
    if not c:
        _note("第 2 次尝试：条目没有可用 bounds，无法改点正文区域", level="warning")
        return False
    _tap(*c, source="条目正文节点 bounds（第 2 次尝试：进详情页）")
    time.sleep(1.0)
    cbtn = _wait_node(lambda n: (n.get("content-desc") or "").startswith("评论")
                      and "按钮" in (n.get("content-desc") or ""),
                      timeout=6.0, desc="详情页评论按钮")
    cc = _center(cbtn.get("bounds")) if cbtn is not None else None
    if cc:
        _tap(*cc, source="uiautomator「评论」按钮节点 bounds（第 2 次尝试）")
    else:
        _note("第 2 次尝试：详情页没有可点的「评论」按钮", level="warning")
    node = _wait_node(lambda n: n.get("class") == "android.widget.EditText",
                      timeout=8.0, desc="回复输入框（第 2 次尝试）")
    return node is not None


def _open_reply_box(it: dict) -> bool:
    """点开这条 @ 的回复框，并确认输入框真的出现、且打开的是目标评论。

    点击优先级：
    1) 「回复评论」uiautomator 节点 bounds，且必须落在该条目自己的行窗口里（不跨行）；
    2) OCR「回复评论」文字锚点，必须是「本条评论文字所在行的下方」；
    3) 条目自身节点 bounds（打开视频/评论面板）。
    绝不按「离条目中心最近的回复按钮」盲选 —— 那会点到相邻评论（t2 根因 1047-1057）。
    输入框 8s 内没出现 → input_not_found；打开的不是目标评论 → hijacked。
    """
    lo, hi = _row_window(it)
    probe = re.sub(r"[^\w\u4e00-\u9fff]", "", it.get("text") or "")[:8]
    sender = it.get("sender") or ""
    root = _dump("reply_btn")
    btn = None
    if root is not None:
        for n in _nodes(root):
            label = (n.get("text") or "").strip() or (n.get("content-desc") or "").strip()
            if label != "回复评论":
                continue
            c = _center(n.get("bounds"))
            if c and lo <= c[1] <= hi:
                btn = c
                break
    if btn is not None:
        _tap(*btn, source=f"uiautomator「回复评论」节点 bounds（行窗口 {lo}-{hi}）")
    else:
        found = _ocr_screen()
        row_y = None
        for t, c in found:
            if probe and probe in re.sub(r"[^\w\u4e00-\u9fff]", "", t):
                row_y = c[1]
                break
        if row_y is None and sender:
            for t, c in found:
                if sender in t:
                    row_y = c[1]
                    break
        cand = None
        if row_y is not None:
            below = [(c, c[1] - row_y) for t, c in found
                     if "回复评论" in t and 0 < c[1] - row_y < 320]
            if below:
                cand = min(below, key=lambda x: x[1])[0]
        if cand is not None:
            _tap(*cand, source=f"OCR「回复评论」文字锚点（本条行下方 row_y={row_y}）")
        else:
            c = it.get("center")
            if not c:
                _fail(FC_REPLY_BTN_NOT_FOUND, "没有「回复评论」锚点，条目也没有可用 bounds")
                return False
            _tap(*c, source="条目自身节点 bounds（打开视频详情）")
            _note("没有「回复评论」锚点，改点条目节点打开视频详情，再点「评论」按钮")
            # 等视频页「评论」按钮出现：timeout=8s，超时即 reply_btn_not_found（不盲点）
            cbtn = _wait_node(
                lambda n: (n.get("content-desc") or "").startswith("评论")
                and "按钮" in (n.get("content-desc") or ""),
                timeout=8.0, desc="评论按钮")
            cc = _center(cbtn.get("bounds")) if cbtn is not None else None
            if not cc:
                _fail(FC_REPLY_BTN_NOT_FOUND,
                      "既没有「回复评论」锚点，视频页也没有可点的「评论」按钮")
                return False
            _tap(*cc, source="uiautomator「评论」按钮节点 bounds")
    # 等回复输入框出现：timeout=8s；第 1 次失败先走「返回清场 → 点正文 → 评论按钮」再试一次（t8）
    node = _wait_node(lambda n: n.get("class") == "android.widget.EditText",
                      timeout=8.0, desc="回复输入框")
    if node is None:
        _note("第 1 次点「回复评论」8s 没展开输入框，改走第 2 次尝试（返回清场 → 点条目正文 → 评论按钮）",
              level="warning")
        if not _try_open_reply_second(it):
            _fail(FC_INPUT_NOT_FOUND, "点开回复两次都没出现 EditText（评论区没展开）")
            return False
    sender_head = re.split(r"[\s,，]", sender)[0][:12]
    matched, checked = _page_contains_any([sender, sender_head, probe])
    if not matched:
        if not checked:
            _fail(FC_DUMP_FAILED, "打开回复框后 dump+OCR 都读不到页面文字，无法确认目标，保守放弃")
        else:
            _fail(FC_HIJACKED,
                  f"打开的回复框对不上目标（没有 {sender!r}/{sender_head!r} 也没有评论前 6 字 {probe!r}）")
        return False
    return True


def _frame_black(desc: str) -> bool:
    """识图说"全黑/看不清/没有画面"时，判为截到黑帧。"""
    t = str(desc or "")
    return (not t.strip()) or any(k in t for k in ("全黑", "一片黑", "看不清", "无法看到", "没有画面", "纯黑"))


async def _describe_frame() -> str:
    """截一帧丢给识图，拿"视频里在演什么"。

    和 QQ 聊天共用同一套识图链路（先缩图，再按网络情况选通道）：
      有 VPN / 谷歌没被封 → Gemini（快、免费）
      否则 → 硅基 Qwen3-VL（赠送额度）
      都不行 → 本地 QwenVL
    原来这里只试 Gemini + 本地，没开 VPN 时 Gemini 必失败、本地又慢或被关，
    结果就是"没画面，光看文案编不出来"。
    """
    img = _screencap()
    if not img:
        return ""
    prompt = "这是抖音视频播放页截图。用一句中文描述视频画面里的人物/动作/场景，30字内，不要描述文字和界面。"
    small = img
    try:
        from plugins.ai_group import _shrink_image
        small = _shrink_image(img)   # 手机截图 1080x2400，压缩后识图快很多
        _note(f"画面截图 原 {len(img)//1024}KB → 压缩 {len(small)//1024}KB")
    except Exception as e:  # noqa: BLE001
        _exc("压缩截图", e)
    # 1) Gemini：只在能连上谷歌时才发，避免白等
    use_gemini = True
    try:
        from plugins.ai_group import _gemini_blocked, _vpn_ok
        if _gemini_blocked():
            use_gemini = False
            _note("Gemini 已被停用（额度/异常），跳过")
        elif not await _vpn_ok():
            use_gemini = False
            _note("未检测到谷歌直连，识图走硅基")
    except Exception:  # noqa: BLE001
        pass
    if use_gemini:
        try:
            from plugins import gemini
            text = await gemini.gemini_vision(prompt, small, mime="image/jpeg")
            if text:
                _note("画面描述来源=Gemini")
                return text.strip()[:120]
        except Exception as e:  # noqa: BLE001
            _exc("gemini 描述画面", e)
    # 2) 硅基 Qwen3-VL（赠送额度，不依赖 VPN）
    try:
        from plugins.ai_group import _sf_vision
        text = await _sf_vision(small, prompt)
        if text:
            _note("画面描述来源=硅基 Qwen3-VL")
            return text.strip()[:120]
    except Exception as e:  # noqa: BLE001
        _exc("硅基描述画面", e)
    # 3) 本地 QwenVL 兜底
    try:
        from plugins.ai_group import _local_qwen_vision
        text = await _local_qwen_vision(small, prompt, timeout=90)
        if text:
            _note("画面描述来源=本地")
            return text.strip()[:120]
    except Exception as e:  # noqa: BLE001
        _exc("本地 qwen 描述画面", e)
    _note("三种识图都没给出画面描述", level="warning")
    return ""


def _grab_frame_strip(n: int = 3, gap: float = 0.6) -> bytes:
    """连拍 n 张拼成横条（手机没有 screenrecord，用它替代抽帧）。

    一帧看不出动作，3 帧横排能让识图看到"正在做什么"。
    """
    from io import BytesIO
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001
        return b""
    shots = []
    for i in range(max(1, n)):
        img = _screencap()
        if img:
            try:
                im = Image.open(BytesIO(img)).convert("RGB")
                w = 480
                h = int(im.height * w / im.width)
                shots.append(im.resize((w, h), Image.LANCZOS))
            except Exception:  # noqa: BLE001
                pass
        if i + 1 < n:
            time.sleep(gap)
    if not shots:
        return b""
    W = sum(s.width for s in shots)
    H = max(s.height for s in shots)
    canvas = Image.new("RGB", (W, H), (0, 0, 0))
    x = 0
    for s2 in shots:
        canvas.paste(s2, (x, 0))
        x += s2.width
    buf = BytesIO()
    canvas.save(buf, format="JPEG", quality=80)
    _note(f"连拍 {len(shots)} 帧拼条 {W}x{H}")
    return buf.getvalue()


async def _describe_frame_retry(tries: int = 2) -> str:
    """截帧识图；识别出"全黑/看不清"就等 1.5 秒重截一次（视频起播常有黑场）。"""
    last = ""
    for i in range(max(1, tries)):
        try:
            d = await _describe_frame()
        except Exception as e:  # noqa: BLE001
            _exc("截帧识图", e)
            d = ""
        if d and not _frame_black(d):
            return d
        last = d or last
        if i + 1 < tries:
            _note("截到黑帧/没画面，等 1.5 秒重截一次")
            time.sleep(1.5)
    return last


def _has_screenrecord() -> bool:
    """探测本机有没有 screenrecord，结果缓存（t5 必改 5）。

    实跑证据：`adb shell exit=127 err='/system/bin/sh: screenrecord: inaccessible or not found'`，
    每轮白试一次 screenrecord + 再 pull 一个不存在的 /sdcard/dy_record.mp4。
    探测一次、缓存到 _SCREENRECORD_OK，之后直接决定走截图路线。
    """
    global _SCREENRECORD_OK
    if _SCREENRECORD_OK is not None:
        return _SCREENRECORD_OK
    out = _adb("shell", "which", "screenrecord", timeout=10, tag="which_screenrecord")
    if not out or "not found" in out.lower():
        out = _adb("shell", "ls", "/system/bin/screenrecord", timeout=10, tag="ls_screenrecord")
    _SCREENRECORD_OK = bool(out) and "No such file" not in out and "not found" not in out.lower()
    _note(f"screenrecord 可用={_SCREENRECORD_OK}（已缓存，后续轮次不再探测）")
    return _SCREENRECORD_OK


async def _describe_video() -> str:
    """录 5 秒屏再抽 3 帧，看完立刻清理临时文件；本机没有 screenrecord 就直接走截图。"""
    if not _has_screenrecord():
        # 手机没有 screenrecord：改用连拍 3 帧拼横条，识图能看出"在做什么"
        strip = _grab_frame_strip(3, 0.6)
        if strip:
            try:
                from plugins.ai_group import _sf_vision, _local_qwen_vision, _gemini_blocked, _vpn_ok
                prompt = ("这是一段抖音视频里连续 3 个瞬间拼成的横条（从左到右）。"
                          "用一句中文说清视频里在演什么：人物/动作/场景，40 字内。不要描述界面文字。")
                d = ""
                try:
                    if not _gemini_blocked() and await _vpn_ok():
                        from plugins import gemini
                        d = await gemini.gemini_vision(prompt, strip, mime="image/jpeg") or ""
                except Exception:  # noqa: BLE001
                    d = ""
                if not d:
                    d = await _local_qwen_vision(strip, prompt, timeout=60) or ""
                if not d:
                    d = await _sf_vision(strip, prompt) or ""
                if d:
                    _note(f"画面描述来源=连拍拼条 {d[:40]}")
                    return d.strip()[:120]
            except Exception as e:  # noqa: BLE001
                _exc("连拍拼条识图", e)
        _note("连拍失败，退回单帧截图")
        return await _describe_frame_retry()
    import shutil
    import tempfile
    remote = "/sdcard/dy_record.mp4"
    tmpdir = Path(tempfile.mkdtemp(prefix="dyrec_"))
    tmp = tmpdir / "v.mp4"
    try:
        _adb("shell", "rm", "-f", remote)
        _adb("shell", "screenrecord", "--time-limit", "5", "--size", "540x960", remote, timeout=20,
             tag="screenrecord")
        _adb("pull", remote, str(tmp), timeout=20, tag="pull_record")
        if not tmp.exists() or tmp.stat().st_size < 1000:
            _note("录屏文件没拿到/太小，退回单帧截图")
            return await _describe_frame()
        import imageio_ffmpeg
        import subprocess
        ff = imageio_ffmpeg.get_ffmpeg_exe()
        frames = []
        for i, pos in enumerate((0.15, 0.5, 0.85)):
            p = tmp.parent / f"f{i}.jpg"
            subprocess.run([ff, "-y", "-ss", str(pos * 5), "-i", str(tmp),
                            "-frames:v", "1", "-vf", "scale=720:-1", str(p)],
                           capture_output=True)
            if p.exists():
                frames.append(p.read_bytes())
        descs = []
        from plugins import gemini
        for img in frames:
            try:
                d = await gemini.gemini_vision(
                    "描述这张视频帧的人物/动作/场景，30字内，不要描述界面文字。", img, mime="image/jpeg")
                if d:
                    descs.append(d.strip())
                    continue
            except Exception as e:  # noqa: BLE001
                _exc("gemini 描述视频帧", e)
            try:
                from plugins.ai_group import _local_qwen_vision
                d = await _local_qwen_vision(img, "描述这张视频帧的人物/动作/场景，30字内。", timeout=90)
                if d:
                    descs.append(d.strip())
            except Exception as e:  # noqa: BLE001
                _exc("本地 qwen 描述视频帧", e)
        if descs:
            return "；".join(descs[:3])[:180]
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[抖音手机] 录屏抽帧失败：{type(e).__name__}: {e}")
    finally:
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception as e:  # noqa: BLE001
            _exc("清理录屏临时目录", e)
        if _SCREENRECORD_OK:
            _adb("shell", "rm", "-f", remote)
    return await _describe_frame()


async def _generate_reply(caption: str, comment: str, frame: str = "") -> str:
    try:
        from plugins.ai_group import call_ai_raw, CURRENT_DAILY_MOOD, emotion_prompt, is_sleeping, just_woke_prompt
        persona = ""
        try:
            persona = (ROOT / "persona.md").read_text(encoding="utf-8")[:1200]
        except Exception as e:  # noqa: BLE001
            _exc("读 persona.md", e)
        state = "刚被叫醒，还迷糊" if just_woke_prompt() else (
            "正在睡觉休息" if is_sleeping() else CURRENT_DAILY_MOOD)
        prompt = (
            f"你是「蓝色大肥鱼」，圆滚滚的蓝色鲸鱼拟人少女。人设：{persona[:900]}\n"
            f"当前状态：{state}；情绪分布：{emotion_prompt()[:200]}。\n"
            f"有人在抖音视频下 @你：{str(comment)[:200]}\n视频文案：{str(caption)[:300]}\n"
            f"视频画面：{str(frame)[:150] or '（没抓到画面）'}\n"
            + ("（你已经看过这个视频的画面，可以直接聊画面内容，别再说看不到/没画面。）\n" if frame else "")
            + "写一句 15~30 字回复，像真人博主回评：可以接梗、撒娇或轻怼，结合你现在的心情/状态；"
            "不脏话、不low、不营销、不带链接、不自称AI；只输出回复正文。"
        )
        _txt = (await call_ai_raw(prompt, "deepseek", temperature=0.95, max_tokens=120) or "").strip()
        try:
            from plugins import moderation
            _safe = moderation.filter_public(_txt, channel="douyin")
            if not _safe:
                logger.info("[审核] 抖音评论命中红线，本条不发")
            return _safe
        except Exception:  # noqa: BLE001
            return _txt
    except Exception as e:  # noqa: BLE001
        _exc("生成回复正文", e)
        return ""


def _mood_folder() -> str:
    """按当前主导情绪选表情包文件夹。"""
    mapping = {"开心": "开心", "害羞": "卖萌", "怒": "生气", "烦": "生气",
               "丧": "委屈", "累": "无语", "害怕": "委屈"}
    try:
        data = json.loads((ROOT / "memory" / "emotion_state.json").read_text(encoding="utf-8"))
        vals = data.get("values") or {}
        top = max(vals, key=lambda k: float(vals.get(k) or 0))
        if top in mapping:
            return mapping[top]
    except Exception as e:  # noqa: BLE001
        _exc("读情绪状态选表情文件夹", e)
    try:
        p = json.loads((ROOT / "memory" / "daily_persona.json").read_text(encoding="utf-8"))
        mood = str(p.get("mood") or "")
        for k in ("得意", "震惊", "开心", "委屈", "无语", "生气", "卖萌"):
            if k in mood:
                return k
    except Exception as e:  # noqa: BLE001
        _exc("读日常心情选表情文件夹", e)
    return ""


def _pick_sticker() -> Path | None:
    """兼容旧调用：只返回文件，不做可观测（新代码请用 _pick_sticker_decision）。"""
    return _pick_sticker_decision()[0]


def _pick_sticker_decision() -> tuple[Path | None, str, str]:
    """决定这一轮插不插表情，并把判定输入完整写进日志（t5 补充：跳过也必须可观测）。

    实跑对照：19:46 那轮进了 sticker 阶段（48s 后失败），19:50 那轮 phases 里根本没有 sticker ——
    差别就是这里的 30% 随机闸门（random.random() < 0.3），之前完全没留痕。
    返回 (文件, reason, detail)：
    - reason="ok"                会插表情
    - reason="random_gate"       30% 随机没中（roll >= rate）
    - reason="attach_disabled"   DOUYIN_STICKER_RATE<=0，主动关闭
    - reason="mood_unmapped"     读不到情绪/映射不到文件夹，且表情库也没有可用图
    - reason="no_mood_folder"    情绪文件夹不存在/为空，且表情库也没有可用图
    - reason="no_sticker_file"   整个表情库都没有 png/jpg
    detail 里带 mood / 目录 / 各目录文件数 / roll / rate，便于事后解释「这轮为什么没图」。
    """
    base = ROOT / "表情包"
    mood = _mood_folder()
    exts = (".png", ".jpg", ".jpeg")
    folder_files: list[Path] = []
    folder_path = base / mood if mood else None
    try:
        if folder_path is not None and folder_path.is_dir():
            folder_files = [p for p in folder_path.glob("*") if p.suffix.lower() in exts]
    except Exception as e:  # noqa: BLE001
        _exc("列情绪表情目录", e)
    all_files: list[Path] = []
    try:
        if base.is_dir():
            all_files = [p for p in base.rglob("*") if p.suffix.lower() in exts]
    except Exception as e:  # noqa: BLE001
        _exc("列整个表情库", e)
    roll = random.random()
    detail = (f"mood={mood!r} folder_files={len(folder_files)} "
              f"library_files={len(all_files)} roll={roll:.3f} rate={STICKER_RATE}")
    if STICKER_RATE <= 0:
        return None, "attach_disabled", detail
    if not mood:
        if not all_files:
            return None, "mood_unmapped", detail
    elif not folder_files and not all_files:
        return None, "no_mood_folder", detail
    if not all_files:
        return None, "no_sticker_file", detail
    if roll >= STICKER_RATE:
        return None, "random_gate", detail
    pool = folder_files or all_files
    picked = random.choice(pool)
    if not folder_files:
        detail += f" fallback={picked.parent.name}（情绪目录空/缺，用全库兜底）"
    return picked, "ok", detail


def _find_text(root: ET.Element, text: str) -> tuple[int, int] | None:
    for n in _nodes(root):
        if (n.get("text") or "").strip() == text:
            return _center(n.get("bounds"))
    return None


def _is_comment_edit_page(root: ET.Element) -> bool:
    """评论编辑页判据（F2）：必须有**回复用 EditText**，不能只认「发送」节点。

    相册预览页/相册搜索页也有「发送」按钮，旧判据会把它们当成编辑页 ——
    _escape_album 一次返回就被误判「已回到编辑页」，然后接着去找发送按钮必败。
    现在统一走 _find_reply_input（EditText 且不是搜索/相册挑选框）。
    """
    return _find_reply_input(root) is not None


def _dismiss_album_permission(root: ET.Element) -> None:
    """首次插表情会弹相册/存储权限：点允许（节点 bounds）。"""
    for label in ("仅使用期间允许", "本次使用允许", "允许", "确定"):
        p = _find_text(root, label)
        if p:
            _tap(*p, source=f"uiautomator 权限弹窗「{label}」节点 bounds")
            return


def _album_rows(root: ET.Element) -> list[tuple[str, tuple[int, int]]]:
    """相册「文件夹列表」的每一行：[(文件夹名, 名称节点中心)]，按 y 排序。

    抖音相册是「缩略图 + 名称 + 数量」的文件夹列表（t5 实跑截图：得意 12 / 无语 16 / …），
    数量是独立节点，所以只用名称做锚点、绝不用数量当锚点。
    判据：2~6 个纯汉字、且正下方 160px 内有纯数字计数节点。
    """
    entries: list[tuple[str, tuple[int, int]]] = []
    nums: list[tuple[int, int]] = []
    for n in _nodes(root):
        t = (n.get("text") or "").strip()
        c = _center(n.get("bounds"))
        if not t or not c:
            continue
        if t.isdigit():
            nums.append(c)
        entries.append((t, c))
    stop = {"所有照片", "全部照片", "相册", "多选", "取消", "下一步", "完成", "发送",
            "预览", "编辑", "确定", "所有视频", "最近项目"}
    rows: list[tuple[str, tuple[int, int]]] = []
    for t, c in entries:
        if t in stop or t.startswith(("返回", "关闭")):
            continue
        if not (2 <= len(t) <= 6) or not re.fullmatch(r"[\u4e00-\u9fff]+", t):
            continue
        # 数量节点在名称正下方（同一行）：纵向 +0~160px、横向 ±120px
        if not any(0 <= ny - c[1] <= 160 and abs(nx - c[0]) <= 120 for nx, ny in nums):
            continue
        rows.append((t, c))
    rows.sort(key=lambda x: x[1][1])
    return rows


def _album_image_hits(root: ET.Element) -> tuple[list[tuple[int, int, str]], str]:
    """相册九宫格的图片候选：[(x, y, bounds)] + 命中来源（t9）。

    1) 优先原路径：content-desc 含「点按两次」或以「照片/图片」开头 → source=text_desc；
    2) 退化到 **几何 + clickable**（真机实测，20:35 那轮的 sticker_grid_timeout.xml）：
       缩略图是 356x356 的 FrameLayout，clickable=true，text/content-desc 全空、resource-id 还可能为空，
       所以只按「可点 + 近似正方形 + 边长 200~500 + 中心在屏幕 15% 以下」识别 → source=geometry。
    3) 都认不出来 → ([], "")。
    """
    hits: list[tuple[int, int, str]] = []
    for n in _nodes(root):
        d = (n.get("content-desc") or "").strip()
        if "点按两次" in d or d.startswith(("照片", "图片")):
            c = _center(n.get("bounds"))
            if c:
                hits.append((c[0], c[1], n.get("bounds") or ""))
    if hits:
        hits.sort(key=lambda p: (p[1], p[0]))
        return hits, "text_desc"
    h = _screen_h(root)
    geo: list[tuple[int, int, int, str]] = []       # (cy, cx, x1, bounds)
    for n in _nodes(root):
        if (n.get("clickable") or "").strip().lower() != "true":
            continue
        b = n.get("bounds") or ""
        m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", b)
        if not m:
            continue
        x1, y1, x2, y2 = map(int, m.groups())
        w, hh = x2 - x1, y2 - y1
        if not (200 <= w <= 500 and 200 <= hh <= 500):
            continue
        if not hh or not (0.9 <= w / hh <= 1.1):
            continue
        cy = (y1 + y2) // 2
        if cy < h * 0.15:
            continue
        geo.append((cy, (x1 + x2) // 2, x1, b))
    geo.sort(key=lambda x: (x[0], x[1]))
    if geo:
        return [(g[1], g[0], g[3]) for g in geo], "geometry"
    return [], ""


def _album_grid(root: ET.Element) -> list[tuple[int, int]]:
    """图片九宫格里可点的图片坐标（按 y、x 排序）；进入文件夹后文件夹行会消失。"""
    return [(x, y) for x, y, _b in _album_image_hits(root)[0]]


def _album_page_kind(root: ET.Element) -> str:
    """相册当前页类型（F1）：folders=文件夹列表 / grid=照片九宫格 / unknown。

    判据顺序很重要：先认文件夹行（名称 + 正下方数量），再认图片网格。
    「所有照片」这几个字在两种页面上都会出现（标题栏），所以绝不能拿它当「已是文件夹列表」的判据。
    """
    if len(_album_rows(root)) >= 2:
        return "folders"
    if len(_album_grid(root)) >= 3:
        return "grid"
    return "unknown"


def _album_title_node(root: ET.Element) -> ET.Element | None:
    """相册标题栏「所有照片 ⌄」节点：text 以「所有照片」开头，且中心 y 在屏幕上方 12%。"""
    h = _screen_h(root)
    for n in _nodes(root):
        t = (n.get("text") or "").strip()
        if not t.startswith("所有照片"):
            continue
        c = _center(n.get("bounds"))
        if c and c[1] <= h * 0.12:
            return n
    return None


def _open_album_folder_list() -> ET.Element | None:
    """把相册切到「文件夹列表」（F1）。

    真机行为：点「插入图片」→ 先打开「所有照片」九宫格（标题栏也叫「所有照片」）；
    必须点标题栏「所有照片 ⌄」下拉才切到文件夹列表（得意/无语/委屈/生气/震惊/卖萌…）。
    以前把「有『所有照片』字样」当成已在文件夹列表 → 永远点不到情绪文件夹，
    空滑 3 次后 sticker_folder_not_found。
    现在：每次 dump 先判页面类型，不是 folders 就点标题切换并等 rows 出现（最多 3 轮）。
    失败时落 album 截图 + 当时的 ui.xml（F8）。
    """
    for attempt in range(3):
        root = _dump(f"album_kind#{attempt + 1}")
        if root is None:
            _note("相册页读不到节点", level="warning")
            return None
        _dismiss_album_permission(root)
        kind = _album_page_kind(root)
        _note(f"相册页面类型={kind} rows={len(_album_rows(root))} grid={len(_album_grid(root))}")
        if kind == "folders":
            return root
        title = _album_title_node(root)
        c = _center(title.get("bounds")) if title is not None else None
        if not c:
            # 也可能是系统相册的「相机」页：有「相册」tab 就先点它
            tab = _find_node(root, lambda n: (n.get("content-desc") or "").strip() == "相册"
                             or (n.get("text") or "").strip() == "相册")
            ctab = _center(tab.get("bounds")) if tab is not None else None
            if ctab:
                _tap(*ctab, source="uiautomator「相册」tab 节点 bounds")
                time.sleep(1.2)
                continue
            _note("相册里找不到标题栏「所有照片 ⌄」，无法切文件夹列表",
                  code=FC_ALBUM_OPEN_FAILED, level="warning")
            _write_cur_artifact("album_no_title.png", _screencap())
            _write_xml_artifact("album_no_title.xml", f"album_kind#{attempt + 1}")
            return None
        _tap(*c, source="uiautomator 相册标题「所有照片 ⌄」节点 bounds（切文件夹列表）")
        _note("点了相册标题，等文件夹列表出现（最多 6s）")
        deadline = time.time() + 6
        while time.time() < deadline:
            r2 = _dump("album_after_title")
            if r2 is not None and len(_album_rows(r2)) >= 2:
                _note(f"已切到文件夹列表：{[x[0] for x in _album_rows(r2)]}")
                return r2
            time.sleep(0.6)
    _note("点标题 3 次仍没切到文件夹列表", code=FC_ALBUM_OPEN_FAILED, level="warning")
    _write_cur_artifact("album_open_failed.png", _screencap())
    _write_xml_artifact("album_open_failed.xml", "album_after_title")
    return None


def _wait_sticker_grid(timeout: float = 10.0) -> list[tuple[int, int]]:
    """等表情文件夹里的九宫格出现：timeout=10s，超时返回 []（调用方记 sticker_pick_failed）。

    t9：候选识别改成「先 text/desc，再 几何+clickable」（真机缩略图是 356x356 可点 FrameLayout，
    没有 text/desc），命中后 events 会写 `九宫格候选=N 选第1张 bounds=… source=geometry|text_desc`。
    """
    deadline = time.time() + timeout
    last = 0
    while True:
        root = _dump("sticker_grid")
        if root is not None:
            hits, src = _album_image_hits(root)
            rows = _album_rows(root)
            last = len(hits)
            # 至少 4 张图，且文件夹列表行已消失，才算真的进了文件夹
            if len(hits) >= 4 and len(rows) <= 1:
                x, y, b = hits[0]
                _note(f"九宫格候选={len(hits)} 选第1张 bounds={b} center={x},{y} source={src}")
                return [(hx, hy) for hx, hy, _b in hits]
        if time.time() >= deadline:
            _note(f"等九宫格超时（图片候选={last}）", level="warning")
            return []
        time.sleep(0.8)


def _find_sticker_folder(mood: str, folder_name: str) -> tuple[int, int, str] | None:
    """在相册文件夹列表里找表情文件夹；首屏没有就滚动列表再找（最多 3 屏）。

    F9：优先按**实际 push 的目录名**（folder_name）找，情绪名（mood）只作提示 ——
    因为 push 到 /sdcard/Pictures/大肥鱼表情包/<目录名> 用的是贴纸自己的父目录名，
    当 mood 与实际目录不一致（比如 mood=开心 但贴纸在 得意/）时，按 mood 找必然落空。
    上滑手势只允许在相册页内用（t5++ 要求 5），且绝不能出现在互动消息列表上。
    返回 (x, y, 文件夹名)；3 屏都找不到返回 None（调用方记 sticker_folder_not_found）。
    """
    wants = [w for w in (folder_name, mood) if w]
    for screen in range(3):
        root = _dump(f"album_folders#{screen + 1}")
        if root is None:
            _note("文件夹列表读不到节点", level="warning")
            return None
        rows = _album_rows(root)
        names = [r[0] for r in rows]
        _note(f"相册文件夹（第 {screen + 1} 屏）：{names}（找 {wants}）")
        if _is_interaction_list(root):
            _note("当前是互动消息列表，不是相册页：拒绝上滑（避免把列表划走）", level="warning")
            return None
        for want in wants:
            for name, c in rows:
                if name == want:
                    return c[0], c[1], name
            for name, c in rows:
                if want and want in name:
                    return c[0], c[1], name
        if screen < 2:
            if not rows and _album_title_node(root) is None:
                _note("相册页判据不成立（既没有文件夹行也没有「所有照片」标题）：不再上滑",
                      level="warning")
                return None
            _swipe("album", reason=f"表情文件夹不在第 {screen + 1} 屏（相册页内上滑翻文件夹）")
            time.sleep(1.2)
    return None


def _escape_album() -> bool:
    """插表情失败后退出相册/图片编辑页，回到评论编辑页（t5 必改 2）。

    最多 4 次返回键，每次回来都 dump 确认（有 EditText 或「发送」节点）。
    返回 False = 退不出去：调用方必须中止本轮（sticker_attach_failed），不许接着去找发送按钮。
    """
    for i in range(4):
        root = _dump(f"escape_album#{i + 1}")
        if root is not None and _is_comment_edit_page(root):
            _note("已退回评论编辑页（可以继续发文字）")
            return True
        _back_gesture()
        time.sleep(0.8)
    root = _dump("escape_album_final")
    ok = root is not None and _is_comment_edit_page(root)
    _note(f"4 次返回后{'回到' if ok else '仍没回到'}评论编辑页")
    return ok


def _attach_sticker(sticker: Path | None) -> bool:
    """把电脑端表情包 push 到手机相册，再从相册「文件夹 → 第一张图」插进评论。

    真机流程（F1 修正后）：插入图片 → 「所有照片」九宫格 → 点标题「所有照片 ⌄」切文件夹列表 →
    点表情文件夹 → 九宫格 → 选第一张 → （有「下一步」就点）→ 回评论编辑页。
    旧实现把「有『所有照片』字样」当成已在文件夹列表 → 永远点不到情绪文件夹（t5 实跑 12 次空轮询）。
    表情是插进同一条评论、和文字一起发出去的，不是先发文字再补图。
    任一子步骤失败都返回 False 并留下唯一码：album_open_failed / sticker_folder_not_found /
    sticker_pick_failed，同时把当时的截图 + ui.xml 落盘（F8）。
    退出相册由调用方 _escape_album() 负责（t5 必改 2）。
    所有点击都来自 uiautomator 节点 bounds。
    """
    if sticker is None:
        return False
    folder = sticker.parent.name or "表情包"
    mood = _mood_folder() or folder
    remote_dir = f"/sdcard/Pictures/大肥鱼表情包/{folder}"
    remote = f"{remote_dir}/{sticker.stem}_{int(time.time())}{sticker.suffix.lower()}"
    _adb("shell", "mkdir", "-p", remote_dir, timeout=15, tag="mkdir_sticker")
    _adb("push", str(sticker), remote, timeout=30, tag="push_sticker")
    _adb("shell", "am", "broadcast", "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
         "-d", f"file://{remote}", tag="media_scan")
    # F9：记录 push 的真实远端路径 + 落盘校验（相册里按这个目录名找，不是按 mood 猜）
    ls_out = _adb("shell", "ls", "-l", remote, timeout=10, tag="ls_sticker")
    _note(f"准备插表情 mood={mood!r} push_dir={folder!r} remote={remote} ls={ls_out[:80]!r}")
    if not ls_out or "No such file" in ls_out:
        _note("push 后在手机上看不到该文件（相册里也不会出现）",
              code=FC_STICKER_PICK_FAILED, level="warning")
        _write_xml_artifact("sticker_push_missing.xml", "album_kind#1")
        return False
    # 等评论编辑页的「插入图片」按钮：timeout=10s，超时即 album_open_failed（正文照发）
    node = _wait_node(lambda n: "插入图片" in ((n.get("content-desc") or "") + (n.get("text") or "")),
                      timeout=10.0, desc="插入图片按钮")
    c = _center(node.get("bounds")) if node is not None else None
    if not c:
        _note("找不到「插入图片」按钮（相册没打开）", code=FC_ALBUM_OPEN_FAILED, level="warning")
        _write_cur_artifact("album_no_insert_btn.png", _screencap())
        return False
    _tap(*c, source="uiautomator「插入图片」节点 bounds")
    # 等相册打开（先出现九宫格或标题栏），再切到文件夹列表
    folder_root = _open_album_folder_list()
    if folder_root is None:
        return False
    hit = _find_sticker_folder(mood, folder)
    if hit is None:
        _note(f"文件夹列表里没有 {folder!r}/{mood!r}", code=FC_STICKER_FOLDER_NOT_FOUND, level="warning")
        _write_cur_artifact("sticker_folder_not_found.png", _screencap())
        _write_xml_artifact("sticker_folder_not_found.xml", "album_folders#1")
        return False
    fx, fy, fname = hit
    _tap(fx, fy, source=f"uiautomator 相册文件夹「{fname}」名称节点 bounds")
    _note(f"已点表情文件夹「{fname}」，等九宫格")
    grid = _wait_sticker_grid(10.0)
    if not grid:
        _note(f"进了「{fname}」但没等到九宫格", code=FC_STICKER_PICK_FAILED, level="warning")
        _write_cur_artifact("sticker_grid_timeout.png", _screencap())
        _write_xml_artifact("sticker_grid_timeout.xml", "sticker_grid")
        return False
    _note(f"九宫格候选={len(grid)}，选第一张 {grid[0]}")
    _tap(*grid[0], source="uiautomator 九宫格首个图片节点 bounds")
    # 可能进图片编辑页（有「下一步/完成」），也可能直接回评论页
    deadline = time.time() + 12
    edited = False
    while True:
        root = _dump("sticker_after_pick")
        if root is not None:
            if _is_comment_edit_page(root):
                _note("表情已插入并回到评论编辑页（表情 + 文字同一条评论）")
                return True
            if not edited:
                nxt = (_find_text(root, "下一步") or _find_text(root, "完成")
                       or _find_text(root, "添加") or _find_text(root, "确定"))
                if nxt:
                    _tap(*nxt, source="uiautomator「下一步/完成」节点 bounds")
                    edited = True
        if time.time() >= deadline:
            _note("选了图但 12s 内没回到评论编辑页", code=FC_STICKER_PICK_FAILED, level="warning")
            _write_cur_artifact("sticker_pick_failed.png", _screencap())
            _write_xml_artifact("sticker_pick_failed.xml", "sticker_after_pick")
            return False
        time.sleep(1.0)


def _collect_interactions(root: ET.Element) -> list[dict]:
    """从「互动消息」页提取 @ / 评论 / 回复 / 点赞 条目。"""
    items: list[dict] = []
    pat = re.compile(r"(提到了你|评论了你|回复了你|回复|评论)[:：]\s*(.*?)\s*(刚刚|\d+\s*(?:分钟|小时)前)")
    like_pat = re.compile(r"(赞了你的评论|赞了你的作品|点赞了你|赞了你)")
    for n in _nodes(root):
        desc = (n.get("content-desc") or "").strip()
        if not desc:
            continue
        m = pat.search(desc)
        like = like_pat.search(desc)
        if not m and not like:
            continue
        if m:
            kind = m.group(1)
            text = m.group(2).strip()
            ts = m.group(3)
        else:
            kind = "点赞"
            text = ""
            tm = re.search(r"(刚刚|\d+\s*(?:分钟|小时)前)", desc)
            ts = tm.group(1) if tm else ""
        age = 0
        if "分钟" in ts:
            age = int(re.sub(r"\D", "", ts) or 0)
        elif "小时" in ts:
            age = int(re.sub(r"\D", "", ts) or 0) * 60
        c = _center(n.get("bounds"))
        if not c:
            continue
        if not text and kind != "点赞":
            continue
        sender = desc.split(",")[0].strip()
        key_text = re.sub(r"[^\w\u4e00-\u9fff]", "", text)[:12] if text else "like"
        key = f"{sender}|{kind}|{key_text}"
        items.append({"key": key, "kind": kind, "sender": sender,
                      "text": text, "center": c, "desc": desc, "age": age,
                      "bounds": n.get("bounds") or ""})
    return items


NAV_FILE = ROOT / "memory" / "douyin_phone_nav.json"


def _save_page(page: str) -> None:
    try:
        NAV_FILE.write_text(json.dumps(
            {"page": page, "ts": time.strftime("%Y-%m-%d %H:%M:%S")},
            ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        _exc("写导航页状态", e)


def _open_app() -> None:
    """确保抖音在前台且界面可读（t5 必改 4 / t5++ 要求 4）。

    旧实现无条件 monkey 启动 + 等「focus 里没有 SplashActivity」最多 20s；
    但本机抖音前台的 activity 名本身带 SplashActivity 字样，判据永远为假 →
    每轮白等 20s（实跑一轮出现 3 次）。
    现在：前台包名精确匹配（mCurrentFocus 里的 component），已在前台就立刻返回；
    不在前台才启动，就绪判据 =「前台是抖音 + 能 dump 到节点」，上限 12s，满足即退出。
    """
    if _wait_pkg(DOUYIN_PKG, 1.5) == DOUYIN_PKG:
        _note("抖音已在前台，跳过启动与前台等待")
        return
    _note("抖音不在前台，启动抖音")
    _adb("shell", "monkey", "-p", DOUYIN_PKG, "1", timeout=20, tag="monkey_launch")
    deadline = time.time() + 12
    while True:
        if _foreground_package() == DOUYIN_PKG and _dump("app_ready") is not None:
            _note("抖音已就绪（前台 + 可读节点）")
            return
        if time.time() >= deadline:
            _note("12s 内抖音没到可读界面（前台/节点至少一样没成）", level="warning")
            return
        time.sleep(1.2)


def _wait_douyin_focus(timeout: float = 10.0) -> str:
    """轮询前台包名直到抖音在前台；超时返回最后一次读到的包名。"""
    return _wait_pkg(DOUYIN_PKG, timeout)


def _wake_and_open_app() -> None:
    """点亮屏幕、必要时解锁，并确保抖音在前台（t5++ 要求 1/2/3）。

    解锁判据收紧到 `dumpsys power` 的 mWakefulness=Asleep/Doze 或 window policy 里明确的
    keyguard showing；`isStatusBarKeyguard` 单独出现不再触发（荣耀未锁屏也为 true，
    之前会在互动消息列表中间来一次 2000→700 上滑，把列表划走）。
    解锁顺序：keyevent 224 → wm dismiss-keyguard → keyevent 82(menu)，仍锁着才兜底上滑，
    且一次唤醒流程最多上滑一次，并记录手势 events。
    """
    try:
        need, why = _needs_unlock()
        if need:
            _note(f"检测到真锁屏（{why}），先走无手势解锁：224 → dismiss-keyguard → 82")
            _adb("shell", "input", "keyevent", "224", tag="wakeup")  # WAKEUP
            _adb("shell", "wm", "dismiss-keyguard", tag="dismiss_keyguard")
            _adb("shell", "input", "keyevent", "82", tag="menu_unlock")  # MENU = 解锁
        else:
            _note(f"无需解锁（{why}）")
            _adb("shell", "input", "keyevent", "224", tag="wakeup")  # 仅唤醒屏幕
            _adb("shell", "wm", "dismiss-keyguard", tag="dismiss_keyguard")
        still, why2 = _needs_unlock()
        if still:
            # 兜底：真锁屏才上滑，且整轮只允许这一次
            _swipe("unlock", reason=f"无手势解锁后仍锁屏（{why2}）")
            _wait_pkg(DOUYIN_PKG, 5.0)
        pkg = _wait_pkg(DOUYIN_PKG, 2.0)
        if pkg != DOUYIN_PKG:
            _note(f"当前前台不是抖音（pkg={pkg!r}），启动抖音")
            _open_app()
        else:
            _note("当前已在抖音前台，直接找互动消息")
    except Exception as e:  # noqa: BLE001
        _exc("点亮/启动抖音", e)


def _find_msg_tab(root: ET.Element) -> tuple[int, int] | None:
    """底部「消息」tab：只认 content-desc 里的「消息，按钮」且节点 bounds 落在屏幕下方 15%。"""
    h = _screen_h(root)
    for n in _nodes(root):
        d = (n.get("content-desc") or "").strip()
        t = (n.get("text") or "").strip()
        if not ("消息，按钮" in d or "消息，1条未读" in d or t == "消息"):
            continue
        c = _center(n.get("bounds"))
        if c and c[1] >= h * 0.85:
            return c
    return None


def _ensure_interaction_page() -> tuple[ET.Element | None, str]:
    """从任意界面导航到「互动消息」：首页→消息 tab，视频→手势返回，消息页→互动消息入口。

    返回 (互动消息列表节点, 原因)：原因 "" 成功；"dump_failed" 连续读不到节点；
    "not_in_douyin" 前台不是抖音；"interaction_page_failed" 在抖音里但没走到互动消息列表。
    所有点击都来自 uiautomator 节点 bounds；未知页面不乱划（直接失败）。
    每轮最多 10 次导航尝试，连续 3 次 dump 失败即放弃（dump_failed）。
    """
    with _phase("wake_open"):
        _wake_and_open_app()
    pkg = _foreground_package()
    if pkg != DOUYIN_PKG:
        # 前台判据用「mCurrentFocus 里的 component 包名」精确匹配，不再拿整份 dumpsys 子串猜
        _note(f"前台不是抖音（pkg={pkg!r}），启动一次")
        _open_app()
        if _foreground_package() != DOUYIN_PKG:
            _note("启动抖音后前台仍不是抖音", level="warning")
            return None, "not_in_douyin"
    # 顶部「关注了你，快去看看吧/回关」弹窗会挡住 uiautomator，先按返回清掉
    root0 = _dump("popup_check")
    if root0 is not None:
        blob = " ".join(((n.get("content-desc") or "") + " " + (n.get("text") or ""))
                        for n in _nodes(root0))
        if "回关" in blob or ("关注了你" in blob and "快去" in blob):
            _note("检测到关注弹窗，先按返回清掉")
            _adb("shell", "input", "keyevent", "4", tag="back_popup")
    dump_fails = 0
    for attempt in range(10):
        root = _dump(f"interaction_lookup#{attempt + 1}")
        if root is None:
            dump_fails += 1
            if attempt == 0:
                _open_app()
                continue
            if dump_fails >= 3:
                return None, "dump_failed"
            time.sleep(1.5)
            continue
        # 1. 已经在互动消息列表
        if _is_interaction_list(root):
            _save_page("interaction")
            return root, ""
        # 2. 消息页的「互动消息」入口（有的版本只在 text 里，不在 content-desc）
        entry = _find_node(root, lambda n: (n.get("content-desc") or "").strip().startswith("互动消息")
                           or (n.get("text") or "").strip().startswith("互动消息"))
        if entry is not None:
            c = _center(entry.get("bounds"))
            if c:
                _save_page("messages")
                _tap(*c, source="uiautomator「互动消息」入口节点 bounds")
                # 等列表出现（最多 8s）；超时就进下一轮重新 dump 判定
                _wait_node(_is_interaction_node, timeout=8.0, desc="互动消息列表")
                continue
        # 3. 底部「消息」tab（首页/朋友/我 都适用）
        msg_tab = _find_msg_tab(root)
        if msg_tab:
            _save_page("tab_messages")
            _tap(*msg_tab, source="uiautomator 底部「消息」tab 节点 bounds")
            _wait_node(lambda n: (n.get("content-desc") or "").strip().startswith("互动消息")
                       or (n.get("text") or "").strip().startswith("互动消息"),
                       timeout=8.0, desc="消息页互动消息入口")
            continue
        # 4. 视频详情页/评论面板：只按返回键连退（最多 4 次），绝不用上滑/下滑手势去猜（t10）
        descs = [(n.get("content-desc") or "") for n in _nodes(root)]
        in_detail = any(("返回按钮" in d) or ("评论" in d and "按钮" in d) for d in descs)
        panel = _is_comment_panel_open(root)
        if in_detail or _is_detail_page() or panel:
            _save_page("detail")
            _note(f"导航：检测到视频详情页/评论面板（activity={_foreground_activity() or '-'}，"
                  f"desc_detail={in_detail} 评论面板={panel}）")
            if _back_out_of_detail():
                last = _dump("interaction_after_back")
                if last is not None and _is_interaction_page(last):
                    _save_page("interaction")
                    return last, ""
                continue
            return None, "interaction_page_failed"
        _save_page("unknown")
        _note("停在未知页面，不乱划（放弃导航）", level="warning")
        break
    last = _dump("interaction_final")
    if last is None:
        return None, "dump_failed"
    if _is_interaction_page(last):
        _save_page("interaction")
        return last, ""
    return None, "interaction_page_failed"


def _refresh_interaction_page() -> ET.Element | None:
    """按住列表中间往下划 = 刷新；往上划才是翻到下面。

    滑动坐标是手势（uiautomator 没有「滚到某节点」接口，见 SWIPE_REFRESH 注释），
    只用于刷新，不用于点击确认。划完轮询等条目重新出现（最多 10s）。
    """
    _swipe("refresh", reason="下拉刷新互动消息列表（页面=互动列表）")
    node = _wait_node(_is_interaction_node, timeout=10.0, desc="刷新后的互动条目")
    if node is None:
        _note("下拉刷新后 10s 没读到互动条目", level="warning")
        return None
    return _dump("after_refresh")


def _back_to_interactions() -> None:
    """从视频详情/评论区退回「互动消息」列表（最多 6 次，点节点或系统返回键）。

    t10：视频详情页（DetailActivity）常常叠着评论面板，要连按 2~3 次返回才退到
    IntegratedInteractionActivity；这里优先点「返回」节点，没有就按返回键，并且每轮
    都 dump 判 `_is_interaction_page`（比只认条目 desc 更宽）。
    绝不用上滑/下滑手势去猜。
    """
    for i in range(6):
        root = _dump(f"back_to_list#{i + 1}")
        if root is not None and _is_interaction_page(root):
            return
        node = None
        if root is not None:
            node = _find_node(root, lambda n: (n.get("content-desc") or "").strip() in ("返回按钮", "返回"))
        c = _center(node.get("bounds")) if node is not None else None
        if c:
            _tap(*c, source="uiautomator「返回」节点 bounds")
        else:
            _note(f"导航：退回互动消息 #{i + 1}（activity={_foreground_activity() or '-'}）")
            _back_gesture()
        time.sleep(1.5)  # 页面切换动画余量：下一轮重新 dump 判定，不靠 sleep 断真假
    _note("6 次返回后仍没回到互动消息列表", level="warning")


def _current_aweme_url() -> str:
    """从当前 Activity 的 intent 里抓视频 id，拼成网页链接。

    实测详情页 intent 长这样：
      Intent { dat=aweme://aweme/detail/?id=7701023029643559396&... }
    这个 id 就是 yt-dlp 能用的抖音视频 id。
    """
    try:
        out = _adb("shell", "dumpsys", "activity", "activities", timeout=20, tag="dumpsys_aweme")
    except Exception:  # noqa: BLE001
        return ""
    m = re.search(r"aweme/detail/\?id=(\d{6,})", out or "")
    if not m:
        m = re.search(r"aweme_id=(\d{6,})", out or "")
    if not m:
        return ""
    vid = m.group(1)
    _note(f"抓到视频 id={vid}")
    return f"https://www.douyin.com/video/{vid}"


async def _preview_item_video(it: dict) -> tuple[str, str]:
    """点开这条互动对应的视频，读标题和首帧，再返回互动列表。"""
    title = ""
    frame = ""
    try:
        c0 = it.get("center")
        if not c0:
            _note("条目没有可用 bounds，跳过视频预览（不盲点）", level="warning")
            return title, frame
        x, y = int(c0[0]), int(c0[1])
        bm = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", str(it.get("bounds") or ""))
        if bm:
            # 条目节点 bounds 右缘内缩 110px = 视频缩略图位置（不是写死的屏幕坐标）
            x = max(300, int(bm.group(3)) - 110)
            y = (int(bm.group(2)) + int(bm.group(4))) // 2
        _tap(x, y, source="条目节点 bounds（右侧视频缩略图）")
        # ⚠️ 判据不能只看节点文字：详情页的 activity 已是 DetailActivity，
        # 但节点里根本没有「返回按钮」四个字（今天 09:17 就是这样误判成"没打开"）。
        opened = None
        _t_open = time.time()
        while time.time() - _t_open < 10:
            try:
                if _is_detail_page():
                    opened = True
                    break
                act = (_foreground_activity() or "").lower()
                if "detail" in act:
                    opened = True
                    break
            except Exception:  # noqa: BLE001
                pass
            root_probe = _dump("detail_probe")
            if root_probe is not None:
                opened = _wait_node(
                    lambda n: any(k in (n.get("content-desc") or "") or k in (n.get("text") or "")
                                  for k in ("返回按钮", "返回")),
                    timeout=1.5, desc="视频详情页节点",
                )
                if opened is not None:
                    break
            time.sleep(1.0)
        _note(f"点开视频页 ok={bool(opened)}（activity={_foreground_activity() or '-'}）")
        if opened is None:
            # 没认到详情页标记时别急着放弃：抖音有些版本详情页没有「返回按钮」文字。
            # 先用互动条目的文字当标题（比空标题强），再截一帧看当前画面。
            fallback_title = re.sub(r"\s+", " ", str(it.get("text") or "")).strip()[:60]
            fallback_frame = ""
            try:
                fallback_frame = (await _describe_frame_retry() or "").strip()
            except Exception as e:  # noqa: BLE001
                _exc("详情页识别失败后截帧", e)
            _note(
                f"preview_partial reason=detail_marker_missing title={fallback_title[:20]!r} "
                f"frame_len={len(fallback_frame)}",
                code="preview_partial", level="warning",
            )
            _back_gesture()
            _wait_node(_is_interaction_node, timeout=8.0, desc="返回后的互动列表")
            _ensure_interaction_page()
            return (fallback_title or title), fallback_frame
        # 先抓视频 id → 走"下载+抽帧+音频转写"（有缓存就直接用缓存）
        _vid_url = _current_aweme_url()
        _rich: dict = {}
        if _vid_url:
            try:
                from plugins import douyin_fetch
                _rich = await douyin_fetch.fetch(_vid_url, with_audio=True)
                _note(f"下载解析 cached={bool(_rich.get('cached'))} "
                      f"标题={bool(_rich.get('title'))} 画面={bool(_rich.get('desc'))} "
                      f"口播={bool(_rich.get('transcript'))}")
            except Exception as e:  # noqa: BLE001
                _exc("下载解析视频", e)
        if _rich.get("desc"):
            frame = str(_rich["desc"])[:120]
            if _rich.get("transcript"):
                frame += f"；口播：{str(_rich['transcript'])[:60]}"
        elif _rich.get("transcript"):
            frame = f"口播：{str(_rich['transcript'])[:120]}"
        else:
            frame = await _describe_video()
        root = _dump("video_title")
        if root is not None:
            for t in [(n.get("text") or "").strip() for n in _nodes(root)]:
                if len(t) >= 8 and not any(k in t for k in ("回复", "评论", "点赞", "分享", "关注")):
                    title = t
                    break
        if not title:
            title = re.sub(r"\s+", " ", str(it.get("text") or "")).strip()[:60]
            _note(f"标题顶部没读到，用互动条目文字兜底：{title[:24]!r}", level="warning")
        _back_gesture()
        _wait_node(_is_interaction_node, timeout=8.0, desc="返回后的互动列表")
        _ensure_interaction_page()
    except Exception as e:  # noqa: BLE001
        _exc("预览视频", e)
    return title, frame


async def _poll_once_impl() -> None:
    """一轮「看抖音」主体。每个返回路径都必须留下结局：replied / failed+失败码 / skipped+失败码。"""
    st = _today(_load())
    if not st.get("enabled", True):
        _skip(FC_DISABLED, "插件开关是关的（enabled=false），本轮不跑")
        return
    with _phase("wake"):
        _wake()
    with _phase("navigate"):
        root, why = _ensure_interaction_page()
    if root is None:
        # 手机没连上 / 没进抖音：先撤掉状态栏，再记失败
        _clear_activity()
        code = (FC_DUMP_FAILED if why == "dump_failed"
                else FC_NOT_IN_DOUYIN if why == "not_in_douyin"
                else FC_INTERACTION_PAGE_FAILED)
        _fail(code, f"没能进入「互动消息」页（原因={why}）")
        return
    # 确认真在抖音互动页上了，再挂状态（之前在导航前挂，手机没连也显示"在玩手机上的抖音"）
    with _phase("activity"):
        try:
            from plugins.activity import set_activity
            set_activity("在玩手机上抖音", 600)
        except Exception as e:  # noqa: BLE001
            _exc("设置活动状态", e)
    with _phase("refresh"):
        root2 = _refresh_interaction_page()
    if root2 is not None:
        root = root2
    else:
        _note("下拉刷新没读到界面，继续用刷新前的节点", level="warning")

    with _phase("collect"):
        items = _collect_interactions(root)
        seen = set(str(x) for x in st.get("seen", []))
        fresh = [i for i in items if i["key"] not in seen
                 and (i.get("sender") in OWNER_NAMES or i.get("age", 0) <= 360)
                 and i.get("kind") == "提到了你"]
        likes = sum(1 for i in items if i.get("kind") == "点赞")
        _note(f"互动 {len(items)} 条（点赞 {likes}），可回 {len(fresh)} 条")
        # 点赞只记账，不点；同一人同一作品不重复记
        like_log = st.get("like_log") or []
        known_likes = {str(x.get("key")) for x in like_log}
        for i in items:
            if i.get("kind") != "点赞":
                continue
            if i["key"] in known_likes:
                continue
            like_log.append({"key": i["key"], "sender": i.get("sender", ""),
                             "ts": time.strftime("%Y-%m-%d %H:%M:%S")})
        st["like_log"] = like_log[-100:]
        _save(st)

    # 闸门顺序（t5 必改 3）：先收集候选 → 无候选判 no_target → 主人 @ 免额度/免间隔/免夜间
    # → 主人手动触发（trigger=user/approve）算 force、免 MIN_GAP → 其余才走夜间/额度/间隔。
    # 旧实现把夜间/额度/间隔放在「有没有候选」之前，才会出现「可回 0 条」却判 quota_or_night。
    trigger = (_CUR.trigger if _CUR is not None else "scheduler")
    manual = trigger in ("user", "approve")   # 主人手动查询/破例入口 = force 语义
    limit = DAILY_LIMIT + int(st.get("bonus") or 0)
    fresh.sort(key=lambda i: 0 if i.get("sender") in OWNER_NAMES else 1)
    owner_at = bool(fresh and fresh[0].get("sender") in OWNER_NAMES)
    _hour = time.localtime().tm_hour
    with _phase("gate"):
        if not fresh:
            _skip(FC_NO_TARGET, f"没有新的 @（互动 {len(items)} 条，全部已读或太旧）")
            return
        _note(f"闸门 trigger={trigger} manual_force={manual} owner_at={owner_at} "
              f"count={st['count']} limit={limit} hour={_hour} "
              f"force_flag={bool(st.get('force'))} last_at_gap_min="
              f"{int((time.time() - float(st.get('last_at') or 0)) / 60)}")
        if owner_at:
            _note("目标是主人的 @：免额度/免间隔/免夜间限制")
        elif manual:
            _note(f"主人手动触发（trigger={trigger}）：本轮按 force 处理，免 MIN_GAP")
        elif _hour < 8 or _hour >= 23:
            _skip(FC_QUOTA_OR_NIGHT, f"夜间 {_hour} 点且不是主人的 @，按边界不自动回")
            return
        if not owner_at and not manual:
            if st["count"] >= limit:
                if fresh and time.time() - float(st.get("asked_at") or 0) > 6 * 3600:
                    it0 = fresh[0]
                    st["asked_at"] = time.time()
                    st["waiting"] = it0
                    _save(st)
                    try:
                        await get_bot().send_private_msg(
                            user_id=int(OWNER_QQ),
                            message=(
                                f"主人……今天抖音本鱼已经回了 {DAILY_LIMIT} 条，到额度了。\n"
                                f"刚刚又有人（{it0['sender']}）{it0['kind']}：{it0['text']}\n"
                                "还要本鱼回吗？你点头我就破例再回一条；不回我就装没看见捏。"
                            ),
                        )
                    except Exception as e:  # noqa: BLE001
                        _exc("给主人发额度询问", e)
                _skip(FC_QUOTA_OR_NIGHT,
                      f"今日额度用完 count={st['count']} limit={limit} owner_at={owner_at}")
                return
            if not st.get("force") and time.time() - float(st.get("last_at") or 0) < MIN_GAP:
                gap_min = int((MIN_GAP - (time.time() - float(st.get("last_at") or 0))) / 60)
                _skip(FC_QUOTA_OR_NIGHT, f"距上次回复还差 {gap_min} 分钟（间隔 ≥{MIN_GAP // 60} 分钟）")
                return

    it = fresh[0]
    key, comment = it["key"], it["text"]
    r = _CUR
    if r is not None:
        r.sender, r.kind, r.comment = it.get("sender", ""), it.get("kind", ""), comment
    _note(f"本轮目标 sender={it.get('sender')!r} kind={it.get('kind')} "
          f"comment={comment[:20]!r} key={key}")

    # t8：目标不可回（正文为空 / 「此内容不可见」/ 已删除）→ 不点回复、不白等，直接计数后放弃本轮
    unavail = _target_unavailable_reason(it)
    if unavail:
        n = _retry_bump(st, key)
        _fail(FC_TARGET_UNAVAILABLE,
              f"target_unavailable key={key} reason={unavail} retry={n}/{RETRY_LIMIT}")
        if n >= RETRY_LIMIT:
            _give_up(st, seen, key, FC_TARGET_UNAVAILABLE, n)
        return

    with _phase("recheck"):
        # 别的应用通知可能把界面顶走，操作前重新确认还在抖音
        if not _in_douyin():
            _note("界面被其他应用顶走，重新回到抖音")
            _ensure_interaction_page()
    with _phase("preview"):
        pre_title, pre_frame = await _preview_item_video(it)
    with _phase("refind"):
        root_after, why2 = _ensure_interaction_page()
        if root_after is None:
            code2 = FC_DUMP_FAILED if why2 == "dump_failed" else FC_INTERACTION_PAGE_FAILED
            _fail(code2, f"看完视频后没回到互动消息（原因={why2}）")
            # t10：退不回互动消息也按 key 计数（20:51/20:52 两轮就是同一条白跑两次）
            _count_failure(st, seen, key, code2)
            return
        items_after = _collect_interactions(root_after)
        it2 = next((x for x in items_after if x["key"] == key), None)
        if it2 is None:
            # 看完视频列表位置会变：回到顶部刷新一次，再按发送者+评论前几个字找
            root_after = _refresh_interaction_page() or root_after
            items_after = _collect_interactions(root_after)
            probe2 = re.sub(r"[^\w\u4e00-\u9fff]", "", comment)[:8]
            it2 = next((x for x in items_after if x["key"] == key), None)
            if it2 is None and probe2:
                it2 = next((x for x in items_after
                            if x.get("sender") == it.get("sender")
                            and probe2 in re.sub(r"[^\w\u4e00-\u9fff]", "", x.get("text", ""))), None)
        if it2 is None:
            _fail(FC_NO_TARGET, f"看完视频后找不到原 @ 条目：{comment[:20]}")
            return
        it = it2

    with _phase("open_reply"):
        if not _open_reply_box(it):
            # t8/t10：点回复失败也要计数，连续 3 次就写 seen 放弃，避免同一 key 每轮白烧 60~200s
            code = ((_CUR.failure_code if _CUR is not None else "") or FC_INPUT_NOT_FOUND)
            _count_failure(st, seen, key, code)
            _back_to_interactions()
            return

    with _phase("caption"):
        root = _dump("caption")
        caption = ""
        if root is not None:
            for n in _nodes(root):
                t = (n.get("text") or "").strip()
                if len(t) > 20 and ("#大肥鱼" in t or "#鲸鱼娘" in t or "鲸鱼" in t):
                    caption = t
                    break
                if len(t) > 40 and len(caption) < len(t) and "评论" not in t:
                    caption = t
        if pre_title:
            caption = pre_title
    with _phase("generate"):
        reply = await _generate_reply(caption, comment, pre_frame or "")
    if not reply:
        _fail(FC_REPLY_GENERATE_FAILED, "生成回复正文失败，本轮不发")
        _back_to_interactions()
        return

    # 插表情决策必须留痕（t5 补充）：决定了插/不插、为什么，全部进 events + 结构化记录
    with _phase("sticker_decide"):
        sticker, sticker_reason, sticker_detail = _pick_sticker_decision()
    if r is not None:
        r.sticker_reason = sticker_reason
        r.sticker_file = (sticker.name if sticker is not None else "")
    if sticker is None:
        _note(f"sticker_skipped reason={sticker_reason} {sticker_detail}",
              code="sticker_skipped", level="info")
    else:
        _note(f"sticker_ok file={sticker.name} folder={sticker.parent.name} {sticker_detail}",
              code="sticker_ok", level="info")
    ok = _compose_and_send(reply, sticker)  # 内含 input / sticker / send 阶段

    with _phase("state"):
        if ok:
            # 只有「确认发出」才走到这里：写 seen / count / last_at
            if it.get("sender") not in OWNER_NAMES:
                st["count"] += 1
            st["last_at"] = time.time()
            st["seen"] = (list(seen) + [key])[-200:]
            if st["count"] > DAILY_LIMIT and int(st.get("bonus") or 0) > 0:
                st["bonus"] = int(st.get("bonus") or 0) - 1
            st["waiting"] = None
            st["force"] = False
            _retry_clear(st, key)   # t8：成功后清掉该 key 的连续失败计数
            st["history"] = (st.get("history") or [])[-19:] + [{
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "sender": it["sender"], "comment": comment,
                "reply": reply, "kind": it["kind"],
                "video_title": caption[:120], "video_frame": pre_frame[:120],
            }]
            _save(st)
            if r is not None:
                r.outcome = "replied"
                r.failure_code = None
                r.failure_detail = ""
                r.reply = reply
            _note(f"已回复 {it.get('kind')}：{reply}")
            try:
                await get_bot().send_private_msg(
                    user_id=int(OWNER_QQ),
                    message=(f"📱 抖音已自动回复（{it['kind']}）\n"
                             f"{it['sender']}：{comment}\n"
                             f"看的是：{caption[:80] or '（没读到标题）'}\n"
                             f"画面：{(pre_frame or '')[:80] or '（没抓到画面）'}\n"
                             f"回复：{reply}"),
                )
            except Exception as e:  # noqa: BLE001
                _exc("给主人发出站通知", e)
        else:
            # 没确认发出：不记 seen，保留到下一轮重试（t2 根因 871-879 的收敛点）
            st["force"] = False
            _save(st)
            _note("本条没有确认发出，不写 seen，保留到下一轮重试", level="warning")

    with _phase("back"):
        _back_to_interactions()


_POLL_LOCK = asyncio.Lock()
_LAST_DONE_NOTICE = 0.0


async def _poll_once(trigger: str = "user") -> None:
    """跑一轮「看抖音」。trigger 进日志/结构化记录，并且决定闸门语义（t5 必改·trigger 归因）。

    trigger 取值：
    - "user"：主人在 QQ 里发「看抖音」手动查询（plugins/ai_group.py 调 _poll_once() 时用的默认值，
      那个文件不在本任务范围，所以把默认值定义成 user）→ 闸门按 force 处理，免 MIN_GAP；
    - "scheduler"：apscheduler 每 10 分钟自动轮询 → 走完整闸门；
    - "approve"：主人点头「破例再回一条」→ force + 免 MIN_GAP。
    无论成功、失败、跳过，收尾都写一条记录到 logs/douyin_runs.jsonl（唯一失败码可追溯）。
    """
    global _LAST_DONE_NOTICE, _CUR
    # 2026-11-07：手机不在线就什么都不做。
    # 定时轮询静默跳过（不写 warning），主人主动叫她时回一句「手机没连」。
    if not await asyncio.to_thread(_device_online):
        r_off = _Round(trigger)
        _CUR = r_off
        try:
            _skip(FC_DEVICE_OFFLINE, "手机不在线：adb devices 里没有 device")
        finally:
            _finish_round(r_off)
            _CUR = None
        if trigger != "scheduler":
            try:
                await get_bot().send_private_msg(
                    user_id=int(OWNER_QQ),
                    message="(*从手机那边抬头*) 连不上手机，无线调试没开着吧？开一下本鱼再去。",
                )
            except Exception as e:  # noqa: BLE001
                _exc("发出「手机没连」提示", e)
        return
    if _POLL_LOCK.locked():
        r_skip = _Round(trigger)
        _CUR = r_skip
        try:
            _skip(FC_CONCURRENT_SKIP, "上一轮还没结束，本轮跳过（锁被占用）")
        finally:
            _finish_round(r_skip)
            _CUR = None
        return
    _t0 = time.time()
    async with _POLL_LOCK:
        r = _Round(trigger)
        _CUR = r
        try:
            with _phase("round"):
                await _poll_once_impl()
        except asyncio.CancelledError:
            _fail(FC_CANCELLED, "本轮被取消")
            raise
        except _RoundBudgetExceeded as e:
            _fail(FC_ROUND_BUDGET, str(e))
        except Exception as e:  # noqa: BLE001
            _fail(FC_UNHANDLED, f"{type(e).__name__}: {e}")
            logger.warning(f"[抖音手机][{r.rid}] 本轮未预期异常：\n{traceback.format_exc()[:800]}")
        finally:
            _finish_round(r)
            _CUR = None
    # 只有在「真的回了一条评论」时才播报。
    # 失败/跳过/手机连不上 一律不发（主人 2026-10-31 要求：连不上就不要发）
    if r.outcome != "replied" or not r.reply:
        logger.info(f"[抖音手机][{r.rid}] 本轮 outcome={r.outcome}，不播报")
        return
    if time.time() - _t0 >= 30 and time.time() - _LAST_DONE_NOTICE > 600:
        _LAST_DONE_NOTICE = time.time()
        try:
            await get_bot().send_private_msg(
                user_id=int(OWNER_QQ),
                message="(*从手机里抬头*) 刚从抖音那边忙完回来，评论回完了。",
            )
        except Exception as e:  # noqa: BLE001
            _exc("发出「已忙完」通知", e)


@scheduler.scheduled_job("interval", minutes=10)
async def douyin_phone_poll():
    try:
        await _poll_once("scheduler")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[抖音手机] 轮询异常：{type(e).__name__}: {e}")


approve_cmd = on_message(priority=8, block=False)


@approve_cmd.handle()
async def _(event: MessageEvent):
    if not isinstance(event, PrivateMessageEvent):
        return
    if str(event.user_id) != OWNER_QQ:
        return
    st = _load()
    if st.get("waiting") and time.time() - float(st.get("asked_at") or 0) > 3600:
        st["waiting"] = None
        _save(st)
    text = event.get_plaintext().strip()
    explicit = any(k in text for k in ("可以回复抖音", "可以回抖音", "准你回抖音", "破例回抖音", "去回抖音"))
    if not explicit and not st.get("waiting"):
        return
    yes = ("可以", "回吧", "同意", "破例", "行", "好")
    no = ("不用", "不回", "算了", "别回", "不要")
    if explicit or any(k in text for k in yes):
        st["bonus"] = int(st.get("bonus") or 0) + 1
        st["waiting"] = None
        st["force"] = True
        _save(st)
        await approve_cmd.finish("(*点头*) 好，本鱼这就去回那条抖音，就破例这一次捏。")
        asyncio.create_task(_poll_once("approve"))
    elif any(k in text for k in no):
        st["waiting"] = None
        _save(st)
        await approve_cmd.finish("(*撇嘴*) 行吧，那本鱼装没看见。")
