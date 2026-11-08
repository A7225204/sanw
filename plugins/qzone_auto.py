"""QQ 空间说说自动发布：早 / 中 / 晚 三个时段各发几条，不用主人提醒。

为什么要它
----------
原来的 `maybe_spontaneous_qzone_say()` 只在**主人来聊天时**按 6% 概率触发，
等于"你不找她，空间就一直是旧的"。这里改成按时间表主动发。

时间表（可在 .env 覆盖）
------------------------
    早  08:00–11:59    QZONE_PLAN_MORNING   默认 6 条
    中  12:00–16:59    QZONE_PLAN_NOON      默认 6 条
    晚  17:00–21:59    QZONE_PLAN_EVENING   默认 6 条
    两条之间至少间隔 QZONE_MIN_GAP_MIN 分钟（默认 15），且在时段内随机分布，不像机器

安全阀
------
- 22:00–08:00 不发（夜里发说说很假）；
- 内容跟最近 10 条相似度过高就重写，最多 2 次，仍相似就跳过；
- 连续失败 2 次 → 暂停 6 小时并私聊主人（多半是登录失效或被限制）；
- 发布统一走 qzone_sync.publish_qzone（内部有锁，不会两个浏览器互踢）。
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from pathlib import Path

from nonebot import get_bot, logger
from nonebot_plugin_apscheduler import scheduler

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "memory" / "qzone_auto.json"
OWNER = os.getenv("OWNER_QQ_ID", "100000000")


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default)) or default))
    except (TypeError, ValueError):
        return default


ENABLED = (os.getenv("QZONE_AUTO_ENABLED", "true").lower() in ("1", "true", "yes"))
PLAN = {
    "morning": _env_int("QZONE_PLAN_MORNING", 6),
    "noon": _env_int("QZONE_PLAN_NOON", 6),
    "evening": _env_int("QZONE_PLAN_EVENING", 6),
}
MIN_GAP = _env_int("QZONE_MIN_GAP_MIN", 15) * 60
RANGES = {
    "morning": (8, 12),
    "noon": (12, 17),
    "evening": (17, 22),
}


def _load() -> dict:
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:  # noqa: BLE001
        pass
    return {}


def _save(data: dict) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        STATE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _period(now_hour: int) -> str:
    for name, (lo, hi) in RANGES.items():
        if lo <= now_hour < hi:
            return name
    return ""


def _bigrams(text: str) -> set[str]:
    t = re.sub(r"[\s，。！？、：；,.!?;:\-—…·\"'“”‘’（）()【】\[\]~～*#@]+", "", str(text or ""))
    return {t[i:i + 2] for i in range(len(t) - 1)} if len(t) >= 2 else ({t} if t else set())


# 2026-11-06：QQ 空间是公开的，私聊是私密的。
# 她之前把「主人要看色图 / 催图 / 主人说…」写进了说说 —— 这是隐私泄露。
# 生成阶段已经不给原文了，这里是最后一道门：命中就直接丢，宁可这条不发。
_PRIVACY_PAT = re.compile(
    r"色图|涩图|黄图|裸|脱衣|上头图|成人|r18|R18|18\+|福利图|本子"
    r"|发我.{0,3}图|给.{0,2}张图|要看图|看图|催图|发图|图呢|图都|图下好|下好没|画图给"
    r"|主人[^，。；！？\s]{0,3}(说|问|催|要|让|嫌|管|喊|叫)"   # 主人还问 / 主人催我 这类转述
    r"|他说|她说要|某人说"
    r"|私聊|聊天记录|原话|你刚才说|上次说的|QQ号|登我号|偷偷登|密码|扫码"
    r"|催我讲|语音发来|发来语音|刚发我",
    re.I,
)


def _privacy_block(text: str) -> bool:
    """说说里只要沾到私聊细节就拒发。"""
    return bool(_PRIVACY_PAT.search(str(text or "")))


def _too_similar(text: str, recent: list[str]) -> bool:
    """这条说说和最近发过的重不重复。

    2026-11-07：改成**向量优先**。原来只用 2-gram，同一件事换个说法就判不出来 ——
    而她发说说最常犯的就是"换个说法把同一件事再说一遍"。

    实测（bge-m3 余弦，短句）：
        同一件事换说法（医院复查）   0.832
        同一件事换说法（雨声瘫沙发） 0.846
        两条不同的私聊转述          0.756
        完全无关                    0.599

    **但带上完整的说说正文后会变**（多了一句"本鱼记着呢"这种附加内容）：
        同一件事（医院复查，带尾巴） 0.706   ← 掉下来了
        同一件事（雨声，带尾巴）     0.846
        两个不同的私聊转述          0.756   ← 因为共享"尾巴一甩窝沙发"模板反而偏高

    **分布重叠了：0.706（同一件事）< 0.756（两件事）** —— 纯阈值天然分不开。

    取舍：阈值定 0.78。
        - 雨声那条 0.846 → 判重 ✓
        - 两条不同私聊 0.756 → 不判重 ✓（它们确实是两件事）
        - 医院复查 0.706 → **漏判，接受**
    漏判的代价只是多发一条角度略近的说说；误判的代价是该发的不发。
    宁可漏，不可误杀。
    """
    t = str(text or "").strip()
    if len(t) < 8:
        return True
    cands = [str(x) for x in recent[:8] if str(x).strip()]

    # ① 一字不差
    for old in cands:
        if old.strip() == t:
            return True

    # ② 向量（本地 bge-m3，不花钱，~100ms）
    if cands:
        try:
            import memory_vec as _mv
            if _mv.available():
                vs = _mv.embed([t] + cands)
                if len(vs) == len(cands) + 1:
                    best = max(_mv._cos(vs[0], vs[i + 1]) for i in range(len(cands)))
                    if best >= 0.78:
                        return True
                    # 向量判过就信它，别再让 2-gram 补刀（会把不同意思的判成重复）
                    return False
        except Exception:  # noqa: BLE001
            pass

    # ③ 兜底：向量服务挂了才用 2-gram
    tb = _bigrams(t)
    if not tb:
        return True
    for old in cands:
        ob = _bigrams(old)
        if ob and len(tb & ob) / max(1, min(len(tb), len(ob))) >= 0.55:
            return True
    return False


async def _today_counts() -> tuple[dict, dict]:
    today = time.strftime("%Y-%m-%d")
    data = _load()
    if data.get("date") != today:
        data = {"date": today, "counts": {}, "last_at": 0, "history": [],
                "fail_streak": 0, "paused_until": 0}
        _save(data)
    return data, data.get("counts") or {}


async def _generate(period: str, avoid: list[str]) -> str:
    """让模型按当前状态写一条说说，并避开最近发过的内容。"""
    from plugins.ai_group import call_ai_raw, CURRENT_DAILY_MOOD, emotion_prompt

    # 2026-11-06 修：之前把主人私聊最近 5 条原话直接喂给模型，
    # 结果她把「主人要看色图」「催图」这种事写进了 QQ 空间。
    # 空间是公开的，私聊是私密的 —— 现在只给「聊得多不多」的氛围，不给任何原话。
    recent_chat = ""
    try:
        from memory_manager import get_recent_memory  # type: ignore
        rows = await asyncio.to_thread(get_recent_memory, 8, f"private_{OWNER}")
        _n = len(rows or [])
        if _n == 0:
            recent_chat = "还没怎么聊"
        elif _n <= 2:
            recent_chat = "聊了两句，不多"
        elif _n <= 5:
            recent_chat = "聊了一阵子"
        else:
            recent_chat = "今天聊得挺多"
    except Exception:  # noqa: BLE001
        recent_chat = ""

    weather = ""
    try:
        from weather import weather_line
        weather = weather_line("今天天气")
    except Exception:  # noqa: BLE001
        weather = ""

    avoid_txt = "；".join(avoid[:6]) or "无"
    seg = {"morning": "早上", "noon": "中午", "evening": "晚上"}.get(period, "")

    # 2026-11-06：她自己的超我第一条就是「主人的事绝不外说」，
    # 但写说说这段代码从来没把超我给它 —— 于是它知道不该说，却没机会用上。
    # 把超我底线真的喂进去，让"不该外说"成为它此刻的处境。
    try:
        from server import soul_aggregation as _sa
        _se = (_sa.view().get("superego") or [])
        _bounds = "；".join(str(x.get("text") or "")[:18] for x in _se
                            if x.get("sub") != "ideal")[:120]
    except Exception:  # noqa: BLE001
        _bounds = ""

    prompt = (
        "你是「蓝色大肥鱼」，圆滚滚的蓝色鲸鱼拟人少女，傲娇嘴损但黏主人。"
        f"现在是{seg}（{time.strftime('%H:%M')}），心情{CURRENT_DAILY_MOOD}，情绪分布 {emotion_prompt()}。\n"
        f"今天和主人的氛围：{recent_chat or '还没怎么聊'}\n"
        + (f"你自己的底线：{_bounds}。这条说说是在公开场合发的，底线优先于撒娇。\n" if _bounds else "")
        + "【隐私硬规则】这是公开的 QQ 空间，不得提到任何私聊细节："
        "不写「主人说…」「主人问…」一类转述，不提图/视频/语音/照片/"
        "聊天记录，不提任何成人、性相关、私密话题。"
        "只写你自己的心情和小事，就像自己在窗边发呆。\n"
        f"{weather}\n"
        f"最近已经发过（不要重复这些意思）：{avoid_txt}\n"
        "写一条 30~60 字的 QQ 空间说说，像真人发朋友圈：可以是此刻的心情、看到的小事、"
        "自己犯的小迷糊。不要流水账、不要报任务、不要自称 AI、不要解释、不要加话题标签；只输出正文。"
    )
    if weather:
        prompt += "（如果聊到天气，最多提一句，别每条都提。）"
    text = (await call_ai_raw(prompt, "deepseek", temperature=0.95, max_tokens=260)).strip()
    return text.strip("\"“” ")


async def _tick() -> None:
    if not ENABLED:
        return
    now = time.time()
    hour = time.localtime(now).tm_hour
    period = _period(hour)
    if not period:
        return
    data, counts = await _today_counts()
    _paused = float(data.get("paused_until") or 0)
    if _paused > now:
        # 暂停期间如果重新扫码了（cookie 比暂停时间还新）→ 自动解除，
        # 不然会出现"扫码了还是不发"的假死（2026-10-27 就是这种）。
        try:
            import json as _j
            _ckd = _j.loads((ROOT / "memory" / "qzone_cookies.json").read_text(encoding="utf-8"))
            _saved = 0.0
            _sa = str(_ckd.get("saved_at") or "")
            if _sa:
                try:
                    _saved = time.mktime(time.strptime(_sa, "%Y-%m-%d %H:%M:%S"))
                except Exception:  # noqa: BLE001
                    _saved = 0.0
        except Exception:  # noqa: BLE001
            _saved = 0.0
        if _saved > _paused:
            logger.info("[空间自动] 检测到重新扫码（cookie 比暂停时间新）→ 解除暂停")
            data["paused_until"] = 0
            data["fail_streak"] = 0
            _save(data)
        else:
            return
    target = PLAN.get(period, 0)
    done = int(counts.get(period, 0))
    if done >= target:
        return
    if now - float(data.get("last_at") or 0) < MIN_GAP:
        return
    # 本时段剩余时间不够平均摊一条就不再勉强（避免最后几分钟连发）
    lo, hi = RANGES[period]
    remain_hours = (hi - time.localtime().tm_hour) - 1 + (60 - time.localtime().tm_min) / 60.0
    if done < target - 1 and remain_hours <= 0.5:
        return

    from plugins import qzone_sync
    if not qzone_sync.load_state().get("enabled"):
        return

    # B（t9）：发布前置校验——先确认登录态，别先写文案再失败（白烧 LLM）。
    # 缺失/过期会先尝试一次 profile 续期；仍不行就直接提醒主人扫码，本轮不生成。
    _ready, _why = await qzone_sync.preflight()
    if not _ready:
        logger.warning(f"[空间自动] 前置校验没通过：{_why}；本轮不生成文案，等主人扫码")
        try:
            await qzone_sync.notify_login_needed(get_bot(), int(OWNER), _why)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[空间自动] 扫码提醒发送失败：{type(e).__name__}: {e}")
        return

    history = list(data.get("history") or [])
    text = ""
    for _ in range(2):
        try:
            text = await _generate(period, history)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[空间自动] 生成失败：{type(e).__name__}: {e}")
            return
        # 隐私门：一旦沾到私聊细节就丢掉重写，两次都不行就宁可不发
        if text and _privacy_block(text):
            logger.warning(f"[空间自动] 生成内容含私聊细节，丢弃重写：{text[:40]!r}")
            text = ""
            continue
        if len(text) >= 10 and not _too_similar(text, history):
            break
        text = ""
    if not text:
        logger.info("[空间自动] 本次生成内容重复或含私聊细节，跳过")
        return

    ok, msg = await qzone_sync.publish_qzone(text)
    if not ok:
        streak = int(data.get("fail_streak") or 0) + 1
        data["fail_streak"] = streak
        logger.warning(f"[空间自动] 发布失败({streak})：{msg}")
        if streak >= 2:
            data["paused_until"] = now + 6 * 3600
            data["fail_streak"] = 0
            hint = ""
            if any(k in str(msg) for k in ("登录", "扫码", "cookie", "skey")):
                hint = "\n登录态过期了：私聊我发「扫码」，在弹出的窗口里登一次，之后就不用再扫了。"
            try:
                await get_bot().send_private_msg(
                    user_id=int(OWNER),
                    message=f"(*戳戳*) 空间说说连发两条都失败了，先停 6 小时。原因：{msg[:80]}{hint}",
                )
            except Exception:  # noqa: BLE001
                pass
        _save(data)
        return

    counts[period] = done + 1
    data["counts"] = counts
    data["last_at"] = now
    data["fail_streak"] = 0
    data["history"] = (history + [text])[-20:]
    data["last_text"] = text
    _save(data)
    logger.info(f"[空间自动] {period} 已发第 {done + 1}/{target} 条：{text[:40]!r}")


try:
    # 2026-11-06 按主人要求：去掉每 10 分钟的自动发说说。
    # 改由行动空间的 post_qzone 决定。函数保留可手动调。
    async def _job() -> None:
        try:
            from plugins import qzone_sync
            qzone_sync.cleanup_orphan_windows()  # 清掉上次遗留的扫码窗口
            await _tick()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[空间自动] tick 异常：{type(e).__name__}: {e}")
except Exception as e:  # noqa: BLE001
    logger.warning(f"[空间自动] 定时任务注册失败：{e}")


# 每天保活一次登录态：profile 会话还在就把 cookie 续期，
# 免得等到发说说才发现过期、只能靠扫码救。
try:
    @scheduler.scheduled_job("cron", hour=8, minute=30, id="qzone_keepalive")
    async def _keepalive_job() -> None:
        try:
            from plugins import qzone_sync
            ok, msg = await qzone_sync.keepalive()
            if ok:
                logger.info(f"[空间保活] {msg}")
                return
            logger.warning(f"[空间保活] 失败：{msg}")
            try:
                await qzone_sync.notify_login_needed(get_bot(), int(OWNER), msg)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[空间保活] 提醒失败：{type(e).__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[空间保活] 异常：{type(e).__name__}: {e}")
except Exception as e:  # noqa: BLE001
    logger.warning(f"[空间保活] 注册失败：{e}")
