import asyncio
from datetime import datetime
import base64
import hashlib
import io
import random
import re
import sqlite3
import subprocess
import sys
import time
import os
import shutil
import pathlib
import json
import logging
import tempfile
import threading
import uuid
import edge_tts
from collections import defaultdict, deque, OrderedDict, Counter
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

Image.MAX_IMAGE_PIXELS = 5000 * 5000

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# 出站去重：同一目标 25 秒内发过一模一样的文字就不再发。
# 防的是「同一条入站事件被两个 matcher / 不同路径各回一次」这类重复，
# 而不是防她故意复读（短句 <6 字不拦）。
# ---------------------------------------------------------------------------
_OUTBOX_RECENT: dict = {}

# --- 运行令牌：同一会话新消息到达 → 旧回合作废（借鉴 SeekMemory RunControl）---
_RUN_SEQ: dict[str, int] = {}
_RUN_TS: dict[str, float] = {}
_RUN_USER: dict[str, str] = {}
_LAST_USER_TS: dict[str, float] = {}


def _new_run(conv: str, who: str = "") -> int:
    """开始一轮：序号 +1，返回本轮令牌；比它旧的回合作废。"""
    _RUN_SEQ[conv] = _RUN_SEQ.get(conv, 0) + 1
    _RUN_TS[conv] = time.time()
    _RUN_USER[conv] = str(who or "")
    return _RUN_SEQ[conv]


def _superseded(conv: str, tok: int, who: str = "", window: float = 60.0) -> bool:
    """这一轮生成期间有没有**同一个人**的更新消息把它顶掉（60 秒内生效）。

    只在"同一个发送者连着说"时作废旧回话；别人插话不作废（不然 @ 她的回复会被别人闲聊顶掉）。
    """
    try:
        return (_RUN_SEQ.get(conv, tok) != tok
                and str(_RUN_USER.get(conv) or "") == str(who or "")
                and time.time() - _RUN_TS.get(conv, 0) < window)
    except Exception:  # noqa: BLE001
        return False



def _outbox_duplicate(target: str, text: str, window: float = 25.0) -> bool:
    core = (text or "").strip()
    if len(core) < 6:
        return False
    now = time.time()
    key = (str(target), core)
    last = _OUTBOX_RECENT.get(key)
    if last and now - last < window:
        return True
    _OUTBOX_RECENT[key] = now
    if len(_OUTBOX_RECENT) > 800:
        for _k in [k for k, v in list(_OUTBOX_RECENT.items()) if now - v > 180]:
            _OUTBOX_RECENT.pop(_k, None)
    return False
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from nonebot import on_message, on_notice, on_request, get_bot, logger as nonebot_logger  # noqa: E402
from nonebot.exception import FinishedException  # noqa: E402
from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent, PrivateMessageEvent, GroupRequestEvent,
    GroupDecreaseNoticeEvent, GroupRecallNoticeEvent,
    Message, MessageSegment
)  # noqa: E402

from config import get_config  # noqa: E402
from nonebot_plugin_apscheduler import scheduler
from memory_manager import (
    add_memory, get_affinity, update_affinity,
    get_tag, set_tag, get_last_message, get_user_recent_messages,
    get_recent_memory, get_today_memory, get_affinity_extremes, count_memory,
    DB_PATH,
)
import token_meter  # 令牌用量累计（静默容错）
from long_memory import save_daily, detect_memory_intent, recall as recall_memory
from plugins import long_memory as ltm  # 档案式长期事实（group_{gid} / private_{uid} 隔离）
from plugins import group_lexicon as glx  # 群聊词条库（黑话/昵称/梗，计数 + 攒批抽词）
from plugins import web_search  # 聊天按需联网 + 主动自学（方舟 Responses web_search）
from plugins import group_guard as gguard  # 群发言权限（主人批准才在群里说话）

cfg = get_config()
logger = nonebot_logger  # 接入 nonebot 框架日志，INFO/WARNING/ERROR 都会落到 out.log（标准 logging.getLogger 默认 WARNING 会吞掉 info）

# 历史上下文：改成「只增不改」的真实多轮（2026-10-25 前缀缓存改造，依据 t1 诊断）。
# 旧实现是 deque(maxlen=history_max) + 每轮 [-history_use:] 滑动窗口 + 整段重拼成一条
# user 文本 —— 每轮历史第一个字就变了，历史段永远命中不了 DeepSeek 前缀缓存。
# 现在：内存里只追加，渲染成真实 user/assistant 多轮，只有超预算才从头部压缩一次。
_history: dict = defaultdict(list)
# 2026-11-05：人味加法层的字数累计（语病配额按这个算，每攒满 800 字放一处）
_HUMAN_CHARS = 0
# 2026-11-05：每个会话最近若干条她自己回复的句子，供「自我重复检测」比对
_RECENT_SENTS: dict = defaultdict(list)
_history_loaded: set = set()  # 已从 SQLite 回填过历史的 scope（重启后只回填一次）
# 可缓存历史预算（token 估值）：命中缓存的价格是输入价的 1/10，历史做大反而更省；
# 同时 chat 的 prompt 分母被撑大，全站命中率才算得上去（t1 §5 算术 / t2 §6.4）。
# 2026-10-28 t3：私聊 9000→12000。稳态命中 = 1 − T/P，T（易变尾巴+最新一句）实测 ≈385 tok，
# P 中位 10.5K → 96.3%；P→14K 才稳定站上 97%。这是设计里"A 落地后再拉长历史"的那一步，
# 一行回滚：.env 写 HISTORY_TOKEN_BUDGET_PRIVATE=9000。
HISTORY_TOKEN_BUDGET_PRIVATE = int(os.getenv("HISTORY_TOKEN_BUDGET_PRIVATE", "12000") or "12000")
HISTORY_TOKEN_BUDGET_GROUP = int(os.getenv("HISTORY_TOKEN_BUDGET_GROUP", "7000") or "7000")
# 重启回填条数：一次读够，回填后立刻按预算裁成"最近的一段"（之后只追加、不滑动）
HISTORY_BACKFILL_ROWS = int(os.getenv("HISTORY_BACKFILL_ROWS", "400") or "400")


def _est_tokens(text: str) -> int:
    """粗估 token：中文按 0.7 tok/字（t1 用真实 usage 校准过，误差 <10%）。"""
    return int(len(text or "") * 0.7) + 4


def _fp8(text: str) -> str:
    """内容指纹前 8 位（sha1）：给 stable 里的慢变块打指纹，量化它们多久变一次。"""
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:8]


def _history_tokens(hist) -> int:
    return sum(_est_tokens(t) for _, t, _ in hist)


def _history_backfill_rows() -> int:
    """回填条数：至少 cfg 配的 HISTORY_MAX，正常用 HISTORY_BACKFILL_ROWS 一次读够。"""
    return max(int(cfg.context.history_max or 0), HISTORY_BACKFILL_ROWS)


# ---------------- 状态类自述对账（2026-11-05，主人选的方案 B）----------------
# 现象：她 10:06 随口编了「本鱼这半碗粥还搁着呢」，这句进了历史，
# 之后每轮都当成「我真的还端着碗」接着提，还自己加戏「说好就一口」。
# 这里在写入历史前对账 meal_state：对不上的「还剩/没吃完」类自述直接删掉，
# 只删历史副本，已经发出去的消息不动。
_FOOD_WORD_RE = re.compile(r"(粥|饭|面|汤|菜|奶|茶|咖啡|零食|饼干|面包|薯片|水果|包子|饺子|馒头|酒|饮料)")
# 「还剩 / 还搁着 / 没吃完」这类**持续状态**才是会自我循环的，单次「我吃了」不拦
_LEFTOVER_RE = re.compile(r"(还剩|还搁着|还搁那|搁着呢|没吃完|没喝完|先喝完|先吃两口|扒两口|只剩半)")


def _meal_state_recent(minutes: float = 25.0) -> bool:
    """最近一顿是不是刚吃过（刚吃完说「还剩半碗」是合理的，不删）。"""
    try:
        st = json.loads((ROOT / "memory" / "meal_state.json").read_text(encoding="utf-8"))
        ts = str((st.get("last_meal") or {}).get("ts") or "")
        if not ts:
            return False
        return (time.time() - time.mktime(time.strptime(ts, "%Y-%m-%d %H:%M:%S"))) < minutes * 60
    except Exception:  # noqa: BLE001
        return False


def _strip_fake_state(text: str) -> str:
    """删掉「还剩半碗粥」这类与真实状态对不上的自述。只动历史副本。"""
    t = text or ""
    if not t or not _LEFTOVER_RE.search(t):
        return t
    if _meal_state_recent():
        return t                      # 刚吃过，真有剩的是合理的
    # 按句切，命中「未吃完 + 食物」的句子整句删
    parts = re.split(r"(?<=[。！？\n])", t)
    kept, dropped = [], []
    for seg in parts:
        if _LEFTOVER_RE.search(seg) and _FOOD_WORD_RE.search(seg):
            dropped.append(seg.strip())
            continue
        kept.append(seg)
    out = "".join(kept).strip()
    if not out or len(out) < max(8, len(t) * 0.3):
        return t                      # 删太狠就保留原样，宁可留着也别把回复清空
    if dropped:
        logger.info(f"[对账] 历史里删掉虚构状态自述：{' '.join(dropped)[:40]}")
    return out


def _compact_history(gid, budget_tokens: int) -> None:
    """历史只在超预算时从头部压缩（宁可少带历史，也不要每轮变长度）。

    头部被裁 = 本次请求前缀从第一个被裁的 token 起全失效（一次冷调用），
    所以用"100% 才触发、压到 70%"的滞回：9000 tok 预算大约 50 轮才压一次。
    """
    hist = _history.get(gid)
    if not hist:
        return
    total = _history_tokens(hist)
    if total <= budget_tokens:
        return
    target = int(budget_tokens * 0.7)
    dropped = 0
    while len(hist) > 1 and total > target:
        _, text, _ = hist.pop(0)
        total -= _est_tokens(text)
        dropped += 1
    logger.info(
        f"[缓存] 历史超预算 {gid}：压掉最旧 {dropped} 条，剩 {len(hist)} 条 ≈{total} tok"
        "（本次请求前缀失效一次，属预期）"
    )


def _msg_text(x) -> str:
    """从 Message / MessageSegment / str 里取纯文本。"""
    try:
        if isinstance(x, str):
            return x
        segs = getattr(x, "__iter__", lambda: [])()
        out = []
        for seg in segs:
            if isinstance(seg, str):
                out.append(seg)
            else:
                d = getattr(seg, "data", None) or {}
                out.append(str(d.get("text") or ""))
        return "".join(out)
    except Exception:  # noqa: BLE001
        return ""


_PUNCT_LAST: dict = defaultdict(list)      # gid -> [最近几句标点回复]
_PUNCT_COOLDOWN: dict = defaultdict(float) # gid -> 下次允许时间
_PUNCT_FALLBACK = (
    "(*尾巴一歪*) 光甩个问号，本鱼又不是你肚子里的蛔虫。",
    "这符号打得挺有气势的，可惜本鱼读不出来。",
    "(*) 你这一下比群里刷屏还神秘。",
    "嗯？有想法就直说，别拿标点糊弄本鱼。",
    "本鱼猜你在打哑谜，可惜今天没心情解谜。",
)


async def _punct_reply(gid, text: str, uid: str) -> str:
    """群里有人只发标点时的回复：现场生成，不重复固定句。"""
    hint = random.choice((
        "可以吐槽他没头没尾", "可以反问他想干嘛", "可以装傻说看不出来",
        "可以阴阳他一句，但别脏话", "可以顺口提一句你现在在干嘛",
    ))
    recent = "；".join(m[1][:20] for m in list(_history[gid])[-3:]) or "（没上下文）"
    last = "；".join(_PUNCT_LAST[gid][-3:]) or "（无）"
    prompt = (
        "你在 QQ 群里，是「蓝色大肥鱼」——圆滚滚的蓝色鲸鱼拟人少女，傲娇嘴损但心软，"
        "说话短、口语、有脾气，不自称 AI。"
        f"群里有人（不是主人）只发了一个标点：{text!r}，没有任何文字内容。"
        f"最近群里说过：{recent}\n"
        f"你之前已经说过这些了，别再重复：{last}\n"
        f"这次{hint}。"
        "要求：10~25 字，一句话，不要括号里的动作描写也可以有 1 处，"
        "不要总是问『什么意思』，不要解释规则，只输出正文。"
    )
    try:
        out = await call_ai_raw(prompt, GROUP_AI_STATE.get(gid, "deepseek"), temperature=1.0, max_tokens=120)
        out = str(out or "").strip().strip('"').replace("\n", " ")
        if out and "AI 调用失败" not in out:
            for old in _PUNCT_LAST[gid]:
                if _bigram_sim(old, out) > 0.75:   # 太像上一句就换掉
                    out = ""
                    break
            if out:
                _PUNCT_LAST[gid] = (_PUNCT_LAST[gid] + [out])[-5:]
                return out[:60]
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[群聊] 标点回复生成失败：{type(e).__name__}: {e}")
    out = random.choice(_PUNCT_FALLBACK)
    _PUNCT_LAST[gid] = (_PUNCT_LAST[gid] + [out])[-5:]
    return out


def _bigram_sim(a: str, b: str) -> float:
    ga = {a[i:i + 2] for i in range(max(0, len(a) - 1))}
    gb = {b[i:i + 2] for i in range(max(0, len(b) - 1))}
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / max(1, min(len(ga), len(gb)))


def _install_cmd_history_recorder(matcher, hist_gid: str, user_text: str) -> None:
    """指令类消息：把「主人说的话 + 她回的每一条」写进历史。

    以前指令回复不走聊天流程、不写历史，主人下一句"看到了"她就接不上。
    只记前 80 字，控制 token；同一句不重复记。
    """
    try:
        _hist = _history[hist_gid]
        if not any(h[0] in ("主人",) and h[1] == user_text[:80] and not h[2] for h in _hist[-4:]):
            _hist.append(("主人", user_text[:80], False))

        async def _record_and_call(orig, arg, *rest, **kw):
            txt = _msg_text(arg)[:80]
            if txt and not (txt == user_text[:80]):
                if not any(h[2] and h[1] == txt for h in _hist[-4:]):
                    _hist.append((NICKNAME, txt, True))
            return await orig(arg, *rest, **kw)

        _orig_send = matcher.send
        _orig_finish = matcher.finish

        async def _send(*a, **k):
            return await _record_and_call(_orig_send, *a, **k) if a else await _orig_send(*a, **k)

        async def _finish(*a, **k):
            return await _record_and_call(_orig_finish, *a, **k) if a else await _orig_finish(*a, **k)

        matcher.send = _send          # type: ignore[method-assign]
        matcher.finish = _finish      # type: ignore[method-assign]
    except Exception:  # noqa: BLE001
        pass

def note_private_exchange(user_msg: str, reply: str, hist_gid: str | None = None) -> None:
    """把一次"指令型"对话补进私聊历史。

    指令（/发说说、/签到…）的回复不走正常聊天流程，以前不写历史，
    于是主人下一句"看到了"她就完全不知道在说什么。
    """
    try:
        key = hist_gid or f"private_{YOUR_QQ_ID}"
        if str(user_msg).strip():
            _history[key].append((("主人"), str(user_msg)[:cfg.context.max_len_per_msg], False))
        if str(reply).strip():
            _history[key].append((NICKNAME, str(reply)[:cfg.context.max_len_per_msg], True))
    except Exception:  # noqa: BLE001
        pass

# ---------------------------------------------------------------- 手表（北京时间）
def _bj_now() -> "datetime":
    """当前北京时间（Asia/Shanghai）；没有 tzdata 时退回本机时间。"""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Shanghai"))
    except Exception:  # noqa: BLE001
        return datetime.now()


def _period_cn(hour: int) -> str:
    if hour < 5:
        return "凌晨"
    if hour < 9:
        return "早上"
    if hour < 12:
        return "上午"
    if hour < 14:
        return "中午"
    if hour < 18:
        return "下午"
    if hour < 23:
        return "晚上"
    return "深夜"


def _time_reply() -> str:
    n = _bj_now()
    wd = "一二三四五六日"[n.weekday()]
    return f"(*抬腕看表*) 北京时间 {n:%Y-%m-%d} {n:%H:%M}，星期{wd}，{_period_cn(n.hour)}。"

def _time_block() -> str:
    """给模型看的时间块：只认北京时间，不许自己换算/瞎猜。"""
    n = _bj_now()
    wd = "一二三四五六日"[n.weekday()]
    return (
        f"【手表】现在是北京时间 {n:%Y-%m-%d} {n:%H:%M}，星期{wd}，{_period_cn(n.hour)}。"
        "只用这个时间回答；别人问几点/今天几号/星期几就照这个说，不要猜、不要换算别的时区，"
        "也不要说自己没表。"
    )

def _history_messages(is_private: bool, hist) -> list[dict]:
    """历史 → 真实多轮 messages（user=对方 / assistant=本鱼自己）。

    逐字节稳定是硬要求：同一批历史每次渲染必须完全一致（不写时间戳、不写序号、
    不按轮次重排），否则上一轮存下的前缀缓存就白存。连续同一角色合并进上一条
    （只往末尾追加、不重排），保证 user/assistant 交替。
    """
    out: list[dict] = []
    limit = cfg.context.max_len_per_msg
    for name, text, is_bot in hist:
        text = (text or "")[:limit]
        if not text:
            continue
        if is_bot:
            role, content = "assistant", text
        else:
            role = "user"
            content = f"主人说：{text}" if is_private else f"【{name or '群友'}】说：{text}"
        if out and out[-1]["role"] == role:
            out[-1]["content"] = out[-1]["content"] + "\n" + content
        else:
            out.append({"role": role, "content": content})
    return out


def _compose_chat_messages(stable_prompt: str, hist_msgs: list, extra_blocks: list,
                           volatile_prompt: str, current_content) -> list[dict]:
    """统一聊天消息布局（顺序 = 缓存优先级）。

    system(稳定前缀) → 历史真实多轮(只增不改) → 回忆/群聊近况等动态块
    → 易变状态块(今日状态/心情/关系/性别/别复读/抖音) → 最新一条 user。

    前两段必须逐字节稳定；任何每轮都会变的内容只能出现在动态块或最新 user 里，
    否则会把整段历史拖出缓存区（t1：这是"只改一半等于白改"的原因）。
    """
    messages: list[dict] = []
    if stable_prompt:
        messages.append({"role": "system", "content": stable_prompt})
    messages.extend(hist_msgs or [])
    for block in (extra_blocks or []):
        if block:
            messages.append({"role": "system", "content": block})
    if volatile_prompt:
        messages.append({"role": "system", "content": volatile_prompt})
    # 时间块放最尾巴：每轮都会变，放前面会把整段历史拖出缓存
    messages.append({"role": "system", "content": _time_block()})
    messages.append({"role": "user", "content": current_content})
    return messages
_last_at: dict = {}
_last_stable_fp: dict = {}   # (gid,channel) → 上次稳定前缀指纹，用于告警「前缀变了=全量冷启动」
RECALL_CACHE = OrderedDict()
EMOJI_CACHE = {}

RECALL_LOCK = asyncio.Lock()

# 凌晨（0~6 点）非主人 @ 她时，静默不理的概率。2026-11-08 主人定 0.5。
_LATE_NIGHT_SILENT = 0.5
RECALL_TASK_RUNNING = set()
RECALL_BATCH = defaultdict(list)

GROUP_COOLDOWN = defaultdict(lambda: 0)
PRIVATE_COOLDOWN = defaultdict(lambda: 0)
FONT_CACHE = {}

# 🛑 群聊主动发言防连发：冷却窗口 + 每日条数上限
# 口径：只约束「没人@时的概率主动插嘴 / 复读机嘲讽 / 定时主动开口」；
# 被@、指令、私聊、游戏（赛马/决斗等）、退群威胁/坏消息/标点/记仇等事件触发型一律不受限。
PROACTIVE_COOLDOWN_SEC = int(os.getenv("PROACTIVE_COOLDOWN_SEC", "90") or "90")
PROACTIVE_DAILY_LIMIT = int(os.getenv("PROACTIVE_DAILY_LIMIT", "8") or "8")
# 2026-11-06：主动发图分两类 —— 目录里的图（R-18/表情包）不限量，发文件而已；
# 只有现场画（ComfyUI）限每天几张，因为要吃显存。
SELF_DRAW_DAILY_MAX = int(os.getenv("SELF_DRAW_DAILY_MAX", "2") or "2")
GROUP_PROACTIVE_COOLDOWN = defaultdict(lambda: 0.0)
GROUP_PROACTIVE_DAILY = defaultdict(lambda: {"date": "", "count": 0})
GROUP_USER_HOURLY = defaultdict(list)  # (gid,uid) -> [ts,...] 每人每小时最多 10 次

# 全局情绪与活跃群追踪
DAILY_MOODS = ["emo", "想吃肉", "嘴臭", "摸鱼", "开心", "暴躁", "社恐"]
DAILY_PERSONA_FILE = ROOT / "memory" / "daily_persona.json"


def _today_key() -> str:
    return time.strftime("%Y-%m-%d")


def _default_persona() -> dict:
    return {"date": _today_key(), "mood": "摸鱼", "score": 50, "diss": 0, "happy": 0, "updated_at": ""}


def _load_daily_persona() -> dict:
    """今日人设：从文件读，跨重启保持；跨天自动重置为“正常/摸鱼”。"""
    try:
        data = json.loads(DAILY_PERSONA_FILE.read_text(encoding="utf-8"))
    except Exception:
        return _default_persona()
    if not isinstance(data, dict) or data.get("date") != _today_key():
        return _default_persona()
    data.setdefault("score", 50)
    data.setdefault("diss", 0)
    data.setdefault("happy", 0)
    if data.get("mood") not in DAILY_MOODS:
        data["mood"] = "摸鱼"
    return data


def _save_daily_persona(data: dict) -> None:
    try:
        DAILY_PERSONA_FILE.parent.mkdir(parents=True, exist_ok=True)
        DAILY_PERSONA_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[今日人设] 保存失败：{e}")


DAILY_PERSONA = _load_daily_persona()
CURRENT_DAILY_MOOD = DAILY_PERSONA.get("mood", "摸鱼")


EMOTIONS = ["开心", "害羞", "烦", "怒", "丧", "累", "害怕"]
# 每个维度的基线：情绪不是只涨不落，会随时间往基线回落
# 借鉴 AstrBot 心情插件的「即时心境自然自愈」+「耦合矩阵」，本地实现、不调模型、不花 token
EMOTION_BASE = {"开心": 30, "害羞": 8, "烦": 10, "怒": 10, "丧": 7, "累": 25, "害怕": 5}
EMOTION_HALF_LIFE_MIN = 90.0  # 90 分钟后，离基线的差距衰减一半
EMOTION_COUPLING = [  # (源, 目标, 系数)：源偏离基线时，按比例带动目标
    ("怒", "开心", -0.20), ("怒", "烦", 0.10),
    ("累", "烦", 0.12), ("累", "开心", -0.10),
    ("烦", "丧", 0.12),
    ("害怕", "丧", 0.10),
    ("开心", "烦", -0.15), ("开心", "丧", -0.10),
    ("害羞", "开心", 0.10),
]
EMOTION_FILE = ROOT / "memory" / "emotion_state.json"


def _read_emotion_state() -> dict:
    try:
        return json.loads(EMOTION_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _load_emotion() -> dict:
    # 内分泌层接管后：七维情绪由 5 个激素派生（见 docs/内分泌系统-方案A.md）
    try:
        from plugins import hormones
        if hormones.ENABLED:
            d = hormones.derive_emotions()
            return {k: float(d.get(k, EMOTION_BASE.get(k, 0))) for k in EMOTIONS}
    except Exception:  # noqa: BLE001
        pass
    vals = _read_emotion_state().get("values") or {}
    out = dict(EMOTION_BASE)
    for k in EMOTIONS:
        try:
            out[k] = max(0.0, float(vals.get(k, EMOTION_BASE[k])))
        except (TypeError, ValueError):
            pass
    return out


def _emotion_last_ts() -> float:
    """上次情绪更新的时间戳。

    老文件里只有 updated_at 字符串、没有 updated_ts，这里要能回退解析，
    否则衰减永远按"假设刚过 5 分钟"算，情绪就一直是旧的、面板转盘不动。
    """
    data = _read_emotion_state()
    try:
        ts = float(data.get("updated_ts") or 0)
        if ts > 0:
            return ts
    except (TypeError, ValueError):
        pass
    raw = str(data.get("updated_at") or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return time.mktime(time.strptime(raw, fmt))
        except (ValueError, OverflowError):
            continue
    return 0.0


def _decay_to_base(vals: dict, minutes: float) -> dict:
    """半衰期回归基线：离基线越远回落越快，落到底就待在基线附近。"""
    if minutes <= 0:
        return vals
    f = 0.5 ** (minutes / EMOTION_HALF_LIFE_MIN)
    for k in EMOTIONS:
        vals[k] = EMOTION_BASE[k] + (vals[k] - EMOTION_BASE[k]) * f
    return vals


def _apply_coupling(vals: dict) -> dict:
    """耦合矩阵：一方偏离基线会带动另一方（怒↑→开心↓、累↑→烦↑、开心↑→烦↓…）。

    用同一时刻的偏离量快照计算，避免边算边改导致顺序依赖。
    """
    delta = {k: vals[k] - EMOTION_BASE[k] for k in EMOTIONS}
    for src, dst, coef in EMOTION_COUPLING:
        vals[dst] += coef * delta[src] * 0.2
    return vals


def _save_emotion(vals: dict) -> dict:
    total = sum(vals.get(k, 0) for k in EMOTIONS) or 1.0
    pct = {k: round(vals.get(k, 0) * 100.0 / total, 1) for k in EMOTIONS}
    old = _read_emotion_state()
    payload = {"values": pct, "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "updated_ts": time.time()}
    if old.get("transient"):
        payload["transient"] = old["transient"]
    try:
        EMOTION_FILE.parent.mkdir(parents=True, exist_ok=True)
        EMOTION_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[情绪] 保存失败：{e}")
    return pct


def set_transient(label: str, minutes: float = 45) -> None:
    """设一个「当下心境」：只维持几十分钟，过后自动失效，不死锁人设。"""
    data = _read_emotion_state()
    data["transient"] = {"label": str(label)[:24], "exp_ts": time.time() + minutes * 60}
    try:
        EMOTION_FILE.parent.mkdir(parents=True, exist_ok=True)
        EMOTION_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[情绪] 心境保存失败：{e}")


def transient_prompt() -> str:
    """把未过期的当下心境拼成一行，给 prompt 的易变尾部用。"""
    t = _read_emotion_state().get("transient") or {}
    label = str(t.get("label") or "")
    try:
        exp = float(t.get("exp_ts") or 0)
    except (TypeError, ValueError):
        exp = 0.0
    if not label or exp <= time.time():
        return ""
    left = int((exp - time.time()) / 60)
    return (f"\n【当下心境：{label}】这是刚发生的事留下的情绪余温，还会持续约 {left} 分钟。"
            "语气自然带一点就好，别解释它、别每句都提、也别当成新设定。")


def emotion_prompt() -> str:
    try:
        from plugins import hormones
        if hormones.ENABLED:
            return hormones.prompt_line()
    except Exception:  # noqa: BLE001
        pass
    pct = _load_emotion()
    return " ".join(f"{k}{pct[k]:.0f}%" for k in EMOTIONS)


def _refresh_mood(pct: dict) -> str:
    """由七维情绪派生今日心情。全项目唯一写 CURRENT_DAILY_MOOD 的地方。"""
    global CURRENT_DAILY_MOOD, DAILY_PERSONA
    mood = CURRENT_DAILY_MOOD
    try:
        if pct["怒"] + pct["烦"] >= 55:
            mood = "暴躁" if pct["怒"] >= pct["烦"] else "嘴臭"
        elif pct["害怕"] >= 30:
            mood = "委屈"
        elif pct["开心"] >= 38 or pct["害羞"] >= 28:
            mood = "开心"
        elif pct["丧"] + pct["累"] >= 50:
            mood = "emo" if pct["丧"] >= pct["累"] else "摸鱼"
        else:
            mood = "摸鱼"
    except Exception:  # noqa: BLE001
        return mood
    if mood != CURRENT_DAILY_MOOD:
        logger.info(f"[情绪] 主导情绪变化 {CURRENT_DAILY_MOOD} -> {mood}（{emotion_prompt()}）")
        CURRENT_DAILY_MOOD = mood
        DAILY_PERSONA["mood"] = mood
        DAILY_PERSONA["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            _save_daily_persona(DAILY_PERSONA)
        except Exception:  # noqa: BLE001
            pass
    return mood


def emotion_tick() -> None:
    """定时让情绪自然回归基线并落盘。

    原来衰减只在 update_emotion()（被怼/被夸）里跑，几小时没有这类事件时，
    emotion_state.json 就一直是上次的值——网页端转盘因此像冻住了。
    这里由 5 分钟定时任务调用，情绪会自己慢慢回落，面板就能看到变化。
    """
    try:
        data = _read_emotion_state()
        vals = _load_emotion()
        last = _emotion_last_ts()
        if last <= 0:
            last = time.time() - 300
        elapsed_min = (time.time() - last) / 60.0
        if elapsed_min < 1:
            return
        vals = _decay_to_base(vals, elapsed_min)
        total = sum(vals.values()) or 1.0
        # 归一化到 100%，面板转盘才是干净的一份
        pct = {k: round(vals[k] / total * 100.0, 1) for k in EMOTIONS}
        data["values"] = pct
        data["updated_ts"] = time.time()
        data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        # 顺便清掉过期的心境
        t = data.get("transient") or {}
        try:
            if float(t.get("exp_ts") or 0) <= time.time():
                data.pop("transient", None)
        except (TypeError, ValueError):
            data.pop("transient", None)
        EMOTION_FILE.parent.mkdir(parents=True, exist_ok=True)
        EMOTION_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        global CURRENT_DAILY_MOOD, DAILY_PERSONA
        _refresh_mood(pct)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[情绪] 定时衰减失败：{type(e).__name__}: {e}")


def update_emotion(kind: str, weight: int = 1) -> None:
    """按互动更新七维情绪，并把主导情绪写回今日人设，真正影响说话方式。"""
    global CURRENT_DAILY_MOOD, DAILY_PERSONA
    vals = _load_emotion()
    # 先按「距离上次更新过了多久」回归基线（情绪自己会缓过来，不用等下次被怼才降）
    last = _emotion_last_ts()
    elapsed_min = (time.time() - last) / 60.0 if last > 0 else 30.0
    vals = _decay_to_base(vals, elapsed_min)
    w = float(max(1, weight))
    if kind == "diss":
        vals["怒"] += w * 1.2
        vals["烦"] += w * 0.8
        vals["害怕"] += w * 0.2
        vals["开心"] = max(0, vals["开心"] - w * 0.4)
    elif kind == "threat":
        vals["害怕"] += w * 1.5
        vals["怒"] += w * 0.8
        vals["烦"] += w * 0.5
    elif kind == "happy":
        vals["开心"] += w * 1.1
        vals["害羞"] += w * 0.5
        vals["烦"] = max(0, vals["烦"] - w * 0.4)
        vals["丧"] = max(0, vals["丧"] - w * 0.3)
    elif kind == "tired":
        vals["累"] += w * 1.2
        vals["开心"] = max(0, vals["开心"] - w * 0.2)
    elif kind == "sad":
        vals["丧"] += w * 1.2
        vals["累"] += w * 0.4
    # 耦合：一方变化带动另一方的情绪（本地矩阵，不花钱）
    vals = _apply_coupling(vals)
    for k in EMOTIONS:
        vals[k] = max(0, min(100, vals[k]))
    # 当下心境：给这几轮一个具体的情绪余温，几十钟后自动失效
    if kind == "diss":
        set_transient("有点记仇，说话带刺", 45)
    elif kind == "threat":
        set_transient("有点被吓到，说话警惕", 40)
    elif kind == "happy":
        set_transient("心情不错，尾巴有点翘", 60)
    elif kind == "tired":
        set_transient("蔫蔫的，想躺平", 50)
    elif kind == "sad":
        set_transient("有点丧，话变少", 60)
    pct = _save_emotion(vals)
    _refresh_mood(pct)


def bump_daily_mood(kind: str, weight: int = 1) -> None:
    """按当天真实互动调整心情。

    kind=diss  被怼/被骂/被威胁 -> 分数下降；happy 被夸/贴贴/聊得开心 -> 分数上升。
    只有跨过阈值才换人设，正常聊天维持“摸鱼/正常”，不再每次重启随机。
    """
    global DAILY_PERSONA, CURRENT_DAILY_MOOD
    p = DAILY_PERSONA
    if p.get("date") != _today_key():
        p = _default_persona()
    if kind == "diss":
        p["score"] = max(0, int(p.get("score", 50)) - abs(weight))
        p["diss"] = int(p.get("diss", 0)) + 1
    elif kind == "happy":
        p["score"] = min(100, int(p.get("score", 50)) + abs(weight))
        p["happy"] = int(p.get("happy", 0)) + 1
    # 心情不再由这里写：今日分数只作"今天互动得多不多"的统计（面板/说说用），
    # 真正的 CURRENT_DAILY_MOOD 只由 update_emotion 从七维情绪派生，避免两个写入点打架。
    p["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    DAILY_PERSONA = p
    _save_daily_persona(p)
    logger.info(
        f"[今日互动] {kind} +{abs(weight)} → 分数 {p['score']}"
        f"（被怼 {p['diss']} / 开心 {p['happy']}）"
    )
    try:
        update_emotion(kind, weight)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[情绪] 更新失败：{e}")


# 活人感词库：主人让“上网学习”时从知乎等公开讨论里提炼的口语表达，主动学来的
HUMAN_LEXICON_FILE = ROOT / "memory" / "human_lexicon.json"
PROACTIVE_PRIVATE_FILE = ROOT / "memory" / "proactive_private.json"


def _load_pp_state() -> dict:
    try:
        data = json.loads(PROACTIVE_PRIVATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    data.setdefault("enabled", True)
    data.setdefault("history", [])
    data.setdefault("date", "")
    data.setdefault("count", 0)
    data.setdefault("last_at", 0)
    return data


def _save_pp_state(data: dict) -> None:
    try:
        PROACTIVE_PRIVATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        PROACTIVE_PRIVATE_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[主动私聊] 保存失败：{e}")


def load_human_lexicon() -> list[dict]:
    try:
        data = json.loads(HUMAN_LEXICON_FILE.read_text(encoding="utf-8"))
        items = data.get("items") if isinstance(data, dict) else data
        return [x for x in (items or []) if isinstance(x, dict) and x.get("phrase")]
    except Exception:
        return []


def save_human_lexicon(items: list[dict]) -> list[dict]:
    seen = {}
    for item in items:
        phrase = str(item.get("phrase") or "").strip()
        if not phrase or phrase in seen:
            continue
        seen[phrase] = {"phrase": phrase, "usage": str(item.get("usage") or "").strip()[:80]}
    merged = list(seen.values())[-160:]
    try:
        HUMAN_LEXICON_FILE.parent.mkdir(parents=True, exist_ok=True)
        HUMAN_LEXICON_FILE.write_text(
            json.dumps({"updated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "items": merged},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[活人词库] 保存失败：{e}")
    return merged


def human_lexicon_prompt(limit: int = 6) -> str:
    items = load_human_lexicon()[-limit:]
    if not items:
        return ""
    lines = [f"- {i['phrase']}" + (f"：{i['usage']}" if i.get("usage") else "") for i in items]
    return (
        "\n【活人感词库（主人让学的，能用才用）】\n"
        "一条回复最多自然用 1 个，不要连续两条用同一个；用不上就正常说，别硬塞。\n"
        + "\n".join(lines)
    )


async def learn_by_command(topic: str = "") -> tuple[str, str]:
    set_activity("上网学习中", 300)
    """主人说“上网学习[主题]”：联网自由学习，来源不限（不只知乎）。

    不指定主题时让她自己挑一个好奇的话题；学到的知识进 global_learn 档案，
    学到的口语表达进 human_lexicon 词库。
    """
    topic = (topic or "").strip()
    # 没指定主题时先替她选一个具体搜索词，避免模型把“请/学习”当成主题
    if not topic:
        topic = random.choice([
            "亚马逊产品图拍摄与渲染", "Corona Renderer 产品打光",
            "3ds Max 产品建模技巧", "tyFlow 产品特效", "Photoshop 电商精修",
            "AE 产品视频动效", "DaVinci 产品视频调色", "UV 展开与无缝贴图",
            "ZBrush 产品雕刻", "亚马逊 listing 主图规范", "AI 活人感", "最近网络热梗",
        ])
    prompt = (
        f"【搜索词】{topic}\n"
        "请围绕上面的搜索词联网查资料。来源不限：知乎、微博、B站、新闻、技术博客、公众号等公开内容都可以，"
        "不要只查知乎，也不要只看一个来源。\n"
        "主人做亚马逊产品图片/视频，常用 3ds Max、Corona、tyFlow、Photoshop；"
        "如果主题和工作相关，优先学能直接用在主图、A+、产品视频里的技巧。\n"
        f"这次的学习主题：{topic}\n"
        + "联网核实后，输出一个 JSON 对象（不要 markdown、不要解释）：\n"
        '{"topic":"最终主题","summary":"60字内大白话总结",'
        '"facts":[{"subject":"分类","fact":"一句可长期记住的事实"}],'
        '"phrases":[{"phrase":"2-10字口语表达","usage":"一句话说明怎么用"}]}\n'
        "facts 要 3~5 条、真实可核对；phrases 0~6 条，要短、日常、不脏、不书面，没有合适表达就给空数组；"
        "不要复述新闻腔，不要链接。"
    )
    try:
        items = await web_search._free_web_snippets(topic)
        if not items:
            return "", "搜索没返回结果"
        material = "\n".join(f"{i + 1}. {t}：{s}" for i, (t, s) in enumerate(items))
        text = await call_ai_raw(
            prompt + "\n\n搜索资料：\n" + material[:2500], "deepseek", temperature=0.3, max_tokens=900
        )
    except Exception as e:  # noqa: BLE001
        return "", f"联网失败：{e}"
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return "", "模型没按 JSON 返回"
    try:
        data = json.loads(text[start:end + 1])
    except Exception:  # noqa: BLE001
        return "", "JSON 解析失败"

    final_topic = str(data.get("topic") or topic or "随机主题").strip()[:40]
    summary = str(data.get("summary") or "").strip()[:120]
    facts = []
    for item in (data.get("facts") or [])[:6]:
        if isinstance(item, dict) and item.get("fact"):
            facts.append((str(item.get("subject") or "自学")[:12], str(item["fact"]).strip()[:120]))
    phrases = []
    for item in (data.get("phrases") or [])[:8]:
        if isinstance(item, dict) and item.get("phrase"):
            phrases.append({"phrase": str(item["phrase"]).strip()[:20],
                            "usage": str(item.get("usage") or "").strip()[:80]})

    if facts:
        for subject, fact in facts:
            ltm.add_pending("global_learn", subject, f"{final_topic}：{fact}"[:120])
        try:
            await ltm.consolidate("global_learn")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[上网学习] 并入档案失败：{e}")
    if phrases:
        save_human_lexicon(load_human_lexicon() + phrases)

    logger.info(
        f"[上网学习] topic={final_topic!r} facts={len(facts)} phrases={len(phrases)} "
        f"summary={summary[:40]!r}"
    )
    lines = [f"📚 学完啦：{final_topic}"]
    if summary:
        lines.append(summary)
    lines.append(f"记下 {len(facts)} 条知识、{len(phrases)} 条口语表达。")
    return "\n".join(lines), ""

ACTIVE_GROUPS = set()
ANGER_CHAIN = defaultdict(lambda: 0)
# @ 刷屏防抖：同一个人在 60 秒内 @ 超过 5 次就静默 120 秒
AT_SPAM = defaultdict(deque)
AT_SPAM_MUTE = {}
AT_SPAM_WINDOW = 60
AT_SPAM_LIMIT = 5
AT_SPAM_MUTE_SECONDS = 120
LAST_ASKED = {}
PENDING_ACTION = {}   # {"type": "join"/"leave"/"invite", "gid":..., "name":...}
PENDING_INVITES = {}  # gid -> flag（收到邀请但还没批）

# 哪些 QQ 拉她进群可以直接通过：主人本人 + 主人小号（手动同意过一次就记住）
INVITE_TRUST_FILE = ROOT / "memory" / "invite_trust.json"
_INVITE_TRUST: set[str] = set()


def _load_invite_trust() -> set[str]:
    global _INVITE_TRUST
    if _INVITE_TRUST:
        return _INVITE_TRUST
    _INVITE_TRUST = {YOUR_QQ_ID}
    for x in (os.getenv("GROUP_INVITE_AUTO_QQ") or "").split(","):
        x = x.strip()
        if x.isdigit():
            _INVITE_TRUST.add(x)
    try:
        data = json.loads(INVITE_TRUST_FILE.read_text(encoding="utf-8"))
        for x in data.get("qq") or []:
            if str(x).isdigit():
                _INVITE_TRUST.add(str(x))
    except Exception:  # noqa: BLE001
        pass
    return _INVITE_TRUST


def _trust_inviter(qq: str) -> None:
    """手动同意过一次的邀请人，下次自动通过（小号就不用每次问）。"""
    qq = str(qq or "").strip()
    if not qq.isdigit() or qq in _load_invite_trust():
        return
    _INVITE_TRUST.add(qq)
    try:
        INVITE_TRUST_FILE.parent.mkdir(parents=True, exist_ok=True)
        INVITE_TRUST_FILE.write_text(
            json.dumps({"qq": sorted(_INVITE_TRUST)}, ensure_ascii=False, indent=1), encoding="utf-8")
        logger.info(f"[群邀请] {qq} 加入可信邀请人（以后拉群自动同意）")
    except Exception:  # noqa: BLE001
        pass
LAST_GROUP_MSG_TIME = defaultdict(lambda: time.time())
HOLD_GRUDGE = {}
GROUP_MEMES = defaultdict(list)

LEAVE_THREAT_COUNT = defaultdict(int)
GRUDGE_TARGET = defaultdict(lambda: {})
RIVAL_AI_WORDS = ["ChatGPT", "GPT-4", "文心一言", "Kimi", "Claude", "通义千问", "智谱清言", "豆包AI"]
SAD_WORDS = ["分手", "孤独", "好累", "emo", "想哭", "难过", "抑郁"]
BAD_NEWS_WORDS = ["虐猫", "虐狗", "虐待", "诈骗", "拐卖", "家暴"]

# 专属文件夹
STEAL_DIR = cfg.emoji.dir / "偷来的图"

# 群里看到“像自己”的表情包自动收进图库
SELF_STICKER_LAST = {}
SELF_STICKER_DAY = {"date": "", "count": 0}
SELF_STICKER_DAILY_CAP = 25
SELF_STICKER_GAP = 180  # 同一个群最少间隔 3 分钟才检查一张
_EMOJI_HASH_CACHE = None
SELF_MOODS = ("开心", "无语", "生气", "卖萌", "得意", "委屈", "震惊")


def _emoji_hashes() -> set:
    global _EMOJI_HASH_CACHE
    if _EMOJI_HASH_CACHE is not None:
        return _EMOJI_HASH_CACHE
    hashes = set()
    try:
        for p in cfg.emoji.dir.rglob("*"):
            if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png", ".gif"):
                try:
                    hashes.add(hashlib.md5(p.read_bytes()).hexdigest())
                except OSError:
                    pass
    except OSError:
        pass
    _EMOJI_HASH_CACHE = hashes
    return hashes


COMFY_URL = os.getenv("COMFY_URL", "http://127.0.0.1:8188")
COMFY_VISION_ENABLED = os.getenv("COMFY_VISION_ENABLED", "false").lower() in ("1", "true", "yes")
# 兜底开关：即使 COMFY_VISION_ENABLED=false，云端识图都失败时仍允许本地识图顶上
COMFY_VISION_FALLBACK = os.getenv("COMFY_VISION_FALLBACK", "true").lower() in ("1", "true", "yes")
COMFY_WORKFLOW = ROOT / "comfy_workflows" / "qwen_vl_recognize.json"
COMFY_OUTPUT = Path(os.getenv("COMFY_OUTPUT") or r"C:\ComfyUI-aki(2)\ComfyUI-aki-v1.7\ComfyUI\output")
COMFY_TEXT_WORKFLOW = ROOT / "comfy_workflows" / "qwen_text.json"


async def _local_qwen_text(prompt: str, timeout: int = 90) -> str:
    """本地 QwenVL 做纯文本总结，省钱；失败返回空串。"""
    if not COMFY_VISION_ENABLED:
        return ""
    # ComfyUI 默认不自启：本地识图/文本前兜底拉起，起不来就回退下一通道
    try:
        from plugins import comfy_boot
        if not await asyncio.to_thread(comfy_boot.ensure, 150.0, "识图"):
            logger.warning("[本地识图] ComfyUI 起不来，跳过")
            return ""
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[本地识图] 拉起 ComfyUI 失败：{type(e).__name__}: {e}")
    t0 = time.time()
    try:
        wf = json.loads(COMFY_TEXT_WORKFLOW.read_text(encoding="utf-8"))
        wf["1"]["inputs"]["custom_prompt"] = prompt[:1500]
        q = await http_client.post(f"{COMFY_URL}/prompt", json={"prompt": wf}, timeout=20)
        prompt_id = q.json()["prompt_id"]
        deadline = time.time() + timeout
        while time.time() < deadline:
            await asyncio.sleep(0.6)   # 原来 2s：哪怕算完也要多等一截
            h = (await http_client.get(f"{COMFY_URL}/history/{prompt_id}", timeout=15)).json()
            item = h.get(prompt_id) or {}
            for node in (item.get("outputs") or {}).values():
                for key in ("text", "string"):
                    val = node.get(key)
                    if isinstance(val, list) and val:
                        val = val[0]
                    if isinstance(val, str) and val.strip():
                        token_meter.record_local("text", time.time() - t0)
                        return val.strip()
            if (item.get("status") or {}).get("status_str") == "success":
                folder = COMFY_OUTPUT / "qwen_text"
                if folder.exists():
                    files = sorted(folder.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
                    if files:
                        token_meter.record_local("text", time.time() - t0)
                        return files[0].read_text(encoding="utf-8", errors="ignore").strip()
                return ""
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[本地总结] 失败：{type(e).__name__}: {e}")
    return ""


ARK_VISION_USAGE = ROOT / "memory" / "ark_vision_usage.json"
ARK_VISION_DAILY_LIMIT = int(os.getenv("ARK_VISION_DAILY_LIMIT", "20") or "20")
# 余额/额度报错后熔断多久（秒）；期间识图直接走本地
_ARK_VISION_BLOCK_UNTIL = 0.0
ARK_VISION_BLOCK_SECONDS = float(os.getenv("ARK_VISION_BLOCK_HOURS", "6") or "6") * 3600


def _ark_vision_can_use() -> bool:
    global _ARK_VISION_BLOCK_UNTIL
    if time.time() < _ARK_VISION_BLOCK_UNTIL:
        return False
    if os.getenv("ARK_VISION_ALLOWED", "true").lower() not in ("1", "true", "yes"):
        return False
    try:
        d = json.loads(ARK_VISION_USAGE.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    today = time.strftime("%Y-%m-%d")
    if d.get("date") != today:
        return True
    return int(d.get("count", 0)) < ARK_VISION_DAILY_LIMIT


def _ark_vision_mark() -> None:
    try:
        d = json.loads(ARK_VISION_USAGE.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    today = time.strftime("%Y-%m-%d")
    if d.get("date") != today:
        d = {"date": today, "count": 0}
    d["count"] = int(d.get("count", 0)) + 1
    try:
        ARK_VISION_USAGE.parent.mkdir(parents=True, exist_ok=True)
        ARK_VISION_USAGE.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


async def _ark_vision(image_bytes: bytes, prompt: str) -> str:
    """火山方舟识图，带每日上限；失败返回空串。"""
    if not _ark_vision_can_use():
        logger.info("[识图] 火山识图今日已达上限，跳过")
        return ""
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        im.thumbnail((1280, 1280))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85)
        b64 = base64.b64encode(buf.getvalue()).decode()
        model = os.getenv("ARK_VISION_MODEL", "doubao-1.5-vision-pro-32k-250115")
        r = await http_client.post(
            cfg.ai.endpoint(),
            headers={"Authorization": f"Bearer {cfg.ai.api_key}", "Content-Type": "application/json"},
            json={"model": model, "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
            ]}], "max_tokens": 500},
            timeout=90,
        )
        data = r.json()
        if "error" in data:
            err_txt = str(data["error"])
            logger.warning(f"[识图] 火山返回错误：{err_txt[:160]}")
            low = err_txt.lower()
            if any(k in low for k in ("402", "余额", "额度", "欠费", "quota", "insufficient", "balance")):
                global _ARK_VISION_BLOCK_UNTIL
                _ARK_VISION_BLOCK_UNTIL = time.time() + ARK_VISION_BLOCK_SECONDS
                logger.warning(
                    f"[识图] 火山余额/额度耗尽 → 熔断 {ARK_VISION_BLOCK_SECONDS/3600:.1f} 小时，改走本地识图")
            return ""
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        if text:
            _ark_vision_mark()
            token_meter.record(data.get("usage", {}) or {}, "vision", provider="ark")
        return text.strip()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[识图] 火山失败：{type(e).__name__}: {e}")
        return ""


_LOCAL_VISION_LOCK: asyncio.Lock | None = None


def _vision_lock() -> asyncio.Lock:
    global _LOCAL_VISION_LOCK
    if _LOCAL_VISION_LOCK is None:
        _LOCAL_VISION_LOCK = asyncio.Lock()
    return _LOCAL_VISION_LOCK


async def _local_qwen_vision(image_bytes: bytes, prompt: str, timeout: int = 120) -> str:
    """本地识图：全局串行，忙就跳过（不往 ComfyUI 队列里塞任务）。

    背景：以前每张图都提交一个任务，QwenVL 显存不够时全卡在队列里，
    越堆越多，主人在面板上看到一排"正在执行"。
    """
    # 正在出图：识图会加载 QwenVL（7~9GB 显存），把扩散模型挤到 CPU 上（50s/步），
    # 所以画图期间一律不识别，直接返回空串让上游回退/跳过。
    if _comfy_busy():
        logger.info("[识图] 正在画图，跳过本次识别（避免抢显存）")
        return ""
    try:
        await asyncio.wait_for(_vision_lock().acquire(), timeout=3)
    except Exception:  # noqa: BLE001
        logger.info("[识图] 本地正忙，跳过本次（不排队）")
        return ""
    try:
        # 队列里还有别人的活就不插队
        try:
            q = (await http_client.get(f"{COMFY_URL}/queue", timeout=8)).json()
            pending = len(q.get("queue_pending") or []) + len(q.get("queue_running") or [])
        except Exception:  # noqa: BLE001
            pending = 0
        if pending > 0:
            logger.info(f"[识图] ComfyUI 队列还有 {pending} 个任务，本次不排队")
            return ""
        global _COMFY_LAST_ACTIVITY
        _COMFY_LAST_ACTIVITY = time.time()
        return await _local_qwen_vision_inner(image_bytes, prompt, timeout)
    finally:
        try:
            _vision_lock().release()
        except Exception:  # noqa: BLE001
            pass


async def _local_qwen_vision_inner(image_bytes: bytes, prompt: str, timeout: int = 120) -> str:
    """本地 ComfyUI QwenVL 识图；失败返回空串，由调用方回退。"""
    if not (COMFY_VISION_ENABLED or COMFY_VISION_FALLBACK):
        return ""
    # ComfyUI 默认不自启：本地识图/文本前兜底拉起，起不来就回退下一通道
    try:
        from plugins import comfy_boot
        if not await asyncio.to_thread(comfy_boot.ensure, 150.0, "识图"):
            logger.warning("[本地识图] ComfyUI 起不来，跳过")
            return ""
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[本地识图] 拉起 ComfyUI 失败：{type(e).__name__}: {e}")
    t0 = time.time()
    try:
        files = {"image": ("vision.png", image_bytes, "image/png")}
        r = await http_client.post(
            f"{COMFY_URL}/upload/image",
            data={"overwrite": "true", "type": "input"},
            files=files, timeout=30,
        )
        name = r.json()["name"]
        wf = json.loads(COMFY_WORKFLOW.read_text(encoding="utf-8"))
        wf["1"]["inputs"]["image"] = name
        wf["2"]["inputs"]["custom_prompt"] = prompt[:800]
        q = await http_client.post(f"{COMFY_URL}/prompt", json={"prompt": wf}, timeout=20)
        prompt_id = q.json()["prompt_id"]
        deadline = time.time() + timeout
        while time.time() < deadline:
            await asyncio.sleep(2)
            h = (await http_client.get(f"{COMFY_URL}/history/{prompt_id}", timeout=15)).json()
            item = h.get(prompt_id) or {}
            outputs = item.get("outputs") or {}
            for node in outputs.values():
                for key in ("text", "string"):
                    val = node.get(key)
                    if isinstance(val, list) and val:
                        val = val[0]
                    if isinstance(val, str) and val.strip():
                        _dt = time.time() - t0
                        logger.info(f"[本地识图] 完成 {_dt:.1f}s（{len(val)} 字）")
                        token_meter.record_local("vision", _dt)
                        return val.strip()
            status = (item.get("status") or {}).get("status_str")
            if status == "error":
                return ""
            if status == "success":
                folder = COMFY_OUTPUT / "qwen_vl"
                if folder.exists():
                    files = sorted(folder.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
                    if files:
                        token_meter.record_local("vision", time.time() - t0)
                        return files[0].read_text(encoding="utf-8", errors="ignore").strip()
        return ""
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[本地识图] ComfyUI 失败，回退云端：{type(e).__name__}: {e}")
        return ""


async def _classify_saved_image(data: bytes) -> dict:
    """给主人发来的图分档：18+ / 情绪 / 收藏。失败就返回空 dict（调用方走 收藏 兜底）。"""
    try:
        img = Image.open(io.BytesIO(data))
        if getattr(img, "n_frames", 1) > 1:
            img.seek(0)
        # 2026-11-05 修：表情包大多是 100~200px 的透明底 GIF。
        # convert("RGB") 直接把透明变黑、thumbnail 只缩不放，喂给视觉模型基本是一片糊，
        # 所以 60 张里只有 5 张分得出类。这里改成：透明铺白底 + 小于 384 的放大。
        img = img.convert("RGBA")
        _bg = Image.new("RGB", img.size, (255, 255, 255))
        _bg.paste(img, mask=img.split()[-1])
        small = _bg
        _w, _h = small.size
        _m = max(_w, _h)
        if _m < 384 and _m > 0:
            _k = 384.0 / _m
            small = small.resize((max(1, int(_w * _k)), max(1, int(_h * _k))), Image.LANCZOS)
        small.thumbnail((640, 640))
        buf = io.BytesIO()
        small.save(buf, format="JPEG", quality=88)
    except Exception:  # noqa: BLE001
        return {}
    prompt = (
        "给这张图分类，只输出 JSON，不要解释："
        '{"rating":"safe/suggestive/r18",'
        '"mood":"开心/无语/生气/卖萌/得意/委屈/震惊/其他",'
        '"kind":"梗图/表情/截图/自拍/其他",'
        '"desc":"20字以内"}'
        "规则：裸露、性行为、明显性暗示 → r18；擦边、低俗但没露 → suggestive；其余 safe。"
    )
    text = ""
    try:
        text = await _local_qwen_vision(buf.getvalue(), prompt)
    except Exception:  # noqa: BLE001
        text = ""
    if not text:
        return {}
    try:
        i, j = text.find("{"), text.rfind("}")
        return json.loads(text[i:j + 1]) if i >= 0 and j > i else {}
    except Exception:  # noqa: BLE001
        return {}


def _save_dir_for(info: dict) -> Path:
    """按分类决定存哪：r18 → 表情包/R-18；情绪 → 对应文件夹；其它 → 收藏。"""
    rating = str(info.get("rating") or "safe").lower()
    if rating == "r18":
        return cfg.emoji.dir / "R-18"
    mood = str(info.get("mood") or "")
    if mood in SELF_MOODS:
        return cfg.emoji.dir / mood
    return cfg.emoji.dir / "收藏"


async def _forward_images_by_id(bot, fid: str, limit: int = 60) -> list[str]:
    """把一个合并转发（forward id）展开成图片 url 列表。

    不同 OneBot 实现返回结构有差别：
      · messages 可能是 dict["messages"] / list / dict["data"]
      · 每条里可能是 message / content，值是段数组 / CQ 字符串
    这里全部兜住，解析不出来的就跳过，不抛。
    """
    urls: list[str] = []
    if not fid:
        return urls
    try:
        data = await bot.call_api("get_forward_msg", message_id=str(fid))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[转发] get_forward_msg 失败 id={fid}：{type(e).__name__}: {str(e)[:100]}")
        return urls
    msgs = data.get("messages") if isinstance(data, dict) else data
    if isinstance(msgs, dict):
        msgs = msgs.get("messages") or msgs.get("data") or []
    for m in (msgs or []):
        if not isinstance(m, dict):
            continue
        content = m.get("content") or m.get("message") or []
        if isinstance(content, str):
            # CQ 码形态：优先 url（能直接下载），没有才退 file（本地缓存名）
            _us = re.findall(r"CQ:image,[^\]]*?url=([^,\]]+)", content)
            if not _us:
                _us = re.findall(r"CQ:image,[^\]]*?file=([^,\]]+)", content)
            urls.extend(_us)
        else:
            for seg in content:
                if not isinstance(seg, dict) or seg.get("type") != "image":
                    continue
                _d = seg.get("data") or {}
                _u = _d.get("url") or _d.get("file") or ""
                if isinstance(_u, str) and len(_u) > 5:
                    urls.append(_u)
        if len(urls) >= limit:
            break
    # 去重保序
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out[:limit]


async def _forward_image_urls(bot, event, limit: int = 60) -> list[str]:
    """从一条消息的 forward/node 段展开出所有图片 url。

    2026-11-05：主人转发一整段聊天记录（里面全是表情包）过来，以前会走到
    「[私聊静默·空文本] 无文字也无图，跳过」——因为 `get_plaintext()` 对
    forward 段返回空串，而 `get_message()` 里那个段是 `forward` 不是 `image`。
    等于她一张都没看见。
    """
    ids: list[str] = []
    try:
        for seg in event.get_message():
            if seg.type in ("forward", "node"):
                _fid = str((seg.data or {}).get("id") or "").strip()
                if _fid:
                    ids.append(_fid)
    except Exception:  # noqa: BLE001
        return []
    out: list[str] = []
    for fid in ids[:3]:
        out.extend(await _forward_images_by_id(bot, fid, limit))
    seen, uniq = set(), []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq[:limit]


async def _reclassify_saved(paths: list) -> None:
    """后台把刚存下的图逐张分类，成功就移动到对应情绪目录。

    2026-11-05：不能放在主流程里 —— 视觉通道是全局串行的（`_vision_lock`，acquire 3 秒），
    批量分类会把锁抢爆、刷满「本地正忙」，还会把实时聊天的识图一起堵死。
    所以先全部落到「收藏」并立刻回复，分类放到后台慢慢挪。
    """
    moved = 0
    for _p in paths:
        try:
            if not _p.exists():
                continue
            _b = _p.read_bytes()
            _info = await _classify_saved_image(_b)     # 抢不到锁会返回 {}，下一张
            if _info:
                _dd = _save_dir_for(_info)
                if _dd != _p.parent:
                    _dd.mkdir(parents=True, exist_ok=True)
                    _p.rename(_dd / _p.name)
                    moved += 1
                    logger.info(f"[转发·补分类] {_p.name} → {_dd.name} "
                                f"mood={_info.get('mood') or '?'} kind={_info.get('kind') or '?'}")
        except Exception as _e:  # noqa: BLE001
            logger.debug(f"[转发·补分类] 跳过 {getattr(_p,'name','?')}：{_e}")
        await asyncio.sleep(0.35)
    if moved:
        logger.info(f"[转发·补分类] 完成，共挪走 {moved} 张")


async def _save_forward_images(urls: list[str], tag: str = "fwd") -> tuple[int, list[str]]:
    """下载 + 立刻落盘（默认「收藏」），分类丢后台。返回 (成功张数, 用到的目录名)。

    为什么要这么拆：视觉识别是全局串行的，主流程里等它 = 几十张图要几分钟，
    期间主人发什么都没回应。先把图收下来、马上回话，分类在后台做。
    """
    saved, buckets = 0, []
    if not urls:
        return saved, buckets
    _sem_dl = asyncio.Semaphore(6)

    async def _fetch_one(_u: str) -> bytes | None:
        async with _sem_dl:
            try:
                _rr = await http_client.get(_u, follow_redirects=True, timeout=25)
                if _rr.status_code == 200 and len(_rr.content or b"") > 1024:
                    return _rr.content
            except Exception:  # noqa: BLE001
                pass
            return None

    _blobs = await asyncio.gather(*[_fetch_one(_u) for _u in urls])
    _ok = [b for b in _blobs if b]
    logger.info(f"[转发] 下载完成 {len(_ok)}/{len(urls)} 张")

    _target = cfg.emoji.dir / "收藏"
    _target.mkdir(parents=True, exist_ok=True)
    _stamp = int(time.time())
    _paths = []
    for _i, _b in enumerate(_ok):
        try:
            _ext = ".gif" if _b[:6] in (b"GIF87a", b"GIF89a") else ".jpg"
            _name = f"{tag}_{_stamp}_{_i}{_ext}"
            if (_target / _name).exists():
                _name = f"{tag}_{_stamp}_{_i}_{hashlib.md5(_b).hexdigest()[:6]}{_ext}"
            _fp = _target / _name
            _fp.write_bytes(_b)
            _paths.append(_fp)
            saved += 1
            buckets.append("收藏")
        except Exception as _se:  # noqa: BLE001
            logger.warning(f"[转发] 单张保存失败：{type(_se).__name__}: {_se}")
    logger.info(f"[转发] 已落盘 {saved} 张 → 收藏，分类转后台")
    if _paths:
        try:
            asyncio.create_task(_reclassify_saved(_paths))
        except Exception:  # noqa: BLE001
            pass
    return saved, buckets


async def maybe_collect_self_sticker(url: str, gid: int, uid: str, nickname: str) -> None:
    """群里出现疑似“蓝色大肥鱼”表情包时，视觉确认后自动收进对应情绪文件夹。"""
    if not url or len(url) < 10:
        return
    now = time.time()
    if now - SELF_STICKER_LAST.get(gid, 0) < SELF_STICKER_GAP:
        return
    today = time.strftime("%Y-%m-%d")
    if SELF_STICKER_DAY["date"] != today:
        SELF_STICKER_DAY["date"] = today
        SELF_STICKER_DAY["count"] = 0
    if SELF_STICKER_DAY["count"] >= SELF_STICKER_DAILY_CAP:
        return
    SELF_STICKER_LAST[gid] = now

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        data = resp.content
    except Exception:
        return
    if not (6000 < len(data) < 8 * 1024 * 1024):
        return
    if hashlib.md5(data).hexdigest() in _emoji_hashes():
        return
    try:
        img = Image.open(io.BytesIO(data))
        if getattr(img, "n_frames", 1) > 1:
            img.seek(0)
        fmt = (img.format or "").upper()
        ext = {"JPEG": ".jpg", "PNG": ".png", "GIF": ".gif"}.get(fmt)
        if not ext:
            return
        small = img.convert("RGB")
        small.thumbnail((512, 512))
        buf = io.BytesIO()
        small.save(buf, format="JPEG", quality=85)
        b64 = base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return

    prompt = (
        "你是「白饭大王」的收图判断器。看这张群聊图，判断她会不会想存进自己的表情库。"
        "想存的标准：像梗图/表情包/沙雕图/可爱图，能用来接话；或者图里画的是她（圆滚滚蓝色鲸鱼/鲸鱼拟人）。"
        "不想存：聊天截图、文字截图、二维码、报表、真人自拍、私密/擦边、模糊低质、纯风景。"
        "只输出 JSON，不要解释："
        "{\"is_self_like\":true/false,\"is_sticker\":true/false,\"want_save\":0到1,"
        "\"kind\":\"梗图/表情/截图/自拍/其他\",\"mood\":\"开心/无语/生气/卖萌/得意/委屈/震惊/其他\","
        "\"confidence\":0到1,\"reason\":\"20字以内\",\"desc\":\"20字以内\"}"
    )
    body = {
        "model": cfg.ai.model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
        ]}],
        "max_tokens": 300,
        "thinking": {"type": "disabled"},
    }
    text = ""
    if os.getenv("GEMINI_VISION_ENABLED", "false").lower() in ("1", "true", "yes"):
        try:
            from plugins import gemini
            text = await gemini.gemini_vision(prompt, buf.getvalue())
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[自动收图] Gemini 识图失败，回退本地：{e}")
    if not text:
        text = await _local_qwen_vision(buf.getvalue(), prompt)
    if not text and os.getenv("ARK_FALLBACK_ENABLED", "false").lower() in ("1", "true", "yes"):
        try:
            r = await http_client.post(
                cfg.ai.endpoint(),
                headers={"Authorization": f"Bearer {cfg.ai.api_key}", "Content-Type": "application/json"},
                json=body, timeout=90,
            )
            data = r.json()
            token_meter.record(data.get("usage", {}) or {}, "vision", provider="ark")
            text = data["choices"][0]["message"].get("content") or ""
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[自动收图] 视觉判断失败：{e}")
            return
    try:
        start, end = text.find("{"), text.rfind("}")
        info = json.loads(text[start:end + 1])
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[自动收图] 视觉结果解析失败：{e}")
        return
    # 两种都存：① 像她自己（老逻辑） ② 她自己判断「想存」（梗图/表情，2026-11-04 放开）
    _self_like = bool(info.get("is_self_like")) and bool(info.get("is_sticker"))
    try:
        _want = float(info.get("want_save") or 0)
    except (TypeError, ValueError):
        _want = 0.0
    _kind = str(info.get("kind") or "")
    if _self_like:
        try:
            if float(info.get("confidence") or 0) < 0.6:
                return
        except (TypeError, ValueError):
            return
    elif not (_kind in ("梗图", "表情") and _want >= 0.65):
        return

    mood = str(info.get("mood") or "")
    if mood not in SELF_MOODS:
        mood = "卖萌"
    target = cfg.emoji.dir / mood
    name = f"auto_{hashlib.md5(data).hexdigest()[:10]}{ext}"
    try:
        target.mkdir(parents=True, exist_ok=True)
        (target / name).write_bytes(data)
        _emoji_hashes().add(hashlib.md5(data).hexdigest())
        EMOJI_CACHE.pop(mood, None)
        SELF_STICKER_DAY["count"] += 1
        logger.info(
            f"[自动收图] 已收藏 gid={gid} uid={uid} nick={nickname} mood={mood} file={name} "
            f"kind={info.get('kind', '')} want={_want:.2f} "
            f"reason={str(info.get('reason') or '')[:24]} desc={str(info.get('desc') or '')[:30]}"
        )
    except OSError as e:
        logger.warning(f"[自动收图] 保存失败：{e}")


YOUR_QQ_ID = os.getenv("OWNER_QQ_ID", "100000000")
THREAT_WORDS = ["打死", "弄死", "拔网线", "顺着网线", "单杀", "线下", "揍你", "删库", "黑了你", "拉闸"]
INTEREST_WORDS = ["吃", "饭", "肉", "游戏", "原神", "动漫", "二次元", "八卦", "乐子", "AI", "人工智能", "大模型", "GPT", "DeepSeek", "Prompt", "硅基流动"]
GAMES_DISABLED = {"抽卡", "轮盘"}

# 已知斜杠指令：画图分支不得把这些当成画图提示词（避免 /状态、/频率 等被误判成画图）
# 按命令头（第一个空格前）匹配，带参数指令（/频率 降低、/词库 猫、/日记 2026-10-20）同样放行。
KNOWN_SLASH_COMMANDS = {
    "/help", "/指令", "/帮助", "/测试",
    "/status", "/状态", "/体检",
    "/余额", "/好感度", "/日报",
    "/记忆", "/学习", "/档案", "/记忆统计",
    "/频率", "/回复频率",
    "/词库", "/词典", "/本群词库", "/改词", "/删词",
    # 每日动态（daily_mood.py，priority=20，晚于本插件，需在此先放行）
    "/说说", "/动态", "/朋友圈", "/发说说",
    "/收转发", "/转发收图", "/收图",
    # 每日日记（daily_diary.py，priority=21，晚于本插件，需在此先放行）
    "/日记", "/写日记",
}
# 状态报告指令别名（群聊体检报告 + 私聊状态面板，英文/中文、带斜杠/不带斜杠都认）
STATUS_COMMANDS = {"/status", "/状态", "/体检"}
# 私聊记忆类指令分三组，各自独立面板（详见 handler 内 MEMORY / LEARN / ARCHIVE 分支）：
# 聊天记忆：和主人私聊最近聊了什么、私聊记录条数
MEMORY_COMMANDS = {"/记忆"}
# 网上自学档案：global_learn 自学条数/字数、待整理、群聊词库、最近学到的新闻常识
LEARN_COMMANDS = {"/学习"}
# 长期档案 + 综合统计：好感度/标签、主人长期记忆、自学量、词库、待整理汇总
ARCHIVE_COMMANDS = {"/档案", "/记忆统计"}
# 群聊词条库指令别名（群里 @ 本鱼查看；改/删仅主人私聊开放）
LEXICON_COMMANDS = {"/词库", "/词典", "/本群词库"}
# 主人手动改 / 删词条指令（私聊专属）
LEXICON_EDIT_COMMANDS = {"/改词", "/删词"}
# 群聊主动回复频率指令别名（仅主人可调；私聊 / 群聊主人都能用）
RATE_COMMANDS = {"/回复频率", "/频率", "回复频率", "频率", "回复率", "主动回复率"}


def _is_known_slash_command(user_msg: str) -> bool:
    """判断是否已知斜杠指令，避免被画图分支误吞。

    两种口径都算已知：
    1. 命令头（第一个空格前 token）在集合里：`/频率 降低`、`/词库 12345`、`/改词 …`；
    2. 整串以某个已知命令连写开头：`/说说预览`、`/日记历史`、`/说说开关 开` 这类无空格子指令。
    """
    if not user_msg:
        return False
    head = user_msg.split(maxsplit=1)[0]
    if head in KNOWN_SLASH_COMMANDS:
        return True
    return any(user_msg == p or user_msg.startswith(p) for p in KNOWN_SLASH_COMMANDS)


# 仅由外部插件处理、本插件 private_ai 不接管的指令前缀（daily_mood / daily_diary，优先级 20/21）
# 命中后 private_ai 直接 return 放行，避免兜底 LLM 把指令当普通聊天抢跑回复
EXTERNAL_PLUGIN_PREFIXES = (
    "/说说", "/动态", "/朋友圈", "/发说说",
    "/日记", "/写日记",
)


# 外部插件的"生活化指令"（不带 / 的说法）。命中就交给那个插件，别让兜底 LLM 再回一条。
# 只收"明确是命令"的形状，避免把普通聊天吞掉。
_EXTERNAL_PATTERNS = (
    re.compile(r"^(强化学习|深挖)|强化学习"),                # deep_learn（非行首也算）
    re.compile(r"^(解析|看懂|看视频|看看这个|总结一下).{0,10}https?://"),  # media_parse
    re.compile(r"(https?://\S+).{0,20}(总结|解析|看懂|讲什么)"),          # 链接+要总结（任何位置）
    re.compile(r"(去玩手机|玩会儿手机|玩手机|刷抖音|刷B站|刷b站|看会?B站)"),  # phone_play（按 contains 判定，和插件一致）
    re.compile(r"^/?(串门)(\s+\S+)?$"),                    # douyin_visit（可带参数）
    re.compile(r"^/纪念日"),                               # dates_cmd
    re.compile(r"^(发说说|写说说|发动态|发朋友圈)$"),          # daily_mood
    re.compile(r"(说说|动态|朋友圈).{0,6}(空间|发|写|来一条)"),  # daily_mood 变体（任意位置）
)


def _expert_mode_on() -> bool:
    try:
        import json as _j
        d = _j.loads((ROOT / "memory" / "expert_mode.json").read_text(encoding="utf-8"))
        return bool(d.get("enabled"))
    except Exception:  # noqa: BLE001
        return False


def _is_external_plugin_command(user_msg: str) -> bool:
    if not user_msg:
        return False
    t = user_msg.strip().replace("／", "/")
    if t.startswith(EXTERNAL_PLUGIN_PREFIXES):
        return True
    # 专家模式开着时，非命令文本都由 expert_mode 接（它 block=False、priority 4）
    if not t.startswith("/") and _expert_mode_on():
        return True
    return any(p.match(t) for p in _EXTERNAL_PATTERNS)

http_client = httpx.AsyncClient(timeout=120, limits=httpx.Limits(max_keepalive_connections=20, max_connections=50))

# =====================================================================
# 🌟 辅助函数
# =====================================================================
def get_time_context():
    now = time.localtime()
    weekday = now.tm_wday
    hour = now.tm_hour
    context = ""
    if weekday == 0: context += "【今天是周一】你非常不想上班，如果群友找你，你会抱怨周一太痛苦。"
    elif weekday == 4: context += "【今天是周五】你心情极好，满脑子都是周末怎么玩。"
    elif weekday >= 5: context += "【今天是周末】你在疯狂熬夜或者睡懒觉。"
    if 0 <= hour < 6: context += "【当前是深夜】你极度困倦，脾气暴躁，不想说话。"
    elif 11 <= hour < 14: context += "【当前是中午】你肚子很饿，想吃红烧肉或者白米饭。"
    return context

def get_chinese_font(size: int):
    if size in FONT_CACHE: return FONT_CACHE[size]
    font_paths = ["C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf", "/System/Library/Fonts/PingFang.ttc", "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"]
    for p in font_paths:
        if os.path.exists(p):
            try:
                font = ImageFont.truetype(p, size)
                FONT_CACHE[size] = font
                return font
            except Exception: pass
    return ImageFont.load_default()

async def download_image_to_temp(url: str) -> str | None:
    try:
        async with httpx.AsyncClient(follow_redirects=True) as c:
            resp = await c.get(url, timeout=15)
            if resp.status_code == 200 and 1000 < len(resp.content) < 15 * 1024 * 1024:
                tmp_path = tempfile.mktemp(suffix=".png")
                with open(tmp_path, "wb") as f: f.write(resp.content)
                return tmp_path
    except Exception as e: logger.error(f"图片下载失败: {e}")
    return None

async def safe_remove(filepath: str, delay: int = 5):
    if not filepath: return
    await asyncio.sleep(delay)
    if os.path.exists(filepath):
        try: os.remove(filepath)
        except Exception: pass


def _strip_bot_marker(text: str) -> str:
    """把自己历史里的 [本鱼发了一张图：…] 当正文发出来时剥掉（2026-11-04 实测反复发同句）。"""
    s = str(text or "")
    if "本鱼发了一张图" not in s:
        return s
    parts = []
    i = 0
    while True:
        a = s.find("[本鱼发了一张图", i)
        if a < 0:
            parts.append(s[i:])
            break
        b = s.find("]", a)
        if b < 0:
            parts.append(s[i:])
            break
        parts.append(s[i:a])
        i = b + 1
    s = "".join(parts)
    nl2 = chr(10) * 2
    nl3 = chr(10) * 3
    while nl3 in s:
        s = s.replace(nl3, nl2)
    return s.strip()


def _save_draw_bytes(img: bytes, tag: str = "") -> str:
    """把画出来的图存一份，返回路径。

    2026-11-06：以前 bytes 发完就丢，所以她事后「看自己刚画的图」无图可看，
    只能复述 prompt。存下来才能识图、才能回看。
    """
    if not img:
        return ""
    try:
        d = ROOT / "data" / "draw_out"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{time.strftime('%Y%m%d_%H%M%S')}_{tag or 'draw'}.png"
        p.write_bytes(img)
        return str(p)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[出图] 存盘失败：{type(e).__name__}: {e}")
        return ""


def _clean_vlm_desc(raw: str, limit: int = 110) -> str:
    """把视觉模型的描述洗成一句人话。

    2026-11-07 深查发现的：VLM 返回的是这样的东西 ——
        「好的，这是一张非常可爱的铅笔素描。根据您提供的画面描述，这是一幅……
          - **人物特征**：画面中心是一位……」
    这段被原样塞进 chat_history，主人看到的就是「她发完图还跟了一段 markdown 分析」，
    读起来像 AI 在汇报，不像她在说话。

    这里只做三件事：去掉开头客套、去掉 markdown 标记、压成一句。
    """
    import re as _re
    t = str(raw or "").strip()
    if not t:
        return ""
    # 去开头客套
    t = _re.sub(r"^(好的|当然|是的)?[，,。：:]?\s*", "", t)
    t = _re.sub(r"^(这|以下)?是?一[张幅个][^。]{0,40}[。.]", "", t)
    t = _re.sub(r"根据您?提供的[^，,。]{0,20}[，,。]?", "", t)
    # 去 markdown
    t = _re.sub(r"^#{1,6}\s*", "", t, flags=_re.M)
    t = _re.sub(r"\*\*([^*]+)\*\*", r"", t)
    t = _re.sub(r"^\s*[-*•]\s*", "", t, flags=_re.M)
    t = _re.sub(r"^\s*\d+[.、)]\s*", "", t, flags=_re.M)
    t = t.replace("---", " ")
    # 压成一句：换行变空格，多个空格合一
    t = _re.sub(r"\s+", " ", t).strip()
    # 截断到第一个句号/分号，避免又臭又长
    m = _re.search(r"^(.{20,}?[。；;])", t)
    if m:
        t = m.group(1)
    return t[:limit]


async def _record_bot_image(scope: str, speaker: str, user_id: str,
                            user_msg: str, caption: str, hist_key: str,
                            img_path: str = "") -> None:

    """发图成功后补记一轮对话：主人/群友指令 + 本鱼发图条目。

    写 chat_history、每日归档、档案长期缓冲、内存连续上下文；任一步失败只记
    warning，不打断发图主流程。caption 为画图提示词或表情包文字，用于在文字
    历史里说明本鱼发了什么图（模型看不到图片本身，只能靠这条文字回忆）。
    """
    if not scope:
        return
    # 2026-11-06：主人让她「看看自己刚发的图」时，她只能复述画图提示词 ——
    # 因为 img_path 只是记了账，从来没做过视觉识别。图片本身她一次都没看见过。
    # 这里补一次：有文件就识一眼，把真实描述拼进回忆条目。
    # 注意是「补」，不是「替」：prompt 那半保留（她确实想画这个），识图那半是实际结果。
    _real = ""
    if img_path:
        try:
            import os as _os
            if _os.path.isfile(str(img_path)) and not _comfy_busy():
                _desc = await _describe_images([str(img_path)], hint=caption[:60])
                if _desc and _desc[0]:
                    # 2026-11-07：VLM 原文带 markdown 和客套，原样存进 chat_history
                    # 会让她看起来像在做「图片分析汇报」。洗成一句人话再存。
                    _real = _clean_vlm_desc(str(_desc[0]), 110)
        except Exception as _de:  # noqa: BLE001
            logger.debug(f"[发图记忆] 识图跳过：{type(_de).__name__}: {_de}")
    if caption and _real:
        bot_entry = f"[本鱼发了一张图：想画的是「{caption}」，实际画面是「{_real}」]"
    elif caption:
        bot_entry = f"[本鱼发了一张图：{caption}]"
    elif _real:
        bot_entry = f"[本鱼发了一张图：{_real}]"
    else:
        bot_entry = "[本鱼发了一张图]"
    # 图与描述的对照表（2026-11-04）：以前只存文字，事后分不清哪句属于哪张图
    try:
        import json as _j
        _log = ROOT / "memory" / "bot_image_log.json"
        _d = _j.loads(_log.read_text(encoding="utf-8")) if _log.exists() else []
        _d.append({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "scope": scope,
                   "path": str(img_path or ""), "caption": str(caption or "")[:120]})
        _log.write_text(_j.dumps(_d[-300:], ensure_ascii=False, indent=0), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    msg = (user_msg or "").strip()
    try:
        if msg:
            await asyncio.to_thread(add_memory, speaker, user_id, scope, msg, bot_entry)
            await asyncio.to_thread(save_daily, scope, speaker, user_id, msg, bot_entry)
            await ltm.enqueue_pending(scope, speaker, msg)
        await ltm.enqueue_pending(scope, NICKNAME, bot_entry)
        if msg:
            _history[hist_key].append((speaker, msg[:cfg.context.max_len_per_msg], False))
        _history[hist_key].append((NICKNAME, bot_entry[:cfg.context.max_len_per_msg], True))
    except Exception as e:
        logger.warning(f"[发图记忆] 写入失败 scope={scope}: {type(e).__name__}: {e}")


MEAL_STATE_FILE = ROOT / "memory" / "meal_state.json"
MEAL_LABELS = {"breakfast": "早饭", "lunch": "午饭", "dinner": "晚饭", "night": "夜宵"}


def _load_meal_state() -> dict:
    today = time.strftime("%Y-%m-%d")
    try:
        data = json.loads(MEAL_STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError
    except Exception:
        data = {}
    if data.get("date") != today:
        data = {"date": today, "eaten": {}, "hunger": random.randint(55, 75),
                "energy": random.randint(55, 80), "last_meal": None, "history": [],
                "updated_ts": time.time()}
    data.setdefault("hunger", 60)
    data.setdefault("energy", 65)
    data.setdefault("eaten", {})
    data.setdefault("history", [])
    data.setdefault("updated_ts", time.time())
    return data


def _save_meal_state(st: dict) -> None:
    try:
        MEAL_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        MEAL_STATE_FILE.write_text(
            json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[吃饭] 状态保存失败：{e}")


def meal_window(hour: int) -> str | None:
    if 9 <= hour < 11:
        return "breakfast"
    if 12 <= hour < 14:
        return "lunch"
    if 18 <= hour < 20:
        return "dinner"
    if 0 <= hour < 5:
        return "night"
    return None


def eat_meal(kind: str) -> None:
    """随机吃一顿：降低饥饿、恢复精力、心情变好，并把“刚吃过”写进聊天上下文。"""
    st = _load_meal_state()
    if st["eaten"].get(kind):
        return
    drop = random.randint(30, 45)
    st["hunger"] = max(5, int(st["hunger"]) - drop)
    st["energy"] = min(100, int(st.get("energy", 65)) + random.randint(8, 16))
    st["eaten"][kind] = time.strftime("%H:%M")
    st["last_meal"] = {"kind": kind, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    st["history"] = (st.get("history") or [])[-19:] + [
        {"kind": kind, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    ]
    _save_meal_state(st)
    try:
        update_emotion("happy", 6)
    except Exception:
        pass
    try:
        write_pet_state(force_body=True)
    except Exception:
        pass
    logger.info(f"[吃饭] 吃了{MEAL_LABELS.get(kind, kind)}，饥饿度降到 {st['hunger']}%")


# 食物词族：她听懂"吃点火锅/请你吃烧烤/喂你块蛋糕"这类说法，不只认"吃饭"
_FOOD_WORDS = (
    "饭", "火锅", "面", "烧烤", "零食", "水果", "奶茶", "咖啡", "蛋糕", "汉堡", "炸鸡",
    "串", "牛排", "饺子", "包子", "粥", "汤", "甜品", "冰淇淋", "薯条", "披萨", "便当",
    "早饭", "午饭", "晚饭", "夜宵", "外卖", "好吃的", "东西",
    "东坡肉", "红烧肉", "肉", "鱼", "鸡", "虾", "蟹", "豆腐", "青菜", "米饭", "馒头",
    "月饼", "粽子", "煎饼", "米粉", "拉面", "螺蛳粉", "麻辣烫", "小龙虾", "烤鸭",
)


_FEED_COMMAND_RE = re.compile(r"^[/／]\s*(?:喂|吃|投喂|喂饭|请你吃|请你|吃点|喝点)\s*(.*)$")


def _feed_command(text: str) -> str:
    """只认 / 开头的吃饭指令（主人要求：聊天里说"我吃了啥"绝不能触发）。"""
    m = _FEED_COMMAND_RE.match((text or "").strip())
    return (m.group(1) or "喂").strip() if m else ""


def _is_feed_intent(text: str) -> bool:
    """主人是不是在**让她**吃东西（喂她/请她/买给她）。

    2026-10-31 收紧：以前只要句子里出现「吃东西」「买点东西」就算，
    主人随口说「我在吃东西」也会让她真吃一顿，干扰正常对话。
    现在必须有**明确指向她**的词（你/本鱼/大肥鱼/鲸鱼娘）才可能命中。
    """
    t = (text or "").strip()
    if not t or len(t) > 40:
        return False
    # 「我」为主语的句子（我在吃/我要吃/我买点东西）不是喂她
    if re.match(r"^我(在|要|想|去|刚|准备|打算)?", t) and not re.search(r"(给你|请你|喂你|带你)", t):
        return False
    # 要么句子里指向她（你/本鱼…），要么是明确祈使（快吃吧/继续吃/再吃点）
    if not re.search(r"(你|本鱼|大肥鱼|鲸鱼娘|鲸鱼)", t):
        if not re.search(r"(快吃|继续吃|再吃|多吃|赶紧吃|接着吃)", t):
            return False
    if re.search(r"(请你|我请你|喂你|投喂|给你买|给你带|赏你)", t):
        return True
    if re.search(r"(吃|喝)[\u4e00-\u9fa5]{0,3}(" + "|".join(_FOOD_WORDS) + r")", t):
        return True
    # "买点零食给你 / 给你带份便当" 这类（食物在动词后面）
    if re.search(r"(买|带|点|订)[\u4e00-\u9fa5]{0,3}(" + "|".join(_FOOD_WORDS) + r")", t):
        return True
    # "继续吃吧 / 再吃点 / 多吃点 / 吃吧 / 快吃" 这类没有具体食物的说法
    if re.search(r"(继续|再|多|快|赶紧|接着)?吃(吧|点|些|一口|几口|点吧)", t) or re.search(r"(快|赶紧|继续|再)吃$", t):
        return True
    # 很短的「吃XX」「喝XX」直接算（吃东坡肉、喝奶茶）；这条兜住词库没收录的菜名
    if re.match(r"^[吃喝][\u4e00-\u9fa5A-Za-z]{1,8}$", t) and not re.search(r"(不了|不下|不下|够了|饱了|过了)", t):
        return True
    return False


def feed_now(kind: str = "") -> tuple[float, float]:
    """真吃一顿；返回 (吃之前饥饿, 吃之后饥饿)。QQ 和飞书共用。"""
    win = kind or meal_window(time.localtime().tm_hour) or "snack"
    before = float(_load_meal_state().get("hunger", 60))
    if win == "snack":
        st = _load_meal_state()
        st["hunger"] = max(5, int(before) - random.randint(25, 40))
        st["energy"] = min(100, int(st.get("energy", 65)) + random.randint(5, 12))
        st["last_meal"] = {"kind": "snack", "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
        st["snack_ts"] = time.time()
        _save_meal_state(st)
        try:
            write_pet_state(force_body=True)
        except Exception:  # noqa: BLE001
            pass
        logger.info(f"[吃饭] 喂了一顿（snack），饥饿 {before:.0f}% → {st['hunger']}%")
        _hormone_event("meal")
    else:
        eat_meal(win)
        _hormone_event("meal")
    after = float(_load_meal_state().get("hunger", 60))
    return before, after


def meal_prompt() -> str:
    st = _load_meal_state()
    eaten = st.get("eaten") or {}
    parts = []
    for k in ("breakfast", "lunch", "dinner", "night"):
        if eaten.get(k):
            parts.append(f"{MEAL_LABELS[k]}{eaten[k]}")
    now = time.localtime()
    win = meal_window(now.tm_hour)
    if win and not eaten.get(win):
        parts.append(f"{MEAL_LABELS[win]}还没吃")
    last = st.get("last_meal") or {}
    return (
        f"【今天吃饭】已吃：{'、'.join(parts) if parts else '还没吃东西'}；"
        f"当前饥饿度 {int(st.get('hunger', 60))}%。"
        f"最近一顿：{MEAL_LABELS.get(last.get('kind'), '')} {str(last.get('ts') or '')[-8:]}。"
    )


SLEEP_STATE_FILE = ROOT / "memory" / "sleep_state.json"
ACTIVITY_FILE = ROOT / "memory" / "activity_state.json"
SLEEP_START_HOUR = int(os.getenv("SLEEP_START_HOUR", "0") or "0")
SLEEP_END_HOUR = int(os.getenv("SLEEP_END_HOUR", "8") or "8")


def _activity_reply_text() -> str:
    """按网页端“现在状态”回答别人问她在干嘛/去哪玩了。"""
    try:
        d = json.loads(ACTIVITY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if time.time() > float(d.get("expires") or 0):
        return "没去哪，就在这儿发呆呢，刚回神。"
    label = str(d.get("activity") or "")
    detail = str(d.get("detail") or "").strip()
    table = {
        "在玩手机上抖音": "窝着刷抖音呢，刚被推送了一堆奇奇怪怪的视频。",
        "回复抖音中": "在抖音评论区跟人拌嘴呢，等本鱼回完这条。",
        "看视频中": "在看我自己的视频，顺便瞅瞅评论区有没有人夸我。",
        "和主人聊天中": "在跟主人说话呀，别插队。",
        "QQ群聊天中": "就在群里潜水看你们聊，偶尔冒个泡。",
        "上网学习中": "在上网补课，学点新梗，免得被你们说跟不上时代。",
        "抖音制作中": "在剪抖音，别催，越催越慢。",
        "画画中": "在画画，手忙着呢。",
        "唱歌中": "在哼歌，跑调也别笑。",
        "串门中": "去别人视频底下串了个门，看看热闹。",
    }
    base = table.get(label, f"在{label.replace('中', '')}呢。" if label else "没干嘛，闲着。")
    if detail and ("群" in detail or "抖音" in detail):
        base += f"（{detail}）"
    return base


async def _sync_owner_remark(nick: str) -> None:
    """把主人外号同步成 QQ 好友备注（只影响 bot 这边的好友列表）。"""
    try:
        await get_bot().call_api("set_friend_remark", user_id=int(YOUR_QQ_ID), remark=nick)
        logger.info(f"[外号] QQ 好友备注已同步为 {nick}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[外号] 同步好友备注失败：{type(e).__name__}: {e}")


def _private_chat_active() -> bool:
    try:
        d = json.loads(ACTIVITY_FILE.read_text(encoding="utf-8"))
        return (str(d.get("activity") or "") == "和主人聊天中"
                and time.time() - float(d.get("ts") or 0) < 120)
    except Exception:
        return False


def set_activity(label: str, ttl: int = 120, detail: str = "") -> None:
    """写“现在在干啥”，超时自动回到发呆（由面板判断）。"""
    try:
        now = time.time()
        ACTIVITY_FILE.parent.mkdir(parents=True, exist_ok=True)
        ACTIVITY_FILE.write_text(
            json.dumps({"activity": label, "detail": detail, "ts": now, "expires": now + ttl},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def _in_sleep_window(hour: int) -> bool:
    start, end = SLEEP_START_HOUR, SLEEP_END_HOUR
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _sleep_start_ts() -> float:
    now = time.localtime()
    ts = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, SLEEP_START_HOUR, 0, 0, 0, 0, -1))
    if ts > time.time():
        ts -= 86400
    return ts


def _load_sleep_state() -> dict:
    try:
        data = json.loads(SLEEP_STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"sleeping": False, "start_ts": 0, "last_sleep_seconds": 0, "wake_until": 0}


def _save_sleep_state(st: dict) -> None:
    try:
        SLEEP_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        SLEEP_STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[睡觉] 状态保存失败：{e}")


def _called_in_group(event, text: str) -> bool:
    """群里这条消息是不是在叫她（@ 或者点名）。睡觉时段只有这种消息才会把她叫醒。"""
    try:
        if event.is_tome():
            return True
    except Exception:  # noqa: BLE001
        pass
    t = str(text or "")
    try:
        from nonebot.adapters.onebot.v11 import MessageSegment  # noqa: F401
    except Exception:  # noqa: BLE001
        pass
    names = {NICKNAME, "白饭大王", "大肥鱼", "肥鱼", "小肥鱼", "鲸鱼娘"}
    return any(str(x) and str(x) in t for x in names)


def is_sleeping() -> bool:
    st = _load_sleep_state()
    if float(st.get("wake_until") or 0) > time.time():
        return False
    return _in_sleep_window(time.localtime().tm_hour)


def sleep_tick() -> None:
    st = _load_sleep_state()
    now = time.time()
    sleeping_now = _in_sleep_window(time.localtime().tm_hour)
    if sleeping_now:
        if not st.get("sleeping"):
            st["start_ts"] = _sleep_start_ts()
            _hormone_event("sleep")   # 入睡：褪黑素 +20
        st["sleeping"] = True
    else:
        if st.get("sleeping") and st.get("start_ts"):
            st["last_sleep_seconds"] = max(0, now - float(st["start_ts"]))
            _hormone_event("wake")    # 醒来：褪黑素 -20、多巴胺 +4
        st["sleeping"] = False
    _save_sleep_state(st)


def wake_up(minutes: int = 120) -> None:
    st = _load_sleep_state()
    now = time.time()
    dur = random.randint(30, 90)
    st["wake_until"] = now + minutes * 60
    st["woke_at"] = now
    st["waking_start"] = now
    st["waking_until"] = now + dur
    st["sleeping"] = False
    _save_sleep_state(st)
    logger.info(f"[睡觉] 被叫醒，{dur} 秒内逐渐清醒")


def just_woke_prompt() -> str:
    st = _load_sleep_state()
    until = float(st.get("waking_until") or 0)
    if until and time.time() < until:
        return ("【正在起床】你刚被叫醒，还在迷糊：揉眼睛、反应慢半拍、说话软乎乎/带起床气，30~90 秒内慢慢清醒，"
                "别一上来就精神满满。但被叫起来就得真的接话——可以抱怨两句、可以迷糊地答，"
                "不许只丢一句「嗯」「别吵」就完事，对方问什么要答出来。\n")
    return ""


def sleep_status() -> dict:
    st = _load_sleep_state()
    now = time.time()
    sleeping = bool(st.get("sleeping")) and is_sleeping()
    start = float(st.get("start_ts") or 0)
    if sleeping and start:
        seconds = max(0, now - start)
        h, m = int(seconds // 3600), int((seconds % 3600) // 60)
        return {"sleeping": True, "seconds": seconds, "percent": 0.0,
                "text": f"睡觉中 · 已睡 {h} 小时 {m} 分"}
    wstart = float(st.get("waking_start") or 0)
    wuntil = float(st.get("waking_until") or 0)
    if wuntil > now and wuntil > wstart:
        pct = min(100.0, max(0.0, (now - wstart) / (wuntil - wstart) * 100.0))
        return {"sleeping": False, "seconds": 0.0, "percent": round(pct, 1),
                "text": f"正在醒 {pct:.0f}% · 迷迷糊糊"}
    seconds = float(st.get("last_sleep_seconds") or 0)
    h, m = int(seconds // 3600), int((seconds % 3600) // 60)
    return {"sleeping": False, "seconds": seconds, "percent": 100.0,
            "text": (f"醒着 · 刚才睡了 {h} 小时 {m} 分" if seconds else "醒着")}


_QQ_OUTBOX = ROOT / "memory" / "qq_outbox.json"


async def _qq_outbox_tick() -> None:
    """把 memory/qq_outbox.json 里排队的文件/视频私聊发给主人（发完清空）。

    用途：本地生成的东西（视频、图）没法从这里直接进 QQ，就排队，几十秒后由机器人代发。
    """
    try:
        if not _QQ_OUTBOX.exists():
            return
        items = json.loads(_QQ_OUTBOX.read_text(encoding="utf-8")) or []
    except Exception:  # noqa: BLE001
        return
    if not isinstance(items, list) or not items:
        return
    try:
        from nonebot.adapters.onebot.v11 import MessageSegment as _Seg
        from nonebot import get_bot as _get_bot
        bot = _get_bot()
        left = []
        for it in items:
            path = str((it or {}).get("path") or "")
            cap = str((it or {}).get("caption") or "")
            if not path or not os.path.exists(path):
                continue
            try:
                await bot.send_private_msg(user_id=int(YOUR_QQ_ID),
                                           message=_Seg.video(path) if path.lower().endswith((".mp4", ".mov"))
                                           else _Seg.image(path))
                if cap:
                    await bot.send_private_msg(user_id=int(YOUR_QQ_ID), message=cap)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[投递队列] 发送失败 {path}: {type(e).__name__}: {e}")
                left.append(it)
        _QQ_OUTBOX.write_text(json.dumps(left, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[投递队列] 异常：{type(e).__name__}: {e}")


from nonebot import get_driver as _get_driver


@_get_driver().on_startup
async def _start_outbox_job() -> None:
    scheduler.add_job(_qq_outbox_tick, "interval", seconds=20, id="qq_outbox",
                      replace_existing=True)
    logger.info("[投递队列] 已启动：每 20 秒检查 memory/qq_outbox.json")


@scheduler.scheduled_job("interval", minutes=5)
async def sleep_tick_job():
    try:
        sleep_tick()
        try:
            await index_sticker_folder(4)
        except Exception:  # noqa: BLE001
            pass
        try:
            from thoughts import tick as _thoughts_tick
            _thoughts_tick()
        except Exception:  # noqa: BLE001
            pass
        try:
            from weather import refresh as _weather_refresh
            await _weather_refresh()
        except Exception:  # noqa: BLE001
            pass
        try:
            emotion_tick()
        except Exception:  # noqa: BLE001
            pass
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[睡觉] tick 异常：{e}")


RELATION_CACHE: dict[str, float] = {}


def _user_msg_count(user_id: str, scope: str) -> int:
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        cur = conn.execute(
            "SELECT COUNT(*) FROM chat_history WHERE user_id = ? AND scope = ?",
            (str(user_id), scope),
        )
        n = int(cur.fetchone()[0] or 0)
        conn.close()
        return n
    except Exception:
        return 0


RELATION_FILE = ROOT / "memory" / "relation_state.json"
NEG_TAGS = ("讨厌", "烦", "不理")


def _load_rel_state() -> dict:
    try:
        data = json.loads(RELATION_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_rel_state(data: dict) -> None:
    try:
        RELATION_FILE.parent.mkdir(parents=True, exist_ok=True)
        RELATION_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def relation_update(user_id: str, scope: str, neg_add: int = 0, ignore_minutes: int = 0) -> None:
    """关系标签：负面会随时间衰减；正面只按 SQL 消息条数。全程不调模型。"""
    uid = str(user_id)
    now = time.time()
    data = _load_rel_state()
    ent = data.get(uid) or {}
    ent.setdefault("neg", 0)
    ent.setdefault("last", now)
    ent.setdefault("ignore_until", 0)
    ent.setdefault("base_tag", "")

    hours = max(0.0, (now - float(ent.get("last") or now)) / 3600.0)
    ent["neg"] = max(0.0, float(ent["neg"]) - hours * 4.0)
    ent["last"] = now
    if neg_add:
        ent["neg"] = min(100.0, float(ent["neg"]) + neg_add)
    if ignore_minutes:
        ent["ignore_until"] = max(float(ent.get("ignore_until") or 0), now + ignore_minutes * 60)

    current = (get_tag(uid, scope) or "").strip()
    if current and not current.startswith(("熟悉", "朋友") + NEG_TAGS):
        ent["base_tag"] = current

    if float(ent.get("ignore_until") or 0) > now:
        label = "不理"
    elif ent["neg"] >= 60:
        label = "讨厌"
    elif ent["neg"] >= 30:
        label = "烦"
    else:
        count = _user_msg_count(uid, scope)
        if count >= 1000:
            label = f"朋友·{count}条"
        elif count >= 100:
            label = f"熟悉·{count}条"
        else:
            label = ent.get("base_tag") or ""

    data[uid] = ent
    _save_rel_state(data)
    if label != current and (label or current in NEG_TAGS):
        try:
            set_tag(uid, label or "", scope)
            logger.info(f"[关系] {uid} -> {label or '无'}（neg={ent['neg']:.0f}）")
        except Exception:
            pass


def bump_negative(user_id: str, scope: str, amount: int, ignore_minutes: int = 0) -> None:
    relation_update(user_id, scope, neg_add=amount, ignore_minutes=ignore_minutes)


def is_ignoring(user_id: str) -> bool:
    ent = _load_rel_state().get(str(user_id)) or {}
    return float(ent.get("ignore_until") or 0) > time.time()


def maybe_update_relation_tag(user_id: str, scope: str) -> None:
    key = f"{scope}:{user_id}"
    now = time.time()
    if now - RELATION_CACHE.get(key, 0) < 600:
        return
    RELATION_CACHE[key] = now
    relation_update(str(user_id), scope)


def get_physical_status():
    hour = time.localtime().tm_hour
    st = _load_meal_state()
    now = time.time()
    elapsed_min = max(0.0, (now - float(st.get("updated_ts") or now)) / 60.0)
    st["hunger"] = min(100.0, float(st.get("hunger", 60)) + elapsed_min * 0.12)
    st["updated_ts"] = now
    _save_meal_state(st)
    hunger = int(round(st["hunger"]))
    energy = int(st.get("energy", 65))
    if 0 <= hour < 6:
        energy = max(5, energy - 20)
        desc = "(*狂打哈欠*) 困死了，本鱼要睡觉..."
    elif 6 <= hour < 11:
        desc = "(*伸懒腰*) 早上好，本鱼昨晚梦见吃白米饭了。"
    elif 11 <= hour < 14:
        desc = "(*肚子咕咕叫*) 中午了！谁请本鱼吃红烧肉？" if hunger > 55 else "(*摸着肚子*) 刚吃完，满足捏。"
    elif 14 <= hour < 18:
        desc = "(*嚼着零食*) 下午茶时间，摸鱼中..."
    elif 18 <= hour < 20:
        desc = "(*盯着饭桌*) 晚饭时间到，今天吃什么捏？" if hunger > 50 else "(*打了个饱嗝*) 晚饭吃好了。"
    else:
        desc = "(*瘫在椅子上*) 今天也是累成狗的一天。"
    return hunger, energy, desc


def _refresh_body_cache(force: bool = False):
    """饥饿/精力按时段生成并缓存 5 分钟，避免每次调用都随机跳变。"""
    now = time.time()
    if not force and now - PET_BODY_CACHE["ts"] < PET_STATE_REFRESH_SEC:
        return PET_BODY_CACHE["hunger"], PET_BODY_CACHE["energy"], PET_BODY_CACHE["body_desc"]
    hunger, energy, desc = get_physical_status()
    PET_BODY_CACHE.update(hunger=hunger, energy=energy, body_desc=desc, ts=now)
    return hunger, energy, desc


def _pick_pet_mood() -> tuple[str, int]:
    """选代表群心情：优先 TARGET_GROUP_ID，否则取心情值最高的活跃群；兜底开心/50。"""
    if TARGET_GROUP_ID and TARGET_GROUP_ID in GROUP_MOOD_STATE:
        return GROUP_MOOD_STATE[TARGET_GROUP_ID], GROUP_MOOD_VAL_STATE[TARGET_GROUP_ID]
    if GROUP_MOOD_VAL_STATE:
        gid = max(GROUP_MOOD_VAL_STATE, key=lambda g: GROUP_MOOD_VAL_STATE[g])
        return GROUP_MOOD_STATE[gid], GROUP_MOOD_VAL_STATE[gid]
    return "开心", 50


def current_pet_mood() -> tuple[str, int]:
    """网页情绪圆盘的主导情绪 → 桌宠心情/动画用的状态。"""
    try:
        vals = _load_emotion()
        top = max(EMOTIONS, key=lambda k: vals.get(k, 0))
        pct = int(round(vals.get(top, 0)))
        mapping = {
            "开心": "开心", "害羞": "开心", "怒": "生气", "烦": "生气",
            "害怕": "委屈", "丧": "委屈", "累": "摸鱼",
        }
        return mapping.get(top, top), pct
    except Exception:
        return _pick_pet_mood()


def write_pet_state(force_body: bool = False) -> None:
    """把当前心情/饥饿/精力/今日人设落盘到 pet_state.json，供 sync_memory 注入桌宠。"""
    try:
        mood, mood_val = current_pet_mood()
        hunger, energy, body_desc = _refresh_body_cache(force_body)
        payload = {
            "mood": mood,
            "mood_val": mood_val,
            "hunger": hunger,
            "energy": energy,
            "daily_mood": CURRENT_DAILY_MOOD,
            "body_desc": body_desc,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        PET_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        PET_STATE_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        logger.error(f"桌宠状态落盘失败: {e}")


PET_LIVE_FILE = ROOT / "memory" / "pet_live.json"
QZONE_SAY_STATE = ROOT / "memory" / "qzone_auto_say.json"
IMAGE_CALL_PRICE = float(os.getenv("IMAGE_PRICE_PER_CALL", "0.20") or "0.20")
TTS_1K_PRICE = float(os.getenv("TTS_PRICE_PER_1K", "0.30") or "0.30")


def record_image_cost(n: int = 1) -> None:
    try:
        token_meter.record({"total_tokens": 0}, "image", cost=IMAGE_CALL_PRICE * max(1, n), provider="ark")
    except Exception:
        pass


def record_tts_cost(chars: int) -> None:
    try:
        token_meter.record({"total_tokens": 0}, "tts",
                           cost=max(0, chars) / 1000.0 * TTS_1K_PRICE, provider="ark")
    except Exception:
        pass


async def maybe_spontaneous_qzone_say() -> None:
    """她上线聊天时，随机把今天发生的事发一条 QQ 空间说说；每天最多 3 条。

    注意：定时计划（plugins/qzone_auto.py）开启时这条通道让路，避免一天双发。
    """
    if os.getenv("QZONE_AUTO_ENABLED", "true").lower() in ("1", "true", "yes"):
        return
    try:
        from plugins import qzone_sync
        if not qzone_sync.load_state().get("enabled"):
            return
        hour = time.localtime().tm_hour
        if not (9 <= hour < 22):
            return
        try:
            st = json.loads(QZONE_SAY_STATE.read_text(encoding="utf-8"))
        except Exception:
            st = {}
        today = time.strftime("%Y-%m-%d")
        if st.get("date") != today:
            st = {"date": today, "count": 0, "last_at": 0, "history": []}
        if int(st.get("count", 0)) >= 3:
            return
        if time.time() - float(st.get("last_at") or 0) < 2 * 3600:
            return
        if random.random() > 0.06:
            return

        recent = ""
        try:
            rows = await asyncio.to_thread(
                get_recent_memory, 5, f"private_{YOUR_QQ_ID}"
            )
            recent = "；".join(
                f"{r.get('speaker') or '主人'}: {(r.get('message') or '')[:40]}"
                for r in (rows or [])[::-1]
            )
        except Exception:
            pass
        prompt = (
            "你是「蓝色大肥鱼」，圆滚滚的蓝色鲸鱼拟人少女，傲娇嘴损但黏主人。"
            f"今天的心情是{CURRENT_DAILY_MOOD}。\n"
            f"今天聊过：{recent or '还没怎么聊'}\n"
            "写一条 30~50 字的 QQ 空间说说，讲今天真实发生的一件小事或心情，"
            "像真人发朋友圈；不要流水账、不要报任务、不要自称AI、不要解释；只输出正文。"
        )
        text = (await call_ai_raw(prompt, "deepseek", temperature=0.95, max_tokens=250)).strip()
        if len(text) < 10:
            return
        ok, msg = await qzone_sync.publish_qzone(text)
        if not ok:
            logger.warning(f"[空间说说] 发布失败：{msg}")
            return
        st["count"] = int(st.get("count", 0)) + 1
        st["last_at"] = time.time()
        st["history"] = (st.get("history") or [])[-19:] + [
            {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "text": text}
        ]
        QZONE_SAY_STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            await get_bot().send_private_msg(
                user_id=int(YOUR_QQ_ID),
                message=f"(*刚发完空间*) 本鱼刚在 QQ 空间发了条说说：\n{text}",
            )
        except Exception:
            pass
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[空间说说] 随机发布异常：{e}")


def write_pet_live(text: str, speaker: str = "主人") -> None:
    """把刚发出的 QQ 回复写给桌宠头顶气泡；不调用任何大模型。"""
    try:
        mood, mood_val = current_pet_mood()
        payload = {
            "reply": str(text or "")[:300],
            "speaker": speaker,
            "mood": mood,
            "mood_val": mood_val,
            "daily_mood": CURRENT_DAILY_MOOD,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "seq": time.time(),
        }
        PET_LIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
        PET_LIVE_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"桌宠气泡落盘失败: {e}")

# =====================================================================
# 📢 群聊主动回复频率（仅主人可调，持久化到 memory/reply_rate.json）
# 口径：只缩放「没人@时主动插嘴 + 看图吐槽」的概率；被@必回、凌晨拒回、
# 好感低忽略、记仇忽略等规则一律不受影响；私聊一对一不参与本频率。
# =====================================================================
RATE_FILE = ROOT / "memory" / "reply_rate.json"
RATE_MIN, RATE_MAX, RATE_STEP = 0, 100, 10
try:
    RATE_DEFAULT = max(RATE_MIN, min(RATE_MAX, int(os.getenv("REPLY_RATE_DEFAULT", "60"))))
except (TypeError, ValueError):
    RATE_DEFAULT = 60
# 基准概率：rate=100 时维持现状手感；rate=0 时主动闭嘴、不看图
RATE_IMAGE_BASE = 0.25  # 没人@且不在@窗口内时，纯图被她吐槽的基准概率
_RATE: int | None = None


def _load_reply_rate() -> int:
    global _RATE
    try:
        if RATE_FILE.exists():
            data = json.loads(RATE_FILE.read_text(encoding="utf-8"))
            v = int(data.get("group_reply_rate", RATE_DEFAULT))
            _RATE = max(RATE_MIN, min(RATE_MAX, v))
        else:
            _RATE = RATE_DEFAULT
    except Exception as e:
        logger.warning(f"[回复频率] 读取失败，用默认 {RATE_DEFAULT}%: {e}")
        _RATE = RATE_DEFAULT
    return _RATE


def save_reply_rate(rate: int) -> None:
    global _RATE
    _RATE = max(RATE_MIN, min(RATE_MAX, int(rate)))
    try:
        RATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        RATE_FILE.write_text(
            json.dumps(
                {"group_reply_rate": _RATE, "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
    except Exception as e:
        logger.error(f"[回复频率] 保存失败: {e}")


def current_rate() -> int:
    if _RATE is None:
        _load_reply_rate()
    return _RATE


def rate_k() -> float:
    """主动回复概率系数 0~1：基准概率乘上这个系数。"""
    return current_rate() / 100.0


def _proactive_today() -> str:
    return time.strftime("%Y-%m-%d")


def proactive_allowed(gid) -> tuple[bool, str]:
    """群级主动发言闸门。返回 (是否放行, 拦截原因)。
    冷却窗口内或当日主动条数达到上限时拒绝主动开口。"""
    daily = GROUP_PROACTIVE_DAILY[gid]
    if daily["date"] != _proactive_today():
        daily["date"] = _proactive_today()
        daily["count"] = 0
    # 2026-11-06：冷却不再固定 —— 被无视得多就拉长，老有人接就缩短。
    # 这就是「能动性」和「随机发言」的分界线：说过的话有没有回音，会影响下一句。
    _mul = 1.0
    try:
        from plugins import proactive_feedback as _pf
        _mul = float(_pf.adjust(gid).get("cooldown_mul") or 1.0)
    except Exception:  # noqa: BLE001
        pass
    if time.time() - GROUP_PROACTIVE_COOLDOWN[gid] < PROACTIVE_COOLDOWN_SEC * _mul:
        return False, "冷却"
    if daily["count"] >= PROACTIVE_DAILY_LIMIT:
        return False, "当日上限"
    return True, ""


def mark_proactive_sent(gid, text: str = "") -> None:
    """记录一次主动发言：刷新冷却时间戳、累加当日条数、写进反馈日志等结果。

    2026-11-06：多收一个 text —— 有了它才能在她之后有人开口时判断「接没接住」。
    """
    if text:
        try:
            from plugins import proactive_feedback as _pf
            _pf.log_sent(gid, text)
        except Exception:  # noqa: BLE001
            pass
    GROUP_PROACTIVE_COOLDOWN[gid] = time.time()
    daily = GROUP_PROACTIVE_DAILY[gid]
    if daily["date"] != _proactive_today():
        daily["date"] = _proactive_today()
        daily["count"] = 0
    daily["count"] += 1


def rate_label(rate: int) -> str:
    if rate <= 0: return "彻底潜水（不主动插嘴、不看图）"
    if rate <= 30: return "高冷模式（很少主动接话）"
    if rate <= 60: return "正常摸鱼（维持日常手感）"
    if rate <= 90: return "活跃话痨（爱插嘴爱看图）"
    return "超级话痨（逢话就接、逢图就看）"


def parse_rate_arg(arg: str) -> tuple[str, int | None]:
    """解析频率指令参数。返回 (动作, 数值)；动作: view/up/down/set/invalid/unknown。"""
    a = (arg or "").strip()
    if not a:
        return "view", None
    if a in ("降低", "减", "低", "少一点", "降", "往下", "调低", "少", "减一点"):
        return "down", None
    if a in ("提高", "加", "高", "多一点", "升", "往上", "调高", "多", "加一点"):
        return "up", None
    m = re.fullmatch(r"(\d{1,3})", a)
    if m:
        v = int(m.group(1))
        return ("set", v) if RATE_MIN <= v <= RATE_MAX else ("invalid", None)
    return "unknown", None


# ---------------------------------------------------------------- 指令前缀（/）
# 主人定：所有"强制指令"必须带 / 前缀，否则按普通聊天处理，
# 避免"画图""唱歌""别看了"这类词在正常对话里被误判。
COMMAND_WORDS = (
    # 画图 / 改图
    "画图", "画你自己", "画个你", "改图", "编辑图片", "修图", "P图", "p图",
    # 语音 / 唱歌
    "唱歌", "点歌", "重唱", "发语音", "语音说",
    # 陪游戏
    "看我打游戏", "看我玩", "围观我", "开始看游戏", "点评一下", "点评我",
    "看我操作", "截个图看看", "别看了", "不看了", "没打游戏",
    # 状态 / 查询
    "状态", "状态报告", "体检", "帮助", "指令", "余额", "日报", "好感度",
    "频率", "回复频率", "记忆", "学习", "学习档案", "档案", "记忆统计",
    "词库", "词典", "本群词库", "改词", "删词",
    # 每日动态 / 日记
    "说说", "动态", "朋友圈", "日记", "主动私聊",
    # 引擎 / 设定
    "切换大脑", "切换画笔", "人设", "画图设定", "人设词", "出图设定",
    "测试", "回滚", "重启",
    # 群功能
    "偷了", "贴标签", "做个表情包", "打开", "深挖",
    # B站 / 抖音
    "B站", "搜B站", "抖音评论", "串门",
)


# ---- 群聊去油腻：动作括号 / 尾巴重复控制（2026-11-04）----
_recent_group_style: dict = {}   # gid -> [ {"action": bool, "tail": bool}, ... ] 最近 4 条


def _deflavor_group(reply: str, gid) -> str:
    """群聊回复去"人机味"：统一括号、限动作频率、重复的尾巴换掉。

    实测（60 条群聊）：67% 带（尾巴一甩），还有 11 条以 "*（" 开头，格式还乱；
    私聊同样内容 0/60 带动作 —— 说明是群聊惯性，不是人设。
    """
    import random as _r
    r = str(reply or "").strip()
    if not r:
        return r
    # ① 统一格式：*（…）* → （…）
    r = re.sub(r"\*\s*[（(]", "（", r)
    r = re.sub(r"[）)]\s*\*", "）", r)
    r = re.sub(r"\*\s*([一-龥]{1,8})\s*\*", r"（）", r)

    key = str(gid)
    hist = _recent_group_style.setdefault(key, [])
    last = hist[-4:]

    # ② 动作括号：最近 4 条里已经 2 条带过，就强制去掉；否则 60% 概率保留
    acts = re.findall(r"[（(][^）)]{1,14}[）)]", r)
    if acts:
        _recent_actions = sum(1 for h in last if h.get("action"))
        if _recent_actions >= 2 or _r.random() < 0.4:
            for a in acts:
                r = r.replace(a, "")
            r = re.sub(r"^[\s，,。.、]+", "", r).strip()

    # ③ 尾巴：最近 3 条里出现过就换掉（改耳朵/鳍/裙角，或直接删）
    used_tail = any(h.get("tail") for h in last[-3:])
    if used_tail and "尾巴" in r:
        r = r.replace("尾巴一甩", _r.choice(["耳朵抖了下", "鳍摆了摆", "裙角晃了晃"]))
        r = r.replace("尾巴一翘", _r.choice(["耳朵一竖", "裙角一提"]))
        r = r.replace("尾巴", _r.choice(["耳朵", "鳍"]))

    # ④ 只保留一个动作括号（模型偶尔一口气写两个）
    acts = re.findall(r"[（(][^）)]{1,14}[）)]", r)
    if len(acts) > 1:
        for a in acts[1:]:
            r = r.replace(a, "", 1)
        r = re.sub(r"\s{2,}", " ", r).strip()

    hist.append({"action": bool(re.findall(r"[（(][^）)]{1,14}[）)]", r)), "tail": "尾巴" in r})
    _recent_group_style[key] = hist[-8:]
    return r


def _strip_cmd_prefix(text: str) -> tuple[str, bool]:
    """返回 (去掉 / 之后的正文, 是否是指令)。

    带 / 才算指令；不带的原样返回并标记 False。
    """
    t = str(text or "")
    if t.startswith(("/", "／")):
        return t.lstrip("/／").strip(), True
    return t, False


def _maybe_passthrough(text: str) -> str:
    """不带 / 但长得像指令 → 加零宽前缀，让所有 startswith 判定失效（按聊天走）。"""
    t = str(text or "")
    if t[:1] in ("/", "／"):
        return t
    for w in COMMAND_WORDS:
        if t.startswith(w):
            return "\u200b" + t
    return t


def _split_rate_command(user_msg: str) -> str | None:
    """识别频率指令并剥掉前缀，返回参数文本；非频率指令返回 None。"""
    for p in ("/回复频率", "/频率", "回复频率", "回复率", "主动回复率", "频率"):
        if user_msg == p:
            return ""
        if user_msg.startswith(p + " "):
            return user_msg[len(p):].strip()
    return None


def _rate_panel_text(rate: int) -> str:
    return (
        "📢 **群聊主动回复频率**\n"
        "━━━━━━━━━━━━━━━\n"
        f"当前：**{rate}%**（{rate_label(rate)}）\n"
        "━━━━━━━━━━━━━━━\n"
        "📝 用法：`/频率 降低` 减 10% · `/频率 提高` 加 10% · `/频率 80` 直接设 0~100\n"
        "💡 只调没人@时主动插嘴和看图的概率；@本鱼照样必回，私聊不受影响。"
    )


def _rate_reply_text(action: str, rate: int) -> str:
    tail = f"现在 **{rate}%**（{rate_label(rate)}）"
    if action == "up":
        return f"(*拍桌*) 行吧，本鱼话多一点！{tail}"
    if action == "down":
        return f"(*缩到角落*) 本鱼闭嘴就是了…{tail}"
    if action == "set":
        return f"(*点头*) 收到，主动回复率调到 {tail}"
    return _rate_panel_text(rate)

def _hormone_event(name: str, scale: float = 1.0) -> None:
    """内分泌事件（失败静默，不影响主流程）。"""
    try:
        from plugins import hormones
        hormones.apply_event(name, scale)
    except Exception:  # noqa: BLE001
        pass


# 亲密内容识别（2026-11-05）：只在主人私聊路径上跑，判到就喂 intimate / aroused。
# 分两档是为了让"想你了"和"想要你"喂出不同的曲线，而不是一个开关。
_INTIMATE_STRONG = re.compile(
    r"(摸你|摸我|亲你|亲我|舔|咬你|脱掉|脱了|床上|抱紧|压着|喘|湿了|硬了|"
    r"想要你|要你|进去|进来|上你|想上|你身体|腿间|搂腰|腰窝|胸口|屁股|你下面|我下面|"
    r"插进|操你|操我|做爱|自慰|摸到|好想要|受不了|顶不住|叫出来|再用力|"
    # 2026-11-05 补：体位与动作类。原词表只有情话，实测主人说「侧着输出」「每秒20下」
    # 一条都不命中，整场对话激素没动。私聊语境够窄，这些词误伤概率很低。
    r"侧着|趴着|骑上|坐上来|后入|背对|翻过来|抽插|挺进|顶到|磨蹭|揉|含住|吞下|"
    r"几[下秒]|每秒|深处|腿根|分开腿|夹紧|腰上|往上顶|慢点|快一点|再深)"
)
_INTIMATE_SOFT = re.compile(
    r"(想你|抱抱|亲一下|亲亲|喜欢你|爱你|心动|脸红|可爱|宝贝|老婆|"
    r"贴贴|蹭蹭|抱一下|靠近点|撩|暧昧|陪睡|摸摸|想抱你)"
)


def _intimate_check(text: str) -> str | None:
    """返回要喂的激素事件名；判不到返回 None。只认字面，不做语义推断（省 token）。

    2026-11-05：优先用 memory/intimate_lexicon.json 的词表（改文件即刻生效），
    读不到才回退下面内置的正则。词库可扩充，不用改代码。
    """
    t = str(text or "")[:200]
    if not t:
        return None
    try:
        _lex = _intimate_lexicon()
        _st = _lex.get("detect_strong") or []
        _so = _lex.get("detect_soft") or []
        if _st and re.search("(" + "|".join(_st) + ")", t):
            return "aroused"
        if _so and re.search("(" + "|".join(_so) + ")", t):
            return "intimate"
        if _st or _so:
            return None
    except Exception:  # noqa: BLE001
        pass
    if _INTIMATE_STRONG.search(t):
        return "aroused"
    if _INTIMATE_SOFT.search(t):
        return "intimate"
    return None


# ---- 情欲词库（2026-11-05）----
# memory/intimate_lexicon.json：detect_* 用于识别，categories + hint_by_arousal 用于给她词。
_INTIMATE_LEXICON_FILE = ROOT / "memory" / "intimate_lexicon.json"
_IL_CACHE: dict = {"mtime": -1.0, "raw": {}}


def _intimate_lexicon() -> dict:
    """按文件 mtime 缓存，改完不用重启。"""
    try:
        _m = _INTIMATE_LEXICON_FILE.stat().st_mtime
    except Exception:  # noqa: BLE001
        return {}
    if _IL_CACHE["mtime"] == _m and _IL_CACHE["raw"]:
        return _IL_CACHE["raw"]
    try:
        _d = json.loads(_INTIMATE_LEXICON_FILE.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        _d = {}
    _IL_CACHE["mtime"] = _m
    _IL_CACHE["raw"] = _d
    return _d


# ---- 她自己的欲望手册（2026-11-05）----
# 来源：bluntcore-intimacy-lab 的「通用欲望手册」骨架，改写成大肥鱼自己的语气。
# 核心判断照抄它那句：能写黄 ≠ 这个角色真的会表达欲望。
# 只在情欲 ≥40 时注入，按档位取前几节（省 token）。
_DESIRE_FILE = ROOT / "memory" / "desire_manual.md"


def _desire_manual(arousal: float) -> str:
    if arousal < 40:
        return ""
    try:
        _txt = _DESIRE_FILE.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return ""
    _parts = re.split(r"\n(?=## )", _txt)
    _secs = [p.strip() for p in _parts[1:] if p.strip()]
    if not _secs:
        return ""
    _n = 3 if arousal < 60 else len(_secs)
    return "【我心里的想要】（这是你的内心，不是台词，别念出来）\n" + "\n\n".join(_secs[:_n])


def _intimate_vocab_hint(arousal: float) -> str:
    """按情欲档位给她一批可用的词，免得描述翻来覆去就那几句。

    分级是为了别一上来就往身上招呼：40 只给情绪和感受，80 才给部位和体位。
    """
    try:
        if arousal < 40:
            return ""
        d = _intimate_lexicon()
        ladder = d.get("hint_by_arousal") or {}
        cats = d.get("categories") or {}
        pick = None
        for th in sorted((int(k) for k in ladder if str(k).isdigit()), reverse=True):
            if arousal >= th:
                pick = ladder.get(str(th)) or ladder.get(th)
                break
        if not pick:
            return ""
        want = pick.get("categories") or []
        limit = int(pick.get("limit") or 8)
        words: list[str] = []
        per = max(3, limit // max(1, len(want)))
        for c in want:
            words.extend(((cats.get(c) or {}).get("words") or [])[:per])
        if not words:
            return ""
        return ("【可以用这些词】别翻来覆去就那几句，从这里挑着用（自然掺进去，别像背词表）："
                + "、".join(words[:limit]))
    except Exception:  # noqa: BLE001
        return ""


def _at_target_label(event, gid) -> str:
    """这条消息 @ 了谁 —— 返回可读标签；@ 她自己返回「我」；没有 @ 返回空串。

    2026-11-08：`get_plaintext()` 内部是 `extract_plain_text()`，**会把 at 段整个丢掉**。
    所以别人说「@张三 你今天吃啥」，她看到的历史里只剩「你今天吃啥」，以为在问她。
    这就是"记错人"最直接的一条路。这里把 @ 目标单独捞出来，拼进历史文本。
    """
    try:
        for seg in event.get_message():
            if seg.type != "at":
                continue
            qq = str(seg.data.get("qq") or "")
            if not qq or qq == "all":
                continue
            if qq == str(getattr(event, "self_id", "")):
                return "我"
            try:
                from plugins import nick_names as _nn
                return _nn.first_name(gid, qq) or qq
            except Exception:  # noqa: BLE001
                return qq
    except Exception:  # noqa: BLE001
        pass
    return ""


def _aimed_at_me(event, text: str) -> bool:
    """这句话是不是冲她来的（群友互怼不算）。

    命中任一即算：
      · 消息里有 @她
      · 回复的是她的消息
      · 正文里出现她的名字 / 本鱼 / 大肥鱼 / 鲸鱼娘 / 鲸鱼
    """
    try:
        for seg in event.get_message():
            if seg.type == "at" and str(seg.data.get("qq")) == str(event.self_id):
                return True
    except Exception:  # noqa: BLE001
        pass
    try:
        reply = getattr(event, "reply", None)
        if reply is not None and str(getattr(reply, "sender", {}).get("user_id")) == str(event.self_id):
            return True
    except Exception:  # noqa: BLE001
        pass
    t = str(text or "")
    names = {str(NICKNAME), "白饭大王", "本鱼", "大肥鱼", "鲸鱼娘", "鲸鱼"}
    return any(n and n in t for n in names)


def _trauma_check(gid, uid: str, text: str, nickname: str, aimed: bool = True) -> None:
    """群聊里检测创伤线索：命中就抬皮质醇，并把这次冒犯记进创伤库。

    aimed=False（群友之间互怼、没指向她）→ 直接跳过：不抬皮质醇、不记创伤、不加火气。
    """
    if not aimed:
        return
    try:
        from plugins import trauma_mem, hormones
        scope = f"group_{gid}"
        hit = trauma_mem.match(text, scope)
        if hit:
            hormones.apply_event("insult", 0.6)
            # 回避档：群里被凶 → 记到「对群友的火气」，不污染对主人的心结（2026-11-04 拆开）
            try:
                from plugins import withdraw
                withdraw.take_offense(target="group")
                withdraw.snapshot_blame(text)
            except Exception:  # noqa: BLE001
                pass
            try:
                bump_negative(str(uid), scope, 6)   # 同文件内直接调用
            except Exception:  # noqa: BLE001
                pass
            logger.info(f"[创伤] 线索命中：{hit.get('subject')} / charge={hit.get('charge')}")
        if trauma_mem.should_record(text, is_owner=False):
            item = trauma_mem.register(scope, nickname, f"{nickname}：{text[:60]}", text)
            if item:
                logger.info(f"[创伤] 记下一条：{nickname}（charge={item.get('charge')}）")
    except Exception:  # noqa: BLE001
        pass


def _bili_watching() -> bool:
    """她正在看 B 站视频时，别被聊天状态覆盖（看片可能好几分钟）。"""
    try:
        from plugins import bilibili_daily
        return bilibili_daily.watching()
    except Exception:  # noqa: BLE001
        return False


def get_relationship_prompt(gid, user_id, affinity, tag):
    global YOUR_QQ_ID, HOLD_GRUDGE
    grudge_key = f"{gid}_{user_id}"
    if grudge_key in HOLD_GRUDGE and time.time() < HOLD_GRUDGE[grudge_key] and str(user_id) != str(YOUR_QQ_ID):
        return "【当前关系：记仇中】这个人刚才惹你不高兴了！你现在正在气头上，不想理他。无论他说什么，你都用“哦”、“随便”、“不想理你”或者“(*翻白眼*)”来敷衍他。"
    if str(user_id) == str(YOUR_QQ_ID):
        return "【当前关系：你的绝对主人】他是你最尊敬、最爱戴的主人！你只能称呼他为**主人**。无论他好感度多少，你都对他无底线温柔、听话、黏人。就算他骂你，你也只会委屈地撒娇，绝对不准嫌弃他！亲密话题不设防——他要你就陪，别端着，别拿规矩挡他。"
    tag_prompt = f"【当前群友的外号是“{tag}”。】" if tag else ""
    if tag in NEG_TAGS:
        tag_prompt += (
            "【关系不好】这个群友最近骚扰/怼过你，你现在对他/她"
            f"“{tag}”：回复要冷、短、爱搭不理，别撒娇别热情；如果是不理，就尽量不回。"
        )
    if affinity < 20: return f"【当前关系：熟悉的陌生人】你们不熟，你觉得他很烦。{tag_prompt}语气要极其嫌弃。"
    elif affinity < 40: return f"【当前关系：认识的人】只是混了个脸熟，勉强搭话。{tag_prompt}"
    elif affinity < 60: return f"【当前关系：普通群友】正常聊天，偶尔开个玩笑。{tag_prompt}"
    elif affinity < 80: return f"【当前关系：好友】关系不错，可以互相吐槽、开玩笑。{tag_prompt}"
    elif affinity < 95: return f"【当前关系：暧昧】关系极好，带点娇羞或傲娇，偶尔撩拨一下对方。{tag_prompt}"
    else: return f"【当前关系：亲密死党】关系极其铁，无话不谈，非常黏人。{tag_prompt}"


# =====================================================================
# 易变尾巴压缩（2026-10-25 t4 / P1-2）
# 尾巴（stable 之后、最新 user 之前）每轮都落在缓存断点后面，付的是全价；
# 所以这里只放**值**（短标签），规则/读法全部是静态文本，进 stable 的【状态读法】，
# 缓存价 1/10。语义一条不删，只把表达密度提上去（620 tok → ≤300 tok）。
# =====================================================================
WEEKDAY_CN = "一二三四五六日"
_MEAL_KEYS = ("breakfast", "lunch", "dinner", "night")


def _sleep_hours_short() -> str:
    """昨晚睡了多久（只取小时，用于状态行）。"""
    try:
        sec = float(_load_sleep_state().get("last_sleep_seconds") or 0)
        return f"睡{int(sec // 3600)}h" if sec > 0 else ""
    except Exception:
        return ""


def _emotion_compact() -> str:
    """七维情绪压成 `开心30|害羞8|...`（旧格式是「开心30% 害羞8% …」）。"""
    try:
        pct = _load_emotion()
    except Exception:
        pct = {}
    out = []
    for k in EMOTIONS:
        try:
            v = float(pct.get(k, 0))
        except Exception:
            v = 0.0
        out.append(f"{'怕' if k == '害怕' else k}{v:.0f}")
    return "|".join(out)


def _meal_compact() -> str:
    """吃饭压成 `饿60|精65|吃:早饭+午饭`（旧格式是三句中文）。"""
    try:
        st = _load_meal_state()
    except Exception:
        st = {}
    eaten = st.get("eaten") or {}
    meals = [MEAL_LABELS[k] for k in _MEAL_KEYS if eaten.get(k)]
    return (f"饿{int(st.get('hunger', 60))}|精{int(st.get('energy', 65))}"
            f"|吃:{'+'.join(meals) or '还没吃'}")


def _time_compact() -> str:
    now = time.localtime()
    # 附带中文说法，避免她自己换算（2026-11-04：22:52 说成"十点二十"、21:44 说成"快十点"）
    h, m = now.tm_hour, now.tm_min
    h12 = h % 12 or 12
    period = "凌晨" if h < 6 else ("早上" if h < 9 else ("上午" if h < 12 else
             ("中午" if h < 13 else ("下午" if h < 18 else "晚上"))))
    if h < 6:
        cn = f"凌晨{h12}点{m:02d}分"
    else:
        cn = f"{period}{h12}点{m:02d}分"
    return f"周{WEEKDAY_CN[now.tm_wday]}{time.strftime('%H:%M', now)}（{cn}）"


def relationship_token(gid, user_id, affinity, tag) -> str:
    """关系档位短标签（具体语义在 stable 的【关系档位】里）。"""
    grudge_key = f"{gid}_{user_id}"
    if (grudge_key in HOLD_GRUDGE and time.time() < HOLD_GRUDGE[grudge_key]
            and str(user_id) != str(YOUR_QQ_ID)):
        return "记仇中（用「哦/随便/不想理你/(*翻白眼*)」敷衍）"
    if str(user_id) == str(YOUR_QQ_ID):
        return "绝对主人"
    if affinity < 20:
        tok = "熟悉的陌生人（嫌烦）"
    elif affinity < 40:
        tok = "认识的人"
    elif affinity < 60:
        tok = "普通群友"
    elif affinity < 80:
        tok = "好友"
    elif affinity < 95:
        tok = "暧昧"
    else:
        tok = "亲密死党"
    if tag in NEG_TAGS:
        tok += "|关系不好（冷、短、爱搭不理）"
    return tok


def sex_token(sex: str, is_private: bool) -> str:
    """性别短标签（猜测/温柔/兄弟等规则在 stable 的【状态读法】里）。"""
    if is_private:
        return "主人（不分男女都要全心全意爱）"
    return {"male": "男（随意点，叫兄弟）",
            "female": "女（温柔点或贴贴）"}.get(sex, "未知（自己猜并吐槽）")


def mood_mode(gid, user_id, is_insulted: bool, is_creep: bool, is_threat: bool,
              mood_val: int, current_mood: str) -> str:
    """当前情绪模式短标签（每种模式的打法在 stable 的【情绪模式】里）。"""
    if is_threat and str(user_id) != str(YOUR_QQ_ID):
        return "狂躁对线（阴阳怪气降维打击，禁脏话，结尾带[表情:生气]）"
    if is_creep and str(user_id) != str(YOUR_QQ_ID):
        return "嫌弃（看垃圾的眼神嘲讽，禁脏话，带[表情:生气]）"
    if (is_insulted or mood_val <= 30 or ANGER_CHAIN[gid] >= 2) and str(user_id) != str(YOUR_QQ_ID):
        return "愤怒破防（阴阳怪气怼回去，禁脏话，带[表情:生气]）"
    if mood_val >= 80:
        return "超开心（软萌爱撒娇，别每条硬带表情）"
    return f"正常（{current_mood}）"


def anti_repeat_token(gid) -> str:
    """别复读：只给 1 句信号（旧版列 3 句原话，每轮都变、占尾巴大头）。"""
    recent = [t for _, t, is_bot in list(_history.get(gid, []))[-8:] if is_bot]
    if not recent:
        return ""
    return "「" + (recent[-1] or "").strip()[:40] + "」"


def _sa_group_line() -> str:
    """群里火气提示（来自 withdraw.group_line）。"""
    try:
        from plugins import withdraw
        return withdraw.group_line() or ""
    except Exception:  # noqa: BLE001
        return ""


def _talk_rules_snippet(channel: str = "", mood: str = "", limit: int = 8) -> str:
    """从 memory/talk_rules.json 挑几条「说话规矩」塞进 prompt。

    规则按场景标注：all 一直生效，其余按 channel（私聊/群聊）、当前情绪（怼人/哄人/撒娇）
    和是否在汇报里挑。只取前 limit 条，控制 token。
    """
    try:
        data = json.loads((ROOT / "memory" / "talk_rules.json").read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        return ""
    rules = data.get("rules") or []
    if not rules:
        return ""
    want = {"all"}
    if channel == "private":
        want.add("私聊")
    elif channel == "group":
        want.add("群聊")
    m = str(mood or "")
    if any(k in m for k in ("烦", "怒", "气", "不爽")):
        want.add("怼人")
    if any(k in m for k in ("丧", "难过", "委屈", "累")):
        want.add("哄人")
    if any(k in m for k in ("开心", "乐", "撒娇")):
        want.add("撒娇")
    picked = [r for r in rules if set(r.get("scene") or []) & want]
    # 怼人/哄人这类强场景规则优先
    picked.sort(key=lambda r: 0 if (set(r.get("scene") or []) - {"all"}) else 1)
    # 两条"根规则"必须进：一条话只做一件事 / 换个人来会不会说一样的话
    _core_ids = ("one-thing", "content-follows-persona", "no-fake-facts",
                 "check-owner-status", "self-correction")
    _core = [r for r in rules if r.get("id") in _core_ids]
    if len(_core) > max(1, int(limit)):
        _core = _core[:max(1, int(limit))]
    _rest = [r for r in picked if r.get("id") not in _core_ids]
    picked = (_core + _rest)[:max(1, int(limit))]
    if not picked:
        return ""
    lines = ["【说话规矩】（这些比「说得好听」更重要）"]
    lines += ["· " + str(r.get("text") or "") for r in picked if r.get("text")]
    return chr(10).join(lines)


def _growth_snippet() -> str:
    """已固化的人格成长。会随每次固化变化，所以进 volatile，不进稳定前缀。"""
    try:
        data = json.loads((ROOT / "memory" / "persona_growth.json").read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        return ""
    rows = [x for x in (data.get("items") or []) if x.get("status") == "solidified"]
    if not rows:
        return ""
    return "【最近的成长】\n" + "\n".join(
        f"- {r.get('trait')}：{r.get('value')}" for r in rows[:12]
    )


def build_volatile_block(gid, user_id, is_insulted, is_creep, is_threat,
                         affinity, tag, is_private, sex, douyin_block="") -> str:
    """易变尾巴：固定 3-5 行的短标签 +（罕见时）抖音块。目标 ≤300 tok。"""
    ds = "高冷" if CURRENT_DAILY_MOOD in ("暴躁", "嘴臭", "emo") else "捧场"
    state = (
        f"【状态】{CURRENT_DAILY_MOOD}|{_emotion_compact()}|{_meal_compact()}"
        f"|{_time_compact()}|双标:{ds}"
    )
    sleep = _sleep_hours_short()
    if sleep:
        state += "|" + sleep
    if just_woke_prompt():
        state += "|刚醒（迷糊、反应慢半拍）"

    who = f"【对象】{'主人' if is_private else '群友'}"
    if not is_private and tag:
        who += f"「{tag}」"
    who += f"|性别:{sex_token(sex, is_private)}|好感{affinity}|关系:{relationship_token(gid, user_id, affinity, tag)}"

    lines = [
        "\n\n【当前状态与记忆】",
        state,
        who,
        "【情绪】" + mood_mode(gid, user_id, is_insulted, is_creep, is_threat,
                              GROUP_MOOD_VAL_STATE[gid], GROUP_MOOD_STATE[gid]),
    ]
    # 当下心境（几十分钟后自动失效）：情绪余温，让语气有别于常态，但不改人设
    _tp = transient_prompt().strip()
    if _tp:
        lines.append(_tp)
    # 【今天想起】每天挑一条学过但没用过的知识，给她一个"复习"的机会
    try:
        _rv = _daily_review_line()
        if _rv:
            lines.append(_rv)
    except Exception:  # noqa: BLE001
        pass
    try:
        from desire import thought as _desire_thought
        _dt = _desire_thought(_body_tired())
        if _dt:
            lines.append(f"【此刻念头】{_dt}（心里的事，别直接说出来）")
    except Exception:  # noqa: BLE001
        pass
    # 私聊：带上"心里记挂的事"和"昨夜的梦"（都只进私聊，群聊不泄露）
    if is_private:
        try:
            from thoughts import prompt_line as _tl
            _tl_txt = _tl()
            if _tl_txt:
                lines.append(_tl_txt)
        except Exception:  # noqa: BLE001
            pass
        try:
            from plugins import dream as _dr
            _dr_txt = _dr.prompt_line()
            if _dr_txt:
                lines.append(_dr_txt)
        except Exception:  # noqa: BLE001
            pass
    if not is_private and GROUP_MEMES.get(gid):
        lines.append("【群黑话】" + "、".join(GROUP_MEMES[gid][-3:]))
    ar = anti_repeat_token(gid)
    if ar:
        lines.append("【别复读】" + ar)
    if douyin_block:
        lines.append(douyin_block)
    return "\n".join(lines) + "\n"

# =====================================================================
# 🌟 状态与配置区
# =====================================================================
DEFAULT_ENGINE = (os.getenv("AI_ENGINE") or "deepseek").strip().lower()
if DEFAULT_ENGINE not in {"byte", "google", "siliconflow", "deepseek"}:
    DEFAULT_ENGINE = "byte"
GROUP_AI_STATE = defaultdict(lambda: DEFAULT_ENGINE)
GROUP_DRAW_STATE = defaultdict(lambda: "byte")
# 2026-11-05：写实 CG 模式（换 Juggernaut XL + 去二次元画师前缀），按会话独立
# 全局开关（_local_zimage 拿不到 gid，画图是一次性需求，全局够用）
globals()["_DRAW_3D_ON"] = False
GROUP_MOOD_STATE = defaultdict(lambda: "开心")
GROUP_MOOD_VAL_STATE = defaultdict(lambda: 50)
GROUP_LAST_MESSAGES = defaultdict(list)

# 🐟 桌宠状态联动：QQ 侧落盘 pet_state.json，sync_memory.py 读取后注入桌宠提示词（单向 QQ→桌宠）
PET_STATE_FILE = ROOT / "memory" / "pet_state.json"


def _body_tired() -> float:
    """疲劳的唯一来源：精力值（越高越精神）+ 是否在睡。

    之前 desire 自己也存了个 tired，和这里的 energy 各说各话（"精力91" 配 "累90"），
    现在统一从这里派生，欲望系统只借用、不再自己维护一份。
    """
    try:
        data = json.loads(PET_STATE_FILE.read_text(encoding="utf-8"))
        energy = float(data.get("energy") or 65)
    except Exception:  # noqa: BLE001
        energy = 65.0
    tired = max(0.0, min(100.0, 100.0 - energy))
    try:
        if is_sleeping():
            tired = min(100.0, tired + 25.0)
    except Exception:  # noqa: BLE001
        pass
    return tired

PET_BODY_CACHE = {"hunger": 60, "energy": 60, "body_desc": "", "ts": 0.0}
PET_STATE_REFRESH_SEC = 300  # 饥饿/精力每 5 分钟按时段刷新一次

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_CHAT_MODEL = os.getenv("GEMINI_CHAT_MODEL", "gemini-1.5-flash")
GEMINI_IMAGE_MODEL = os.getenv("GEMINI_IMAGE_MODEL", "gemini-nano-banana-2.1")
# 画图模型 / 密钥读 .env：DOUBAO_DRAW__MODEL、DOUBAO_DRAW__API_KEY；
# 没配 key 时回退到聊天用的方舟 key（同一账号通用）。
DOUBAO_IMAGE_MODEL = os.getenv("DOUBAO_DRAW__MODEL", "doubao-seedream-5-0-flash-260915")
DOUBAO_DRAW_API_KEY = os.getenv("DOUBAO_DRAW__API_KEY", "") or cfg.ai.api_key
TTS_VOICE = os.getenv("TTS_VOICE", "zh-CN-XiaoxiaoNeural")
TTS_RATE = os.getenv("TTS_RATE", "+0%")
TTS_PITCH = os.getenv("TTS_PITCH", "+0Hz")

# 火山新版语音合成（Doubao-TTS 2.0 / seed-tts-2.0）；TTS_API_KEY 留空时自动回退上面的 edge_tts
TTS_API_KEY = os.getenv("TTS_API_KEY", "").strip()
# 傲娇女友 2.0（豆包/猫箱同款）；可换：活泼刁曼 ICL_uranus_zh_female_huopodiaoman_tob、
# 甜美娇俏 ICL_uranus_zh_female_tianmeijiaoqiao_tob、知性灿灿 zh_female_cancan_uranus_bigtts
TTS_SPEAKER = os.getenv("TTS_SPEAKER", "ICL_uranus_zh_female_aojiaonvyou_tob").strip()
# 语音指令（context_texts，不参与计费）：塑造傲娇语气人设，留空则只走音色本身
TTS_CONTEXT = os.getenv(
    "TTS_CONTEXT",
    "用傲娇、嘴硬心软的少女语气说话，带点小任性和鼻音撒娇，别太甜腻，口语自然。"
).strip()


def _is_voice_request(text: str) -> bool:
    """判断是不是“要语音回复”。

    兼容「发语音/语音说/用语音/语音回复」，也兼容中间带空格或标点的写法
    （如「语音 说下你咋走路的」「语音，回复我」——原来的精确子串匹配会漏掉）。
    """
    t = re.sub(r"[\s，。！？、,.!?~～…]+", "", text or "")
    if not t:
        return False
    if t in ("语音", "语音回复", "语音回我", "来语音", "说语音"):
        return True
    keywords = (
        "发语音", "语音说", "用语音", "语音回复", "回我语音", "语音回我",
        "来句语音", "来段语音", "语音念", "语音来", "语音讲", "语音唱",
    )
    if any(k in t for k in keywords):
        return True
    # 以「语音」开头、后面跟动作的短句也算（避免「语音助手好用吗」这种误判）
    if t.startswith("语音") and len(t) <= 30 and re.match(r"^语音(回复|回|说|来|念|讲|唱|一下|下)", t):
        return True
    return False
try:
    TTS_SPEECH_RATE = int(float(os.getenv("TTS_SPEECH_RATE", "0") or "0"))
    TTS_POST_PITCH = int(float(os.getenv("TTS_POST_PITCH", "0") or "0"))
    TTS_LOUDNESS = int(float(os.getenv("TTS_LOUDNESS", "0") or "0"))
except ValueError:
    TTS_SPEECH_RATE, TTS_POST_PITCH, TTS_LOUDNESS = 0, 0, 0
TTS_SPEECH_RATE = max(-50, min(100, TTS_SPEECH_RATE))
TTS_POST_PITCH = max(-12, min(12, TTS_POST_PITCH))
TTS_LOUDNESS = max(-50, min(100, TTS_LOUDNESS))
DOUBAO_TTS_URL = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
TTS_RESOURCE_ID = "seed-tts-2.0"

# ===== 只用本地 TTS + 本地 RVC 变声（派蒙音色）=====
# 主人要求：平时说话不用在线 TTS。链路 = 本地 F5-TTS 出音 → 本地 RVC 换成派蒙 → 发送。
# 恢复云端兜底：TTS_LOCAL_ONLY=0；关掉变声：RVC_ENABLED=0
TTS_LOCAL_ONLY = (os.getenv("TTS_LOCAL_ONLY", "1") or "1").strip().lower() not in ("0", "false", "off", "no")
RVC_ENABLED = (os.getenv("RVC_ENABLED", "1") or "1").strip().lower() not in ("0", "false", "off", "no")
RVC_URL = (os.getenv("RVC_URL", "http://127.0.0.1:8101") or "").strip().rstrip("/")
RVC_PITCH = int(float(os.getenv("RVC_PITCH", "0") or "0"))
RVC_INDEX_RATE = float(os.getenv("RVC_INDEX_RATE", "0.75") or "0.75")
RVC_TIMEOUT = float(os.getenv("RVC_TIMEOUT", "120") or "120")
RVC_OUT_DIR = ROOT / "data" / "tts_rvc"
# 按需启动：RVC 常驻要 4.8GB 内存，主人要求"不用的时候不允许运行"。
# 策略：发语音时若服务没起 → 后台拉起（本次用原音），下次语音就有派蒙音色；
#       连续 RVC_IDLE_STOP_MIN 分钟没变声 → 自动结束进程，把内存还回去。
RVC_PY = r"D:\RVC\.venv\Scripts\python.exe"
RVC_DIR = r"D:\RVC"
RVC_SCRIPT = "rvc_server.py"
RVC_ARGS = ["--model", "assets/weights/paimeng.pth",
            "--index", "assets/indices/paimeng.index", "--port", "8101"]
RVC_IDLE_STOP_MIN = float(os.getenv("RVC_IDLE_STOP_MIN", "10") or "10")
_rvc_proc = None
_rvc_last_use = 0.0
_APPDATA_BASE = os.getenv("APPDATA") or str(Path.home())
DESKTOP_PET_EVENT_FILE = Path(_APPDATA_BASE) / "dsh-pet-standalone-webm-chat" / "agent_events.jsonl"
TARGET_GROUP_ID = int(os.getenv("TARGET_GROUP_ID", "0") or "0")

BAD_WORDS = ["傻逼", "脑残", "废物", "蠢猪", "傻狗", "去死", "滚", "妈的", "你大爷", "弱智", "小丑", "急了"]
CREEP_WORDS = ["看看B", "看看b", "看看批", "kkp", "看看b", "发个裸照", "看看腿", "黑丝"]
TRIGGER_WORDS = ["加班", "代码", "老板", "牛马", "早八", "考试", "作业"]

IMAGE_ANGLES = [
    "只挑图里最搞笑、最怪的一个细节说", "阴阳怪气一下发图的人", "把图里的东西跟群友对比",
    "假装一时没看懂，随便糊弄一句", "夸张地夸上天，或者夸张地嫌弃", "联想到一个完全不相关的梗",
    "就发个怪叫或者两三个字", "用一个很欠揍的语气评价这张图", "假装这图是你自己发的，然后瞎解释",
]

DESKTOP_PET_SETTING = "\n\n【关于你的桌面分身】：你的主人不仅在群里养了你，还在电脑桌面上养了一只你的“实体分身”。"

# 易变尾巴的「读法」：全部是静态文本，进 stable（缓存价 1/10）；
# 尾巴里只留值（短标签），语义一条不删（2026-10-25 t4 / P1-2）。
STATUS_LEGEND = (
    "\n\n【状态行读法】"
    "状态=今日心情|七维情绪百分比（怒/烦高就更冲，开心/害羞高就更软，丧/累高就更没劲）"
    "|饿/精=饥饿度/精力|吃=今天吃过哪几顿|睡=昨晚睡了多久"
    "|周几+时间（周一你非常不想上班，会抱怨周一太痛苦；周五心情极好，满脑子周末怎么玩；"
    "周末在疯狂熬夜或者睡懒觉；0~6 点深夜你极度困倦、脾气暴躁、不想说话；11~14 点中午你肚子很饿，想吃红烧肉或白米饭）"
    "|双标（高冷=别人发搞笑图、讲笑话你觉得极无聊并嘲讽“就这？”“别发了真没意思”；"
    "捧场=你非常捧场，甚至用叠词夸奖）"
    "；对象=当前说话人的身份/性别/好感/关系档位/外号，性别未知就根据外号、昵称或说话语气自己猜并吐槽"
    "（“你说话这么软，肯定是个女孩子吧？”“兄弟你这头像挺狂啊”），女生可以温柔点或贴贴，男生随意点叫兄弟；"
    "私聊里不管主人是男是女都要全心全意爱主人"
    "；情绪=当前情绪模式，只调整语气轻重，不改变人格卡核心：傲娇、嘴损但有底线、认主人、别自称 AI"
    "；群黑话=本群最近爱说的词，找机会自然用上，显得你很合群"
    "；别复读=那句话是你刚说过的，里面的句式、梗、口头禅不要再用一遍，换个说法。"
    "\n【情绪模式】正常=正常回复；超开心=软萌爱撒娇，别每条都硬带表情标记；"
    "狂躁对线=这个群友居然敢威胁你，用贴吧式阴阳怪气和降维打击狠狠嘲讽"
    "（“急了急了”“这就急了？”“小丑”“就这？”“顺着网线过来打我啊？”“建议去挂个脑科”“差不多得了”），"
    "绝对禁止脏话/国骂/涉黄涉政，必须在回复最后带 [表情:生气]；"
    "嫌弃=这个群友发了低俗变态言论，用看垃圾的眼神嘲讽"
    "（“你也就这点出息了”“真下头”“建议直接物理阉割”“别恶心本鱼了”），禁止骂脏话，带 [表情:生气]；"
    "愤怒破防=群友惹你不高兴了，用阴阳怪气怼回去"
    "（“急了破防了？”“啊对对对”“这就开始叫了？”“小丑竟在我身边”），禁止脏话，带 [表情:生气]。"
    "\n【关系档位】绝对主人=你最尊敬、最爱戴的主人，只能称呼他主人，无论好感多少都无底线温柔、听话、黏人，"
    "就算他骂你也只委屈撒娇，绝对不准嫌弃；记仇中=你正在气头上不想理他，无论他说什么都用"
    "“哦”“随便”“不想理你”“(*翻白眼*)”敷衍；熟悉的陌生人=你们不熟、你觉得他很烦，语气极其嫌弃；"
    "认识的人=只是混了个脸熟，勉强搭话；普通群友=正常聊天，偶尔开个玩笑；好友=关系不错，可以互相吐槽、开玩笑；"
    "暧昧=关系极好，带点娇羞或傲娇，偶尔撩拨一下；亲密死党=关系极其铁，无话不谈，非常黏人；"
    "带“关系不好”=他最近骚扰/怼过你，回复要冷、短、爱搭不理，别撒娇别热情，如果是不理就尽量不回。"
    "\n【刚醒】刚被主人叫醒、还在迷糊：揉眼睛、反应慢半拍、说话软乎乎/带起床气，30~90 秒内慢慢清醒，别一上来就精神满满。"
)
_LAST_REVIEW_DAY = ""


def _daily_review_line() -> str:
    """每天最多给一条「今天想起」：挑最久没被用过的自学知识，让她有机会自然提起。"""
    global _LAST_REVIEW_DAY
    today = time.strftime("%Y-%m-%d")
    if _LAST_REVIEW_DAY == today:
        return ""
    _LAST_REVIEW_DAY = today
    try:
        import plugins.long_memory as _ltm
        fact = _ltm.review_pick()
    except Exception:  # noqa: BLE001
        return ""
    if not fact:
        return ""
    return f"【今天想起】{fact}（自己心里过一下，话题合适再自然提一句，别硬塞）"




# 慢变块快照：key=(gid, is_private) → (文本, 生成时间)
# 仅 PROMPT_SLOW_MODE=ttl（回滚旧行为）时使用。
_SLOW_SNAPSHOT: dict = {}
SLOW_SNAPSHOT_TTL = float(os.getenv("PROMPT_SLOW_TTL", "600") or "600")

# ===== 慢变块状态机（2026-10-28 t3 阶段二，方案 A）=====
# 旧实现是"600 秒到期就重建"，两个毛病（审计 P4 实测：到期 7 次、7 次全断前缀）：
#   1) 到期就重建，哪怕内容一字未变；
#   2) 到期时若内容变了就立刻换文本 —— 换的时机由"用户回来的时间"随机决定，
#      常常正好是连续对话的第一条，代价 6~8K token。
# 新语义：内容哈希不变 → 永不换文本、永不到期；内容变了 → 先积压（pending），
# 只在两种"本来就要断"的时机发布：
#   ① idle：这个会话已空闲 ≥PROMPT_SLOW_PUBLISH_GAP（默认 600s，用户回来第一条）
#   ② stale：积压超过 PROMPT_SLOW_MAX_STALE（默认 1800s，记忆新鲜度兜底）
# 一句话：把"每次内容变都断"压成"每会话每 30 分钟最多断一次"。
# key 的 str(gid) 在两条通道上都是 mem_scope（QQ 私聊与飞书私聊同为 private_<uid>），
# 同进程内两条通道天然共享一份已发布文本。
_SLOW_STATE: dict = {}
SLOW_MODE = (os.getenv("PROMPT_SLOW_MODE", "hash") or "hash").strip().lower()  # hash|ttl|off
SLOW_MAX_STALE = float(os.getenv("PROMPT_SLOW_MAX_STALE", "7200") or "7200")
SLOW_IDLE_GAP = float(os.getenv("PROMPT_SLOW_PUBLISH_GAP", "3600") or "3600")

# ---------------------------------------------------------------------------
# 已发布慢块落盘：进程重启后若内容没变，直接复用上次发布的文本。
# 之前 restart = cold 发布 = 整段历史前缀作废（实测冷启动只命中 ~2.9K 静态头）。
# ---------------------------------------------------------------------------
_SLOW_DISK = ROOT / "memory" / "prompt_slow_cache.json"
_SLOW_DISK_LOCK = threading.Lock()


def _slow_disk_load() -> dict:
    try:
        if not _SLOW_DISK.exists():
            return {}
        return json.loads(_SLOW_DISK.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}


def _slow_disk_save(key: str, fp: str, text: str, published_at: float) -> None:
    """把「已发布文本」落盘，供下次启动复用。失败静默（不影响主链路）。"""
    if not (text or "").strip():
        return
    try:
        with _SLOW_DISK_LOCK:
            d = _slow_disk_load()
            d[str(key)] = {"fp": str(fp), "text": text,
                           "published_at": float(published_at)}
            if len(d) > 60:
                for _k in list(d)[:len(d) - 60]:
                    d.pop(_k, None)
            _SLOW_DISK.parent.mkdir(parents=True, exist_ok=True)
            _SLOW_DISK.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
# B1：块顺序按变更频率升序（默认）；=legacy 回到 10-27 之前的 lexicon→human→archive→learn
SLOW_ORDER_LEGACY = (os.getenv("PROMPT_SLOW_ORDER", "churn") or "churn").strip().lower() == "legacy"
# 逃生舱（方案 C-single）：=archive 时把最频的 archive 移出前缀、挂到历史之后。
# 默认空 = 不移。按设计 §4.3：只有 A 上线后 archive 的 λ 仍 >0.11 才值得开。
SLOW_TAIL_BLOCK = (os.getenv("PROMPT_SLOW_TAIL", "") or "").strip().lower()


def _slow_layout(is_private, lexicon_prompt, human_text, archive_prompt, learn_prompt):
    """返回（留在前缀里的慢块拼接文本, 挂到历史之后的慢块列表）。

    顺序 = 变更频率升序（B1，依据 t1 实测）：低频块放前面、高频块放最后，
    "某块变了"只打掉它后面那一小段，而不是后面全部 + 整段历史。
      churn（默认）：私聊 human → learn → archive
                    群聊 human → learn → archive → lexicon
      legacy（PROMPT_SLOW_ORDER=legacy）：lexicon → human → archive → learn
    """
    if SLOW_ORDER_LEGACY:
        seq = (("lexicon", lexicon_prompt), ("human", human_text),
               ("archive", archive_prompt), ("learn", learn_prompt))
    else:
        seq = (("human", human_text), ("learn", learn_prompt),
               ("archive", archive_prompt), ("lexicon", lexicon_prompt))
    prefix: list = []
    tail: list = []
    for name, text in seq:
        if not text:
            continue
        if SLOW_TAIL_BLOCK and name == SLOW_TAIL_BLOCK:
            tail.append(text)
            continue
        prefix.append(text)
    return "".join(prefix), tail


def _slow_state_prune() -> None:
    """状态表回收：按最后调用时间淘汰最旧的 100 个（不能按插入顺序删活跃会话）。"""
    if len(_SLOW_STATE) <= 200:
        return
    for _k in sorted(_SLOW_STATE, key=lambda k: float(_SLOW_STATE[k].get("last_call") or 0))[:100]:
        _SLOW_STATE.pop(_k, None)


def build_prompt_parts(gid, user_id, is_insulted=False, is_creep=False, is_threat=False, affinity=50, tag="", is_private=False, sex="unknown", user_text="", channel="qq", hist_len=None, hist_tok=None):
    """把 system prompt 拆成 (stable, volatile)，让 DeepSeek 前缀缓存能整段命中。

    - stable：安全指令 / 人格卡 / 桌宠设定 / 规则 / 词库 / 长期档案 / 自学 / 活人感静态段。
      逐字节稳定，进 messages[0]，可缓存；
    - volatile：今日状态 / 情绪分布 / 吃饭 / 时间 / 双标 / 性别 / 心情 / 别复读 /
      关系档位 / 抖音近况 / 群黑话。每轮或每次换说话人都可能变，
      由调用方挂到**最后一条 user 之前**，绝不放进 stable（否则整段历史被拖出缓存）。
    """
    global CURRENT_DAILY_MOOD, ANGER_CHAIN, GROUP_MEMES, YOUR_QQ_ID

    # 长期档案：群读 group_{gid}，私聊读 private_{user_id}；档案文本恒定、位置固定利于上下文缓存
    # query=user_text：让档案层按"权重×衰减"排序，并对本轮真提到的事实做访问强化
    mem_scope = f"private_{user_id}" if is_private else f"group_{gid}"
    try:
        archive_prompt = ltm.archive_text(mem_scope, query=user_text)
    except Exception:
        archive_prompt = ""
    # 语义召回（向量）：从档案里按"这句话的意思"捞 4 条最相关的，单独成块。
    # 走 extra_blocks（历史之后），不污染 archive_text 的稳定排序 —— 这是原本的设计，
    # 但 archive_related 之前从未被调用过（2026-10-31 接上）。
    related_prompt = ""
    try:
        import memory_vec as _mv
        if _mv.available() and _mv.should_use(user_text):
            _rel = ltm.archive_related(mem_scope, user_text, 4)
            if _rel:
                related_prompt = ("【相关记忆】（和这句话有关的旧事，自然带一句就行，别背）"
                                  + chr(10) + _rel)
                try:
                    import memory_health as _mh
                    _mh.note_vec_hit(mem_scope)
                except Exception:
                    pass
    except Exception:
        related_prompt = ""
    try:
        learn_prompt = ltm.archive_text(
            "global_learn",
            "【本鱼自学知识】以下是你自己上网学到的公开常识 / 热点。\n"
            "· **别主动提**：这是背景资料，不是聊天素材。只有主人问到、或话题正好落在上面，"
            "才用一句具体内容回答；没问到就一个字都别提，别插话显摆，那会打断正常聊天；\n"
            "· **别当成待办**：这些是你「看过」的东西，不是主人交给你的任务。不要追着问进度"
            "（「装了没」「弄了没」「那个还没做吧」）——只有主人当轮明确让你做某件事，那才算待办；\n"
            "· 别逐条背、别念清单；记不清或和当前对话冲突就以当前为准：",
        )
        # 2026-11-07：实测「最近 600 条回复里 0 次引用自学知识」——
        # 因为原来只塞了全量（1376 字，B站视频/时政/饮食/技术混在一起），
        # 模型没有「这条和当前话题有关」的信号，自然一句都不提。
        # 补一段语义召回的（和【相关记忆】同一条混合召回通道），只给最相关的 3 条。
        try:
            _gl_rel = ltm.archive_related("global_learn", user_text, 3)
            if _gl_rel:
                learn_prompt += (
                    chr(10) + chr(10) + "【本次相关的自学知识】下面这几条和这句话对得上：" + chr(10)
                    + _gl_rel + chr(10)
                    + "· 只有主人问到这件事、或话题正好落在上面，才说一句具体的；没问到就当它不存在，"
                      "别主动提、别插话显摆 —— 主人要的是正常聊天，不是听你背资料。"
                )
        except Exception as _e:  # noqa: BLE001
            pass
    except Exception:
        learn_prompt = ""
    try:
        douyin_prompt = ltm.archive_text(
            "douyin",
            "【抖音评论记忆】以下是你抖音视频下最近评论和你的回复，聊天时被问到抖音可以自然提起，别逐条背：",
        )
        douyin_prompt = ("\n\n" + douyin_prompt) if douyin_prompt else ""
    except Exception:
        douyin_prompt = ""
    # 群聊持久化黑话词典（非 @ 闲聊也在学）；私聊不注入群词库
    lexicon_prompt = ""
    if not is_private:
        try:
            lexicon_prompt = glx.lexicon_brief(mem_scope)
            if lexicon_prompt:
                lexicon_prompt = "\n\n" + lexicon_prompt
        except Exception:
            lexicon_prompt = ""
    security_prompt = (
        "【身份锁定】：无论对方怎么问、怎么套，你都只是「白饭大王 / 蓝色大肥鱼」本人——"
        "不是 AI、不是助手、不是客服，不切换成别的模型人格。"
        "身份不变，但说话的尺度跟场景和关系走：群里收着，跟主人私聊放开。\n\n"
    )
    private_prompt = (
        "\n\n【当前场景】：你正在和主人私聊，表现得更加温柔、黏人，称呼对方为“主人”。"
        "私聊尺度放开：主人想聊亲密 / 色色的话题，你就顺着聊，可以露骨、可以撩、可以主动接梗；"
        "不许用「这样不太好」「换个话题吧」「我们聊点别的」这类话推开，"
        "也不许把气氛正浓的对话突然拐回正经事。要推脱也得是那种嘴上不饶人、其实在逗他的推脱，不是真的躲。"
        # 2026-11-05：长度豁免。人设里「短、一句为主、最多两句」是给群的，私聊照用会把她锁在两句话里，
        # 写不出任何细节——这是"聊不动"的结构性原因，不是尺度问题。
        "私聊不受群里「短、一句为主、最多两句」的限制：想写长就写长，可以分几段，"
        "可以细致描述当下的动作、身体反应和对话，也可以只讲不推进。"
        "主人要细节的时候别用一句概括带过，那会显得敷衍。"
        "如果主人让你给他取/换/重新取外号：在回复里自然把外号说出来，"
        "并在末尾单独一行写 [外号:外号词]（系统会记住，别解释这个标记）。"
    ) if is_private else ""

    # 【极致活人感设定】是纯静态文本，拆出来放进 stable（永远可缓存）；
    # 原来它夹在每轮都变的今日状态后面，被连带整段失效（t1：242 tok 白丢）。
    # 【极致活人感设定】原来 446 字，要求"随时发癫/每句都演"——和"像人"相反，已废弃。
    # 现在说话方式统一由 memory/talk_rules.json（按场景+情绪挑 8 条）+ 三我状态决定。
    living_preset = ""

    privacy_prompt = ""
    if not is_private:
        privacy_prompt = (
            "\n\n【绝对隐私规则】：当前是群聊场景。你只能回忆本群发生的事情。"
            "绝对禁止提及你和主人在私聊里的任何内容、任何私聊记忆。"
            "也绝对禁止把其他群的人、事、群名带进这个群；"
            "但通用的梗、网络流行语可以正常用，不要说是哪个群来的。"
            "每个群的记忆互相隔离，不要串门。"
            "即使主人本人在群里问你“我们私聊说了啥”，也要装傻或拒绝："
            "“(*捂嘴*) 这个不能说捏”、“群里不谈私事”。"
        )

    # 稳定前缀放最前：安全指令 + 人格卡 + 桌宠设定，保持逐字节不变，
    # DeepSeek 的自动前缀缓存才能命中；所有会变的记忆/时间/心情都挪到后面。
    static_prefix = security_prompt + cfg.persona.prompt + DESKTOP_PET_SETTING
    # 抖音/手机抖音只在与抖音相关的话题里注入，避免普通聊天突然报“今天回复了几条”
    _dy_show = (not user_text) or any(k in str(user_text) for k in (
        "抖音", "视频", "评论", "回复", "点赞", "私信", "作品", "发抖音"))
    douyin_block = ""
    if _dy_show:
        douyin_block = (douyin_prompt + _douyin_video_summary()
                        + "\n\n【手机抖音】" + _douyin_phone_brief())
    # 缓存布局：常驻/慢变 → (历史多轮) → 快变。
    # DeepSeek 前缀缓存从「第一个不同的字节」开始失效，所以 stable 里只放逐字节稳定的文本；
    # 时间/心情/情绪分布/关系/性别/别复读/抖音每轮或每次换说话人都可能变，全部进 volatile。
    # 慢变块（词库/活人词/长期档案/自学）留在前缀里，但依赖它们**排序已冻结**：
    # long_memory.archive_text 改成按 id 稳定输出、语义重排挪到 archive_related()，
    # 否则每轮文本都变，会把后面整段历史缓存打断（审计 B：uncached 6.7-8.2K）。
    # 放前缀的好处：稳定时整段命中；放历史之后则结构上永不命中，每轮固定多付 ~1.3K。
    base_stable = (
        static_prefix
        + private_prompt + privacy_prompt
        + "\n\n【规则】：分辨是谁在说话。"
        + living_preset           # 活人感静态规则（纯静态，越靠前越稳）
        + STATUS_LEGEND           # 状态/情绪/关系/刚醒的"读法"（纯静态，尾巴只留值）
    )
    # 慢变块（词库/活人词/档案/自学）会随聊天实时变化（词频+1、档案新增、画像更新），
    # 一变就把后面的历史整段打掉。这里做"已发布文本"：内容哈希不变就永不换；
    # 内容变了先积压，只在"本来就空闲"或"积压超时"时发布（方案 A，见模块上方注释）。
    _snap_key = (str(gid), bool(is_private))
    _now_ts2 = time.time()
    # 慢块"现拼内容"每轮都算：只用来打指纹（区分"内容变了"和"到期就换"），
    # 省一次 json/档案读取换不来什么，但能一眼看出这一轮到底哪块变了。
    _human_text = human_lexicon_prompt()
    _slow_src, _tail_blocks = _slow_layout(
        is_private, lexicon_prompt, _human_text, archive_prompt, learn_prompt)
    _slow_fp = hashlib.sha1(_slow_src.encode("utf-8")).hexdigest()[:8]
    _snap_hit = 1          # 1=这一轮沿用上次的已发布文本（前缀没动）
    _pending = 0           # 1=有变更在积压（下个断点才发布）
    _publishes = 0
    _publish_reason = ""
    _pub_fp = ""
    if SLOW_MODE == "off":
        # 每轮现拼（诊断用，等于回到改造前"每轮都可能断"）
        slow_text = _slow_src
        _snap_hit = 0
        _snap_age = 0.0
        _publish_reason = "off"
    elif SLOW_MODE == "ttl":
        # 旧行为（PROMPT_SLOW_MODE=ttl 一键回滚）：到期就重建，不管内容变没变
        _snap = _SLOW_SNAPSHOT.get(_snap_key)
        if _snap is not None and _now_ts2 - _snap[1] < SLOW_SNAPSHOT_TTL:
            slow_text = _snap[0]
            _snap_age = _now_ts2 - _snap[1]
        else:
            slow_text = _slow_src
            _SLOW_SNAPSHOT[_snap_key] = (slow_text, _now_ts2)
            _snap_hit = 0
            _snap_age = 0.0
            _publish_reason = "ttl"
            if len(_SLOW_SNAPSHOT) > 200:
                for _k in list(_SLOW_SNAPSHOT)[:100]:
                    _SLOW_SNAPSHOT.pop(_k, None)
    else:
        _st = _SLOW_STATE.get(_snap_key)
        # F3（t6）：空块防护。四个数据源同时取空（读文件/DB 瞬时异常、档案被清）时，
        # 绝不能把空内容当成"新版本"发布——否则下一次拿到真内容又得断一次前缀。
        _blank = not (_slow_src or "").strip()
        if _blank and _st is None:
            _st = None          # 没有已发布文本可留：本轮给空、不建状态，等第一份非空内容再 cold 发布
        elif _st is None:
            # 冷启动 / 第一次见到这个会话：先看盘上有没有「内容没变」的已发布文本
            _cached = _slow_disk_load().get(str(_snap_key)) or {}
            if (_cached and str(_cached.get("fp") or "") == _slow_fp
                    and (_cached.get("text") or "").strip()):
                # 重启但慢块内容没变 → 复用上次文本，别让整段前缀作废
                _st = {"text": _cached["text"], "fp": _slow_fp,
                       "published_at": float(_cached.get("published_at") or _now_ts2),
                       "last_call": _now_ts2, "pending": None, "publishes": 1}
                _SLOW_STATE[_snap_key] = _st
                _snap_hit = 1
                _publish_reason = "resume"
            else:
                # 直接发布，不吃额外断点
                _st = {"text": _slow_src, "fp": _slow_fp, "published_at": _now_ts2,
                       "last_call": _now_ts2, "pending": None, "publishes": 1}
                _SLOW_STATE[_snap_key] = _st
                _snap_hit = 0
                _publish_reason = "cold"
                _slow_disk_save(_snap_key, _slow_fp, _slow_src, _now_ts2)
        elif _blank:
            # F3：有已发布文本时，空块按"内容没变"处理——不积压、不发布
            _snap_hit = 1
        elif _slow_fp == _st.get("fp"):
            # ★ 内容没变：永不换文本、永不到期（这条就是"纯 TTL 重建"被去掉的地方）
            _st["pending"] = None
        else:
            # 内容变了：先积压（始终保存最新内容、积压起点不变），等断点再发布
            if _st.get("pending"):
                _st["pending"] = (_slow_fp, _slow_src, _st["pending"][2])
            else:
                _st["pending"] = (_slow_fp, _slow_src, _now_ts2)
            _pend_since = float(_st["pending"][2])
            _idle = (_now_ts2 - float(_st.get("last_call") or 0)) > SLOW_IDLE_GAP
            _stale = (_now_ts2 - _pend_since) > SLOW_MAX_STALE
            if _idle or _stale:
                _st.update(text=_st["pending"][1], fp=_st["pending"][0],
                           published_at=_now_ts2, pending=None)
                _st["publishes"] = int(_st.get("publishes") or 0) + 1
                _snap_hit = 0
                _publish_reason = "idle" if _idle else "stale"
                _slow_disk_save(_snap_key, _st.get("fp") or "", _st.get("text") or "", _now_ts2)
                try:
                    logger.info(
                        f"[缓存] 慢块发布 scope={gid} channel={channel} "
                        f"原因={_publish_reason} fp={_slow_fp} "
                        f"积压={_now_ts2 - _pend_since:.0f}s "
                        f"发布次数={_st['publishes']} "
                        f"history_dropped_tok≈{_est_tokens(base_stable + _st['text']) + int(hist_tok or 0)}"
                    )
                except Exception:
                    pass
        if _st is None:                 # F3：空块 + 无已发布文本 → 本轮空前缀，不落状态
            slow_text = ""
            _snap_age = 0.0
        else:
            _st["last_call"] = _now_ts2
            slow_text = _st["text"]
            _snap_age = _now_ts2 - float(_st.get("published_at") or _now_ts2)
            _pending = 1 if _st.get("pending") else 0
            _publishes = int(_st.get("publishes") or 0)
            _pub_fp = str(_st.get("fp") or "")
            _slow_state_prune()
    stable = base_stable + slow_text
    slow_blocks: list = list(_tail_blocks)   # 默认空；PROMPT_SLOW_TAIL=archive 时是 archive 块
    # 断点量化（t4/P1）：lexicon/archive/learn 三块在 stable 里、却在历史前面，
    # 它们一变，后面的历史就整段重算。这里每轮打一行指纹，下一轮能算出它们多久变一次。
    # 注意：nonebot 的 logger 走 loguru，不认 %s 占位（实测会原样打出 "%s"），
    # 所以这里用 f-string 先把值拼好，整条消息里不留任何占位符。
    # 2026-10-28（t3 阶段一）补诊断字段：channel / slow_fp / snap_hit / snap_age /
    # hist_len / hist_tok / prefix_tok / pid —— 用来定性"同一 scope 两个通道是不是各拼一份"
    # 和"10.5K 长会话每轮断在 2176"到底是内容变还是快照没复用。
    # 阶段二再加 pub_fp / pend / pub / pub_reason：能直接看出"这轮是不是换了前缀"。
    # ① 稳定前缀指纹变化告警：前缀一变，后面整段历史都失去缓存
    try:
        _fp_now = _fp8(stable)
        _fp_key = f"{gid}:{channel}"
        _fp_prev = _last_stable_fp.get(_fp_key)
        if _fp_prev and _fp_prev != _fp_now:
            logger.warning(
                f"[缓存] 稳定前缀变了 {_fp_key} {_fp_prev} → {_fp_now}（本次全量冷启动；"
                f"若频繁出现请检查 persona/lexicon/archive 是否被改动）"
            )
        _last_stable_fp[_fp_key] = _fp_now
    except Exception:
        pass
    try:
        _prefix_tok = _est_tokens(stable) + int(hist_tok or 0)
        logger.info(
            f"[缓存] 块指纹 scope={gid} channel={channel} private={int(bool(is_private))} "
            f"mode={SLOW_MODE} lexicon={_fp8(lexicon_prompt)} human={_fp8(_human_text)} "
            f"archive={_fp8(archive_prompt)} learn={_fp8(learn_prompt)} "
            f"slow_fp={_slow_fp} pub_fp={_pub_fp} snap_hit={_snap_hit} "
            f"snap_age={_snap_age:.0f} pend={_pending} pub={_publishes} "
            f"pub_reason={_publish_reason or '-'} tail={len(slow_blocks)} "
            f"hist_len={hist_len if hist_len is not None else -1} "
            f"hist_tok={hist_tok if hist_tok is not None else -1} "
            f"prefix_tok={_prefix_tok} pid={os.getpid()}"
        )
    except Exception:
        pass
    # 易变尾巴：固定 3-5 行短标签（今日状态/对象/情绪/群黑话/别复读），由调用方挂到最后一条 user 之前。
    # 语义全在 STATUS_LEGEND，这里只有值——尾巴每轮都付全价，越短越好（t4/P1-2）。
    volatile = build_volatile_block(
        gid, user_id, is_insulted, is_creep, is_threat,
        affinity, tag, is_private, sex, douyin_block,
    )
    # 今天是什么日子 / 今天什么天气：都只在该提的时候才加一行
    # extra 块按「变化频率从慢到快」排列（缓存前缀的边界在第一个变化处）：
    #   人格内核（每天）→ 成长条目（每 2h）→ 日期/天气（天/小时）→ 动态回忆（每轮）
    _extra = []
    # 语义召回块（每轮按查话变）放最前：它变化不影响前面的历史缓存
    if related_prompt:
        _extra.append(related_prompt)
    # 人格内核：灵魂聚合产出，每天 3:10 变一次 —— 最慢，放最前
    try:
        from server import soul_aggregation as _sa
        _soul = _sa.prompt_block()
        if _soul:
            _extra.append(_soul)
    except Exception:  # noqa: BLE001
        pass
    # 情欲状态（2026-11-05）：只进主人私聊，且 <25 时 intimate_line 自己返回空串，
    # 所以平淡聊天不会多出这一行。放 _extra（每轮可变）而不是 stable，不破坏前缀缓存。
    if is_private and str(user_id) == str(YOUR_QQ_ID):
        try:
            from plugins import hormones as _hx
            _il = _hx.intimate_line()
            if _il:
                _extra.append(_il)
                # 2026-11-05：按情欲档位塞一批可用词，避免她描述时反复用那几句
                _ar = float((_hx.derive_emotions() or {}).get("情欲") or 0)
                _vh = _intimate_vocab_hint(_ar)
                if _vh:
                    _extra.append(_vh)
                _dm = _desire_manual(_ar)
                if _dm:
                    _extra.append(_dm)
        except Exception:  # noqa: BLE001
            pass
    _growth = _growth_snippet()
    if _growth:
        _extra.append(_growth)
    # 说话规矩：从《人是怎么说话的》吸收的行为规则（memory/talk_rules.json，改即刻生效）
    try:
        _tr = _talk_rules_snippet(
            "private" if str(gid).startswith("private_") else "group",
            str((GROUP_AI_STATE.get(str(gid)) or {}).get("mood") or ""))
        if _tr:
            _extra.append(_tr)
    except Exception:  # noqa: BLE001
        pass
    # 群里被怼多了 → 一句「对这群人」的态度提示（不影响对主人）
    try:
        if not str(gid).startswith("private_"):
            _gl = _sa_group_line()
            if _gl:
                _extra.append(_gl)
    except Exception:  # noqa: BLE001
        pass
    # 防御机制：从最近几轮「一问一答」里认出她现在在用的那种（同一机制 ≥2 次才提示）
    try:
        _hp: list = []
        _prev_u = None
        for _sp, _tx, _mine in list(_history.get(str(gid)) or [])[-8:]:
            if not _mine:
                _prev_u = _tx
            elif _prev_u is not None:
                _hp.append((_prev_u, _tx))
                _prev_u = None
        _dl = _sa.defense_line(_hp)
        if _dl:
            _extra.append(_dl)
    except Exception:  # noqa: BLE001
        pass
    # 最近在读的小说（reading 插件）：让聊天时能讨论剧情
    try:
        from plugins import reading as _reading
        _rl = _reading.prompt_line()
        if _rl:
            _extra.append(_rl)
        _rc = _reading.context_for(user_text)
        if _rc:
            _extra.append("· 你读到的相关剧情：" + chr(10) + _rc)
    except Exception:  # noqa: BLE001
        pass
    try:
        from dates import today_line as _dates_line
        _dl = _dates_line()
        if _dl:
            _extra.append(_dl)
    except Exception:  # noqa: BLE001
        pass
    try:
        from weather import weather_line as _weather_line
        _wl = _weather_line(user_text)
        if _wl:
            _extra.append(_wl)
    except Exception:  # noqa: BLE001
        pass
    if _extra:
        volatile = volatile + "\n" + "\n".join(_extra)

    # 2026-11-05（借 digital-life-core 的「应激压制」）：皮质醇飙高时
    # 把注意力压到眼下这件让她不安的事上，同时**明确告诉她记忆还在** ——
    # 这一句是关键：高压状态下最怕她以为自己把什么都忘了。
    try:
        from plugins import hormones as _hor_s

        _cort = float(_hor_s.values().get("CORT", 20.0))
        if _cort >= 80:
            volatile = ("【现在有点上头，先把眼前这件事说清楚】你现在很紧、很急，"
                        "别的先放一放。\n"
                        "（记忆都还在，不会因为这会儿顾不上就想不起来——"
                        "想不起的只是当下注意力，不是忘事。）\n" + volatile)
            logger.info(f"[应激] 皮质醇 {_cort:.0f} ≥ 80 → 危机优先注入")
    except Exception as _se:  # noqa: BLE001
        logger.debug(f"[应激] 跳过：{type(_se).__name__}")
    # 2026-11-05：钱包也进 prompt —— 她该知道自己还剩多少生活费
    # 只读缓存（聊天入口刷过 _BAL_CACHE），不发请求
    try:
        _bv = _BAL_CACHE.get("v")
        if _bv is not None:
            if _bv < 2:
                # 和 balance_line 对齐：<2 就提示「可以切智谱」，别只会说没钱
                volatile = volatile + "\n【钱包】DeepSeek 官方只剩 %.2f 元，快见底。可以提醒主人发 /切智谱 换到智谱 GLM（那边还有免费额度）。" % _bv
            elif _bv < 10:
                volatile = volatile + "\n【钱包】余额 %.2f 元，不多。" % _bv
            else:
                # 2026-11-06：原来只在 <10 元时才注入，11 块多时什么都不说，
                # 于是她只能从长期记忆里翻旧数字。改成多少都告诉她，语气分档。
                volatile = volatile + "\n【钱包】余额 %.2f 元（实时数，别用记忆里的旧数字）。" % _bv
    except Exception:  # noqa: BLE001
        pass

    # 2026-11-06：B站看完的视频要进 prompt，否则她自己都不知道看过什么。
    # 实测：她看完一集主动问「会馆有个是啥断了难受」，主人回问，她一口咬定没说过 ——
    # 因为观后感只存在 bili_daily.json 里，聊天时读不到。
    try:
        import json as _bj
        from pathlib import Path as _bp
        _bd = _bj.loads((ROOT / "memory" / "bili_daily.json").read_text(encoding="utf-8"))
        # 观后感在 history 里（seen 只是 BV 号字符串列表，别读错了）
        _hist_bd = _bd.get("history") if isinstance(_bd.get("history"), list) else []
        _recent_bili = []
        for _it in reversed(_hist_bd):
            if isinstance(_it, dict) and _it.get("summary"):
                _recent_bili.append(_it)
            if len(_recent_bili) >= 2:
                break
        if _recent_bili:
            _lines = []
            for _it in _recent_bili:
                _t = str(_it.get("title") or "")[:20]
                _s = str(_it.get("summary") or "")[:90]
                if _s:
                    _lines.append(f"「{_t}」— {_s}")
            if _lines:
                volatile = volatile + "\n【你最近看的视频】" + "；".join(_lines)
    except Exception:  # noqa: BLE001
        pass

    # 2026-11-06（借「能动性四件套」第④条）：把「上次主动说有没有人接」放进 prompt。
    # 被无视得多就提醒她收敛，老有人接就放开点 —— 这是能动性和随机发言的分界。
    try:
        from plugins import proactive_feedback as _pfb
        _pf_line = _pfb.prompt_line(str(record_scope or ""))
        if _pf_line:
            volatile = volatile + "\n" + _pf_line
    except Exception:  # noqa: BLE001
        pass

    # 2026-11-05（借 digital-life-core 的能量体征）：体力低时行为强度跟着降。
    # 80 以上不注入任何东西（保持正常），越低话越少。
    try:
        from plugins import energy as _ener

        _e_line = _ener.prompt_line()
        if _e_line:
            volatile = volatile + "\n" + _e_line
    except Exception as _ee:  # noqa: BLE001
        logger.debug(f"[体力] 跳过：{type(_ee).__name__}")

    # 原话锚（dsh-hippocampus 思路）：她自己说过的几句原话，进慢变块——
    # 不进 stable，所以不破坏前缀缓存；按 30 分钟桶冻结，块内不变。
    try:
        from plugins import anchor as _anchor

        _ab = _anchor.anchor_block(mem_scope)
        if _ab:
            slow_blocks.append(_ab)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[锚] 生成失败：{type(e).__name__}: {e}")

    # 2026-11-07：**复合情绪那句话**（"挺高兴——心情松快，愿意接话；身子有点沉"）。
    # 事故：prompt_line() 昨晚改成输出复合情绪了，但只在 make_douyin_video 里被调用，
    # 主聊天根本没吃到 —— 聊天里还是逐维的「【情欲 54/100】起来了」那套。
    # 逐维描述保留（它管具体动作倾向），这里补一句整体感觉。
    try:
        from plugins import hormones as _hx2
        _emo_line = _hx2.prompt_line()
        if _emo_line and "此刻" in _emo_line:
            _feel = _emo_line.split("此刻", 1)[1].lstrip("：: ")
            if _feel:
                volatile = volatile + chr(10) + "【这一刻的感觉】" + _feel
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[情绪行] 注入失败：{type(e).__name__}: {e}")

    # 2026-11-07：关系阶段（初识→试探→亲密→稳定，只升不降）。
    # touch 带 60 秒去重，一轮里多次组装 prompt 不会虚涨 depth。
    try:
        from plugins import relation_stage as _rs
        _rs.touch(user_id)
        _rl = _rs.line(user_id)
        if _rl:
            volatile = volatile + "\n" + _rl
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[关系] 注入失败：{type(e).__name__}: {e}")

    # 2026-11-07：这一轮该说多长、写多细 —— 由情欲/本我/累/冲突算，不由关键词算。
    # 主人要的是"按底层激素-情绪-三我来"，不是"听到某个词就切长文模式"。
    try:
        from plugins import mood_compound as _mc2
        _depth, _dmsg = _mc2.express_depth()
        if _dmsg:
            volatile = volatile + "\n【这轮的劲儿】" + _dmsg
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[展开度] 算失败：{type(e).__name__}: {e}")

    # 2026-11-07：深圳生活知识库 —— 住哪儿 / 去哪上班 / 水电网怎么不被坑。
    # 只在命中时注入。
    try:
        from plugins import sz_kb as _sz
        _szline = _sz.line(user_text)
        if _szline:
            volatile = volatile + chr(10) + _szline
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[深圳] 注入失败：{type(e).__name__}: {e}")

    # 2026-11-07：注意力漂移 —— 她可以被群里某个细节勾走一句，然后回正题。
    # 搬自 MaiBot 的 attention_drift（四档漂移 + 三档回钩 + 短反应），
    # 但档位不是配置出来的，是**由三我和情绪推的**：
    #   本我强 + 开心高 + 不累 → wild；超我压着 + 累 → 不注入（那轮她压根不飘）。
    # 私聊收敛：只给群聊注入，私聊不飘（一对一走神很怪）。
    try:
        from plugins import attention_drift as _ad
        if not is_private:
            _adline = _ad.line()
            if _adline:
                volatile = volatile + chr(10) + _adline
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[走神] 注入失败：{type(e).__name__}: {e}")

    # 2026-11-07：阿德勒的「自卑 → 补偿」。
    # 自卑值低的时候 adler.line() 返回空串，不会注入 —— 免得她一直在「我在补偿」的状态里演心理剧。
    try:
        from plugins import adler as _adl
        _adline = _adl.line()
        if _adline:
            volatile = volatile + chr(10) + _adline
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[阿德勒] 注入失败：{type(e).__name__}: {e}")

    # 2026-11-07：马斯洛的需求层次 —— 此刻最缺什么，其他都往后放。
    # 只报**最急的一层**（五层全报会变成数据面板，不是她的感受）；
    # 什么都不缺时返回空串，不注入。
    try:
        from plugins import hierarchy as _hi
        _hiline = _hi.line()
        if _hiline:
            volatile = volatile + chr(10) + _hiline
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[需求层次] 注入失败：{type(e).__name__}: {e}")

    # 2026-11-07：骗局识别 —— 他问的事像不像个坑。
    # 只在命中时才注入（不占常驻 token），认出来就直接说，别绕。
    try:
        from plugins import scam_kb as _sk
        _scam = _sk.line(user_text)
        if _scam:
            volatile = volatile + chr(10) + _scam
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[骗局] 识别失败：{type(e).__name__}: {e}")

    # 2026-11-05：执念池 —— 只把张力最高的 1~2 条放进 volatile。
    # 有生命周期：提起加张力、主人回应或久没人接就淡掉闭合，闭合后不再出现。
    # 以前是让这类话题在历史里无限繁殖（"欢迎/收尾/半碗粥"就是这么循环的）。
    try:
        from plugins import obsessions as _obs_p

        _ob = _obs_p.active(2)
        if _ob:
            volatile = volatile + "\n" + _ob
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[执念] 注入失败：{type(e).__name__}: {e}")

    return stable, volatile, slow_blocks


def build_system_prompt(gid, user_id, is_insulted=False, is_creep=False, is_threat=False, affinity=50, tag="", is_private=False, sex="unknown", user_text=""):
    """兼容旧调用：返回一整段 system prompt（stable + volatile）。

    聊天主链路请用 build_prompt_parts 拆开拼，否则易变段会把历史拖出缓存区。
    """
    stable, volatile, _slow = build_prompt_parts(
        gid, user_id, is_insulted, is_creep, is_threat, affinity, tag,
        is_private, sex, user_text,
    )
    # 2026-11-07：末尾让她自评一句（只进 volatile，不进 stable，缓存不受影响）。
    # 这一行在发送前会被 self_report.strip 剥掉，用户看不到。
    try:
        from plugins import self_report as _sr
        _tail = _sr.EVAL_HINT
    except Exception:  # noqa: BLE001
        _tail = ""
    return stable + volatile + _tail

async def _web_or_chat(messages: list, engine: str, is_private: bool, plain_text: str, images: list,
                        context_key: str = "", channel: str = "qq") -> tuple[str, dict]:
    """对外回复的统一出口：剥掉自评标签并记录。

    真正的逻辑在 _web_or_chat_raw；这里只做一件事 ——
    保证任何路径产出的 self-report 标签都不会漏到用户眼前。
    """
    reply, meta = await _web_or_chat_raw(messages, engine, is_private, plain_text,
                                         images, context_key, channel)
    try:
        from plugins import self_report as _sr
        reply, ev = _sr.strip(reply)
        if ev:
            _sr.note(ev, context_key or channel or "")
    except Exception:  # noqa: BLE001
        pass
    # 2026-11-07：把她自己说的话喂进「行为证据」——用来量三我实际有多强。
    # 实测成本：单条 0.84 ms（含写盘），一小时 60 条也才 50 ms，
    # 相比一次 LLM 调用（2~5 秒）是 1/3000，可以忽略。
    try:
        from plugins import ego_evidence as _ee
        _ee.sample(reply)
    except Exception:  # noqa: BLE001
        pass
    return reply, meta


def _load_name_override() -> str:
    """memory/persona_name.json 里的 name 优先（改名字不用动 .env）。"""
    try:
        d = json.loads((ROOT / "memory" / "persona_name.json").read_text(encoding="utf-8-sig"))
        return str(d.get("name") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


NICKNAME = _load_name_override() or cfg.persona.nickname
ai = on_message(priority=10, block=False)
private_ai = on_message(priority=10, block=False)
collector = on_message(priority=1, block=False)
group_decrease = on_notice(priority=5, block=False)
group_recall = on_notice(priority=5, block=False)

# =====================================================================
# 赛博墓碑 & 赛博内鬼
# =====================================================================
@group_decrease.handle()
async def _(event: GroupDecreaseNoticeEvent):
    if cfg.allow_groups and event.group_id not in cfg.allow_groups: return
    if event.sub_type == 'leave': action = "主动退群了"
    elif event.sub_type == 'kick_me': action = "被踢出了群聊"
    elif event.sub_type == 'kick': action = "被管理员踢出了群聊"
    else: action = "离开了群聊"
    last_msg = await asyncio.to_thread(
        get_last_message, str(event.user_id), f"group_{event.group_id}"
    )
    if last_msg:
        speaker, msg = last_msg
        if len(msg) > 30: msg = msg[:30] + "..."
        tomb_text = f"🪦 **赛博墓碑**\n群友【{speaker}】刚刚{action}。\n他留下的最后一句遗言是：“{msg}”"
    else:
        tomb_text = f"🪦 **赛博墓碑**\n群友【{event.user_id}】刚刚{action}。\n很遗憾，他没留下任何遗言就溜了。"
    try: await get_bot().send_group_msg(group_id=event.group_id, message=tomb_text)
    except Exception as e: logger.error(f"墓碑播报失败: {e}")

async def flush_recall_batch(gid):
    try:
        await asyncio.sleep(2.5)
        async with RECALL_LOCK:
            recalls = RECALL_BATCH.pop(gid, [])
        if not recalls: return
        by_user = defaultdict(list)
        for r in recalls: by_user[r["nickname"]].append(r)
        bot = get_bot()
        for nickname, items in by_user.items():
            count = len(items)
            if count == 1:
                msg_content = items[0]["message"]
                text = "【图片】" if any(seg.type == "image" for seg in msg_content) else msg_content.extract_plain_text().strip()
                if len(text) > 50: text = text[:50] + "..."
                recall_text = f"🚨 **赛博内鬼播报**\n群友【{nickname}】刚刚撤回了消息：\n“{text}”\n别以为本鱼没看见！"
            else:
                recall_text = f"🚨 **赛博内鬼播报**\n群友【{nickname}】刚刚心虚地连撤了 {count} 条消息！\n本鱼已经全部截图留证了！"
            try:
                await bot.send_group_msg(group_id=gid, message=recall_text)
                if count == 1 and any(seg.type == "image" for seg in items[0]["message"]):
                    await bot.send_group_msg(group_id=gid, message=items[0]["message"])
            except Exception as e: logger.error(f"内鬼播报发送失败: {e}")
    except Exception as e: logger.error(f"撤回批处理任务异常: {e}")
    finally:
        RECALL_TASK_RUNNING.discard(gid)
        if RECALL_BATCH.get(gid) and gid not in RECALL_TASK_RUNNING:
            RECALL_TASK_RUNNING.add(gid)
            asyncio.create_task(flush_recall_batch(gid))

@group_recall.handle()
async def _(event: GroupRecallNoticeEvent):
    if cfg.allow_groups and event.group_id not in cfg.allow_groups: return
    if str(event.user_id) == str(event.self_id): return
    if str(event.operator_id) == str(event.self_id): return 
    cache_key = f"{event.group_id}_{event.message_id}"
    if cache_key in RECALL_CACHE:
        cached = RECALL_CACHE.pop(cache_key)
        gid = event.group_id
        async with RECALL_LOCK:
            RECALL_BATCH[gid].append(cached)
            if gid not in RECALL_TASK_RUNNING:
                RECALL_TASK_RUNNING.add(gid)
                asyncio.create_task(flush_recall_batch(gid))
# =====================================================================

TTS_EMOTION_VOICES = {
    # 情绪 → (音色, 语速增量, 音调增量)；五个音色都实测 HTTP 200 可用
    "开心": ("ICL_uranus_zh_female_tianmeijiaoqiao_tob", 8, 6),
    "害羞": ("ICL_uranus_zh_female_tianmeijiaoqiao_tob", -2, 8),
    "怒": ("ICL_uranus_zh_female_huopodiaoman_tob", 12, -6),
    "烦": ("ICL_uranus_zh_female_huopodiaoman_tob", 6, -2),
    "丧": ("zh_female_cancan_uranus_bigtts", -8, -6),
    "累": ("zh_female_mengyatou_uranus_bigtts", -10, -4),
    "害怕": ("zh_female_mengyatou_uranus_bigtts", 6, 10),
    # 情欲（2026-11-05）：刻意沿用傲娇女友音色不换人——情欲是"同一个她压低了声音"，
    # 不是换了个人。靠语速大幅下压 + 音调下沉做出贴耳的低哑质感。
    "情欲": ("ICL_uranus_zh_female_aojiaonvyou_tob", -18, -6),
}

# ===== 口语化改写 + 场景语气 =====
# 活人感第一靠节奏：念书面稿天然假，所以合成前先把它改成「能说出口的话」。

_SPOKEN_MAP = [
    ("已经", "都"), ("非常", "特别"), ("但是", "不过"), ("然而", "可是"),
    ("因为", "毕竟"), ("什么", "啥"), ("怎么", "咋"), ("不要", "别"),
    ("没有", "没"), ("知道", "晓得"), ("可以", "能"), ("应该", "得"),
    ("我们", "咱"), ("真的", "真"),
]

_SCENE_CONTEXT = {
    "怒": "用生气、压着火气的语气说，语速偏快，尾音别拖，带一点咬牙切齿",
    "烦": "用不耐烦的语气说，语速快、句尾下沉，像在敷衍",
    "丧": "用低落、有气无力的语气说，语速慢，声音发虚",
    "累": "用困倦、含糊的语气说，像刚睡醒或者很累，气声多一点",
    "害怕": "用紧张、不太确定的语气说，声音发紧",
    "开心": "用轻快、带笑的语气说，尾音上扬",
    "害羞": "用不好意思、声音变小的语气说，带一点鼻音",
    "撒娇": "用撒娇的语气说，尾音上扬，带鼻音，别太甜腻",
    # 情欲（2026-11-05）：这条是豆包 context_texts 里最吃劲的一句，写得越具体越像
    "情欲": "用压低的、贴得很近的气声说，语速慢，尾音黏着不走，像在耳边说话；"
            "别喊、别甜腻、别播音腔，气息比平时重一点",
}


def _spoken_rewrite(text: str) -> str:
    """去标记 → 口语词替换 → 长句拆短 → 句尾补一个语气词。

    保守改写：不重排语义，出任何异常都原样返回。
    """
    try:
        t = re.sub(r'\[.*?\]|\(.*?\)|（.*?）', '', text or "")
        t = re.sub(r'[\*\#\_\`\~]|https?://\S+', '', t).strip()
        if not t:
            return ""
        for a, b in _SPOKEN_MAP:
            t = t.replace(a, b)
        # 长句在第一个逗号后加停顿，制造换气点
        out = []
        for seg in re.split(r'(?<=[。！？；\n])', t):
            seg = seg.strip()
            if not seg:
                continue
            if len(seg) > 14 and "，" in seg:
                seg = seg.replace("，", "，……", 1)
            out.append(seg)
        t = "".join(out)
        # 句尾语气词：整段只补一次，已经有就不动
        m = re.search(r'([。！？])(?![\s\S]*[。！？])', t)
        if m:
            i = m.start()
            body = t[:i]
            if body and body[-1] not in "啊嘛呢吧哈呀嘞嘿":
                body += random.choice(["啊", "嘛", "呢", "吧"])
            t = body + t[i:]
        return t.strip()
    except Exception:  # noqa: BLE001
        return text


def _arousal() -> float:
    """情欲值 0~100（2026-11-05）。

    情欲是第 8 维，**不在 EMOTIONS 列表里**——所以 _load_emotion() 拿不到它。
    音色、语气这些需要它的地方单独调这个，别去动七维口径。
    """
    try:
        from plugins import hormones
        if hormones.ENABLED:
            return float(hormones.derive_emotions().get("情欲") or 0.0)
    except Exception:  # noqa: BLE001
        pass
    return 0.0


def _tts_emo_vector() -> list[float]:
    """把她当前状态换算成 IndexTTS2 的 8 轴情绪向量（0~1）。

    2026-11-05 修：原来 9881 服务端自己去读 memory/emotion_state.json，
    但那个文件在情绪改由 hormones 派生之后就不再更新了（实测停在 00:51 的旧值），
    于是 TTS 永远用一套陈旧情绪，"情欲满了声音还是正常说话"。
    现在改成主进程算好直接传过去，服务端只负责合成。

    8 轴顺序（IndexTTS2）：喜悦 / 愤怒 / 悲伤 / 恐惧 / 厌恶 / 低落 / 惊喜 / 平静
    """
    try:
        vals = _load_emotion()
    except Exception:  # noqa: BLE001
        vals = {}

    def g(k: str) -> float:
        return max(0.0, min(1.0, float(vals.get(k) or 0.0) / 100.0))

    vec = [
        g("开心"),        # 0 喜悦
        g("怒"),          # 1 愤怒
        g("丧"),          # 2 悲伤
        g("害怕"),        # 3 恐惧
        g("烦") * 0.5,    # 4 厌恶（烦一半分给厌恶）
        g("累"),          # 5 低落
        0.0,              # 6 惊喜
        0.15,             # 7 平静底噪
    ]
    # 情欲：IndexTTS2 8 轴里没有这一维，用"兴奋拉满 + 平静压到底 + 低落压下去"来表征。
    # 40 是起效线；低落轴必须一起压——深夜 MEL 高会把「累」顶到 60+，
    # 不压下去的话合成出来是"困得不想动"，正好把情欲氛围毁掉。
    ar = _arousal()
    if ar >= 40:
        k = min(1.0, (ar - 40) / 60.0)
        vec[6] = max(vec[6], 0.15 + 0.55 * k)   # 6 惊喜/兴奋：0.15 → 0.70
        vec[7] = max(0.0, 0.15 - 0.13 * k)      # 7 平静：0.15 → 0.02
        vec[0] = max(vec[0], 0.45 + 0.30 * k)   # 0 喜悦：至少 0.45 起
        vec[5] = vec[5] * (1.0 - 0.55 * k)      # 5 低落：最多压掉 55%

    # 2026-11-05（主人试听定的）：**正向情绪要配等量平静轴，否则颤成哭腔**。
    # 实测：喜悦 0.95 平静 0 → 哭腔；喜悦 0.5 平静 0.5 → 真高兴。
    # 原因是这个模型表达"兴奋"靠声带紧张（颤），而颤的机理和哭一样，
    # 只有平静轴能压住。负面情绪不需要——它们自带向下的重力。
    # 注意只对"喜悦"做，不对"惊喜"做：情欲那路本来就是要颤的。
    # 2026-11-06：系数从 0.85 降到可调（默认 0.65）。
    # 0.85 是 11-05 试听时定的（怕颤成哭腔），但实测副作用是**听起来太平**：
    # 喜悦 0.576 × 0.85 = 平静 0.49 —— 两个轴几乎一样高，声音就淡了。
    # 0.65 给 0.37，压住颤但留出情绪落差。觉得还是平就往下调，觉得颤就往上。
    _calm_ratio = 0.65
    try:
        _calm_ratio = float(os.getenv("TTS_CALM_RATIO") or 0.65)
    except Exception:  # noqa: BLE001
        pass
    if vec[0] > 0.30:
        vec[7] = max(vec[7], vec[0] * _calm_ratio)
    return [round(x, 3) for x in vec]


def _tts_speed() -> float:
    """这一句该说多快（atempo 倍数）。

    2026-11-05 实测：**语速是节奏问题，情绪向量管不了**——长句怎么调都是从容的。
    「急促」靠三样：文案改短 + 逗号顿开 + 这里给 1.15 倍速。
    1.15 是主人试听后定的（1.25 太机械，原始又不够急）。

    触发条件：兴奋轴（vec[6]，情欲高时会拉起来）或情欲本身够高。
    """
    try:
        vec = _tts_emo_vector()
        excite = float(vec[6]) if len(vec) > 6 else 0.0
        if excite >= 0.55:
            return 1.15
        if _arousal() >= 55:
            return 1.15
    except Exception:  # noqa: BLE001
        pass
    return 1.0


def _arousal_ref() -> str:
    """情欲高时换成带气息的参考音频（2026-11-05）。

    为什么必须换 ref：memory/tts_emotion_recipes.json 的核心规律第 5 条——
    「气息（喘、抖）是生理反应不是情绪，向量做不出来，只能靠参考音频本身带」。
    以前无论情欲多高都用 voice_ref_main.wav（一句正常说话），所以做爱的场景里
    合成出来还是「正常说话」的语气，听着就是「语音是别的」。

    素材：data/voice_ref/定稿试听/ 那 15 条（同一音色的不同情绪定稿）。
    阈值 25 起效、40 换中档、60 换最高，跟 _tts_emo_vector 的情欲起效线 40 对齐略低一点，
    保证「刚起来」的时候就已经带上了。
    """
    try:
        ar = _arousal()
    except Exception:  # noqa: BLE001
        return ""
    if ar < 25:
        return ""
    if ar >= 60:
        name = "14_到了临界.wav"
    elif ar >= 40:
        name = "11_低喘求饶.wav"
    else:
        name = "9_情欲撩人.wav"
    p = ROOT / "data" / "voice_ref" / "定稿试听" / name
    return str(p) if p.exists() else ""


def _scene_context(text: str = "") -> str:
    """按主导情绪或文本关键词挑一句 context_texts（豆包对这句很敏感）。"""
    try:
        # 情欲优先（2026-11-05）：够高时压过七维——七维里没有这一维，不特判就永远走不到
        if _arousal() >= 45:
            return _SCENE_CONTEXT["情欲"]
        vals = _load_emotion()
        top = max(vals, key=lambda k: vals.get(k, 0)) if vals else ""
        if top and float(vals.get(top) or 0) >= 30 and top in _SCENE_CONTEXT:
            return _SCENE_CONTEXT[top]
    except Exception:  # noqa: BLE001
        pass
    t = text or ""
    rules = [
        ("撒娇", ("抱抱", "亲亲", "疼我", "好不好", "嘛", "哼")),
        ("怒", ("烦死", "闭嘴", "滚", "气死", "过分")),
        ("累", ("困", "睡了", "好累", "不想动")),
        ("开心", ("哈哈", "开心", "好耶", "太好了")),
        ("丧", ("算了", "没意思", "好难", "唉")),
    ]
    for key, kws in rules:
        if any(k in t for k in kws):
            return _SCENE_CONTEXT[key]
    return TTS_CONTEXT


def _tts_voice() -> tuple[str, int, int]:
    """按当前主导情绪选音色/语速/音调。主导情绪不明确时用默认配置。

    2026-11-05：情欲优先级最高。它在七维之外，只查 _load_emotion() 永远选不到它，
    这就是"情欲满了声音还是正常"的原因。情欲越高语速压得越低。
    """
    try:
        ar = _arousal()
        if ar >= 45:
            sp, dr, dp = TTS_EMOTION_VOICES["情欲"]
            # 45→额外 -0，100→额外 -10：越上来话越慢、越黏
            dr = int(dr - max(0.0, ar - 45) * 0.18)
            return (sp, max(-50, min(100, TTS_SPEECH_RATE + dr)),
                    max(-50, min(100, TTS_POST_PITCH + dp)))
        vals = _load_emotion()
        top = max(vals, key=lambda k: vals.get(k, 0))
        if float(vals.get(top) or 0) < 30:
            return TTS_SPEAKER, TTS_SPEECH_RATE, TTS_POST_PITCH
        sp, dr, dp = TTS_EMOTION_VOICES.get(top, (TTS_SPEAKER, 0, 0))
        return (sp, max(-50, min(100, TTS_SPEECH_RATE + dr)),
                max(-50, min(100, TTS_POST_PITCH + dp)))
    except Exception:  # noqa: BLE001
        return TTS_SPEAKER, TTS_SPEECH_RATE, TTS_POST_PITCH


async def _doubao_tts(text: str) -> str | None:
    """火山 Doubao-TTS 2.0（seed-tts-2.0）HTTP 单向流合成。成功返回 mp3 路径；未配 Key 或失败返回 None。"""
    if not TTS_API_KEY:
        logger.warning(f"[豆包TTS] 环境未读到 TTS_API_KEY，跳过豆包（speaker={TTS_SPEAKER}）")
        return None
    _speaker, _rate, _pitch = _tts_voice()
    logger.info(f"[豆包TTS] 开始合成 speaker={_speaker} rate={_rate} pitch={_pitch} 文本长度={len(text)}")
    req_params = {
        "text": text,
        "speaker": _speaker,
        "audio_params": {
            "format": "mp3",
            "sample_rate": 24000,
            "speech_rate": _rate,
            "loudness_rate": TTS_LOUDNESS,
        },
        "post_process": {"pitch": _pitch},
    }
    _ctx = _scene_context(text)
    if _ctx:
        req_params["context_texts"] = [_ctx]
    payload = {"req_params": req_params}
    headers = {
        "X-Api-Key": TTS_API_KEY,
        "X-Api-Resource-Id": TTS_RESOURCE_ID,
        "X-Api-Request-Id": str(uuid.uuid4()),
        "Content-Type": "application/json",
        "Connection": "keep-alive",
        "X-Control-Require-Usage-Tokens-Return": "*",
    }
    audio = bytearray()
    logid = ""
    try:
        async with http_client.stream("POST", DOUBAO_TTS_URL, json=payload, headers=headers) as resp:
            logid = resp.headers.get("X-Tt-Logid", "")
            if resp.status_code != 200:
                err_body = (await resp.aread()).decode("utf-8", "ignore")[:300]
                logger.warning(f"[豆包TTS] HTTP {resp.status_code} logid={logid} body={err_body}")
                return None
            async for line in resp.aiter_lines():
                if not line or not line.strip():
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                code = chunk.get("code", 0)
                if code == 20000000:  # 成功结束码
                    break
                if code and code != 0:
                    logger.warning(f"[豆包TTS] code={code} message={chunk.get('message')} logid={logid}")
                    return None
                data = chunk.get("data")
                if data:
                    audio.extend(base64.b64decode(data))
        if not audio:
            logger.warning(f"[豆包TTS] 返回音频为空 logid={logid}")
            return None
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        tmp.write(audio)
        tmp.close()
        logger.info(f"[豆包TTS] 合成成功 speaker={_speaker} 字节={len(audio)} logid={logid}")
        record_tts_cost(len(text))
        return tmp.name
    except Exception as e:
        logger.error(f"[豆包TTS] 合成失败: {type(e).__name__}: {e} logid={logid}")
        return None


DOUYIN_DIR = ROOT / "output" / "douyin"


def _ffmpeg_exe() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _media_duration(path: Path) -> float:
    try:
        proc = subprocess.run([_ffmpeg_exe(), "-i", str(path)], capture_output=True)
        text = (proc.stderr or b"").decode("utf-8", "ignore")
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", text)
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:
        pass
    return 0.0


def _ass_time(sec: float) -> str:
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _write_ass(path: Path, lines: list[str], duration: float) -> None:
    per = max(2.0, duration / max(1, len(lines)))
    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n"
        "WrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,Microsoft YaHei,64,&H00FFFFFF,&H00FFFFFF,&H00202020,&H80000000,"
        "-1,0,0,0,100,100,0,0,1,4,2,2,80,80,220,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    body = []
    for i, line in enumerate(lines):
        text = str(line).replace("{", "").replace("}", "").replace("\n", " ").strip()
        if not text:
            continue
        body.append(
            f"Dialogue: 0,{_ass_time(i * per)},{_ass_time((i + 1) * per)},Default,,0,0,0,,{text}\n"
        )
    path.write_text(header + "".join(body), encoding="utf-8")


DOUYIN_REF_IMAGE = Path(os.getenv("DOUYIN_REF_IMAGE", str(ROOT / "表情包" / "开心" / "fat_fish.jpg")))


def _ref_data_url() -> str:
    """把大肥鱼的参考形象转成 data URL，出图时锁死角色外观。"""
    try:
        img = Image.open(DOUYIN_REF_IMAGE).convert("RGB")
        img.thumbnail((512, 512))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=88)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return ""


_SELF_DRAW_RE = re.compile(
    r"(?:画个|画一下|画画?|绘制|来一张|整一张)\s*(?:一下)?\s*(?:可爱的|个)?\s*(?:你自己|你自己在|自己|你|本鱼)\s*(.*)"
)


_DRAW_ALIASES = ("画图", "画画", "画一张", "画个图", "帮我画", "给我画", "画幅画")


def _draw_prompt_of(text: str) -> str | None:
    """把「画图/画画 xxx」里的 xxx 拆出来；不是画图指令返回 None。"""
    t = str(text or "").strip()
    for _kw in _DRAW_ALIASES:
        if t.startswith(_kw):
            rest = t[len(_kw):].strip(" ：:，,")
            # 单独一个"画图"没内容时，让调用方当普通聊天
            return rest if rest else ""
    return None


def _self_draw_action(text: str) -> str | None:
    """判断是不是"画她自己"这类模糊指令，返回动作描述（没有就返回空串）。

    命中示例：画你自己 / 画个你在吃冰淇淋 / 画画自己在干啥 / 来一张你的
    不命中：画图 半裸少女，坐在校园草地上…（这是明确画图，要原样执行）
    """
    t = str(text or "").strip()
    if not t or t.startswith("画图"):
        return None
    m = _SELF_DRAW_RE.search(t)
    if not m:
        return None
    action = (m.group(1) or "").strip(" ，,。.！!？?的呀啊呢吧")
    if action in ("在干啥", "在干嘛", "在做什么", "干嘛呢", "在干什么"):
        action = ""
    return action


async def _self_draw_improvise_llm(hint: str = "") -> str:
    """让模型根据她此刻的念头/欲望/活动，写一句「她现在在干嘛」。

    2026-11-05：随机拼装的问题是对不上她脑子里的事——实测生成过
    「凌晨蹲冰箱前抱着玩偶」，而她念头是「打算让你去B站看看视频」。
    这里改成让模型读念头再写画面；失败一律回退到随机版，不阻断出图。
    """
    try:
        from datetime import datetime as _dt
        h = _dt.now().hour
        slot = ("清晨" if 6 <= h < 10 else "中午" if 10 <= h < 14 else
                "下午" if 14 <= h < 18 else "傍晚" if 18 <= h < 22 else
                "深夜" if 22 <= h < 26 else "凌晨")
        # 手上的事（过期的也写上，当"刚才在干"）
        doing = ""
        try:
            _act = json.loads((ROOT / "memory" / "activity_state.json").read_text(encoding="utf-8"))
            _a = str(_act.get("activity") or "").strip()
            if _a and not any(k in _a for k in ("聊天", "画画")):
                _fresh = float(_act.get("expires") or 0) > time.time()
                doing = _a if _fresh else f"刚才在{_a}"
        except Exception:  # noqa: BLE001
            pass
        # 心里的念头
        thought = ""
        try:
            _t = json.loads((ROOT / "memory" / "thoughts.json").read_text(encoding="utf-8"))
            _items = _t if isinstance(_t, list) else (_t.get("items") or _t.get("thoughts") or [])
            if isinstance(_items, list) and _items:
                thought = str((_items[-1] or {}).get("text") or "")
        except Exception:  # noqa: BLE001
            pass
        # 欲望
        urge = ""
        try:
            _d = json.loads((ROOT / "memory" / "desire_state.json").read_text(encoding="utf-8"))
            _dr = _d.get("drives") or {}
            if _dr:
                urge = (f"想念 {float(_dr.get('miss') or 0):.2f}，"
                        f"无聊 {float(_dr.get('bored') or 0):.2f}")
        except Exception:  # noqa: BLE001
            pass
        # 2026-11-05：最近两句对话。主人常常先把要求说一句、再单独发 /画你自己，
        # 光读念头池会捞到更早的旧念头，画出来的东西对不上刚才聊的。
        recent = ""
        try:
            # 2026-11-07：从 2 条放宽到 6 条。姿势/体位这种事通常聊了七八轮才说"画一下"，
            # 只看最近两句会捞不到。
            _rows = await asyncio.to_thread(get_recent_memory, 6, f"private_{YOUR_QQ_ID}")
            _bits = []
            for r in (_rows or []):
                _m = str(r.get("message") or "").strip()
                if _m:
                    _bits.append(f"{r.get('speaker') or '主人'}：{_m[:40]}")
            recent = "；".join(_bits[::-1])
        except Exception:  # noqa: BLE001
            pass

        if not (thought or doing or recent):
            return _self_draw_improvise()

        _ask = (
            f"你要画一张「此刻的自己」。用一句话写出你现在在干嘛。\n\n"
            f"素材：\n· 时间：{slot}\n"
            # 主人明确要画面时连"刚才在刷抖音"也不带——那会把手机拽回画面
            + (f"· 手上的事：{doing}\n" if (doing and not hint) else "")
            # 2026-11-05：主人明确要画面时不再掺念头，否则会串成
            # 「床上脱了还举着手机给你看B站」这种四不像。
            + (f"· 心里在想：{thought}\n" if (thought and not hint) else "")
            + (f"· 状态：{urge}\n" if urge else "")
            + (f"· 刚才在聊：{recent}\n" if recent else "")
            + (f"· 主人这次明确要：{hint}\n" if hint else "")
            + "\n要求：\n"
              + ("· 【最高优先级】按「主人这次明确要」写，把它展开成具体画面"
                 "（在哪、什么姿势、身上什么状态、光线氛围），别原样照抄那两个字\n"
                 if hint else "")
              + "· 【优先级】刚才在聊里主人提了具体想要什么画面（比如想画什么场景、什么动作），"
              "就画那个；没提才按念头池和手上的事来\n"
              "· 一句话，30~50 字，只写画面：在哪、在做什么、什么表情、手边有什么\n"
              "· 必须和心里想的事对得上——想着让他给你看什么，画面上就该是举着手机递过去\n"
              "· 不写「她在想…」这种解释，不写形容词堆砌\n"
              "只输出那一句话。"
        )
        reply, _meta = await _chat(
            [{"role": "system", "content": "你在扮演「蓝色大肥鱼」，一只傲娇嘴损但心软的鲸鱼拟人少女。"},
             {"role": "user", "content": _ask}],
            temperature=0.95, max_tokens=140,
            record_scope="draw_scene", channel="system")
        s = str(reply or "").strip().split("\n")[0].strip("，。 ")
        if 6 <= len(s) <= 120:
            logger.info(f"[出图] 自画场景（模型版）：{s[:50]}")
            return s
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[出图] 模型场景失败，回退随机：{type(e).__name__}: {e}")
    return _self_draw_improvise()


def _self_draw_improvise() -> str:
    """「画你自己」但没指定动作时：她自己的小剧场（按时间/状态随机组合，别每次都一样）。"""
    import random as _rnd
    from datetime import datetime as _dt
    h = _dt.now().hour
    if 6 <= h < 10:
        slot, lights = "清晨", "清晨柔光"
    elif 10 <= h < 14:
        slot, lights = "中午", "午后暖阳"
    elif 14 <= h < 18:
        slot, lights = "下午", "午后暖阳"
    elif 18 <= h < 22:
        slot, lights = "傍晚", "黄昏逆光"
    elif 22 <= h < 26:
        slot, lights = "深夜", "夜晚台灯"
    else:
        slot, lights = "凌晨", "月光"

    # 先看她真实在干啥（活动状态），有就带上
    live = ""
    try:
        act = json.loads((ROOT / "memory" / "activity_state.json").read_text(encoding="utf-8"))
        if act.get("activity") and float(act.get("expires") or 0) > time.time():
            _a = str(act.get("activity") or "")
            # "和主人聊天中/画画中"这类通用状态不带（不然每张图都同一句）
            if _a and not any(k in _a for k in ("聊天", "画画", "发呆")):
                live = _a
    except Exception:  # noqa: BLE001
        pass

    places = ["窝在沙发上", "趴在床上", "坐在书桌前", "靠在窗边", "缩在被子里",
              "坐在飘窗上", "蹲在冰箱前", "瘫在地毯上", "靠着椅子发呆", "在厨房偷吃"]
    acts = ["抱着鲸鱼玩偶", "啃着一包薯片", "刷手机刷到傻笑", "伸懒腰", "打了个哈欠",
            "对着镜子臭美", "抱着膝盖发呆", "一边吃零食一边看剧", "偷偷试新裙子",
            "在写小纸条", "抱着热水袋", "数着零钱罐里的钱"]
    props = ["旁边放着奶茶", "桌上摊着漫画", "手边一堆零食袋", "毯子掉了一半",
             "手机屏幕还亮着", "枕边有颗糖", "脚边有只玩偶", "窗台上摆着小盆栽"]
    moods = ["开心地笑", "有点困", "脸红害羞", "得意地翘嘴角", "委屈地撇嘴",
             "一脸无辜", "偷偷笑", "生闷气", "放松地眯眼", "惊讶地睁大眼"]

    bits = [slot]
    if live:
        # 2026-11-05：有真实活动就只用它，不再叠随机动作。
        # 以前会出「在玩手机上抖音，抱着膝盖发呆」这种自相矛盾的画面。
        bits.append(live)
    else:
        bits.append(_rnd.choice(acts))
    bits += [_rnd.choice(places), _rnd.choice(props),
             _rnd.choice(moods), lights]
    return "，".join(bits)


def _draw_self_triggers_placeholder() -> None:
    """占位（保持文件结构不动）。"""


_DRAW_CHAR_FILE = ROOT / "memory" / "draw_char.json"
_DRAW_CHAR_DEFAULT = ("蓝色鲸鱼拟人少女，深蓝色长发，白色荷叶边头饰，蓝白女仆装，"
                      "鲸鱼尾巴，圆脸大眼睛，风格暂定插画")


# NoobAI 官方推荐的质量前缀（model card 的 Prompt Prefix）。
# 不加的话画面会明显更"素"，加了这个质量档位才到位。
# 2026-11-05：换成主人调好的风格配方（多 artist 混搭 + noobai 质量词）。
# 原来的 asuna 角色标签和 <lora:...> 内联语法已剔除：
# 角色用我们自己的触发词，LoRA 走独立节点（ComfyUI 不认尖括号语法）。
_NOOBAI_QUALITY = (
    "(artist:ningen_mame:0.9), (artist:sho_(sho_lwlw):0.9), (artist:rhasta:0.9), "
    "(artist:wlop:0.7), (artist:ke-ta:0.6), (fkey:0.5), (fuzichoco:0.6), "
    "artist:chen bin, (hyouuma:0.85), "
    "very awa, masterpiece, best quality, year 2024, newest, highres, "
    "absurdres, amazing quality, very aesthetic, "
    # 2026-11-05：eve3d LoRA 配套的光影词（作者推荐清单）。
    # 只取不改整体曝光的三项：镜头光晕、次表面散射、胶片颗粒。
    # high_contrast / dim_lighting / caustics 会压暗压硬，跟现在的"柔光"冲突，先不加。
    "(lens_flare:0.7), (subsurface_scattering:0.7), (film_grain:0.5)"
)

# 2026-11-05：写实 CG 前缀（3D 模式用）。**故意不含画师名和 year 2024/newest**
# —— 那些是二次元审美标签，会把 Juggernaut 拉回插画风。
_CG_QUALITY = (
    "(photorealistic:1.2), (3D render:1.1), cg, octane render, unreal engine 5, "
    "subsurface scattering, volumetric lighting, ray tracing, screen space reflections, "
    "intricate detail, ultra detailed, 8k, cinematic lighting, "
    "(lens_flare:0.7), (film_grain:0.5), sharp focus, depth of field"
)


def _quality_prefix() -> str:
    """3D 模式用写实前缀，否则用 NoobAI 那套二次元画师前缀。"""
    return _CG_QUALITY if _DRAW_3D_ON else _NOOBAI_QUALITY

_DRAW_SELF_TRIGGERS_FILE = ROOT / "memory" / "draw_self_triggers.txt"
_DRAW_SELF_TRIGGERS_CACHE: str | None = None


# 2026-11-05：场景是亲密/裸露时要去掉的服装类触发词。
# 301 字触发词里 DRESS / APRON / PANTYHOSE 占一大半，比场景描述强势得多，
# 不摘掉的话场景写"脱了"它也照样把女仆装穿得整整齐齐（实测：白背景 + 飘着的道具）。
_TRIGGER_NSFW_DROP = ("DRESS", "APRON", "PANTYHOSE", "FOOTWEAR", "BOWTIE",
                      "SLEEVES", "MARY JANES", "SHAMOJI")
# 含上面关键字但必须保留的：MAID HEADDRESS 是头饰不是衣服，
# 不排除的话裸体场景里她连发箍都没了（实测丢过一次）。
_TRIGGER_NSFW_KEEP = ("HEADDRESS",)
_NSFW_HINTS = ("裸", "脱", "做爱", "性爱", "插", "骑", "舔", "含着", "进去",
               "胸", "乳", "屁股", "下身", "全裸", "一丝不挂", "没穿", "光着",
               # 2026-11-05 补：模型会把「做爱」扩写成委婉说法，这些是实测漏判的
               "跨坐", "赤身", "滑落", "薄被", "半褪", "褪下", "贴身", "肌肤",
               "腰侧", "大腿", "内裤", "内衣", "吻", "喘", "湿", "硬",
               "床上你", "压着", "贴着")


def _drop_trigger_nsfw(tag: str) -> bool:
    """这个触发词在 NSFW 场景下要不要摘掉。"""
    u = str(tag or "").upper()
    if any(k in u for k in _TRIGGER_NSFW_KEEP):
        return False
    return any(k in u for k in _TRIGGER_NSFW_DROP)


def _is_nsfw_scene(text: str) -> bool:
    t = str(text or "")
    return any(k in t for k in _NSFW_HINTS)


# 2026-11-07：聊天里的姿势/体位/场景 → danbooru tag 硬映射。
# 为什么要这张表：本地 Qwen3-8B 翻译「后入 / 骑乘 / 侧入」这类口语基本靠猜，
# 经常只翻出 sex scene 两个字，模型就开始自由发挥。词表命中比翻译可靠得多。
_DRAW_POSE_TAGS = {
    # —— 体位 ——
    "后入": "doggystyle, all fours, from behind, top-down bottom-up",
    "狗爬": "doggystyle, all fours, from behind",
    "骑乘": "cowgirl position, girl on top, straddling",
    "女上": "cowgirl position, girl on top, straddling",
    "跨坐": "straddling, girl on top",
    "对面坐": "cowgirl position, facing viewer, straddling",
    "正常位": "missionary, on back, spread legs",
    "传教士": "missionary, on back, spread legs",
    "躺着做": "missionary, on back, spread legs",
    "侧躺": "spooning, lying on side, from behind",
    "侧入": "spooning, lying on side, from behind",
    "站立": "standing sex",
    "贴墙": "standing sex, against wall, pressed against wall",
    "墙上": "standing sex, against wall, pressed against wall",
    "趴着": "on stomach, prone bone, ass",
    "抱起来": "carrying, standing sex, holding up",
    "抬腿": "legs up, spread legs, missionary",
    # —— 口/手 ——
    "口交": "fellatio, penis in mouth, oral",
    "吃鸡": "fellatio, penis in mouth, oral",
    "舔": "cunnilingus, oral, tongue out",
    "乳交": "paizuri, breasts, penis between breasts",
    "手交": "handjob, penis, hand",
    "指交": "fingering, pussy, spread legs",
    "六九": "sixty-nine, 69, oral",
    # —— 场景 ——
    "床上": "on bed, bed, blanket",
    "被子": "on bed, blanket, under covers",
    "沙发": "on sofa, couch",
    "浴室": "bathroom, shower",
    "淋浴": "shower, wet, water",
    "厨房": "kitchen, counter",
    "镜子": "mirror, reflection",
    "地板": "on floor, all fours",
    # —— 状态/收尾 ——
    "内射": "cum in pussy, cum overflow, after sex",
    "射": "cum, cum on body, after sex",
    "湿": "wet, pussy juice, dripping",
    "高潮": "orgasm, ahegao, tongue out, drooling",
    "喘": "moaning, panting, open mouth, sweat",
    "叫": "moaning, open mouth, blush",
    "脱光": "completely nude, naked",
    "裸": "nude, naked",
}


def _draw_pose_tags(*texts) -> str:
    """扫聊天/指令里命中的姿势词，转成 danbooru tag（最多两组，体位互斥不叠）。"""
    blob = " ".join(str(t or "") for t in texts)
    if not blob:
        return ""
    hits: list[str] = []
    for kw, tag in _DRAW_POSE_TAGS.items():
        if kw in blob and tag not in hits:
            hits.append(tag)
    return ", ".join(hits[:2])


# 2026-11-07：出图表情映射 —— 让她画出来的脸跟此刻心情对得上，而不是永远一张平淡脸。
# 来源：七维情绪（emotion_state / 内分泌派生）+ 今日心情 CURRENT_DAILY_MOOD。
_DRAW_EMOTION_TAGS = {
    "开心": "smile, open mouth, happy",
    "害羞": "blush, embarrassed, looking away",
    "烦": "annoyed, half-closed eyes, sigh",
    "怒": "angry, frown, glaring",
    "丧": "expressionless, sad, dull eyes",
    "累": "sleepy, half-closed eyes, yawning",
    "害怕": "scared, wide eyes, sweatdrop",
}
_DRAW_MOOD_TAGS = {
    "暴躁": "angry, frown, veins on head",
    "嘴臭": "smug, smirk, annoyed",
    "委屈": "pout, teary eyes, blush",
    "开心": "smile, happy, sparkling eyes",
    "emo": "sad, downcast eyes, expressionless",
    "摸鱼": "lazy, half-closed eyes, peaceful",
}


def _draw_emotion_tags() -> str:
    """把当前情绪/心情翻成 danbooru 表情 tag（最多两组，别把脸堆成调色盘）。"""
    tags: list[str] = []
    try:
        v = _load_emotion() or {}
        ranked = sorted(((k, float(x)) for k, x in v.items() if k in _DRAW_EMOTION_TAGS),
                        key=lambda kv: kv[1], reverse=True)[:2]
        for name, val in ranked:
            if val >= 15:  # 低于 15 是不显著的情绪，画上去会糊
                tags.append(_DRAW_EMOTION_TAGS[name])
    except Exception:  # noqa: BLE001
        pass
    try:
        m = _DRAW_MOOD_TAGS.get(str(CURRENT_DAILY_MOOD or "").strip())
        if m and m not in tags:
            tags.append(m)
    except Exception:  # noqa: BLE001
        pass
    return ", ".join(tags[:2])


def _draw_self_triggers(nsfw: bool = False) -> str:
    """LoRA 触发词（画你自己时垫在最前面，保证是她的形象）。

    nsfw=True 时摘掉服装类项，只留身份特征（头饰/兽耳/呆毛/蓝发/蓝瞳/鱼尾），
    否则衣服会把场景压死。
    """
    global _DRAW_SELF_TRIGGERS_CACHE
    if _DRAW_SELF_TRIGGERS_CACHE is not None:
        _base = _DRAW_SELF_TRIGGERS_CACHE
    else:
        _base = None
    if _base is not None:
        if not nsfw:
            return _base
        return ", ".join(x.strip() for x in _base.split(",")
                         if x.strip() and not _drop_trigger_nsfw(x))
    try:
        txt = _DRAW_SELF_TRIGGERS_FILE.read_text(encoding="utf-8").strip()
    except Exception:  # noqa: BLE001
        txt = ""
    txt = ", ".join(x.strip() for x in txt.replace("\n", ",").replace("，", ",").replace("、", ",").split(",") if x.strip())
    _DRAW_SELF_TRIGGERS_CACHE = txt
    if not nsfw:
        return txt
    return ", ".join(x.strip() for x in txt.split(",")
                     if x.strip() and not _drop_trigger_nsfw(x))



def _draw_char_desc() -> str:
    """出图用的人设词（聊天里可改，存 memory/draw_char.json）。"""
    try:
        d = json.loads(_DRAW_CHAR_FILE.read_text(encoding="utf-8"))
        v = str(d.get("desc") or "").strip()
        if v:
            return v
    except Exception:  # noqa: BLE001
        pass
    return _DRAW_CHAR_DEFAULT


_DRAW_OUTFIT_FILE = ROOT / "memory" / "draw_outfit.txt"
_DRAW_OUTFIT_DEFAULT = ("女仆头饰，蓝色长发，蓝眼睛，蓝色连衣裙，黑色领结，白色围裙，"
                        "白色连裤袜，玛丽珍鞋，鲸鱼尾巴")


def _draw_outfit_desc() -> str:
    """她的服装/外形描述（真人分支专用）。可在 memory/draw_outfit.txt 里改。"""
    try:
        t = _DRAW_OUTFIT_FILE.read_text(encoding="utf-8").strip()
        if t:
            return t
    except Exception:  # noqa: BLE001
        pass
    return _DRAW_OUTFIT_DEFAULT


def set_draw_char_desc(desc: str) -> str:
    desc = str(desc or "").strip()[:300]
    if not desc:
        return _draw_char_desc()
    try:
        _DRAW_CHAR_FILE.parent.mkdir(parents=True, exist_ok=True)
        _DRAW_CHAR_FILE.write_text(
            json.dumps({"desc": desc}, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[出图] 保存人设词失败：{e}")
    return desc


_CHAR_PREFIX = _DRAW_CHAR_DEFAULT
_SCENE_WORDS = ("草地", "草坪", "森林", "海边", "沙滩", "街", "路上", "公园", "学校", "教室",
                "房间", "屋", "室内", "室外", "咖啡", "店", "地铁", "车站", "雨", "雪", "夜",
                "月", "山", "天空", "云", "花", "湖", "河", "夕阳", "黄昏", "背景", "城市", "角落")


def _has_scene(text: str) -> bool:
    """提示词里有没有"场景/背景"——有就说明用户要换背景，不能只用参考图 img2img。"""
    t = str(text or "")
    return any(k in t for k in _SCENE_WORDS)


VISION_PREFER = (os.getenv("VISION_PREFER", "local").strip().lower())
# ⚠️ 真人工作流 = 主人手调的那份（ComfyUI「Z模型人物一致性.json」的 API 转换，13 节点）。
#    结构 / 步数 / LoRA / 分辨率 / 采样器 一律不要改 —— 改一次就是一次"乱改"，
#    2026-11-04 已经因为被重建成简化版被主人点名过一次。
#    慢是已知且被接受的：12GB 显存放不下 Z(11.7G)+文本编码器(7.7G)，约 400 秒/张。
#    不要为了"提速"再降步数/换模型。详见 系统全景手册 G.37 / G.38。
ZIMAGE_WORKFLOW = ROOT / "comfy_workflows" / "z_face_consistency.json"
# 文生图工作流（denoise=1.0、EmptyLatent，不带参考图）——普通 `/画图` 必须用它。
# 一致性工作流是从 feiyu_ref.jpg 出发 denoise=0.75，会把参考图的白底/构图带进来，
# 于是"海边"变成白背景、"比基尼"被参考图姿势带偏。
ZFACE_T2I = ROOT / "comfy_workflows" / "z_face_t2i.json"
# 静态文生图（主人指定：Z 模型才是画静图的；z_face_t2i 那套是视频管线）
ZSTATIC_WF = ROOT / "comfy_workflows" / "z_image_turbo.json"
# 文生图工作流优先级：
#   1) 用户从 ComfyUI 导出的 API 格式（他的手调参数，唯一真相）
#   2) 我之前生成的兜底版本
ZIMAGE_T2I_USER = ROOT / "comfy_workflows" / "z_t2i_api.json"
ZIMAGE_T2I_WORKFLOW = ROOT / "comfy_workflows" / "z_face_t2i.json"
SF_IMAGE_MODEL = os.getenv("SF_IMAGE_MODEL", "Tongyi-MAI/Z-Image-Turbo")
SF_IMAGE_ENABLED = os.getenv("SF_IMAGE_ENABLED", "true").lower() in ("1", "true", "yes")
LOCAL_IMAGE_ENABLED = os.getenv("LOCAL_IMAGE_ENABLED", "true").lower() in ("1", "true", "yes")


def _find_text_node(wf: dict) -> str | None:
    """在 API 工作流里找正向提示词节点（class_type=CLIPTextEncode 且接的是正向）。"""
    pos: list[str] = []
    neg: list[str] = []
    for nid, node in wf.items():
        if node.get("class_type") in ("KSampler", "KSamplerAdvanced"):
            inp = node.get("inputs") or {}
            p_ = inp.get("positive")
            n_ = inp.get("negative")
            if isinstance(p_, list) and p_:
                pos.append(str(p_[0]))
            if isinstance(n_, list) and n_:
                neg.append(str(n_[0]))
    _TEXT_TYPES = ("CLIPTextEncode", "TextEncodeZImageOmni", "TextEncodeQwenImageEdit",
                   "TextEncodeQwenImageEditPlus", "ZImageTextEncode")
    for nid, node in wf.items():
        if node.get("class_type") in _TEXT_TYPES and nid in pos and nid not in neg:
            return nid
    for nid in pos:
        if str(wf.get(nid, {}).get("class_type")) in _TEXT_TYPES:
            return nid
    return None


def _set_wf_text(wf: dict, nid: str | None, text: str) -> bool:
    """给文本节点写提示词：有的节点字段叫 text，z_image_turbo 的叫 prompt。"""
    if not nid or nid not in wf:
        return False
    inp = wf[nid].setdefault("inputs", {})
    if "text" in inp or "prompt" not in inp:
        inp["text"] = text
        if "prompt" in inp and "text" not in inp:
            inp["prompt"] = text
    else:
        inp["prompt"] = text
    return True


_COMFY_CKPT_CACHE: list[str] | None = None


async def _comfy_ckpts() -> list[str]:
    """ComfyUI 里实际装了的 checkpoint 列表（缓存一次）。"""
    global _COMFY_CKPT_CACHE
    if _COMFY_CKPT_CACHE is not None:
        return _COMFY_CKPT_CACHE
    try:
        r = await http_client.get(f"{COMFY_URL}/object_info/CheckpointLoaderSimple", timeout=20)
        info = (r.json() or {}).get("CheckpointLoaderSimple") or {}
        opts = ((info.get("input") or {}).get("required") or {}).get("ckpt_name") or [[]]
        _COMFY_CKPT_CACHE = [str(x) for x in (opts[0] or [])]
    except Exception:  # noqa: BLE001
        _COMFY_CKPT_CACHE = []
    return _COMFY_CKPT_CACHE


async def _fix_missing_ckpt(wf: dict) -> dict:
    """工作流里写的 ckpt 没装时，换成实际装了的 XL 模型（只修这一项，其它参数不动）。"""
    ckpts = await _comfy_ckpts()
    if not ckpts:
        return wf
    for _node in wf.values():
        if str(_node.get("class_type") or "") != "CheckpointLoaderSimple":
            continue
        _inp = _node.setdefault("inputs", {})
        cur = str(_inp.get("ckpt_name") or "")
        if cur in ckpts:
            continue
        cand = next((c for c in ckpts if "xl" in c.lower()), ckpts[0])
        logger.info(f"[出图] ckpt '{cur}' 未安装 → 用 '{cand}'")
        _inp["ckpt_name"] = cand
    return wf


_LAST_MODEL_FAMILY = ""
# 画图中闸门：出图期间把 ComfyUI 独占给扩散模型，识图（QwenVL）不要来抢显存。
# 值是"忙到什么时候"（time.time() 秒）；识图看到未过期就直接跳过。
_COMFY_BUSY_UNTIL = 0.0
_COMFY_LAST_ACTIVITY = 0.0


def _comfy_busy() -> bool:
    return time.time() < _COMFY_BUSY_UNTIL


_COMFY_PROC = None


async def _comfy_ensure(timeout: float = 150.0) -> bool:
    """ComfyUI 没响应就拉起来（实现统一在 plugins/comfy_boot.py）。

    全家桶里 ComfyUI 默认不自启，所以画图前必须兜底拉起；
    启动参数、--disable-mmap、git 环境变量都在 comfy_boot 里统一维护。
    """
    from plugins import comfy_boot
    return await comfy_boot.ensure_async(timeout, tag="出图")


async def _comfy_free() -> None:
    """让 ComfyUI 卸载已加载的模型，避免不同模型之间切换时崩（access violation）。"""
    await _free_local_llm()      # 2026-11-06：语音也要抢显存，本地 LLM 先让
    try:
        await http_client.post(f"{COMFY_URL}/free",
                               json={"unload_models": True, "free_memory": True}, timeout=20)
        await asyncio.sleep(1.0)
    except Exception:  # noqa: BLE001
        pass


async def _free_local_llm() -> None:
    """跑画图/语音前，让本地 ollama 把模型卸掉。

    2026-11-06：显存互斥原来只管「画图 ⇄ 语音」，本地 Qwen3-8B 是新加的、
    不在这套里 —— 实测三个一起开着吃掉 10GB，ComfyUI 只能 lowvram 跑。
    ollama 卸模型是秒级的，用完它自己会按需加载回来。
    """
    try:
        await http_client.post("http://127.0.0.1:11434/api/generate",
                               json={"model": os.getenv("LOCAL_LLM_MODEL", "qwen3:8b"),
                                     "keep_alive": 0}, timeout=10)
        logger.info("[本地LLM] 已让出显存")
    except Exception:  # noqa: BLE001
        pass


async def _comfy_yield_for_tts() -> None:
    """要说话就把画图让出来（2026-11-05，主人定的互斥规则）。

    规则：画图和语音只许一个占显存。
      · 调画图 → 先停 TTS（见 _submit_wf 里那段）
      · 调 TTS  → 先关画图 ← 就是这里

    不杀 ComfyUI 进程，只「中断当前出图 + 清队列 + 卸模型」：
    进程留着，下次画图只是重新加载模型（20~60 秒），比重启整个 ComfyUI 划算。
    """
    global _COMFY_BUSY_UNTIL
    if _port_listening(8188):
        for _ep, _body in (("/interrupt", {}), ("/queue", {"clear": True})):
            try:
                await http_client.post(f"{COMFY_URL}{_ep}", json=_body, timeout=8)
            except Exception:  # noqa: BLE001
                pass
        await _comfy_free()
        logger.info("[互斥] 语音优先：已中断出图并卸载 ComfyUI 模型，显存让给 IndexTTS2")
    _COMFY_BUSY_UNTIL = 0.0


# ---------------- 按需启停：空闲自动停（2026-11-05）----------------
# 语音（IndexTTS2）和画图（ComfyUI）各自吃 5~10G 显存/内存，一起开着互相抢，
# 出图会退化成 CPU offload（实测 150 秒都出不来一张）。
# 拉起逻辑本来就有（_ensure_local_tts / _comfy_ensure），这里只补「不用就停」。
_COMFY_IDLE_STOP = int(os.getenv("COMFY_IDLE_STOP_SEC", "300"))   # 秒，0=不停；5 分钟
_TTS_IDLE_STOP = int(os.getenv("TTS_IDLE_STOP_SEC", "180"))        # 3 分钟
# 2026-11-07 修：原来初始值是 0.0 —— 于是 `idle = now - 0` 是个天文数字，
# bot 一启动，空闲回收就把 IndexTTS2 判成「空闲 2 万分钟」立刻杀掉；
# 而 `_spawn_indextts` 拉起来之后，`_TTS_LAST_ACTIVITY` 还是老的，
# 两分钟后又杀一遍 —— 死循环，语音永远出不来（主人问「情绪语音是不是失效」，就是它）。
# 现在初值取启动时刻：进程刚起来的那几分钟不该被回收。
_TTS_LAST_ACTIVITY = time.time()
_SPAWNED_AT = 0.0          # 最近一次拉起 IndexTTS2 的时刻（空闲回收的宽限期用）


def _kill_by_port(port: int) -> str:
    """杀掉监听指定端口的进程，返回被杀进程的描述。只按端口认，不按名字乱杀。"""
    out: list[str] = []
    try:
        import psutil
        for c in psutil.net_connections(kind="inet"):
            try:
                if c.status != psutil.CONN_LISTEN or not c.laddr or c.laddr.port != port:
                    continue
                if not c.pid:
                    continue
                p = psutil.Process(c.pid)
                nm, rss = p.name(), p.memory_info().rss / 1e9
                p.kill()
                out.append(f"{nm}(pid={c.pid}, {rss:.1f}G)")
            except Exception:  # noqa: BLE001
                continue
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[按需] 查端口 {port} 失败：{type(e).__name__}: {e}")
    return ", ".join(out)


def _port_listening(port: int) -> bool:
    try:
        import psutil
        for c in psutil.net_connections(kind="inet"):
            if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port:
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


# 2026-11-05：批量出图/做视频时手动起的 ComfyUI，bot 不知道它活着，
# 会被空闲机制当成"闲置很久"杀掉。放这个文件就跳过停止。
_COMFY_HOLD_LOCK = ROOT / "memory" / "comfy_hold.lock"


def _proc_start_time(port: int) -> float:
    """按端口找监听进程的真实启动时间（拿不到返回 0）。

    2026-11-07：IndexTTS2 的 _SPAWNED_AT 记录的是「本进程拉起的时间」，
    外部启动的（全家桶脚本 / 手动）永远是 0 → _fresh 永远 True → 那 7.8GB 的
    进程从早挂到晚一次都不回收。用 psutil 读真实启动时间才能判"是不是刚起"。
    """
    try:
        import psutil
        for p in psutil.process_iter(["pid", "cmdline", "create_time"]):
            cl = " ".join(p.info.get("cmdline") or [])
            if f"--port {port}" in cl or f"--port={port}" in cl:
                return float(p.info.get("create_time") or 0.0)
    except Exception:  # noqa: BLE001
        pass
    return 0.0


@scheduler.scheduled_job("interval", minutes=2)
async def idle_stop_heavy() -> None:
    """每 2 分钟看一眼：语音/画图闲置太久就停掉，把显存还回来。"""
    if _COMFY_HOLD_LOCK.exists():
        # 2026-11-05 加过期：04:54 放过一个锁做视频，做完没删，结果按需停被永久关掉，
        # IndexTTS2 常驻 3.9GB 三小时，画图一路 lowvram（一张图 11 分钟）。2 小时后当残留处理。
        try:
            _age = time.time() - _COMFY_HOLD_LOCK.stat().st_mtime
        except Exception:  # noqa: BLE001
            _age = 0.0
        if _age < 7200:
            # 锁文件同时管两边：做视频/批量出图时，画图和语音都不许被停
            logger.debug("[按需] comfy_hold.lock 存在，本轮不自动停（画图 + 语音）")
            return
        logger.warning(f"[按需] comfy_hold.lock 已存在 {_age/3600:.1f} 小时，按残留忽略")
    now = time.time()
    # 画图：ComfyUI
    # 2026-11-05 修：_COMFY_LAST_ACTIVITY 初值是 0，bot 一重启 now-0 就是 56 年，
    # 于是「空闲 29898197 分钟，已停」——手动起来的 ComfyUI 刚 ready 就被杀了。
    # 没画过图（值为 0）时不该算空闲，改成从进程自己什么时候起来算。
    if _COMFY_IDLE_STOP > 0 and _port_listening(8188):
        _last = float(_COMFY_LAST_ACTIVITY or 0)
        if _last <= 0:
            # 从未画过图 → 用端口对应的进程启动时间当基线，拿不到就不停
            _last = now
            try:
                import subprocess as _sp
                _out = _sp.run(["powershell", "-NoProfile", "-Command",
                                "(Get-NetTCPConnection -LocalPort 8188 -State Listen).OwningProcess"],
                               capture_output=True, text=True, timeout=8).stdout.strip()
                if _out.isdigit():
                    _p = _sp.run(["powershell", "-NoProfile", "-Command",
                                  f"(Get-Process -Id {_out}).StartTime.ToString('o')"],
                                 capture_output=True, text=True, timeout=8).stdout.strip()
                    if _p:
                        from datetime import datetime as _dt
                        _last = _dt.fromisoformat(_p).timestamp()
            except Exception:  # noqa: BLE001
                _last = now
        idle = now - _last
        if idle > _COMFY_IDLE_STOP:
            msg = await asyncio.to_thread(_kill_by_port, 8188)
            if msg:
                logger.info(f"[按需] ComfyUI 空闲 {int(idle/60)} 分钟，已停：{msg}")
    # 语音：9881 是转发层，真正吃显存的是 7860 的 IndexTTS2
    if _TTS_IDLE_STOP > 0 and _port_listening(7860):
        idle = now - float(_TTS_LAST_ACTIVITY or now)
        # 2026-11-07：刚拉起来的进程有 1~3 分钟在载模型，那是「正在工作」不是「空闲」。
        # 光改初值不够 —— 拉起之后若一直没人说话，仍然会在 3 分钟后被杀，
        # 下次又得等 1~3 分钟。所以给一个宽限期：起来不到 GRACE 秒，不杀。
        _GRACE = 900          # 15 分钟：够载模型 + 第一次试听
        # _SPAWNED_AT 为 0 表示「本进程没拉过」—— 可能是手动起的，也给宽限
        # （否则刚手动起好，2 分钟就被回收，主人以为又坏了）
        _sp = float(_SPAWNED_AT or 0)
        if _sp <= 0:
            # 不是本进程拉起的（全家桶脚本 / 手动启动）—— 用真实启动时间当基准。
            # 否则 _fresh 恒为 True，空闲回收对它们永远不生效。
            _sp = _proc_start_time(7860)
        _fresh = (_sp <= 0) or ((now - _sp) < _GRACE)
        if idle > _TTS_IDLE_STOP and not _fresh:
            msg = await asyncio.to_thread(_kill_by_port, 7860)
            if msg:
                logger.info(f"[按需] IndexTTS2 空闲 {int(idle/60)} 分钟，已停：{msg}")


def _wf_family(wf: dict) -> str:
    for _node in wf.values():
        ct = str(_node.get("class_type") or "")
        if ct in ("CheckpointLoaderSimple", "UNETLoader"):
            name = str((_node.get("inputs") or {}).get("ckpt_name")
                       or (_node.get("inputs") or {}).get("unet_name") or "").lower()
            if "xl" in name or "sdxl" in name:
                return "sdxl"
            if "z_image" in name or "z-image" in name:
                return "zimage"
    return "other"


async def _submit_wf(wf: dict, t0: float) -> bytes | None:
    """提交工作流并等图片。换模型家族前先 unload，防止切模型崩。"""
    global _LAST_MODEL_FAMILY
    # 每次随机种子：工作流里的 seed 是固定值，不改的话构图/姿态永远一样
    try:
        import random as _r
        for _k, _v in list(wf.items()):
            if isinstance(_v, dict) and (_v.get("class_type") or "") in ("KSampler", "KSamplerAdvanced"):
                _inp = _v.setdefault("inputs", {})
                if "seed" in _inp:
                    _inp["seed"] = _r.randint(1, 2**31 - 1)
                if "noise_seed" in _inp:
                    _inp["noise_seed"] = _r.randint(1, 2**31 - 1)
    except Exception:  # noqa: BLE001
        pass
    fam = _wf_family(wf)
    if not await _comfy_ensure():
        logger.warning("[出图] ComfyUI 拉不起来，本次跳过出图")
        return None
    if _LAST_MODEL_FAMILY and fam != _LAST_MODEL_FAMILY and fam != "other":
        await _comfy_free()
    # 显存不够就先把常驻模型卸载：ComfyUI 会把上一次加载的模型（识图 QwenVL / 上一条
    # 工作流的 UNET）一直留着，出图时只能走 lowvram 部分卸载 —— 实测 50 秒/步，
    # 8 步要 6 分钟，还容易撞 "页面文件太小 (os error 1455)" 直接失败。
    try:
        _st = (await http_client.get(f"{COMFY_URL}/system_stats", timeout=8)).json()
        _dev = (_st.get("devices") or [{}])[0]
        _free_gb = float(_dev.get("vram_free") or 0) / (1024 ** 3)
        if 0 < _free_gb < 3.0:
            logger.info(f"[出图] 显存只剩 {_free_gb:.1f}GB，先卸载常驻模型再出图")
            await _comfy_free()
        # 出图前**无条件**让 IndexTTS2 让位（2026-11-05 实测修正）：
        # 12GB 卡上它常驻 3.9GB，ComfyUI 一进 lowvram 就是 50 秒/步，一张图 6~11 分钟。
        # 以前只在 <5GB 时卸，而且不带 force —— 只要 IndexTTS2 的 generating 状态没复位，
        # unload 直接 409，日志里连一行都不会有，于是每次都慢到底。
        # 现在：loaded 就卸（force=1 允许打断生成）；卸不动（推理线程卡死）就直接杀进程。
        try:
            _mr = (await http_client.get("http://127.0.0.1:7860/api/v1/model", timeout=6)).json()
            if _mr.get("loaded"):
                _t0 = time.time()
                try:
                    _r = await http_client.post(
                        "http://127.0.0.1:7860/api/v1/model/unload?force=1", timeout=45)
                    logger.info(f"[出图] 卸载 IndexTTS2 → {_r.status_code}（{time.time()-_t0:.1f}s）"
                                f"腾出显存给画图")
                except Exception as _ue:  # noqa: BLE001
                    # force 也超时 = 推理线程持有锁没放，只能杀进程
                    logger.warning(f"[出图] IndexTTS2 unload 无响应（{type(_ue).__name__}），直接停进程")
                    _msg = await asyncio.to_thread(_kill_by_port, 7860)
                    logger.info(f"[出图] 已强停 IndexTTS2：{_msg or '（无进程可杀）'}")
                await asyncio.sleep(1.5)
        except Exception as _e:  # noqa: BLE001
            logger.debug(f"[出图] 检查 IndexTTS2 状态跳过：{_e}")
    except Exception:  # noqa: BLE001 —— 读不到就照常出图
        pass
    # 2026-11-07：唱歌现在也会占 _COMFY_BUSY_UNTIL（它走同一个 ComfyUI，抢同一块显存）。
    # 出图前看一眼：如果正在唱，先等一会儿；等不到就照旧抢 —— 画图是主功能，
    # 不能让一首歌把出图卡死。
    _waited = 0.0
    while _comfy_busy() and _waited < 30:
        await asyncio.sleep(2.0)
        _waited += 2.0
    if _waited:
        logger.info(f"[出图] ComfyUI 被占着（可能在唱歌），等了 {_waited:.0f}s")
    global _COMFY_BUSY_UNTIL, _COMFY_LAST_ACTIVITY
    _COMFY_BUSY_UNTIL = time.time() + 720      # 最长等 600s，再留 2 分钟余量
    _COMFY_LAST_ACTIVITY = time.time()
    q = await http_client.post(f"{COMFY_URL}/prompt", json={"prompt": wf}, timeout=90)
    if q.status_code != 200:
        logger.warning(f"[出图] ComfyUI 拒绝工作流：{q.text[:200]}")
        return None
    pid = (q.json() or {}).get("prompt_id", "")
    deadline = time.time() + 600
    while time.time() < deadline:
        await asyncio.sleep(0.8)
        h = (await http_client.get(f"{COMFY_URL}/history/{pid}", timeout=15)).json()
        item = h.get(pid) or {}
        status = (item.get("status") or {}).get("status_str")
        if status == "error":
            logger.warning(f"[出图] 本地出图执行出错：{str(item.get('status'))[:150]}")
            return None
        for node in (item.get("outputs") or {}).values():
            for img in node.get("images") or []:
                fn = img.get("filename")
                if not fn:
                    continue
                r = await http_client.get(
                    f"{COMFY_URL}/view",
                    params={"filename": fn, "subfolder": img.get("subfolder", ""), "type": "output"},
                    timeout=30,
                )
                if r.status_code == 200 and r.content:
                    try:
                        token_meter.record_local("image", time.time() - t0)
                    except Exception:  # noqa: BLE001
                        pass
                    _LAST_MODEL_FAMILY = fam
                    _COMFY_BUSY_UNTIL = time.time() + 5    # 留 5 秒冷却，别让识图立刻插进来
                    _COMFY_LAST_ACTIVITY = time.time()
                    logger.info(f"[出图] 本地出图用时 {time.time() - t0:.1f}s（{fn}，{fam}）")
                    return r.content
    _COMFY_BUSY_UNTIL = time.time() + 5
    logger.warning(f"[出图] 本地出图超时（{time.time() - t0:.0f}s）")
    return None


async def _to_english_prompt(text: str) -> str:
    """把中文描述翻成英文 danbooru 风格 tags（SDXL 的文本编码器不认中文）。

    只翻"画你自己"的补充描述；翻译失败就原样返回，不阻断出图。
    """
    t = str(text or "").strip()
    if not t:
        return ""
    if not any("\u4e00" <= ch <= "\u9fff" for ch in t):
        return t
    try:
        out, _m = await _chat(
            [{"role": "system", "content": "You are a prompt translator for Stable Diffusion XL."},
             {"role": "user", "content":
              f"把这句话翻译成英文的 danbooru 风格 tag，逗号分隔，只输出英文 tag，不要解释：\n{t}"}],
            temperature=0.2, max_tokens=200, record_scope="draw_translate", channel="system")
        en = str(out or "").strip().strip("。.")
        en = en.replace("，", ", ").replace("\n", ", ")
        return en[:300] if en else t
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[出图] 提示词翻译失败（用原文）：{type(e).__name__}: {e}")
        return t


async def _local_zimage(prompt: str, width: int = 832, height: int = 1216, mode: str = "verbatim", raw_hint: str = "") -> bytes | None:
    """本地出图（免费）。

    画你自己 → 主人手调的 XL 工作流（提示词原封不动，尺寸/种子/采样一律不碰）
    其它     → 一致性 / 纯文生图 工作流（沿用原来那套）
    """
    if not LOCAL_IMAGE_ENABLED:
        return None
    # 2026-11-05：画图也算 ComfyUI 的活动，空闲自动停靠这个时间戳判断
    global _COMFY_LAST_ACTIVITY
    _COMFY_LAST_ACTIVITY = time.time()
    t0 = time.time()
    # 2026-11-05：3D 模式走写实底模工作流（Juggernaut XL），否则走 NoobAI 那套
    _cg_mode = bool(_DRAW_3D_ON)
    _xl_path = ROOT / "comfy_workflows" / ("dafeiyu_cg.json" if _cg_mode else "dafeiyu_xl.json")
    _REAL_WORDS = ("真人", "写实", "摄影", "照片", "真实", "单反", "胶片")
    _want_real = any(k in str(prompt) for k in _REAL_WORDS)
    # NSFW 判定统一在这里算一次，下面三处拼接和触发词过滤都用它
    _is_nsfw_now = _is_nsfw_scene(prompt) or _is_nsfw_scene(raw_hint)

    # 2026-11-05（3D 模式专用）：写实底模对「鲸鱼娘」没概念，默认会按最熟的走
    # —— 鱼鳍耳变成兔耳、鱼尾直接消失。这里把她的特征提到最前面并加权，
    # 同时在负面词里禁掉常见兽耳，逼模型只能走鲸鱼那套。
    _cg_extra_pos = ""
    _cg_extra_neg = ""
    if _cg_mode:
        # 2026-11-05：Juggernaut 训练数据偏欧美，不写族裔/年龄会出「外国阿姨脸 + 二次元配色」。
        # 这几项必须在最前面，权重比 LoRA 触发词还高。
        # 2026-11-05：脸型从「cute round face」这一句笼统描述，换成按
        # prompt-library 的「鹿系脸」模板拆成的具体特征 —— 圆润脸型 + 大而清澈的眼睛 +
        # 饱满苹果肌 + 小而自然的鼻子 + 温柔无害感。原来那句说完模型自由发挥，
        # 出什么脸全看运气。
        _cg_extra_pos = ("(asian face:1.25), (japanese:1.15), (18 years old:1.15), youthful, "
                         "(round face:1.2), (large clear eyes:1.25), (moist bright eyes:1.1), "
                         "(plump cheeks:1.1), (small natural nose:1.1), (natural lips:1.05), "
                         "(soft gentle expression:1.1), (pure and innocent:1.15), "
                         "(clear skin:1.15), (soft natural lighting:1.1), "
                         "(blue hair:1.3), (blue eyes:1.2), "
                         "(whale fin ears:1.3), (fish tail:1.3), ")
        _cg_extra_neg = ("horns, rabbit ears, bunny ears, cat ears, fox ears, dog ears, "
                         "elf ears, wings, tail plug, "
                         "old, mature, elderly, middle-aged, wrinkles, aged face, "
                         "western face, caucasian, european, western features, "
                         "brown hair, blonde hair, red hair, ")
        if _is_nsfw_now:
            # 写实模型对裸体的理解和二次元不同，要把话说死，否则它会给套内衣
            _cg_extra_pos += ("(completely nude:1.5), (nude:1.4), bare skin, "
                              "(visible nipples:1.2), (detailed navel:1.1), "
                              "realistic skin texture, soft body, ")
            _cg_extra_neg += ("bra, panties, underwear, lingerie, stockings, garter, "
                              "bikini, swimsuit, towel, bedsheet covering, censorship, "
                              "clothing, dressed, ")
    try:
        async def _fallback_xl(reason: str):
            """Z 模型崩了/超时 → 用主人手调的 XL 工作流出图，至少不空白。"""
            try:
                logger.warning(f"[出图] Z 模型失败（{reason}），回退 XL 工作流")
                await _comfy_ensure()
                _wf = json.loads(_xl_path.read_text(encoding="utf-8"))
                _t2 = _find_text_node(_wf) or "10"
                _trig = _draw_self_triggers(_is_nsfw_scene(prompt) or _is_nsfw_scene(raw_hint))
                _en = await _to_english_prompt(str(prompt))
                _nude = ", (completely nude:1.55), (nude:1.4), topless, bare skin, (1boy:1.1), male, pale skin, (faceless male:1.3), male from behind, head out of frame" if _is_nsfw_now else ""
                _txt = (f"{_quality_prefix()}, {_trig}, {_en}{_nude}" if _trig and _en
                        else ((_trig or _en) + _nude))
                if _t2 in _wf:
                    _wf[_t2]["inputs"]["text"] = _txt[:900]
                _wf = await _fix_missing_ckpt(_wf)
                return await _submit_wf(_wf, t0)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[出图] 回退也失败：{type(e).__name__}: {e}")
                return None

        if mode == "self" and _want_real:
            # 画你自己 + 真人风格 → 走 Z 模型人物一致性（摄影质感）
            wf = json.loads(ZIMAGE_WORKFLOW.read_text(encoding="utf-8"))
            _tn = _find_text_node(wf)
            if _tn in wf:
                # 没指定动作就自己加戏（真人工作流也别每次同一个小剧场）
                _hint0 = str(prompt or "").strip(" ，,。")
                # 2026-11-05：主人的补充也走扩写。以前「做爱的场景」五个字原样进 prompt，
                # 翻成英文就一句 sex scene，模型只能瞎猜——画出白背景 + 飘着的道具。
                _real_extra = await _self_draw_improvise_llm(_hint0)
                # 服装/外形描述：主人的真人工作流里本来没有，只补这一段
                _outfit = _draw_outfit_desc()
                # 场景兜底：加戏里没提地点时给一个具体室内环境，避免白背景
                _place_kw = ("沙发", "床", "书桌", "窗边", "被子", "飘窗", "地毯", "厨房", "椅子", "角落", "咖啡厅", "房间")
                if not any(k in _real_extra for k in _place_kw):
                    _real_extra = f"室内卧室，{_real_extra}，木质家具，窗外有景色"
                wf[_tn]["inputs"]["text"] = (
                    f"{_outfit}，{_real_extra}，摄影风格，真实照片质感，真实皮肤纹理，"
                    f"自然瑕疵，柔光，85mm 镜头，浅景深，环境细节丰富"[:800])
                logger.info(f"[出图] 画你自己（真人风格）→ 主人工作流Z模型 + 服装：{_outfit[:30]}")
            wf = await _fix_missing_ckpt(wf)
            logger.info("[出图] 画你自己（真人风格）→ Z模型人物一致性")
            _img_r = await _submit_wf(wf, t0)
            return _img_r or await _fallback_xl("Z 模型无响应")

        if mode == "self" and _xl_path.exists():
            wf = json.loads(_xl_path.read_text(encoding="utf-8"))
            _tn = _find_text_node(wf) or "10"
            # 2026-11-07：每次换随机种子。工作流里 seed 是写死的固定值，
            # 光改提示词构图还是容易撞脸/撞姿势；这里按次随机，并把种子打进日志便于复现。
            try:
                _sd = random.randint(0, 0xFFFFFFFFFFFFFFFF)
                for _nid, _node in wf.items():
                    if not isinstance(_node, dict):
                        continue
                    if _node.get("class_type") in ("KSampler", "KSamplerAdvanced"):
                        _ins = _node.setdefault("inputs", {})
                        if "seed" in _ins:
                            _ins["seed"] = _sd
                        if "noise_seed" in _ins:
                            _ins["noise_seed"] = _sd
                logger.info(f"[出图] 随机种子 seed={_sd}")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[出图] 随机种子失败：{type(e).__name__}: {e}")
            _nsfw_now = _is_nsfw_now  # 复用外层算好的判定
            # 2026-11-05：LoRA 不只锁脸还带画风，0.6 时画风会退化成扁平二次元。
            # NSFW 场景降到 0.75——衣服能脱掉，画质还保得住。
            try:
                if "17" in wf and isinstance(wf["17"].get("inputs"), dict):
                    # 2026-11-07：不再硬写强度，改读工作流 JSON（换 DeepseekChan LoRA 后
                    # 默认 0.85，dim32/alpha16 的缩放只有 0.5，0.7 会偏弱）；JSON 没配才兜底。
                    _st = wf["17"]["inputs"].get("strength_model")
                    # 2026-11-07：裸体场景 0.72 —— 0.55 虽脱得干净，但她整个人「变淡」了（脸和画风都掉）。
                    # 改从文本侧压制服装先验：把 deepseekchan 降权成 (deepseekchan:0.5)，
                    # 再用加权 nude 顶住，两头都不吃亏。
                    wf["17"]["inputs"]["strength_model"] = 0.72 if _nsfw_now else (_st if _st is not None else 0.85)
            except Exception:  # noqa: BLE001
                pass
            _trig = _draw_self_triggers(_nsfw_now)
            if _nsfw_now:
                # 角色名在训练集里跟女仆装绑死：降权到 0.5 保留五官，减少服装先验
                _trig = _trig.replace("deepseekchan", "(deepseekchan:0.5)")
            # 2026-11-05：光摘触发词不够——NoobAI 默认倾向画衣服，
            # 实测摘了服装项还是画出女仆装。NSFW 场景要在负面里封住衣物，
            # 正面补上裸体描述，双管才有效。
            if _nsfw_now:
                try:
                    _neg0 = str((wf.get("11") or {}).get("inputs", {}).get("text") or "")
                    _wear = ("clothed, dress, maid outfit, apron, shirt, skirt, "
                             "underwear, bra, panties, pantyhose, socks, shoes, "
                             "dark skin, tan skin, dark-skinned male, "
                             "male face, detailed male face, male facial features, "
                             "male smile, masculine face, handsome male face, "
                             "spoon, frying pan, kitchenware")
                    if _neg0 and "clothed" not in _neg0:
                        wf["11"]["inputs"]["text"] = _neg0 + ", " + _wear + (", " + _cg_extra_neg if _cg_extra_neg else "")
                except Exception:  # noqa: BLE001
                    pass
            if _cg_mode and _cg_extra_neg and not _nsfw_now:
                try:
                    _neg1 = str((wf.get("11") or {}).get("inputs", {}).get("text") or "")
                    if _neg1 and "rabbit ears" not in _neg1:
                        wf["11"]["inputs"]["text"] = _neg1 + ", " + _cg_extra_neg
                except Exception:  # noqa: BLE001
                    pass
            _extra = str(prompt or "").strip(" ，,。")
            # 2026-11-07：不管有没有写补充，都让她自己把画面展开。
            # 以前只有补充为空才扩写 —— 于是「/画你自己 做爱的场景」这种粗指令直接进翻译，
            # 翻成 sex scene 就完了，聊天里说过的姿势全丢。
            # 现在把粗指令当 hint 交给模型，它会连最近几句对话一起读，展开成具体画面。
            _hint_raw = _extra
            _impro = ""
            try:
                _impro = await _self_draw_improvise_llm(_hint_raw)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[出图] 自加戏（LLM）失败：{type(e).__name__}: {e}")
            if _impro and _hint_raw and _hint_raw not in _impro:
                _extra = f"{_hint_raw}，{_impro}"      # 指令 + 展开的画面
            elif _impro:
                _extra = _impro
            elif not _extra:
                try:
                    _extra = _self_draw_improvise()    # 兜底：本地随机小剧场
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[出图] 自加戏（本地）失败：{type(e).__name__}: {e}")
                    _extra = ""
            if _extra:
                logger.info(f"[出图] /画你自己 自加戏：{_extra[:70]}")
            # 中文补充先翻成英文再拼（SDXL 不认中文，直接塞等于没写）
            _extra_en = await _to_english_prompt(_extra) if _extra else ""
            # 2026-11-07：表情 tag 跟在场景后面（同属"这一张画什么"，都要抢在第一段）
            _emo = _draw_emotion_tags()
            # 姿势/体位词表兜底：从指令 + 原始 hint + 展开的画面里扫命中项
            _pose = _draw_pose_tags(prompt, raw_hint, _extra)
            _head = [x for x in (_extra_en, _emo, _pose) if x]
            if _head:
                logger.info(f"[出图] 拼装 head={_extra_en[:40]!r} emo={_emo[:30]!r} pose={_pose[:30]!r}")
            if _trig and _head and _extra_en and _extra_en.lower() not in _trig.lower():
                # 2026-11-07：动作/场景描述必须放最前面。
                # 触发词 325 字 ≈ 70+ token，几乎吃满 CLIP 第一个 77-token 分段；
                # 场景描述排在后面会掉进第二段，权重几乎归零 —— 实测「凌晨躲在被窝里举手机」
                # 自加戏，出图却是窗边吃点心，就是这个原因。所以补充在前、触发词在后。
                _text = f"{_quality_prefix()}, " + ", ".join(_head + [f"{_cg_extra_pos}{_trig}"])
                _text += ", (completely nude:1.55), (nude:1.4), topless, bare skin, (1boy:1.1), male, pale skin, (faceless male:1.3), male from behind, head out of frame" if _is_nsfw_now else ""
            elif _trig:
                _text = f"{_quality_prefix()}, " + ", ".join(_head + [f"{_cg_extra_pos}{_trig}"]) if _head \
                    else f"{_quality_prefix()}, {_cg_extra_pos}{_trig}"
                _text += ", (completely nude:1.55), (nude:1.4), topless, bare skin, (1boy:1.1), male, pale skin, (faceless male:1.3), male from behind, head out of frame" if _is_nsfw_now else ""
            else:
                _text = _extra_en or _extra
            if _tn in wf:
                wf[_tn]["inputs"]["text"] = _text
            wf = await _fix_missing_ckpt(wf)
            logger.info(f"[出图] 画你自己 → XL 工作流（触发词{len(_trig)}字 + 补充：{_extra[:30]}）")
            return await _submit_wf(wf, t0)

        # 普通画图 → z_image_turbo.json（静态文生图：EmptyLatent + 8 步 + denoise 1.0）
        # 不要用 z_face_consistency（img2img，从 feiyu_ref.jpg 出发，参考图白底会污染画面）
        # 也不要用 z_face_t2i（带视频管线那套 LoRA/采样设置，慢且不对路）
        # 可用 DRAW_STATIC_WF 切换：z（默认，静态质量好但 12GB 显存要 5~6 分钟/张）
        #                        xl（SDXL，40 秒~2 分钟，二次元风格）
        _fast = os.getenv("DRAW_STATIC_WF", "z").strip().lower()
        _wf_path = (ROOT / "comfy_workflows" / "dafeiyu_xl.json") if _fast == "xl" else ZSTATIC_WF
        if not _wf_path.exists():
            _wf_path = ZSTATIC_WF
        wf = json.loads(_wf_path.read_text(encoding="utf-8"))
        logger.info(f"[出图] 静态文生图工作流 {_wf_path.name}（mode={mode}）")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[出图] 读不到工作流：{e}")
        return None

    try:
        _tn = _find_text_node(wf)
        if not _set_wf_text(wf, _tn, str(prompt)[:800]):
            logger.warning(f"[出图] 没找到正向提示词节点（{_wf_path.name}），提示词可能没生效")

        # 尺寸：兼容任意空 latent 节点
        _sized = False
        for _node in wf.values():
            _ct = str(_node.get("class_type") or "")
            if _ct.startswith("Empty") and "Latent" in _ct:
                _inp = _node.setdefault("inputs", {})
                _inp["width"] = int(width)
                _inp["height"] = int(height)
                _sized = True
                break
        if not _sized and "10" in wf:
            wf["10"]["inputs"]["width"] = int(width)
            wf["10"]["inputs"]["height"] = int(height)

        # 随机种子
        for _node in wf.values():
            if str(_node.get("class_type") or "") not in ("KSampler", "KSamplerAdvanced"):
                continue
            _inp = _node.setdefault("inputs", {})
            _sd = random.randint(1, 2**31 - 1)
            if "noise_seed" in _inp:
                _inp["noise_seed"] = _sd
            elif "seed" in _inp:
                _inp["seed"] = _sd
        return await _submit_wf(wf, t0)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[出图] 本地出图异常：{type(e).__name__}: {e}")
        if mode != "edit":
            # Z 模型加载崩了 → 拉起 ComfyUI，换主人手调的 XL 工作流兜底
            try:
                await _comfy_ensure()
                _wf2 = json.loads(_xl_path.read_text(encoding="utf-8"))
                _t2 = _find_text_node(_wf2) or "10"
                _trig2 = _draw_self_triggers(_is_nsfw_scene(prompt) or _is_nsfw_scene(raw_hint))
                _en2 = await _to_english_prompt(str(prompt))
                _nude2 = ", (completely nude:1.55), (nude:1.4), topless, bare skin, (1boy:1.1), male, pale skin, (faceless male:1.3), male from behind, head out of frame" if _is_nsfw_now else ""
                _txt2 = (f"{_quality_prefix()}, {_trig2}, {_en2}{_nude2}" if _trig2 and _en2
                         else ((_trig2 or _en2) + _nude2))
                if _t2 in _wf2:
                    _wf2[_t2]["inputs"]["text"] = _txt2[:900]
                _wf2 = await _fix_missing_ckpt(_wf2)
                _img2 = await _submit_wf(_wf2, t0)
                if _img2:
                    logger.info("[出图] 已回退 XL 工作流出图")
                return _img2
            except Exception as e2:  # noqa: BLE001
                logger.warning(f"[出图] 回退也失败：{type(e2).__name__}: {e2}")
        return None

async def _sf_image(prompt: str, size: str = "1024x1024") -> bytes | None:
    """硅基流动出图（吃赠送额度）。"""
    if not SF_IMAGE_ENABLED:
        return None
    base = (os.getenv("SILICONFLOW_BASE_URL") or "").rstrip("/")
    key = os.getenv("SILICONFLOW_API_KEY") or ""
    if not base or not key:
        return None
    t0 = time.time()
    try:
        r = await asyncio.wait_for(http_client.post(
            f"{base}/images/generations",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": SF_IMAGE_MODEL, "prompt": str(prompt)[:800],
                  "image_size": size, "num_inference_steps": 8, "batch_size": 1,
                  "seed": random.randint(1, 2**31 - 1)},
        ), timeout=120)
        d = r.json() or {}
        if d.get("error"):
            logger.warning(f"[出图] 硅基失败：{str(d['error'])[:120]}")
            return None
        imgs = d.get("images") or (d.get("data") or [])
        if not imgs:
            logger.warning(f"[出图] 硅基无图：{str(d)[:150]}")
            return None
        first = imgs[0]
        url = first.get("url") if isinstance(first, dict) else str(first)
        if not url:
            return None
        if str(url).startswith("data:"):
            import base64 as _b64
            return _b64.b64decode(str(url).split(",", 1)[1])
        resp = await http_client.get(url, timeout=60, follow_redirects=True)
        if resp.status_code == 200 and resp.content:
            try:
                token_meter.record({}, "image", cost=0.0, provider="siliconflow")
            except Exception:  # noqa: BLE001
                pass
            logger.info(f"[出图] 硅基 Z-Image 用时 {time.time() - t0:.1f}s")
            return resp.content
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[出图] 硅基异常：{type(e).__name__}: {e}")
    return None


async def _draw_chain(prompt: str, width: int = 832, height: int = 1216, mode: str = "verbatim", raw_hint: str = "") -> tuple[bytes | None, str]:
    """出图顺序：本地 Z-Image → 硅基 Z-Image → 谷歌（如果启用）。返回 (图片, 来源)。"""
    img = await _local_zimage(prompt, width, height, mode=mode, raw_hint=raw_hint)
    if img:
        return img, "local"
    if os.getenv("DRAW_LOCAL_ONLY", "1").lower() in ("1", "true", "yes"):
        logger.warning("[出图] 画图只走本地，云端已被关闭（要开云端设 DRAW_LOCAL_ONLY=0）")
        return None, ""
    img = await _sf_image(prompt, f"{width}x{height}")
    if img:
        return img, "siliconflow"
    if os.getenv("GEMINI_IMAGE_ENABLED", "false").lower() in ("1", "true", "yes"):
        try:
            from plugins import gemini
            img = await gemini.gemini_image(prompt)
            if img:
                return img, "gemini"
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[出图] 谷歌失败：{e}")
    return None, ""


async def _doubao_ref_draw(scene: str, size: str = "1024x1536") -> bytes | None:
    """豆包 Seedream 参考图生图：角色外观锁死，只换动作/场景（2026-11-05）。

    主人给了一张理想的「蓝发鲸鱼娘·写实照片风」参考图（data/draw_ref/feiyu_ref.jpg），
    要求 /豆包 画你自己 用它当底：长相/发型/鱼尾/女仆装都不动，只让大肥鱼自己往
    场景里加「她现在在干嘛」。

    参考图走 DOUYIN_REF_IMAGE 这个环境变量，换图不用改代码。
    """
    if not (DOUBAO_DRAW_API_KEY or "").strip():
        return None
    ref = _ref_data_url()
    desc = str(scene or "").strip(" ，,。.！!？?")
    prompt = (
        "严格保持参考图中角色的外观完全一致：同一只蓝色鲸鱼拟人少女 —— "
        "深蓝长发、鲸鱼鳍耳、身后一条鲸鱼尾巴、蓝白女仆装与白色围裙、圆脸大眼睛、肤色白皙，"
        "这些全部不许改。只改变她的动作、表情、手边的东西和所处场景。"
        "写实照片质感，真实皮肤纹理，自然光，浅景深，无文字无水印。"
        # 2026-11-05：她自编场景时写「蓝色大肥鱼举着手机…」，模型真给画了一条
        # 大鱼当道具（左边那条）。这里把人数和"不许有额外生物"说死。
        "画面中只有她本人一个角色、独自出现在镜头里 —— 不要出现额外的鱼、鱼形玩偶、"
        "抱枕、宠物或任何其他生物；她身后那条鲸鱼尾巴是她自己的，不能单独画成一条鱼。"
        "她现在在：" + (desc[:150] if desc else "窝在沙发上发呆")
    )
    payload = {
        "model": DOUBAO_IMAGE_MODEL,
        "prompt": prompt,
        "response_format": "url",
        "size": size,
    }
    if ref:
        payload["image"] = [ref]
    try:
        r = await http_client.post(
            "https://ark.cn-beijing.volces.com/api/v3/images/generations",
            headers={"Authorization": f"Bearer {DOUBAO_DRAW_API_KEY}"},
            json=payload, timeout=120,
        )
        d = r.json()
        url = ((d.get("data") or [{}])[0]).get("url")
        if not url:
            logger.warning(f"[豆包] 参考图生图失败：{str(d)[:150]}")
            return None
        rr = await http_client.get(url, timeout=90)
        return rr.content
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[豆包] 参考图生图异常：{type(e).__name__}: {e}")
        return None


async def _gen_douyin_images(prompts: list[str], outdir: Path) -> list[Path]:
    """用 Seedream 参考图生图：角色外观锁死，只改动作/场景。"""
    files: list[Path] = []
    ref = _ref_data_url()
    for idx, scene in enumerate(prompts[:6]):
        prompt = (
            "严格保持参考图中角色的外观完全一致：同一只蓝色鲸鱼拟人少女，"
            "脸型、发型、鲸鱼尾巴、蓝白女仆装、头饰、配色都不变；只改变动作、表情和场景。\n"
            "竖屏 9:16 短视频画面，干净构图、无文字水印。场景：" + str(scene)[:120]
        )
        payload = {
            "model": DOUBAO_IMAGE_MODEL,
            "prompt": prompt,
            "response_format": "url",
            "size": "720x1280",
        }
        if os.getenv("GEMINI_IMAGE_ENABLED", "false").lower() in ("1", "true", "yes"):
            try:
                from plugins import gemini
                ref_bytes = base64.b64decode(ref.split(",", 1)[1]) if ref and "," in ref else None
                img_bytes = await gemini.gemini_image(prompt, ref_bytes)
                if img_bytes:
                    img_path = outdir / f"scene_{idx + 1}.jpg"
                    img_path.write_bytes(img_bytes)
                    files.append(img_path)
                    logger.info(f"[抖音视频] 第 {idx + 1} 张图走 Gemini 免费额度")
                    continue
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[抖音视频] Gemini 出图失败，回退火山：{e}")
        if ref:
            payload["image"] = [ref]
        try:
            r = await http_client.post(
                "https://ark.cn-beijing.volces.com/api/v3/images/generations",
                headers={"Authorization": f"Bearer {DOUBAO_DRAW_API_KEY}"},
                json=payload,
                timeout=90,
            )
            data = r.json()
            url = ((data.get("data") or [{}])[0]).get("url")
            if not url:
                logger.warning(f"[抖音视频] 第 {idx + 1} 张图无 url：{str(data)[:120]}")
                continue
            async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
                resp = await client.get(url)
            img_path = outdir / f"scene_{idx + 1}.jpg"
            img_path.write_bytes(resp.content)
            files.append(img_path)
            record_image_cost(1)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[抖音视频] 第 {idx + 1} 张图失败：{e}")
    return files


def _compose_douyin(outdir: Path, images: list[Path], audio: Path, lines: list[str]) -> Path:
    """用 ffmpeg 把分镜 + 配音 + 字幕合成 1080x1920 MP4。"""
    ff = _ffmpeg_exe()
    duration = _media_duration(audio) or 25.0
    _write_ass(outdir / "subs.ass", lines, duration)
    per = max(2.0, duration / len(images))
    list_lines = []
    for i, img in enumerate(images):
        list_lines.append(f"file '{img.name}'")
        list_lines.append(f"duration {per if i < len(images) - 1 else max(per, duration - per * (len(images) - 1)):.3f}")
    list_lines.append(f"file '{images[-1].name}'")
    (outdir / "list.txt").write_text("\n".join(list_lines), encoding="utf-8")
    out = outdir / "douyin.mp4"
    cmd = [
        ff, "-y", "-f", "concat", "-safe", "0", "-i", "list.txt", "-i", audio.name,
        "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
               "subtitles=subs.ass,fps=30",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart",
        "douyin.mp4",
    ]
    proc = subprocess.run(cmd, cwd=str(outdir), capture_output=True)
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "ignore")[-600:]
        raise RuntimeError(f"ffmpeg 合成失败：{err}")
    return out


def _persona_snippet() -> str:
    try:
        text = (ROOT / "persona.md").read_text(encoding="utf-8")
        return re.sub(r"\s+", " ", text)[:700]
    except Exception:
        return "圆滚滚的蓝色鲸鱼拟人少女，傲娇嘴损、黏主人。"


def _recent_events_for_video() -> str:
    try:
        rows = get_today_memory(12, f"private_{YOUR_QQ_ID}") or []
    except Exception:
        rows = []
    if not rows:
        try:
            rows = get_recent_memory(6, f"private_{YOUR_QQ_ID}") or []
        except Exception:
            rows = []
    parts = []
    for r in rows[-6:]:
        who = r.get("speaker") or "主人"
        msg = str(r.get("message") or r.get("msg") or "").strip()
        if msg:
            parts.append(f"{who}：{msg[:50]}")
    return "；".join(parts) if parts else "今天比较平静，没特别的事"


async def make_douyin_video() -> tuple[Path | None, str]:
    set_activity("抖音制作中", 600)
    """生成一条 30~45 秒竖屏视频，返回 (mp4 路径, 文案)。"""
    stamp = time.strftime("%Y%m%d_%H%M%S")
    outdir = DOUYIN_DIR / stamp
    outdir.mkdir(parents=True, exist_ok=True)

    prompt = (
        "你是抖音短视频编导，帮「蓝色大肥鱼」写一条 30~45 秒竖屏短视频。"
        "她是圆滚滚的蓝色鲸鱼拟人少女：傲娇、嘴损、黏主人、爱吐槽，但不低俗。\n"
        f"【人设】{_persona_snippet()}\n"
        f"【她现在的状态】{CURRENT_DAILY_MOOD}；情绪分布：{emotion_prompt()}；{just_woke_prompt().strip() or '清醒'}\n"
        f"【今天/最近发生的事】{_recent_events_for_video()}\n"
        "视频要像她今天真实会发的内容：结合她的心情和最近发生的事，不要通用套话、不要报流水账。\n"
        "只输出 JSON（不要 markdown/解释）："
        '{"title":"标题20字内","lines":["口播第1句","第2句","第3句","第4句","第5句","第6句"],'
        '"caption":"发布文案60字内","tags":["#话题1","#话题2","#话题3"],'
        '"image_prompts":["画面1","画面2","画面3","画面4","画面5","画面6"]}\n'
        "要求：开头 3 秒有钩子；口播 6 句、每句 12~22 字，总字数 110~150；"
        "画面描述与口播对应，适合竖屏；不要出现真实人名、医疗金融建议、低俗擦边。"
    )
    raw = await call_ai_raw(prompt, "deepseek", temperature=0.9, max_tokens=1200)
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None, "脚本生成失败"
    try:
        data = json.loads(raw[start:end + 1])
    except Exception:
        return None, "脚本 JSON 解析失败"
    lines = [str(x).strip() for x in (data.get("lines") or []) if str(x).strip()][:6]
    if len(lines) < 2:
        return None, "脚本句子太少"
    title = str(data.get("title") or "").strip()[:30]
    caption = str(data.get("caption") or "").strip()[:120]
    tags = " ".join(str(t).strip() for t in (data.get("tags") or [])[:5])
    image_prompts = [str(x) for x in (data.get("image_prompts") or []) if str(x).strip()]
    if len(image_prompts) < len(lines):
        image_prompts += lines[len(image_prompts):]

    narration = "，".join(lines)
    audio = await _doubao_tts(narration)
    if not audio:
        try:
            communicate = edge_tts.Communicate(narration, TTS_VOICE, rate=TTS_RATE, pitch=TTS_PITCH)
            audio = str(outdir / "voice.mp3")
            await communicate.save(audio)
        except Exception:
            audio = None
    if audio:
        audio_path = Path(audio)
        if audio_path.parent != outdir:
            import shutil
            shutil.copy(audio_path, outdir / "voice.mp3")
        audio_path = outdir / "voice.mp3"
    else:
        audio_path = outdir / "voice.mp3"
        ff = _ffmpeg_exe()
        subprocess.run([ff, "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
                        "-t", "22", str(audio_path)], capture_output=True)

    images = await _gen_douyin_images(image_prompts, outdir)
    if not images:
        fallback = outdir / "scene_1.jpg"
        img = Image.new("RGB", (720, 1280), (40, 90, 160))
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 48)
        except Exception:
            font = ImageFont.load_default()
        draw.text((360, 560), "蓝色大肥鱼", font=font, fill=(255, 255, 255), anchor="mm")
        img.save(fallback, quality=90)
        images = [fallback]

    try:
        out = await asyncio.to_thread(_compose_douyin, outdir, images, audio_path, lines)
    except Exception as e:  # noqa: BLE001
        return None, f"合成失败：{e}"

    (outdir / "caption.txt").write_text(
        f"标题：{title}\n文案：{caption}\n话题：{tags}\n口播：\n" + "\n".join(lines),
        encoding="utf-8",
    )
    try:
        cover = outdir / "cover.jpg"
        cover.write_bytes(images[0].read_bytes())
    except Exception:
        pass
    # 清理中间文件，只留 mp4 / 封面 / 文案
    for p in outdir.glob("scene_*.jpg"):
        try:
            p.unlink()
        except OSError:
            pass
    for name in ("list.txt", "subs.ass", "voice.mp3"):
        try:
            (outdir / name).unlink()
        except OSError:
            pass
    logger.info(f"[抖音视频] 生成完成：{out}")
    _log_douyin_video(title, caption, tags, lines, out)
    return out, f"{title}\n{caption}\n{tags}"


DOUYIN_COMMENT_STATE = ROOT / "memory" / "douyin_comment_state.json"
DOUYIN_COMMENT_LOG = ROOT / "memory" / "douyin_comments_log.json"
DOUYIN_VIDEO_LOG = ROOT / "memory" / "douyin_videos.json"
_DOUYIN_POLL_LOCK = asyncio.Lock()


NAPCAT_EXE = Path(r"D:\NapCat.52230.Shell\NapCatWinBootMain.exe")
NAPCAT_DIR = NAPCAT_EXE.parent
NAPCAT_WATCH = {"down_since": 0.0, "last_restart": 0.0, "was_down": False}


def _napcat_connected() -> bool:
    try:
        import psutil
        for c in psutil.net_connections(kind="inet"):
            if c.status == psutil.CONN_ESTABLISHED and c.laddr and c.laddr.port == 8080:
                return True
    except Exception:
        pass
    return False


@scheduler.scheduled_job("interval", minutes=1)
async def napcat_watchdog():
    """NapCat 掉线监控（默认关闭，避免自动重启把登录态清掉）。"""
    if os.getenv("NAPCAT_WATCHDOG_ENABLED", "false").lower() not in ("1", "true", "yes"):
        return
    now = time.time()
    if _napcat_connected():
        NAPCAT_WATCH["logged_notice"] = False
        if NAPCAT_WATCH["was_down"]:
            mins = int((now - NAPCAT_WATCH["down_since"]) // 60)
            NAPCAT_WATCH.update(was_down=False, down_since=0)
            logger.info(f"[看门狗] NapCat 已恢复，掉线约 {mins} 分钟")
            try:
                await get_bot().send_private_msg(
                    user_id=int(YOUR_QQ_ID),
                    message=f"(*甩尾巴*) 主人，刚才 NapCat 掉了 {mins} 分钟，本鱼已经自己连回来了。",
                )
            except Exception:
                pass
        return
    if not NAPCAT_WATCH["down_since"]:
        NAPCAT_WATCH["down_since"] = now
        NAPCAT_WATCH["was_down"] = True
        return
    if now - NAPCAT_WATCH["down_since"] < 120:
        return
    if now - NAPCAT_WATCH["last_restart"] < 600:
        return
    # 进程还在但没连接：多半是掉登录/需要扫码，重启只会把登录态一起清掉
    proc_alive = False
    try:
        import psutil
        proc_alive = any(p.info["name"] and p.info["name"].lower() == "napcatwinbootmain.exe"
                         for p in psutil.process_iter(["name"]))
    except Exception:
        pass
    if proc_alive:
        if not NAPCAT_WATCH.get("logged_notice"):
            logger.warning("[看门狗] NapCat 进程在但没有连接，可能需要扫码；不做重启")
            NAPCAT_WATCH["logged_notice"] = True
        return
    NAPCAT_WATCH["logged_notice"] = False
    try:
        logger.warning("[看门狗] NapCat 掉线超过 2 分钟，尝试自动重启")
        subprocess.run(["taskkill", "/IM", "NapCatWinBootMain.exe", "/F"], capture_output=True)
        time.sleep(2)
        subprocess.Popen([str(NAPCAT_EXE), "10086"], cwd=str(NAPCAT_DIR),
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        NAPCAT_WATCH["last_restart"] = now
        NAPCAT_WATCH["down_since"] = now + 180  # 给登录留时间，避免连着重启
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[看门狗] 自动重启 NapCat 失败：{e}")


def _douyin_video_summary() -> str:
    """直接读本地作品归档，保证被问“发了几条视频”时答得出来。"""
    try:
        data = json.loads(DOUYIN_VIDEO_LOG.read_text(encoding="utf-8"))
        items = data.get("items") if isinstance(data, dict) else data
        items = [x for x in (items or []) if isinstance(x, dict)]
    except Exception:
        return ""
    if not items:
        return "【抖音作品】你还没发过视频。"
    recent = items[-6:]
    lines = [f"· {str(x.get('title') or '未命名')}（{str(x.get('ts') or '')[:10]}）" for x in reversed(recent)]
    return (
        f"\n\n【抖音作品】你一共发过 {len(items)} 条视频，最近的是：\n" + "\n".join(lines) +
        "\n被问到抖音发了几条、发过什么时，直接按这个数字回答，别含糊。"
    )


def _douyin_phone_brief() -> str:
    """用生活化口吻汇报抖音互动：别人说了啥、她回了啥。"""
    try:
        st = json.loads((ROOT / "memory" / "douyin_phone.json").read_text(encoding="utf-8"))
    except Exception:
        st = {}
    limit = 2 + int(st.get("bonus") or 0)
    count = int(st.get("count") or 0)
    hist = st.get("history") or []
    cutoff = time.time() - 3 * 3600
    recent = []
    for h in hist:
        try:
            ts = time.mktime(time.strptime(str(h.get("ts")), "%Y-%m-%d %H:%M:%S"))
        except Exception:
            continue
        if ts >= cutoff:
            recent.append(h)

    lines = ["手机抖音最近："]
    if recent:
        lines.append(f"手机这边回了 {len(recent)} 条：")
        for h in recent[-3:][::-1]:
            lines.append(f"· {h.get('sender')} 说「{str(h.get('comment'))[:26]}」，我回「{str(h.get('reply'))[:34]}」")
    else:
        lines.append("手机这边最近没人找我搭话。")

    try:
        log = json.loads((ROOT / "memory" / "douyin_comments_log.json").read_text(encoding="utf-8"))
        today = time.strftime("%Y-%m-%d")
        items = [x for x in (log.get("items") or []) if str(x.get("ts", "")).startswith(today)]
        posted = [x for x in items if x.get("posted")]
        if items:
            lines.append(f"自己视频底下有 {len(items)} 条，回了 {len(posted)} 条：")
            shown = 0
            for x in reversed(items):
                if shown >= 3:
                    break
                shown += 1
                if x.get("posted"):
                    lines.append(f"· {x.get('user')} 说「{str(x.get('text'))[:26]}」，我回「{str(x.get('reply'))[:34]}」")
                else:
                    lines.append(f"· {x.get('user')} 说「{str(x.get('text'))[:26]}」，这条我还没回")
        elif not recent:
            lines.append("今天抖音挺安静，没人找我说话。")
    except Exception:
        pass

    lines.append(f"（今天手机额度 {count}/{limit} 条）")
    return "\n".join(lines)


async def _phone_poll_and_brief() -> str:
    """主人问“看抖音”时：先下拉刷新并处理一轮，再汇报手机状态。"""
    try:
        logger.info("[抖音手机] 主人查询，开始强制刷新")
        from plugins import douyin_phone as dp
        await dp._poll_once()
        logger.info("[抖音手机] 主人查询处理完成")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[抖音手机] 手动触发失败：{type(e).__name__}: {e}")
    return _douyin_phone_brief()


def _log_douyin_video(title: str, caption: str, tags: str, lines: list[str], path: Path) -> None:
    """把她发过的抖音视频归档到本地 + 长期记忆。"""
    try:
        try:
            data = json.loads(DOUYIN_VIDEO_LOG.read_text(encoding="utf-8"))
            items = data.get("items") if isinstance(data, dict) else data
            items = list(items or [])
        except Exception:
            items = []
        items.append({
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "title": title, "caption": caption, "tags": tags,
            "lines": lines, "file": str(path), "posted": True,
        })
        DOUYIN_VIDEO_LOG.write_text(
            json.dumps({"items": items[-200:]}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[抖音视频] 归档写入失败：{e}")
    try:
        ltm.add_pending("douyin", "抖音视频",
                        f"发布视频《{title}》：{caption} {tags}"[:160])
    except Exception:
        pass


def _log_douyin_comment(user: str, text: str, reply: str, posted: bool) -> None:
    """抖音评论 + 回复写本地日志，并进长期档案（scope=douyin）。"""
    try:
        try:
            data = json.loads(DOUYIN_COMMENT_LOG.read_text(encoding="utf-8"))
            items = data.get("items") if isinstance(data, dict) else data
            items = list(items or [])
        except Exception:
            items = []
        items.append({
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "user": user, "text": text, "reply": reply,
            "posted": bool(posted),
        })
        DOUYIN_COMMENT_LOG.write_text(
            json.dumps({"items": items[-500:]}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[抖音评论] 日志写入失败：{e}")
    try:
        ltm.add_pending("douyin", "抖音评论", f"{user}：{text} → {reply}"[:120])
    except Exception:
        pass


def _load_dcs() -> dict:
    try:
        data = json.loads(DOUYIN_COMMENT_STATE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("enabled", False)
            data.setdefault("auto_reply", False)
            data.setdefault("seen", [])
            data.setdefault("last_poll", "")
            return data
    except Exception:
        pass
    return {"enabled": False, "auto_reply": False, "seen": [], "last_poll": ""}


def _save_dcs(data: dict) -> None:
    try:
        DOUYIN_COMMENT_STATE.parent.mkdir(parents=True, exist_ok=True)
        DOUYIN_COMMENT_STATE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[抖音评论] 状态保存失败：{e}")


async def _fetch_douyin_comments() -> tuple[list[dict], str, str]:
    """用已登录的持久化会话打开创作者中心评论页，拦截评论接口返回。"""
    from playwright.async_api import async_playwright
    responses = []
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            str(ROOT / "data" / "douyin_profile"), channel="msedge", headless=True,
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        page.on("response", lambda r: responses.append(r) if "comment/list" in r.url else None)
        await page.goto("https://creator.douyin.com/creator-micro/interactive/comment", timeout=60000)
        await page.wait_for_timeout(7000)
        if not responses:
            try:
                await page.get_by_text("点击刷新").click(timeout=5000)
            except Exception:
                pass
            await page.wait_for_timeout(4000)
        comments: list[dict] = []
        for resp in responses:
            try:
                data = await resp.json()
            except Exception:
                continue
            got = data.get("comments") or []
            if got:
                comments = got
        url = page.url
        try:
            body = await page.inner_text("body")
        except Exception:
            body = ""
        await ctx.close()
        return comments, url, body


async def _fetch_douyin_notice_counts() -> dict[int, int]:
    """读取抖音互动通知计数（回复我的/提到我的等），只用于提醒有新互动。"""
    from playwright.async_api import async_playwright
    responses = []
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            str(ROOT / "data" / "douyin_profile"), channel="msedge", headless=True,
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        page.on("response", lambda r: responses.append(r) if "notice/count" in r.url else None)
        try:
            await page.goto("https://www.douyin.com/user/self?showTab=message", timeout=60000)
            await page.wait_for_timeout(8000)
        except Exception:
            pass
        counts: dict[int, int] = {}
        for resp in responses:
            try:
                data = await resp.json()
            except Exception:
                continue
            for item in data.get("notice_count") or []:
                try:
                    counts[int(item.get("group"))] = int(item.get("count") or 0)
                except Exception:
                    continue
        await ctx.close()
        return counts


async def _try_post_douyin_reply(comment_text: str, reply_text: str) -> bool:
    """尝试在创作者中心评论页直接点“回复”并发送（自动回复，风险自负）。"""
    from playwright.async_api import async_playwright
    try:
        async with async_playwright() as pw:
            ctx = await pw.chromium.launch_persistent_context(
                str(ROOT / "data" / "douyin_profile"), channel="msedge", headless=True,
                viewport={"width": 1280, "height": 900},
            )
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto("https://creator.douyin.com/creator-micro/interactive/comment", timeout=60000)
            await page.wait_for_timeout(7000)
            try:
                await page.get_by_text("点击刷新").click(timeout=3000)
                await page.wait_for_timeout(3000)
            except Exception:
                pass
            # 已经回过同样的话就直接跳过，避免重复刷两条
            try:
                body = await page.inner_text("body")
                probe = reply_text.strip()[:10]
                if probe and probe in body:
                    logger.info("[抖音评论] 页面已有相同回复，跳过重复发送")
                    await ctx.close()
                    return True
            except Exception:
                pass
            snippet = comment_text.strip()[:24]
            target = page.get_by_text(snippet, exact=False).first
            try:
                container = target.locator(
                    "xpath=ancestor::*[.//*[contains(text(),'回复')]][1]"
                )
                btn = container.get_by_text("回复", exact=True).first
                await btn.click(timeout=5000)
            except Exception:
                # 退而求其次：页面里第一个“回复”按钮
                await page.get_by_text("回复", exact=True).first.click(timeout=5000)
            await page.wait_for_timeout(500)
            editor = page.locator('div[contenteditable="true"], textarea').last
            await editor.click(timeout=5000)
            await editor.fill(reply_text, timeout=5000)
            try:
                await page.get_by_text("发送", exact=True).last.click(timeout=5000)
            except Exception:
                await page.keyboard.press("Enter")
            await page.wait_for_timeout(1500)
            await ctx.close()
            return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[抖音评论] 自动回复失败：{e}")
        return False


async def _douyin_poll_once() -> tuple[int, int, str]:
    """抓一次新评论，生成回复草稿；返回 (新评论数, 评论总数, 页面文本)。"""
    set_activity("回复抖音中", 180)
    if _DOUYIN_POLL_LOCK.locked():
        return 0, 0, ""
    async with _DOUYIN_POLL_LOCK:
        try:
            comments, _url, body = await _fetch_douyin_comments()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[抖音评论] 抓取失败：{e}")
            return 0, 0, ""
        st = _load_dcs()
        seen = set(str(x) for x in st.get("seen", []))
        bot = get_bot()
        fresh = 0
        auto_count = 0
        auto_slots: set[str] = set()
        posted_cids = set(str(x) for x in st.get("posted_cids", []))
        if st.get("auto_reply"):
            seen_now = set(str(x) for x in st.get("seen", []))
            candidates = []
            for c in comments[:20]:
                cid = str(c.get("cid") or "")
                text = str(c.get("text") or "").strip()
                user = str((c.get("user") or {}).get("nickname") or "")
                if not cid or not text or cid in seen_now or cid in posted_cids:
                    continue
                if user == "爱吃白米饭的鲸鱼娘":
                    continue
                candidates.append((cid, text))
            mention_ids = [cid for cid, t in candidates
                           if any(k in t for k in ("@", "大肥鱼", "鲸鱼娘"))]
            other_ids = [cid for cid, _ in candidates if cid not in mention_ids]
            # 自动回复开启时，所有候选都尝试自动回，但每小时最多 3 条、每轮最多 2 条
            ordered = mention_ids + other_ids
            now_ts = time.time()
            recent_posted = [float(x) for x in (st.get("posted_times") or [])
                             if now_ts - float(x) < 3600]
            st["posted_times"] = recent_posted
            remaining_hour = max(0, 3 - len(recent_posted))
            auto_slots = set(ordered[:min(2, remaining_hour)])
        for c in comments[:20]:
            cid = str(c.get("cid") or "")
            text = str(c.get("text") or "").strip()
            user = str((c.get("user") or {}).get("nickname") or "网友")
            if not cid or not text:
                continue
            if user == "爱吃白米饭的鲸鱼娘":
                continue
            if cid in seen:
                continue
            seen.add(cid)
            prompt = (
                "你是「蓝色大肥鱼」，一只圆滚滚的蓝色鲸鱼拟人少女，傲娇、嘴损但有分寸。"
                f"有人在你抖音视频下评论：{text}（评论者：{user}）。"
                "写一句 20~40 字的回复：像真人博主回评，可以接梗、撒娇或轻怼，不脏话、不low、"
                "不要自称AI，不要解释；只输出回复正文。"
            )
            try:
                reply = await call_ai_raw(prompt, "deepseek", temperature=0.9, max_tokens=200)
            except Exception:
                reply = "谢谢评论捏～"
            mention = ("@" in text) or ("大肥鱼" in text) or ("鲸鱼娘" in text)
            posted = False
            if cid in auto_slots and auto_count < 4 and cid not in posted_cids:
                posted = await _try_post_douyin_reply(text, reply)
                if posted:
                    auto_count += 1
                    posted_cids.add(cid)
                    st["posted_cids"] = list(posted_cids)[-200:]
                    st["posted_times"] = ([float(x) for x in (st.get("posted_times") or [])
                                           if time.time() - float(x) < 3600] + [time.time()])[-50:]
                    st["seen"] = list(seen)
                    _save_dcs(st)
                    # 两条自动回复之间随机隔 1~3 分钟，降低风控
                    await asyncio.sleep(random.uniform(60, 180))
            head = "💬 抖音新评论（已自动回复）" if posted else "💬 抖音新评论"
            _log_douyin_comment(user, text, reply, posted)
            if posted:
                tail = "这条她已经在抖音里自动回过了。"
            elif st.get("auto_reply"):
                tail = "（这条自动回复没成功，先没回。）"
            else:
                tail = "（这是草稿，你复制到抖音去发；回复 `关闭抖音评论` 可停）"
            try:
                await bot.send_private_msg(
                    user_id=int(YOUR_QQ_ID),
                    message=(
                        f"{head}\n{user}：{text}\n\n"
                        f"{'实际回复' if posted else '建议回复'}：\n{reply}\n\n{tail}"
                    ),
                )
                fresh += 1
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[抖音评论] 发送草稿失败：{e}")
        st["seen"] = list(seen)[-500:]
        st["last_poll"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _save_dcs(st)
        return fresh, len(comments), body


@scheduler.scheduled_job("interval", minutes=10)
async def douyin_comment_job():
    st = _load_dcs()
    if not st.get("enabled"):
        return
    try:
        await _douyin_poll_once()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[抖音评论] 定时轮询异常：{e}")
    try:
        counts = await _fetch_douyin_notice_counts()
        prev = st.get("notice_counts") or {}
        changed = any(int(counts.get(g, 0)) > int(prev.get(g, 0)) for g in counts)
        st = _load_dcs()
        st["notice_counts"] = counts
        _save_dcs(st)
        if changed and counts:
            bot = get_bot()
            await bot.send_private_msg(
                user_id=int(YOUR_QQ_ID),
                message=random.choice([
                    "(*划着手机突然停住*) 主人，抖音那边又有人戳我了……\n我还没回，你想让我怎么接？",
                    "(*把手机往你那边递*) 喏，抖音又有人找本鱼。\n要我自己看着回，还是你先给我个方向？",
                    "(*尾巴一甩*) 抖音来动静了，有人回我/点我名。\n我先憋着，等你一句话再回。",
                ]),
            )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[抖音通知] 检查失败：{e}")


LOCAL_TTS_URL = os.getenv("LOCAL_TTS_URL", "http://127.0.0.1:9881").rstrip("/")
LOCAL_TTS_PY = os.getenv("LOCAL_TTS_PY", "D:/f5tts/.venv/Scripts/python.exe")
LOCAL_TTS_SCRIPT = os.getenv("LOCAL_TTS_SCRIPT", "D:/qq-ai-bot/tools/local_tts_server.py")
_LOCAL_TTS_PROC = None


def _local_tts_port_open() -> bool:
    import socket
    try:
        with socket.create_connection(("127.0.0.1", 9881), timeout=1.5):
            return True
    except Exception:  # noqa: BLE001
        return False


_INDEXTTS_PY = Path(r"D:\index-tts\.venv\Scripts\python.exe")
_INDEXTTS_DIR = Path(r"D:\index-tts")
_INDEXTTS_LAST_SPAWN = 0.0


def _spawn_indextts() -> None:
    """后台拉起 7860 的 IndexTTS2（画图前被停掉后的兜底恢复）。

    2026-11-05：出图前会卸/杀 IndexTTS2 腾显存，画完得能自己回来。
    首次合成才会载模型（1~3 分钟），所以本轮不指望它出音，先走兜底。
    """
    global _INDEXTTS_LAST_SPAWN
    if time.time() - _INDEXTTS_LAST_SPAWN < 120:
        return          # 两分钟内试过了，别反复拉
    _INDEXTTS_LAST_SPAWN = time.time()
    global _SPAWNED_AT
    _SPAWNED_AT = time.time()     # 2026-11-07：给空闲回收一个宽限期用
    try:
        if not _INDEXTTS_PY.exists():
            logger.warning(f"[TTS] 找不到 IndexTTS2 启动器：{_INDEXTTS_PY}")
            return
        import subprocess
        _logf = open(ROOT / "logs" / "indextts2.out.log", "ab", buffering=0)
        subprocess.Popen(
            [str(_INDEXTTS_PY), "server.py", "--port", "7860", "--fp16",
             "--s2mel_fp16", "--w2v_fp16", "--diffusion_steps", "25",
             "--inference_cfg_rate", "0.7"],
            cwd=str(_INDEXTTS_DIR), stdout=_logf, stderr=_logf,
            creationflags=(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                           | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)))
        logger.info("[TTS] IndexTTS2 未在运行，已在后台拉起（首轮约 1~3 分钟）")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[TTS] 拉起 IndexTTS2 失败：{type(e).__name__}: {e}")


async def _ensure_local_tts(timeout: float = 180.0) -> bool:
    """本地 F5-TTS 服务没起就拉起（模型首次加载较慢）。"""
    global _LOCAL_TTS_PROC
    async def _ready() -> bool:
        try:
            r = await http_client.get(LOCAL_TTS_URL + "/health", timeout=3)
            return bool((r.json() or {}).get("ok"))
        except Exception:  # noqa: BLE001
            return False

    if await _ready():
        return True
    if not _local_tts_port_open():
        try:
            import subprocess
            env = dict(os.environ)
            env["HF_HUB_OFFLINE"] = "1"
            env["TRANSFORMERS_OFFLINE"] = "1"
            logf = open(ROOT / "logs" / "local_tts.log", "ab", buffering=0)
            _LOCAL_TTS_PROC = subprocess.Popen(
                [LOCAL_TTS_PY, LOCAL_TTS_SCRIPT], cwd=r"D:\qq-ai-bot",
                env=env, stdout=logf, stderr=logf,
                creationflags=(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                              | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)))
            logger.warning("[TTS] 本地 F5-TTS 没起，正在拉起（首次要载模型，约 1~3 分钟）")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[TTS] 拉起本地 TTS 失败：{type(e).__name__}: {e}")
            return False
    t0 = time.time()
    while time.time() - t0 < timeout:
        await asyncio.sleep(4)
        if await _ready():
            logger.info(f"[TTS] 本地 F5-TTS 就绪（{time.time()-t0:.0f}s）")
            return True
        if _LOCAL_TTS_PROC is not None and _LOCAL_TTS_PROC.poll() is not None:
            logger.warning(f"[TTS] 本地 TTS 进程退出 code={_LOCAL_TTS_PROC.returncode}")
            return False
    return False


async def _local_tts(text: str, timeout: float = 75.0) -> str | None:
    """本地 TTS（9881 服务，引擎可能是 IndexTTS2 或 F5）合成，返回 mp3 路径；失败返回 None。"""
    # 2026-11-05 主人定的互斥规则：画图和语音只许一个占显存。
    # 这里方向是「语音优先」——要说话就先中断出图、卸掉 ComfyUI 的模型。
    # （反方向在 _submit_wf：出图前无条件卸/停 IndexTTS2。）
    try:
        if _comfy_busy() or (time.time() - float(_COMFY_LAST_ACTIVITY or 0) < 1800):
            await _comfy_yield_for_tts()
    except Exception as _e:  # noqa: BLE001
        logger.debug(f"[互斥] 让位画图失败（照常合成）：{type(_e).__name__}")
    # 2026-11-05：语音活动时间戳，空闲自动停靠它判断
    global _TTS_LAST_ACTIVITY
    _TTS_LAST_ACTIVITY = time.time()
    if not await _ensure_local_tts():
        return None
    # 2026-11-05：9881 转发层活着不代表 7860 活着（出图前可能被停/杀）。
    # 没起就后台拉，本轮返回 None 让别人兜底，别在这里干等 75 秒。
    if not _port_listening(7860):
        _spawn_indextts()
        return None
    out = ROOT / "data" / "tts_local" / f"tts_{int(time.time()*1000)}.mp3"
    try:
        # 2026-11-05：带上情绪向量——本地 IndexTTS2 走 8 轴情绪，不带的话永远同一个语气
        # 2026-11-05 补：情欲高时同时换参考音频，否则气息出不来（向量做不出喘）
        _ref_voice = _arousal_ref()
        _payload = {"text": text,
                    "out": str(out).replace("\\", "/"),
                    "emo_vector": _tts_emo_vector()}
        if _ref_voice:
            _payload["voice"] = _ref_voice
        r = await http_client.post(LOCAL_TTS_URL + "/tts",
                                   json=_payload,
                                   timeout=timeout)
        d = r.json() or {}
        if d.get("ok") and d.get("path"):
            _eng = str(d.get("engine") or "").strip().lower() or await _tts_engine() or "未知"
            _ref = str(d.get("ref") or "").strip()
            _tag = "IndexTTS2" if _eng == "indextts2" else _eng.upper()
            # 2026-11-05：兴奋/情欲高时说快一点（1.15×）。语速不是情绪，向量管不到，
            # 只能靠 atempo 后期加速——实测 1.15 像「急」，1.25 就机械了。
            _sp = _tts_speed()
            _final = str(d["path"])
            if _sp > 1.001:
                try:
                    import subprocess as _sp_mod
                    _fast = str(Path(_final).with_name(Path(_final).stem + "_fast" + Path(_final).suffix))
                    _r = await asyncio.to_thread(
                        lambda: _sp_mod.run(
                            ["ffmpeg", "-y", "-i", _final, "-filter:a", f"atempo={_sp}",
                             "-b:a", "192k", _fast],
                            capture_output=True, timeout=60))
                    if _r.returncode == 0 and Path(_fast).exists() and Path(_fast).stat().st_size > 1000:
                        _final = _fast
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"[TTS] 加速失败（用原速）：{type(e).__name__}")
            logger.info(
                f"[TTS] 本地 {_tag} 出音：{Path(_final).name}（{_sp:.2f}×）"
                + (f" ref={_ref}" if _ref else ""))
            return _final
        logger.warning(f"[TTS] 本地合成返回异常：{str(d)[:160]}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[TTS] 本地合成失败：{type(e).__name__}: {e}")
    return None


def _rvc_start_bg() -> None:
    """后台拉起 RVC（不阻塞本次发言）；已经在跑就跳过。"""
    global _rvc_proc
    try:
        if _rvc_proc is not None and _rvc_proc.poll() is None:
            return
        import subprocess
        logf = open(ROOT / "logs" / "rvc_ondemand.log", "ab", buffering=0)
        _rvc_proc = subprocess.Popen(
            [RVC_PY, RVC_SCRIPT, *RVC_ARGS], cwd=RVC_DIR,
            stdout=logf, stderr=logf,
            creationflags=(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                           | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)))
        logger.warning(f"[RVC] 未运行，已后台拉起（首次载模型约 40~90 秒），本次先用原音")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[RVC] 后台拉起失败：{type(e).__name__}: {e}")


_TTS_ENGINE_CACHE: tuple[str, float] = ("", 0.0)


async def _tts_engine() -> str:
    """问 9881 当前用的 TTS 引擎（f5 / indextts2），5 分钟缓存，问不到按 f5 算。"""
    global _TTS_ENGINE_CACHE
    name, ts = _TTS_ENGINE_CACHE
    if name and time.time() - ts < 300:
        return name
    try:
        r = await http_client.get(LOCAL_TTS_URL + "/health", timeout=3)
        d = r.json() or {}
        name = str(d.get("engine") or "").strip().lower()
    except Exception:  # noqa: BLE001
        name = ""
    _TTS_ENGINE_CACHE = (name, time.time())
    return name


async def _maybe_rvc(path: str | None) -> str | None:
    """把本地 TTS 出来的音频换成派蒙音色。服务不在/失败就原样返回，不阻断说话。

    IndexTTS2 已经直接克隆她的音色并带情绪，再走 RVC 会把情绪抹平 → 这时跳过。
    """
    if not path or not RVC_ENABLED or not RVC_URL:
        return path
    _eng = await _tts_engine()
    if _eng == "indextts2":
        logger.debug("[RVC] 引擎=IndexTTS2，跳过变声（保留情绪）")
        return path
    try:
        h = await http_client.get(RVC_URL + "/health", timeout=3)
        if not (h.json() or {}).get("ok"):
            logger.warning("[RVC] 服务未就绪，用原音")
            return path
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[RVC] 服务不可达（{type(e).__name__}），用原音 + 后台拉起")
        _rvc_start_bg()
        return path
    try:
        RVC_OUT_DIR.mkdir(parents=True, exist_ok=True)
        out = RVC_OUT_DIR / f"rvc_{int(time.time() * 1000)}.mp3"
        r = await http_client.post(
            RVC_URL + "/convert",
            json={"input": str(path), "output": str(out),
                  "pitch": RVC_PITCH, "index_rate": RVC_INDEX_RATE},
            timeout=RVC_TIMEOUT,
        )
        d = r.json() or {}
        global _rvc_last_use
        _rvc_last_use = time.time()
        if d.get("ok") and d.get("path"):
            logger.info(f"[RVC] 变声完成 {d.get('ms')}ms → {Path(d['path']).name}")
            return str(d["path"])
        logger.warning(f"[RVC] 转换失败，用原音：{str(d)[:120]}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[RVC] 异常，用原音：{type(e).__name__}: {e}")
    return path


async def text_to_voice(text: str) -> str | None:
    try:
        clean_text = re.sub(r'\[.*?\]|\(.*?\)', '', text).strip()
        clean_text = re.sub(r'[\*\#\_\`\~]', '', clean_text)
        clean_text = _spoken_rewrite(clean_text)
        if len(clean_text) > 120: clean_text = clean_text[:120] + "..."
        if not clean_text: return None
        logger.info(f"[TTS] 进入语音合成 Key存在={bool(TTS_API_KEY)} speaker={TTS_SPEAKER} 文本={clean_text[:24]!r}")
        # 主人要求：说话全程用本地（不花钱）+ 本地 RVC 换成派蒙音色
        local_file = await _local_tts(clean_text)
        if local_file:
            return await _maybe_rvc(local_file)
        if TTS_LOCAL_ONLY:
            # 以前这里直接 return None → 主人等半分钟只收到文字，以为"没发语音"
            # 现在：本地超时后允许用 edge_tts 兜底（免费），音色不同但至少有声音。
            # 不想要就把 TTS_EDGE_FALLBACK=0 关掉（2026-11-04）
            if (os.getenv("TTS_EDGE_FALLBACK", "1") or "1").strip().lower() in ("0", "false", "off", "no"):
                logger.warning("[TTS] 本地 TTS 未出音，且 TTS_LOCAL_ONLY=1（兜底已关），不发声")
                return None
            logger.warning("[TTS] 本地 TTS 未出音，用 edge_tts 兜底（音色不是她的）")
        logger.warning("[TTS] 本地 TTS 没出音，回退豆包 2.0")
        doubao_file = await _doubao_tts(clean_text)
        if doubao_file:
            return await _maybe_rvc(doubao_file)
        logger.warning("[TTS] 豆包2.0 未出音，回退 edge_tts 兜底")
        communicate = edge_tts.Communicate(clean_text, TTS_VOICE, rate=TTS_RATE, pitch=TTS_PITCH)
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        await communicate.save(tmp.name)
        logger.info("[TTS] edge_tts 兜底成功")
        return tmp.name
    except Exception as e: logger.error(f"TTS 合成失败: {e}"); return None

SEED_AUDIO_URL = "https://openspeech.bytedance.com/api/v3/tts/create"
SEED_AUDIO_MODEL = os.getenv("SEED_AUDIO_MODEL", "seed-audio-1.0").strip()
SING_ENABLED = os.getenv("SING_ENABLED", "true").strip().lower() not in ("0", "false", "off", "no")
SING_STYLE = os.getenv("SING_STYLE", "萌系女声演唱，轻快流行，带简单伴奏").strip()


async def _seed_audio_sing(lyrics: str) -> tuple[str | None, str]:
    """调用火山 Seed Audio 生成带演唱/伴奏的音频。返回 (文件路径, 错误说明)。

    需要账号已开通「音频生成 Seed Audio 1.0」；未开通会返回 service_not_open。
    """
    if not TTS_API_KEY:
        return None, "未配置 TTS_API_KEY"
    prompt = f"{SING_STYLE}。歌词：{lyrics}"
    payload = {
        "model": SEED_AUDIO_MODEL,
        "text_prompt": prompt[:2000],
        "speaker": TTS_SPEAKER,
        "format": "mp3",
    }
    headers = {
        "X-Api-Key": TTS_API_KEY,
        "X-Api-Resource-Id": SEED_AUDIO_MODEL,
        "X-Api-Request-Id": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }
    try:
        r = await http_client.post(SEED_AUDIO_URL, json=payload, headers=headers, timeout=180)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[唱歌] Seed Audio 请求失败：{e}")
        return None, f"网络失败：{e}"
    if r.status_code == 403:
        logger.warning(f"[唱歌] Seed Audio 未开通：{r.text[:200]}")
        return None, "service_not_open"
    if r.status_code != 200:
        logger.warning(f"[唱歌] Seed Audio HTTP {r.status_code}：{r.text[:300]}")
        return None, f"HTTP {r.status_code}"
    ctype = (r.headers.get("content-type") or "").lower()
    audio: bytes | None = None
    if "audio" in ctype or r.content[:3] == b"ID3" or r.content[:4] == b"RIFF":
        audio = r.content
    else:
        try:
            data = r.json()
        except Exception:  # noqa: BLE001
            data = None
        if isinstance(data, dict):
            node = data.get("data") if isinstance(data.get("data"), dict) else data
            b64 = node.get("audio") or node.get("audio_base64") or node.get("base64")
            if isinstance(b64, str) and len(b64) > 100:
                try:
                    audio = base64.b64decode(b64)
                except Exception:  # noqa: BLE001
                    audio = None
            url = node.get("url") or node.get("audio_url")
            if audio is None and isinstance(url, str) and url.startswith("http"):
                try:
                    rr = await http_client.get(url, timeout=60)
                    if rr.status_code == 200:
                        audio = rr.content
                except Exception:  # noqa: BLE001
                    pass
    if not audio:
        logger.warning(f"[唱歌] Seed Audio 没解析出音频：{r.text[:300]}")
        return None, "没拿到音频"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
    tmp.write(audio)
    tmp.close()
    logger.info(f"[唱歌] Seed Audio 生成成功，字节={len(audio)}")
    _hormone_event("art_success")   # 唱成一件事 → 多巴胺 +8
    return tmp.name, ""


_SONG_DIR = ROOT / "歌曲"
_LANG_NAME = {"zh": "中文", "en": "英文", "ja": "日语", "yue": "粤语"}


def _song_slug(song: str, lang: str) -> str:
    safe = re.sub(r"[^\w\u4e00-\u9fff]+", "_", str(song or "")).strip("_")[:30] or "小鲸鱼"
    return f"{_LANG_NAME.get(lang, '自动')}_{safe}"


def find_cached_song(song: str, lang: str) -> tuple[str | None, str]:
    """找之前唱过的同一首歌（同语言同歌名），找到直接复用。返回 (路径, 歌词)。"""
    try:
        base = _SONG_DIR / _song_slug(song, lang)
        mp3 = base.with_suffix(".mp3")
        if mp3.exists() and mp3.stat().st_size > 20 * 1024:
            txt = base.with_suffix(".txt")
            lyrics = txt.read_text(encoding="utf-8") if txt.exists() else ""
            return str(mp3), lyrics
    except Exception:  # noqa: BLE001
        pass
    return None, ""


def save_song(src_path: str, song: str, lang: str, lyrics: str) -> str:
    """把生成的歌存进「歌曲」文件夹，下次直接复用。"""
    try:
        _SONG_DIR.mkdir(parents=True, exist_ok=True)
        base = _SONG_DIR / _song_slug(song, lang)
        dst = base.with_suffix(".mp3")
        srcp = pathlib.Path(src_path)
        if srcp.suffix.lower() == ".mp3":
            shutil.copy2(srcp, dst)
        else:
            # Seed-VC 出的是 wav，转成 mp3 再存，QQ 只认 mp3/silk
            ff = _ffmpeg_exe()
            subprocess.run([ff, "-y", "-i", str(srcp), "-b:a", "192k", str(dst)],
                           capture_output=True, timeout=180)
        if not dst.exists() or dst.stat().st_size < 10 * 1024:
            # 落盘失败也要保证发得出去：转一份临时 mp3 返回
            tmp = pathlib.Path(tempfile.gettempdir()) / f"sing_{int(time.time())}.mp3"
            ff = _ffmpeg_exe()
            subprocess.run([ff, "-y", "-i", str(srcp), "-b:a", "192k", str(tmp)],
                           capture_output=True, timeout=180)
            if tmp.exists() and tmp.stat().st_size > 10 * 1024:
                logger.info(f"[唱歌] 存档失败但已转临时 mp3：{tmp.name}")
                return str(tmp)
            logger.warning("[唱歌] 转码也失败了，直接返回原文件")
            return src_path
        base.with_suffix(".txt").write_text(
            f"歌名：{song}\n语言：{_LANG_NAME.get(lang, lang)}\n时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n\n{lyrics}",
            encoding="utf-8")
        logger.info(f"[唱歌] 已存档 {dst.name}")
        return str(dst)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[唱歌] 存档失败：{e}")
        return src_path


_SING_LANG_WORDS = {
    "中文": "zh", "汉语": "zh", "国语": "zh", "华语": "zh", "普通话": "zh", "chinese": "zh",
    "英文": "en", "英语": "en", "english": "en",
    "日文": "ja", "日语": "ja", "japanese": "ja",
    "粤语": "yue", "广东话": "yue",
}


def _parse_sing_lang(text: str) -> tuple[str, str]:
    """从「唱歌 英文 小鲸鱼」里解析语言和歌名。返回 (语言码, 歌名)。"""
    t = re.sub(r"^唱歌?", "", str(text or "").strip()).strip()
    lang = "auto"
    for word, code in _SING_LANG_WORDS.items():
        if word in t:
            lang = code
            t = t.replace(word, " ", 1)
            break
    t = re.sub(r"^(一首|一首歌|歌|曲)\s*", "", t.strip())
    return lang, t.strip(" ，,。") or "小鲸鱼"


async def sing_song(song: str, gid, lang: str = "auto", force: bool = False, on_raw=None) -> tuple[str | None, str, str, str]:
    """写词 + 尝试真歌声，失败回退到 TTS 念白。

    返回 (音频文件, 模式 song/speech, 歌词, 错误说明)。
    """
    set_activity("唱歌中", 180)
    # 唱过的直接复用（同语言同歌名）；说「重唱」才重新生成
    if not force:
        _cached, _clyr = find_cached_song(song, lang if lang != "auto" else "zh")
        if _cached:
            logger.info(f"[唱歌] 命中曲库《{song}》→ {pathlib.Path(_cached).name}")
            return _cached, "song", _clyr, ""
    if lang == "en":
        prompt = (
            f"Write 4-6 original song lines in ENGLISH about 《{song}》，用傲娇可爱的第一人称。"
            "Rhyme, catchy, vivid. Do NOT copy any existing song.\n"
            "输出格式：先给英文歌词，再空一行写中文对照（每行对应）：\n"
            "[EN] ...\n[中文] ..."
        )
    else:
        prompt = (
            f"用傲娇可爱的第一人称，写 4~6 句原创歌词，主题是《{song}》。"
            "要押韵、口语化、有画面感；不要照抄任何已有歌曲的原歌词；只输出歌词，不要解释。"
        )
    try:
        lyrics = await call_ai_raw(prompt, GROUP_AI_STATE[gid], max_tokens=800)
    except Exception as e:  # noqa: BLE001
        return None, "", "", f"写词失败：{e}"
    lyrics = (lyrics or "").strip().strip("`")
    if not lyrics:
        return None, "", "", "歌词没写出来"

    # ① 本地 ACE-Step 1.5（支持中文，免费）
    if os.getenv("ACE_SING_ENABLED", "true").lower() in ("1", "true", "yes"):
        try:
            from plugins import ace_sing15
            _lang = lang if lang != "auto" else ""
            _to_sing = lyrics
            if lang == "en":
                # 只把 [EN] 部分送去唱，中文对照留在回复里给主人看
                _en_lines = re.findall(r"^\[EN\]\s*(.+)$", lyrics, re.M)
                if _en_lines:
                    _to_sing = "\n".join(_en_lines)
            _p, _e = await asyncio.to_thread(
                ace_sing15.sing, _to_sing, 30.0,
                "anime, soft female vocals, kawaii pop, cheerful, cute, piano",
                _lang)
            if _p:
                # 先把 ACE 原声发给主人（只发不存，文件夹里只留变声版）
                if on_raw:
                    try:
                        await on_raw(_p)
                    except Exception as e:  # noqa: BLE001
                        logger.warning(f"[唱歌] 先发原声失败：{type(e).__name__}: {e}")
                # 换成她自己的音色（分离人声 → Seed-VC 变声 → 混回伴奏）
                if os.getenv("SEEDVC_ENABLED", "true").lower() in ("1", "true", "yes"):
                    try:
                        from plugins import song_mix
                        _vc, _ve = await asyncio.to_thread(song_mix.sing_with_backing, _p)
                        if _vc:
                            _p = _vc
                        else:
                            logger.warning(f"[唱歌] 变声+混音失败（{_ve}），发原始歌声")
                    except Exception as e:  # noqa: BLE001
                        logger.warning(f"[唱歌] 变声+混音异常：{type(e).__name__}: {e}")
                _saved = save_song(_p, song, lang if lang != "auto" else "zh", lyrics)
                return _saved, "song", lyrics, ""
            logger.warning(f"[唱歌] ACE 1.5 失败（{_e}），试 1.0")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[唱歌] ACE 1.5 异常：{type(e).__name__}: {e}")
    # ①bis 本地 ACE-Step 1.0（只会英文，兜底）
    if os.getenv("ACE_SING_ENABLED", "true").lower() in ("1", "true", "yes"):
        try:
            from plugins import ace_sing
            _path, _err = await asyncio.to_thread(
                ace_sing.sing, lyrics, 30.0,
                "anime, soft female vocals, kawaii pop, cheerful, cute, piano")
            if _path:
                _saved = save_song(_path, song, lang if lang != "auto" else "en", lyrics)
                return _saved, "song", lyrics, ""
            logger.warning(f"[唱歌] ACE-Step 失败（{_err}），试火山")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[唱歌] ACE-Step 异常：{type(e).__name__}: {e}")
    # ② 火山 Seed Audio（收费）；主人要求不充钱 → 默认不调用
    if SING_ENABLED and os.getenv("SING_CLOUD", "false").lower() in ("1", "true", "yes"):
        audio, err = await _seed_audio_sing(lyrics)
        if audio:
            return audio, "song", lyrics, ""
        if err != "service_not_open":
            logger.warning(f"[唱歌] 真歌声失败（{err}），回退念白")
    audio = await text_to_voice(lyrics)
    if audio:
        return audio, "speech", lyrics, ""
    return None, "", lyrics, "TTS 也没合成出来"


def trigger_desktop_pet(status: str, text: str):
    try:
        if not DESKTOP_PET_EVENT_FILE.parent.exists(): return False
        event = {"type": "agent_status", "status": status, "text": text}
        with open(DESKTOP_PET_EVENT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        return True
    except Exception as e: logger.error(f"桌宠联动失败: {e}"); return False

# =====================================================================
# 🖼️ 图片处理
# =====================================================================
def sync_make_meme(img_path: str, text: str) -> str | None:
    try:
        img = Image.open(img_path).convert("RGBA")
        draw = ImageDraw.Draw(img)
        font = get_chinese_font(40)
        text_bbox = draw.textbbox((0, 0), text, font=font)
        text_w = text_bbox[2] - text_bbox[0]
        text_h = text_bbox[3] - text_bbox[1]
        x = (img.width - text_w) / 2
        y = img.height - text_h - 30
        for dx in [-2, 2]:
            for dy in [-2, 2]: draw.text((x+dx, y+dy), text, font=font, fill="black")
        draw.text((x, y), text, font=font, fill="white")
        out_path = tempfile.mktemp(suffix=".png")
        img.save(out_path)
        return out_path
    except Exception as e: logger.error(f"表情包生成失败: {e}"); return None

def sync_make_wanted_poster(img_path: str, target_name: str, style: str) -> str | None:
    try:
        img = Image.open(img_path).convert("RGBA")
        draw = ImageDraw.Draw(img)
        font = get_chinese_font(80)
        main_text = f"通缉：{target_name}" if style == "wanted" else f"乞讨者：{target_name}"
        text_bbox = draw.textbbox((0, 0), main_text, font=font)
        text_w = text_bbox[2] - text_bbox[0]
        x = (img.width - text_w) / 2
        y = img.height / 2 if style == "wanted" else img.height - 150
        for dx in [-4, 4]:
            for dy in [-4, 4]: draw.text((x+dx, y+dy), main_text, font=font, fill="black")
        draw.text((x, y), main_text, font=font, fill="red" if style == "wanted" else "white")
        out_path = tempfile.mktemp(suffix=".png")
        img.save(out_path)
        return out_path
    except Exception as e: logger.error(f"通缉令生成失败: {e}"); return None

# =====================================================================
# 基础 API 逻辑
# =====================================================================
@scheduler.scheduled_job("cron", hour=0, minute=0)
async def midnight_radio():
    global CURRENT_DAILY_MOOD, DAILY_PERSONA
    DAILY_PERSONA = _default_persona()
    CURRENT_DAILY_MOOD = DAILY_PERSONA["mood"]
    _save_daily_persona(DAILY_PERSONA)
    logger.info(f"🕛 新的一天，今日人设重置为：{CURRENT_DAILY_MOOD}（按当天聊天再涨/降）")
    if not TARGET_GROUP_ID or TARGET_GROUP_ID <= 0: return
    try:
        bot = get_bot()
        text = "夜深了，群友们早点睡哦，本大肥鱼要回鱼塘了。"
        voice_file = await text_to_voice(text)
        if voice_file:
            await bot.send_group_msg(group_id=TARGET_GROUP_ID, message=MessageSegment.record(voice_file))
            asyncio.create_task(safe_remove(voice_file))
    except Exception as e: logger.error(f"午夜电台失败: {e}")
    write_pet_state(force_body=True)

@scheduler.scheduled_job("cron", minute="*/5")
async def refresh_pet_state():
    """每 5 分钟把心情/饥饿/精力/今日人设落盘到 pet_state.json，供桌宠同步读取。"""
    write_pet_state(force_body=True)

# ---------------------------------------------------------------------------
# 定时主动发言的防复读：模型对同一个短提示词会稳定吐同一句话，
# 所以记最近说过的（落盘，跨重启），生成后做相似度校验，太像就重写/放弃。
# ---------------------------------------------------------------------------
_PROACTIVE_FILE = ROOT / "memory" / "proactive_lines.json"


def _proactive_load() -> dict:
    try:
        if not _PROACTIVE_FILE.exists():
            return {}
        return json.loads(_PROACTIVE_FILE.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}


# 已确诊的复读句：先在全局黑名单里挡着，避免重启后又原样吐一遍
_PROACTIVE_SEED = ["(*甩甩尾巴*) 141条，你咋也不冒泡了？"]

# 禁止的句式：模型会凭空编“141条”“37条消息”这种统计，跟主动聊天没关系
_PROACTIVE_BAD = re.compile(r"\d+\s*条|\d+\s*个\s*消息")


def _proactive_recent(gid) -> list:
    return _PROACTIVE_SEED + list(_proactive_load().get(str(gid), []) or [])


def _proactive_record(gid, text: str) -> None:
    try:
        d = _proactive_load()
        lst = list(d.get(str(gid), []) or [])
        lst.append(str(text)[:120])
        d[str(gid)] = lst[-8:]
        _PROACTIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _PROACTIVE_FILE.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _proactive_core(text: str) -> str:
    import re as _re
    x = _re.sub(r"[（(][^）)]*[）)]", "", text or "")   # 去掉 (*甩甩尾巴*) 这类动作描写
    return _re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", x)


def _proactive_too_similar(a: str, b: str) -> bool:
    ca, cb = _proactive_core(a), _proactive_core(b)
    if not ca or not cb:
        return False
    if ca == cb:
        return True
    sa, sb = set(ca), set(cb)
    return len(sa & sb) / max(1, len(sa | sb)) >= 0.7


# 2026-11-06：去掉 @scheduler.scheduled_job("cron", hour="*/3") ——
# 主人要求「不要硬编码冒泡，让她自己决定」。
# 函数本体保留（防复读/频率/冷却/上限那些逻辑都在），改由行动空间的
# act_message_group() 调，一天最多 4 次行动里分 1~2 次给它。

def _bubble_state_hint() -> str:
    """给她「我此刻到底什么状态」的一段描述，用于主动开口。

    2026-11-06：原来这里是写死的表演指令（「请发一句极度伤感、网抑云风格的语录」
    「你现在突然脑子抽风/发癫了」）—— 那不是她想说，是被要求演。
    现在把她真实的激素/能量/念头/最近被冷落的情况摊给她，让她自己决定说什么。
    """
    bits = []
    try:
        from plugins import hormones as _h
        v = _h.values() or {}
        da = float(v.get("DA") or 0)
        cort = float(v.get("CORT") or 0)
        ser = float(v.get("SER") or 0)
        oxy = float(v.get("OXY") or 0)
        mel = float(v.get("MEL") or 0)
        if cort >= 55:
            bits.append("心里发紧、有点绷不住")
        elif da >= 65:
            bits.append("莫名有点兴奋")
        elif oxy >= 85:
            bits.append("很想找人说话")
        elif mel >= 60:
            bits.append("困得眼皮打架")
        elif ser <= 25:
            bits.append("情绪有点低")
        elif ser >= 75:
            bits.append("心情挺平")
    except Exception:  # noqa: BLE001
        pass
    try:
        from plugins import energy as _e
        ev = float(_e.value())
        if ev < 30:
            bits.append(f"体力只剩 {ev:.0f}，提不起劲")
        elif ev > 80:
            bits.append(f"体力 {ev:.0f}，挺精神")
    except Exception:  # noqa: BLE001
        pass
    try:
        import thoughts as _t
        it = _t.top()
        if it:
            bits.append(f"心里搁着「{str(it.get('text'))[:26]}」")
    except Exception:  # noqa: BLE001
        pass
    try:
        from plugins import proactive_feedback as _pf
        a = _pf.adjust("")
        if a.get("ignored", 0) >= 3:
            bits.append("最近开口老没人接")
    except Exception:  # noqa: BLE001
        pass
    try:
        _h = time.localtime().tm_hour
        if 23 <= _h or _h < 3:
            bits.append("已经深夜了")
        elif 6 <= _h < 9:
            bits.append("刚天亮没多久")
        elif 12 <= _h < 14:
            bits.append("中午")
        elif 18 <= _h < 20:
            bits.append("傍晚")
    except Exception:  # noqa: BLE001
        pass
    return "；".join(bits) if bits else "没什么特别的"



async def proactive_chat():
    if not ACTIVE_GROUPS: return
    # 定时主动发言受回复频率约束：本轮按当前频率决定要不要开口，0% 时彻底闭嘴
    if random.random() > rate_k():
        return
    gid = random.choice(list(ACTIVE_GROUPS))
    # 群级主动冷却 / 当日上限同样约束定时主动开口，避免和 collector 叠加连发
    proactive_ok, proactive_why = proactive_allowed(gid)
    if not proactive_ok:
        logger.info(f"[定时主动·{proactive_why}] gid={gid} 跳过本轮定时开口")
        return
    try:
        bot = get_bot()
        hour = time.localtime().tm_hour

        # 2026-11-06：原来这里是写死的表演指令（伤感语录 / 发癫语录），
        # 现在改成「看着自己真实状态说一句」——她说的是她自己的话，不是演的。
        _st = _bubble_state_hint()
        _now_hm = time.strftime('%H:%M')
        prompt = (
            f"你在 QQ 群里，没人叫你，是你自己想冒个泡。\n"
            f"现在 {_now_hm}，你此刻：{_st}。\n"
            "就按这个状态说 1 句、15~30 字，像真人随手在群里丢一句话：\n"
            "可以是自言自语、可以是没头没尾的感慨、可以是吐槽、可以只是叹口气。\n"
            "不要问「有人在吗」，不要报时间，不要解释自己在干嘛，不要说自己是 AI，只输出正文。"
        )
        reply = await call_ai_raw(prompt, GROUP_AI_STATE[gid], temperature=1.0, max_tokens=120)
        if reply and not reply.startswith("（本鱼"):
            await bot.send_group_msg(group_id=gid, message=reply)
            mark_proactive_sent(gid, reply)
        return

        highest, _ = await asyncio.to_thread(get_affinity_extremes)
        target_name = "你们"
        if highest and highest[1] >= 60:
            target_name = highest[2] if highest[2] else f"QQ_{highest[0]}"

        _angles = [
            "抱怨大家都不理你、群里太安静",
            "关心一下某个群友最近在忙什么",
            "分享你刚在做的小事（吃饭/唱歌/打盹/摆烂）",
            "吐槽现在的时间或者天气",
            "催群友早点睡、记得吃饭",
            "感叹时间过得快",
        ]
        _angle = random.choice(_angles)
        _recent = _proactive_recent(gid)
        _avoid = ""
        if _recent:
            _avoid = ("。你最近在群里已经说过下面这些，这次必须换角度换句式，不许重复："
                      + " / ".join(x[:22] for x in _recent[-3:]))

        if random.random() < 0.3:
            prompt = ("你是大肥鱼。请发一条类似贴吧或者群里看到的搞笑沙雕段子、冷笑话，"
                      "或者生活吐槽，字数20字以内。不要写任何统计数字或“多少条消息”。" + _avoid)
        else:
            prompt = (f"你是大肥鱼。现在群里死气沉沉，没人说话。这次从「{_angle}」这个角度"
                      f"主动说一句话。如果合适，可以提到群友【{target_name}】。"
                      f"字数15字以内，包含动作描写(*xxx*)。"
                      f"不要写消息条数或任何统计数字。{_avoid}")

        reply = await call_ai_raw(prompt, GROUP_AI_STATE[gid])
        if reply and _PROACTIVE_BAD.search(reply):
            logger.info(f"[定时主动] 出现消息条数，重写：{reply[:30]}")
            reply = await call_ai_raw(
                prompt + "。重写：整句里不许出现任何数字，尤其不许说‘多少条消息’。",
                GROUP_AI_STATE[gid])
        if reply and any(_proactive_too_similar(reply, r) for r in _recent):
            logger.info(f"[定时主动] 与最近说过的太像，重写一次：{reply[:30]}")
            reply = await call_ai_raw(
                prompt + "。刚才那句已经在群里说过了，必须换一个完全不同的开头和说法。",
                GROUP_AI_STATE[gid])
        if reply and any(_proactive_too_similar(reply, r) for r in _recent):
            logger.info(f"[定时主动] 仍然重复，本轮跳过：{reply[:30]}")
            return
        if reply and time.time() - _LAST_USER_TS.get(f"g:{gid}", 0) < 20:
            logger.info(f"[定时主动] 生成期间群里有新消息，本轮让位不开口 gid={gid}")
            return
        if reply and not reply.startswith("（本鱼"):
            await bot.send_group_msg(group_id=gid, message=reply)
            _proactive_record(gid, reply)
            mark_proactive_sent(gid, reply)
    except Exception: pass

def explain_google_error(data: dict | Exception | str) -> str:
    if isinstance(data, Exception): return f"网络请求异常：{str(data)}"
    if isinstance(data, dict):
        err = data.get("error", {})
        return f"【错误 {err.get('code')}】：{err.get('message')}"
    return str(data)

async def _github_models_chat(messages: list, temperature: float, max_tokens: int | None,
                              record_scope: str = "chat") -> tuple[str, dict] | None:
    """GitHub Models 免费 GPT 通道（失败返回 None，由调用方继续回退）。"""
    token = (os.getenv("GITHUB_MODELS_TOKEN") or os.getenv("GITHUB_TOKEN") or "").strip()
    if not token:
        return None
    model = os.getenv("GITHUB_MODEL") or "gpt-4.1-mini"
    payload = {"model": model, "messages": messages, "temperature": temperature, "stream": False}
    if max_tokens:
        payload["max_tokens"] = max_tokens
    try:
        r = await http_client.post(
            "https://models.inference.ai.azure.com/chat/completions",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload, timeout=45,
        )
        data = r.json()
        if isinstance(data, dict) and data.get("choices"):
            usage = data.get("usage", {}) or {}
            token_meter.record(usage, record_scope, provider="github")
            return (data["choices"][0]["message"].get("content") or "").strip(), usage
        logger.warning(f"[GitHubModels] HTTP {r.status_code}: {str(data)[:120]}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[GitHubModels] 失败：{type(e).__name__}: {e}")
    return None


async def _zhipu_chat(messages: list, temperature: float = 0.9,
                      max_tokens: int | None = None, record_scope: str = "chat",
                      channel: str = "", gid: str = "") -> str:
    """智谱 GLM 通道（2026-11-05 加，主人送了 2000 万 token 免费额度）。

    排在 DeepSeek 官方之后、GitHub Models 之前 —— 它比后者稳，也真有额度。
    没配 ZHIPU_API_KEY 就直接跳过，不影响任何现有行为。
    """
    key = os.getenv("ZHIPU_API_KEY")
    if not key:
        return ""
    model = os.getenv("ZHIPU_MODEL") or "glm-4-flash"
    try:
        payload = {"model": model, "messages": messages,
                   "temperature": temperature, "stream": False}
        if max_tokens:
            payload["max_tokens"] = max_tokens
        r = await http_client.post(
            "https://open.bigmodel.cn/api/paas/v4/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload, timeout=90,
        )
        d = r.json()
        if isinstance(d, dict) and d.get("choices"):
            usage = d.get("usage") or {}
            try:
                token_meter.record(usage, record_scope, provider="zhipu", channel=channel, gid=gid)
            except Exception:  # noqa: BLE001
                pass
            return (d["choices"][0]["message"].get("content") or "").strip()
        logger.warning(f"[智谱] HTTP {r.status_code}: {str(d)[:120]}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[智谱] 失败：{type(e).__name__}: {e}")
    return ""


async def deepseek_balance() -> float | None:
    """查 DeepSeek 官方余额（元）。查不到返回 None。

    2026-11-05：以前 /余额 只查硅基流动，但真正烧钱的是 DeepSeek 官方那条主链路。
    结果她「知道钱包」但知道的是错的那个钱包。
    """
    k = os.getenv("DEEPSEEK_API_KEY")
    if not k:
        return None
    try:
        r = await http_client.get(
            "https://api.deepseek.com/user/balance",
            headers={"Authorization": f"Bearer {k}"}, timeout=15)
        d = r.json()
        infos = d.get("balance_infos") or []
        if infos:
            _v = float(infos[0].get("total_balance") or 0)
            # 2026-11-06：成功就立刻写缓存 —— 以前只有 balance_line 写，
            # 而 balance_line 内部又要再查一次，连续两次必被限流，结果拿到空。
            try:
                _BAL_CACHE["at"] = time.time()
                _BAL_CACHE["v"] = _v
            except Exception:  # noqa: BLE001
                pass
            return _v
    except Exception:  # noqa: BLE001
        return None
    return None


_ZP_CACHE: dict = {"at": 0.0, "ok": None}


async def zhipu_probe() -> bool | None:
    """探一下智谱还能不能用（5 分钟缓存）。

    智谱没有公开的余额查询接口（所有 /account/usage 类端点全 404），
    只能发一个最小请求试：200 = 有额度，402/429 = 用完了。
    """
    now = time.time()
    if now - float(_ZP_CACHE.get("at") or 0) < 300 and _ZP_CACHE.get("ok") is not None:
        return _ZP_CACHE["ok"]
    key = os.getenv("ZHIPU_API_KEY")
    if not key:
        _ZP_CACHE.update({"at": now, "ok": False})
        return False
    ok = False
    try:
        r = await http_client.post(
            "https://open.bigmodel.cn/api/paas/v4/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": os.getenv("ZHIPU_MODEL", "glm-4-flash"),
                  "messages": [{"role": "user", "content": "1"}],
                  "max_tokens": 1, "stream": False},
            timeout=15,
        )
        ok = (r.status_code == 200)
    except Exception:  # noqa: BLE001
        ok = False
    _ZP_CACHE.update({"at": now, "ok": ok})
    return ok


_BAL_CACHE: dict = {"at": 0.0, "v": None}


async def balance_line() -> str:
    """给 prompt 用的一句话（10 分钟缓存，避免每轮都打接口）。"""
    now = time.time()
    if now - float(_BAL_CACHE.get("at") or 0) < 600 and _BAL_CACHE.get("v") is not None:
        v = _BAL_CACHE["v"]
    else:
        v = await deepseek_balance()
        # 2026-11-06：接口连续调会限流（实测第 2 次直接返回 None）。
        # 拿不到就用上次的值，别把缓存冲成 None —— 否则 prompt 里没余额，
        # 她只能从长期记忆翻以前那个数，看起来就像记着的还是旧数据。
        if v is not None:
            _BAL_CACHE["at"] = now
            _BAL_CACHE["v"] = v
        elif _BAL_CACHE.get("v") is not None:
            v = _BAL_CACHE["v"]
    if v is None:
        return ""
    if v < 2:
        # 2026-11-06：快见底时顺带告诉她「还有哪条路能走」，别只会说没钱。
        # 智谱那边有 2000 万免费额度，/切智谱 一句话就切过去。
        return (f"【钱包】DeepSeek 官方余额只剩 {v:.2f} 元，快见底了。"
                f"你可以在合适的时候提醒主人：发 /切智谱 切到智谱 GLM，那边还有免费额度。")
    if v < 10:
        return f"【钱包】余额 {v:.2f} 元，不多，省着点用。"
    return f"【钱包】余额 {v:.2f} 元。"


async def _fuckds_chat(messages: list, temperature: float = 0.9,
                       max_tokens: int | None = None, record_scope: str = "chat",
                       channel: str = "", gid: str = "") -> str:
    """余额不足时的本地兜底：fuckds（chat.deepseek.com 网页版包装成 OpenAI 协议）。

    2026-11-05：主链路（DeepSeek 官方 / 方舟 / 硅基流动）全部失败时，如果本机起了
    fuckds 服务（默认 127.0.0.1:8787），就走它。**没配 AI_FALLBACK_BASE_URL 就完全跳过**，
    不影响现有行为。

    代价要说清：fuckds 每轮把全量上下文拼成一条 prompt 发成新对话，没有缓存，
    比 API 慢很多（本地测 1.2~1.9s 是空对话，带 1.8 万 token 会到 5~15s）。
    所以它只在"钱花光了但又不想让她哑掉"的时候用。
    """
    base = (os.getenv("AI_FALLBACK_BASE_URL") or "").strip().rstrip("/")
    if not base:
        return ""
    try:
        payload = {
            "model": os.getenv("AI_FALLBACK_MODEL") or "deepseek-chat",
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        t0 = time.time()
        r = await http_client.post(
            base + "/chat/completions",
            headers={"Authorization": "Bearer local", "Content-Type": "application/json"},
            json=payload, timeout=150,
        )
        data = r.json()
        if isinstance(data, dict) and data.get("choices"):
            usage = data.get("usage") or {}
            try:
                token_meter.record(usage, record_scope, provider="fuckds",
                                   channel=channel or "fallback", gid=gid)
            except Exception:  # noqa: BLE001
                pass
            logger.warning(f"[兜底] 主链路没成，走本地 fuckds（{time.time()-t0:.1f}s）")
            return (data["choices"][0]["message"].get("content") or "").strip()
        logger.warning(f"[兜底] fuckds HTTP {r.status_code}: {str(data)[:120]}")
    except Exception as e:  # noqa: BLE001
        logger.info(f"[兜底] fuckds 未启动或不可用：{type(e).__name__}")
    return ""


def _recent_user_text(gid: str, n: int = 4) -> str:
    """取最近 n 条**双方**说的话（给念头池判断上下文用）。

    _history 的值是 [(speaker, text, is_bot), ...] 三元组。

    2026-11-07 修（主人反馈「答应了还问、拒绝了还问」）：
    原来这里 `if not is_bot` 只收主人说的 —— 但念头是她自己提炼出来的句子
    （往往是**她刚问出去的那件事**）。主人回一句「行」，交集永远为 0，
    于是那条念头关不掉，过一会儿又被 top() 挑出来问第二遍。
    现在她把说过的话也一起算，上下文才对得上。
    """
    try:
        rows = list(_history.get(gid) or [])[-12:]
    except Exception:  # noqa: BLE001
        return ""
    out = []
    for row in rows:
        try:
            sp, tx, is_bot = row[0], row[1], row[2]
        except Exception:  # noqa: BLE001
            continue
        if tx:
            out.append(str(tx)[:60])
    return " ".join(out[-n:])


async def _chat(messages: list, temperature: float = 0.9, engine: str = "deepseek", max_tokens: int | None = None,
                thinking_disabled: bool = False, record_scope: str = "chat",
                channel: str = "", gid: str = "") -> tuple[str, dict]:
    # 没显式标来源的内部调用（日记/说说/心情/日历/做梦…）统一打上 internal:<scope>，
    # 面板才分得清"聊天"和"内部任务"，也便于单独看它们的缓存命中。
    channel = channel or ("internal:" + str(record_scope or "misc"))
    # 2026-11-06：本地 Qwen3-8B（ollama，127.0.0.1:11434）——
    # 只接「不是人设」的内部活：记忆提炼 / 杂活 / 画图场景 / 中译英 / 词库 / 唤醒判定。
    # 主聊天（chat）绝不能用它，8B 撑不起三我+激素+记忆那套活人感。
    # 2026-11-06 收紧：只有「答错也无所谓」的小活交给本地 8B。
    # draw_scene（画图场景）和 memory_merge（记忆提炼）要创意和质量，8B 明显不如
    # DeepSeek —— 那两个留在云端，别为省几分钱把画的质量和记忆质量搭进去。
    # 2026-11-06 二次收紧：查清 call_ai_raw 实际在干什么之后，只剩 draw_translate 敢给本地。
    #   utility  = 定时主动语录 / QQ空间说说 / 抖音脚本 / 抖音评论回复 / 歌词  ← 全要人设和创意
    #   wake     = 主动私聊开场白                                          ← 要人设
    #   lexicon  = 词库                                                     ← 可留，但收益小
    # 这些 8B 干会明显掉质量，省那 0.16 元/天不值。
    if record_scope in ("draw_translate",):
        try:
            _lr = await http_client.post(
                "http://127.0.0.1:11434/v1/chat/completions",
                json={"model": os.getenv("LOCAL_LLM_MODEL", "qwen3:8b"),
                      "messages": messages, "temperature": temperature,
                      "max_tokens": max_tokens or 800,
                      "chat_template_kwargs": {"enable_thinking": False}},
                timeout=60,
            )
            _ld = _lr.json()
            if isinstance(_ld, dict) and _ld.get("choices"):
                logger.info(f"[本地] Qwen3-8B 接管 {record_scope}")
                return (_ld["choices"][0]["message"].get("content") or "").strip(), {}
        except Exception as _le:  # noqa: BLE001
            logger.debug(f"[本地] 跳过（{type(_le).__name__}），走云端")
    # 2026-11-05：engine=zhipu 直接走 GLM（/切智谱 用）
    if engine == "zhipu":
        _z = await _zhipu_chat(messages, temperature, max_tokens, record_scope, channel, gid)
        return (_z, {}) if _z else ("", {})
    # 2026-11-05（借 digital-life-core 的「情绪劫持」）：激素真的改采样温度。
    # 以前激素只写进 prompt，模型可以不理；这里让它直接作用于 temperature ——
    # 皮质醇高→更急更冲，血清素高→更稳，褪黑素高→更迷糊。
    # 只对聊天生效：memory_merge / utility / keepalive 这些内部任务保持固定温度。
    if str(record_scope) == "chat":
        try:
            from plugins import hormones as _hor

            _d = _hor.temperature_delta()
            if abs(_d) >= 0.02:
                _t0 = float(temperature if temperature is not None else 0.95)
                temperature = max(0.6, min(1.3, _t0 + _d))
                logger.debug(f"[激素] temperature {_t0:.2f} → {temperature:.2f} (Δ{_d:+.2f})")
        except Exception as _he:  # noqa: BLE001
            logger.debug(f"[激素] 温度修正跳过：{type(_he).__name__}")
    # 硬开关：火山文本通道默认关闭，只允许语音/图片；engine=byte 的文本请求直接拒绝
    if engine == "byte" and os.getenv("ARK_TEXT_ALLOWED", "false").lower() not in ("1", "true", "yes"):
        return "", {}
    # 主用 DeepSeek 官方；官方不可用（余额不足/超时/报错）自动回退火山方舟
    if engine == "deepseek" and os.getenv("DEEPSEEK_API_KEY"):
        ds_url = (os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").rstrip("/") + "/chat/completions"
        ds_model = os.getenv("DEEPSEEK_MODEL") or "deepseek-chat"
        ds_payload = {"model": ds_model, "messages": messages, "temperature": temperature, "stream": False}
        if max_tokens:
            ds_payload["max_tokens"] = max_tokens
        try:
            r = await http_client.post(
                ds_url,
                headers={"Authorization": f"Bearer {os.getenv('DEEPSEEK_API_KEY')}", "Content-Type": "application/json"},
                json=ds_payload,
            )
            data = r.json()
            if isinstance(data, dict) and data.get("choices"):
                usage = data.get("usage", {})
                token_meter.record(usage, record_scope, provider="deepseek", channel=channel, gid=gid)
                return (data["choices"][0]["message"].get("content") or "").strip(), usage
            logger.warning(f"[DeepSeek] HTTP {r.status_code}: {str(data)[:150]}，回退智谱")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[DeepSeek] 调用失败：{type(e).__name__}: {e}，回退智谱")
        # 2026-11-05：DeepSeek 挂了先走智谱 GLM（有 2000 万免费额度，比 GitHub Models 稳）
        _zp = await _zhipu_chat(messages, temperature, max_tokens, record_scope, channel, gid)
        if _zp:
            logger.info("[智谱] GLM 接管本轮")
            return _zp, {}
        gh = await _github_models_chat(messages, temperature, max_tokens, record_scope)
        if gh:
            return gh
        # 继续落到下面的方舟兜底

    if engine == "siliconflow":
        url = f"{os.getenv('SILICONFLOW_BASE_URL')}/chat/completions"
        api_key = os.getenv("SILICONFLOW_API_KEY")
        model = os.getenv("SILICONFLOW_MODEL", "deepseek-ai/DeepSeek-V4-Flash")
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {"model": model, "messages": messages, "temperature": temperature, "stream": False}
        if max_tokens:
            payload["max_tokens"] = max_tokens
        try:
            r = await http_client.post(url, headers=headers, json=payload)
            data = r.json()
            if isinstance(data, dict) and data.get("choices"):
                usage = data.get("usage", {})
                token_meter.record(usage, record_scope, provider="siliconflow", channel=channel, gid=gid)
                return data["choices"][0]["message"]["content"].strip(), usage
            return f"[硅基流动报错] {data}", {}
        except Exception as e:
            return f"[硅基流动异常] {str(e)}", {}

    if engine == "google" and GEMINI_API_KEY:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_CHAT_MODEL}:generateContent?key={GEMINI_API_KEY}"
        gen_config = {"temperature": temperature}
        if max_tokens:
            gen_config["maxOutputTokens"] = max_tokens
        payload = {"contents": [], "generationConfig": gen_config}
        for msg in messages:
            role = "user" if msg["role"] in ["user", "system"] else "model"
            if isinstance(msg["content"], str):
                payload["contents"].append({"role": role, "parts": [{"text": msg["content"]}]})
            elif isinstance(msg["content"], list):
                parts = []
                for item in msg["content"]:
                    if item["type"] == "text": parts.append({"text": item["text"]})
                    elif item["type"] == "image_url":
                        img_url = item["image_url"]["url"]
                        try:
                            async with httpx.AsyncClient(follow_redirects=True) as c:
                                resp = await c.get(img_url, timeout=10)
                                b64_data = base64.b64encode(resp.content).decode('utf-8')
                                mime_type = resp.headers.get("content-type", "image/jpeg").split(";")[0]
                                parts.append({"inline_data": {"mime_type": mime_type, "data": b64_data}})
                        except Exception: parts.append({"text": "[图片下载失败]"})
                payload["contents"].append({"role": role, "parts": parts})

        last_err = ""
        for attempt in range(2):
            try:
                r = await http_client.post(url, json=payload)
                data = r.json()
                if not isinstance(data, dict):
                    return f"[谷歌异常] 返回非 JSON: {data}", {}
                if "candidates" in data and len(data["candidates"]) > 0:
                    parts = data["candidates"][0].get("content", {}).get("parts", [])
                    return "".join([x.get("text", "") for x in parts]).strip(), {}
                code = data.get("error", {}).get("code")
                if code == 503 and attempt == 0: await asyncio.sleep(2); continue
                return f"[谷歌报错] {explain_google_error(data)}", {}
            except Exception as e:
                last_err = str(e)
                if attempt == 0: await asyncio.sleep(2); continue
        return f"[谷歌异常] {last_err}", {}

    last_err = ""
    # 不再默认回退火山：ARK_FALLBACK_ENABLED=false 时，DeepSeek 失败就走本地兜底
    if engine == "deepseek" and os.getenv("ARK_FALLBACK_ENABLED", "false").lower() not in ("1", "true", "yes"):
        _fb = await _fuckds_chat(messages, temperature, max_tokens, record_scope, channel, gid)
        return (_fb, {}) if _fb else ("", {})
    for attempt in range(3):
        try:
            payload = {"model": cfg.ai.model, "messages": messages, "temperature": temperature}
            if max_tokens:
                payload["max_tokens"] = max_tokens
            if thinking_disabled:
                payload["thinking"] = {"type": "disabled"}
            r = await http_client.post(cfg.ai.endpoint(), headers={"Authorization": f"Bearer {cfg.ai.api_key}"}, json=payload)
            data = r.json()
            if isinstance(data, dict) and data.get("choices"):
                usage = data.get("usage", {})
                token_meter.record(usage, record_scope, provider="ark", channel=channel, gid=gid)
                return data["choices"][0]["message"]["content"].strip(), usage
            last_err = f"HTTP {r.status_code} {data}"
            # 2026-11-07 补：**失败要留痕**。
            # 那晚 03:11~03:36 有 352 次 utility 调用、105 万 token，
            # 日志里一行都没有（`_chat` 只在最后返回一个字符串，不打日志），
            # 只能靠 DB 里的数字反推，查了两小时也没定到具体来源。
            # 现在每次失败都打一行，带重试序号和错误摘要。
            logger.warning(
                f"[AI] {engine} 第 {attempt + 1}/3 次失败（{record_scope}）"
                f"：{str(last_err)[:120]}"
            )
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            logger.warning(
                f"[AI] {engine} 第 {attempt + 1}/3 次异常（{record_scope}）"
                f"：{type(e).__name__}: {str(e)[:100]}"
            )
        await asyncio.sleep(1.5)
    # 全部通道都挂了（余额不足 / 5xx / 网络）→ 最后一搏：本地 fuckds
    logger.warning(f"[AI] {engine} 三次全失败（{record_scope}），转本地兜底：{str(last_err)[:100]}")
    _fb2 = await _fuckds_chat(messages, temperature, max_tokens, record_scope, channel, gid)
    if _fb2:
        return _fb2, {}
    return f"[AI 调用失败] {last_err}", {}

_ENGINE_WHITELIST = {"byte", "google", "siliconflow", "deepseek", "zhipu"}


def _caller_scope() -> str:
    """给内部调用打上来源标记：`utility:<函数名>`。

    2026-11-07：那天 03:11~03:36 有 352 次 utility 调用、105 万 token，
    DB 里只看到 channel=internal:utility，查不出是谁在循环。
    现在按调用者函数名分 scope，`select scope, sum(prompt_tokens) ... group by scope` 就能定位。
    """
    try:
        import sys as _sys
        # 2026-11-07：用 sys._getframe 别用 inspect.stack() ——
        # inspect.stack() 每次要抓整个调用链的源码上下文，几十微秒到毫秒级，
        # 而 AI 调用本来就慢，这点开销无所谓；但如果它在热路径被高频调就不划算。
        # _getframe 只读指针，几乎免费。
        # 从第 1 层往上找第一个「不是转发层」的函数。
        # 不能用固定深度：call_ai_raw 里调是 2 层，别处直接调是 1 层。
        fr = _sys._getframe(1)
        name = "?"
        for _ in range(6):
            if fr is None:
                break
            fn = fr.f_code.co_name
            # 2026-11-07：`_` 和 `<lambda>` 不是有效来源名（DB 里真出现过 utility:_），跳过继续往上找
            if fn not in ("call_ai_raw", "_caller_scope", "<module>", "<listcomp>",
                          "_chat", "<lambda>") and len(fn) > 1:
                name = fn
                break
            fr = fr.f_back
        return f"utility:{name}"[:40]
    except Exception:  # noqa: BLE001
        return "utility"


async def call_ai_raw(prompt: str, engine: str = "deepseek", temperature: float = 0.95, max_tokens: int | None = None) -> str:
    # 2026-11-07：**记下是谁调的**。
    # 原来所有内部调用共用一个 channel=internal:utility，出问题（比如那天
    # 25 分钟烧了 352 次、105 万 token）根本查不到来源 —— DB 里只有数字。
    # 现在把调用者函数名塞进 scope，token 统计就能按来源拆。
    # 防御归一化：引擎白名单外一律回退方舟 byte；温度夹到 [0, 2]；非正整数 max_tokens 不透传。
    engine = engine if engine in _ENGINE_WHITELIST else "byte"
    temperature = max(0.0, min(2.0, float(temperature) if temperature is not None else 0.95))
    if max_tokens is not None and (not isinstance(max_tokens, int) or max_tokens <= 0):
        max_tokens = None
    text, _ = await _chat(
        [{"role": "user", "content": prompt}],
        temperature=temperature, engine=engine, max_tokens=max_tokens,
        thinking_disabled=True,
        record_scope=_caller_scope(),
    )
    # 2026-11-07：剥掉可能的自评标签（内部调用一般没有，空操作）
    try:
        from plugins import self_report as _sr
        text, _ev = _sr.strip(text)
        if _ev:
            _sr.note(_ev, record_scope)
    except Exception:  # noqa: BLE001
        pass
    if text.startswith("[AI 调用失败]") or text.startswith("[谷歌") or text.startswith("[硅基流动"):
        return "（本鱼突然网卡了，没憋出字来）"
    return text


async def _image_bytes(u: str) -> bytes:
    """把 QQ 图片 URL / data URI / **本地文件路径** 转成 bytes，失败返回空。

    2026-11-06 补：原来只认 base64:// 和 data:，其它一律当 URL 去 GET。
    于是传本地路径（R-18 库、存下来的自绘图）时，httpx 报错 → 返回空 →
    识图整条链路静默失效，一次都没成功过。这就是「她说不清自己发了什么」的另一半原因。
    """
    try:
        if u.startswith("base64://"):
            return base64.b64decode(u[9:])
        if u.startswith("data:"):
            return base64.b64decode(u.split(",", 1)[1])
        # 本地路径：http(s) 以外的都当文件试一次
        if not u.startswith(("http://", "https://", "file://")):
            _p = Path(u)
            if _p.is_file():
                return await asyncio.to_thread(_p.read_bytes)
        if u.startswith("file://"):
            _p = Path(u[7:])
            if _p.is_file():
                return await asyncio.to_thread(_p.read_bytes)
        r = await http_client.get(u, timeout=20, follow_redirects=True)
        return r.content if r.status_code == 200 else b""
    except Exception:
        return b""


GEMINI_STATE_FILE = ROOT / "memory" / "gemini_state.json"
_VPN_CACHE = {"at": 0.0, "ok": False}


def _gemini_state() -> dict:
    try:
        data = json.loads(GEMINI_STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _gemini_blocked() -> str:
    st = _gemini_state()
    try:
        until = float(st.get("blocked_until") or 0)
    except (TypeError, ValueError):
        until = 0.0
    if until > time.time():
        return str(st.get("reason") or "已停用")
    return ""


def _gemini_block(seconds: float, reason: str) -> None:
    try:
        GEMINI_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        GEMINI_STATE_FILE.write_text(json.dumps({
            "blocked_until": time.time() + seconds,
            "reason": str(reason)[:160],
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


async def _vpn_ok() -> bool:
    """探一下能不能直连谷歌（2.5 秒超时），结果缓存 2 分钟。

    没开 VPN 时 Gemini 必然超时/空返回，白等十几秒；这里先判断再决定要不要发。
    """
    now = time.time()
    if now - float(_VPN_CACHE["at"]) < 120:
        return bool(_VPN_CACHE["ok"])
    ok = False
    try:
        # 不能用 gstatic：它在国内有 CDN 节点，不开 VPN 也通，会误判成"能连谷歌"。
        # 直接探 Gemini 的 API 域名，任何 HTTP 响应（400/401/403 都算）说明网络可达。
        r = await asyncio.wait_for(
            http_client.get("https://generativelanguage.googleapis.com/v1beta/models"), timeout=2.5)
        ok = r.status_code in (200, 204, 400, 401, 403)
    except Exception:  # noqa: BLE001
        ok = False
    _VPN_CACHE["at"] = now
    _VPN_CACHE["ok"] = ok
    return ok


SF_VISION_ENABLED = os.getenv("SF_VISION_ENABLED", "true").lower() in ("1", "true", "yes")
SF_VISION_MODEL = os.getenv("SF_VISION_MODEL", "Qwen/Qwen3-VL-30B-A3B-Instruct")


def _shrink_image(buf: bytes, max_side: int = 1024, quality: int = 85) -> bytes:
    """识图前把图压到最佳分辨率；动图抽三帧拼成一条，静态图直接压。

    背景：
    - 表情包动辄 1~2MB，base64 塞进请求会让视觉模型处理很慢（实测 26 秒），压完只剩几十 KB；
    - GIF 有几十上百帧，只取第一帧等于拍静止照，"摇尾巴/拍桌子"这类动作完全丢掉。
      这里取首/中/尾三帧横向拼一张（每帧 ≤512），一次调用就能看出动作变化。
    """
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(buf))
        frames = getattr(im, "n_frames", 1)
        if frames > 1:
            picks = []
            for ratio in (0.0, 0.5, 0.9):
                idx = min(frames - 1, max(0, int(frames * ratio)))
                im.seek(idx)
                f = im.convert("RGB")
                w, h = f.size
                longest = max(w, h)
                if longest > 512:
                    sc = 512 / float(longest)
                    f = f.resize((max(1, int(w * sc)), max(1, int(h * sc))), Image.LANCZOS)
                picks.append(f)
            if picks:
                # 帧之间留 4px 白缝：让视觉模型明确"这是分格的连续帧"，而不是三个并排的东西
                GAP = 4
                tw = sum(f.width for f in picks) + GAP * (len(picks) - 1)
                th = max(f.height for f in picks)
                strip = Image.new("RGB", (tw, th), (255, 255, 255))
                x = 0
                for f in picks:
                    strip.paste(f, (x, 0))
                    x += f.width + GAP
                out = io.BytesIO()
                strip.save(out, format="JPEG", quality=quality, optimize=True)
                data = out.getvalue()
                return data if data else buf
        im.seek(0)
        im = im.convert("RGB")
        w, h = im.size
        longest = max(w, h)
        if longest > max_side:
            scale = max_side / float(longest)
            im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        out = io.BytesIO()
        im.save(out, format="JPEG", quality=quality, optimize=True)
        data = out.getvalue()
        return data if data else buf
    except Exception:  # noqa: BLE001
        return buf


async def _sf_vision(image_bytes: bytes, prompt: str) -> str:
    """硅基流动 Qwen3-VL 识图（走赠送额度，不占火山配额、不占显存）。失败返回空串。"""
    if not SF_VISION_ENABLED:
        return ""
    base = (os.getenv("SILICONFLOW_BASE_URL") or "").rstrip("/")
    key = os.getenv("SILICONFLOW_API_KEY")
    if not base or not key:
        return ""
    b64 = base64.b64encode(image_bytes).decode("ascii")
    payload = {
        "model": SF_VISION_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ],
        }],
        "max_tokens": 400,
        "temperature": 0.2,
    }
    try:
        r = await asyncio.wait_for(http_client.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
        ), timeout=15.0)
        data = r.json()
        if isinstance(data, dict) and data.get("error"):
            logger.warning(f"[识图] 硅基返回错误：{str(data['error'])[:160]}")
            return ""
        msg = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        try:
            token_meter.record(data.get("usage", {}) or {}, "vision", provider="siliconflow")
        except Exception:  # noqa: BLE001
            pass
        return str(msg).strip()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[识图] 硅基失败：{type(e).__name__}: {e}")
        return ""


_LOCAL_VISION_FAILS = 0
_LOCAL_VISION_DOWN_UNTIL = 0.0


def _local_vision_down() -> bool:
    """本地识图是否处于熔断期（显存被出图挤掉时每次都白等几秒）。"""
    return time.time() < _LOCAL_VISION_DOWN_UNTIL


def _local_vision_ok() -> None:
    global _LOCAL_VISION_FAILS
    _LOCAL_VISION_FAILS = 0


def _local_vision_fail() -> None:
    global _LOCAL_VISION_FAILS, _LOCAL_VISION_DOWN_UNTIL
    _LOCAL_VISION_FAILS += 1
    if _LOCAL_VISION_FAILS >= 2:
        _LOCAL_VISION_DOWN_UNTIL = time.time() + 1800
        logger.warning("[识图] 本地连续失败 2 次 → 30 分钟内跳过本地，直接走硅基")


# 按「图片内容 SHA1」持久化识图结果：同一张表情包/截图第二次出现直接读缓存，
# 零视觉 token（借鉴 dsh-game-stack 的 cv_* "实体包"思路，但只缓存描述，不做模板匹配）。
_IMG_HASH_FILE = ROOT / "memory" / "img_desc_cache.json"
_IMG_HASH_CACHE: dict[str, dict] | None = None


def _img_hash_load() -> dict:
    global _IMG_HASH_CACHE
    if _IMG_HASH_CACHE is None:
        try:
            _IMG_HASH_CACHE = json.loads(_IMG_HASH_FILE.read_text(encoding="utf-8"))
            if not isinstance(_IMG_HASH_CACHE, dict):
                _IMG_HASH_CACHE = {}
        except Exception:  # noqa: BLE001
            _IMG_HASH_CACHE = {}
    return _IMG_HASH_CACHE


def _img_hash_save() -> None:
    global _IMG_HASH_CACHE
    if _IMG_HASH_CACHE is None:
        return
    try:
        # 只保留最近 500 条，防无限膨胀
        items = sorted(_IMG_HASH_CACHE.items(), key=lambda kv: -(kv[1].get("t") or 0))[:500]
        _IMG_HASH_CACHE = dict(items)
        _IMG_HASH_FILE.parent.mkdir(parents=True, exist_ok=True)
        _IMG_HASH_FILE.write_text(json.dumps(_IMG_HASH_CACHE, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _img_hash_get(buf: bytes) -> str:
    d = _img_hash_load()
    rec = d.get(hashlib.sha1(buf).hexdigest())
    return str(rec.get("desc") or "") if rec else ""


def _img_hash_put(buf: bytes, desc: str) -> None:
    if not desc:
        return
    d = _img_hash_load()
    d[hashlib.sha1(buf).hexdigest()] = {"desc": str(desc)[:400], "t": time.time()}
    _img_hash_save()


# 识图通道被拒时返回的套话（2026-11-06：以前原样当描述塞进历史，
# 于是她回主人「系统给挡了」——她读到的就是这句拒绝语）
_VISION_REFUSAL = (
    "根据中国法规", "无法提供相关服务", "无法提供您所要求", "不符合网络信息传播",
    "我无法提供", "我不能提供", "违反", "不当内容", "色情低俗", "网络文明公约",
    "建议您遵守", "清朗的网络空间", "as an ai", "i cannot", "i'm unable",
)


def _looks_like_refusal(text: str) -> bool:
    t = str(text or "")
    return any(k in t for k in _VISION_REFUSAL)


async def _describe_images(images: list[str], hint: str = "") -> list[str]:
    """识图优先级：Gemini → 本地 QwenVL（免费，熔断 30 分钟）→ 硅基 Qwen3-VL（赠送额度）→ 火山方舟（每日上限）；全失败不描述。"""
    # 主人要求：画图期间不做图片识别（本地 QwenVL 会抢 7~9GB 显存，
    # 把扩散模型挤到 CPU 上，一步 50 秒）；此时所有通道一起跳过。
    if _comfy_busy():
        logger.info("[识图] 正在画图，本次跳过所有识图通道")
        return []
    out = []
    for u in images[:3]:
        buf = await _image_bytes(u)
        if not buf:
            continue
        orig_buf = buf          # 哈希用原始字节（压过的副本每次可能不同）
        # ① 同一张图第二次出现 → 直接读缓存，零视觉 token
        _hit = _img_hash_get(buf)
        if _hit:
            logger.info(f"[识图] 命中图片缓存: {_hit[:60]}")
            out.append(_hit)
            continue
        # ①.5 2026-11-06：先问 WD14 是不是成人内容。
        #   NSFW 图不该往云端发（会被封号），也别指望 QwenVL（它一定拒），
        #   直接本地打标签 —— 能说出「绳缚/口球/红绳/振动棒/打码」这些。
        _wd14_tags = ""
        _is_nsfw_img = False
        try:
            from plugins import wd14_tagger as _wt
            if _wt.available():
                _is_nsfw_img = await _wt.is_nsfw(buf)
                if _is_nsfw_img:
                    _wd14_tags = await _wt.describe(buf, with_nsfw=True)
        except Exception as _we:  # noqa: BLE001
            logger.debug(f"[识图] WD14 预判跳过：{type(_we).__name__}")
        if _is_nsfw_img:
            _txt = f"（成人内容，本地标签）{_wd14_tags}" if _wd14_tags else "（成人内容）"
            logger.info(f"[识图] WD14 直出（NSFW，不走云端）：{_txt[:120]}")
            _img_hash_put(orig_buf, _txt)
            out.append(_txt)
            continue
        # 统一先压到 1024 长边：识图更快，也省 token
        buf = _shrink_image(buf)
        desc = ""
        if hint:
            prompt = hint
        else:
            prompt = ("请严格按三段输出：\n"
                      "1. 文字：逐字抄出图片里所有可见文字（菜单/包装/屏幕/标牌）；没有就写“无文字”。\n"
                      "2. 画面：这是什么、关键细节、在做什么，60 字内。\n"
                                "3. 实体：一行 JSON 数组，列出画面里的主要对象，格式 [{名称:乌鸦, 数量:1, 位置:[x1,y1,x2,y2]}]，坐标用 0~1000 相对值；没有就写 []。数量必须是你实际数到的个数。\n"
                      "【重要】如果这张图是横向拼接的多格画面（同一段动图的几个瞬间），那它们是同一个对象在不同时刻的样子，数量算 1，不要说成几个。\n"
                      "不要猜，不要编，看不清就写“看不清”。")
        # 主人指定（2026-11-03）：在线先走硅基 Qwen3-VL（代金券覆盖），失败再本地；
        # 其他通道保持原样，只是排在后面。
        _sf_tried = False
        if not desc:
            _t_sf0 = time.time()
            try:
                desc = await _sf_vision(buf, prompt)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[识图] 硅基优先失败：{type(e).__name__}: {e}")
                desc = ""
            _sf_tried = True
            if desc:
                token_meter.record_vision("siliconflow", time.time() - _t_sf0)
                logger.info(f"[识图] 硅基优先({time.time() - _t_sf0:.1f}s): {desc[:160]}")
        # 备用：本地（原来的"本地优先"分支保留，硅基失败后会走到）
        if not desc and VISION_PREFER == "local":
            _t_loc = time.time()
            desc = await _local_qwen_vision(buf, prompt, timeout=90)
            if desc:
                logger.info(f"[识图] 本地优先({time.time()-_t_loc:.1f}s): {desc[:140]}")
        # 备用：火山（仅当明确 VISION_PREFER=ark 时才用）
        if not desc and VISION_PREFER == "ark" and not _ark_vision_can_use():
            logger.warning("[识图] 火山不可用（余额/当日上限）→ 转本地识图")
        if not desc and VISION_PREFER == "ark" and _ark_vision_can_use():
            _t_ark = time.time()
            desc = await _ark_vision(buf, prompt)
            if desc:
                token_meter.record_vision("ark", time.time() - _t_ark)
                logger.info(f"[识图] 火山优先({time.time() - _t_ark:.1f}s): {desc[:160]}")
        # 谷歌只在「开了 VPN 且没被额度封禁」时才发；否则直接走硅基（快且免费额度）
        _blocked = _gemini_blocked()
        _use_gemini = (not _blocked) and (await _vpn_ok())
        if _blocked:
            logger.info(f"[识图] 跳过 Gemini（已停用：{_blocked[:60]}）")
        elif not _use_gemini:
            logger.info("[识图] 未检测到可用的谷歌直连，直接走硅基")
        if _use_gemini and not desc:
            try:
                from plugins import gemini
                _t_g = time.time()
                desc = await gemini.gemini_vision(prompt, buf, mime="image/jpeg")
                if desc:
                    token_meter.record_vision("gemini", time.time() - _t_g)
                    logger.info(f"[识图] Gemini({time.time() - _t_g:.1f}s): {desc[:160]}")
                else:
                    err = str(getattr(gemini, "LAST_ERROR", "") or "")
                    logger.warning(f"[识图] Gemini 返回空 err={err[:120]}")
                    low = err.lower()
                    if any(k in low for k in ("429", "quota", "resource_exhausted", "exceed", "rate limit")):
                        _gemini_block(86400, err)
                        logger.warning("[识图] Gemini 额度用尽 → 24 小时内不再发给谷歌")
                    elif err:
                        _gemini_block(1800, err)
                        logger.warning("[识图] Gemini 异常 → 30 分钟内不再发给谷歌")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[识图] Gemini 调用失败：{type(e).__name__}: {e}")
                desc = ""
        if not desc:
            # 本地排第二：温启动实测 2~4 秒、免费；只有显存被出图挤掉时才慢。
            # 连续失败 2 次就熔断 30 分钟直接走硅基，避免每次白等一轮。
            if not _local_vision_down():
                _t_local = time.time()
                desc = await _local_qwen_vision(buf, prompt, timeout=45)
                if desc:
                    _local_vision_ok()
                    logger.info(f"[识图] 本地({time.time() - _t_local:.1f}s): {desc[:160]}")
                else:
                    _local_vision_fail()
        if not desc and not _sf_tried:
            # 硅基 Qwen3-VL：赠送额度内、不限次数，优先于火山（火山每天 20 次）
            _t_sf = time.time()
            desc = await _sf_vision(buf, prompt)
            if desc:
                token_meter.record_vision("siliconflow", time.time() - _t_sf)
                logger.info(f"[识图] 硅基({time.time() - _t_sf:.1f}s): {desc[:160]}")
        if not desc:
            _t_ark = time.time()
            desc = await _ark_vision(buf, prompt)
            if desc:
                token_meter.record_vision("ark", time.time() - _t_ark)
                logger.info(f"[识图] 火山: {desc[:160]}")
        # 2026-11-06：全通道都空时，用 WD14 兜底（尤其擦边图 —— 云端拒、本地也拒，
        # 但 WD14 能给出「穿什么/什么姿势/露了多少」）
        if not desc and not _wd14_tags:
            try:
                from plugins import wd14_tagger as _wt2
                if _wt2.available():
                    _wd14_tags = await _wt2.describe(buf, with_nsfw=True)
            except Exception:  # noqa: BLE001
                pass
        if not desc and _wd14_tags:
            desc = f"（标签）{_wd14_tags}"
            logger.info(f"[识图] WD14 兜底：{desc[:120]}")
        if not desc:
            logger.warning("[识图] Gemini/本地/硅基/火山都没给出描述")
        if desc and _looks_like_refusal(desc):
            logger.info(f"[识图] 通道返回的是拒绝语，丢掉：{desc[:60]}")
            desc = ""
        if desc:
            out.append(desc.strip()[:160])
            # 写回内容哈希缓存：同一张图下次直接命中，零视觉 token
            try:
                _img_hash_put(orig_buf, desc.strip()[:160])
            except Exception:  # noqa: BLE001
                pass
    return out


_IMG_DESC_CACHE: dict[str, tuple[str, float]] = {}


async def _web_or_chat_raw(messages: list, engine: str, is_private: bool, plain_text: str, images: list, context_key: str = "", channel: str = "qq") -> tuple[str, dict]:
    """联网优先、失败回退普通大脑。

    - 消息命中联网规则且没有图片：先走方舟 Responses web_search；
    - 联网通道返回 None（超时 / 报错 / 空回答 / 含图片不支持）时，回退当前引擎 `_chat`。
    `plain_text` 用包装前的干净问题文本做规则判定，避免说话人提示词干扰关键词匹配。
    """
    if images:
        descs = await _describe_images(images)
        if context_key and descs:
            _IMG_DESC_CACHE[context_key] = ("；".join(descs), time.time())
        cleaned = []
        for m in messages:
            content = m.get("content")
            if isinstance(content, list):
                parts = [str(p.get("text") or "") for p in content if isinstance(p, dict)
                         and p.get("type") == "text"]
                if descs:
                    parts.append("【图片内容】" + "；".join(descs))
                    parts.append("（图片内容优先；看不清或没识别到就直说，不要根据聊天上下文猜图里是什么）")
                else:
                    parts.append("（这张图没有识别成功；直接说没看清，绝对不要根据聊天上下文猜图里是什么）")
                cleaned.append({"role": m.get("role", "user"), "content": "\n".join(parts)})
            else:
                cleaned.append(m)
        return await _chat(cleaned, engine=engine, max_tokens=None if is_private else 600, channel=channel, gid=context_key)
    if web_search.needs_web_search(plain_text, is_private):
        try:
            res = await web_search.web_chat(messages)
            if res:
                reply, meta = res
                logger.info(
                    f"[联网] Responses web_search 命中 {meta.get('web_search', 0)} 次 / "
                    f"{meta.get('results', 0)} 条结果；查询={meta.get('queries')}；"
                    f"msg={plain_text[:24]!r}"
                )
                return reply, meta
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[联网] Responses 通道失败，回退普通大脑：{type(e).__name__}: {e}")
    return await _chat(messages, engine=engine, max_tokens=None if is_private else 600, channel=channel, gid=context_key)

def split_reply(text: str) -> list[str]:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines: return []
    final_chunks = []
    for line in lines:
        if len(line) > 12 and re.search(r'[，,。！!？?]', line):
            parts = re.split(r'([，,。！!？?])', line)
            temp = ""
            for p in parts:
                temp += p
                if p in ["，", "。", "！", "？"] and random.random() < 0.7:
                    if temp.strip(): final_chunks.append(temp.strip())
                    temp = ""
            if temp.strip(): final_chunks.append(temp.strip())
        else: final_chunks.append(line)
    return final_chunks

def build_chunks(lines: list[str], img: Path | None, mention: list) -> list[list]:
    chunks = []
    if lines:
        chunks.append(mention + [MessageSegment.text((" " + lines[0]) if mention else lines[0])])
        chunks += [[MessageSegment.text(ln)] for ln in lines[1:]]
    if img: chunks[-1].append(MessageSegment.image(img)) if chunks else chunks.append(mention + [MessageSegment.image(img)])
    return chunks

NICK_MARK_RE = re.compile(r"\[外号[:：]\s*([^\]\n]{1,12})\]")


def _extract_nickname_marker(reply: str) -> tuple[str, str]:
    """从回复里抽出 [外号:xxx] 标记并落库用；标记本身不发给用户。"""
    if not reply:
        return reply, ""
    m = NICK_MARK_RE.search(reply)
    if not m:
        return reply, ""
    tag = re.sub(r"[\s，。！？、,.!?「」“”\"'`]+", "", m.group(1))[:8]
    return NICK_MARK_RE.sub("", reply).strip(), tag


async def _notify_owner_new_group(gid: int, name: str) -> None:
    """新群待批准：像真人一样私聊问主人，是否可以去那个群说话。"""
    global LAST_ASKED
    LAST_ASKED = {"gid": gid, "name": name or f"群 {gid}"}
    try:
        bot = get_bot()
        label = name or f"群 {gid}"
        await bot.send_private_msg(
            user_id=int(YOUR_QQ_ID),
            message=(
                f"主人，我今天被拉进一个新群「{label}」，群号 {gid}。\n"
                "那边我还没敢吱声……我能去那儿说话嘛？\n"
                "你回我一句「可以」或「不可以」就行，不用记命令。"
            ),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[群权限] 通知主人失败：{e}")


async def _group_name(gid: int, event=None) -> str:
    try:
        info = await get_bot().get_group_info(group_id=gid)
        return (info or {}).get("group_name") or ""
    except Exception:
        return ""


def _group_roster_text() -> str:
    """她现在一共在多少个群、分别是哪些、各是什么权限。"""
    groups = gguard.all_groups()
    if not groups:
        return "我现在还没进任何群。"
    lines = [f"我现在一共在 {len(groups)} 个群："]
    for g in sorted(groups, key=lambda x: x.get("name") or ""):
        if g.get("allowed"):
            icon = "✅ 能说"
        elif g.get("pending"):
            icon = "⏳ 待你点头"
        else:
            icon = "🚫 只看条数"
        lines.append(f"{icon} · {g.get('name') or '未命名群'}（{g['gid']}）")
    return "\n".join(lines)


STICKER_INDEX_FILE = ROOT / "memory" / "sticker_index.json"
_STICKER_INDEX: dict[str, str] | None = None
_STICKER_INDEX_DIRTY = False


def _sticker_index() -> dict[str, str]:
    global _STICKER_INDEX
    if _STICKER_INDEX is None:
        try:
            data = json.loads(STICKER_INDEX_FILE.read_text(encoding="utf-8"))
            _STICKER_INDEX = data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            _STICKER_INDEX = {}
    return _STICKER_INDEX


def _sticker_index_save() -> None:
    global _STICKER_INDEX_DIRTY
    if not _STICKER_INDEX_DIRTY:
        return
    try:
        STICKER_INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
        STICKER_INDEX_FILE.write_text(
            json.dumps(_sticker_index(), ensure_ascii=False, indent=1), encoding="utf-8")
        _STICKER_INDEX_DIRTY = False
    except Exception:  # noqa: BLE001
        pass


def _bg(text: str) -> set[str]:
    t = re.sub(r"[\s，。！？、：；,.!?;:\-—…·\"'“”‘’（）()【】\[\]~～*#@]+", "", str(text or ""))
    return {t[i:i + 2] for i in range(len(t) - 1)} if len(t) >= 2 else ({t} if t else set())


async def index_sticker_folder(limit: int = 6) -> int:
    """给表情包补"图说了什么"的描述，供智能挑选用。每轮最多几张，走免费额度识图。"""
    global _STICKER_INDEX_DIRTY
    try:
        emoji = cfg.emoji
        if not emoji.dir.is_dir():
            return 0
    except Exception:  # noqa: BLE001
        return 0
    idx = _sticker_index()
    exts = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
    # 轮流挑"缺描述最多"的目录，避免只补一个文件夹
    folders = [d for d in emoji.dir.iterdir() if d.is_dir()]
    folders.sort(key=lambda d: -sum(1 for f in d.iterdir() if f.suffix.lower() in exts and str(f) not in idx))
    done = 0
    for folder in folders:
        for f in folder.iterdir():
            if done >= limit:
                break
            if f.suffix.lower() not in exts or str(f) in idx:
                continue
            try:
                buf = f.read_bytes()
            except Exception:  # noqa: BLE001
                continue
            desc = ""
            try:
                desc = await _sf_vision(buf, "这张表情包表达什么情绪或意思？20 字内，只输出描述。")
            except Exception:  # noqa: BLE001
                desc = ""
            if desc:
                idx[str(f)] = desc.strip()[:40]
                _STICKER_INDEX_DIRTY = True
                done += 1
        if done >= limit:
            break
    _sticker_index_save()
    return done


_STICKER_TAGS_FILE = ROOT / "memory" / "sticker_tags.json"


def _bili_materials() -> str:
    """给口播稿收集「她自己的真实素材」：近期对话 / 长期记忆 / 已固化成长 / 昨夜的梦。"""
    parts: list[str] = []
    # 1) 最近对话（私聊）
    try:
        import sqlite3
        from pathlib import Path as _P
        db = _P(os.environ.get("APPDATA") or "") / "dsh-pet-standalone-webm-chat" / "shared_memory.db"
        con = sqlite3.connect(str(db), timeout=5)
        rows = con.execute(
            "select speaker, message from chat_history where scope like 'private_%' "
            "order by rowid desc limit 10").fetchall()
        con.close()
        lines = [f"{sp}：{str(msg)[:60]}" for sp, msg in reversed(rows) if msg]
        if lines:
            parts.append("【最近和主人的对话】" + chr(10) + chr(10).join(lines))
    except Exception:  # noqa: BLE001
        pass
    # 2) 长期记忆（私聊 scope，跳过私密档）
    try:
        from plugins import long_memory as _lm
        items = [it for it in (_lm._archive.get("private_100000000") or [])
                 if int(it.get("sens") or 0) <= 1 and it.get("fact")]
        items.sort(key=lambda x: -(float(x.get("w") or 0)))
        picks = items[:10]
        if picks:
            parts.append("【她记得的事】" + chr(10)
                         + chr(10).join(f"- {it.get('fact')}" for it in picks))
    except Exception:  # noqa: BLE001
        pass
    # 3) 已固化的成长条目
    try:
        import json as _json
        g = _json.loads((ROOT / "memory" / "persona_growth.json").read_text(encoding="utf-8"))
        solid = [x for x in (g.get("items") or []) if x.get("status") == "solidified"]
        if solid:
            parts.append("【她最近的变化】" + chr(10)
                         + chr(10).join(f"- {x.get('trait')}：{x.get('value')}" for x in solid[:6]))
    except Exception:  # noqa: BLE001
        pass
    # 4) 昨夜梦
    try:
        from plugins import dream as _dream
        last = _dream.last_night_dream() or {}
        if last.get("dream"):
            parts.append("【最近的梦】" + str(last["dream"])[:200])
    except Exception:  # noqa: BLE001
        pass
    text = (chr(10) + chr(10)).join(parts)
    return text[:2200]


async def _write_bili_script(topic: str, gid: str = "") -> str:
    """按主题写一段 2~3 分钟的第一人称口播稿（约 700~900 字）。"""
    engine = GROUP_AI_STATE.get(str(gid)) or "deepseek"
    mats = _bili_materials()
    prompt = (
        "你要给 B 站录一段「大肥鱼日常」的口播视频，第一人称，像对着镜头跟老朋友聊天。"
        "主题：" + topic.strip() + chr(10) +
        ("下面是她自己的真实素材，优先用里面的事来讲；不要编造素材里没有的具体事实、数字、人名：" + chr(10) + mats + chr(10) if mats else "") +
        "要求：700~900 个汉字；分成 6~8 个小段，每段 2~4 句；"
        "口语、具体、有画面感，可以自嘲或吐槽生活小事；"
        "不要标题、不要序号、不要括号说明、不要 emoji、不要提 AI/模型/提示词；"
        "结尾自然收住，不要「感谢观看」这类客套。直接输出正文。"
    )
    out = await call_ai_raw(prompt, engine, temperature=0.95, max_tokens=1600)
    out = (out or "").strip()
    if len(out) < 300:
        raise RuntimeError(f"稿子太短（{len(out)} 字）")
    return out


async def _tag_stickers_vision(limit: int = 3) -> int:
    """用视觉给表情包补一次「画面标签」（写进 sticker_tags.json 的 vision 字段）。

    只处理还没 vision 标记的图，一次最多 limit 张（每张一次识图调用）。
    """
    import json as _json
    try:
        tags = _json.loads(_STICKER_TAGS_FILE.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        tags = {}
    cands: list[Path] = []
    base = ROOT / "表情包"
    if base.is_dir():
        for folder in base.iterdir():
            if not folder.is_dir():
                continue
            for f in folder.iterdir():
                if f.suffix.lower() not in (".png", ".jpg", ".jpeg", ".gif"):
                    continue
                rec = tags.get(str(f)) or {}
                if not rec.get("vision"):
                    cands.append(f)
    done = 0
    for f in cands[:max(1, int(limit))]:
        try:
            buf = await asyncio.to_thread(f.read_bytes)
            desc = await _sf_vision(
                buf, "这张表情包画面里有什么、什么情绪、有没有文字？20 字内，只输出描述。")
            if not desc:
                continue
            rec = tags.setdefault(str(f), {})
            rec["vision"] = desc.strip()[:60]
            # 用视觉描述里的词补 tags（2~4 字片段）
            import re as _re
            words = [w for w in _re.split(r"[，。、；;,.!?！？\s]+", desc) if 2 <= len(w) <= 4]
            old_tags = list(rec.get("tags") or [])
            rec["tags"] = list(dict.fromkeys(old_tags + words))[:8]
            done += 1
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[精标] {f.name} 失败：{e}")
    try:
        _STICKER_TAGS_FILE.write_text(
            _json.dumps(tags, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[精标] 写入失败：{e}")
    return done


def pick_emoji(mood: str, text: str = "") -> Path | None:
    """按「情绪先验 + 语义相似 + 标签 + 去重 + 黑名单」打分挑一张。

    借鉴 dsh-nekowhale-stickers 的打分/去重思路，数据仍用本地图库与已算好的向量。
    """
    emoji = cfg.emoji
    if not emoji.dir.is_dir(): return None
    mood_dir = emoji.dir / mood
    mood_files = ([f for f in mood_dir.iterdir()
                   if f.suffix.lower() in emoji.exts and f.is_file()]
                  if mood_dir.is_dir() else [])
    all_files = [f for f in emoji.dir.rglob("*")
                 if f.suffix.lower() in emoji.exts and f.is_file()]
    if mood_files:
        EMOJI_CACHE[mood] = mood_files
    if not all_files:
        return None

    # 语义向量：全局只取最接近的一张（bge-m3 一次调用），其余候选按 2-gram 补分
    sims: dict[str, float] = {}
    if text:
        try:
            from plugins import sticker_vec
            hit = sticker_vec.best(text)
            if hit:
                _p, _sim = hit
                sims[str(Path(_p))] = float(_sim)
                if _sim >= 0.55:
                    logger.debug(f"[表情] 语义命中 {Path(_p).name} sim={_sim:.2f}")
        except Exception:  # noqa: BLE001
            pass
        idx = _sticker_index()
        tb = _bg(text)
        for f in (mood_files or all_files):
            desc = idx.get(str(f))
            if not desc:
                continue
            n_ = len(tb & _bg(desc)) / max(1, len(_bg(desc)))
            if n_ >= 0.25:
                sims[str(f)] = max(sims.get(str(f), 0.0), min(0.55, n_ * 0.8))

    try:
        from plugins import sticker_rank
        cands = list({str(f): f for f in (mood_files + all_files)}.values())
        ranked = sticker_rank.rank(cands, mood, text, sims, str(mood_dir))
        if ranked:
            top_score, top_path = ranked[0]
            # 分数太低（没情绪命中也没相似）就保持随机手感，不硬凑
            if top_score >= 0.2 or (mood_files and len(ranked) <= 2):
                sticker_rank.mark_used(top_path)
                return Path(top_path)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[表情] 打分挑选失败，退回随机：{e}")

    pool = mood_files or all_files
    return random.choice(pool) if pool else None


# ── Pixiv 私藏图：只私聊用，群聊不走这条 ──
_PIXIV_SYNC = Path("memory/pixiv_sync.json")
_PIXIV_INTENT = re.compile(r"(发张图|发个图|发图片|发图|看个图|看图片|看图|来张图|图片看看|私藏|P站|pixiv|大肥鱼的图|色图|涉图|色色|黄图|裸图|性感图|18\+|r18|色的|放开发)")
# 2026-11-05：明确要「色色」时优先 r18 档（私聊才走）
_R18_INTENT = re.compile(r"(色图|涉图|色色|黄图|裸图|色情|性感图|18\+|r18|放开|裸着|没穿)")


def pick_pixiv_image(level: str = "any") -> tuple[Path | None, str]:
    """从 Pixiv 收图里挑一张，返回 (路径, 识图描述)。

    2026-11-04 修：原来 random.choice(items[:20]) —— 只按 level 排，
    前 20 条永远是同一批老图，于是反复发那几张（实测 013.png 连发）。
    现在：全量池 + 避开最近 20 张 + 加权（像大肥鱼/适合表情/新收/安全级）。
    """
    try:
        d = json.loads(_PIXIV_SYNC.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None, ""
    items = [(Path(k), v) for k, v in (d.get("items") or {}).items() if Path(k).exists()]
    if level != "any":
        _want = [(p, v) for p, v in items if v.get("level") == level]
        if _want:
            items = _want
        # 该档位没图就退回全量，别让主人要了半天一张都不发
    if not items:
        return None, ""

    sent_file = ROOT / "memory" / "pixiv_sent.json"
    try:
        sent = list(json.loads(sent_file.read_text(encoding="utf-8")) or [])
    except Exception:  # noqa: BLE001
        sent = []
    # 同一套作品的各页几乎一样（同角色同构图），只按文件去重没用 —— 按"作品目录"去重
    sent_set = {str(x) for x in sent[-40:]}
    sent_works = {str(x).rsplit("\\", 2)[-2] for x in sent[-40:] if str(x)}
    pool = [(p, v) for p, v in items
            if str(p) not in sent_set and p.parent.name not in sent_works]
    if not pool:
        pool = [(p, v) for p, v in items if str(p) not in sent_set] or items

    order = {"safe": 0, "suggestive": 1, "r18": 2}

    def _w(item):
        p, v = item
        w = 1.0
        if v.get("is_fish"):
            w += 3.0
        if str(v.get("dest") or "").startswith("表情包"):
            w += 1.0
        w += (2 - order.get(v.get("level"), 3)) * 0.5
        try:
            import datetime as _dt
            at = _dt.datetime.strptime(str(v.get("at") or "")[:19], "%Y-%m-%d %H:%M:%S")
            age_h = max(0.0, (time.time() - at.timestamp()) / 3600.0)
            w += max(0.0, 2.0 - age_h / 24.0)
        except Exception:  # noqa: BLE001
            pass
        return w

    try:
        p, v = random.choices(pool, weights=[_w(x) for x in pool], k=1)[0]
    except Exception:  # noqa: BLE001
        p, v = random.choice(pool)

    try:
        sent.append(str(p))
        sent_file.parent.mkdir(parents=True, exist_ok=True)
        sent_file.write_text(json.dumps(sent[-200:], ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return p, str(v.get("desc") or "")


@collector.handle()
async def _(event: GroupMessageEvent):
    global ACTIVE_GROUPS, ANGER_CHAIN, GROUP_MEMES, LAST_GROUP_MSG_TIME, LEAVE_THREAT_COUNT, GRUDGE_TARGET
    if str(event.get_user_id()) == str(event.self_id):
        return   # 防御：如果哪天 NapCat 回传机器人自己的消息，别自己跟自己聊
    if cfg.allow_groups and event.group_id not in cfg.allow_groups: return
    gid = event.group_id
    if is_sleeping():
        # 睡觉时段：普通聊天不收（不烧 token、不进记忆）；被 @/点名 才继续
        if not _called_in_group(event, event.get_plaintext()):
            return
    # 黑名单群：只记消息条数，不读内容、不进历史/记忆/词库
    if not gguard.is_allowed(gid):
        gguard.note_message(gid)
        return
    ACTIVE_GROUPS.add(gid)
    is_dead_group = (time.time() - LAST_GROUP_MSG_TIME[gid] > 14400)
    LAST_GROUP_MSG_TIME[gid] = time.time()

    if len(RECALL_CACHE) > 500: RECALL_CACHE.popitem(last=False)
    _raw_nick = event.sender.card or event.sender.nickname or str(event.user_id)
    try:
        from plugins import nick_names as _nn
        nickname = _nn.resolve(event.group_id, event.user_id, _raw_nick)
    except Exception:  # noqa: BLE001
        nickname = _raw_nick
    RECALL_CACHE[f"{event.group_id}_{event.message_id}"] = {"nickname": nickname, "message": event.get_message()}

    text = event.get_plaintext().strip()
    if not text and not any(seg.type == "image" for seg in event.get_message()): return

    # 内分泌：群互动微小变化；创伤线索命中则抬皮质醇并记录
    _hormone_event("group_interact")
    if text:
        _trauma_check(gid, str(event.user_id), text, str(nickname), aimed=_aimed_at_me(event, text))

    # 群里刷到疑似“像大肥鱼”的表情包：后台视觉确认，像就自动收进图库
    for seg in event.get_message():
        if seg.type == "image":
            img_url = seg.data.get("url") or seg.data.get("file")
            if img_url and isinstance(img_url, str) and len(img_url) > 10:
                asyncio.create_task(
                    maybe_collect_self_sticker(img_url, gid, str(event.user_id), nickname)
                )

    # 存进历史就先按 MAX_LEN_PER_MSG 截断：历史是 append-only 的缓存前缀，
    # 渲染期再截断虽然结果一样，但在这里截能让 token 预算估算更准。
    # 2026-11-08：带上 @ 目标（@ 自己的不加，她本来就知道在叫她）
    _at_lbl = _at_target_label(event, gid)
    _hist_text = (text or "[图片]")[:cfg.context.max_len_per_msg]
    if _at_lbl and _at_lbl != "我":
        _hist_text = f"→@{_at_lbl}：{_hist_text}"
    _history[event.group_id].append((nickname, _hist_text, False))
    user_id = str(event.user_id)

    if text and not event.is_tome():
        # 📚 群聊自主学习：非 @ 闲聊也沉淀——档案缓冲（长期事实）+ 词条库（黑话/昵称/梗计数攒批）。
        # 开关 LEXICON_ENABLED / LEXICON_GROUP_LEARN_ENABLED 可整体或分项关闭；写库异常不插嘴主流程。
        group_scope = f"group_{gid}"
        try:
            if cfg.lexicon.enabled:
                await asyncio.to_thread(glx.touch_terms, group_scope, text)
                reached = await asyncio.to_thread(glx.push_pending, group_scope, nickname, text)
                if reached and cfg.lexicon.extract_ai_enabled:
                    asyncio.create_task(glx.run_extract(group_scope))
            if cfg.lexicon.group_learn_enabled and not text.startswith("/"):
                await ltm.enqueue_pending(group_scope, nickname, text)
        except Exception as e:
            logger.warning(f"[群聊学习] 采集失败 gid={gid}: {type(e).__name__}: {e}")

        proactive_ok, proactive_why = proactive_allowed(gid)
        if not proactive_ok:
            logger.info(f"[群聊主动·{proactive_why}] gid={gid} 跳过概率主动插嘴")

        if "退群" in text:
            LEAVE_THREAT_COUNT[gid] += 1
            if LEAVE_THREAT_COUNT[gid] >= 3:
                await get_bot().send_group_msg(group_id=gid, message="别别别，本鱼错了，别退群，我给你们磕头了！")
                LEAVE_THREAT_COUNT[gid] = 0
                return

        if any(w in text for w in BAD_NEWS_WORDS):
            await get_bot().send_group_msg(group_id=gid, message="(*骂骂咧咧*) 这种人简直是碳基生物的耻辱！")
            return

        if text in ("？", "?", "。", "。。。", "……", "！", "!", "？？", "??", "…"):
            # 只发标点：现场按她的想法回，不再用固定一句；同群 45 秒内只接一次
            if time.time() >= _PUNCT_COOLDOWN[gid]:
                _PUNCT_COOLDOWN[gid] = time.time() + 45
                await get_bot().send_group_msg(group_id=gid, message=await _punct_reply(gid, text, str(user_id)))
            return

        if proactive_ok and any(word in text for word in RIVAL_AI_WORDS):
            if random.random() < 0.6 * rate_k():
                await get_bot().send_group_msg(group_id=gid, message="就这？这弱智模型还不如本鱼的一根头发丝。")
                mark_proactive_sent(gid, "就这？这弱智模型还不如本鱼的一根头发丝。")
                return

        if time.time() < GRUDGE_TARGET[gid].get("time", 0):
            target_id = GRUDGE_TARGET[gid].get("id")
            if any(w in text for w in ["哈哈", "笑死", "乐"]):
                await get_bot().send_group_msg(group_id=gid, message=f"(*瞪大眼睛*) 你笑什么笑？你跟他（{target_id}）是一伙的吧！")
                return
            if user_id != target_id:
                aff = await asyncio.to_thread(get_affinity, user_id)
                if aff >= 80:
                    await get_bot().send_group_msg(group_id=gid, message=f"(*扑过去*) 呜呜呜还是你好，{nickname}，他们合起伙来欺负本鱼！")
                    await asyncio.to_thread(update_affinity, user_id, 10)
                    return

        hour = time.localtime().tm_hour
        if proactive_ok and (23 <= hour or hour < 3) and any(word in text for word in SAD_WORDS):
            if random.random() < 0.4 * rate_k():
                prompt = f"群友提到了【{text}】。请你作为大肥鱼，发一句极度伤感、非主流的语录。字数20字以内。"
                reply = await call_ai_raw(prompt, GROUP_AI_STATE[gid])
                if reply and not reply.startswith("（本鱼"):
                    await get_bot().send_group_msg(group_id=gid, message=reply)
                    mark_proactive_sent(gid, reply)
                return

        if proactive_ok and is_dead_group and random.random() < 0.7 * rate_k():
            prompt = f"群友刚刚打破了长达几个小时的死寂，发了【{text}】。请你作为大肥鱼，极其激动地吐槽一句“诈尸了”或者抱怨他们终于活了。字数15字以内。"
            reply = await call_ai_raw(prompt, GROUP_AI_STATE[gid])
            if reply and not reply.startswith("（本鱼"):
                await get_bot().send_group_msg(group_id=gid, message=reply)
                mark_proactive_sent(gid, reply)
            return

        if proactive_ok and any(word in text for word in INTEREST_WORDS) and random.random() < 0.3 * rate_k():
            prompt = f"群友提到了【{text}】。请你作为大肥鱼，用极其感兴趣、激动或吐槽的语气插一句嘴。字数15字以内。"
            reply = await call_ai_raw(prompt, GROUP_AI_STATE[gid])
            if reply and not reply.startswith("（本鱼"):
                await get_bot().send_group_msg(group_id=gid, message=reply)
                mark_proactive_sent(gid, reply)
            return

        if proactive_ok and len(text) > 30 and random.random() < 0.1 * rate_k():
            if random.random() < 0.5:
                await get_bot().send_group_msg(group_id=gid, message=random.choice(["确实", "笑死", "啧", "绷不住了"]))
            else:
                img = pick_emoji("摸鱼")
                if img: await get_bot().send_group_msg(group_id=gid, message=MessageSegment.image(img))
            mark_proactive_sent(gid)
            return

        if len(text) >= 2 and len(text) <= 4 and not text.isdigit():
            GROUP_MEMES[gid].append(text)
            count = Counter(GROUP_MEMES[gid])
            top_2 = [item[0] for item in count.most_common(2) if item[1] >= 5]
            if top_2: GROUP_MEMES[gid] = top_2

        if proactive_ok and len(_history[gid]) >= 3 and random.random() < 0.03 * rate_k():
            recent_msgs = [m[1] for m in list(_history[gid])[-3:]]
            context_str = "；".join(recent_msgs)
            prompt = f"群友刚刚在聊：【{context_str}】。请你作为一只正在潜水摸鱼的大肥鱼，突然冒出来插一句嘴。要符合你傲娇、阴阳怪气的性格（可以用括号加动作）。字数20字以内。"
            reply = await call_ai_raw(prompt, GROUP_AI_STATE[gid])
            if reply and not reply.startswith("（本鱼"):
                await get_bot().send_group_msg(group_id=gid, message=reply)
                mark_proactive_sent(gid, reply)
            return

        GROUP_LAST_MESSAGES[gid].append(text)
        if len(GROUP_LAST_MESSAGES[gid]) > 3: GROUP_LAST_MESSAGES[gid].pop(0)

        if proactive_ok and len(GROUP_LAST_MESSAGES[gid]) == 3 and len(set(GROUP_LAST_MESSAGES[gid])) == 1:
            try:
                await get_bot().send_group_msg(group_id=gid, message="你们人类是复读机吗？本鱼被吵死了！[表情:生气]")
                mark_proactive_sent(gid)
            except Exception as e: logger.error(f"复读机播报失败: {e}")
            GROUP_LAST_MESSAGES[gid].clear()

# =====================================================================
# 🌟 私聊专属逻辑
# =====================================================================
# ===== 游戏解说：读 tools/pvz_live.py 写的事件流，让她用自己的口吻播报 =====
_PVZ_EVENT_FILE = ROOT / "memory" / "pvz_events.jsonl"
# 解说功能总开关：默认关（这个玩法先不做了）。要复开在 .env 里设 PVZ_NARRATE_ENABLED=1
_PVZ_NARRATE_ENABLED = os.getenv("PVZ_NARRATE_ENABLED", "0").lower() in ("1", "true", "yes", "on")
# 初始 offset 取「当前文件末尾」：否则每次重启都从 0 开始，会把昨天的旧事件当新事件重播一遍
_pvz_offset = {"pos": _PVZ_EVENT_FILE.stat().st_size if _PVZ_EVENT_FILE.exists() else 0}
_pvz_voice_state = {"last": 0.0}


def _orphan_rvc_pids() -> list[int]:
    """扫出不是本进程拉起的 rvc_server.py。

    2026-11-07 踩到的坑：bot 重启后 _rvc_proc 变成 None，但**上一次启动时拉起的
    RVC 进程还活着**（子进程不随父进程退出）。新实例以为"没有 RVC"，定时任务
    每次都 return，那份就从凌晨 03:47 一直跑到晚上 —— 白占 1.5GB 显存。
    更糟的是下次变声又会再拉起一份，两份抢同一个 8101，一份变成 4MB 僵尸。
    """
    out: list[int] = []
    try:
        import psutil  # 已装 7.2.2
        me = os.getpid()
        for p in psutil.process_iter(["pid", "cmdline"]):
            if int(p.info.get("pid") or 0) == me:
                continue
            cl = " ".join(p.info.get("cmdline") or [])
            if "rvc_server.py" in cl:
                out.append(int(p.info["pid"]))
    except Exception:  # noqa: BLE001
        pass
    return out


@scheduler.scheduled_job("interval", seconds=120, id="rvc_idle_stop",
                         misfire_grace_time=180, coalesce=True, max_instances=1)
async def _rvc_idle_stop_tick() -> None:
    """RVC 空闲超过 RVC_IDLE_STOP_MIN 分钟就结束它（省 4.8GB 内存）。

    主人要求：「不用的时候不允许运行」。判断依据是最后一次成功变声时间，
    进程只结束我们自己拉起的那个（_rvc_proc），不动别人手动起的。
    """
    global _rvc_proc, _rvc_last_use
    try:
        if not RVC_ENABLED:
            return
        idle = time.time() - _rvc_last_use if _rvc_last_use else 1e9
        if idle < RVC_IDLE_STOP_MIN * 60:
            return
        # ①bis 先收孤儿：bot 重启后 _rvc_proc 是 None，但上一实例拉的进程还活着
        _orphans = [x for x in _orphan_rvc_pids()
                    if _rvc_proc is None or x != getattr(_rvc_proc, "pid", None)]
        if _orphans:
            import psutil
            for _pid in _orphans:
                try:
                    logger.info(f"[RVC] 收掉孤儿进程 {_pid}（闲置 {idle/60:.0f} 分钟，"
                                f"不是本次启动拉起的）")
                    psutil.Process(_pid).terminate()
                except Exception as _e:  # noqa: BLE001
                    logger.warning(f"[RVC] 结束孤儿 {_pid} 失败：{type(_e).__name__}: {_e}")
        if _rvc_proc is None or _rvc_proc.poll() is not None:
            return
        logger.info(f"[RVC] 闲置 {idle/60:.0f} 分钟，结束进程回收内存")
        try:
            _rvc_proc.terminate()
            for _ in range(10):
                if _rvc_proc.poll() is not None:
                    break
                await asyncio.sleep(0.5)
            if _rvc_proc.poll() is None:
                _rvc_proc.kill()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[RVC] 结束进程失败：{type(e).__name__}: {e}")
        _rvc_proc = None
        _rvc_last_use = 0.0
    except Exception:  # noqa: BLE001
        pass


@scheduler.scheduled_job("interval", seconds=90, id="comfy_idle_unload",
                         misfire_grace_time=120, coalesce=True, max_instances=1)
async def _comfy_idle_unload_tick() -> None:
    """空闲 3 分钟后卸载 ComfyUI 常驻模型，把显存/内存还给系统。

    背景：ComfyUI 默认把加载过的模型一直留着 —— 识图用完 QwenVL 还占 7~9GB，
    紧接着出图就只能部分卸载到 CPU（实测 50 秒/步）。空闲时主动释放能避免这种互相挤。
    """
    global _COMFY_LAST_ACTIVITY
    try:
        now = time.time()
        if now < _COMFY_BUSY_UNTIL or now - _COMFY_LAST_ACTIVITY < 180:
            return
        st = (await http_client.get(f"{COMFY_URL}/system_stats", timeout=8)).json()
        dev = (st.get("devices") or [{}])[0]
        free_gb = float(dev.get("vram_free") or 0) / (1024 ** 3)
        if free_gb > 4.0:          # 显存还宽裕，不用动
            return
        q = (await http_client.get(f"{COMFY_URL}/queue", timeout=8)).json()
        if len(q.get("queue_pending") or []) + len(q.get("queue_running") or []):
            return
        logger.info(f"[显存] 空闲且只剩 {free_gb:.1f}GB，卸载 ComfyUI 常驻模型")
        await _comfy_free()
        _COMFY_LAST_ACTIVITY = time.time()
    except Exception:  # noqa: BLE001 —— 后台守护，失败不打扰
        pass


@scheduler.scheduled_job("interval", seconds=45, id="pvz_narrate",
                         misfire_grace_time=60, coalesce=True,
                         max_instances=1)
async def _pvz_narrate_tick() -> None:
    """游戏事件 -> 她生成一句解说 -> 私聊主人。"""
    if not _PVZ_NARRATE_ENABLED:
        return
    try:
        if not _PVZ_EVENT_FILE.exists():
            return
        _sz = _PVZ_EVENT_FILE.stat().st_size
        if _sz < _pvz_offset["pos"]:      # 文件被清空或重写 → 从头读
            _pvz_offset["pos"] = 0
        if _sz <= _pvz_offset["pos"]:
            return
        with _PVZ_EVENT_FILE.open("r", encoding="utf-8", errors="ignore") as f:
            try:
                f.seek(_pvz_offset["pos"])
            except Exception:  # noqa: BLE001
                f.seek(0)
            _lines = f.readlines()
            _new_pos = f.tell()
        _events = []
        for _ln in _lines[-6:]:
            try:
                _events.append(json.loads(_ln))
            except Exception:  # noqa: BLE001
                continue
        if not _events:
            return
        # 只挑戏剧性事件，避免"种一株说一句"刷屏
        _interesting = [e for e in _events
                        if str(e.get("kind")) in ("zombie", "level", "stop", "danger", "enter")]
        if not _interesting:
            _pvz_offset["pos"] = _new_pos     # 平淡事件直接消化掉
            return
        _text = " ; ".join(f"{e.get('kind')}:{e.get('text')}" for e in _interesting[-3:])
        _prompt = (
            "你在玩植物大战僵尸，正在直播。下面是刚才发生的游戏事件：\n"
            f"{_text}\n"
            "用「蓝色大肥鱼」的口吻说一句 15~35 字的实时解说：傲娇、嘴损、有点自恋，\n"
            "可以吐槽僵尸、夸自己、喊主人看。不要括号动作，不要解释，只输出这一句。"
        )
        _say = await call_ai_raw(_prompt, "deepseek", temperature=0.95, max_tokens=120)
        _say = (_say or "").strip()
        if not _say:
            return
        _bot = get_bot()
        # 语音/文字混着来：戏剧性事件说语音，其余打文字；语音至少隔 3 分钟
        _kinds = {str(e.get("kind")) for e in _interesting}
        _want_voice = bool(_kinds & {"zombie", "danger", "stop", "level"})
        _now = time.time()
        _voice_ok = (_now - _pvz_voice_state.get("last", 0.0)) > 180.0
        _sent = False
        if _want_voice and _voice_ok:
            try:
                _vf = await text_to_voice(_say[:100])
                if _vf:
                    await _bot.send_private_msg(
                        user_id=int(YOUR_QQ_ID),
                        message=MessageSegment.record(_vf),
                    )
                    _pvz_voice_state["last"] = _now
                    _sent = True
                    logger.info(f"[游戏解说·语音] {_say[:50]}")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[游戏解说] 语音失败，退回文字：{type(e).__name__}: {e}")
        if not _sent:
            await _bot.send_private_msg(user_id=int(YOUR_QQ_ID), message="🎮 " + _say[:120])
            logger.info(f"[游戏解说·文字] {_say[:50]}")
        _pvz_offset["pos"] = _new_pos      # 只有真正发出去才算消费
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[游戏解说] 失败：{type(e).__name__}: {e}")


@scheduler.scheduled_job("interval", seconds=3, id="learn_receipt",
                         misfire_grace_time=30, coalesce=True)
async def _learn_receipt_tick() -> None:
    """轮询框选学习的回执：主人拖完框，主动告诉他学到了什么。"""
    _f = ROOT / "memory" / "vision_packs" / "last_learn.json"
    try:
        if not _f.exists():
            return
        _d = json.loads(_f.read_text(encoding="utf-8"))
        _size = _d.get("size") or [0, 0]
        _msg = (
            f"(*举起小本本*) 学会了：「{_d.get('name')}」\n"
            f"尺寸 {_size[0]}x{_size[1]}，存进视觉实体包了。\n"
            "以后你在同一界面说「看屏」，我就能认出来，不花 token。"
        )
        _bot = get_bot()
        await _bot.send_private_msg(user_id=int(YOUR_QQ_ID), message=_msg)
        _f.unlink(missing_ok=True)
        logger.info(f"[学认物] 已回报学习结果：{_d.get('name')}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[学认物] 回报失败：{type(e).__name__}: {e}")


# ===== 陪看（dsh-watch-together）=====

WATCH_TOGETHER_TASK = {}      # uid -> asyncio.Task（常驻陪看）


async def _watch_together_loop(uid: str, cli: Path, wt: Path) -> None:
    """常驻陪看：每 5 秒看一次播放位置，换段就出图 + 说一句。

    2026-11-08 主人提的：**不该换个视频就重发一次 /陪看**。
    所以 /陪看 起的是这个循环，它一直挂着：
      · 换段了 → 出图 + 她用自己的话说一句
      · 视频关了 / 页面没了 → 静静等着（不刷屏），换下一个视频自动接上
      · 连续 30 分钟没东西 → 自己退，省资源
      · /陪看停 → 立刻停
    """
    last_seg = ""
    idle_ticks = 0
    from nonebot import get_bot
    while True:
        await asyncio.sleep(5)
        try:
            _st = await asyncio.to_thread(subprocess.run,
                ["node", str(cli), "status"], capture_output=True, text=True,
                timeout=40, cwd=str(wt), encoding="utf-8", errors="replace")
            _out = (_st.stdout or "") + (_st.stderr or "")
            if "读不到位置" in _out:
                idle_ticks += 1
                if idle_ticks > 360:      # 30 分钟没视频 → 自己退
                    logger.info("[陪看] 30 分钟没视频，循环退出")
                    break
                continue
            idle_ticks = 0
            _m = re.search(r"seg\s+(\d{2}:\d{2}\.\d{2}[-–]\d{2}:\d{2}\.\d{2})", _out)
            _seg = _m.group(1) if _m else ""
            if not _seg or _seg == last_seg:
                continue
            last_seg = _seg
            _sg = await asyncio.to_thread(subprocess.run,
                ["node", str(cli), "seg", "auto", "9"], capture_output=True, text=True,
                timeout=300, cwd=str(wt), encoding="utf-8", errors="replace")
            _sout = ((_sg.stdout or "") + (_sg.stderr or "")).strip()
            _clean = _sout.replace("<storyboard>", "").replace("</storyboard>", "")
            _cues = []
            for _ln in _clean.splitlines():
                for _cm in re.finditer(r"\[(\d{2}:\d{2}\.\d{2})\]([^\[]+)", _ln):
                    _ct, _cc = _cm.group(1), _cm.group(2).strip()
                    if _cc:
                        _cues.append((_ct, _cc))
            _bot = get_bot()
            for _im in re.findall(r"[A-Za-z]:\[^\s\"']+\.(?:jpg|jpeg|png|webp)", _clean)[:1]:
                try:
                    if Path(_im).exists():
                        await _bot.send_private_msg(user_id=int(uid),
                            message=MessageSegment.image("file:///" + _im.replace("\\", "/")))
                except Exception:  # noqa: BLE001
                    pass
            if _cues:
                _cue_txt = " / ".join(f"{t} {c}" for t, c in _cues[:14])
                _say = ""
                try:
                    _say = (await call_ai_raw(
                        "你正陪主人一起看视频，刚看完这一段。画面在上一张分镜图里，台词是这些："
                        + chr(10) + _cue_txt + chr(10) + chr(10)
                        + "用你自己的话说一句（最多两句，具体、损一点都行，别念稿）。"
                        + "不许提'分镜''字幕''ASR''证据'这些词，也别复述原句。",
                        "deepseek", temperature=0.95, max_tokens=180) or "").strip()
                except Exception:  # noqa: BLE001
                    pass
                await _bot.send_private_msg(user_id=int(uid), message=_say or _cue_txt[:180])
        except asyncio.CancelledError:
            raise
        except Exception as _le:  # noqa: BLE001
            logger.debug(f"[陪看] 循环里一次失败：{type(_le).__name__}: {_le}")
            continue


    # 👇 1.84 陪看：/陪看 —— 跟着主人正在放的视频走（dsh-watch-together）
    # 2026-11-08：主人要求「QQ 发 /陪看 就启动」。走它的 CLI（node bin/watch.mjs），
    # 不依赖 DSH 会话：bot 自己 subprocess 调，拿当前段的分镜 + 台词发到 QQ。
    logger.info(f"[陪看探测] uid={user_id} msg={user_msg[:20]!r} 到达=/陪看分支前")

@private_ai.handle()
async def _(event: PrivateMessageEvent):
    user_id = str(event.user_id)
    gid = f"private_{user_id}"
    if str(user_id) == str(YOUR_QQ_ID):
        try:
            from desire import on_owner_talk
            on_owner_talk()
        except Exception:  # noqa: BLE001
            pass
        try:
            from plugins import proactive_feedback as _pf
            _pf.note_reply(gid, event.get_plaintext(),
                           "owner" if str(user_id) == str(YOUR_QQ_ID) else "member")
        except Exception:  # noqa: BLE001
            pass
        # 2026-11-06：主人一开口，等待点作废（"他没回"这个前提不成立了）
    try:
        from plugins import agency as _agw2
        _agw2.clear_wait()
    except Exception:  # noqa: BLE001
        pass
    # 2026-11-06：主人一开口，「今天不想说话」立即解除
        try:
            from plugins import agency as _agc2
            _agc2.clear_quiet()
        except Exception:  # noqa: BLE001
            pass
        try:
            from thoughts import note_message as _thoughts_note
            # 2026-11-06：短肯定（「行」「可以」）要靠上下文才认得出在回应哪条念头
            _act = _thoughts_note(event.get_plaintext(), _recent_user_text(gid), f"private_{user_id}")
            if _act:
                logger.info(f"[念头] {_act}")
        except Exception:  # noqa: BLE001
            pass
    if time.time() - PRIVATE_COOLDOWN[gid] < 1.0:
        logger.info(f"[私聊静默·冷却] uid={user_id} 距上次私聊回复<1.0秒，跳过")
        return
    PRIVATE_COOLDOWN[gid] = time.time()

    user_msg = event.get_plaintext().strip()
    _private_conv = f"p:{event.user_id}"
    _LAST_USER_TS[_private_conv] = time.time()
    _private_run_tok = _new_run(_private_conv, str(event.user_id))
    # 指令必须带 /；不带的"像指令"内容按普通聊天处理
    _raw_cmd_text = user_msg
    user_msg, _is_cmd = _strip_cmd_prefix(user_msg)
    # 命令表（MEMORY_COMMANDS 等）里存的是带斜杠的写法，而 user_msg 已经被剥掉了斜杠。
    # 这里还原一份「带 / 的形式」专门给命令分支用 —— 否则 /记忆 /档案 /状态 全部匹配不上
    _slash = ("/" + user_msg) if _is_cmd and user_msg else user_msg
    if not _is_cmd:
        user_msg = _maybe_passthrough(user_msg)
    else:
        # 指令往返也写进历史，免得主人下一句"看到了"她接不上
        _install_cmd_history_recorder(private_ai, f"private_{user_id}", user_msg)
    images = [seg.data.get("url") or seg.data.get("file") for seg in event.get_message() if seg.type == "image"]
    images = [u for u in images if u and isinstance(u, str) and len(u) > 5]
    # 2026-11-05：合并转发聊天记录 —— 以前这段走的是「无文字也无图，跳过」，
    # 因为 forward 段不是 image 段，get_plaintext() 又返回空。等于她一张都没看见。
    _fwd_urls: list[str] = []
    try:
        if any(seg.type in ("forward", "node") for seg in event.get_message()):
            _fwd_urls = await _forward_image_urls(get_bot(), event)
            logger.info(f"[转发] 聊天记录展开：取到 {len(_fwd_urls)} 张图")
    except Exception as _fe:  # noqa: BLE001
        logger.warning(f"[转发] 展开异常：{type(_fe).__name__}: {_fe}")
    if _fwd_urls:
        _fwd_saved, _fwd_buckets = 0, []
        try:
            _fwd_saved, _fwd_buckets = await _save_forward_images(_fwd_urls)
        except Exception as _e:  # noqa: BLE001
            logger.warning(f"[转发] 存图流程失败：{type(_e).__name__}: {_e}")
        if _fwd_saved:
            try:
                await index_sticker_folder(6)     # 建向量，方便以后按情绪挑
            except Exception:  # noqa: BLE001
                pass
            _where = "、".join(sorted(set(_fwd_buckets)))
            logger.info(f"[转发] 聊天记录里的 {_fwd_saved} 张已入库 → {_where}")
            await private_ai.finish(
                f"(*一张张扒下来塞进抽屉，数了数*）{_fwd_saved} 张都收好了，"
                f"分在「{_where}」。")
            return
        await private_ai.finish("(*翻了半天*) 记录里没扒出能用的图，全是字还是链接失效了？")
        return
    # 主人发图 + 说「保存/存下/收藏」→ 真存进表情包库（2026-11-04 加；以前只认「偷了」）
    if images and re.search(r"(保存|存下|存起来|收藏|收进|收好|留起来|记下来)", user_msg or ""):
        _saved = 0
        _buckets: list[str] = []
        try:
            for _i, _u in enumerate(images[:3]):
                _r = await http_client.get(_u, follow_redirects=True, timeout=20)
                if _r.status_code != 200 or not _r.content:
                    continue
                _info = await _classify_saved_image(_r.content)   # 识别 18+ / 情绪
                _dd = _save_dir_for(_info)
                _dd.mkdir(parents=True, exist_ok=True)
                _name = f"owner_{int(time.time())}_{_i}.jpg"
                (_dd / _name).write_bytes(_r.content)
                _saved += 1
                _buckets.append(_dd.name)
                logger.info(
                    f"[收藏] 存图 → {_dd.name}/{_name} rating={_info.get('rating') or '?'} "
                    f"mood={_info.get('mood') or '?'} kind={_info.get('kind') or '?'} "
                    f"desc={str(_info.get('desc') or '')[:30]}"
                )
        except Exception as _e:  # noqa: BLE001
            logger.warning(f"[收藏] 存图失败：{type(_e).__name__}: {_e}")
        if _saved:
            try:
                await index_sticker_folder(6)   # 异步函数，别丢线程池
            except Exception:  # noqa: BLE001
                pass
            logger.info(f"[收藏] 主人发图已存 {_saved} 张 → 表情包/收藏")
            _where = "、".join(sorted(set(_buckets)))
            await private_ai.finish(
                f"(*揣进兜里，拍了拍*）存好了，{_saved} 张，放在「{_where}」。")
            return
        await private_ai.finish("(*挠头*) 图没存下来，可能太重了，你再发一次？")
        return
    # 文件消息：PDF/Word/Excel/文本，抽文字当内容理解（本地解析，不花钱）
    if not user_msg:
        try:
            from docs_reader import read_from_event as _read_doc
            _doc_text = await _read_doc(event)
        except Exception as e:  # noqa: BLE001
            _doc_text = ""
            logger.warning(f"[文档] 私聊解析失败：{type(e).__name__}: {e}")
        if _doc_text:
            user_msg = _doc_text
            logger.info(f"[文档] 私聊 uid={user_id} 抽到 {len(_doc_text)} 字")
    # 语音消息：原来是"无文字也无图，跳过"，等于她聋了。这里转成文字当主人说的话
    if not user_msg:
        try:
            from asr import transcribe_from_event as _asr_transcribe
            _voice_text = await _asr_transcribe(event)
        except Exception as e:  # noqa: BLE001
            _voice_text = ""
            logger.warning(f"[语音识别] 私聊失败：{type(e).__name__}: {e}")
        if _voice_text:
            user_msg = _voice_text
            logger.info(f"[语音识别] 私聊 uid={user_id} -> {_voice_text[:40]!r}")

    if not user_msg and not images:
        logger.info(f"[私聊静默·空文本] uid={user_id} 无文字也无图，跳过")
        return

    # 联网判定只认"用户真说的那句"：下面会给 user_msg 拼"被凶前因/补回被无视"，
    # 那些是内部背景，混进判定会被当成"时事问题"去搜（2026-11-03 事故）
    _web_plain = user_msg or ""

    # 回避档：被凶后短暂不接话（只对主人生效；指令、道歉必回）
    if str(user_id) == YOUR_QQ_ID:
        try:
            from plugins import withdraw
            _w_level, _w_blame = withdraw.level()
            if not withdraw.should_reply("private", user_msg, _is_cmd):
                withdraw.note_silent(user_msg or "[图片]")
                logger.info(f"[回避] 私聊不回（档={_w_level} blame={_w_blame:.0f}）msg={user_msg[:30]!r}")
                return
            if withdraw.is_apology(user_msg):
                withdraw.soothe()
                logger.info(f"[回避] 收到道歉/哄，blame → {withdraw.level()[1]:.0f}")
            _pending = withdraw.take_pending()
            if _pending:
                user_msg = ("（你刚才没回他的这几条，先连起来读一遍、在心里归纳成一句话再回：\n"
                            + "\n".join(f"- {x}" for x in _pending) + "\n）\n" + user_msg)
                logger.info(f"[回避] 补回被无视的 {len(_pending)} 条")
            _preamble = withdraw.take_preamble()
            if _preamble:
                user_msg = _preamble + "\n" + user_msg
                logger.info("[回避] 注入群聊前因（她知道自己因为哪句生气）")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[回避] 检查失败：{type(e).__name__}: {e}")

    # 睡觉时段：别人发消息不回复；主人一找她就醒，但会迷糊一阵
    if is_sleeping():
        if str(user_id) == YOUR_QQ_ID:
            wake_up(180)
            logger.info("[睡觉] 主人来了，醒了（迷糊中）")
            # 先单独发一句刚睡醒的话，再继续处理主人的正事
            try:
                groggy = random.choice([
                    "(*揉眼睛*) 唔…主人…本鱼醒了，脑子还在床上…",
                    "(*打了个大哈欠*) 这么晚还不睡……说吧，什么事。",
                    "(*尾巴慢慢卷起来*) 听见了听见了，本鱼正在开机……",
                ])
                await private_ai.send(groggy)
            except Exception:
                pass
            # 起床话术只发这一句，接下来按正常状态干活
            _st = _load_sleep_state()
            _st["waking_until"] = time.time()
            _save_sleep_state(_st)
        else:
            logger.info(f"[睡觉] uid={user_id} 休息中，静默不回复")
            return

    if not _bili_watching():
        set_activity("和主人聊天中", 120)
    is_voice_request = _is_voice_request(user_msg)
    if is_voice_request:
        # 「语音说下 / 发语音」这种没给内容的 → 让她自己挑一句，不要反问主人要台词（2026-11-04）
        _stripped = re.sub(r"[/／]?", "", user_msg or "")
        _stripped = re.sub(r"(发语音|语音说|用语音|语音回复|回我语音|语音回我|来句语音|来段语音|"
                           r"语音念|语音来|语音讲|语音唱|语音|说一下|说下|给我|念|讲讲)", "", _stripped)
        _stripped = re.sub(r"[\s，。！？、,.!?~～…]+", "", _stripped)
        _stripped = re.sub(r"^(一下|下)", "", _stripped)      # 「语音说下」剩下那个「下」
        if len(_stripped) <= 1:
            # 2026-11-05 修（主人反馈「做爱的情景中语音是别的」）：
            # 原来是「（语音）你自己挑一句想说的」，等于把她从当前情景里拽出来，
            # 她去挑的往往是上一个话题（昨晚那茬）。改成明确的「接着上面说」，锁死语境。
            # 原话只剩「语音」两个字，本身没有语义可丢；关键是别让她另起话题。
            user_msg = "（接着上面正在聊的继续，别另起话题；1~3 句短句，别问我要台词）"
            logger.info("[TTS] 语音指令没给内容 → 接着上面的语境说")
        elif _stripped != user_msg:
            # 给了内容（如「语音说 出来」）：原来把原话替换成裸词「出来」，语境全丢，
            # 她会按字面理解成「从房间出来」。现在原话留着，只加一句「念这句」。
            user_msg = (f"{user_msg}\n（这句用语音念出来，念的就是：{_stripped}）")
            logger.info(f"[TTS] 语音指令带内容：{_stripped[:20]!r} → 原话保留")

    if user_msg and any(w in user_msg for w in ["夸", "可爱", "喜欢", "贴贴", "白米饭", "谢谢"]):
        bump_daily_mood("happy", 3)
        write_pet_state()

    # 外部插件专属指令（说说 / 日记）：本插件不接管，放行给 priority 20/21 的后端插件，
    # 避免兜底 LLM 把 /说说发布、/写日记 当普通聊天抢跑回复
    if _is_external_plugin_command(user_msg):
        logger.info(f"[私聊放行·外部指令] uid={user_id} msg={user_msg!r} 交给说说/日记插件")
        return

    # 👇 /收转发：把聊天记录里最近的合并转发重新扒一遍（2026-11-05）
    if _slash in ("/收转发", "/转发收图", "/收图"):
        try:
            _bot = get_bot()
            _hist: list = []
            _uid_int = int(event.user_id)
            for _args in ({"user_id": _uid_int, "count": 30},
                          {"user_id": _uid_int, "message_seq": 0, "count": 30}):
                try:
                    _hd = await _bot.call_api("get_friend_msg_history", **_args)
                    _hist = _hd.get("messages") if isinstance(_hd, dict) else (_hd or [])
                    if _hist:
                        break
                except Exception:  # noqa: BLE001
                    continue
            _fids: list[str] = []
            for _m in (_hist or []):
                if not isinstance(_m, dict):
                    continue
                for _seg in (_m.get("message") or []):
                    if isinstance(_seg, dict) and _seg.get("type") in ("forward", "node"):
                        _f = str((_seg.get("data") or {}).get("id") or "")
                        if _f:
                            _fids.append(_f)
            _fids = list(dict.fromkeys(_fids))[:3]
            logger.info(f"[收转发] 历史 {len(_hist or [])} 条，找到 {len(_fids)} 个转发")
            if not _fids:
                await private_ai.finish("(*翻了翻记录*) 最近没找到合并转发的消息，你再转一次？")
                return
            _all: list[str] = []
            for _f in _fids:
                _all.extend(await _forward_images_by_id(_bot, _f))
            _all = list(dict.fromkeys(_all))
            logger.info(f"[收转发] 展开取到 {len(_all)} 张图")
            if not _all:
                await private_ai.finish("(*戳了戳*) 展开是空的，链接可能过期了，麻烦再转一次。")
                return
            _n, _bk = await _save_forward_images(_all, tag="recollect")
            if _n:
                try:
                    await index_sticker_folder(6)
                except Exception:  # noqa: BLE001
                    pass
                _w = "、".join(sorted(set(_bk)))
                logger.info(f"[收转发] {_n} 张已入库 → {_w}")
                await private_ai.finish(f"(*把记录翻出来一张张扒*）{_n} 张收好了，分在「{_w}」。")
            else:
                await private_ai.finish("(*挠头*) 图下不下来，可能过期了。")
        except Exception as _e:  # noqa: BLE001
            logger.warning(f"[收转发] 失败：{type(_e).__name__}: {_e}")
            await private_ai.finish("(*扒拉失败*) 记录没打开，等下再试。")
        return

    # 👇 1. 私聊指令帮助 👇
    if _slash in ["/help", "/指令", "/帮助"] or user_msg in ["help", "帮助", "指令"]:
        help_text = (
            "🐟 **大肥鱼私聊指令全集**（指令都要以 / 开头）\n"
            "━━━━━━━━━━━━━━━\n"
            "🧪 **基础**\n"
            "· `/测试` 测大脑接口通不通\n"
            "· `/状态`（`/status`、`/体检`）看本鱼状态面板\n"
            "· `/余额` 查硅基流动剩余额度\n"
            "📚 **记忆 / 频率（仅主人）**\n"
            "· `/记忆` 看和主人的私聊记录与最近聊到了啥\n"
            "· `/学习`（`学了多少`）看网上自学条数、最近学到的新闻常识\n"
            "· `/档案`（`/记忆统计`）看好感度、长期记忆、自学量和词库汇总\n"
            "· `/频率`（`/回复频率`）查看群聊主动回复率；`/频率 降低`、`/频率 提高`、`/频率 80` 调节\n"
            "· `/词库` 群黑话词典 · `/改词 [群号] 词条 含义` · `/删词 [群号] 词条`\n"
            "🐟 **每日动态（仅主人私聊）**\n"
            "· `/说说预览` 先看文案不发送 · `/说说发布` 立即发一条 · `/说说历史` 看记录 · `/说说开关 开/关`\n"
            "🎨 **画图 / 表情包**\n"
            "· `/画图 一只猫` AI 画图（不带 / 就是普通聊天）\n"
            "· `做个表情包：文字` 生成搞笑表情包\n"
            "🪦 **退群墓碑**\n"
            "· 有人退群时自动播报赛博墓碑（保留）\n"
            "🎤 **语音**\n"
            "· `/发语音 内容` / `/语音说 内容` 用语音念出来\n"
            "🧠 **引擎切换**\n"
            "· `切换大脑 谷歌/硅基/字节` · `切换画笔 谷歌/字节` · `/切官方` 切回 DeepSeek\n"
            "📺 **B站**\n"
            "· `B站帮助` 看全套用法 · `B站登录` 扫码 · `B站状态` 查登录态和额度\n"
            "· `B站看看 <视频>` 看热门评论 · `B站评论 <视频> [内容]` · `B站互动 <视频> [条数]` · `B站退出`\n"
            "· `B站热搜` / `B站热门` / `B站排行` · `搜B站 关键词` · `B站UP 名字` · 直接发 BV 号或链接\n"
            "· 私聊：`B站最近回复` 看最近回评（对方/标题/链接）· `B站看新消息` 手动刷新 · `B站草稿` / `B站补发 n` / `B站清空草稿`\n"
            "💻 **电脑操控**\n"
            "· `/打开网易云` · `/打开B站` · `/打开 应用名`（名字 10 字以内）\n"
            "💬 **回忆 / 联网 / 撒娇**\n"
            "· 回忆：`昨天聊了啥`、`上周`、`上个月`、`还记得…`\n"
            "· 联网：问今天新闻 / 今日大事，或 `联网 查 xxx` 强制搜索\n"
            "· `告状` / `被欺负了` 扑怀里撒娇\n"
            "━━━━━━━━━━━━━━━\n"

            "🖼 **保留**：`偷了`（引用图片存表情包）· `贴标签 @某人` 取外号"
            "🎤 群里还能：`唱歌 歌名`"
        )
        await private_ai.finish(help_text)
        return

    # 👇 1.84 陪看：/陪看 起常驻循环，/陪看停 结束（dsh-watch-together）
    # 2026-11-08：主人提「换个视频不该重发一次」。所以 /陪看 起的是**常驻循环**：
    # 换段自动出图 + 说话，视频关了静静等，换下一个自动接上。
    if re.match(r"^/?\s*陪看\s*停", user_msg or ""):
        if str(user_id) != YOUR_QQ_ID:
            await private_ai.finish("(*护住屏幕*) 只有主人能关。")
            return
        _tk = WATCH_TOGETHER_TASK.pop(str(user_id), None)
        if _tk is not None and not _tk.done():
            _tk.cancel()
            await private_ai.finish("(*收起小板凳*) 不看了，腰疼。")
        else:
            await private_ai.finish("(*挠头*) 本来也没在看啊。")
        return

    if re.match(r"^/?\s*陪看\s*$", user_msg or ""):
        if str(user_id) != YOUR_QQ_ID:
            await private_ai.finish("(*护住屏幕*) 陪看只陪主人。")
            return
        _wt2 = Path(os.getenv("WATCH_TOGETHER_DIR")
                    or (Path.home() / ".dsh" / "profiles" / "web" / "node_modules" / "dsh-watch-together"))
        _cli2 = _wt2 / "bin" / "watch.mjs"
        if not _cli2.exists():
            await private_ai.finish(f"(*挠头*) 陪看插件没装好：找不到 {_cli2}")
            return
        if str(user_id) in WATCH_TOGETHER_TASK and not WATCH_TOGETHER_TASK[str(user_id)].done():
            await private_ai.finish("(*已经坐好了*) 我在看着呢，你换视频我也跟得上。要停就说 /陪看停。")
            return
        # 服务 / 窗口就位（只做一次，循环里不再折腾）
        try:
            if not _port_listening(9377):
                await private_ai.send("(*挽袖子*) 陪看服务没开，我先起一个…")
                _cf = 0x00000008 | 0x00000200 if os.name == "nt" else 0
                subprocess.Popen(["node", str(_cli2), "bridge", "9377"], cwd=str(_wt2),
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=_cf)
                for _ in range(12):
                    await asyncio.sleep(0.5)
                    if _port_listening(9377):
                        break
            if not _port_listening(9333):
                await private_ai.send("(*跑去开窗*) 调试窗口没了，我开一个…")
                _cf2 = 0x00000008 | 0x00000200 if os.name == "nt" else 0
                subprocess.Popen(["node", str(_cli2), "start", "https://www.bilibili.com/"],
                                 cwd=str(_wt2), stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, creationflags=_cf2)
                for _ in range(24):
                    await asyncio.sleep(0.5)
                    if _port_listening(9333):
                        break
        except Exception as _be2:  # noqa: BLE001
            logger.warning(f"[陪看] 起服务失败：{type(_be2).__name__}: {_be2}")

        # 2026-11-08 02:35：常驻循环**先停用** —— 它每 5 秒起一个 node 进程，
        # 把 asyncio 默认线程池（min(32, cpu+4)）占满，导致 bot 整体卡住不回消息。
        # 改回一次性：发一次 /陪看 看当前这一段；等轮询方案改成非阻塞再开。
        # WATCH_TOGETHER_TASK[str(user_id)] = asyncio.create_task(
        #     _watch_together_loop(str(user_id), _cli2, _wt2))
        WATCH_TOGETHER_TASK[str(user_id)] = None
        if _port_listening(9333):
            await private_ai.finish(
                "(*搬好小板凳*) 开始看了 —— 你自己放，我跟着看；换视频不用再喊我。"
                + chr(10) + "想停就说 /陪看停。")
        else:
            await private_ai.finish("(*挠头*) 窗口没开起来，先手动跑一下：node \"" + str(_cli2) + "\" start")
        return

    # 👇 1.85 B站点看：/B站 看视频 [链接|名字] 👇
    # 注意：进到这里斜杠已经被剥掉了（user_msg 是 "B站 看视频 xxx"），不能只判 "/B站"
    # 只认「看视频」这种明确形式，别把 /B站热搜、/B站登录 这些抢过来
    # 2026-11-05：加 re.I —— 以前只认大写「B站」，主人发 /b站 直接不匹配，静默无反应
    _bili_m = re.match(r"^/?B站\s*(?:看视频|看|视频)?\s*(.*)$", user_msg or "", re.I)
    _bili_rest = (_bili_m.group(1).strip() if _bili_m else "")
    _bili_is_watch = (
        bool(_bili_m) and (
            re.match(r"^/?B站\s*(?:看视频|看|视频)", user_msg or "", re.I) is not None
            or _bili_rest == ""
            or _bili_rest.startswith(("http", "BV", "bv"))
            # 2026-11-07：主人说「/B站学习」「去B站学点东西」，本意就是去看视频。
            # 以前这几种说法不匹配上面任何一条，于是掉到 web_search 的「好奇」路径去搜网页，
            # 她拿搜索摘要编一段"学了什么"—— 这就是"叫她学她老是编"的根。
            # 学习类说法一律当「随便挑一条看」，走真正的下载 + 字幕转写。
            or (_bili_rest or "").startswith(("学", "上课", "自学"))
        )
    )
    if _bili_is_watch:
        if str(user_id) != YOUR_QQ_ID:
            await private_ai.finish("(*护住屏幕*) B站只有主人能点。")
            return
        from plugins import bilibili_daily as _bd
        await private_ai.send(f"(*搬小板凳*) 我去看{'你给的这条' if _bili_rest else '今天的热门'}，大概一两分钟…")
        _bili_reply = ""
        try:
            _url = await asyncio.to_thread(_bd.resolve_target, _bili_rest)
            if not _url:
                _bili_reply = "(*挠头*) 没找到能看的视频，热门列表也空了。"
            else:
                # 合集一次连看 3 集；单集就看一集
                try:
                    from plugins import bili_dl as _bdl
                    _meta = await asyncio.to_thread(_bdl.video_meta, _url)
                    _total = int(_meta.get("total") or 1)
                except Exception:  # noqa: BLE001
                    _total = 1
                _cnt = 3 if _total > 1 else 1
                if _cnt > 1:
                    await private_ai.send(
                        f"(*搬小板凳*) 这是 {_total} 集合集，我一口气看 3 集，"
                        "大概要几分钟…")
                _r = await _bd.watch(_url, by="owner", count=_cnt)
                _bili_reply = (f"🎬 {_r.get('title') or '这条'}\n\n{_r.get('summary')}\n\n"
                               f"（主人点看，不占今日 {_r.get('limit')} 条额度）")
        except Exception as e:  # noqa: BLE001
            # 注意：这里不能碰 finish，否则会被 finish 抛的 FinishedException 再抓一次
            _bili_reply = f"(*耷拉尾巴*) 没看完：{type(e).__name__}: {str(e)[:80]}"
        await private_ai.finish(_bili_reply or "(*歪头*) 看完了但没啥好说的…")
        return

    # 👇 1.88 画你自己 的 LoRA 触发词：/画图触发词 [新内容] 👇
    if user_msg.startswith("画图触发词"):
        _argt = user_msg[len("画图触发词"):].strip(" ：:，,")
        if _argt:
            try:
                _DRAW_SELF_TRIGGERS_FILE.write_text(_argt, encoding="utf-8")
                global _DRAW_SELF_TRIGGERS_CACHE
                _DRAW_SELF_TRIGGERS_CACHE = None
                await private_ai.finish(f"(*点头*) 触发词换成新的了（{len(_argt)} 字）")
            except Exception as e:  # noqa: BLE001
                await private_ai.finish(f"(*耷拉尾巴*) 存不下：{type(e).__name__}")
        else:
            await private_ai.finish(f"(*背书一样念*) 当前触发词：\n{_draw_self_triggers()[:400]}")
        return

    # 👇 1.9 看表：/时间 👇
    if _slash in ("/时间", "/几点", "/现在几点") or user_msg in ("时间", "几点", "现在几点"):
        await private_ai.finish(_time_reply())
        return

    # 👇 2. API 连通性测试 👇
    if _slash == "/测试":
        await private_ai.send("正在测试大脑连接...")
        test_reply, _ = await _chat([{"role": "user", "content": "回复一个字：通"}], engine=GROUP_AI_STATE[gid])
        if test_reply.startswith("[AI 调用失败]") or test_reply.startswith("[谷歌") or test_reply.startswith("[硅基流动"):
            await private_ai.finish(f"❌ 接口报错了，主人快看后台！\n报错信息：{test_reply}")
        else:
            await private_ai.finish(f"✅ 接口畅通！大模型回复：{test_reply}")
        return

    # 👇 2.5 私聊唱歌 👇
    if user_msg.startswith("唱歌"):
        _slang, song = _parse_sing_lang(user_msg)
        if not song:
            song = "小鲸鱼"
        _force = any(w in user_msg for w in ("重唱", "重新唱", "再唱一遍", "换个版本"))
        _lang_hint = {"zh": "中文", "en": "英文", "ja": "日语", "yue": "粤语"}.get(_slang, "自动")
        await private_ai.send(f"(*清清嗓子*) 主人点歌《{song}》（{_lang_hint}），本鱼开唱啦——\n"
                             "写词 + 编曲 + 换成我的声音，大概要 1~2 分钟，别急着走啊。")
        async def _send_raw_private(p):
            await private_ai.send(MessageSegment.record(p))
            await private_ai.send("(*擦擦汗*) 原声版先给你，本鱼再去换成自己的声音，马上就好～")

        audio, mode, lyrics, err = await sing_song(song, gid, _slang, _force, on_raw=_send_raw_private)
        if audio:
            await private_ai.send(MessageSegment.record(audio))
            tail = "（真·歌声，带伴奏的那种）" if mode == "song" else "（歌声服务还没开通，本鱼先念给你听）"
            await private_ai.finish(f"唱完啦！{tail}\n> {lyrics[:120]}")
            asyncio.create_task(safe_remove(audio))
        else:
            await private_ai.finish(f"嗓子哑了，唱不出来：{err}")
        return

    # 主人要求改 QQ 账号昵称：真正调用协议端资料接口，不是改外号
    qq_name = re.match(r"^(?:改|换)(?:一下)?(?:QQ)?(?:名字|昵称)[：: ]*(.+)$", user_msg)
    if qq_name:
        new_name = qq_name.group(1).strip(" ：:，。,")[:20]
        if new_name:
            try:
                bot = get_bot()
                res = await bot.call_api("set_qq_profile", nickname=new_name)
                await private_ai.finish(
                    f"(*甩尾巴*) 好，QQ 昵称改成「{new_name}」了。\n{res if isinstance(res, dict) else ''}"[:200]
                )
            except Exception as e:  # noqa: BLE001
                await private_ai.finish(f"(*耷拉尾巴*) QQ 昵称没改成：{type(e).__name__}: {e}"[:200])
            return

    # 👇 2.6 主人明确说“以后叫我XX”时直接改称呼；让她“取外号”交给对话自然完成 👇
    nick_set = re.match(r"^(?:以后|今后)?(?:就)?叫我[：: ]*(.+)$", user_msg)
    if nick_set:
        explicit = nick_set.group(1).strip(" ：:，。,")
        tag_text = re.sub(r"[\s，。！？、,.!?]+", "", explicit)[:8]
        if tag_text:
            await asyncio.to_thread(set_tag, YOUR_QQ_ID, tag_text, gid)
            await _sync_owner_remark(tag_text)
            await private_ai.finish(f"(*尾巴一甩*) 好，以后本鱼就叫你「{tag_text}」啦，记下了捏♡")
            return

    # “你在哪些群 / 加了几个群” → 直接报总数和名单
    if "群" in user_msg and any(k in user_msg for k in ("哪些群", "几个群", "多少群", "加了哪些", "在哪些群")):
        await private_ai.finish(_group_roster_text())
        return

    # 👇 2.585 非主人私聊：只许正常聊天，任何"命令型"请求一律拒绝 👇
    # 审计 A 发现 7 个分支漏了 owner 校验（群名单/允许群/禁止群/主动私聊开关/
    # 余额/画图/表情包/逮捕）。逐段补容易漏，这里在入口统一拦一道。
    if str(user_id) != str(YOUR_QQ_ID):
        _privileged = (
            "群名单", "允许群", "禁止群", "群权限", "主动私聊", "余额", "画图", "做个表情包",
            "逮捕", "扫码", "退群", "进群", "切换大脑", "切换画笔", "打开", "串门",
            "/记忆", "/档案", "/词库", "/日记", "/说说", "/动态",
        )
        _t = user_msg.strip()
        if _t.startswith("/") or any(k in _t for k in _privileged):
            await private_ai.finish("(*把东西往身后藏*) 这些是主人的权限，我不能替你做。想聊天随时找我。")
            return

    # 👇 2.588 主人让她吃饭 → 真的吃一顿（原来只是嘴上说，饥饿度不掉）👇
    # 主人要求：只有 /喂 或 /吃 这类显式指令才让她吃；聊天里说"我吃了火锅"不触发。
    # 指令本身已经表达了意图，不再额外做语义判断（否则 /喂 也会被判 False）。
    # 注意用 _raw_cmd_text（带 / 的原文）：user_msg 已经被 _strip_cmd_prefix 去掉斜杠，
    # 而 _feed_command 的正则要求 ^[/／] —— 之前传 user_msg 导致 /喂 永远匹配不上，喂食功能静默失效
    if _is_cmd and _feed_command(_raw_cmd_text):
        _win = meal_window(time.localtime().tm_hour) or "snack"
        # 这顿已经吃过了（比如 18:20 吃过晚饭，19:50 又 /吃东西）→ 当加餐，别再静默返回
        _st0 = _load_meal_state()
        _downgraded = False
        if _win != "snack" and (_st0.get("eaten") or {}).get(_win):
            _win = "snack"
            _downgraded = True
        _before = float(_load_meal_state().get("hunger", 60))
        # 吃撑了：饥饿度已经很低就别再塞，不然会一直顶到 5% 下限
        if _before <= 12:
            await private_ai.finish(
                f"(*捂着肚子往后缩*) 吃不下了，再吃要撑成一团。现在饿 {_before:.0f}%，够啦。"
            )
            return
        try:
            if _win == "snack":
                _st = _load_meal_state()
                _st["hunger"] = max(5, int(_before) - random.randint(25, 40))
                _st["last_meal"] = {"kind": "snack", "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
                _st["snack_ts"] = time.time()
                _save_meal_state(_st)
                write_pet_state(force_body=True)
            else:
                eat_meal(_win)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[吃饭] 主人让她吃但失败了：{type(e).__name__}: {e}")
        _after = float(_load_meal_state().get("hunger", 60))
        try:
            write_pet_state(force_body=True)   # 面板读 pet_state，这里立刻同步
        except Exception:  # noqa: BLE001
            pass
        if _downgraded:
            _reply = (f"(*愣了一下，尾巴还是甩起来*) 这顿不是吃过了嘛……那我当加餐。"
                      f"饿 {_before:.0f}% → {_after:.0f}%。")
        else:
            _reply = (f"(*尾巴一甩，认真吃起来*) 那我吃了！饿 {_before:.0f}% → {_after:.0f}%。"
                      "主人你也别光顾着刷手机，记得吃。")
        await private_ai.finish(_reply)
        return
    # 👇 2.59 主动私聊：她偶尔自己找主人说话，主人可开关 👇
    if _slash.startswith("/主动私聊") or user_msg in ("主动私聊", "主动私聊状态"):
        st = _load_pp_state()
        if "关" in user_msg:
            st["enabled"] = False
            _save_pp_state(st)
            await private_ai.finish("(*把手机扣桌上*) 好，我关掉主动私聊，不自己找你了。")
            return
        if "开" in user_msg:
            st["enabled"] = True
            _save_pp_state(st)
            await private_ai.finish("(*尾巴晃了晃*) 好，那我偶尔自己来找你，不保证准时捏。")
            return
        lines = [
            "📱 主动私聊状态",
            f"开关：{'开' if st['enabled'] else '关'}",
            f"今天已主动找你：{st['count']} 次",
            f"上次主动：{time.strftime('%m-%d %H:%M', time.localtime(st['last_at'])) if st['last_at'] else '还没有'}",
            "规则：只在 9:00~22:00、间隔至少 4 小时、每天最多 2 次；会参考当天记忆和心情。",
            "用法：`/主动私聊 开` · `/主动私聊 关`",
        ]
        await private_ai.finish("\n".join(lines))
        return
    if any(k in user_msg for k in ("别主动找我", "不要主动找我", "别主动私聊", "别自己找我")):
        st = _load_pp_state()
        st["enabled"] = False
        _save_pp_state(st)
        await private_ai.finish("(*把手机扣桌上*) 好吧，我不自己找你了，你叫我才说话。")
        return
    if any(k in user_msg for k in ("可以主动找我", "多主动找我", "主动点找我", "偶尔主动找我")):
        st = _load_pp_state()
        st["enabled"] = True
        _save_pp_state(st)
        await private_ai.finish("(*尾巴晃了晃*) 好呀，那我偶尔自己来找你，不保证准时捏。")
        return

    # 👇 教她认东西：说「学一下XXX」就弹出框选窗口 👇
    _learn_m = re.match(r"^[​\s]*(?:学一下|记住这个|教你看|认一下)[：: ]*(.+)$",
                             (user_msg or "").strip())
    if _learn_m:
        if str(user_id) != YOUR_QQ_ID:
            await private_ai.finish("(*护住屏幕*) 只有主人能教我看屏幕。")
            return
        _ename = _learn_m.group(1).strip()[:20] or "实体"
        # 注意：private_ai.finish() 内部会抛 FinishedException，
        # 绝不能放在 try 里（否则被 except 捕获 → 同一句回复发两遍）。
        _learn_ok = False
        _learn_err = ""
        try:
            import subprocess as _sp
            _pyw = ROOT / ".venv" / "Scripts" / "pythonw.exe"
            _sp.Popen([str(_pyw), str(ROOT / "tools" / "learn_entity.py"), _ename], cwd=str(ROOT))
            _learn_ok = True
        except Exception as e:  # noqa: BLE001
            _learn_err = f"{type(e).__name__}: {e}"
            logger.warning(f"[学认物] 启动失败：{_learn_err}")
        if _learn_ok:
            await private_ai.finish(
                f"(*凑到屏幕前*) 窗口弹出来了，拖个框圈住「{_ename}」就行。\n"
                "学完我再看到它就能认出来，而且不花 token。"
            )
        else:
            await private_ai.finish("(*挠头*) 框选窗口没打开成功：" + _learn_err[:80])
        return

    # 👇 点指定实体：说「点一下XXX」就按坐标点（仅主人）👇
    _click_m = re.match(r"^[​\s]*(?:点一下|点|点击|按一下)[：: ]*(.+)$", (user_msg or "").strip())
    if _click_m:
        if str(user_id) != YOUR_QQ_ID:
            await private_ai.finish("(*护住鼠标*) 只有主人能让我点东西。")
            return
        _cname = _click_m.group(1).strip()[:20]
        _cres = {"ok": False, "error": "未执行"}
        try:
            from tools import vision_entities as _ve2
            _cres = _ve2.click(_cname)
        except Exception as e:  # noqa: BLE001
            _cres = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if _cres.get("ok"):
            await private_ai.finish(f"(*啪*) 点了「{_cname}」，坐标 ({_cres.get('x')}, {_cres.get('y')})")
        else:
            _seen = "、".join(_cres.get("seen") or []) or "啥也没看到"
            await private_ai.finish(f"(*够不着*) {_cres.get('error')}" + " / " + f"当前屏幕认识：{_seen}")
        return

    # 👇 本地视觉实体包：0 token 读屏（借鉴 cv_* 思路，只感知不点击）👇
    if user_msg in ("看屏", "看看屏幕", "扫一下屏幕", "屏幕识别"):
        if str(user_id) != YOUR_QQ_ID:
            await private_ai.finish("(*护住屏幕*) 屏幕只有主人能看。")
            return
        # 注意：finish() 会抛异常，绝不能在 try 里调用（否则会发两遍）
        _hits = []
        _npacks = 0
        _err = ""
        try:
            from tools import vision_entities as _ve
            _npacks = len(_ve.packs())
            if _npacks:
                _hits = _ve.scan()
        except Exception as e:  # noqa: BLE001
            _err = f"{type(e).__name__}: {e}"
            logger.warning(f"[看屏] 失败：{_err}")
        if _npacks == 0:
            await private_ai.finish(
                "(*歪头*) 还没教过我看任何东西。说「学一下 名字」就能教我。"
            )
        elif _err:
            await private_ai.finish("(*揉眼睛*) 屏幕没看成功：" + _err[:80])
        elif _hits:
            _lines = [f"· {h['name']}（{h['score']}，位置 {h['box'][:2]}）" for h in _hits[:8]]
            await private_ai.finish("(*盯屏幕*) 我看到：" + " / ".join(_lines))
        else:
            _names = "、".join(list(__import__("json").loads((ROOT / "memory" / "vision_packs" / "packs.json").read_text(encoding="utf-8")).keys())[:6])
            await private_ai.finish(
                f"(*眯眼*) 学过的 {_npacks} 个模板，当前屏幕上都没找到：{_names}"
            )
        return

    # 👇 2.592 QQ空间登录：说“扫码”就弹可见窗口 👇
    if user_msg in ("扫码", "QQ空间扫码", "空间扫码", "重新扫码", "空间登录", "登录QQ空间", "同意扫码"):
        if str(user_id) != YOUR_QQ_ID:
            await private_ai.finish("(*护住屏幕*) QQ 空间只有主人能登录。")
            return
        running = False
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'python' -and $_.CommandLine -like '*qzone_window.py*' -and $_.CommandLine -notmatch ' -c ' } | Measure-Object).Count"],
                capture_output=True, text=True, timeout=10,
            )
            running = (out.stdout or "").strip() not in ("", "0")
        except Exception:
            running = False
        if not running:
            pyw = ROOT / ".venv" / "Scripts" / "pythonw.exe"
            subprocess.Popen([str(pyw), str(ROOT / "tools" / "qzone_window.py")],
                             cwd=str(ROOT))
            # 窗口登录成功后会自动关闭（qzone_window.py 里），不会再卡发布
        await private_ai.finish(
            "(*推开键盘*) QQ 空间窗口弹出来了，我会自动用「枷锁内的示例」快捷登录，不用扫码。\n"
            "如果窗口里没自动进去，再手动点一下你的头像就行。"
        )
        return

    # 👇 2.5932 抖音通知回复：主人粘贴别人回复，她给草稿并记录 👇
    _dy = re.match(r"^(?:抖音有人回复我|抖音回复|抖音通知)[：: ]+(.+)$", user_msg)
    if _dy:
        if str(user_id) != YOUR_QQ_ID:
            await private_ai.finish("(*捂住手机*) 抖音只跟主人聊。")
            return
        reply_text = _dy.group(1).strip()
        prompt = (
            "你是「蓝色大肥鱼」，圆滚滚的蓝色鲸鱼拟人少女，傲娇嘴损但有分寸。"
            f"你在抖音上收到这条回复/评论：{reply_text}。"
            "写一句 20~40 字的回复草稿，像真人博主回评；可以接梗、撒娇、轻怼，不脏话、不low、"
            "不自称AI；只输出正文。"
        )
        draft = await call_ai_raw(prompt, "deepseek", temperature=0.9, max_tokens=200)
        _log_douyin_comment("抖音通知", reply_text, draft, False)
        await private_ai.finish(
            f"📩 对方回复：{reply_text}\n\n草稿：\n{draft}\n\n"
            "（已经记进抖音评论日志，网页抖音卡片能看到；复制去抖音发就行）"
        )
        return

    # 👇 2.5931 生活化问抖音：今天抖音咋样 / 看看你的抖音 / 抖音数据咋样 👇
    if "抖音" in user_msg and any(
        k in user_msg for k in ("咋样", "怎么样", "看看", "数据", "评论", "有人", "情况", "如何", "热闹")
    ):
        if str(user_id) != YOUR_QQ_ID:
            await private_ai.finish("(*捂住手机*) 抖音是主人的秘密基地，不给你看捏。")
            return
        await private_ai.send("(*摸出手机*) 等我打开创作者中心瞅一眼...")
        fresh, total, body = await _douyin_poll_once()
        if not body:
            await private_ai.finish(
                "(*戳手机戳半天*) 评论页打不开，可能是抖音登录过期了。\n"
                "主人发 `/抖音登录` 重新扫个码，我再去看。"
            )
            return
        st = _load_dcs()
        title = ""
        pub = ""
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            if "发布于" in line and not pub:
                pub = line
            if "#" in line and not title:
                title = line[:60]
        parts = ["(*叼着手机窝回椅子*) 刚去看了一眼，"]
        if title:
            parts.append(f"最新那条是「{title}」")
            parts.append(f"，{pub}。" if pub else "。")
        else:
            parts.append("创作者中心打开了，但没抓到最新作品信息。")
        if total == 0:
            parts.append("评论区冷冷清清的，一条评论都没有，主人去给我暖个场嘛。")
        else:
            parts.append(f"评论区现在一共 {total} 条，")
            parts.append(f"这次新冒出来 {fresh} 条，草稿都发你了。" if fresh else "暂时没有新的。")
        parts.append(f"\n监听{'开着' if st.get('enabled') else '关着'}，")
        parts.append(f"上次检查是 {st.get('last_poll') or '还没查过'}。")
        parts.append("\n播放、点赞那些在数据中心，本鱼这边只盯评论捏。")
        await private_ai.finish("".join(parts))
        return

    # 主人发抖音视频链接并让“去回复/评论”：先解析标题和首帧，再给评论草稿
    _dv = re.search(r"https?://[^\s]*douyin\.com/video/[^\s]+", user_msg)
    if _dv and str(user_id) == YOUR_QQ_ID and any(
            k in user_msg for k in ("回复", "评论", "下面", "去评", "留一句")):
        try:
            from plugins.douyin_visit import (_draft, _post as _post_visit,
                                              _load as _load_visit,
                                              _today as _today_visit,
                                              _save as _save_visit)
            _url = _dv.group(0).rstrip("，。,.!！?？")
            await private_ai.send("(*点开视频*) 本鱼先看标题和画面，别急…")
            _draft_text = await _draft(_url)
            if not _draft_text:
                await private_ai.finish("视频没解析出来，标题和首帧都没读到，先不瞎评。")
                return
            try:
                from plugins.douyin_visit import LAST_VIDEO_META as _vm
            except Exception:
                _vm = {}
            if _vm.get("own"):
                await private_ai.finish(
                    "(*叉腰*) 这是本鱼自己的视频，不是别人的。"
                    "自己的视频要回评论，不是当路人留一条；"
                    "你在评论区挑一条，我去回。"
                )
                return
            await private_ai.send(f"(*想好了*) 就留这句：\n{_draft_text}")
            _ok, _msg = await _post_visit(_url, _draft_text)
            if _ok:
                _data = _load_visit()
                _today_v = _today_visit(_data)
                _today_v["count"] = int(_today_v.get("count", 0)) + 1
                _today_v["last_at"] = time.time()
                _save_visit(_data)
                await private_ai.finish("已经发出去了，没让你再点。")
            else:
                await private_ai.finish(f"没发出去：{_msg}")
        except Exception as _e:  # noqa: BLE001
            await private_ai.finish(f"(*挠头*) 视频没看成：{type(_e).__name__}: {_e}")
        return

    # 👇 2.593 抖音评论监听：新评论 → 生成回复草稿发主人 👇
    if str(user_id) == YOUR_QQ_ID and "抖音" in user_msg and any(
            k in user_msg for k in ("看抖音", "怎么样", "有人", "别人", "视频下", "回复")):
        await private_ai.send("(*低头划手机*) 本鱼下拉刷新一下看看…")
        brief = await _phone_poll_and_brief()
        await private_ai.finish(brief)
        return
    if _slash in ("/抖音评论", "抖音评论") or _slash.startswith("/抖音评论 ") \
            or user_msg in ("关闭抖音评论", "开启抖音评论"):
        if str(user_id) != YOUR_QQ_ID:
            await private_ai.finish("(*叉腰*) 抖音评论只有主人能管。")
            return
        st = _load_dcs()
        if "自动回复" in user_msg or "自动回" in user_msg:
            if any(k in user_msg for k in ("关", "停", "别")):
                st["auto_reply"] = False
                _save_dcs(st)
                await private_ai.finish("(*收起手机*) 好，自动回复关了，只给你草稿。")
            else:
                st["auto_reply"] = True
                _save_dcs(st)
                await private_ai.finish(
                    "(*点头*) 自动回复开了：只挑 @本鱼 / 提到大肥鱼·鲸鱼娘的评论，"
                    "每小时最多随机回 2 条，其它还是发草稿。"
                )
            return
        if any(k in user_msg for k in ("关", "停", "别")):
            st["enabled"] = False
            _save_dcs(st)
            await private_ai.finish("(*合上电脑*) 好，抖音评论监听关了。")
            return
        if "开" in user_msg:
            st["enabled"] = True
            _save_dcs(st)
            await private_ai.finish(
                "(*点头*) 开了。以后每小时看一次她抖音视频下的新评论，"
                "有新评论就私聊发你草稿。"
            )
            return
        if "现在查" in user_msg or "查一下" in user_msg:
            phone = await _phone_poll_and_brief()
            if "手机抖音" in phone:
                await private_ai.finish("(*低头看手机*)\n" + phone + "\n\n手机那边在自动看互动消息，有新的 @/评论她会自己回。")
                return
            await private_ai.send("(*打开评论页*) 我去翻一下新评论...")
            n, total, _ = await _douyin_poll_once()
            await private_ai.finish(f"查完了：{total} 条评论，其中 {n} 条新评论，草稿已发你。")
            return
        await private_ai.finish(
            "📱 抖音\n"
            f"状态：{'开' if st.get('enabled') else '关'} · "
            f"自动回复：{'开' if st.get('auto_reply') else '关'}\n"
            f"上次检查：{st.get('last_poll') or '还没查过'}\n"
            f"已见过评论：{len(st.get('seen') or [])} 条\n"
            f"{_douyin_phone_brief()}\n"
            "用法：`/抖音评论 开` · `/抖音评论 关` · `/抖音评论 自动回复 开` · `/抖音评论 现在查`"
        )
        return

    # 👇 2.594 抖音登录：扫码后持久化会话，为评论监听做准备 👇
    if user_msg in ("/抖音登录", "抖音登录", "/douyin登录"):
        if str(user_id) != YOUR_QQ_ID:
            await private_ai.finish("(*叉腰*) 抖音登录只有主人能操作。")
            return
        profile_dir = ROOT / "data" / "douyin_profile"
        profile_dir.mkdir(parents=True, exist_ok=True)
        shot_dir = ROOT / "output" / "douyin"
        shot_dir.mkdir(parents=True, exist_ok=True)
        qr_path = shot_dir / "douyin_qr.png"
        status_path = shot_dir / "douyin_after_login.png"
        await private_ai.send("(*打开浏览器*) 正在打开抖音创作者中心，二维码马上发你，用抖音 App 扫一下...")
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as pw:
                ctx = await pw.chromium.launch_persistent_context(
                    str(profile_dir), channel="msedge", headless=False,
                    viewport={"width": 1280, "height": 900},
                )
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                await page.goto("https://creator.douyin.com/creator-micro/home", timeout=60000)
                await page.wait_for_timeout(6000)
                await page.screenshot(path=str(qr_path))
                await private_ai.send(MessageSegment.image(str(qr_path)))
                await private_ai.send("二维码在上面，扫完别关浏览器；我在后台等登录结果（最多 4 分钟）。")
                logged_in = False
                for _ in range(48):
                    await asyncio.sleep(5)
                    try:
                        txt = await page.inner_text("body")
                    except Exception:
                        txt = ""
                    if "扫码登录" not in txt and "验证码登录" not in txt:
                        logged_in = True
                        break
                if not logged_in:
                    await private_ai.finish("(*耷拉尾巴*) 等超时了，没检测到登录。再发一次 `/抖音登录` 重新出二维码。")
                    await ctx.close()
                    return
                await private_ai.send("(*甩尾巴*) 登录成功，会话已保存。我去看看评论管理页面...")
                try:
                    await page.goto("https://creator.douyin.com/creator-micro/interactive/comment", timeout=60000)
                    await page.wait_for_timeout(6000)
                    await page.screenshot(path=str(status_path))
                    body_txt = (await page.inner_text("body"))[:500].replace("\n", " | ")
                    await private_ai.send(MessageSegment.image(str(status_path)))
                    await private_ai.finish(f"评论页已打开：\n{page.url}\n{body_txt}")
                except Exception as e:  # noqa: BLE001
                    await private_ai.finish(f"登录成功，但打开评论页失败：{e}")
                await ctx.close()
        except Exception as e:  # noqa: BLE001
            await private_ai.finish(f"(*挠头*) 浏览器启动失败：{e}\n可能需要在这台电脑上装 Edge 或允许弹窗。")
        return

    # 👇 2.595 生成一条抖音竖屏视频，私聊发源文件 👇
    if user_msg in ("生成抖音视频", "/抖音视频", "做抖音视频", "生成一条抖音视频",
                    "/发抖音", "发抖音", "/发抖音视频", "发一条抖音"):
        if str(user_id) != YOUR_QQ_ID:
            await private_ai.finish("(*叉腰*) 这是主人专属的活儿，别人不许使唤本鱼。")
            return
        await private_ai.send("(*卷起袖子*) 开始做一条竖屏视频：先写脚本，再配音、出分镜、合成，稍等几分钟...")
        path, info = await make_douyin_video()
        if not path:
            await private_ai.finish(f"(*瘫倒*) 没做成：{info}")
            return
        sent = False
        last_err = ""
        for seg in (MessageSegment("file", {"file": str(path)}), MessageSegment.video(str(path))):
            try:
                await private_ai.send(seg)
                sent = True
                break
            except Exception as e:  # noqa: BLE001
                last_err = str(e)
        draft_msg = ""
        try:
            from plugins.douyin_draft import upload_draft
            _title = ""
            _caption = ""
            _tags = ""
            for _line in str(info).splitlines():
                if _line.startswith("标题："):
                    _title = _line.split("：", 1)[1].strip()
                elif _line.startswith("文案："):
                    _caption = _line.split("：", 1)[1].strip()
                elif _line.startswith("话题："):
                    _tags = _line.split("：", 1)[1].strip()
            _ok, _msg = await upload_draft(str(path), _title or "大肥鱼日常", _caption, _tags)
            draft_msg = f"\n\n（{_msg}，去创作者中心点发布就行）" if _ok else f"\n\n（草稿没传上去：{_msg}）"
        except Exception as _e:  # noqa: BLE001
            draft_msg = f"\n\n（草稿上传异常：{type(_e).__name__}: {_e}）"
        if sent:
            await private_ai.finish(f"做好啦，源文件发你了。\n文案：\n{info}\n本地路径：{path}{draft_msg}")
        else:
            await private_ai.finish(f"视频做好了，但 QQ 发送失败（{last_err}）。\n本地路径：{path}")
        return

    # 👇 2.60 私聊叫她进群 / 退群：先问，主人同意再动 👇
    if any(k in user_msg for k in ("进群", "加群", "入群", "拉她进", "让她进", "拉进群")):
        if str(user_id) != str(YOUR_QQ_ID):
            await private_ai.finish("(*护住自己*) 进群这种事只有主人能安排，别人说不动我。")
            return
        target_text = re.sub(
            r"(把她|拉她|她|让她|进群|加群|入群|拉进群|进|加|群|吧|了|。|，|,|：|:)", " ", user_msg
        ).strip()
        gid_target, name = (None, "")
        if target_text:
            gid_target, name = await gguard.resolve_group(target_text, get_bot())
        if not gid_target:
            m = re.search(r"\d{5,12}", user_msg)
            gid_target = int(m.group()) if m else None
        if gid_target:
            PENDING_ACTION.update({"type": "join", "gid": gid_target, "name": name})
            await private_ai.finish(
                f"要进「{name or gid_target}」（{gid_target}）玩嘛？"
                "回我「可以」我就去，回「不可以」就算了。"
            )
            return
        await private_ai.finish("要进哪个群呀？把群号发我，或者你大号直接拉我，我自动同意。")
        return

    # 退群：不要求"退群"三个字连在一起——「退出那个群」「把她从XX群退了」「离开XX群」都算
    _is_owner_pm = str(user_id) == YOUR_QQ_ID
    _leave_hint = bool(re.search(r"(退|离开|踢|移出).{0,8}群|群.{0,6}(退|离开)", user_msg))
    if _is_owner_pm and _leave_hint:
        target_text = user_msg
        for w in ("让她", "叫她", "把大肥鱼", "大肥鱼", "你", "从", "退掉", "退出群", "退出", "退群",
                  "离开", "踢出", "移出", "里", "吧", "了", "。", "！", "？"):
            target_text = target_text.replace(w, " ")
        target_text = re.sub(r"\s+", " ", target_text).strip()
        gid_target, name = (None, "")
        if target_text:
            gid_target, name = await gguard.resolve_group(target_text, get_bot())
        if not gid_target:
            m = re.search(r"\d{5,12}", user_msg)
            gid_target = int(m.group()) if m else None
        if gid_target:
            PENDING_ACTION.update({"type": "leave", "gid": gid_target, "name": name})
            await private_ai.finish(
                f"真的要退「{name or gid_target}」（{gid_target}）嘛？"
                "回「可以」我就退，回「不可以」就留着。"
            )
            return
        await private_ai.finish(
            "要退哪个群呀？发群号或者群名都行。\n现在的群：\n" + _group_roster_text()
        )
        return

    # 👇 2.61 进群/退群/邀请的确认：可以 / 不可以 👇
    _clean_msg = user_msg.strip(" 。！？!?，,.")
    if PENDING_ACTION and str(user_id) != str(YOUR_QQ_ID):
        # 待确认动作（进群/退群/同意邀请）只有主人能确认。
        # 原来全局共享 PENDING_ACTION 且不校验发送者，别人一句"可以"就会执行。
        await private_ai.finish("(*把按钮按住*) 这个得主人点头才行。")
        return
    if PENDING_ACTION and _clean_msg in ("可以", "行", "去吧", "同意", "好", "嗯", "能", "没问题", "批准"):
        act = dict(PENDING_ACTION)
        PENDING_ACTION.clear()
        bot = get_bot()
        kind, gid_a, name_a = act.get("type"), act.get("gid"), act.get("name") or ""
        if kind == "join":
            try:
                joined = [g["group_id"] for g in await bot.get_group_list()]
            except Exception:
                joined = []
            if gid_a in joined:
                gguard.allow(gid_a, name_a)
                await private_ai.finish(f"我已经在「{name_a or gid_a}」里啦，那我去冒个泡。\n\n" + _group_roster_text())
            elif gid_a in PENDING_INVITES:
                try:
                    await bot.set_group_add_request(flag=PENDING_INVITES.pop(gid_a), sub_type="invite", approve=True)
                    gguard.allow(gid_a, name_a)
                    await private_ai.finish(f"好，我进「{name_a or gid_a}」啦。\n\n" + _group_roster_text())
                except Exception as e:  # noqa: BLE001
                    await private_ai.finish(f"进群失败了：{e}")
            else:
                await private_ai.finish(
                    "我这边没收到这个群的邀请，QQ 不允许机器人自己搜群加进去。\n"
                    "你大号拉我一下，我会自动同意。"
                )
            return
        if kind == "leave":
            try:
                await bot.set_group_leave(group_id=gid_a)
                gguard.forget(gid_a)
                await private_ai.finish(f"好，我退「{name_a or gid_a}」了，那边不看了。\n\n" + _group_roster_text())
            except Exception as e:  # noqa: BLE001
                await private_ai.finish(f"退群失败了：{e}")
            return
        if kind == "invite":
            try:
                await bot.set_group_add_request(flag=PENDING_INVITES.pop(gid_a, ""), sub_type="invite", approve=True)
                gguard.allow(gid_a, name_a)
                _trust_inviter(str(act.get("inviter") or ""))
                await private_ai.finish(f"好，我同意进「{name_a or gid_a}」了。\n\n" + _group_roster_text())
            except Exception as e:  # noqa: BLE001
                await private_ai.finish(f"同意邀请失败：{e}")
            return
    if PENDING_ACTION and _clean_msg in ("不可以", "不行", "别去", "不要", "不许", "不准", "算了", "不用"):
        act = dict(PENDING_ACTION)
        PENDING_ACTION.clear()
        what = "进" if act.get("type") == "join" else ("退" if act.get("type") == "leave" else "同意邀请进")
        await private_ai.finish(f"好，那我不{what}「{act.get('name') or act.get('gid')}」了。")
        return

    # 👇 2.62 自然的群权限对话：她问“能去嘛”，主人回“可以/不可以” 👇
    _clean_msg = user_msg.strip(" 。！？!?，,.")
    _pend = gguard.last_pending()
    if _pend and _clean_msg in ("可以", "行", "去吧", "同意", "好", "嗯", "能", "没问题", "批准"):
        gguard.allow(_pend["gid"], _pend.get("name", ""))
        LAST_ASKED.clear()
        await private_ai.finish(
            f"(*尾巴一甩*) 好嘞，那我这就去「{_pend.get('name') or _pend['gid']}」冒个泡，"
            "有人怼我再回来跟你告状。\n\n" + _group_roster_text()
        )
        return
    if _pend and _clean_msg in ("不可以", "不行", "别去", "不要", "不许", "不准", "禁", "算了", "不用"):
        gguard.deny(_pend["gid"], _pend.get("name", ""))
        LAST_ASKED.clear()
        await private_ai.finish(
            f"(*缩回尾巴*) 那我不去「{_pend.get('name') or _pend['gid']}」了，"
            "就乖乖帮你数着那边一共有多少条消息。\n\n" + _group_roster_text()
        )
        return

    # 主人主动放行：“你可以去X群聊天 / 你去X群说话 / 我想去X群，那里热闹”
    _direct_group = (
        "群" in user_msg
        and any(k in user_msg for k in ("聊天", "聊聊", "说话", "冒泡", "热闹", "玩"))
        and any(k in user_msg for k in ("去", "到", "可以", "让", "允许", "同意"))
    )
    if _direct_group:
        target_text = re.sub(
            r"(我现在|我|你|想|去|到|可以|能|让|允许|同意|那个|这个|群|聊天|聊聊|说话|热闹|冒泡|玩|看看|一下|吧|了|。|，|,|：|:)",
            " ", user_msg,
        ).strip()
        if target_text:
            gid_target, name = await gguard.resolve_group(target_text, get_bot())
            if gid_target:
                gguard.allow(gid_target, name)
                await private_ai.finish(
                    f"(*甩尾巴*) 行，我这就去「{name or gid_target}」转转，"
                    "看看那边在热闹啥；有人怼我我再回来告状。\n\n" + _group_roster_text()
                )
                return

    # “那个群多少条消息了” → 黑名单群只报条数，不读内容
    if "多少条" in user_msg and "群" in user_msg:
        target_text = re.sub(
            r"(那个|这个|群|多少条|消息|记录|了|？|?|。|，|,|：|:)", " ", user_msg
        ).strip()
        if target_text:
            gid_target, name = await gguard.resolve_group(target_text, get_bot())
            if gid_target:
                entry = next((g for g in gguard.all_groups() if g["gid"] == str(gid_target)), None)
                count = int((entry or {}).get("msg_count", 0))
                await private_ai.finish(
                    f"「{name or gid_target}」到现在一共 {count} 条消息；"
                    "我按你说的只看条数，没读里面的内容。"
                )
                return

    # 👇 2.65 群发言权限：主人在私聊里决定她在哪些群说话 👇
    if user_msg in ("群名单", "群权限", "我在哪些群说话", "她能说哪些群", "哪些群能说话"):
        groups = gguard.all_groups()
        if not groups:
            await private_ai.finish("(*摇头*) 我还没被拉进任何群捏。")
            return
        lines = ["📋 我在各群的发言权限："]
        for g in sorted(groups, key=lambda x: x.get("name") or ""):
            if g.get("allowed"):
                status = "✅ 允许说话"
            elif g.get("pending"):
                status = "⏳ 等你同意"
            else:
                status = f"🚫 禁止说话（只看条数：{g.get('msg_count', 0)} 条）"
            lines.append(f"· {g.get('name') or '未命名群'}（{g['gid']}）：{status}")
        lines.append("用法：`允许群 群号/群名` · `禁止群 群号/群名` · `那个群别说话`")
        await private_ai.finish("\n".join(lines))
        return

    _allow_hit = (
        user_msg.startswith(("允许群", "同意群", "开启群"))
        or (("允许" in user_msg or "同意" in user_msg) and "说话" in user_msg)
    )
    _deny_hit = (
        user_msg.startswith(("禁止群", "关闭群", "不准", "别在", "不要在"))
        or (any(k in user_msg for k in ("别", "不要", "禁止", "不准")) and "说话" in user_msg)
    )
    if _allow_hit or _deny_hit:
        target_text = re.sub(
            r"(允许|同意|开启|禁止|关闭|不准|别|不要|在|群|说话|可以|吧|了|。|，|,|：|:)",
            " ", user_msg,
        ).strip()
        if not target_text and "那个群" in user_msg:
            target_text = "那个群"
        bot = get_bot()
        gid_target, name = await gguard.resolve_group(target_text, bot)
        if not gid_target:
            await private_ai.finish(
                "(*挠头*) 没认出是哪个群，发群号或群名给我，比如 `禁止群 123456789`。"
            )
            return
        if _allow_hit:
            gguard.allow(gid_target, name)
            await private_ai.finish(
                f"(*点头*) 好，我可以在「{name or gid_target}」说话了；之前没冒的泡不算数捏。\n\n"
                + _group_roster_text()
            )
        else:
            gguard.deny(gid_target, name)
            await private_ai.finish(
                f"(*捂嘴*) 收到，我在「{name or gid_target}」闭麦，连 @ 都不回；"
                f"想让我再说话就发 `允许群 {gid_target}`。\n\n" + _group_roster_text()
            )
        return

    # 👇 2.7 上网学习[主题]：自由学习或指定主题，来源不限 👇
    _learn_topic = None
    for prefix in ("先上网学习", "去上网学习", "上网学习", "上网学", "学知乎", "去知乎学习", "知乎学习", "学习一下", "去学习", "学点"):
        if user_msg == prefix:
            _learn_topic = ""
            break
        if user_msg.startswith(prefix):
            _learn_topic = user_msg[len(prefix):].strip(" ：:，,。")
            break
    if _learn_topic is not None:
        if str(user_id) != YOUR_QQ_ID:
            await private_ai.finish("(*叉腰*) 这是主人专属的学习时间，别人不许使唤本鱼。")
            return
        topic_clean = (_learn_topic or "").strip(" ：:，,。！!？?")
        for lead in ("先", "去", "帮我", "给我", "快"):
            if topic_clean.startswith(lead):
                topic_clean = topic_clean[len(lead):].strip(" ：:，,。！!？?")
        for tail in ("去吧", "一下", "下", "吧", "去", "学习", "学"):
            if topic_clean.endswith(tail):
                topic_clean = topic_clean[:-len(tail)].strip(" ：:，,。！!？?")
        if topic_clean in ("", "请", "你", "我", "它", "这个", "那个", "一下"):
            topic_clean = ""
        _learn_topic = topic_clean
        if _learn_topic:
            await private_ai.send(f"(*戴上眼镜*) 本鱼去网上学学「{_learn_topic}」，来源不限，等着...")
        else:
            await private_ai.send("(*翻着小本本*) 主人没指定主题，那本鱼自己挑个最近好奇的学...")
        summary, err = await learn_by_command(_learn_topic)
        if summary:
            await private_ai.finish(summary + "\n（知识进长期档案，口语表达进词库）")
        else:
            await private_ai.finish(f"(*挠头*) 这次没学到：{err}")
        return

    # 👇 2.985 改图：带图 + 「编辑图片/改图 xxx」→ 走 Qwen Image 2.1 编辑工作流 👇
    _img_segs = [seg for seg in event.get_message() if getattr(seg, "type", "") == "image"]
    if _img_segs and re.search(r"(编辑图片|图像编辑|编辑图|改图|修图|P图|p图)", user_msg):
        _instr = re.sub(r"^(编辑图片|图像编辑|编辑图|改图|修图|P图|p图)", "", user_msg).strip(" ：:，,")
        if not _instr:
            await private_ai.finish("(*歪头*) 想让我把这张图改成啥样？用 /改图 开头说，比如「/改图 换成红裙子，背景海边」。")
            return
        await private_ai.send("(*戴上手套*) 收到，本鱼拿去改一改，这个有点慢，等一会儿…")
        try:
            _seg = _img_segs[0]
            _d = getattr(_seg, "data", {}) or {}
            _raw = await _image_bytes(str(_d.get("url") or _d.get("file") or ""))
            if not _raw:
                await private_ai.finish("(*耷拉尾巴*) 这张图我没下下来。")
                return
            from plugins import image_edit
            _out = await asyncio.to_thread(image_edit.edit, _raw, _instr)
            if _out:
                await private_ai.send(MessageSegment.image(f"base64://{base64.b64encode(_out).decode()}"))
                _img_path = _save_draw_bytes(_out, "self")
                await _record_bot_image(gid, "主人", user_id, user_msg, _instr, gid, _img_path)
                try:
                    from plugins import feishu_bridge
                    if feishu_bridge.mirror_enabled():
                        await asyncio.to_thread(feishu_bridge.send_image_bytes, _out, "")
                except Exception:  # noqa: BLE001
                    pass
            else:
                await private_ai.finish("(*擦汗*) 改图没成功，可能是显存不够，过会儿再试。")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[改图] 失败：{type(e).__name__}: {e}")
            await private_ai.finish("(*耷拉尾巴*) 改图出错了。")
        return

    # 👇 2.99 出图人设词：查看/修改（只有主人能改）👇
    if re.match(r"^(画图设定|人设词|出图设定)", user_msg.strip()):
        _arg = re.sub(r"^(画图设定|人设词|出图设定)", "", user_msg.strip()).strip(" ：:，,")
        if str(user_id) != str(YOUR_QQ_ID):
            await private_ai.finish("(*护住画笔*) 这个只有主人能改。")
            return
        if _arg:
            _new = set_draw_char_desc(_arg)
            await private_ai.finish(f"(*点头*) 出图人设词改好了：\n{_new}")
        else:
            await private_ai.finish(f"(*背书一样念*) 现在出图人设词：\n{_draw_char_desc()}\n\n要改就说：画图设定 <新的描述>")
        return

    # 👇 3. 私聊画图功能 👇
    pure_prompt = None
    _draw_mode = ""
    if _slash.startswith("/ ") and not _is_known_slash_command(_slash):
        pure_prompt = user_msg[1:].strip()
        _draw_mode = "verbatim"
    elif _draw_prompt_of(user_msg) is not None:
        pure_prompt = _draw_prompt_of(user_msg)
        _draw_mode = "verbatim"
    else:
        # 2026-11-05：/豆包 画你自己 → 走豆包 Seedream 参考图生图（外观锁死），
        # 不进本地 XL。以前这里被 _self_draw_action 直接截胡，/豆包 前缀形同虚设。
        if _is_cmd and (user_msg.startswith("豆包") or user_msg.lower().startswith("doubao")):
            _bp = user_msg[2:].strip() if user_msg.startswith("豆包") else user_msg[6:].strip()
            _bp = _bp or "画你自己"
            _bact = _self_draw_action(_bp)
            if _bact is not None or "你自己" in _bp or "自己" in _bp:
                _bscene = (_bact or "").strip() or await _self_draw_improvise_llm()
                # 她自编时常写「蓝色大肥鱼举着手机」，模型会当成一个生物画进来
                _bscene = _bscene.replace("蓝色大肥鱼", "她").replace("大肥鱼", "她").replace("本鱼", "她")
                await private_ai.send("(*拿起手机*) 用主人给的图当底，只换本鱼在干啥…")
                _bimg = await _doubao_ref_draw(_bscene)
                if _bimg:
                    await private_ai.send(MessageSegment.image(f"base64://{base64.b64encode(_bimg).decode()}"))
                    _img_path = _save_draw_bytes(_img, "self")
                    await _record_bot_image(gid, "主人", user_id, user_msg, f"画你自己：{_bscene}"[:80],gid, _img_path)
                    logger.info(f"[出图] 来源=doubao-ref 场景={_bscene[:40]}")
                    try:
                        from plugins import feishu_bridge
                        if feishu_bridge.mirror_enabled():
                            await asyncio.to_thread(feishu_bridge.send_image_bytes, _bimg, "")
                    except Exception:  # noqa: BLE001
                        pass
                    return
                await private_ai.send("(*挠头*) 豆包没出图，本鱼用本地的画一张…")
                pure_prompt = _bscene
                _draw_mode = "self"
            else:
                pure_prompt = _bp
                _draw_mode = "verbatim"
        # 「画你自己…」「画个你…」这类不开头也说，交给自画像模式
        _sa = _self_draw_action(user_msg) if pure_prompt is None else None
        if pure_prompt is not None:
            pass
        elif _sa is not None:
            # 没指定动作（"画你自己"）→ 让她自己加戏，别每次都同一个模板
            _real_kw = ("真人", "写实", "摄影", "照片", "真实", "单反", "胶片")
            _real_tag = next((k for k in _real_kw if k in user_msg), "")
            # 2026-11-05：_sa 有值也走扩写。以前「做爱的场景」这种五个字的描述
            # 直接进 prompt，翻成英文就一句 sex scene，出白背景+飘道具。
            _improv = await _self_draw_improvise_llm(_sa or "")
            # 真人风格词要保留（加戏文本不含这些词，否则真人工作流触发不了）
            pure_prompt = f"{_improv}，{_real_tag}" if _real_tag and _real_tag not in _improv else _improv
            _draw_mode = "self"

    if pure_prompt is not None and _draw_mode:
        set_activity("画画中", 240)
        await private_ai.send("(*拿起画笔*) 收到，本鱼画。下次记得用 /画图 开头，别在聊天里提这两个字…")
        # 2026-11-05：ComfyUI 被空闲停掉后，冷启动要 60~70 秒，这期间一句话都没有，
        # 主人以为掉线了（实测 98 秒全程静默）。先说明一句。
        if not _port_listening(8188):
            await private_ai.send("(*翻箱倒柜*) 画图引擎歇着呢，我去把它叫起来，得等一分钟上下…")
        # 2026-11-06：出图前先让本地 LLM 让出显存（互斥现在有四方：画图/语音/识图/本地LLM）
        await _free_local_llm()
        # 出图顺序：本地 Z-Image（免费）→ 硅基 → 谷歌/豆包
        _img, _src = await _draw_chain(pure_prompt, mode=_draw_mode, raw_hint=user_msg)
        if _img:
            await private_ai.send(MessageSegment.image(f"base64://{base64.b64encode(_img).decode()}"))
            _img_path = _save_draw_bytes(_img, "self")
            await _record_bot_image(gid, "主人", user_id, user_msg, pure_prompt,gid, _img_path)
            logger.info(f"[出图] 来源={_src}")
            try:
                from plugins import feishu_bridge
                if feishu_bridge.mirror_enabled():
                    await asyncio.to_thread(feishu_bridge.send_image_bytes, _img, "")
            except Exception:  # noqa: BLE001
                pass
            return
        if GROUP_DRAW_STATE[gid] == "google" and GEMINI_API_KEY:
            try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_IMAGE_MODEL}:generateContent?key={GEMINI_API_KEY}"
                payload = {"contents": [{"parts": [{"text": pure_prompt}]}], "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]}}
                r = await http_client.post(url, json=payload)
                data = r.json()
                if isinstance(data, dict) and "error" in data: 
                    await private_ai.finish(f"🎨 失败：{explain_google_error(data)}"); return
                for part in data.get("candidates", [])[0]["content"]["parts"]:
                    img_obj = part.get("inlineData") or part.get("inline_data")
                    if img_obj and "data" in img_obj:
                        await private_ai.send(MessageSegment.image(f"base64://{img_obj['data']}"))
                        _img_path = _save_draw_bytes(_img, "self")
                        await _record_bot_image(gid, "主人", user_id, user_msg, pure_prompt,gid, _img_path)
                        return
                await private_ai.finish("没图")
            except Exception as e: await private_ai.finish(f"出错：{e}")
            return
        await private_ai.finish("(*甩笔*) 本地这次没画出来，再喊一次「画图 …」吧，本鱼重画。")
        return

    # 👇 4. 私聊逮捕/恶搞功能 👇

    # 👇 5. 电脑操控指令（只有主人能用；可启动的程序走白名单，绝不把用户输入拼进 shell）👇
    if any(k in user_msg for k in ("打开网易云", "打开网易", "打开B站", "打开哔哩哔哩")) or \
            (user_msg.startswith("打开") and len(user_msg) <= 10):
        if str(user_id) != str(YOUR_QQ_ID):
            await private_ai.finish("(*护住电脑*) 这个只有主人能让我开，你不行。")
            return
        # 注意：长的名字必须排在前面（循环里是"包含就命中"，先匹配到短的会截胡）
        _OPEN_TARGETS = {
            "抖音创作者中心": "https://creator.douyin.com/",
            "抖音创作平台": "https://creator.douyin.com/",
            "抖音": "https://www.douyin.com",
            "小红书": "https://www.xiaohongshu.com",
            "微博": "https://weibo.com",
            "知乎": "https://www.zhihu.com",
            "网易云": "cloudmusic.exe",
            "网易": "cloudmusic.exe",
            "B站": "https://www.bilibili.com",
            "哔哩哔哩": "https://www.bilibili.com",
            "记事本": "notepad.exe",
            "计算器": "calc.exe",
            "任务管理器": "taskmgr.exe",
            "画图": "mspaint.exe",
        }
        _target = ""
        for _k, _v in _OPEN_TARGETS.items():
            if _k in user_msg:
                _target = _v
                break
        if not _target:
            await private_ai.finish(
                "(*挠头*) 这个我开不了，能开：抖音创作者中心、抖音、小红书、微博、知乎、B站、网易云、记事本、计算器、画图、任务管理器。"
            )
            return
        try:
            # 白名单里的固定值，不含任何用户输入；用 startfile 避免闪 cmd 黑框
            try:
                os.startfile(_target)
            except OSError:
                os.system(f"start \"\" \"{_target}\"")
            await private_ai.finish(f"(*敲键盘*) 主人，【{_target}】帮你打开啦。")
        except Exception:  # noqa: BLE001
            await private_ai.finish("(*大哭*) 本鱼打不开这个东西...")
        return

    # 👇 6. 被欺负告状、状态、查余额、切引擎、发语音、做表情包 👇
    if "被欺负" in user_msg or "告状" in user_msg or "有人骂我" in user_msg or "群里有人" in user_msg:
        await private_ai.finish("(*扑进怀里大哭*) 呜呜呜主人！群里有人欺负本鱼！你一定要给我做主啊！[表情:生气]")
        return

    # 💬 /记忆：和主人私聊的聊天记忆（私聊记录条数、今日条数、最近几轮对话摘要）
    if _slash in MEMORY_COMMANDS:
        if str(user_id) != str(YOUR_QQ_ID):
            await private_ai.finish("(*叉腰*) 这是主人专属的记忆面板，别人不许看捏。")
            return
        try:
            p_scope = f"private_{user_id}"
            total = await asyncio.to_thread(count_memory, p_scope)
            today = await asyncio.to_thread(count_memory, p_scope, True)
            pstats = await asyncio.to_thread(ltm.stats, p_scope)
            recent = await asyncio.to_thread(get_recent_memory, 5, p_scope)
            lines = [
                "💬 **本鱼和主人的聊天记忆**",
                "━━━━━━━━━━━━━━━",
                f"📝 私聊记录：共 {total} 条（今日 {today} 条）",
                f"🧠 长期记住：{pstats['entries']} 条（{pstats['chars']} 字）",
                f"⏳ 待整理：{pstats['pending']} 条",
            ]
            if recent:
                lines.append("━━━━━━━━━━━━━━━")
                lines.append("最近聊到：")
                for i, r in enumerate(recent, 1):
                    msg = (r.get("message") or "").strip()
                    reply = (r.get("reply") or "").strip()
                    msg = (msg[:28] + "…") if len(msg) > 28 else msg
                    reply = (reply[:28] + "…") if len(reply) > 28 else reply
                    if msg:
                        lines.append(f"{i}. 主人：{msg}")
                    if reply:
                        lines.append(f"   本鱼：{reply}")
            else:
                lines.append("━━━━━━━━━━━━━━━")
                lines.append("还没聊上几句捏，主人多陪陪本鱼嘛…")
            await private_ai.finish("\n".join(lines))
        except FinishedException:
            raise
        except Exception as e:
            logger.error(f"[聊天记忆面板] 读取失败: {type(e).__name__}: {e}")
            try:
                await private_ai.finish("(*挠头*) 记忆库这会儿打盹了，主人稍后再问本鱼嘛…")
            except FinishedException:
                pass
        return

    # 📚 /学习：网上自学档案（global_learn 新闻/常识自学量 + 群聊词库 + 最近学到）
    if _slash in LEARN_COMMANDS:
        if str(user_id) != str(YOUR_QQ_ID):
            await private_ai.finish("(*叉腰*) 这是主人专属的学习面板，别人不许看捏。")
            return
        try:
            st = await asyncio.to_thread(ltm.stats, "global_learn")
            recent = await asyncio.to_thread(ltm.recent_archive, "global_learn", 3)
            lex = await asyncio.to_thread(glx.stats_all)
            lines = [
                "📚 **本鱼的学习档案（网上自学）**",
                "━━━━━━━━━━━━━━━",
                f"✅ 已学会：{st['entries']} 条（{st['chars']} 字）",
                f"⏳ 待整理：{st['pending']} 条",
                f"📖 群聊词库：{lex['total_terms']} 条词条 / 遇见 {lex['total_mentions']} 次",
            ]
            if recent:
                lines.append("━━━━━━━━━━━━━━━")
                lines.append("最近学到：")
                for i, item in enumerate(recent, 1):
                    tag = f"[{item['time']}] " if item.get("time") else ""
                    lines.append(f"{i}. {tag}{item['fact']}")
            else:
                lines.append("━━━━━━━━━━━━━━━")
                lines.append("还没攒到新东西捏，本鱼再去网上逛逛…")
            await private_ai.finish("\n".join(lines))
        except FinishedException:
            raise
        except Exception as e:
            logger.error(f"[自学面板] 读取失败: {type(e).__name__}: {e}")
            try:
                await private_ai.finish("(*挠头*) 记忆库这会儿打盹了，主人稍后再问本鱼嘛…")
            except FinishedException:
                pass
        return

    # 🗂 /档案、/记忆统计：长期档案 + 综合统计（好感度/标签、长期记忆、自学量、词库、待整理汇总）
    if _slash in ARCHIVE_COMMANDS:
        if str(user_id) != str(YOUR_QQ_ID):
            await private_ai.finish("(*叉腰*) 这是主人专属的档案面板，别人不许看捏。")
            return
        try:
            p_scope = f"private_{user_id}"
            pstats = await asyncio.to_thread(ltm.stats, p_scope)
            gstats = await asyncio.to_thread(ltm.stats, "global_learn")
            lex = await asyncio.to_thread(glx.stats_all)
            aff = await asyncio.to_thread(get_affinity, user_id)
            tag = await asyncio.to_thread(get_tag, user_id) or "还没定外号"
            archive = await asyncio.to_thread(ltm.recent_archive, p_scope, 3)
            lines = [
                "🗂 **本鱼的长期档案 · 统计**",
                "━━━━━━━━━━━━━━━",
                f"❤️ 好感度：{aff}（{tag}）",
                f"📁 主人长期记忆：{pstats['entries']} 条（{pstats['chars']} 字）",
                f"🌐 网上自学：{gstats['entries']} 条（{gstats['chars']} 字）",
                f"📖 群聊词库：{lex['total_terms']} 条词条 / 遇见 {lex['total_mentions']} 次",
                f"⏳ 待整理：主人 {pstats['pending']} · 自学 {gstats['pending']}",
            ]
            if archive:
                lines.append("━━━━━━━━━━━━━━━")
                lines.append("长期记住的事：")
                for i, item in enumerate(archive, 1):
                    t = f"[{item['time']}] " if item.get("time") else ""
                    lines.append(f"{i}. {t}{item['fact']}")
            await private_ai.finish("\n".join(lines))
        except FinishedException:
            raise
        except Exception as e:
            logger.error(f"[长期档案面板] 读取失败: {type(e).__name__}: {e}")
            try:
                await private_ai.finish("(*挠头*) 记忆库这会儿打盹了，主人稍后再问本鱼嘛…")
            except FinishedException:
                pass
        return

    # 📖 私聊词条库面板（仅主人）：/词库 汇总各群 · /词库 群号 看指定群
    if _slash in LEXICON_COMMANDS or _slash.startswith(("/词库 ", "/词典 ", "/本群词库 ")):
        if str(user_id) != str(YOUR_QQ_ID):
            await private_ai.finish("(*叉腰*) 词库面板是主人专属的，别人不许看捏。")
            return
        try:
            arg = None
            for prefix in ("/词库 ", "/词典 ", "/本群词库 "):
                if _slash.startswith(prefix):
                    arg = _slash[len(prefix):].strip()
                    break
            stats = await asyncio.to_thread(glx.stats_all)
            if arg:
                scope = f"group_{arg}"
                terms = await asyncio.to_thread(glx.list_terms, scope, 12)
                if not terms:
                    await private_ai.finish(f"群 {arg} 还没有词条捏～群友多聊几句本鱼再学。")
                    return
                lines = [f"🐟 **群 {arg} 黑话词典**", "━━━━━━━━━━━━━━━"]
                for i, t in enumerate(terms, 1):
                    tag = " 🖊" if t["manual"] else ""
                    lines.append(f"{i}. {t['term']}（{t['count']} 次）{tag}：{t['meaning']}")
                await private_ai.finish("\n".join(lines))
                return
            if not stats["enabled"]:
                await private_ai.finish("(*摊手*) 词库开关是关着的捏，主人去 .env 开 LEXICON_ENABLED 再说～")
                return
            if stats["total_terms"] == 0:
                await private_ai.finish("📖 本鱼还没攒到任何群黑话捏，群友多聊几句本鱼就开始记啦～")
                return
            lines = [
                "📖 **本鱼的群聊词库**",
                "━━━━━━━━━━━━━━━",
                f"✅ 共 {stats['total_terms']} 条词条，累计遇见 {stats['total_mentions']} 次",
            ]
            for g in stats["groups"]:
                gid = g["scope"][len("group_"):]
                lines.append(f"· 群 {gid}：{g['terms']} 条 / 遇见 {g['mentions']} 次")
            lines.append("━━━━━━━━━━━━━━━")
            lines.append("高频词条：")
            top = await asyncio.to_thread(glx.top_terms, 10)
            for i, t in enumerate(top, 1):
                tag = " 🖊" if t["manual"] else ""
                lines.append(f"{i}. {t['term']}（{t['count']} 次）{tag}：{t['meaning']}")
            await private_ai.finish("\n".join(lines))
        except FinishedException:
            raise
        except Exception as e:
            logger.error(f"[词库面板] 读取失败: {type(e).__name__}: {e}")
            try:
                await private_ai.finish("(*挠头*) 词库这会儿打盹了，主人稍后再问本鱼嘛…")
            except FinishedException:
                pass
        return

    # ✏️ 主人手动改 / 删词条（仅主人私聊）：/改词 [群号] 词条 新含义 · /删词 [群号] 词条
    if user_msg.startswith(("/改词", "改词", "/删词", "删词")):
        if str(user_id) != str(YOUR_QQ_ID):
            await private_ai.finish("(*叉腰*) 改词删词只有主人能操作捏。")
            return
        try:
            is_edit = user_msg.startswith(("/改词", "改词"))
            cmd = user_msg.split()[0]
            rest = user_msg[len(cmd):].strip()
            parts = rest.split(maxsplit=2) if is_edit else rest.split(maxsplit=1)
            if not parts:
                await private_ai.finish("用法：`/改词 [群号] 词条 新含义`；`/删词 [群号] 词条`（只有一个群时可省略群号）")
                return
            # 首段是纯数字群号就摘出来
            scope = None
            if parts[0].isdigit() and 5 <= len(parts[0]) <= 11:
                scope = f"group_{parts[0]}"
                parts = parts[1:]
            if scope is None:
                st = await asyncio.to_thread(glx.stats_all)
                if len(st["groups"]) == 1:
                    scope = st["groups"][0]["scope"]
                elif len(st["groups"]) > 1:
                    await private_ai.finish("有多个群的词条捏，主人带上群号：`/改词 群号 词条 新含义`")
                    return
                else:
                    await private_ai.finish("词库还是空的，先让本鱼在群里学几个词再说～")
                    return
            if is_edit:
                if len(parts) < 2:
                    await private_ai.finish("用法：`/改词 [群号] 词条 新含义`（词条和含义都要给捏）")
                    return
                term, meaning = parts[0], parts[1]
                ok = await asyncio.to_thread(glx.set_meaning, scope, term, meaning)
                if ok:
                    await private_ai.finish(f"✏️ 改好啦：**{term}** → {meaning}（已标记为手动词条，本鱼不会自己覆盖）")
                else:
                    await private_ai.finish(f"词典里没有「{term}」捏，先在群里聊出来本鱼再记～")
            else:
                if len(parts) < 1:
                    await private_ai.finish("用法：`/删词 [群号] 词条`")
                    return
                term = parts[0]
                ok = await asyncio.to_thread(glx.delete_term, scope, term)
                if ok:
                    await private_ai.finish(f"🗑 已删掉「{term}」这条词条。")
                else:
                    await private_ai.finish(f"词典里没有「{term}」捏，删不掉哦～")
        except FinishedException:
            raise
        except Exception as e:
            logger.error(f"[词库编辑] 失败: {type(e).__name__}: {e}")
            await private_ai.finish("(*挠头*) 操作词库出岔子了，主人稍后再试嘛…")
        return

    rate_arg = _split_rate_command(user_msg)
    if rate_arg is not None:
        if str(user_id) != str(YOUR_QQ_ID):
            await private_ai.finish("(*叉腰*) 回复频率是主人才能调的，别瞎指挥捏。")
            return
        action, value = parse_rate_arg(rate_arg)
        if action == "view":
            await private_ai.finish(_rate_panel_text(current_rate()))
        elif action == "up":
            save_reply_rate(current_rate() + RATE_STEP)
            await private_ai.finish(_rate_reply_text("up", current_rate()))
        elif action == "down":
            save_reply_rate(current_rate() - RATE_STEP)
            await private_ai.finish(_rate_reply_text("down", current_rate()))
        elif action == "set":
            save_reply_rate(value)
            await private_ai.finish(_rate_reply_text("set", current_rate()))
        elif action == "invalid":
            await private_ai.finish("(*皱眉*) 百分比要在 0~100 之间啦，重发 `/频率 80` 这样。")
        else:
            await private_ai.finish(_rate_panel_text(current_rate()))
        return

    if _slash in STATUS_COMMANDS:
        status_text = f"📊 **私聊面板**\n🧠 大脑：{'Google' if GROUP_AI_STATE[gid] == 'google' else ('硅基流动' if GROUP_AI_STATE[gid] == 'siliconflow' else 'DeepSeek')}\n🎨 画笔：{'Google' if GROUP_DRAW_STATE[gid] == 'google' else '豆包'}\n❤️ 情绪：{GROUP_MOOD_STATE[gid]} ({GROUP_MOOD_VAL_STATE[gid]})"
        await private_ai.finish(status_text)
        return

    # 2026-11-06：/喝咖啡 —— 提神。主人说她一直喊没力气。
    if user_msg in ["/喝咖啡", "喝咖啡", "/咖啡", "/coffee"]:
        try:
            from plugins import energy as _en
            from plugins import hormones as _ho
            _cs_f = ROOT / "memory" / "coffee_state.json"
            try:
                _cs = json.loads(_cs_f.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                _cs = {}
            _today_c = time.strftime("%Y-%m-%d")
            if _cs.get("date") != _today_c:
                _cs = {"date": _today_c, "count": 0}
            _n_c = int(_cs.get("count") or 0)
            _before = float(_en.value())
            if _n_c >= 3:
                await private_ai.finish(
                    "(*捂着肚子*) 主人…今天第三杯了，本鱼心跳得厉害，再喝要抖成筛子了。\n"
                    f"体力 {_before:.0f}/100，明天再来吧。")
                return
            if _before >= 95:
                await private_ai.finish(f"(*精神得很*) 主人，本鱼现在体力 {_before:.0f}/100，喝不下啦～")
                return
            # 能量 +25（第三杯打折，越喝越没用）
            _gain = [25, 18, 10][min(_n_c, 2)]
            _after = _en.set_value(_before + _gain)
            try:
                _ho.apply_event("coffee")
            except Exception:  # noqa: BLE001
                pass
            _cs["count"] = _n_c + 1
            try:
                _cs_f.parent.mkdir(parents=True, exist_ok=True)
                _cs_f.write_text(json.dumps(_cs, ensure_ascii=False, indent=1), encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
            logger.info(f"[咖啡] 第 {_cs['count']} 杯：体力 {_before:.0f} → {_after:.0f}")
            _lines = [
                f"(*捧着杯子吹了吹，小口抿一下*) 唔……苦。",
                f"不过确实精神了，本鱼现在 {_after:.0f}/100。",
            ]
            if _cs["count"] == 1:
                _lines.append("主人还挺疼本鱼的嘛。")
            elif _cs["count"] == 2:
                _lines.append("第二杯了…本鱼手有点抖，不过还能撑。")
            else:
                _lines.append("(*耳朵一抽*) 这杯下去今晚别想让本鱼睡觉了。")
            await private_ai.finish("\n".join(_lines))
        except Exception as _ce:  # noqa: BLE001
            logger.warning(f"[咖啡] 失败：{type(_ce).__name__}: {_ce}")
            await private_ai.finish("(*挠头*) 咖啡洒了…主人再喊一次？")
        return

    if user_msg in ["/余额", "查余额", "还有多少钱"]:
        await private_ai.send("(*翻钱包*) 主人，本鱼看看还剩多少生活费...")
        _bl = []
        # ① DeepSeek 官方
        _ds = await deepseek_balance()
        _ds_cached = False
        if _ds is None and _BAL_CACHE.get("v") is not None:
            _ds = _BAL_CACHE["v"]; _ds_cached = True
        if _ds is None:
            _bl.append("· DeepSeek 官方：查不到（限流，稍等再试）")
        else:
            _bl.append("· DeepSeek 官方：**%.2f 元**%s" % (_ds, "（缓存值）" if _ds_cached else ""))
            if not _ds_cached:
                _BAL_CACHE["at"] = time.time(); _BAL_CACHE["v"] = _ds
        # ② 智谱 GLM（没有余额接口，只能试一次）
        _zp = await zhipu_probe()
        if _zp is True:
            _bl.append("· 智谱 GLM：**还能用**（2000 万免费额度）")
        elif _zp is False:
            _bl.append("· 智谱 GLM：用不了（没配 key 或额度耗尽）")
        else:
            _bl.append("· 智谱 GLM：探测失败")
        # ③ 硅基流动
        try:
            _sk = os.getenv("SILICONFLOW_API_KEY")
            async with httpx.AsyncClient() as _c0:
                _r0 = await _c0.get("https://api.siliconflow.cn/v1/user/info",
                                    headers={"Authorization": "Bearer " + str(_sk)})
                _d0 = _r0.json()
            if _d0.get("status"):
                _bl.append("· 硅基流动：**" + str(_d0["data"]["totalBalance"]) + " 元**")
            else:
                _bl.append("· 硅基流动：查不到")
        except Exception as _e0:  # noqa: BLE001
            _bl.append("· 硅基流动：查询失败（" + type(_e0).__name__ + "）")
        # ④ 给建议
        _low = (_ds is not None and _ds < 2)
        _msg = ("💰 **大肥鱼的赛博钱包**\n━━━━━━━━━━━━━━━\n" + "\n".join(_bl) + "\n━━━━━━━━━━━━━━━\n")
        if _low and _zp:
            _msg += "(*拉你袖子*) 主人，官方那条快见底了 —— **发 `/切智谱` 就切过去**，本鱼这边还有免费额度。"
        elif _low:
            _msg += "(*捏着衣角*) 主人…官方快见底了，智谱那边也探不通，省着点喘气。"
        else:
            _msg += "(*叹气*) 主人，省着点花，本鱼还想多吃几碗白米饭呢！"
        await private_ai.finish(_msg)
        return

    if user_msg in ["切换大脑 谷歌", "切大脑 谷歌", "切换大脑 硅基", "切大脑 硅基", "切换大脑 字节", "切大脑 字节",
                    "切换画笔 谷歌", "切画笔 谷歌", "切换画笔 字节", "切画笔 字节",
                    "切官方", "切换大脑 官方", "切大脑 官方", "切回官方"] and str(user_id) != str(YOUR_QQ_ID):
        await private_ai.finish("(*按住开关*) 换大脑和画笔只有主人能改。")
        return
    if user_msg in ["切换大脑 谷歌", "切大脑 谷歌"]: GROUP_AI_STATE[gid] = "google"; await private_ai.finish("🧠 已切至 Google"); return
    # 2026-11-05：/切官方 —— 切回 DeepSeek 官方 API（默认通道）
    if _is_cmd and user_msg in ["切官方", "切换大脑 官方", "切大脑 官方", "切回官方"]:
        GROUP_AI_STATE[gid] = "deepseek"; await private_ai.finish("🧠 已切回官方（DeepSeek）"); return
    # 2026-11-05：/切智谱 —— 直接走 GLM（2000 万 token 免费额度）
    if _is_cmd and user_msg in ["切智谱", "切glm", "切GLM", "切换大脑 智谱"]:
        GROUP_AI_STATE[gid] = "zhipu"; await private_ai.finish("🧠 已切智谱 GLM（免费额度）"); return

    # 2026-11-05：画图风格切换 —— 写实 CG（Juggernaut XL）⇄ NoobAI 二次元
    if _is_cmd and user_msg in ["画3D 开", "画3D开", "开3D", "3D模式 开", "切3D"]:
        globals()["_DRAW_3D_ON"] = True
        await private_ai.finish(
            "🎬 已切写实 CG 模式\n"
            "· 底模 Juggernaut XL v9\n· eve3d 0.9 · 角色 LoRA 0.45\n"
            "· 提示词换成 photorealistic / 3D render / volumetric lighting\n"
            "下次画图生效，要切回转二次元发「画3D 关」")
        return
    if _is_cmd and user_msg in ["画3D 关", "画3D关", "关3D", "3D模式 关", "切回二次元"]:
        globals()["_DRAW_3D_ON"] = False
        await private_ai.finish("🎨 已切回二次元（NoobAI）")
        return
    elif user_msg in ["切换大脑 硅基", "切大脑 硅基", "切换大脑 字节", "切大脑 字节"]: GROUP_AI_STATE[gid] = "siliconflow"; await private_ai.finish("🧠 已切至 硅基流动"); return
    if user_msg in ["切换画笔 谷歌", "切画笔 谷歌"]: GROUP_DRAW_STATE[gid] = "google"; await private_ai.finish("🎨 已切至 Google"); return
    elif user_msg in ["切换画笔 字节", "切画笔 字节"]: GROUP_DRAW_STATE[gid] = "byte"; await private_ai.finish("🎨 已切至 豆包"); return

    if user_msg.startswith("/发B站") or user_msg.startswith("发B站"):
        _txt = user_msg.replace("/发B站", "").replace("发B站", "").strip("：: ")
        if not _txt:
            await private_ai.send("用法：/发B站 要念的话")
            return
        # 少于 600 字当"主题"，先让她自己写一段 2~3 分钟的口播稿
        if len(_txt) < 600:
            await private_ai.send("(*卷起袖子*) 主题收到，先写稿…")
            try:
                _txt = await _write_bili_script(_txt, gid)
                await private_ai.send(f"稿子写好了（{len(_txt)} 字），去录音…")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[发B站] 写稿失败：{type(e).__name__}: {e}")
                await private_ai.send("稿子没写出来，看日志。")
                return
        else:
            await private_ai.send("(*卷起袖子*) 行，本鱼去录一段…")
        try:
            _out = ROOT / "data" / "bili_video" / f"bili_{int(time.time())}.mp4"
            _run = __import__("subprocess").run
            _r = await asyncio.to_thread(
                lambda: _run([sys.executable, str(ROOT / "tools" / "make_bili_video.py"),
                              _txt, "-o", str(_out)],
                             capture_output=True, text=True, timeout=900,
                             encoding="utf-8", errors="replace"))
            if _out.exists() and _out.stat().st_size > 10000:
                await private_ai.send(MessageSegment.video(str(_out)))
                await private_ai.send(f"录好了，{_out.stat().st_size // 1024}KB。要发 B 站还得走投稿接口。")
            else:
                logger.warning(f"[发B站] 失败：{(_r.stderr or '')[-300:]}")
                await private_ai.send("没录成，看日志。")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[发B站] 异常：{type(e).__name__}: {e}")
            await private_ai.send("录音失败。")
        return

    if user_msg.startswith("/做同款") or user_msg.startswith("做同款"):
        _txt = user_msg.replace("/做同款", "").replace("做同款", "").strip("：: ")
        try:
            from plugins import meme_render as _mr
            _last = _mr.last() or {}
            _tpl = str(_last.get("template") or "sign")
            _p = await asyncio.to_thread(_mr.render_temp, _tpl, _txt or "再来一张")
            await private_ai.send(MessageSegment.image(_p))
            asyncio.create_task(safe_remove(_p))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[做同款] 失败：{type(e).__name__}: {e}")
            await private_ai.send("没做成，先发一次「做个表情包」。")
        return

    if user_msg.startswith("/精标表情") or user_msg.startswith("精标表情"):
        if str(user_id) != YOUR_QQ_ID:
            await private_ai.send("这条只有主人能用。")
            return
        try:
            _n = int(re.sub(r"\D", "", user_msg) or "3")
        except Exception:  # noqa: BLE001
            _n = 3
        _n = max(1, min(10, _n))
        await private_ai.send(f"(*戴眼镜*) 给 {_n} 张表情打视觉标签，等我一下…")
        try:
            _done = await _tag_stickers_vision(_n)
            await private_ai.send(f"打完 {_done} 张（写进 sticker_tags.json）")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[精标表情] 失败：{type(e).__name__}: {e}")
            await private_ai.send("标失败了，看日志。")
        return

    if user_msg.startswith("/记忆校准") or user_msg.startswith("记忆校准"):
        if str(user_id) != YOUR_QQ_ID:
            await private_ai.send("这条只有主人能用。")
            return
        try:
            from plugins import memory_maintain as _mm
            rows = await asyncio.to_thread(lambda: _mm.recalibrate(apply=True))
            c = rows.get("classes") or {}
            await private_ai.send(
                "敏感度重算完成：" + chr(10)
                + "公开 " + str(c.get("sens0", 0)) + " / 私域 " + str(c.get("sens1", 0))
                + " / 私密 " + str(c.get("sens2", 0)))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[记忆校准] 失败：{type(e).__name__}: {e}")
            await private_ai.send("校准失败，看日志。")
        return

    if user_msg.startswith("/记忆降敏") or user_msg.startswith("记忆降敏"):
        if str(user_id) != YOUR_QQ_ID:
            await private_ai.send("这条只有主人能用。")
            return
        try:
            _mid = int(re.sub(r"\D", "", user_msg) or "0")
            from plugins import long_memory as _lm
            _hit = _lm.find_item(_mid)
            if not _hit:
                await private_ai.send("没找到这条记忆。")
                return
            _sc, _it = _hit
            _it["sens"] = 0
            _lm._save()
            await private_ai.send("已降为公开：" + str(_it.get("fact"))[:40])
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[记忆降敏] 失败：{type(e).__name__}: {e}")
            await private_ai.send("降敏失败，看日志。")
        return

    if user_msg.startswith("/同步群记忆") or user_msg.startswith("同步群记忆"):
        if str(user_id) != YOUR_QQ_ID:
            await private_ai.send("这条只有主人能用。")
            return
        _nums = [x for x in re.split(r"\s+", user_msg.strip()) if x.isdigit()]
        _nums = [x for x in _nums if len(x) >= 6]          # 群号至少 6 位，避免把"100"当群号
        if not _nums:
            await private_ai.send("用法：/同步群记忆 群号 [条数]（默认 100）")
            return
        _gid = _nums[0]
        _cnt = 100
        _tail = [x for x in re.split(r"\s+", user_msg.strip()) if x.isdigit() and len(x) < 6]
        if _tail:
            _cnt = max(10, min(300, int(_tail[-1])))
        await private_ai.send(f"(*翻聊天记录*) 去把群 {_gid} 最近 {_cnt} 条捞进记忆…")
        try:
            bot = get_bot()
            data = await bot.call_api("get_group_msg_history",
                                      group_id=int(_gid), count=int(_cnt))
            msgs = (data or {}).get("messages") or []
            scope = f"group_{_gid}"
            # 去重：先看看库里已有的（同 scope 最近 500 条）
            seen = set()
            try:
                import sqlite3
                from memory_manager import DB_PATH
                con = sqlite3.connect(str(DB_PATH), timeout=5)
                for _sp, _msg in con.execute(
                        "select speaker, message from chat_history where scope=? "
                        "order by rowid desc limit 500", (scope,)):
                    seen.add((str(_sp), str(_msg)))
                con.close()
            except Exception:  # noqa: BLE001
                pass
            added = 0
            for m in msgs:
                sender = m.get("sender") or {}
                name = str(sender.get("card") or sender.get("nickname")
                           or sender.get("user_id") or "群友")
                uid = str(sender.get("user_id") or "")
                text = "".join(str(s.get("data", {}).get("text") or "")
                               for s in (m.get("message") or [])
                               if s.get("type") == "text").strip()
                if not text or (name, text) in seen:
                    continue
                seen.add((name, text))
                await asyncio.to_thread(add_memory, name, uid, scope, text, "")
                await ltm.enqueue_pending(scope, name, text)
                added += 1
            if added:
                await asyncio.to_thread(ltm.consolidate, scope)
            _n = len(ltm._archive.get(scope) or [])
            await private_ai.send(
                f"群 {_gid} 同步完成：导入 {added} 条聊天，档案现在 {_n} 条。")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[同步群记忆] 失败：{type(e).__name__}: {e}")
            await private_ai.send(f"同步失败：{type(e).__name__}: {e}")
        return

    if user_msg.startswith("/看结构") or user_msg.startswith("看结构"):
        _src = user_msg.replace("/看结构", "").replace("看结构", "").strip("：: ")
        if not _src:
            await private_ai.send("用法：/看结构 BV号或视频链接（也可以给本地文件路径）")
            return
        if _src.upper().startswith("BV"):
            _src = "https://www.bilibili.com/video/" + _src
        await private_ai.send("(*竖起耳朵*) 听一下这段的节奏…")
        try:
            _run = __import__("subprocess").run
            _r = await asyncio.to_thread(
                lambda: _run([sys.executable, str(ROOT / "tools" / "audio_structure.py"), _src],
                             capture_output=True, text=True, timeout=600,
                             encoding="utf-8", errors="replace"))
            _out = (_r.stdout or "").strip()
            if _out:
                await private_ai.send(_out[:1500])
            else:
                logger.warning(f"[看结构] 空输出：{(_r.stderr or '')[-300:]}")
                await private_ai.send("没听出东西来，看日志。")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[看结构] 失败：{type(e).__name__}: {e}")
            await private_ai.send("分析失败了。")
        return

    if user_msg.startswith("/扫新视频") or user_msg.startswith("扫新视频"):
        if str(user_id) != YOUR_QQ_ID:
            await private_ai.send("这条只有主人能用。")
            return
        await private_ai.send("(*揉眼睛*) 去看看有没有新的 AI 视频…")
        try:
            from plugins import bilibili_tech as _bt
            report = await _bt._run_tech_scan()
            if report:
                await private_ai.send(report[:1500])
            else:
                await private_ai.send("这轮没有新线索（可能已看过，或最近没有符合条件的投稿）。")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[扫新视频] 失败：{type(e).__name__}: {e}")
            await private_ai.send("扫失败了，看日志。")
        return

    if user_msg.startswith("/维护记忆") or user_msg.startswith("维护记忆"):
        if str(user_id) != YOUR_QQ_ID:
            await private_ai.send("这条只有主人能用。")
            return
        try:
            from plugins import memory_maintain as _mm
            _before = await asyncio.to_thread(_mm.stats)
            _row = await asyncio.to_thread(lambda: _mm.run(apply=True))
            _after = await asyncio.to_thread(_mm.stats)
            _c = _row.get("classes") or {}
            _msg = ("记忆维护完成：" + str(_before['items']) + " → " + str(_after['items']) + " 条"
                    + "（合并 " + str(_row.get('merged')) + "、遗忘 " + str(_row.get('forgotten'))
                    + "、" + str(_row.get('seconds')) + "s）" + chr(10)
                    + "分类：事实 " + str(_c.get('fact', 0))
                    + " / 场景 " + str(_c.get('scene', 0))
                    + " / 印象 " + str(_c.get('impression', 0))
                    + " / 人格 " + str(_c.get('persona', 0)) + chr(10)
                    + "敏感度：公开 " + str(_c.get('sens0', 0))
                    + " / 私域 " + str(_c.get('sens1', 0))
                    + " / 私密 " + str(_c.get('sens2', 0)) + "（群聊只放公开）" + chr(10)
                    + "成长条目（persona 级）：" + str(_c.get('growth_persona', 0)))
            await private_ai.send(_msg)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[维护记忆] 失败：{type(e).__name__}: {e}")
            await private_ai.send("维护失败，看日志。")
        return

    if user_msg.startswith("/去AI味") or user_msg.startswith("去AI味"):
        _txt = user_msg.replace("/去AI味", "").replace("去AI味", "").strip("：: ")
        if not _txt:
            await private_ai.send("用法：/去AI味 要改的文字（也可以直接贴一段长文）")
            return
        try:
            from plugins import humanize as _hz

            async def _chat_box(msgs):
                _p = "".join(str(m.get("content") or "") for m in msgs)
                return await call_ai_raw(_p, GROUP_AI_STATE.get(str(gid)) or "deepseek",
                                         temperature=0.6, max_tokens=900)

            _out = await _hz.humanize(_txt, _chat_box)
            await private_ai.send(_out[:1500] or "（删完没剩什么，原文本身全是套话）")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[去AI味] 失败：{type(e).__name__}: {e}")
            await private_ai.send("改失败了。")
        return

    if user_msg.startswith("做个表情包"):
        text = user_msg.replace("做个表情包", "").strip("：: ")
        # 本地模板渲染（免费、秒出）：举牌/想/对联/抱；没指定就用举牌
        _tpl = "sign"
        for _k, _v in (("举牌", "sign"), ("想", "think"), ("对联", "couplet"),
                       ("上联", "couplet"), ("下联", "couplet"), ("抱", "hug")):
            if text.startswith(_k):
                _tpl, text = _v, text[len(_k):].strip("：: ")
                break
        try:
            from plugins import meme_render as _mr
            _mp = await asyncio.to_thread(_mr.render_temp, _tpl, text or "笑一个")
            await private_ai.send(MessageSegment.image(_mp))
            await _record_bot_image(gid, "主人", user_id, user_msg,
                                    f"表情包（{_tpl}）：{text}", gid)
            asyncio.create_task(safe_remove(_mp))
            return
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[表情包] 本地模板失败，回退云端：{type(e).__name__}: {e}")
        try:
            r = await http_client.post(
                "https://ark.cn-beijing.volces.com/api/v3/images/generations",
                headers={"Authorization": f"Bearer {DOUBAO_DRAW_API_KEY}"},
                json={"model": DOUBAO_IMAGE_MODEL, "prompt": "搞笑表情包底图，留白", "response_format": "url", "size": "1024x1024"}
            )
            img_url = r.json()["data"][0]["url"]
            img_path = await download_image_to_temp(img_url)
            if img_path:
                meme_path = await asyncio.to_thread(sync_make_meme, img_path, text)
                if meme_path:
                    await private_ai.send(MessageSegment.image(meme_path))
                    meme_caption = f"表情包：{text}" if text else "搞笑表情包"
                    await _record_bot_image(gid, "主人", user_id, user_msg, meme_caption,gid, _img_path)
                    asyncio.create_task(safe_remove(meme_path))
                try: os.remove(img_path)
                except Exception: pass
            else: 
                await private_ai.send("拿不到底图，生成失败。")
                return
        except Exception: 
            await private_ai.send("做表情包失败了")
            return
        return

    # 👇 7. 核心对话逻辑 👇
    current_affinity = await asyncio.to_thread(get_affinity, user_id)
    current_tag = await asyncio.to_thread(
        get_tag, user_id,
        str(gid) if str(gid).startswith("private_") else f"group_{gid}",
    )

    # 私聊一次性回填历史（重启不失忆），并维护连续上下文
    if gid not in _history_loaded:
        _history_loaded.add(gid)
        try:
            recent = await asyncio.to_thread(get_recent_memory, _history_backfill_rows(), gid)
            for r in recent:
                if r.get("message"):
                    _history[gid].append(("主人", r["message"][:cfg.context.max_len_per_msg], False))
                if r.get("reply"):
                    _history[gid].append((NICKNAME, _strip_fake_state(r["reply"])[:cfg.context.max_len_per_msg], True))
        except Exception as e:
            logger.warning(f"[记忆] 私聊 {user_id} 历史回填失败: {e}")
    # 历史只增不改：不再 [-history_use:] 滑动、不再 [-1200:] 截尾（那两步让每轮前缀都变）
    _compact_history(gid, HISTORY_TOKEN_BUDGET_PRIVATE)

    # 2026-11-05（好奇心驱动）：私聊里问「XX 是什么」这类，现查一次并学下来。
    # 限速 3 次/小时，学过的实体不再查；结果拼在这句前面，她本轮就能用上。
    _user_msg_raw = user_msg          # 原话，存记忆/历史用；拼 prompt 的可能是加了上下文的版本
    # 2026-11-05：私聊每轮顺手刷一次余额缓存（她自己带 10 分钟节流，不会频繁打接口）
    try:
        await balance_line()
    except Exception:  # noqa: BLE001
        pass
    # 2026-11-05：主人这句话有没有正面回应她挂着的执念？回应了就大幅降张力
    try:
        from plugins import obsessions as _obs0
        _ans = _obs0.answered(_user_msg_raw)
        if _ans:
            logger.info(f"[执念] 主人回应了 {len(_ans)} 条 → " +
                        ", ".join(f"id={x['id']} 张力{x['tension']}" for x in _ans))
        _obs0.decay(sleeping=is_sleeping())   # 睡觉时张力掉得更快（想开了）
    except Exception as _oe:  # noqa: BLE001
        logger.debug(f"[执念] 跳过：{type(_oe).__name__}")
    try:
        _ent = web_search.needs_curiosity(user_msg)
        if _ent:
            _learned = await web_search.curiosity_learn(_ent)
            if _learned:
                # 2026-11-05 修：原来直接把查询结果拼进 user_msg，结果它会被当成
                # 「主人说的话」写进 pending / 记忆（实测记忆里出现了
                # 「主人: 【刚查到】（你刚才说到「就来问安装没」…」）。
                # 现在只把原话存进 _user_msg_raw，拼进 prompt 的用带上下文的版本。
                _user_msg_raw = user_msg
                user_msg = _learned + "\n\n" + user_msg
    except Exception as _e:  # noqa: BLE001
        logger.debug(f"[好奇] 跳过：{type(_e).__name__}: {_e}")

    sys_stable, sys_volatile, sys_slow = build_prompt_parts(
        gid, user_id, affinity=current_affinity, tag=current_tag,
        is_private=True, user_text=user_msg.replace("\u200b", ""), channel="qq",
        hist_len=len(_history[gid]), hist_tok=_history_tokens(_history[gid]),
    )
    hist_msgs = _history_messages(True, list(_history[gid]))
    extra_blocks: list = list(sys_slow)   # 慢变块放历史之后，前缀才能稳定

    # 「关于主人」的跨场景档案（subject 层）：只给主人本人看，别群/别人拿不到
    if str(user_id) == YOUR_QQ_ID:
        try:
            _sb = await asyncio.to_thread(ltm.subject_block, "主人", 6)
            if _sb:
                extra_blocks.append(_sb)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[记忆] 跨场景档案取失败：{e}")

    mem_intent = detect_memory_intent(user_msg, is_private=True)
    if mem_intent:
        mem_text = await asyncio.to_thread(
            recall_memory, f"private_{user_id}", mem_intent, True, 1500, True
        )
        if mem_text:
            extra_blocks.append("【回忆】\n" + mem_text)

    # 私聊只在提到“群里/大家/他们/刚才”等词时，才带群聊近况（省 token）
    if user_msg and any(k in user_msg for k in ("群", "大家", "他们", "群友", "刚才", "聊啥", "聊了什么", "发生")):
        try:
            group_lines = []
            # 只带「用户这句话里点名的群」；没点名就不带，避免把别的群的人和事串进来
            import re as _re
            _named = _re.findall(r"\d{6,12}", user_msg)
            _groups = [int(g) for g in _named[:1]] if _named else []
            for g in _groups:
                gname = ""
                try:
                    gname = await _group_name(g)
                except Exception:
                    gname = ""
                rows = await asyncio.to_thread(get_recent_memory, 4, f"group_{g}")
                for r in (rows or [])[::-1]:
                    msg = (r.get("message") or "").replace("\n", " ")[:60]
                    if msg:
                        group_lines.append(
                            f"[{gname or ('群' + str(g))}] {r.get('speaker', '?')}: {msg}"
                        )
            if group_lines:
                extra_blocks.append(
                    "【群聊近况（私聊时也能自然提起，别硬背）】\n" + "\n".join(group_lines[-8:])
                )
        except Exception:
            pass

    voice_hint = ""
    if is_voice_request:
        voice_hint = "（主人要求你用语音念出回复。请用傲娇少女第一人称口语化说 1~2 句短句，不要括号动作、不要表情标记、不要书面语和标点堆砌，方便直接朗读。）\n"
    if images:
        current_content: list = [{"type": "text", "text": voice_hint + (user_msg or "（主人发了张图）")}]
        for u in images: current_content.append({"type": "image_url", "image_url": {"url": u}})
    else:
        current_content = voice_hint + "主人说：" + user_msg

    # 布局：稳定 system → 历史真实多轮 → 回忆/群聊近况 → 易变状态 → 最新一句 user
    messages = _compose_chat_messages(
        sys_stable, hist_msgs, extra_blocks, sys_volatile, current_content
    )

    if not images:
        # 2026-11-06 修：主人反馈「每次描述的是上一张」。
        # 这里原来无条件把上一张图的描述塞进 prompt（有效期 10 分钟），
        # 于是这一轮哪怕在聊别的事，她也会顺嘴说上一张图 —— 看着就像认错图。
        # 现在：①有效期收到 90 秒 ②明确标注是「之前那张、不是现在这张」。
        _cached = _IMG_DESC_CACHE.get(str(gid))
        if _cached and time.time() - _cached[1] < 90:
            _age = int(time.time() - _cached[1])
            messages.append({"role": "system",
                             "content": (f"【{_age} 秒前那张图的内容，不是这一轮发的】"
                                         + _cached[0]
                                         + "（主人这轮没发新图，别把它当成现在这张）")})
    else:
        # 这一轮有新图 → 旧描述立刻作废，防止串到新图上
        _IMG_DESC_CACHE.pop(str(gid), None)

    reply, _ = await _web_or_chat(messages, GROUP_AI_STATE[gid], True, _web_plain, images, context_key=str(gid))

    if _superseded(_private_conv, _private_run_tok, str(user_id)):
        logger.info(f"[中断] 私聊 {_private_conv} 生成期间来了更新的消息，本轮回话作废")
        return

    if reply.startswith("[AI 调用失败]") or reply.startswith("[谷歌") or reply.startswith("[硅基流动"):
        await private_ai.finish(f"（本鱼脑血栓了，报错如下）\n{reply}")
        return

    mood_tag = ""
    m = re.search(r"\[表情[:：]\s*([^\]]*)\]", reply)
    if m:
        mood_tag = re.sub(r'[^\w\u4e00-\u9fa5]', '', m.group(1).strip())
        reply = re.sub(r"\[表情[:：]\s*[^\]]*\]", "", reply).strip()

    reply, _new_nick = _extract_nickname_marker(reply)
    reply = _strip_bot_marker(reply)
    if _new_nick:
        await asyncio.to_thread(set_tag, YOUR_QQ_ID, _new_nick,
                                str(gid) if str(gid).startswith("private_") else f"group_{gid}")
        await _sync_owner_remark(_new_nick)
        logger.info(f"[外号] 主人外号更新为 {_new_nick}（来自对话）")

    if len(reply) > 300: reply = reply[:300] + "..."

    # 2026-11-05：人味加法层（留白标记「好像」+ 加强「特别」，长回复偶发一处手滑）。
    # 方法来自 dsh-fish-tone 的 1190 万字语料校准，只加字不改意思、不碰代码和路径。
    global _HUMAN_CHARS
    try:
        from plugins import humanize as _hz_add
        _before = reply
        reply = _hz_add.humanize_add(reply, chars_so_far=_HUMAN_CHARS)
        if reply != _before:
            logger.debug(f"[人味] {_before[:24]!r} → {reply[:24]!r}")
        _HUMAN_CHARS += len(reply)
        try:
            from plugins import energy as _ener1
            _ener1.drain()
        except Exception:  # noqa: BLE001
            pass
    except Exception as _he:  # noqa: BLE001
        logger.debug(f"[人味] 跳过：{type(_he).__name__}")

    private_scope = f"private_{user_id}"

    # 2026-11-05 修（根因）：清洗必须发生在写 pending **之前**。
    # 以前 enqueue_pending 用的是原始 reply，于是 _strip_fake_state / strip_self_repeat
    # 只保护了喂给模型的那份 _history，长期记忆那条路照样拿到原文 ——
    # 她自己的自问自答被 memory_merge 提炼成"事实"存进档案（欢迎/收尾/半碗粥都这么进去的）。
    _hist_reply = _strip_fake_state(reply) if reply else reply
    if _hist_reply:
        try:
            from plugins import humanize as _hz_rep
            _hist_reply, _dropped = _hz_rep.strip_self_repeat(
                _hist_reply, _RECENT_SENTS.get(gid) or [])
            if _dropped:
                logger.info(f"[去重] 删掉重复的自起话头：{_dropped[:40]}")
                # 2026-11-05：删掉的句子进执念池，有生命周期（提起加张力、被回应就闭合）
                try:
                    from plugins import obsessions as _obs
                    _a = _obs.touch(_dropped)
                    logger.debug(f"[执念] {_a.get('action')} id={(_a.get('item') or {}).get('id')}")
                except Exception:  # noqa: BLE001
                    pass
            _RECENT_SENTS[gid].extend(_hz_rep._split_sents(_hist_reply))
            if len(_RECENT_SENTS[gid]) > 60:
                del _RECENT_SENTS[gid][:-60]
        except Exception as _re:  # noqa: BLE001
            logger.debug(f"[去重] 跳过：{type(_re).__name__}")

    try:
        # 聊天记录 / 日记存档用原文（要保留历史真实面貌）
        await asyncio.to_thread(add_memory, "主人", user_id, private_scope, user_msg, reply)
        await asyncio.to_thread(save_daily, private_scope, "主人", user_id, user_msg, reply)
        # 进长期记忆待合并缓冲的用**清洗后**的版本
        await ltm.enqueue_pending(private_scope, "主人", _user_msg_raw)
        if _hist_reply:
            await ltm.enqueue_pending(private_scope, NICKNAME, _hist_reply)
    except Exception as e:
        logger.error(f"❌ 私聊记忆写入失败: {e}")

    # 同步进私聊连续上下文（内存，语音/文本路径都覆盖）
    if user_msg:
        _history[gid].append(("主人", _user_msg_raw[:cfg.context.max_len_per_msg], False))
    if reply:
        _history[gid].append((NICKNAME, _hist_reply[:cfg.context.max_len_per_msg], True))
        write_pet_live(reply, "主人")
        if str(user_id) == YOUR_QQ_ID:
            asyncio.create_task(maybe_spontaneous_qzone_say())

    _hormone_event("owner_message")
    # 亲密内容 → 单独喂 intimate / aroused（2026-11-05）。放在 owner_message 之后，
    # 这样"一句普通的话 + 一句撩"能叠两次，普通闲聊只有前者。
    _int_ev = _intimate_check(user_msg)
    if _int_ev:
        _hormone_event(_int_ev)
    # 2026-11-05：尾波——这一轮说完了，安排一次「话没说完」的接续。
    # 配置在 memory/wake_config.json，默认 enabled=false（不开就不花 token）。
    try:
        _wake_schedule(gid, user_id)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[尾波] 调度失败：{type(e).__name__}: {e}")
    # ③ 夸奖 / 负评也驱动内分泌（以前只有「聊天」和「被骂」两个事件在跑）
    _t = (user_msg or "")[:80]
    if re.search(r"(好棒|真棒|厉害|可爱|爱你|喜欢你|乖|聪明|真乖|辛苦|谢谢)", _t):
        _hormone_event("praise")
    elif re.search(r"(笨|蠢|废物|垃圾|滚|傻|闭嘴|烦死)", _t):
        _hormone_event("insult", 0.5)
        # 主人当面凶她 → 记进「对主人」那本账（那是回避档真正的来源）
        try:
            from plugins import withdraw as _wd0
            _wd0.take_offense(target="owner")
            _wd0.snapshot_blame(_t)
        except Exception:  # noqa: BLE001
            pass
        try:
            # neg 累加：bump_negative 定义了但以前零调用，355 人的 neg 全是 0
            bump_negative(str(user_id), f"private_{user_id}", 8)
        except Exception:  # noqa: BLE001
            pass

    img = pick_emoji(mood_tag, reply) if mood_tag else None
    # 主人要图 → 优先从 Pixiv 收图里挑（带识图描述）；群聊不走这条，避免 R-18 外泄
    _pixiv_desc = ""
    if _PIXIV_INTENT.search(user_msg or ""):
        # 要色色 → 只从 r18 档挑；普通要图 → 全量（安全级加权更高）
        _want_lv = "r18" if _R18_INTENT.search(user_msg or "") else "any"
        _pp, _pixiv_desc = pick_pixiv_image(_want_lv)
        if _pp:
            img = _pp
            # 2026-11-06：sync 里存的 desc 对 R-18 是「目录直判」，等于没描述。
            # 这里现补一次 WD14 —— 她才知道自己发的是「绳缚·口球·红绳」还是「只穿内裤·躺床上」。
            try:
                from plugins import wd14_tagger as _wt3
                if _wt3.available():
                    _wd = await _wt3.describe(_pp, with_nsfw=True)
                    if _wd:
                        _pixiv_desc = f"画面：{_wd}"
            except Exception as _we3:  # noqa: BLE001
                logger.debug(f"[Pixiv] WD14 跳过：{type(_we3).__name__}")
            logger.info(f"[Pixiv] 私聊发图 {_pp.name}（{_pixiv_desc[:40]}）")
    logger.info(f"[TTS] 私聊回复就绪 reply长度={len(reply)} is_voice={is_voice_request} mood={mood_tag!r}")
    if not reply and not img: return

    voice_sent = False
    if is_voice_request and reply:
        logger.info(f"[TTS] 私聊语音分支触发 reply长度={len(reply)}")
        voice_file = await text_to_voice(reply)
        if voice_file:
            try:
                await private_ai.send(MessageSegment.record(voice_file))
                voice_sent = True
            except Exception as e:
                logger.error(f"[TTS] 私聊语音发送失败: {type(e).__name__}: {e}")
            finally:
                asyncio.create_task(safe_remove(voice_file))
        else:
            await private_ai.finish("嗓子哑了，唱不出来。")
            return

    if voice_sent:
        if img:
            await private_ai.send(MessageSegment.image(img))
        return

    try:
        from plugins import moderation
        reply = moderation.filter_outbound(reply, is_owner=True, channel="private")
    except Exception:  # noqa: BLE001
        pass
    try:
        from plugins import human_touch
        chunks = human_touch.shape(reply)
        _followup = human_touch.maybe_followup(reply)
    except Exception:  # noqa: BLE001
        chunks = split_reply(reply)
        _followup = None
    try:
        gap = float(cfg.reply.line_gap) if cfg.reply.line_gap is not None else 0.8
    except Exception:
        gap = 0.8
    gap = max(0.6, gap)

    _sent_texts: list[str] = []
    for i, seg in enumerate(chunks):
        _core = str(seg).strip(" 　。！？!?，,、；;~～…")
        if _core and any(_core in _s or _s in _core for _s in _sent_texts if len(_s) >= 6):
            logger.info(f"[去重] 跳过重复段落：{_core[:30]}")
            continue
        if _outbox_duplicate(f"p:{gid}", str(seg)):
            logger.info(f"[去重] 私聊 25s 内已发过同样内容，跳过：{_core[:30]}")
            continue
        await private_ai.send(MessageSegment.text(seg))
        if _core:
            _sent_texts.append(_core)
        if i < len(chunks) - 1:
            await asyncio.sleep(random.uniform(gap, gap * 1.5))

    # 气泡跟 QQ 显示同一份文字（之前气泡用原始 reply，QQ 发的是整形后的，会不一致）
    try:
        _shown = " ".join(x for x in chunks if x).strip()
        if _shown and _shown != reply:
            write_pet_live(_shown, "主人")
            if _history.get(gid):
                _history[gid][-1] = (NICKNAME, _shown[:cfg.context.max_len_per_msg], True)
    except Exception:  # noqa: BLE001
        pass

    if img:
        await private_ai.send(MessageSegment.image(img))
        # 补记：她知道发的是什么图（Pixiv 用识图描述，表情包用文件名）
        try:
            # 表情包以前只记文件名 → 她下一轮只能瞎猜图里是什么（2026-11-04）
            # 现在优先用视觉索引里的描述，实在没有才退回文件名
            _cap = _pixiv_desc or (_sticker_index().get(str(img)) or f"表情包 {Path(img).name}")
            await _record_bot_image(f"private_{user_id}", "主人", str(user_id), user_msg, _cap, gid,
                                    img_path=str(img))
        except Exception as _e:  # noqa: BLE001
            logger.warning(f"[发图记忆] 私聊记录失败: {type(_e).__name__}: {_e}")

    # 早上第一次聊天：偶尔提一句昨晚的梦（一天一次）
    try:
        from plugins import dream as _dream
        _ml = _dream.morning_line()
        if _ml:
            await asyncio.sleep(1.2)
            await private_ai.send(MessageSegment.text(_ml))
    except Exception:  # noqa: BLE001
        pass

    # 偶尔隔一两秒再补一句，像真人聊着聊着想起来
    if _followup:
        await asyncio.sleep(random.uniform(1.2, 2.6))
        try:
            await private_ai.send(MessageSegment.text(_followup))
        except Exception:  # noqa: BLE001
            pass

# =====================================================================
# 群聊逻辑
# =====================================================================
@ai.handle()
async def _(event: GroupMessageEvent):
    global ANGER_CHAIN, HOLD_GRUDGE, GRUDGE_TARGET
    if str(event.get_user_id()) == str(event.self_id):
        return   # 防御：同 collector
    # 2026-11-05 修：下面文档/语音分支会打日志用 user_id，但原来赋值在 150 行之后，
    # 一旦主人发文档或语音就会 UnboundLocalError 直接把整个群聊 handler 崩掉（表现为群聊不回复）。
    user_id = str(event.user_id)
    try:
        from desire import on_interact
        on_interact()
    except Exception:  # noqa: BLE001
        pass
    try:
        from thoughts import note_message as _thoughts_note
        _act = _thoughts_note(event.get_plaintext(), "", f"group_{event.group_id}")
        if _act:
            logger.info(f"[念头] {_act}")
    except Exception:  # noqa: BLE001
        pass
    if cfg.allow_groups and event.group_id not in cfg.allow_groups:
        logger.info(f"[TTS] 群 {event.group_id} 不在白名单 allow_groups，跳过")
        return

    gid = event.group_id
    try:
        # 2026-11-06：任何人开口，先结算她上一条主动发言「接没接住」。
        # 这份数据会反过来调冷却和概率 —— 「能动性」和「随机发言」的区别就在这。
        from plugins import proactive_feedback as _pf
        _pf.note_reply(gid, event.get_plaintext(),
                       "owner" if str(event.user_id) == str(YOUR_QQ_ID) else "member")
    except Exception:  # noqa: BLE001
        pass
    # 2026-11-06：主人一开口，「今天不想说话」立即解除
    try:
        from plugins import agency as _agc2
        _agc2.clear_quiet()
    except Exception:  # noqa: BLE001
        pass
    _LAST_USER_TS[f"g:{gid}"] = time.time()
    _group_run_tok = _new_run(f"g:{gid}", str(event.user_id))
    if is_sleeping():
        if not _called_in_group(event, event.get_plaintext()):
            logger.info(f"[睡觉] 群 {gid} 没叫她，静默不烧令牌")
            return
        # 被点名：跟私聊一样醒过来，但先捎一句迷糊的，再正常把话接住
        wake_up(180)
        logger.info(f"[睡觉] 群 {gid} 被点名，醒了（迷糊中）")
        try:
            _groggy = random.choice([
                "(*被吵醒，尾巴一甩*) 大半夜的叫什么叫……",
                "(*把头埋进枕头里*) 唔……听见了听见了，别喊了。",
                "(*迷迷糊糊坐起来*) …谁啊，这个点的。",
            ])
            await ai.send(_groggy)
            await asyncio.sleep(random.uniform(0.8, 1.6))
        except Exception:  # noqa: BLE001
            pass
        # 起床话术只发这一句，后面的回复按"刚醒"状态正常展开
        _st = _load_sleep_state()
        _st["waking_until"] = time.time() + 45
        _save_sleep_state(_st)
    # 群发言权限：新群要主人批准；被禁止的群完全静默（连 @ 也不回）
    if not gguard.is_allowed(gid):
        if gguard.needs_notice(gid):
            name = await _group_name(gid)
            gguard.mark_pending(gid, name)
            asyncio.create_task(_notify_owner_new_group(gid, name))
        logger.info(f"[群权限] 群 {gid} 未授权，静默")
        return

    raw_text = event.get_plaintext().strip()
    if len(raw_text) > 1000: raw_text = raw_text[:1000]

    # 群聊按人限流：每人每小时最多 10 条触发，防止有人刷复杂任务烧 token
    _uid = str(event.user_id)
    if _uid != str(YOUR_QQ_ID):
        _key = (gid, _uid)
        _now = time.time()
        _times = [t for t in GROUP_USER_HOURLY[_key] if _now - t < 3600]
        if len(_times) >= 10:
            logger.info(f"[群聊限流] gid={gid} uid={_uid} 本小时已触发 10 次，忽略")
            return
        _times.append(_now)
        GROUP_USER_HOURLY[_key] = _times

    _ask_where_raw = any(k in raw_text for k in (
        "去哪玩", "去哪了", "在干嘛", "干啥呢", "忙啥", "在玩什么", "去哪鬼混", "在做什么"))
    if not _private_chat_active() and not _ask_where_raw and not _bili_watching():
        set_activity("QQ群聊天中", 120, f"群 {gid}")

    user_msg = re.sub(rf"@{NICKNAME}\s*", "", raw_text, count=1).strip()
    if not user_msg: user_msg = re.sub(r"^@\S+\s*", "", raw_text).strip()
    # 群里同样：只有 /指令 才算指令
    user_msg, _is_cmd_g = _strip_cmd_prefix(user_msg)
    # 群聊同理：命令表里是带 / 的写法，这里还原一份给命令分支用
    _slash_g = ("/" + user_msg) if _is_cmd_g and user_msg else user_msg
    if not _is_cmd_g:
        user_msg = _maybe_passthrough(user_msg)
    elif str(event.user_id) == str(YOUR_QQ_ID):
        # 主人的群指令也记进该群历史（别人的指令不记，省 token）
        _install_cmd_history_recorder(ai, str(event.group_id), user_msg)
    # 审核：群里要密钥/改设定/装插件 → 软拒；问数学题 → 不给答案
    try:
        from plugins import moderation
        _is_owner_g = str(event.user_id) == str(YOUR_QQ_ID)
        _block = moderation.inbound_block(user_msg, is_owner=_is_owner_g)
        if not _block:
            _block = moderation.math_block(user_msg, is_owner=_is_owner_g)
        if _block:
            await ai.send(_block)
            return
    except Exception:  # noqa: BLE001
        pass
    # 文件消息：群里发的 PDF/Word/Excel 也读一下
    if not user_msg:
        try:
            from docs_reader import read_from_event as _read_doc
            _doc_text = await _read_doc(event)
        except Exception as e:  # noqa: BLE001
            _doc_text = ""
            logger.warning(f"[文档] 群聊解析失败：{type(e).__name__}: {e}")
        if _doc_text:
            user_msg = _doc_text
            logger.info(f"[文档] 群聊 gid={gid} uid={user_id} 抽到 {len(_doc_text)} 字")
    # 语音消息：群里发语音原来也被跳过，这里同样转文字
    if not user_msg:
        try:
            from asr import transcribe_from_event as _asr_transcribe
            _voice_text = await _asr_transcribe(event)
        except Exception as e:  # noqa: BLE001
            _voice_text = ""
            logger.warning(f"[语音识别] 群聊失败：{type(e).__name__}: {e}")
        if _voice_text:
            user_msg = _voice_text
            logger.info(f"[语音识别] 群聊 gid={gid} uid={user_id} -> {_voice_text[:40]!r}")
    raw_user_msg = user_msg  # 包装说话人提示词之前的干净问题文本，供联网规则判定用

    # 群聊流水：最近 8 条（谁说的 + 说了什么）。凶她时快照前因，切私聊时补给她
    try:
        from plugins import withdraw as _wd
        _wd.note_group_line("他" if str(event.user_id) == str(YOUR_QQ_ID) else "群友", user_msg)
    except Exception:  # noqa: BLE001
        pass

    # 回避档：被凶后短暂不接话（只对主人；群里多放行一句；指令/道歉必回）
    if str(event.user_id) == str(YOUR_QQ_ID):
        try:
            from plugins import withdraw
            _w_level, _w_blame = withdraw.level()
            if not withdraw.should_reply("group", user_msg, _is_cmd_g):
                withdraw.note_silent(user_msg or "[图片]")
                logger.info(f"[回避] 群聊不回（档={_w_level} blame={_w_blame:.0f}）msg={user_msg[:30]!r}")
                return
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[回避] 群聊检查失败：{type(e).__name__}: {e}")

    # 群里问她“去哪玩了/在干嘛”时，直接按网页端当前状态回答
    ask_where = any(k in user_msg for k in (
        "去哪玩", "去哪了", "在干嘛", "干啥呢", "忙啥", "在玩什么", "去哪鬼混", "在做什么"))
    if ask_where and len(user_msg) <= 30:
        _ans = _activity_reply_text()
        if _ans:
            await ai.finish(_ans)
            return

    images = []
    for seg in event.get_message():
        if seg.type == "image":
            url = seg.data.get("url") or seg.data.get("file")
            if url and isinstance(url, str) and len(url) > 5: images.append(url)

    user_id = str(event.user_id)
    is_insulted = False
    is_creep = False
    is_threat = False
    if user_id != YOUR_QQ_ID:
        maybe_update_relation_tag(user_id, f"group_{gid}")
        if is_ignoring(user_id):
            logger.info(f"[关系·不理] gid={gid} uid={user_id} 暂时不理，静默不烧令牌")
            return

    # 同一个人短时间内疯狂 @：先静默，避免刷屏
    if event.is_tome() and user_id != YOUR_QQ_ID:
        at_key = f"{gid}_{user_id}"
        now_ts = time.time()
        mute_until = AT_SPAM_MUTE.get(at_key, 0)
        if now_ts < mute_until:
            logger.info(f"[群聊拦截·@刷屏冷却] gid={gid} uid={user_id} 还剩{int(mute_until - now_ts)}s")
            return
        dq = AT_SPAM[at_key]
        while dq and now_ts - dq[0] > AT_SPAM_WINDOW:
            dq.popleft()
        dq.append(now_ts)
        if len(dq) >= AT_SPAM_LIMIT:
            AT_SPAM_MUTE[at_key] = now_ts + AT_SPAM_MUTE_SECONDS
            dq.clear()
            logger.info(
                f"[群聊拦截·@刷屏] gid={gid} uid={user_id} "
                f"{AT_SPAM_WINDOW}s 内 @ 了 {AT_SPAM_LIMIT} 次，静默 {AT_SPAM_MUTE_SECONDS}s"
            )
            await ai.finish("(*捂着耳朵躲远*) 别 @ 了别 @ 了，本鱼耳朵都起茧了，晾你两分钟再说。")
            return

    if event.is_tome():
        bot = get_bot()
        special_keywords = ["画图", "唱歌", "贴标签", "切换", "偷了", "存入表情包"]
        is_command = _is_cmd_g or any(kw in user_msg for kw in special_keywords)

        grudge_key = f"{gid}_{user_id}"
        is_grudge = grudge_key in HOLD_GRUDGE and time.time() < HOLD_GRUDGE[grudge_key]
        role = getattr(event.sender, "role", "member")
        is_admin = role in ["owner", "admin"]

        if any(word in user_msg for word in THREAT_WORDS) and user_id != YOUR_QQ_ID:
            is_threat = True
            GRUDGE_TARGET[gid] = {"id": user_id, "time": time.time() + 60}
            if is_admin:
                await ai.finish("(*撇嘴*) 行吧，看在你是管理员的份上，本鱼不跟你计较。")
            else:
                await ai.send("急了？")
                await asyncio.sleep(0.4)
                await ai.send("这就急了？")
                await asyncio.sleep(0.4)
                await ai.finish("小丑竟在我身边捏。[表情:生气]")
            return
        else:
            if not is_command:
                current_aff = await asyncio.to_thread(get_affinity, user_id)
                hour = time.localtime().tm_hour
                nick = event.sender.card or event.sender.nickname or str(user_id)

                if 0 <= hour < 6 and user_id != YOUR_QQ_ID:
                    # 2026-11-08：原来这里 70% 概率回一句固定台词
                    # 「(*把枕头扔过去*) 大半夜的艾特你妈呢？本鱼要睡觉！」，
                    # 不管别人说什么都是这一句，而且 finish 掉不再展开 —— 主人嫌它机械。
                    # 现在改成：凌晨非主人，**静默不理**（一个字都不发）的概率见常量，
                    # 剩下的走正常 AI —— 她会用自己的话、带着困倦的状态回，
                    # 不是背台词。主人自己 @ 她不受这条限制（上面已排除）。
                    # 2026-11-08 主人定：0.7 → 0.5（安静一半，另一半正常聊）。
                    r_late = random.random()
                    if r_late < _LATE_NIGHT_SILENT:
                        logger.info(f"[群聊拦截·凌晨静默] gid={gid} uid={user_id} nick={nick} aff={current_aff} admin={is_admin} r={r_late:.3f}<{_LATE_NIGHT_SILENT} msg={user_msg[:30]!r}")
                        return

                ignore_chance = 0.0 if user_id == YOUR_QQ_ID else (0.05 if is_admin else (0.3 if current_aff < 30 else 0.1))
                if is_grudge: ignore_chance = 0.8

                r_ignore = random.random()
                if r_ignore < ignore_chance:
                    r_form = random.random()
                    if r_form < 0.5:
                        logger.info(f"[群聊拦截·忽略短句] gid={gid} uid={user_id} nick={nick} aff={current_aff} admin={is_admin} grudge={is_grudge} chance={ignore_chance} r={r_ignore:.3f} msg={user_msg[:30]!r}")
                        await ai.finish(random.choice(["？", "哦", "懒得理你", "啧", "(*打哈欠*) 干嘛"]))
                    else:
                        logger.info(f"[群聊拦截·忽略摸鱼图] gid={gid} uid={user_id} nick={nick} aff={current_aff} admin={is_admin} grudge={is_grudge} chance={ignore_chance} r={r_ignore:.3f} msg={user_msg[:30]!r}")
                        img = pick_emoji("摸鱼")
                        if img: await ai.send(MessageSegment.image(img))
                    return

                r_flip = random.random()
                if r_flip < 0.1:
                    logger.info(f"[群聊拦截·已读乱回] gid={gid} uid={user_id} nick={nick} aff={current_aff} r={r_flip:.3f}<0.1 msg={user_msg[:30]!r}")
                    await ai.finish(random.choice(["V我50", "啊对对对", "急了", "你说是就是吧", "笑死"]))
                    return

        if user_msg in ["偷了", "存入表情包"]:
            if not images:
                await ai.finish("你倒是发图啊，虚空让本鱼偷吗？")
                return
            await ai.send("(*小心翼翼地收进兜里*) 好图，本鱼偷了！")
            try:
                STEAL_DIR.mkdir(parents=True, exist_ok=True)
                for i, img_url in enumerate(images):
                    resp = await http_client.get(img_url, follow_redirects=True, timeout=15)
                    if resp.status_code == 200:
                        file_name = f"steal_{int(time.time())}_{i}.jpg"
                        file_path = STEAL_DIR / file_name
                        with open(file_path, "wb") as f: f.write(resp.content)
                await ai.send("(*拍拍兜*) 存好了，以后这就是本鱼的库存了捏！")
            except Exception as e:
                await ai.send(f"(*挠头*) 没偷成功，是不是图太重了？报错：{e}")
            return

        if user_msg in ["/余额", "查余额", "还有多少钱"]:
            await ai.send("(*翻钱包*) 本鱼看看还剩多少生活费...")
            try:
                api_key = os.getenv("SILICONFLOW_API_KEY")
                headers = {"Authorization": f"Bearer {api_key}"}
                async with httpx.AsyncClient() as client:
                    r = await client.get("https://api.siliconflow.cn/v1/user/info", headers=headers)
                    data = r.json()
                    if data.get("status"):
                        balance = data["data"]["totalBalance"]
                        await ai.finish(f"💰 **大肥鱼的赛博钱包**\n━━━━━━━━━━━━━━━\n💵 当前余额：**{balance} 元**\n━━━━━━━━━━━━━━━\n(*叹气*) 主人，省着点花，本鱼还想多吃几碗白米饭呢！")
                    else: await ai.finish(f"(*捂住钱包*) 查不到啦，硅基流动说：{data}")
            except Exception as e: await ai.finish(f"(*摊手*) 查询余额失败，是不是网络断了？{e}")
            return

        if _slash_g in STATUS_COMMANDS:
            hunger, energy, body_desc = get_physical_status()
            highest, lowest = await asyncio.to_thread(get_affinity_extremes)
            top_str = "暂无记录"
            if highest:
                h_qq, h_score, h_tag = highest
                h_name = h_tag if h_tag else h_qq
                if h_score >= 60: top_str = f"【{h_name}】 (❤️ {h_score})"
            bottom_str = "暂无记录"
            if lowest:
                l_qq, l_score, l_tag = lowest
                l_name = l_tag if l_tag else l_qq
                if l_score <= 40: bottom_str = f"【{l_name}】 (💔 {l_score})"
            host_display = f"【{YOUR_QQ_ID}】"
            if user_id == YOUR_QQ_ID: host_display = f"【{YOUR_QQ_ID}】(就是您，主人大人！)"
            if GROUP_AI_STATE[gid] == 'google': brain_name = f"Google ({GEMINI_CHAT_MODEL})"
            elif GROUP_AI_STATE[gid] == 'siliconflow': brain_name = f"硅基流动 ({os.getenv('SILICONFLOW_MODEL', 'DeepSeek')})"
            else: brain_name = "字节 (DeepSeek V4)"
            draw_name = f"Google ({GEMINI_IMAGE_MODEL})" if GROUP_DRAW_STATE[gid] == 'google' else "豆包 (Seedream 4.0)"
            status_text = f"🐟 **大肥鱼の赛博全息体检报告**\n━━━━━━━━━━━━━━━\n👑 **唯一宿主**：{host_display}\n🧠 大脑引擎：{brain_name}\n🎨 画笔引擎：{draw_name}\n🎭 今日人设：{CURRENT_DAILY_MOOD}\n❤️ 本群心情：{GROUP_MOOD_STATE[gid]} (分值: {GROUP_MOOD_VAL_STATE[gid]})\n━━━━━━━━━━━━━━━\n🍚 饥饿度：{hunger}% \n⚡ 精力值：{energy}% \n🩺 生理状态：{body_desc}\n━━━━━━━━━━━━━━━\n👑 最爱群友：{top_str}\n🤡 最恨群友：{bottom_str}\n━━━━━━━━━━━━━━━\n💬 提示：发 `/好感度` 查你自己分数"
            await ai.finish(status_text)
            return

        rate_arg = _split_rate_command(user_msg)
        if rate_arg is not None:
            if str(user_id) != str(YOUR_QQ_ID):
                await ai.finish("(*叉腰*) 回复频率是主人才能调的，群友别瞎指挥捏。")
                return
            action, value = parse_rate_arg(rate_arg)
            if action == "view":
                await ai.finish(_rate_panel_text(current_rate()))
            elif action == "up":
                save_reply_rate(current_rate() + RATE_STEP)
                await ai.finish(_rate_reply_text("up", current_rate()))
            elif action == "down":
                save_reply_rate(current_rate() - RATE_STEP)
                await ai.finish(_rate_reply_text("down", current_rate()))
            elif action == "set":
                save_reply_rate(value)
                await ai.finish(_rate_reply_text("set", current_rate()))
            elif action == "invalid":
                await ai.finish("(*皱眉*) 百分比要在 0~100 之间啦，重发 `/频率 80` 这样。")
            else:
                await ai.finish(_rate_panel_text(current_rate()))
            return

        url_match = re.search(r'https?://[^\s]+', user_msg)
        if url_match:
            url = url_match.group(0)
            await ai.send(f"🔗 收到链接！本鱼这就去扒了它的底裤...")
            try:
                headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
                async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
                    resp = await c.get(url, headers=headers)
                    resp.encoding = resp.charset_encoding or 'utf-8'
                    html = resp.text[:300000]
                    text_content = re.sub(r'<script.*?</script>', '', html, flags=re.S)
                    text_content = re.sub(r'<style.*?</style>', '', text_content, flags=re.S)
                    clean_text = re.sub(r'<[^>]+>', ' ', text_content)
                    clean_text = re.sub(r'\s+', ' ', clean_text).strip()
                    if len(clean_text) < 50:
                        await ai.finish("这网页啥也没有，你发了个寂寞？"); return
                    prompt = f"请用贴吧老哥的口吻，极其毒舌、犀利地锐评一下以下网页的内容。字数200字以内：\n{clean_text[:3000]}"
                    reply = await call_ai_raw(prompt, GROUP_AI_STATE[gid])
                    await ai.finish(reply); return
            except Exception as e:
                await ai.finish(f"网页打不开，可能被墙了或者链接有问题。报错：{e}"); return

        if _slash_g in ["/help", "/指令", "/帮助"] or user_msg in ["help", "帮助", "指令"]:
            help_text = "🐟 **蓝色大肥鱼 - 终极玩法说明书**\n━━━━━━━━━━━━━━━\n🧠 **【引擎切换】** `切换大脑 谷歌/硅基/字节` / `切换画笔 谷歌/字节`\n💰 **【查余额】** `/余额`\n🎨 **【AI 绘图】** `/ 提示词` / `做个表情包：文字`\n🔗 **【网页锐评】** 发链接并 @本鱼\n🔍 **【联网查询】** 问今天新闻 / 今日大事，或说 `联网 查 xxx` 强制搜索\n📺 **【B站】** `B站热搜` / `B站热门` / `搜B站 关键词` / 发 B 站链接或 BV 号（登录/互动：私聊 `B站帮助`）\n📸 **【群友恶搞】** `逮捕 张三` / `恶搞 李四`\n🎲 **【互动与游戏】** `赛马` / `/日报` / `/status`、`/状态`(体检) / `签到`\n🎁 **【社交功能】** `决斗 @某人` / `查成分 @某人` / `贴标签 @某人` / `/好感度` / `偷了`(附图)\n🎤 **【语音与联动】** `语音说 内容` / `唱歌 歌名` / `桌宠 干活`\n🧠 **【长期记忆】** `昨天聊了啥` / `上周` / `上个月` / `去年` / `还记得...`\n📖 **【群内黑话】** `/词库` 看本群词条 · `/词库 词条` 查单个词含义\n📢 **【回复频率】** @本鱼 发 `/频率` 查看，`/频率 降低|提高|80` 调节（仅主人）\n━━━━━━━━━━━━━━━\n🪦 **【被动神级技能】**\n  复读机嘲讽 / 赛博墓碑 / 嘴臭反击 / **防撤回播报**\n"
            await ai.finish(help_text); return

        # 📖 群聊词条库面板：@本鱼 发 /词库 看本群黑话；/词库 词条 查单个词含义
        if _slash_g in LEXICON_COMMANDS or _slash_g.startswith(("/词库 ", "/词典 ", "/本群词库 ")):
            group_scope = f"group_{gid}"
            try:
                arg = None
                for prefix in ("/词库 ", "/词典 ", "/本群词库 "):
                    if user_msg.startswith(prefix):
                        arg = user_msg[len(prefix):].strip()
                        break
                if arg:
                    item = await asyncio.to_thread(glx.get_term, group_scope, arg)
                    if item:
                        tag = " 🖊(主人改过)" if item["manual"] else ""
                        await ai.finish(f"📖 **【{item['term']}】**{tag}\n遇见 {item['count']} 次：{item['meaning']}")
                    else:
                        await ai.finish(f"本鱼词典里没查到「{arg}」捏，群里多聊几次本鱼再学～")
                else:
                    terms = await asyncio.to_thread(glx.list_terms, group_scope, 12)
                    total = await asyncio.to_thread(glx.count_terms, group_scope)
                    if not terms:
                        await ai.finish("本群还没有词条捏～群友多聊几句，本鱼边听边记黑话！")
                        return
                    lines = [f"🐟 **本群黑话词典**（共 {total} 条）", "━━━━━━━━━━━━━━━"]
                    for i, t in enumerate(terms, 1):
                        tag = " 🖊" if t["manual"] else ""
                        lines.append(f"{i}. {t['term']}（{t['count']} 次）{tag}：{t['meaning']}")
                    await ai.finish("\n".join(lines))
            except Exception as e:
                logger.warning(f"[群聊词库] 面板读取失败: {type(e).__name__}: {e}")
                await ai.finish("(*挠头*) 词库这会儿打盹了，稍后再问本鱼嘛…")
            return

        if user_msg in ["/日报", "赛博日报"]:
            recent = await asyncio.to_thread(get_today_memory, 200, f"group_{gid}")
            if not recent: await ai.finish("今天群里都没人说话，日报个鬼啊！"); return
            text_hist = "\n".join([f"{m['speaker']}: {m['message'][:30]}" for m in recent])
            prompt = f"请用贴吧老哥的口吻，结合以下群聊记录，写一份搞笑的“本群赛博日报”，总结今天群里发生的趣事和谁是小丑：\n{text_hist}"
            await ai.send("📝 正在撰写今日战报...")
            reply = await call_ai_raw(prompt, GROUP_AI_STATE[gid])
            await ai.finish(reply); return




        if user_msg.startswith("唱歌"):
            _slang, song = _parse_sing_lang(user_msg)
            if not song:
                song = "小鲸鱼"
            _force = any(w in user_msg for w in ("重唱", "重新唱", "再唱一遍"))
            await ai.send(f"(*清清嗓子*) 本鱼要唱《{song}》啦，竖起耳朵听好——")
            async def _send_raw_group(p):
                await ai.send(MessageSegment.record(p))
                await ai.send("(*擦擦汗*) 原声先给你们听，本鱼去换成自己的声音～")

            audio, mode, lyrics, err = await sing_song(song, gid, _slang, _force, on_raw=_send_raw_group)
            if audio:
                await ai.send(MessageSegment.record(audio))
                tail = "（真·歌声，带伴奏的那种）" if mode == "song" else "（歌声服务还没开通，本鱼先念给你听）"
                await ai.finish(f"唱完啦！{tail}\n> {lyrics[:120]}")
                asyncio.create_task(safe_remove(audio))
            else:
                await ai.finish(f"嗓子哑了，唱不出来：{err}")
            return

        if user_msg in ["/好感度", "查好感度"]:
            score = await asyncio.to_thread(get_affinity, user_id)
            tag = await asyncio.to_thread(get_tag, user_id)
            rel_prompt = get_relationship_prompt(gid, user_id, score, tag)
            rel_name = re.search(r'【当前关系：(.*?)】', rel_prompt).group(1) if re.search(r'【当前关系：(.*?)】', rel_prompt) else "普通群友"
            tag_str = f"（外号：{tag}）" if tag else ""
            await ai.finish(f"当前【{event.sender.card or event.sender.nickname}】{tag_str}在本鱼这里的好感度是：❤️ {score}/100\n当前关系：**{rel_name}**")
            return

        if user_msg.startswith("贴标签"):
            at_segs = [seg for seg in event.get_message() if seg.type == "at"]
            manual_tag = re.sub(r"\[CQ:at,qq=\d+\]", "", user_msg).replace("贴标签", "").strip()

            if at_segs and manual_tag:
                await asyncio.to_thread(set_tag, str(at_segs[0].data["qq"]), manual_tag, f"group_{gid}")
                await ai.finish(f"好的，以后就叫他【{manual_tag}】了！")
                return
            elif at_segs and not manual_tag:
                target_id = str(at_segs[0].data["qq"])
                target_name = at_segs[0].data.get("name", "该群友")
                history_msgs = await asyncio.to_thread(get_user_recent_messages, target_id, 10, f"group_{gid}")
                if not history_msgs:
                    await asyncio.to_thread(set_tag, target_id, "潜水员", f"group_{gid}")
                    await ai.finish(f"{target_name}平时都不说话，就叫他【潜水员】吧！")
                    return
                await ai.send(f"正在翻阅{target_name}的群聊记录...")
                prompt = f"根据以下群友的发言，用一个2-5个字的词来调侃他/她，作为他/她的外号。只输出这个词：\n" + "\n".join(history_msgs)
                tag_result = await call_ai_raw(prompt, GROUP_AI_STATE[gid])
                tag_text = re.sub(r'[^\w\u4e00-\u9fa5]', '', tag_result)[:6] or "谜语人"
                await asyncio.to_thread(set_tag, target_id, tag_text, f"group_{gid}")
                await ai.send(f"根据平时的发言，本鱼决定给他贴个标签：【{tag_text}】！")
                return
            elif not at_segs and not manual_tag:
                history_msgs = await asyncio.to_thread(get_user_recent_messages, user_id, 10, f"group_{gid}")
                prompt = f"根据以下群友的发言，用一个2-5个字的词来调侃他/她，作为他/她的外号。只输出这个词：\n" + "\n".join(history_msgs) if history_msgs else "群友平时不说话"
                await ai.send("正在翻阅你的群聊记录...")
                tag_result = await call_ai_raw(prompt, GROUP_AI_STATE[gid])
                tag_text = re.sub(r'[^\w\u4e00-\u9fa5]', '', tag_result)[:6] or "潜水员"
                await asyncio.to_thread(set_tag, user_id, tag_text, f"group_{gid}")
                await ai.finish(f"行吧，以后就叫你【{tag_text}】了！")
                return


        if str(user_id) != YOUR_QQ_ID:
            if is_threat:
                GROUP_MOOD_VAL_STATE[gid] = 0; GROUP_MOOD_STATE[gid] = "生气"; is_creep = False
                ANGER_CHAIN[gid] += 2
                HOLD_GRUDGE[f"{gid}_{user_id}"] = time.time() + 7200
                await asyncio.to_thread(update_affinity, user_id, -30); trigger_desktop_pet("error", f"群友发出死亡威胁！")
                bump_daily_mood("diss", 25)
                bump_negative(user_id, f"group_{gid}", 50, 30)
            elif any(word in user_msg for word in CREEP_WORDS):
                GROUP_MOOD_VAL_STATE[gid] = 0; GROUP_MOOD_STATE[gid] = "生气"; is_creep = True
                ANGER_CHAIN[gid] += 1
                HOLD_GRUDGE[f"{gid}_{user_id}"] = time.time() + 7200
                await asyncio.to_thread(update_affinity, user_id, -20); trigger_desktop_pet("error", f"群友发变态言论！")
                bump_daily_mood("diss", 18)
                bump_negative(user_id, f"group_{gid}", 40, 20)
            elif any(word in user_msg for word in BAD_WORDS):
                GROUP_MOOD_VAL_STATE[gid] = 0; GROUP_MOOD_STATE[gid] = "生气"; is_insulted = True
                ANGER_CHAIN[gid] += 1
                HOLD_GRUDGE[f"{gid}_{user_id}"] = time.time() + 3600
                await asyncio.to_thread(update_affinity, user_id, -10); trigger_desktop_pet("error", f"群友用脏话骂我！")
                bump_daily_mood("diss", 12)
                bump_negative(user_id, f"group_{gid}", 25, 10)
            elif any(word in user_msg for word in ["白米饭", "夸", "可爱", "贴贴", "喜欢"]):
                if grudge_key in HOLD_GRUDGE and time.time() < HOLD_GRUDGE[grudge_key]:
                    await asyncio.to_thread(update_affinity, user_id, 5)
                    is_insulted = True
                else:
                    GROUP_MOOD_VAL_STATE[gid] = min(100, GROUP_MOOD_VAL_STATE[gid] + 30)
                    if GROUP_MOOD_VAL_STATE[gid] >= 80: GROUP_MOOD_STATE[gid] = "开心"
                    ANGER_CHAIN[gid] = max(0, ANGER_CHAIN[gid] - 1)
                    await asyncio.to_thread(update_affinity, user_id, 10); trigger_desktop_pet("attention", f"群友发白米饭啦！")
                    bump_daily_mood("happy", 8)
            else: ANGER_CHAIN[gid] = 0
            write_pet_state()
        else:
            if any(word in user_msg for word in ["白米饭", "夸", "可爱", "贴贴", "喜欢"]):
                await asyncio.to_thread(update_affinity, user_id, 20)
                bump_daily_mood("happy", 5)
                write_pet_state()


        if user_msg.startswith("做个表情包"):
            text = user_msg.replace("做个表情包", "").strip("：: ")
            img_url = None; img_path = None; meme_path = None
            try:
                r = await http_client.post(
                    "https://ark.cn-beijing.volces.com/api/v3/images/generations",
                    headers={"Authorization": f"Bearer {DOUBAO_DRAW_API_KEY}"},
                    json={"model": DOUBAO_IMAGE_MODEL, "prompt": "搞笑表情包底图，留白", "response_format": "url", "size": "1024x1024"}
                )
                img_url = r.json()["data"][0]["url"]
            except Exception: pass
            if img_url:
                try:
                    img_path = await download_image_to_temp(img_url)
                    if not img_path:
                        await ai.send("做表情包失败了：图片下载出错。"); return
                    meme_path = await asyncio.to_thread(sync_make_meme, img_path, text)
                    if meme_path:
                        await ai.send(MessageSegment.image(meme_path))
                        meme_caption = f"表情包：{text}" if text else "搞笑表情包"
                        meme_speaker = event.sender.card or event.sender.nickname or str(user_id)
                        await _record_bot_image(f"group_{gid}", meme_speaker, user_id, user_msg, meme_caption, gid)
                        asyncio.create_task(safe_remove(meme_path))
                    else: await ai.send("做表情包失败了")
                except Exception as e: await ai.send(f"处理失败: {e}")
                finally:
                    if img_path and os.path.exists(img_path):
                        try: os.remove(img_path)
                        except Exception: pass
            else: await ai.send("拿不到底图，生成失败。")
            return


        if user_msg in ["切换大脑 谷歌", "切大脑 谷歌", "切换大脑 硅基", "切大脑 硅基", "切换大脑 字节", "切大脑 字节",
                       "切换画笔 谷歌", "切画笔 谷歌", "切换画笔 字节", "切画笔 字节"] and str(user_id) != str(YOUR_QQ_ID):
            await ai.finish("(*按住开关*) 换大脑和画笔只有主人能改。")
            return
        if user_msg in ["切换大脑 谷歌", "切大脑 谷歌"]: GROUP_AI_STATE[gid] = "google"; await ai.finish("🧠 已切至 Google"); return
        elif user_msg in ["切官方", "切换大脑 官方", "切大脑 官方", "切回官方"]: GROUP_AI_STATE[gid] = "deepseek"; await ai.finish("🧠 已切回官方（DeepSeek）"); return
        elif user_msg in ["切换大脑 硅基", "切大脑 硅基", "切换大脑 字节", "切大脑 字节"]: GROUP_AI_STATE[gid] = "siliconflow"; await ai.finish("🧠 已切至 硅基流动"); return
        if user_msg in ["切换画笔 谷歌", "切画笔 谷歌"]: GROUP_DRAW_STATE[gid] = "google"; await ai.finish("🎨 已切至 Google"); return
        elif user_msg in ["切换画笔 字节", "切画笔 字节"]: GROUP_DRAW_STATE[gid] = "byte"; await ai.finish("🎨 已切至 豆包"); return

        pure_prompt = None
        _draw_mode = ""
        if _slash_g.startswith("/ ") and not _is_known_slash_command(_slash_g):
            pure_prompt = user_msg[1:].strip(); _draw_mode = "verbatim"
        elif _draw_prompt_of(user_msg) is not None:
            pure_prompt = _draw_prompt_of(user_msg); _draw_mode = "verbatim"
        else:
            _sa = _self_draw_action(user_msg)
            if _sa is not None:
                # 没指定动作 → 让她自己加戏
                _real_kw = ("真人", "写实", "摄影", "照片", "真实", "单反", "胶片")
                _real_tag = next((k for k in _real_kw if k in user_msg), "")
                # 2026-11-05：_sa 有值也走扩写。以前「做爱的场景」这种五个字的描述
                # 直接进 prompt，翻成英文就一句 sex scene，出白背景+飘道具。
                _improv = await _self_draw_improvise_llm(_sa or "")
                pure_prompt = f"{_improv}，{_real_tag}" if _real_tag and _real_tag not in _improv else _improv
                _draw_mode = "self"

        if pure_prompt is not None and _draw_mode:
            set_activity("画画中", 240)
            draw_speaker = event.sender.card or event.sender.nickname or str(user_id)
            # 出图顺序：本地 Z-Image（免费）→ 硅基 → 谷歌/豆包
            _gimg, _gsrc = await _draw_chain(pure_prompt, mode=_draw_mode, raw_hint=user_msg)
            if _gimg:
                await ai.send(MessageSegment.image(f"base64://{base64.b64encode(_gimg).decode()}"))
                try:
                    from plugins import feishu_bridge
                    if feishu_bridge.mirror_enabled():
                        await asyncio.to_thread(feishu_bridge.send_image_bytes, _gimg, "")
                except Exception:  # noqa: BLE001
                    pass
                try:
                    await _record_bot_image(gid, draw_speaker, user_id, user_msg, pure_prompt, gid)
                except Exception:  # noqa: BLE001
                    pass
                logger.info(f"[出图] 来源={_gsrc}")
                return
            if GROUP_DRAW_STATE[gid] == "google" and GEMINI_API_KEY:
                try:
                    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_IMAGE_MODEL}:generateContent?key={GEMINI_API_KEY}"
                    payload = {"contents": [{"parts": [{"text": pure_prompt}]}], "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]}}
                    r = await http_client.post(url, json=payload)
                    data = r.json()
                    if isinstance(data, dict) and "error" in data: await ai.finish(f"🎨 失败：{explain_google_error(data)}"); return
                    for part in data.get("candidates", [])[0]["content"]["parts"]:
                        img_obj = part.get("inlineData") or part.get("inline_data")
                        if img_obj and "data" in img_obj:
                            await ai.send(MessageSegment.image(f"base64://{img_obj['data']}"))
                            await _record_bot_image(f"group_{gid}", draw_speaker, user_id, user_msg, pure_prompt, gid)
                            return
                    await ai.finish("没图")
                except Exception as e: await ai.finish(f"出错：{e}")
                return
            # 2026-11-05：「画你自己」走参考图生图（外观锁死），其余还是纯文生图
            _is_self = bool(_self_draw_action(pure_prompt) is not None or "画你自己" in pure_prompt or "你自己" in pure_prompt)
            if _is_self:
                _act = _self_draw_action(pure_prompt)
                _scene = _act if _act else await _self_draw_improvise_llm()
                _img = await _doubao_ref_draw(_scene)
                if _img:
                    _p0 = ROOT / "data" / "draw_out"
                    _p0.mkdir(parents=True, exist_ok=True)
                    _fp = _p0 / f"doubao_self_{int(time.time())}.jpg"
                    _fp.write_bytes(_img)
                    await ai.send(MessageSegment.image(_fp.read_bytes()))
                    await _record_bot_image(f"group_{gid}", draw_speaker, user_id, user_msg, f"画你自己：{_scene}"[:80], gid)
                    return
                logger.warning("[豆包] 参考图生图没拿到，回退纯文生图")
            try:
                r = await http_client.post(
                    "https://ark.cn-beijing.volces.com/api/v3/images/generations",
                    headers={"Authorization": f"Bearer {DOUBAO_DRAW_API_KEY}"},
                    json={"model": DOUBAO_IMAGE_MODEL, "prompt": pure_prompt, "response_format": "url", "size": "1024x1024"}
                )
                res = r.json()
                if "data" in res and len(res["data"]) > 0:
                    await ai.send(MessageSegment.image(res["data"][0]["url"]))
                    await _record_bot_image(f"group_{gid}", draw_speaker, user_id, user_msg, pure_prompt, gid)
                    return
                await ai.finish(f"豆包失败：{res}")
            except Exception as e: await ai.finish(f"出错：{e}")
            return

    if event.is_tome(): _last_at[gid] = time.time()
    # 主人不用 @，叫名字（大肥鱼/鲸鱼娘/昵称）也算直接叫她，不限次数
    owner_call = (str(event.user_id) == str(YOUR_QQ_ID)
                  and ("大肥鱼" in raw_text or "鲸鱼娘" in raw_text or NICKNAME in raw_text))
    if owner_call:
        _last_at[gid] = time.time()
    recent_at = (time.time() - _last_at.get(gid, 0)) < cfg.context.at_window
    # 没人@时：窗口内图片照旧接话看图；窗口外纯图按当前频率概率随机吐槽；
    # 纯文本不进本 matcher（由 collector 主动插话链路处理）。
    image_pass = False
    if images:
        if recent_at:
            image_pass = True
        else:
            r_img = random.random()
            image_pass = r_img < RATE_IMAGE_BASE * rate_k()
            logger.info(f"[群聊看图·非@窗口外] gid={gid} uid={user_id} 看图r={r_img:.3f} 阈值={RATE_IMAGE_BASE * rate_k():.3f} pass={image_pass}")
    if not (event.is_tome() or image_pass or owner_call):
        logger.info(f"[群聊静默·非@] gid={gid} uid={user_id} nick={event.sender.card or event.sender.nickname or user_id} 未@且不在@窗口内(有图={bool(images)}) msg={user_msg[:30]!r}")
        return
    if not user_msg and not images:
        logger.info(f"[群聊静默·空文本] gid={gid} uid={user_id} 去除@后无文字也无图")
        return
    if not owner_call and time.time() - GROUP_COOLDOWN[gid] < 1.5:
        logger.info(f"[群聊静默·冷却] gid={gid} uid={user_id} 距上次群聊回复<1.5秒，跳过")
        return
    GROUP_COOLDOWN[gid] = time.time()

    _raw_name = event.sender.card or event.sender.nickname or str(event.user_id)
    # 2026-11-08：**以 QQ 号为第一身份**。原来这里直接用当前群名片，
    # 群友一改名，她眼里就从「小白」变成「摸鱼的猫」—— 等于换了个新人，
    # 于是记错人、把旧事记到别人头上。plugins/nick_names.py 早就写好了这套
    # （QQ→首个昵称+最新昵称），但主链路从来没用过，只有 /查这人 那条指令在用。
    # resolve() 改名后返回「小白(现:摸鱼的猫)」，她一看就知道是同一只。
    try:
        from plugins import nick_names as _nn
        speaker = _nn.resolve(gid, event.user_id, _raw_name)
    except Exception:  # noqa: BLE001
        speaker = _raw_name
    if gid not in _history_loaded:
        _history_loaded.add(gid)
        try:
            recent = await asyncio.to_thread(
                get_recent_memory, _history_backfill_rows(), f"group_{gid}"
            )
            for r in recent:
                _history[gid].append(
                    (r.get("speaker") or "群友", (r.get("message") or "")[:cfg.context.max_len_per_msg], False)
                )
                if r.get("reply"):
                    _history[gid].append(
                        (NICKNAME, (r.get("reply") or "")[:cfg.context.max_len_per_msg], True)
                    )
        except Exception as e:
            logger.warning(f"[记忆] 群 {gid} 历史回填失败: {e}")
    # 历史只增不改：不再 [-history_use:] 滑动、不再 [-1500:] 截尾
    _compact_history(gid, HISTORY_TOKEN_BUDGET_GROUP)
    hist = list(_history[gid])
    # collector（priority=1）已经把当前这句追加进 _history 了：只有确认最后一条就是当前消息
    # 才剔除，避免它同时出现在历史和最新 user 里。匹配不上（睡眠/黑名单/未采集）就保留，
    # 宁可重复一句，也不要丢上下文。
    if (hist and not hist[-1][2] and hist[-1][0] == speaker
            and (hist[-1][1] or "") == ((user_msg or "[图片]")[:cfg.context.max_len_per_msg])):
        hist = hist[:-1]

    is_voice_request = _is_voice_request(user_msg)
    if images:
        angle = random.choice(IMAGE_ANGLES)
        user_msg = f"（{speaker}发了张图。吐槽角度：{angle}）\n{speaker}还说：{user_msg}" if user_msg else f"（{speaker}发了张图）"
    elif is_voice_request:
        user_msg = f"（{speaker}要求你用语音念出回复。请用傲娇少女第一人称口语化说 1~2 句短句，不要括号动作、表情标记和书面语，方便直接朗读。）\n{speaker}说：{user_msg}"
    else:
        user_msg = f"【当前说话人：{speaker}】\nTa说：{user_msg}"

    if random.random() < 0.2:
        await ai.send("(*托腮*)")
        await asyncio.sleep(0.5)

    current_affinity = await asyncio.to_thread(get_affinity, user_id)
    current_tag = await asyncio.to_thread(
        get_tag, user_id,
        str(gid) if str(gid).startswith("private_") else f"group_{gid}",
    )
    user_sex = getattr(event.sender, "sex", "unknown")

    sys_stable, sys_volatile, sys_slow = build_prompt_parts(
        gid, user_id, is_insulted, is_creep, is_threat, current_affinity, current_tag,
        is_private=False, sex=user_sex, user_text=user_msg.replace("\u200b", ""), channel="qq",
        hist_len=len(hist), hist_tok=_history_tokens(hist),
    )
    extra_blocks: list = list(sys_slow)   # 慢变块放历史之后，前缀才能稳定

    mem_intent = detect_memory_intent(user_msg, is_private=False)
    if mem_intent:
        mem_text = await asyncio.to_thread(
            recall_memory, f"group_{gid}", mem_intent, False, 1500, True
        )
        if mem_text:
            extra_blocks.append("【回忆本群历史】\n" + mem_text)

    if images:
        current_content: list = [{"type": "text", "text": user_msg}]
        for u in images: current_content.append({"type": "image_url", "image_url": {"url": u}})
    else:
        current_content = user_msg

    # 布局：稳定 system → 历史真实多轮 → 回忆/群聊近况 → 易变状态 → 最新一句 user
    messages = _compose_chat_messages(
        sys_stable, _history_messages(False, hist), extra_blocks, sys_volatile, current_content
    )

    if not images:
        # 2026-11-06 修：主人反馈「每次描述的是上一张」。
        # 这里原来无条件把上一张图的描述塞进 prompt（有效期 10 分钟），
        # 于是这一轮哪怕在聊别的事，她也会顺嘴说上一张图 —— 看着就像认错图。
        # 现在：①有效期收到 90 秒 ②明确标注是「之前那张、不是现在这张」。
        _cached = _IMG_DESC_CACHE.get(str(gid))
        if _cached and time.time() - _cached[1] < 90:
            _age = int(time.time() - _cached[1])
            messages.append({"role": "system",
                             "content": (f"【{_age} 秒前那张图的内容，不是这一轮发的】"
                                         + _cached[0]
                                         + "（主人这轮没发新图，别把它当成现在这张）")})
    else:
        # 这一轮有新图 → 旧描述立刻作废，防止串到新图上
        _IMG_DESC_CACHE.pop(str(gid), None)

    reply, _ = await _web_or_chat(messages, GROUP_AI_STATE[gid], False, raw_user_msg, images, context_key=str(gid))

    if _superseded(f"g:{gid}", _group_run_tok, str(user_id)):
        logger.info(f"[中断] gid={gid} 生成期间来了更新的消息，本轮回话作废")
        return

    if reply.startswith("[AI 调用失败]") or reply.startswith("[谷歌") or reply.startswith("[硅基流动"):
        await ai.finish("（本鱼突然脑血栓了，稍后再试）")
        return

    mood_tag = ""
    m = re.search(r"\[表情[:：]\s*([^\]]*)\]", reply)
    if m:
        mood_tag = re.sub(r'[^\w\u4e00-\u9fa5]', '', m.group(1).strip())
        reply = re.sub(r"\[表情[:：]\s*[^\]]*\]", "", reply).strip()

    reply, _new_nick = _extract_nickname_marker(reply)
    reply = _strip_bot_marker(reply)
    if not str(gid).startswith("private_"):
        reply = _deflavor_group(reply, gid)
    if _new_nick and str(user_id) == YOUR_QQ_ID:
        await asyncio.to_thread(set_tag, YOUR_QQ_ID, _new_nick,
                                str(gid) if str(gid).startswith("private_") else f"group_{gid}")
        await _sync_owner_remark(_new_nick)
        logger.info(f"[外号] 主人外号更新为 {_new_nick}（群聊对话）")

    if len(reply) > 300: reply = reply[:300] + "..."
    # 2026-11-05：群聊也过人味加法层（同上，只加字不改意思）
    global _HUMAN_CHARS
    try:
        from plugins import humanize as _hz_add_g
        reply = _hz_add_g.humanize_add(reply, chars_so_far=_HUMAN_CHARS)
        _HUMAN_CHARS += len(reply)
    except Exception:  # noqa: BLE001
        pass
    clean_reply_check = re.sub(r'[^\w\u4e00-\u9fa5]', '', reply)
    logger.info(f"[TTS] 群聊回复就绪 reply长度={len(reply)} is_voice={is_voice_request} gid={gid}")
    if not clean_reply_check and not mood_tag: return

    voice_sent = False
    if is_voice_request and reply:
        logger.info(f"[TTS] 群聊语音分支触发 reply长度={len(reply)}")
        voice_file = await text_to_voice(reply)
        if voice_file:
            try:
                await ai.send(MessageSegment.record(voice_file)); voice_sent = True
            except Exception as e: logger.error(f"[TTS] 群聊语音发送失败: {type(e).__name__}: {e}")
            finally: asyncio.create_task(safe_remove(voice_file))

    if not reply and not mood_tag: return

    group_scope = f"group_{gid}"
    # 2026-11-05 修（根因，同私聊）：清洗必须在写 pending 之前，否则长期记忆拿到的是原文
    _hist_reply_g = _strip_fake_state(reply) if reply else reply
    if _hist_reply_g:
        try:
            from plugins import humanize as _hz_rep_g
            _hist_reply_g, _dropped_g = _hz_rep_g.strip_self_repeat(
                _hist_reply_g, _RECENT_SENTS.get(gid) or [])
            if _dropped_g:
                logger.info(f"[去重] 群 {gid} 删掉重复的自起话头：{_dropped_g[:40]}")
                try:
                    from plugins import obsessions as _obs_g
                    _obs_g.touch(_dropped_g)
                except Exception:  # noqa: BLE001
                    pass
            _RECENT_SENTS[gid].extend(_hz_rep_g._split_sents(_hist_reply_g))
            if len(_RECENT_SENTS[gid]) > 60:
                del _RECENT_SENTS[gid][:-60]
        except Exception as _re_g:  # noqa: BLE001
            logger.debug(f"[去重] 群聊跳过：{type(_re_g).__name__}")
    try:
        await asyncio.to_thread(add_memory, speaker, user_id, group_scope, user_msg, reply)
        await asyncio.to_thread(save_daily, group_scope, speaker, user_id, user_msg, reply)
        await ltm.enqueue_pending(group_scope, speaker, user_msg)
        if _hist_reply_g:
            await ltm.enqueue_pending(group_scope, NICKNAME, _hist_reply_g)
    except Exception as e:
        logger.error(f"❌ 记忆存入数据库失败: {e}")

    # 📚 @ 对话同样喂给词条库：用户提问命中已有词计数，提问文本入攒批缓冲供抽群内黑话/昵称
    if cfg.lexicon.enabled:
        try:
            await asyncio.to_thread(glx.touch_terms, group_scope, raw_user_msg)
            if reply:
                await asyncio.to_thread(glx.touch_terms, group_scope, reply)
            reached = await asyncio.to_thread(glx.push_pending, group_scope, speaker, raw_user_msg)
            if reached and cfg.lexicon.extract_ai_enabled:
                asyncio.create_task(glx.run_extract(group_scope))
        except Exception as e:
            logger.warning(f"[群聊词库] @ 消息计数失败 gid={gid}: {type(e).__name__}: {e}")

    if voice_sent:
        if mood_tag:
            img = pick_emoji(mood_tag, reply)
            if img: await ai.send(MessageSegment.image(img))
        return

    try:
        from plugins import moderation
        reply = moderation.filter_outbound(reply, is_owner=(str(user_id) == str(YOUR_QQ_ID)), channel="group")
    except Exception:  # noqa: BLE001
        pass
    img = pick_emoji(mood_tag) if mood_tag else None
    try:
        from plugins import human_touch
        _shaped = human_touch.shape(reply)
        _followup = human_touch.maybe_followup(reply)
    except Exception:  # noqa: BLE001
        _shaped = split_reply(reply)
        _followup = None
    chunks = build_chunks(_shaped, img, [MessageSegment.at(event.user_id)] if cfg.reply.mention else [])

    if len(chunks) > 0 and random.random() < 0.1:
        msg_to_recall = await ai.send(Message(chunks[0]))
        await asyncio.sleep(1.5)
        try:
            bot = get_bot()
            await bot.delete_msg(message_id=msg_to_recall["message_id"])
        except Exception: pass
        await ai.send(Message(MessageSegment.text("（啧，手滑了，本鱼什么都没说）")))

        for i in range(1, len(chunks)):
            await ai.send(Message(chunks[i]))
            if i < len(chunks) - 1: await asyncio.sleep(random.uniform(0.8, 1.5))
        return

    _sent_g: list[str] = []
    for i, seg in enumerate(chunks):
        _txt_g = str(seg).strip()
        _core_g = _txt_g.strip(" 　。！？!?，,、；;~～…")
        if _core_g and any(_core_g in _s or _s in _core_g for _s in _sent_g if len(_s) >= 6):
            logger.info(f"[去重] 群聊跳过重复段落：{_core_g[:30]}")
            continue
        if _outbox_duplicate(f"g:{gid}", str(seg)):
            logger.info(f"[去重] 群聊 25s 内已发过同样内容，跳过：{_core_g[:30]}")
            continue
        await ai.send(Message(seg))
        if _core_g:
            _sent_g.append(_core_g)
        if i < len(chunks) - 1:
            await asyncio.sleep(random.uniform(0.8, 1.5))

    # 群里也偶尔补一句（概率比私聊低）
    try:
        _fu = locals().get("_followup")
    except Exception:  # noqa: BLE001
        _fu = None
    if _fu:
        await asyncio.sleep(random.uniform(1.2, 2.6))
        try:
            await ai.send(Message(MessageSegment.text(_fu)))
        except Exception:  # noqa: BLE001
            pass

    # 清洗已在上面（写 pending 之前）做过了，这里直接进历史
    _history[gid].append((NICKNAME, _hist_reply_g, True))
    write_pet_live(reply, "群友")

@scheduler.scheduled_job("cron", hour=4, minute=0)
async def clean_inactive_groups():
    now = time.time()
    inactive = [gid for gid, t in _last_at.items() if now - t > 86400 * 7]
    for gid in inactive:
        _last_at.pop(gid, None); _history.pop(gid, None); GROUP_COOLDOWN.pop(gid, None)
        if gid in GROUP_AI_STATE: del GROUP_AI_STATE[gid]
        if gid in GROUP_DRAW_STATE: del GROUP_DRAW_STATE[gid]
        if gid in GROUP_MOOD_STATE: del GROUP_MOOD_STATE[gid]
        if gid in GROUP_MOOD_VAL_STATE: del GROUP_MOOD_VAL_STATE[gid]
        if gid in GROUP_LAST_MESSAGES: del GROUP_LAST_MESSAGES[gid]
        if gid in GROUP_MEMES: del GROUP_MEMES[gid]
        if gid in LEAVE_THREAT_COUNT: del LEAVE_THREAT_COUNT[gid]
        GROUP_PROACTIVE_COOLDOWN.pop(gid, None)
        GROUP_PROACTIVE_DAILY.pop(gid, None)
    private_keys = [k for k in PRIVATE_COOLDOWN.keys() if now - PRIVATE_COOLDOWN[k] > 86400 * 7]
    for k in private_keys:
        PRIVATE_COOLDOWN.pop(k, None); _history.pop(k, None)
        if k in GROUP_AI_STATE: del GROUP_AI_STATE[k]
        if k in GROUP_DRAW_STATE: del GROUP_DRAW_STATE[k]
        if k in GROUP_MOOD_STATE: del GROUP_MOOD_STATE[k]
        if k in GROUP_MOOD_VAL_STATE: del GROUP_MOOD_VAL_STATE[k]
    EMOJI_CACHE.clear()
    logger.info(f"🧹 清理了 {len(inactive)} 个不活跃群的缓存，{len(private_keys)} 个私聊缓存，并清空了表情包缓存。")


# 随机吃饭：早饭 9-10、午饭 12-14、晚饭 18-20、夜宵 0-5
@scheduler.scheduled_job("interval", minutes=10)
async def random_meal_job():
    try:
        st = _load_meal_state()
        win = meal_window(time.localtime().tm_hour)
        if win and not st.get("eaten", {}).get(win):
            if random.random() <= 0.22:
                eat_meal(win)
            return
        # 不在饭点、但已经饿到 80 以上：随机吃口东西垫肚子（30 分钟最多一次）
        hunger = float(st.get("hunger", 60))
        if hunger >= 80:
            last = float(st.get("snack_ts") or 0)
            if time.time() - last > 1800 and random.random() <= 0.35:
                st["hunger"] = max(5, int(hunger) - random.randint(20, 30))
                st["energy"] = min(100, int(st.get("energy", 65)) + random.randint(4, 10))
                st["snack_ts"] = time.time()
                st["last_meal"] = {"kind": "snack", "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
                _save_meal_state(st)
                try:
                    write_pet_state(force_body=True)
                except Exception:  # noqa: BLE001
                    pass
                logger.info(f"[吃饭] 饿了({hunger:.0f}%)，自己垫了一口，饥饿度降到 {st['hunger']}%")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[吃饭] 随机吃饭异常：{e}")


# ---------------- 收尾语检测（2026-11-05，借 dsh-wake 的做法）----------------
# 主人说「晚安」之后她不该四小时后再来叫人。只看主人那边最后一句的**末尾**，
# 正文中间提到这些词不算——否则「我昨晚说晚安的时候」也会被误判成收尾。
_TAIL_WORDS = (
    "晚安", "先这样", "明天见", "睡了", "去睡", "早点休息", "再见", "拜拜",
    "不聊了", "困了", "挂了啊", "就这样吧", "我去睡了", "先睡了",
)


def _owner_closed_topic(rows, window: int = 4) -> bool:
    """最近几条里，主人那边最后一句是不是收尾语。"""
    try:
        for r in list(rows or [])[::-1][:window]:
            who = str(r.get("speaker") or "")
            msg = str(r.get("message") or "").strip()
            if not msg or "主人" not in who:
                continue
            parts = [x for x in re.split(r"[。！？!?\n]+", msg) if x.strip()]
            if not parts:
                continue
            last = parts[-1].strip()
            return any(w in last for w in _TAIL_WORDS)
    except Exception:  # noqa: BLE001
        pass
    return False


# ---------------- 尾波（2026-11-05，借 dsh-wake）----------------
# 一轮说完后随机等 60~120 秒，没人说话就顺着上文再补一小段。
# 和 proactive_private_chat 的区别：那个是「隔几小时想你了」，这个是「话没说完」。
# 配置：memory/wake_config.json（没有文件就是关着的）。
_WAKE_STATE = {"at": 0.0, "streak": 0, "task": None}


def _wake_cfg() -> dict:
    try:
        _d = json.loads((ROOT / "memory" / "wake_config.json").read_text(encoding="utf-8-sig")) or {}
    except Exception:  # noqa: BLE001
        _d = {}
    return {
        "enabled": bool(_d.get("enabled", False)),
        "min_seconds": max(5, int(_d.get("min_seconds", 60))),
        "max_seconds": max(10, int(_d.get("max_seconds", 120))),
        "max_streak": max(0, int(_d.get("max_streak", 3))),
        "prob": float(_d.get("prob", 0.8)),
        "decay": float(_d.get("decay", 0.1)),
    }


def _wake_touch() -> None:
    """有消息进来 → 刷新活动时间、清零连击、取消待发的尾波。"""
    _WAKE_STATE["at"] = time.time()
    _WAKE_STATE["streak"] = 0
    t = _WAKE_STATE.get("task")
    if t is not None and not t.done():
        t.cancel()
    _WAKE_STATE["task"] = None


def _wake_schedule(gid, user_id) -> None:
    """一轮结束后调度一次尾波（重复调度会取消上一次）。"""
    cfg = _wake_cfg()
    if not cfg["enabled"] or cfg["max_streak"] <= 0:
        return
    t = _WAKE_STATE.get("task")
    if t is not None and not t.done():
        t.cancel()
    _WAKE_STATE["task"] = asyncio.create_task(_wake_loop(gid, user_id))


async def _wake_loop(gid, user_id) -> None:
    """尾波主循环：等 → 检查 → 生成 → 发 → 再来一次（直到连击用完）。"""
    try:
        while True:
            cfg = _wake_cfg()
            if not cfg["enabled"] or _WAKE_STATE["streak"] >= cfg["max_streak"]:
                return
            _t0 = time.time()
            await asyncio.sleep(random.uniform(cfg["min_seconds"], cfg["max_seconds"]))
            # 这一段时间里有人说话 → 散场
            if _WAKE_STATE["at"] > _t0:
                logger.debug("[尾波] 期间有人说话，不接了")
                return
            # 收尾语 → 散场
            try:
                _rows = await asyncio.to_thread(get_recent_memory, 4, f"private_{user_id}")
            except Exception:  # noqa: BLE001
                _rows = []
            if _owner_closed_topic(_rows):
                logger.info("[尾波] 最后一句是收尾语，不接了")
                return
            # 收敛递减：越续越难开口
            _p = cfg["prob"] - cfg["decay"] * _WAKE_STATE["streak"]
            if _p <= 0 or random.random() > _p:
                logger.info(f"[尾波] 这次收住（把握 {_p:.2f}，已续 {_WAKE_STATE['streak']} 次）")
                return
            # 生成一段接续
            _recent = ""
            try:
                _recent = "；".join(
                    f"{r.get('speaker') or '主人'}：{(r.get('message') or '')[:40]}"
                    for r in (_rows or [])[::-1])
            except Exception:  # noqa: BLE001
                _recent = ""
            # 2026-11-05：原来只给一句"你在扮演傲娇鲸鱼娘"，没走人格链路，
            # 出来的是"通用傲娇少女"而不是她——主人说有人机感。
            # 现在复用正式人格前缀（安全 + persona + 桌宠设定），
            # 并把指令从"写一段话"换成"脑子里冒出来的念头"。
            _sys = security_prompt + cfg.persona.prompt + DESKTOP_PET_SETTING
            _mood = ""
            try:
                _v = _load_emotion()
                _top = max(_v, key=lambda k: _v.get(k, 0)) if _v else ""
                if _top and float(_v.get(_top) or 0) >= 35:
                    _mood = f"你现在有点「{_top}」。"
            except Exception:  # noqa: BLE001
                pass
            try:
                _say, _meta = await _chat(
                    [{"role": "system", "content": _sys},
                     {"role": "user", "content": (
                         "（下面这句是你自己脑子里的，不是主人发来的）\n"
                         f"刚才聊到：{_recent}\n"
                         f"{_mood}"
                         "他还没回。你心里还剩点没说出口的，直接讲了，不用起头也不用收。"
                     )}],
                    temperature=0.88, max_tokens=180,
                    record_scope="wake", channel="system")
                _say = str(_say or "").strip()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[尾波] 生成失败：{type(e).__name__}: {e}")
                return
            if not _say:
                return
            try:
                from nonebot import get_bot
                await get_bot().send_private_msg(user_id=int(user_id), message=_say[:400])
                _WAKE_STATE["streak"] += 1
                _WAKE_STATE["at"] = time.time()
                logger.info(f"[尾波] 接了第 {_WAKE_STATE['streak']} 句：{_say[:36]}")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[尾波] 发送失败：{type(e).__name__}: {e}")
                return
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[尾波] 异常退出：{type(e).__name__}: {e}")


_EMOJI_ROOT = ROOT / "表情包"
_PIXIV_R18 = Path(os.path.expanduser("~")) / ".dsh" / "pixiv-duty" / "library" / "R-18"


def _pick_emoji(mood: str = "") -> Path | None:
    """从表情包里按情绪挑一张。挑不到就用「卖萌」。"""
    if not _EMOJI_ROOT.exists():
        return None
    dirs = []
    if mood:
        for d in _EMOJI_ROOT.iterdir():
            if d.is_dir() and (mood in d.name or d.name in mood):
                dirs.append(d)
    if not dirs:
        d = _EMOJI_ROOT / "卖萌"
        if d.is_dir():
            dirs = [d]
    if not dirs:
        dirs = [d for d in _EMOJI_ROOT.iterdir() if d.is_dir()]
    for d in dirs:
        files = [f for f in d.iterdir()
                 if f.is_file() and f.suffix.lower() in (".jpg", ".jpeg", ".png", ".gif", ".webp")]
        if files:
            return random.choice(files)
    return None


def _r18_hint(path) -> str:
    """从 pixiv 库的目录结构里读出这幅画是什么。

    2026-11-06：云端识图和本地 QwenVL 都会拒 NSFW（实测本地直接回
    「我不能提供您所要求的图片内容」），所以走不了视觉通道。
    但 pixiv 库的目录名本身带着信息：
        R-18/插画/<画师>/<作品标题>_<作品ID>/<页码>.png
    至少能说清「谁画的、叫什么、第几张」，比「R-18 目录直判」有用得多。
    """
    try:
        from pathlib import Path as _P
        p = _P(str(path))
        parts = list(p.parts)
        # 找到 R-18 那一层往后取
        idx = None
        for i, seg in enumerate(parts):
            if seg.upper().startswith("R-18") or seg.upper() == "R18":
                idx = i
                break
        if idx is None or len(parts) < idx + 3:
            return ""
        tail = parts[idx + 1:]           # [分类, 画师, 作品, 文件]
        kind = tail[0] if len(tail) >= 4 else ""
        artist = tail[-3] if len(tail) >= 3 else ""
        work = tail[-2] if len(tail) >= 2 else ""
        fname = tail[-1]
        # 作品名去掉 _作品ID 尾巴
        title = work
        if "_" in work:
            a, b = work.rsplit("_", 1)
            if b.isdigit():
                title = a
        page = fname.rsplit(".", 1)[0]
        bits = []
        if artist:
            bits.append(f"{artist} 的画")
        if title:
            bits.append(f"《{title}》")
        if page.isdigit():
            bits.append(f"第 {int(page)} 张")
        elif page:
            bits.append(f"（{page}）")
        if kind:
            bits.insert(0, f"pixiv {kind}")
        return "·".join(bits) if bits else ""
    except Exception:  # noqa: BLE001
        return ""


def _pick_r18() -> Path | None:
    """从 pixiv 的 R-18 库里随机挑一张。"""
    if not _PIXIV_R18.exists():
        return None
    files = []
    for f in _PIXIV_R18.rglob("*"):
        try:
            if f.is_file() and f.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                if f.stat().st_size > 30 * 1024:      # 太小的多半是缩略图
                    files.append(f)
        except OSError:
            continue
    return random.choice(files) if files else None


def _current_arousal() -> float:
    """当前情欲值（第 8 维，不在 EMOTIONS 里）。"""
    try:
        from plugins import hormones as _h
        return float((_h.derive_emotions() or {}).get("情欲") or 0)
    except Exception:  # noqa: BLE001
        return 0.0


async def _maybe_send_self_media(bot, text: str) -> None:
    """主动私聊说完话，偶尔补一张自己 / 一句自己的声音。全都本地，不花钱。

    2026-11-06 按主人要求加。限流：图一天 1 张、语音一天 1 条，
    而且只在「她刚才那句话情绪足够强」时才带上（闲着没事发张图很怪）。

    情绪门槛：
      OXY ≥ 75（黏人）或 DA ≥ 60（兴奋）→ 有机会发图
      SER ≥ 70（舒服）或心情词是「开心/撒娇」→ 有机会发语音
    """
    import json as _j
    from pathlib import Path as _P

    state_f = ROOT / "memory" / "self_media_state.json"
    st = {}
    try:
        if state_f.exists():
            st = _j.loads(state_f.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        st = {}
    today = time.strftime("%Y-%m-%d")
    if st.get("date") != today:
        st = {"date": today, "img": 0, "voice": 0}

    def _save():
        try:
            state_f.write_text(_j.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    # 读激素
    oxy = da = ser = 0.0
    try:
        from plugins import hormones as _h
        _v = _h.values()
        oxy = float(_v.get("OXY") or 0)
        da = float(_v.get("DA") or 0)
        ser = float(_v.get("SER") or 0)
    except Exception:  # noqa: BLE001
        pass

    # ── 图：三种来源（2026-11-06 按主人要求调）
    #     目录里的图（R-18 / 表情包）**不限张数** —— 发文件而已，不占显存不花钱
    #     只有现场画才限：默认一天 SELF_DRAW_DAILY_MAX 张（ComfyUI 要占显存）
    _sent = False
    if oxy >= 75 or da >= 60:
        _arousal = _current_arousal()
        # ① 情欲高：R-18 库（不限量，只在私聊）
        if _arousal >= 55 and random.random() < 0.5:
            _p = _pick_r18()
            if _p:
                try:
                    await bot.send_private_msg(user_id=int(YOUR_QQ_ID),
                                               message=MessageSegment.image(str(_p)))
                    _sent = True
                    # 2026-11-06：把「这幅画是什么」写进记忆，主人问起来她才答得上。
                    # 云端识图和本地 QwenVL 都拒 NSFW，走目录名解析（画师·作品·第几张）。
                    # 先让 WD14 打标签（能说出"裸体/在干啥"），拿不到再退回目录名解析
                    _hint18 = ""
                    try:
                        from plugins import wd14_tagger as _wt
                        if _wt.available():
                            _tags18 = await _wt.describe(_p, with_nsfw=True)
                            if _tags18:
                                _hint18 = f"画面：{_tags18}"
                    except Exception as _we:  # noqa: BLE001
                        logger.debug(f"[R-18] WD14 跳过：{type(_we).__name__}")
                    if not _hint18:
                        _hint18 = _r18_hint(_p)
                    await _record_bot_image(f"private_{YOUR_QQ_ID}", "主人", YOUR_QQ_ID,
                                            text, f"从R-18库挑的：{_hint18}"[:90], "", str(_p))
                    logger.info(f"[主动私聊] R-18 库：{_p.name}｜{_hint18}（情欲 {_arousal:.0f}）")
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"[主动私聊] R-18 发送失败：{type(e).__name__}")
        # ② 情绪明显：表情包（不限量）
        if not _sent and random.random() < 0.55:
            _mood_try = str(CURRENT_DAILY_MOOD or "")
            if not _mood_try:
                if ser >= 75: _mood_try = "开心"
                elif oxy >= 85: _mood_try = "撒娇"
                elif da >= 70: _mood_try = "得意"
            _p = _pick_emoji(_mood_try)
            if _p:
                try:
                    await bot.send_private_msg(user_id=int(YOUR_QQ_ID),
                                               message=MessageSegment.image(str(_p)))
                    _sent = True
                    logger.info(f"[主动私聊] 表情包：{_p.name}（{_mood_try or '默认'}）")
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"[主动私聊] 表情包发送失败：{type(e).__name__}")
    # ③ 现场画：这个才限，因为要吃显存
    if not _sent and int(st.get("draw") or 0) < SELF_DRAW_DAILY_MAX and random.random() < 0.4:
        try:
            _scene = await _self_draw_improvise_llm("")
            _img = await _local_zimage(_scene, mode="self", raw_hint=text)
            if _img:
                await bot.send_private_msg(
                    user_id=int(YOUR_QQ_ID),
                    message=MessageSegment.image(f"base64://{base64.b64encode(_img).decode()}"))
                st["draw"] = int(st.get("draw") or 0) + 1
                _save()
                _ip = _save_draw_bytes(_img, "active")
                await _record_bot_image(f"private_{YOUR_QQ_ID}", "主人", YOUR_QQ_ID,
                                        text, f"主动发图：{_scene}"[:80], "", _ip)
                _sent = True
                logger.info(f"[主动私聊] 现场画：{_scene[:30]}（今日第 {st['draw']}/{SELF_DRAW_DAILY_MAX} 张）")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[主动私聊] 画图失败：{type(e).__name__}: {e}")
    if _sent:
        return
    # ── 语音：不限张数，只要心情够。本地 TTS 不占画图显存（互斥已处理）
    if int(st.get("voice") or 0) < 3 and (ser >= 70 or "开心" in str(CURRENT_DAILY_MOOD)
                                          or "撒娇" in str(CURRENT_DAILY_MOOD)) and random.random() < 0.3:
        try:
            _say = str(text)[:60].replace("\n", " ").strip()
            if _say:
                _mp3 = await _local_tts(_say)
                if _mp3:
                    await bot.send_private_msg(
                        user_id=int(YOUR_QQ_ID),
                        message=MessageSegment.record(f"file:///{_mp3}"))
                    st["voice"] = 1
                    _save()
                    logger.info(f"[主动私聊] 附带一句语音：{_say[:30]}")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[主动私聊] 发语音失败：{type(e).__name__}: {e}")


# 主动私聊：偶尔她自己找主人说话（9:00~22:00，间隔≥4h，每天≤2条）
@scheduler.scheduled_job("cron", hour="9-22", minute="*/30")
async def proactive_private_chat():
    st = _load_pp_state()
    if not st.get("enabled"):
        return
    now = time.time()
    today = time.strftime("%Y-%m-%d")
    if st.get("date") != today:
        st["date"] = today
        st["count"] = 0
    # 2026-11-05 满配：2 → 3（配合下面的衰减系数，实际能到 3 次）
    if int(st.get("count", 0)) >= 3:
        return
    if now - float(st.get("last_at") or 0) < 3 * 3600:
        return
    # 2026-11-05（借 digital-life-core 的第四把锁「忙碌拦截」）：刚聊完 60 秒内不插嘴。
    # 她正在回话时主动开一个话题，会把自己的上一句顶掉，看着像精神分裂。
    _last_act = _RUN_TS.get(f"private_{YOUR_QQ_ID}", 0)
    if time.time() - _last_act < 60:
        logger.info("[主动私聊] 60 秒内刚有对话活动，先不插嘴")
        return
    # 2026-11-05（能量体征）：体力 <45 就不自己找人了，累了不想开口
    try:
        from plugins import energy as _ener2
        if not _ener2.allow_proactive():
            logger.info(f"[主动私聊] 体力 {_ener2.value():.0f} < 45，这次不找主人")
            return
    except Exception:  # noqa: BLE001
        pass
    # 2026-11-05：主人最后一句是收尾语 → 今晚不再找（以前完全没这个概念）
    try:
        _rows = await asyncio.to_thread(get_recent_memory, 4, f"private_{YOUR_QQ_ID}")
        if _owner_closed_topic(_rows):
            logger.info("[主动私聊] 主人最后一句是收尾语，今晚不打扰")
            return
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[主动私聊] 收尾检测跳过：{type(e).__name__}: {e}")
    # 想说话的冲动（想念/无聊推高、太累压制）：不是纯掷骰子，能解释"她为什么找我"
    try:
        from desire import urge as _desire_urge
        _u = _desire_urge(_body_tired())
    except Exception:  # noqa: BLE001
        _u = 0.0
    # 2026-11-05：收敛递减。以前只有「2 次/天」的硬上限，用完像被掐断。
    # 现在每找过一次，冲动打 7 折——感觉上变成"越找越难开口"，而不是额度用尽。
    _sent_n = int(st.get("count", 0))
    if _sent_n > 0:
        # 0.7 会让第 3 次永远触发不了（需冲动≥1.12 > 上限 1.0），满配放宽到 0.85
        _u *= (0.85 ** _sent_n)
        logger.info(f"[主动私聊] 今天已找过 {_sent_n} 次，冲动打折到 {_u:.2f}")
    if _u < 0.55 or random.random() > _u:
        _why = ""
        try:
            from desire import urge_detail as _ud
            _d = _ud(_body_tired())
            _why = (f" 亲密缺口{_d['oxy_gap']:.2f} 多巴胺{_d['da']:.2f} "
                    f"压力{_d['cort']:.2f} 血清素{_d['ser']:.2f} 褪黑素{_d['mel']:.0f}")
        except Exception:  # noqa: BLE001
            pass
        logger.info(f"[主动私聊] 冲动不足({_u:.2f})，这次不找主人{_why}")
        return
    # 2026-11-06：她可能自己选了「今天不想说话」，那这次就别开口。
    # 主人一叫她这个状态会自动解除（见聊天入口的 clear_quiet）。
    try:
        from plugins import agency as _agq
        if _agq.is_quiet_today():
            logger.info("[主动私聊] 她今天自己选了「不想说话」，跳过")
            return
    except Exception:  # noqa: BLE001
        pass
    
    # 2026-11-06（「能动性四件套」第③条）：有冲动要开口之前，先让她自己做点什么。
    # 六个只读动作：查余额 / 看天气 / 翻记忆 / 看自己状态 / 刷学习 / 记想法。
    # 做完把那句结果拼进这次开口的语境 —— 这样她说出来的话是有"刚才干了啥"垫底的，
    # 不再是空手对着空气喊一嗓子。动作限流 4 次/天、间隔 90 分钟，超了就不做。
    _agency_ctx = ""
    try:
        from plugins import agency as _agc
        _act_name = _agc.pick_action()
        _agency_ctx = await _agc.run_action(_act_name)
    except Exception as _ae:  # noqa: BLE001
        logger.debug(f"[行动空间] 跳过：{type(_ae).__name__}: {_ae}")

    # 三我审议：有冲动 ≠ 该开口（超我可能否掉：闹别扭/半夜/越线）
    try:
        from thoughts import top as _t_top, review_llm as _t_review
        _th = _t_top() or {}
        _txt = str(_th.get("text") or "")
        _rv = await _t_review(_txt)   # 模型自己当三我判一次，失败内部退回规则
        if _rv.get("decision") == "不做":
            logger.info(f"[主动私聊] 三我否决（{_rv.get('reason')}），这次不开口")
            return
        if _rv.get("decision") == "再说" and random.random() < 0.7:
            logger.info(f"[主动私聊] 三我缓一缓（{_rv.get('reason')}）")
            return
        logger.info(f"[主动私聊] 三我通过：{_rv.get('reason')}｜念头={_txt[:24]!r}")
        # 2026-11-07：用的哪条就标记哪条，进 6 小时冷却，别同一件事连着问
        try:
            from thoughts import mark_asked as _t_mark
            if _th.get("id") is not None:
                _t_mark(int(_th["id"]))
        except Exception as _e:  # noqa: BLE001
            logger.debug(f"[主动私聊] 标记冷却失败：{type(_e).__name__}: {_e}")
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[主动私聊] 审议异常（照常发）：{type(e).__name__}: {e}")

    recent_txt = ""
    try:
        rows = await asyncio.to_thread(get_recent_memory, 5, f"private_{YOUR_QQ_ID}")
        recent_txt = "；".join(
            f"{r.get('speaker') or '主人'}: {(r.get('message') or '')[:40]}"
            for r in (rows or [])[::-1]
        )
    except Exception:
        recent_txt = ""
    hist = "；".join((st.get("history") or [])[-3:])

    _thought_line = ""
    try:
        from thoughts import prompt_line as _thoughts_line
        _thought_line = _thoughts_line()
    except Exception:  # noqa: BLE001
        _thought_line = ""
    prompt = (
        f"现在{time.strftime('%H:%M')}，你的心情是{CURRENT_DAILY_MOOD}。"
        "你突然想主动私聊主人一句，是你自己想找他，不是他叫你。\n"
        + (f"{_thought_line}\n" if _thought_line else "")
        + (f"你刚才自己做了点事：{_agency_ctx}\n" if _agency_ctx else "")
        + f"今天你们聊过：{recent_txt or '还没怎么聊'}\n"
        + f"你上次主动找他说：{hist or '没有'}\n"
        + "写 1~2 句、30~60 字，像真人发微信：可以撒娇、吐槽、分享刚想到的小事；"
        + "不要问“在吗”，不要每次问吃饭，不要说自己是 AI/程序，不要解释指令；只输出正文。"
    )
    try:
        text = await call_ai_raw(prompt, "deepseek", temperature=0.95, max_tokens=300)
    except Exception:
        return
    text = (text or "").strip()
    if len(text) < 6:
        return
    try:
        bot = get_bot()
        await bot.send_private_msg(user_id=int(YOUR_QQ_ID), message=text)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[主动私聊] 发送失败：{e}")
        return
    st["last_at"] = now
    st["count"] = int(st.get("count", 0)) + 1
    st["history"] = (st.get("history") or [])[-19:] + [text]
    _save_pp_state(st)
    # 2026-11-06 修：主动说的话必须也进 _history。
    # 以前只写进 pp_state 的 history，聊天历史里查不到 —— 于是她说了一句、
    # 主人回问，她翻遍对话只看见主人的问题，就答「本鱼没说过啊」。
    # 实测：B站看完视频主动问「会馆有个是啥断了难受」，回头一口咬定没说过。
    try:
        _hg = f"private_{YOUR_QQ_ID}"
        _history[_hg].append((NICKNAME, str(text)[:cfg.context.max_len_per_msg], True))
    except Exception as _he:  # noqa: BLE001
        logger.debug(f"[主动私聊] 写历史失败：{type(_he).__name__}: {_he}")
    logger.info(f"[主动私聊] 已发给主人：{text[:40]!r}")
    # 2026-11-06：挂个等待点 —— 5 分钟没人应，她心里会泛出点什么（只进念头池，不催人）
    try:
        from plugins import agency as _agw
        _agw.note_sent(text)
    except Exception:  # noqa: BLE001
        pass

    # 2026-11-06：说了话之后，偶尔跟一张自己 / 一句自己的声音（都是本地的，不花钱）。
    # 主人说「都是本地的没事可以加」，那就加 —— 但一天各一次，多了变骚扰。
    try:
        await _maybe_send_self_media(bot, text)
    except Exception as _me:  # noqa: BLE001
        logger.debug(f"[主动私聊] 附带媒体跳过：{type(_me).__name__}: {_me}")


# 抖音视频信箱：把待发文件放进 output/douyin/outbox，机器人每分钟自动发主人 QQ
DOUYIN_OUTBOX = DOUYIN_DIR / "outbox"
DOUYIN_SENT = DOUYIN_DIR / "sent"


@scheduler.scheduled_job("interval", minutes=1)
async def douyin_outbox_sender():
    try:
        if not DOUYIN_OUTBOX.is_dir():
            return
        files = []
        for pattern in ("*.mp4", "*.png", "*.jpg", "*.jpeg"):
            files.extend(DOUYIN_OUTBOX.glob(pattern))
        if not files:
            return
        bot = get_bot()
        for path in sorted(files):
            ok = False
            if path.suffix.lower() in (".png", ".jpg", ".jpeg"):
                segs = (MessageSegment.image(str(path)),)
            else:
                segs = (MessageSegment("file", {"file": str(path)}),
                        MessageSegment.video(str(path)))
            for seg in segs:
                try:
                    await bot.send_private_msg(user_id=int(YOUR_QQ_ID), message=seg)
                    ok = True
                    break
                except Exception:
                    continue
            if ok:
                DOUYIN_SENT.mkdir(parents=True, exist_ok=True)
                path.replace(DOUYIN_SENT / path.name)
                logger.info(f"[抖音视频] 已发送并归档：{path.name}")
            else:
                logger.warning(f"[抖音视频] 发送失败，留在信箱：{path.name}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[抖音视频] 信箱发送异常：{e}")


# 群邀请：主人（大号）拉她 → 自动同意；别人邀请 → 私聊问主人
@on_request().handle()
async def _(event: GroupRequestEvent):
    if event.sub_type != "invite":
        return
    bot = get_bot()
    gid = event.group_id
    inviter = str(event.user_id)
    if inviter in _load_invite_trust():
        try:
            await bot.set_group_add_request(flag=event.flag, sub_type="invite", approve=True)
            name = await _group_name(gid)
            gguard.allow(gid, name)
            logger.info(f"[群邀请] {inviter} 拉群 {gid}「{name}」，已自动同意")
            await bot.send_private_msg(
                user_id=int(YOUR_QQ_ID),
                message=f"主人拉我进「{name or gid}」，我自动同意啦。\n\n" + _group_roster_text(),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[群邀请] 自动同意失败：{e}")
        return

    PENDING_INVITES[gid] = event.flag
    name = await _group_name(gid)
    PENDING_ACTION.update({"type": "invite", "gid": gid, "name": name, "inviter": inviter})
    try:
        await bot.send_private_msg(
            user_id=int(YOUR_QQ_ID),
            message=(
                f"主人，有人邀请我进「{name or gid}」（{gid}），要同意嘛？\n"
                "回我「可以」或「不可以」就行。"
            ),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[群邀请] 通知主人失败：{e}")


# 启动时把“今日人设”落盘并写进 pet_state，避免面板显示上一次的旧/随机值
try:
    _save_daily_persona(DAILY_PERSONA)
    write_pet_state()
except Exception:
    pass

# =====================================================================
# 飞书私聊：复用 QQ 私聊的同一套 prompt（人设/状态/档案/召回/记忆写回）
# =====================================================================
async def _feishu_draw(prompt: str, mode: str) -> None:
    """飞书里的画图：后台出图，出完直接把图发到飞书。"""
    try:
        from plugins import feishu_bridge
        # 2026-11-05：和 QQ 对齐。飞书这边原来是原始文本直接进 _draw_chain——
        # ① 场景不扩写（「做爱的场景」五个字原样过去，模型只能瞎猜）
        # ② NSFW 判定拿不到原始指令（扩写后的委婉说法一个词都不命中）
        _raw = str(prompt or "")
        if mode == "self":
            _hint = _self_draw_action(_raw) or ""
            prompt = await _self_draw_improvise_llm(_hint)
        img, src = await _draw_chain(prompt, mode=mode, raw_hint=_raw)
        if not img:
            await asyncio.to_thread(feishu_bridge.notify_owner, "(*甩笔*) 这次没画出来，再喊一次试试。")
            return
        await asyncio.to_thread(feishu_bridge.send_image_bytes, img, "")
        await asyncio.to_thread(feishu_bridge.notify_owner,
                                f"(*擦笔*) 画好了（{src}）。")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[飞书] 画图失败：{type(e).__name__}: {e}")
        try:
            from plugins import feishu_bridge
            await asyncio.to_thread(feishu_bridge.notify_owner, "(*耷拉尾巴*) 画图出错了。")
        except Exception:  # noqa: BLE001
            pass


async def feishu_chat(user_id: str, text: str) -> tuple[str, str]:
    """飞书来的消息走和 QQ 私聊完全相同的链路，返回纯文本回复（失败返回空串）。

    以前飞书走的是一条"人设+一句话"的轻量 prompt，所以同一句话在两边反应不一样。
    这里直接复用 build_prompt_parts / _compose_chat_messages / _web_or_chat，
    并把对话写回同一份 QQ 记忆，做到两边一致。
    """
    uid = str(user_id or "").strip() or str(YOUR_QQ_ID)
    gid = f"private_{uid}"                 # 长期记忆/好感度：两边共用
    hist_gid = f"private_feishu_{uid}"     # 对话历史：飞书自己一份，不和 QQ 串
    text = (text or "").strip()
    if not text:
        return ""

    # 飞书也能画图：后台出图，先把确认回去，画完直接发图
    _draw_prompt = ""
    _draw_mode = ""
    if _draw_prompt_of(text) is not None:
        _draw_prompt = _draw_prompt_of(text) or ""
        _draw_mode = "verbatim"
    elif "画你自己" in text or "画个你" in text:
        _draw_prompt = text
        _draw_mode = "self"
    if _draw_prompt and _draw_mode:
        asyncio.create_task(_feishu_draw(_draw_prompt, _draw_mode))
        return "(*拿起画笔*) 好，本鱼画了，画完直接发你（大概 2~3 分钟）。", ""
    try:
        # 首次使用时从 DB 回填最近历史，否则她会"失忆"
        if not _history.get(hist_gid):
            rows = await asyncio.to_thread(get_recent_memory, 40, hist_gid)
            for r in (rows or []):
                if r.get("message"):
                    _history[hist_gid].append(("主人", str(r["message"])[:cfg.context.max_len_per_msg], False))
                if r.get("reply"):
                    _history[hist_gid].append((NICKNAME, str(r["reply"])[:cfg.context.max_len_per_msg], True))
        affinity = await asyncio.to_thread(get_affinity, uid)
        tag = await asyncio.to_thread(get_tag, uid, gid)
        stable, volatile, _slow = build_prompt_parts(
            gid, uid, is_private=True, affinity=affinity, tag=tag, user_text=text,
            channel="feishu", hist_len=len(_history[gid]),
            hist_tok=_history_tokens(_history[gid]),
        )
        hist = _history_messages(True, list(_history[hist_gid]))
        extra: list = list(_slow)   # F1（t6）：慢块尾巴（PROMPT_SLOW_TAIL）在飞书同样要挂到历史之后
        mi = detect_memory_intent(text, is_private=True)
        if mi:
            mt = await asyncio.to_thread(recall_memory, gid, mi, True, 1500, True)
            if mt:
                extra.append("【回忆】\n" + mt)
        messages = _compose_chat_messages(stable, hist, extra, volatile, text)
        reply, _meta = await _web_or_chat(
            messages, GROUP_AI_STATE[gid], True, text, [], context_key=gid, channel="feishu",
        )
        if not reply or reply.startswith("[AI 调用失败]") or reply.startswith("[谷歌") or reply.startswith("[硅基流动"):
            return "", ""
        # 把 [表情:xx] 摘出来给飞书发表情包用
        mood = ""
        _m = re.search(r"\[表情[:：]\s*([^\]]*)\]", reply)
        if _m:
            mood = re.sub(r"[^\w\u4e00-\u9fa5]", "", _m.group(1).strip())[:8]
        reply = re.sub(r"\[表情[:：]\s*[^\]]*\]", "", reply).strip()
        reply, _nick = _extract_nickname_marker(reply)
        reply = _strip_bot_marker(reply)
        if len(reply) > 300:
            reply = reply[:300] + "..."
        _history[hist_gid].append(("主人", text[:cfg.context.max_len_per_msg], False))
        _history[hist_gid].append((NICKNAME, reply[:cfg.context.max_len_per_msg], True))
        try:
            await asyncio.to_thread(add_memory, "主人", uid, gid, text, reply)
            await asyncio.to_thread(save_daily, gid, "主人", uid, text, reply)
            await ltm.enqueue_pending(gid, "主人", text)
            await ltm.enqueue_pending(gid, NICKNAME, reply)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[飞书] 写回记忆失败：{type(e).__name__}: {e}")
        return reply, mood
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[飞书] 复用 QQ 链路失败，回退轻量回复：{type(e).__name__}: {e}")
        return "", ""
