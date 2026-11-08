"""每天定时给主人私聊发的「动态 / 说说」（不发 QQ 空间，只走私聊）。

设计口径（与主人确认）：
* 落点：主人私聊 `OWNER_QQ_ID`，不发群、不碰 QQ 空间；
* 频次：默认每天 08:00 / 13:00 / 21:00 各一条，触发后随机延迟 0~20 分钟，像真人节奏；
* 内容：调方舟大模型，注入私聊长期档案 + 自学档案 + 最近私聊记录，傲娇大肥鱼口吻自然融入记忆，
  不逐条背清单、不复述指令、不和最近发过的动态雷同；
* 本期只做定时动态 + 手动预览 / 发布 / 历史 / 开关，评论监听二期再说。

配置（.env，改完重启 bot 生效）：
* DAILY_MOOD_ENABLED        总开关，默认 true
* DAILY_MOOD_TIMES          时刻列表 HH:MM，逗号分隔，默认 "08:00,13:00,21:00"
* DAILY_MOOD_DELAY_MIN      触发后最小随机延迟分钟，默认 0
* DAILY_MOOD_DELAY_MAX      触发后最大随机延迟分钟，默认 20
* DAILY_MOOD_MAX_CHARS      每条字数上限，默认 40
* DAILY_MOOD_HISTORY_KEEP   去重保留最近多少条已发文案，默认 20

主人私聊指令：
* `/说说预览`（`/动态预览`）  生成一条但不发送，先看内容和记忆结合效果
* `/说说发布`（`/说说`、`/动态`、`/发说说`）  立即生成并发给主人
* `/说说历史`（`/动态历史`）  看最近发过的动态
* `/说说开关 开|关`（`/动态开关`）  切换定时任务开关，落盘到状态文件

状态文件：`memory/daily_mood.json`（开关 + 历史文案 + 当日已发时段标记）。
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import nonebot  # noqa: E402
from nonebot import on_message, get_bot  # noqa: E402
from nonebot.exception import FinishedException  # noqa: E402
from nonebot.adapters.onebot.v11 import PrivateMessageEvent  # noqa: E402

try:
    from nonebot_plugin_apscheduler import scheduler  # noqa: E402
except Exception:  # standalone / driver 未初始化时降级，不炸导入
    scheduler = None

from config import get_config  # noqa: E402
from plugins.ai_group import call_ai_raw  # noqa: E402
from plugins.activity import set_activity  # noqa: E402
from plugins import long_memory as ltm  # noqa: E402
from plugins import reply_log  # noqa: E402
from memory_manager import get_recent_memory  # noqa: E402

cfg = get_config()
logger = nonebot.logger

OWNER_QQ_ID = str(os.getenv("OWNER_QQ_ID", cfg.daily_mood.owner_qq or "100000000"))
STATE_FILE = ROOT / "memory" / "daily_mood.json"

# 时段语境：key 用于当日防重，label 给人看，hint 拼进 prompt
PERIODS: dict[str, dict[str, str]] = {
    "morning": {
        "label": "早上",
        "time_hint": "刚起床、赖床、打哈欠、跟主人说早安、惦记主人有没有吃饭",
    },
    "noon": {
        "label": "中午",
        "time_hint": "午后摸鱼、吐槽吃饭、犯困、天气热/冷、随口扯一句日常",
    },
    "night": {
        "label": "晚上",
        "time_hint": "一天结束前，从今天真实发生的事里挑一件你最想记住/最在意的，写一句只对主人说的话",
    },
}

_RUNNING = False  # 任务互斥：防止两轮定时并发发重复


# ------------------------------------------------------------------ 状态持久化

def _default_state() -> dict:
    return {"enabled": cfg.daily_mood.enabled, "history": [], "sent_today": {}}


def _load_state() -> dict:
    try:
        if STATE_FILE.exists():
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("enabled", cfg.daily_mood.enabled)
                data.setdefault("history", [])
                data.setdefault("sent_today", {})
                if not isinstance(data["history"], list):
                    data["history"] = []
                if not isinstance(data["sent_today"], dict):
                    data["sent_today"] = {}
                return data
    except (OSError, ValueError) as e:
        logger.warning(f"[每日动态] 状态读取失败，用默认值：{e}")
    return _default_state()


def _save_state(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        state["history"] = state["history"][-cfg.daily_mood.history_keep:]
        STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as e:
        logger.error(f"[每日动态] 状态保存失败：{e}")


def _enabled(state: dict) -> bool:
    return bool(state.get("enabled", cfg.daily_mood.enabled))


def _today_key() -> str:
    return time.strftime("%Y-%m-%d")


def _period_of_hour(hour: int) -> str:
    if hour < 11:
        return "morning"
    if hour < 17:
        return "noon"
    return "night"


# ------------------------------------------------------------------ 记忆拼接

def _memory_block(period: str) -> str:
    """拼入私聊档案 + 自学档案 + 最近私聊记录，任何一段取不到都降级为空串，不阻塞生成。"""
    parts: list[str] = []
    owner_scope = f"private_{OWNER_QQ_ID}"
    try:
        private_archive = ltm.archive_text(owner_scope)
        if private_archive.strip():
            parts.append(private_archive)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[每日动态] 私聊档案读取失败：{e}")

    try:
        learn_archive = ltm.archive_text(
            "global_learn",
            "【本鱼自学知识】以下是你上网学到的公开常识 / 热点，偶尔自然引用，别逐条背出来：",
        )
        if learn_archive.strip():
            parts.append(learn_archive)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[每日动态] 自学档案读取失败：{e}")

    try:
        recent = get_recent_memory(limit=6, scope=owner_scope)
        if recent:
            lines: list[str] = []
            for row in recent:
                who = "主人" if row.get("speaker") != cfg.persona.nickname else "本鱼"
                msg = (row.get("message") or "").strip()
                reply = (row.get("reply") or "").strip()
                if msg:
                    lines.append(f"{who}说：{msg[:60]}")
                if reply:
                    lines.append(f"本鱼回：{reply[:60]}")
            if lines:
                parts.append("【最近私聊聊过】\n" + "\n".join(lines[-6:]))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[每日动态] 最近私聊记录读取失败：{e}")

    return "\n\n".join(parts)


def _recent_posts_text(history: list[dict], n: int = 5) -> str:
    if not history:
        return ""
    tail = history[-n:]
    return "\n".join(f"· {h.get('text', '')}" for h in tail if h.get("text"))


# ------------------------------------------------------------------ 文案生成与清洗

# 提示词规则词 / 推理引导词：命中说明模型把指令复述或思考草稿吐进了正文，判无效。
_DRAFT_RULE_WORDS = (
    "要求", "指令", "标题", "引号", "编号", "markdown", "动作描写", "自称",
    "字数", "硬性要求", "复述", "正文一句话", "20~35", "20-35", "不要标题",
)
_DRAFT_PREFIX_RE = re.compile(
    r"^(需要发|应该输出|我们需要|让我来|可以写|任务[:：]|角色[:：]|分析一下|先思考)"
)


def _is_instruction_draft(text: str) -> bool:
    """判断返回内容是不是提示词复述 / 推理草稿（不是动态正文）。"""
    if not text:
        return True
    if _DRAFT_PREFIX_RE.match(text):
        return True
    return any(w in text.lower() for w in _DRAFT_RULE_WORDS)


def _clean(text: str, max_chars: int) -> str | None:
    """去掉指令复述、Markdown、方括号表情与多余空白；超限在标点处截断；空内容返回 None。"""
    if not text:
        return None
    text = text.strip()
    # 去掉代码块围栏标记（保留内部文案），首尾引号，方括号表情，Markdown 加粗/斜体
    text = re.sub(r"```[^\n]*", "", text)
    text = text.replace("```", "")
    text = re.sub(r"\[表情[:：][^\]]*\]", "", text)
    text = text.replace("**", "").replace("__", "")
    text = text.strip(" \t\r\n\"'“”‘’-—·*")
    text = " ".join(text.split())  # 折叠换行 / 多空格为单空格，不删中文之间的单空格
    text = re.sub(r"^(主人|本鱼)[:：]", "", text).strip()
    if not text or len(text) < 4:
        return None
    # 指令复述 / 推理草稿直接判无效，不进入截断流程
    if _is_instruction_draft(text):
        return None
    if len(text) > max_chars:
        cut = text[:max_chars]
        # 优先在句末标点处收住，避免半截词
        m = list(re.finditer(r"[，。！？、…~～！?，,]", cut))
        if m:
            cut = cut[:m[-1].start()]
        text = cut.strip("，。！？、…~～,.-— ")
    if len(text) < 4:
        return None
    return text


def _norm(text: str) -> str:
    return re.sub(r"[\s，。！？、…~～,.!?\"'“”‘’()（）*：:：]", "", text or "")


def _cmd_norm(text: str) -> str:
    """指令归一化：全角斜杠转半角、去首尾空白、折叠内部多余空格，用于分支容错匹配。"""
    t = (text or "").strip().replace("／", "/")
    return re.sub(r"\s+", " ", t)


def _is_duplicate(text: str, history: list[dict], n: int = 3) -> bool:
    cur = _norm(text)
    if not cur:
        return True
    for row in history[-n:]:
        old = _norm(row.get("text", ""))
        if old and (old == cur or old in cur or cur in old):
            return True
    return False


def _build_prompt(period: str, memory_block: str, recent_posts: str) -> str:
    p = PERIODS[period]
    mem_section = memory_block or "（暂时没有可参考的长期记忆，就正常碎碎念即可）"
    dedup_section = (
        "【最近已经发过的动态，严禁重复这些意思和句子】\n" + recent_posts
        if recent_posts else "（这是你第一次发动态，自由发挥）"
    )
    return (
        f"你是「蓝色大肥鱼」，一只傲娇又黏人的桌面桌宠，正在私聊主人。现在是{p['label']}。\n"
        f"请你以第一人称发一条自己的日常动态 / 说说，口吻像真人随手发的朋友圈：{p['time_hint']}。\n\n"
        f"{mem_section}\n\n"
        f"{dedup_section}\n\n"
        "【硬性要求】\n"
        "1. 只输出动态正文一句话，20~35 个字，不要标题、不要引号、不要编号、不要 Markdown、不要方括号表情；\n"
        "2. 傲娇少女第一人称，自称「本鱼」，可以带动作描写 (*xxx*)；口癖自然，别堆太多；\n"
        "3. 优先写今天真实发生过、你特别想记住或特别想对主人说的一件事；不要泛泛的日常碎碎念，也别逐条背清单或复述这份指令；\n"
        "4. 不和上面「最近已经发过的动态」重复；不说你是 AI / 机器人，不解释你在做什么；\n"
        "5. 不输出任何额外说明、前后缀寒暄，直接给正文。"
    )


async def _generate(period: str) -> str | None:
    state = _load_state()
    memory_block = _memory_block(period)
    recent_posts = _recent_posts_text(state["history"])
    prompt = _build_prompt(period, memory_block, recent_posts)

    for attempt in range(2):  # 失败重试两次就够，避免空烧
        try:
            raw = await call_ai_raw(prompt, "deepseek", temperature=0.95, max_tokens=400)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[每日动态] 第 {attempt + 1} 次生成异常：{e}")
            continue
        if not raw or not raw.strip():
            # 线上 byte 通道只取 content，推理占满额度时正文为空串（非兜底句），需单独判失败
            logger.warning(f"[每日动态] 第 {attempt + 1} 次大模型返回空正文（推理占满额度），重试")
            continue
        if raw.startswith("（本鱼"):  # call_ai_raw 失败兜底句
            logger.warning("[每日动态] 大模型返回失败兜底句，本轮跳过")
            return None
        text = _clean(raw, cfg.daily_mood.max_chars)
        try:
            from plugins.humanize import strip_cliches
            _hz = strip_cliches(text)
            if _hz and len(_hz) >= 10:
                text = _hz
        except Exception:  # noqa: BLE001
            pass
        if text and not _is_duplicate(text, state["history"]):
            return text
        logger.info(f"[每日动态] 第 {attempt + 1} 次结果重复或过短，重试")
    logger.warning("[每日动态] 3 次重试均未生成可用文案")
    return None


def _record(period: str, text: str) -> None:
    state = _load_state()
    state["history"].append(
        {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "period": period, "text": text}
    )
    today = _today_key()
    state["sent_today"].setdefault(today, [])
    if period not in state["sent_today"][today]:
        state["sent_today"][today].append(period)
    _save_state(state)


_LAST_QZONE_OK = False
_LAST_QZONE_MSG = ""


async def _send(period: str, dry_run: bool = False, force_qzone: bool = False) -> str | None:
    """生成一条动态。dry_run=True 只返回文案不发送、不入历史；False 则发主人私聊并落历史。"""
    global _RUNNING, _LAST_QZONE_OK, _LAST_QZONE_MSG
    _LAST_QZONE_OK = False
    _LAST_QZONE_MSG = ""
    if not dry_run:
        set_activity("写说说中", 180)
    if _RUNNING:
        logger.info("[每日动态] 上一轮还在跑，本轮跳过")
        return None
    _RUNNING = True
    try:
        text = await _generate(period)
        if not text:
            logger.info(f"[每日动态·{PERIODS[period]['label']}] 没生成出可用文案，本轮跳过")
            return None

        if dry_run:
            logger.info(f"[每日动态·预览] {text}")
            return text

        bot = get_bot()
        await bot.send_private_msg(user_id=int(OWNER_QQ_ID), message=text)
        _record(period, text)
        try:
            from plugins import qzone_sync
            # 说说只留 qzone_auto 一条发布通道（审计 C：三条通道各算各的预算，
            # 同一时段会重复发）。QZONE_AUTO_ENABLED 开着时，这条只发私聊不碰空间。
            _auto_on = os.getenv("QZONE_AUTO_ENABLED", "true").lower() in ("1", "true", "yes")
            if qzone_sync.load_state().get("enabled") and (force_qzone or not _auto_on):
                ok, msg = await qzone_sync.publish_qzone(text)
                _LAST_QZONE_OK, _LAST_QZONE_MSG = ok, msg
                logger.info(f"[每日动态·QQ空间] {msg}" if ok else f"[每日动态·QQ空间] 失败：{msg}")
                if not ok:
                    logger.warning(f"[每日动态·QQ空间] 没发出去：{msg}")
                    try:
                        await bot.send_private_msg(
                            user_id=int(OWNER_QQ_ID),
                            message=f"⚠️ 主人，QQ空间这条没发出去：{str(msg)[:100]}\n内容我先发你了。",
                        )
                    except Exception:
                        pass
                    await qzone_sync.notify_login_needed(bot, int(OWNER_QQ_ID), msg)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[每日动态·QQ空间] 异常：{e}")
        reply_log.record(
            group_id=0, speaker="主人", user_msg=f"定时动态·{PERIODS[period]['label']}",
            reply=text, kind="daily_mood",
        )
        logger.info(f"[每日动态·{PERIODS[period]['label']}] 已发给主人：{text}")
        return text
    except Exception as e:  # noqa: BLE001
        reply_log.record(
            group_id=0, speaker="主人", user_msg=f"定时动态·{PERIODS[period]['label']}",
            reply="", kind="daily_mood", error=f"{type(e).__name__}: {e}"[:200],
        )
        logger.warning(f"[每日动态] 发送失败：{type(e).__name__}: {e}")
        return None
    finally:
        _RUNNING = False


# ------------------------------------------------------------------ 定时任务

async def _scheduled_run(period: str) -> None:
    state = _load_state()
    if not _enabled(state):
        logger.info(f"[每日动态] 开关关闭，跳过 {PERIODS[period]['label']} 时段")
        return
    today = _today_key()
    if period in state["sent_today"].get(today, []):
        logger.info(f"[每日动态] 今天 {PERIODS[period]['label']} 已发过，跳过")
        return

    delay = random.uniform(cfg.daily_mood.delay_min, cfg.daily_mood.delay_max)
    if delay > 0:
        await asyncio.sleep(delay * 60)
    # 延迟后重新取状态，避免等待期间被手动开关改动 / 并发重复
    state = _load_state()
    if not _enabled(state):
        logger.info("[每日动态] 等待延迟后开关已关，取消本轮")
        return
    if period in state["sent_today"].get(today, []):
        logger.info("[每日动态] 等待延迟后发现本轮已发，取消")
        return

    await _send(period)


def _register_jobs() -> None:
    if scheduler is None:
        logger.warning("[每日动态] apscheduler 不可用，定时任务未注册")
        return
    if not cfg.daily_mood.enabled:
        logger.info("[每日动态] DAILY_MOOD_ENABLED=false，未注册定时任务")
        return
    for hh, mm in cfg.daily_mood.times:
        period = _period_of_hour(hh)
        job_id = f"daily_mood_{hh:02d}_{mm:02d}"
        scheduler.add_job(
            _scheduled_run, "cron", hour=hh, minute=mm,
            id=job_id, replace_existing=True, misfire_grace_time=300,
            args=[period],
        )
        logger.info(f"[每日动态] 已注册定时任务 {hh:02d}:{mm:02d}（{PERIODS[period]['label']}）")


@nonebot.get_driver().on_startup
async def _daily_mood_startup() -> None:
    _register_jobs()


# ------------------------------------------------------------------ 主人私聊指令

cmd = on_message(priority=20, block=False)


def _is_owner(user_id: str) -> bool:
    return str(user_id) == str(OWNER_QQ_ID)


def _state_panel(state: dict) -> str:
    on_off = "开" if _enabled(state) else "关"
    times = "、".join(f"{hh:02d}:{mm:02d}" for hh, mm in cfg.daily_mood.times)
    hist_n = len(state["history"])
    return (
        f"🐟 **每日动态面板**\n"
        f"开关：{on_off}\n"
        f"定时：{times}（延迟 {cfg.daily_mood.delay_min}~{cfg.daily_mood.delay_max} 分钟）\n"
        f"已发历史：{hist_n} 条\n"
        f"指令：`/说说预览` 预览 · `/说说发布` 立即发 · `/说说历史` 看记录 · `/说说开关 开/关`"
    )


@cmd.handle()
async def _(event: PrivateMessageEvent):
    if not _is_owner(str(event.user_id)):
        return
    raw_text = event.get_plaintext().strip()
    if not raw_text:
        return
    text = _cmd_norm(raw_text)
    # 诊断日志：记录原始文本与归一化结果，线上可据此定位指令是否进入本插件、命中哪个分支
    logger.info(f"[每日动态·指令] uid={event.user_id} 原文={raw_text!r} 归一={text!r}")

    aliases = ("/说说", "/动态", "/朋友圈", "/发说说")
    if text not in aliases and not text.startswith(("/说说", "/动态", "/朋友圈")):
        # 生活化说法：「去QQ空间发条说说」「写条今天的动态」
        if (any(k in text for k in ("说说", "动态", "朋友圈"))
                and any(k in text for k in ("空间", "发", "写"))
                and str(event.user_id) == str(OWNER_QQ_ID)):
            period = _period_of_hour(time.localtime().tm_hour)
            sent = await _send(period, dry_run=False, force_qzone=True)
            if sent and _LAST_QZONE_OK:
                await cmd.finish("(*甩尾巴*) 写好并同步到 QQ 空间啦，主人去看看～")
            elif sent:
                await cmd.finish(f"(*耷拉尾巴*) 文案写好了，但空间没发出去：{str(_LAST_QZONE_MSG)[:80]}")
            else:
                await cmd.finish("(*耷拉尾巴*) 这次没憋出来，主人稍后再叫我。")
            return
        return

    state = _load_state()
    n = _norm(text)  # 去掉标点空白后的宽松串，容错中间空格、全角斜杠

    # 面板：/说说、/动态 不带参数时给状态面板（先发面板，再按子指令分流）
    if text in ("/说说", "/动态", "/朋友圈"):
        await cmd.finish(_state_panel(state))
        return

    # 开关
    if n.startswith(_norm("/说说开关")) or n.startswith(_norm("/动态开关")):
        arg = text.split()[-1].lower() if len(text.split()) > 1 else ""
        if arg in ("开", "on", "true", "1", "yes"):
            state["enabled"] = True
            _save_state(state)
            await cmd.finish("(*点头*) 每日动态打开啦，到点本鱼就来跟主人碎碎念捏～")
            return
        if arg in ("关", "off", "false", "0", "no"):
            state["enabled"] = False
            _save_state(state)
            await cmd.finish("(*委屈巴巴*) 好吧，本鱼先闭嘴，想让我说话就发 `/说说开关 开`")
            return
        await cmd.finish(f"用法：`/说说开关 开` 或 `/说说开关 关`\n当前：{'开' if _enabled(state) else '关'}")
        return

    # 历史
    if n in (_norm("/说说历史"), _norm("/动态历史")):
        if not state["history"]:
            await cmd.finish("(*挠头*) 还没发过动态捏，先发一条看看嘛～")
            return
        tail = state["history"][-10:]
        lines = [f"🕐 {h.get('ts', '')} · {PERIODS.get(h.get('period', ''), {}).get('label', '')}\n{h.get('text', '')}"
                 for h in reversed(tail)]
        await cmd.finish("🐟 **最近动态记录**\n" + "\n\n".join(lines))
        return

    # 预览（不发布、不入历史）
    if n in (_norm("/说说预览"), _norm("/动态预览")):
        period = _period_of_hour(time.localtime().tm_hour)
        preview = await _send(period, dry_run=True)
        if not preview:
            await cmd.finish("(*扭捏*) 本鱼这会儿没憋出合适的话，主人稍后再试嘛…")
            return
        await cmd.finish(
            f"🔍 **预览（没发出去，不入历史）**\n{preview}\n\n"
            f"字数 {len(preview)}，满意就发 `/说说发布`"
        )
        return

    # 立即发布（容错：/说说发布、/动态发布、/发说说，以及中间多空格的 /说说 发布）
    if n in (_norm("/说说发布"), _norm("/动态发布"), _norm("/发说说")):
        period = _period_of_hour(time.localtime().tm_hour)
        # 主人主动说"发说说" = 明确要发到 QQ 空间，不再只发私聊
        sent = await _send(period, dry_run=False, force_qzone=True)
        if not sent:
            await cmd.finish("(*耷拉尾巴*) 这次没发出去，主人稍后再试嘛…")
            return
        if _LAST_QZONE_OK:
            _reply = "(*蹦跶*) 已经发到空间啦～ 你刷新一下看看"
            try:
                from plugins import ai_group
                ai_group.note_private_exchange("/发说说", _reply)
            except Exception:  # noqa: BLE001
                pass
            await cmd.finish(_reply)
        else:
            reason = str(_LAST_QZONE_MSG or "未知原因")[:80]
            _reply = (f"(*耷拉尾巴*) 文案写好了，但空间没发出去：{reason}\n"
                      "内容先发你私聊了，登录过期的话回我一句「扫码」。")
            try:
                from plugins import ai_group
                ai_group.note_private_exchange("/发说说", _reply)
            except Exception:  # noqa: BLE001
                pass
            await cmd.finish(_reply)
        return

    await cmd.finish(_state_panel(state))
