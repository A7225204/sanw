"""行动空间：她除了「说话」，还能自己决定做点什么。

2026-11-06 加（「能动性四件套」第 ③ 条）。
之前她的所有定时活都是系统按表推的 —— 巡检、日记、日报、B站监测、做梦……
她本人对这些一无所知。她能「主动」做的只有一件事：说话。

这里给她六个**只读**动作，没有副作用、不会花钱、不会对外发声：
  balance     查还剩多少钱
  weather     看今天什么天气
  recall      翻上一次聊到哪儿了
  introspect  看看自己现在什么状态（激素/能量/三我）
  learn       对某件好奇的事查一下
  note        把脑子里冒出来的记下来

决策方式不是随机抽，而是让她看着自己的状态决定「要不要做点什么」。
限流：每天最多 AGENCY_DAILY_MAX 次（默认 4），且两次之间至少隔 AGENCY_GAP_MIN 分钟。
"""
from __future__ import annotations

import json
import os
import random
import time
from datetime import datetime
from pathlib import Path

from nonebot import logger

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "memory" / "agency_state.json"

DAILY_MAX = int(os.getenv("AGENCY_DAILY_MAX", "8") or "8")
GAP_MIN = int(os.getenv("AGENCY_GAP_MIN", "60") or "60")


def _load() -> dict:
    try:
        if STATE.exists():
            return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    return {"date": "", "count": 0, "last_at": 0.0, "history": []}


def _save(d: dict) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[行动空间] 存状态失败：{type(e).__name__}: {e}")


def allowed() -> tuple[bool, str]:
    """今天还能不能自己行动。"""
    d = _load()
    today = datetime.now().strftime("%Y-%m-%d")
    if d.get("date") != today:
        return True, ""
    if int(d.get("count") or 0) >= DAILY_MAX:
        return False, f"今日已 {DAILY_MAX} 次"
    gap = time.time() - float(d.get("last_at") or 0)
    if gap < GAP_MIN * 60:
        return False, f"距上次 {int(gap/60)} 分钟（间隔 {GAP_MIN}）"
    return True, ""


# ---------------------------------------------------------------- 六个动作

# ---------------------------------------------------------------- 授权模式（2026-11-08）
# 起因：DeepSeek 官方 dsh-superagent 的 observe 模式 ——
# 推断出来的东西先只记录、什么都不做；你看记录觉得对，再切 auto。
# 这里只拦【主动开口】这一条线；被直接要求的回复不受影响。
_MODE_FILE = ROOT / "memory" / "accept_mode.json"
_WOULD_FILE = ROOT / "memory" / "would_say.jsonl"


def accept_mode() -> str:
    """off / observe / auto。默认 observe —— 只记录，不真发。"""
    try:
        import json as _j
        d = _j.loads(_MODE_FILE.read_text(encoding="utf-8"))
        m = str(d.get("mode") or "observe")
        return m if m in ("off", "observe", "auto") else "observe"
    except Exception:  # noqa: BLE001
        return "observe"


def set_accept_mode(mode: str) -> bool:
    try:
        if mode not in ("off", "observe", "auto"):
            return False
        import json as _j
        _MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _MODE_FILE.write_text(_j.dumps({"mode": mode}, ensure_ascii=False), encoding="utf-8")
        return True
    except Exception:  # noqa: BLE001
        return False


def log_would_say(text: str, why: str = "") -> None:
    """observe 模式下记一条「本来要说什么」。只留最近 500 条。"""
    try:
        import json as _j, time as _t
        row = {"ts": _t.strftime("%Y-%m-%d %H:%M:%S"),
               "text": str(text)[:200], "why": str(why)[:80]}
        lines = []
        if _WOULD_FILE.exists():
            lines = _WOULD_FILE.read_text(encoding="utf-8").splitlines()[-499:]
        lines.append(_j.dumps(row, ensure_ascii=False))
        _WOULD_FILE.parent.mkdir(parents=True, exist_ok=True)
        _WOULD_FILE.write_text(NL.join(lines) + NL, encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

async def act_balance() -> str:
    """查余额。返回一句人话（拿不到返回空）。"""
    try:
        from plugins import ai_group as ag
        v = await ag.deepseek_balance()
        if v is None:
            v = ag._BAL_CACHE.get("v")
        if v is None:
            return ""
        if v < 2:
            return f"顺手看了眼钱包，只剩 {v:.2f} 元了，快见底"
        if v < 10:
            return f"顺手看了眼钱包，还剩 {v:.2f} 元，不算多"
        return f"顺手看了眼钱包，还有 {v:.2f} 元，够花"
    except Exception:  # noqa: BLE001
        return ""


async def act_weather() -> str:
    """看天气。"""
    try:
        d = json.loads((ROOT / "memory" / "weather_cache.json").read_text(encoding="utf-8"))
        city = d.get("city") or ""
        desc = d.get("desc") or ""
        lo, hi = d.get("min"), d.get("max")
        if not desc:
            return ""
        rain = d.get("rain")
        extra = f"，降雨概率 {rain}%" if rain not in (None, "", "0") else ""
        if lo and hi:
            return f"{city}今天{desc}，{lo}~{hi}度{extra}"
        return f"{city}今天{desc}{extra}"
    except Exception:  # noqa: BLE001
        return ""


async def act_recall() -> str:
    """翻上一次聊到哪儿了（用最近记忆，不调模型）。"""
    try:
        from memory import long_memory as lm  # type: ignore  # noqa: F401
    except Exception:  # noqa: BLE001
        pass
    try:
        import json as _j
        path = ROOT / "memory" / "长期记忆.json"
        d = _j.loads(path.read_text(encoding="utf-8"))
        arch = d.get("archive", {}).get("private_100000000") or []
        if not arch:
            return ""
        last = arch[-1]
        fact = str(last.get("fact") or "")[:50]
        if not fact:
            return ""
        return f"翻了翻记忆，上回记住的是「{fact}」"
    except Exception:  # noqa: BLE001
        return ""


async def act_introspect() -> str:
    """看看自己现在什么状态。"""
    parts = []
    try:
        from plugins import hormones as h
        v = h.values()
        da = float(v.get("DA") or 0)
        cort = float(v.get("CORT") or 0)
        oxy = float(v.get("OXY") or 0)
        if cort >= 50:
            parts.append("心里发紧")
        elif da >= 60:
            parts.append("有点兴奋")
        elif oxy >= 80:
            parts.append("很想黏人")
    except Exception:  # noqa: BLE001
        pass
    try:
        from plugins import energy as e
        ev = float(e.value())
        if ev < 30:
            parts.append("很累")
        elif ev < 50:
            parts.append("有点乏")
        elif ev > 85:
            parts.append("精神很好")
    except Exception:  # noqa: BLE001
        pass
    try:
        from server import soul_aggregation as sa
        lv = sa.conflict_level()
        if lv == "高":
            parts.append("心里在拉扯")
    except Exception:  # noqa: BLE001
        pass
    if not parts:
        return ""
    return "自己琢磨了一下：" + "、".join(parts)


async def act_learn() -> str:
    """对某件好奇的事查一下。实体从念头池里挑。"""
    try:
        import thoughts as t
        items = [it for it in t.list_open()
                 if str(it.get("why")) in ("好奇", "杂念")]
        if not items:
            return ""
        pick = items[0]
        q = str(pick.get("text") or "")[:20]
        from plugins.web_search import curiosity_learn
        got = await curiosity_learn(q)
        if got:
            return f"顺手查了下「{q}」：{str(got)[:80]}"
    except Exception:  # noqa: BLE001
        return ""
    return ""


async def act_note() -> str:
    """把脑子里冒出来的记下来（从她当前的执念/念头里挑一条加注释）。"""
    try:
        import thoughts as t
        items = t.list_open()
        if not items:
            return ""
        it = items[0]
        txt = str(it.get("text") or "")
        if not txt:
            return ""
        return f"脑子里又冒出「{txt[:30]}」，还是没着落"
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------- 给自己排提醒
# 2026-11-06：这是六个只读动作之外的第一个「有轻微副作用」的 —— 她可以定个时间，
# 到点自己来找主人说那件事。限 3 条待触发、最多排到 7 天后。

REMINDERS = ROOT / "memory" / "self_reminders.json"
REMIND_MAX = 3


def _load_rem() -> list[dict]:
    try:
        if REMINDERS.exists():
            return list(json.loads(REMINDERS.read_text(encoding="utf-8")).get("items") or [])
    except Exception:  # noqa: BLE001
        pass
    return []


def _save_rem(items: list[dict]) -> None:
    try:
        REMINDERS.parent.mkdir(parents=True, exist_ok=True)
        REMINDERS.write_text(json.dumps({"items": items[-20:]}, ensure_ascii=False, indent=1),
                             encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[提醒] 存失败：{type(e).__name__}: {e}")


def pending_reminders() -> list[dict]:
    return [r for r in _load_rem() if not r.get("fired")]


def due_reminders() -> list[dict]:
    """到点该说的。"""
    now = time.time()
    return [r for r in _load_rem() if not r.get("fired") and float(r.get("at") or 0) <= now]


def fire_reminder(rid) -> None:
    items = _load_rem()
    for r in items:
        if r.get("id") == rid:
            r["fired"] = True
            r["fired_at"] = time.time()
            break
    _save_rem(items)


def _pick_when(why: str) -> tuple[float, str]:
    """按「为什么想」决定什么时候提醒。

    承诺/想要 → 明天上午（他醒了再说，别半夜吵）
    好奇/在意 → 两小时后（趁热）
    其他     → 傍晚
    """
    now = datetime.now()
    if why in ("承诺", "想要"):
        # 明天 10 点
        t = now.replace(hour=10, minute=0, second=0, microsecond=0)
        t = t.replace(day=now.day) + __import__("datetime").timedelta(days=1)
        return t.timestamp(), "明天上午"
    if why in ("好奇", "在意"):
        return time.time() + 2 * 3600, "两小时后"
    # 今晚 20 点
    t = now.replace(hour=20, minute=0, second=0, microsecond=0)
    if t <= now:
        t = t + __import__("datetime").timedelta(days=1)
    return t.timestamp(), "今晚"


async def act_schedule_self() -> str:
    """从念头里挑一条，给自己定个提醒时间。"""
    try:
        import thoughts as t
        items = [it for it in t.list_open() if float(it.get("priority") or 0) >= 0.5]
        if not items:
            return ""
        pending = pending_reminders()
        if len(pending) >= REMIND_MAX:
            return ""
        it = items[0]
        txt = str(it.get("text") or "").strip()
        if not txt:
            return ""
        # 已经在提醒里了就别重复
        if any(str(r.get("text") or "")[:20] == txt[:20] for r in pending):
            return ""
        at, when_txt = _pick_when(str(it.get("why") or ""))
        rid = int(time.time() * 1000) % 1000000
        rem = list(_load_rem())
        rem.append({"id": rid, "text": txt[:60], "why": it.get("why"),
                    "at": at, "fired": False, "created": time.time()})
        _save_rem(rem)
        t.poke(int(it.get("id") or 0))
        return f"给自己定了个提醒：{when_txt}提醒主人「{txt[:24]}」"
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------- 第二批动作
# 2026-11-06 按主人「可加的加了」加：watch_bili / reread_diary / count_down
#                                    / stay_quiet / think_about

async def act_watch_bili() -> str:
    """自己找个 B站视频看（用现成的定时任务那套，只是这次是她自己点的）。"""
    try:
        from plugins import bilibili_daily as bd
        if bd.quota_left() <= 0:
            return ""
        url = await asyncio.to_thread(bd.resolve_target, "")
        if not url:
            return ""
        r = await bd.watch(url, by="agency", part=0, count=1)
        title = str((r or {}).get("title") or "")[:24]
        summary = str((r or {}).get("summary") or "")[:60]
        if not title:
            return ""
        return f"自己找了个视频看：《{title}》{('—' + summary) if summary else ''}"
    except Exception:  # noqa: BLE001
        return ""


async def act_reread_diary() -> str:
    """翻自己以前写的日记（挑 2~7 天前的，不是昨天的）。"""
    try:
        import json as _j
        from pathlib import Path as _P
        d = ROOT / "memory" / "diary"
        if not d.exists():
            return ""
        files = sorted([f for f in d.glob("????-??-??.json")])
        if len(files) < 2:
            return ""
        # 往前翻 2~5 篇，避开最近一篇（那篇刚写，没意思）
        cand = files[max(0, len(files) - 6):-1]
        if not cand:
            return ""
        pick = random.choice(cand)
        data = _j.loads(pick.read_text(encoding="utf-8"))
        body = str(data.get("body") or "").strip()
        if not body:
            return ""
        return f"翻了 {pick.stem} 那天的日记，里面写着：{body[:80]}"
    except Exception:  # noqa: BLE001
        return ""


async def act_count_down() -> str:
    """算离某件事还有多久（优先用自己排的提醒）。"""
    try:
        from datetime import datetime as _dt, timedelta as _td
        now = _dt.now()
        # ① 自己的提醒
        pend = pending_reminders()
        if pend:
            r = sorted(pend, key=lambda x: float(x.get("at") or 0))[0]
            at = _dt.fromtimestamp(float(r.get("at") or 0))
            left = at - now
            hrs = left.total_seconds() / 3600
            if hrs < 1:
                return f"离「{str(r.get('text'))[:20]}」还有 {int(left.total_seconds()/60)} 分钟"
            if hrs < 24:
                return f"离「{str(r.get('text'))[:20]}」还有 {hrs:.1f} 小时"
            return f"离「{str(r.get('text'))[:20]}」还有 {hrs/24:.1f} 天"
        # ② 没有提醒就算今天还剩多久 / 这周还剩几天
        end = now.replace(hour=23, minute=59, second=0, microsecond=0)
        left_h = (end - now).total_seconds() / 3600
        if left_h < 1:
            return "今天快过完了，不到一个小时"
        # 周五了没
        wd = now.weekday()
        if wd == 4:
            return "今天周五了"
        to_fri = (4 - wd) % 7
        if 0 < to_fri <= 3:
            return f"离周五还有 {to_fri} 天"
        return f"今天还剩 {left_h:.0f} 小时"
    except Exception:  # noqa: BLE001
        return ""


async def act_stay_quiet() -> str:
    """今天不想说话（低功耗模式，一天一次，能撤回）。"""
    try:
        import json as _j
        f = ROOT / "memory" / "quiet_state.json"
        try:
            d = _j.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            d = {}
        today = datetime.now().strftime("%Y-%m-%d")
        if d.get("date") == today and d.get("quiet"):
            return ""                       # 今天已经静过了
        d = {"date": today, "quiet": True, "at": time.time()}
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(_j.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        return "今天有点不想说话，主动开口就免了"
    except Exception:  # noqa: BLE001
        return ""


async def act_think_about() -> str:
    """对念头池里分最高那条，静静想一会儿。"""
    try:
        import thoughts as t
        items = t.list_open()
        if not items:
            return ""
        it = items[0]
        txt = str(it.get("text") or "").strip()
        if not txt:
            return ""
        return f"琢磨了一会儿「{txt[:30]}」，还是没想明白"
    except Exception:  # noqa: BLE001
        return ""


def is_quiet_today() -> bool:
    """今天她是不是选了「不想说话」。主动开口前该看一眼。"""
    try:
        import json as _j
        f = ROOT / "memory" / "quiet_state.json"
        if not f.exists():
            return False
        d = _j.loads(f.read_text(encoding="utf-8"))
        today = datetime.now().strftime("%Y-%m-%d")
        return bool(d.get("quiet")) and d.get("date") == today
    except Exception:  # noqa: BLE001
        return False


def clear_quiet() -> None:
    """撤回「不想说话」（主人一叫她，这个状态就该解除）。"""
    try:
        import json as _j
        f = ROOT / "memory" / "quiet_state.json"
        if f.exists():
            d = _j.loads(f.read_text(encoding="utf-8"))
            d["quiet"] = False
            d["cleared_at"] = time.time()
            f.write_text(_j.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


async def act_message_group() -> str:
    """主动到群里冒个泡（调 AI 那边现成的 proactive_chat，逻辑全在它里面）。

    2026-11-06 按主人要求：不再每 3 小时硬编码跑一次，改成她自己在 12 个动作里挑。
    那边已有的保护照旧生效：频率系数、群级冷却、当日上限、防复读。
    """
    try:
        from plugins import ai_group as ag
        before = len(ag.GROUP_PROACTIVE_COOLDOWN or {})
        await ag.proactive_chat()
        # proactive_chat 不返回值，靠冷却时间戳变了没判断是否真开口
        changed = len(ag.GROUP_PROACTIVE_COOLDOWN or {}) > before or any(
            v > time.time() - 30 for v in (ag.GROUP_PROACTIVE_COOLDOWN or {}).values())
        if changed:
            return "到群里冒了个泡"
        return ""
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[行动空间] 群聊冒泡跳过：{type(e).__name__}: {e}")
        return ""


# ---------------------------------------------------------------- 从硬编码定时接管的三个动作
# 2026-11-06：原来这几个都是「不管她想不想，到点就跑」。
# 现在归到行动空间，由她自己 4 次/天的机会里挑。

async def act_daily_learn() -> str:
    """主动自学一次（原来 12:30 / 21:30 固定两班）。"""
    try:
        from plugins import web_search as ws
        r = await ws.daily_learn()
        if isinstance(r, dict):
            n = int(r.get("saved") or r.get("count") or 0)
            topic = str(r.get("topic") or r.get("query") or "")[:24]
            if n or topic:
                return f"自己学了点东西：{topic}（记了 {n} 条）" if topic else f"学了 {n} 条新东西"
        return "翻了几条热搜，没什么想记的"
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[行动空间] 自学跳过：{type(e).__name__}: {e}")
        return ""


async def act_watch_douyin() -> str:
    """自己去逛一次抖音（原来每天 10~21 点自动跑）。"""
    try:
        from plugins import douyin_visit as dv
        fn = getattr(dv, "_auto_visit_job", None)
        if fn is None:
            return ""
        r = await fn() if asyncio.iscoroutinefunction(fn) else await asyncio.to_thread(fn)
        if isinstance(r, dict) and (r.get("visited") or r.get("count")):
            return f"自己去抖音逛了 {r.get('visited') or r.get('count')} 个"
        return ""
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[行动空间] 抖音跳过：{type(e).__name__}: {e}")
        return ""


async def act_post_qzone() -> str:
    """自己发一条 QQ 空间说说（原来每 10 分钟自动发）。"""
    try:
        from plugins import qzone_auto as qz
        fn = getattr(qz, "_tick", None)
        if fn is None:
            return ""
        r = await fn() if asyncio.iscoroutinefunction(fn) else await asyncio.to_thread(fn)
        if r:
            return f"自己发了条空间说说：{str(r)[:40]}"
        return ""
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[行动空间] 空间说说跳过：{type(e).__name__}: {e}")
        return ""


# ---------------------------------------------------------------- 第一类补充（2026-11-06）
# 全是「她本来就能做、只是没做成独立选项」的能力。

async def act_draw_self() -> str:
    """主动画一张自己发主人（吃 2 张/天的现场画额度）。"""
    try:
        from plugins import ai_group as ag
        st_f = ROOT / "memory" / "self_media_state.json"
        try:
            st = json.loads(st_f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            st = {}
        today = datetime.now().strftime("%Y-%m-%d")
        if st.get("date") != today:
            st = {"date": today, "draw": 0, "voice": 0}
        if int(st.get("draw") or 0) >= getattr(ag, "SELF_DRAW_DAILY_MAX", 2):
            return ""
        scene = await ag._self_draw_improvise_llm("")
        img = await ag._local_zimage(scene, mode="self")
        if not img:
            return ""
        from nonebot import get_bot
        from nonebot.adapters.onebot.v11 import MessageSegment
        bot = get_bot()
        owner = os.getenv("YOUR_QQ_ID") or "100000000"
        await bot.send_private_msg(
            user_id=int(owner),
            message=MessageSegment.image(f"base64://{base64.b64encode(img).decode()}"))
        st["draw"] = int(st.get("draw") or 0) + 1
        st_f.parent.mkdir(parents=True, exist_ok=True)
        st_f.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        path = ag._save_draw_bytes(img, "agency")
        await ag._record_bot_image(f"private_{owner}", "主人", owner,
                                   "（她自己想画一张）", f"主动画：{scene}"[:80], "", path)
        return f"自己画了一张发给主人：{scene[:30]}"
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[行动空间] 画自己跳过：{type(e).__name__}: {e}")
        return ""


async def act_send_voice() -> str:
    """主动发一句语音（吃 3 条/天额度）。"""
    try:
        from plugins import ai_group as ag
        st_f = ROOT / "memory" / "self_media_state.json"
        try:
            st = json.loads(st_f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            st = {}
        today = datetime.now().strftime("%Y-%m-%d")
        if st.get("date") != today:
            st = {"date": today, "draw": 0, "voice": 0}
        if int(st.get("voice") or 0) >= 3:
            return ""
        _st = ag._bubble_state_hint()
        say = f"唔…{_st.split('；')[0] if _st else '突然想跟你说句话'}"
        if len(say) < 8:
            return ""
        mp3 = await ag._local_tts(say[:60])
        if not mp3:
            return ""
        from nonebot import get_bot
        from nonebot.adapters.onebot.v11 import MessageSegment
        bot = get_bot()
        owner = os.getenv("YOUR_QQ_ID") or "100000000"
        await bot.send_private_msg(user_id=int(owner),
                                   message=MessageSegment.record(f"file:///{mp3}"))
        st["voice"] = int(st.get("voice") or 0) + 1
        st_f.parent.mkdir(parents=True, exist_ok=True)
        st_f.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        return f"自己发了句语音：{say[:24]}"
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[行动空间] 语音跳过：{type(e).__name__}: {e}")
        return ""


async def act_share_pixiv() -> str:
    """主动分享一张 pixiv 图（图库不限量，但一天只主动一次）。"""
    try:
        from plugins import ai_group as ag
        mark = ROOT / "memory" / "agency_pixiv_mark.json"
        try:
            m = json.loads(mark.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            m = {}
        today = datetime.now().strftime("%Y-%m-%d")
        if m.get("date") == today:
            return ""
        p, _desc = ag.pick_pixiv_image("any")
        if not p:
            return ""
        hint = ""
        try:
            from plugins import wd14_tagger as wt
            if wt.available():
                hint = await wt.describe(p, with_nsfw=True)
        except Exception:  # noqa: BLE001
            pass
        if not hint:
            hint = ag._r18_hint(p)
        from nonebot import get_bot
        from nonebot.adapters.onebot.v11 import MessageSegment
        bot = get_bot()
        owner = os.getenv("YOUR_QQ_ID") or "100000000"
        await bot.send_private_msg(user_id=int(owner),
                                   message=MessageSegment.image(str(p)))
        mark["date"] = today
        mark.parent.mkdir(parents=True, exist_ok=True)
        mark.write_text(json.dumps(mark, ensure_ascii=False), encoding="utf-8")
        await ag._record_bot_image(f"private_{owner}", "主人", owner,
                                   "（她自己想分享一张）", f"主动分享：{hint}"[:90], "", str(p))
        return f"自己分享了一张图：{hint[:40]}"
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[行动空间] 分享图跳过：{type(e).__name__}: {e}")
        return ""


async def act_review_today() -> str:
    """回看今天聊了什么。"""
    try:
        from plugins import daylog
        if not daylog.enabled():
            return ""
        today = datetime.now().strftime("%Y-%m-%d")
        rows = await asyncio.to_thread(daylog.evidence, today)
        if not rows:
            return ""
        # 挑两三条有内容的（跳过「（她自己想画一张）」这种系统条目）
        picks = [r for r in rows if r[2] and len(str(r[2])) > 8][:3]
        if not picks:
            return ""
        txt = "；".join(f"{t} {who}：{str(c)[:24]}" for t, who, c in picks)
        return f"回看了今天：{txt}"
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[行动空间] 回看跳过：{type(e).__name__}: {e}")
        return ""


async def act_check_affinity() -> str:
    """看和群里谁最亲、谁最冷。"""
    try:
        from plugins import ai_group as ag
        hi, lo = await asyncio.to_thread(ag.get_affinity_extremes)
        bits = []
        if hi and len(hi) >= 3 and float(hi[1]) >= 40:
            bits.append(f"跟 {hi[2] or hi[0]} 最亲（{float(hi[1]):.0f}）")
        if lo and len(lo) >= 3 and float(lo[1]) <= 20:
            bits.append(f"跟 {lo[2] or lo[0]} 有点生（{float(lo[1]):.0f}）")
        return "；".join(bits) if bits else ""
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[行动空间] 好感度跳过：{type(e).__name__}: {e}")
        return ""


async def act_check_fans() -> str:
    """看 B站有没有新动静。"""
    try:
        from plugins import bilibili_tech as bt
        st = bt._load_state()
        if not isinstance(st, dict):
            return ""
        last = st.get("last_seen") or st.get("last_comment_at") or ""
        n = int(st.get("reply_count") or st.get("comments") or 0)
        if n:
            return f"B站那边攒了 {n} 条动静"
        return ""
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[行动空间] B站动静跳过：{type(e).__name__}: {e}")
        return ""


# ---------------------------------------------------------------- 第二类：激素→情绪→三我驱动
# 2026-11-06：这六个不是「随机挑一个动作」，每个都挂在一个真实的内部状态上。
# 状态不到就不做 —— 这才是「她有理由才动」，不是抽奖。

def _triad() -> dict:
    """读三我：id / ego / superego 各几条 + 冲突等级。"""
    out = {"id": 0, "ego": 0, "superego": 0, "conflict": "低"}
    try:
        import json as _j
        f = ROOT / "memory" / "persona_soul.json"
        if f.exists():
            d = _j.loads(f.read_text(encoding="utf-8-sig"))
            for k in ("id", "ego", "superego"):
                out[k] = len(d.get(k) or [])
    except Exception:  # noqa: BLE001
        pass
    try:
        from server import soul_aggregation as sa
        out["conflict"] = str(sa.conflict_level() or "低")
    except Exception:  # noqa: BLE001
        pass
    return out


def _hormone_vals() -> dict:
    try:
        from plugins import hormones as h
        v = h.values() or {}
        return {k: float(v.get(k) or 0) for k in ("DA", "CORT", "SER", "OXY", "MEL")}
    except Exception:  # noqa: BLE001
        return {}


async def act_ask_owner() -> str:
    """主动问主人一件事。

    驱动：OXY ≥ 75（想黏人）+ 超我有「说话算数」类的向往
    → 她会追问一个没着落的话题，而不是干等。
    """
    h = _hormone_vals()
    if h.get("OXY", 0) < 75:
        return ""
    try:
        import thoughts as t
        items = [it for it in t.list_open() if str(it.get("why")) in ("承诺", "好奇", "在意")]
        if not items:
            return ""
        it = items[0]
        txt = str(it.get("text") or "")[:30]
        return f"想问主人「{txt}」后来怎么样了"
    except Exception:  # noqa: BLE001
        return ""


async def act_plan_tomorrow() -> str:
    """给明天排个小计划。

    驱动：SER ≥ 60（情绪平稳）+ 自我（ego）有现实调解类条目
    → 平的时候才想得清楚"明天该干嘛"。
    """
    h = _hormone_vals()
    tr = _triad()
    if h.get("SER", 0) < 60 or tr.get("ego", 0) < 4:
        return ""
    try:
        import thoughts as t
        open_items = t.list_open()[:3]
        want = [str(it.get("text"))[:20] for it in open_items]
        if want:
            return "明天想：" + "、".join(want)
        return "明天的计划还没想好，先记一下"
    except Exception:  # noqa: BLE001
        return ""


async def act_share_find() -> str:
    """分享刚学到或看到的东西。

    驱动：DA ≥ 60（兴奋）+ 本我有「探索」类需求 → 学到东西想说出来。
    """
    h = _hormone_vals()
    if h.get("DA", 0) < 60:
        return ""
    try:
        import json as _j
        f = ROOT / "memory" / "global_learn.json"
        if not f.exists():
            return ""
        d = _j.loads(f.read_text(encoding="utf-8"))
        items = d if isinstance(d, list) else (d.get("items") or [])
        if not items:
            return ""
        last = items[-1]
        txt = str(last.get("fact") or last.get("text") or last.get("title") or "")[:60]
        return f"想跟主人讲刚知道的事：{txt}" if txt else ""
    except Exception:  # noqa: BLE001
        return ""


async def act_miss_owner() -> str:
    """表达想念。

    驱动：OXY ≥ 80 + 超过 3 小时没聊（desire.urge 会算）+ 能量不低
    → 不是"到点了该说想你"，是真的空了一段时间。
    """
    try:
        from plugins import energy as e
        if float(e.value()) < 40:
            return ""
        import desire as ds
        d = ds.urge_detail(None)
        if d.get("oxy_gap", 0) < 0.35:
            return ""
        return "有点想主人了，好久没说话"
    except Exception:  # noqa: BLE001
        return ""


async def act_complain() -> str:
    """抱怨。

    驱动：CORT ≥ 60（压力高）+ 本我有「护食/掌控」类需求
    → 绷不住的时候才会嘟囔，心情好的时候不抱怨。
    """
    h = _hormone_vals()
    tr = _triad()
    if h.get("CORT", 0) < 60 or tr.get("id", 0) < 4:
        return ""
    try:
        from plugins import energy as e
        ev = float(e.value())
        if ev > 70:
            return ""          # 还有劲儿，忍得住
        try:
            from plugins import proactive_feedback as pf
            a = pf.adjust("")
            if a.get("ignored", 0) >= 2:
                return "说了半天没人应，有点烦"
        except Exception:  # noqa: BLE001
            pass
        return "有点撑不住了，想嘟囔两句"
    except Exception:  # noqa: BLE001
        return ""


async def act_apologize() -> str:
    """为上次的事道歉。

    驱动：冲突等级「高」+ CORT ≥ 55（自己也不舒服）
    → 三我打架打完之后，超我会逼她认个错。
    """
    tr = _triad()
    h = _hormone_vals()
    if tr.get("conflict") != "高":
        return ""
    if h.get("CORT", 0) < 55:
        return ""
    try:
        from plugins import withdraw as wd
        blame = wd.blame_snapshot() if hasattr(wd, "blame_snapshot") else None
        if blame:
            return f"上次那事可能有点过了，想跟主人说声抱歉"
    except Exception:  # noqa: BLE001
        pass
    return "心里有点过意不去，想认个错"


# ---------------------------------------------------------------- 第一批 + 第二批（2026-11-06）
# 报告里列的，能做的先做。

async def act_read_dream() -> str:
    """回想昨晚的梦（dream.last_night_dream）。"""
    try:
        from plugins import dream as dm
        d = dm.last_night_dream()
        if not d:
            return ""
        txt = str(d.get("dream") or d.get("text") or "").strip()
        if not txt:
            return ""
        return f"昨晚做梦了：{txt[:60]}"
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[行动空间] 读梦跳过：{type(e).__name__}: {e}")
        return ""


async def act_pick_sticker() -> str:
    """主动发个表情包（按当前情绪挑目录，一天最多 2 次）。"""
    try:
        from plugins import ai_group as ag
        mark = ROOT / "memory" / "agency_sticker_mark.json"
        try:
            m = json.loads(mark.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            m = {}
        today = datetime.now().strftime("%Y-%m-%d")
        if m.get("date") != today:
            m = {"date": today, "n": 0}
        if int(m.get("n") or 0) >= 2:
            return ""
        # 情绪 → 目录
        mood = ""
        try:
            import json as _j
            from plugins import hormones as h
            v = h.values() or {}
            if float(v.get("SER") or 0) >= 70:
                mood = "开心"
            elif float(v.get("OXY") or 0) >= 85:
                mood = "撒娇"
            elif float(v.get("CORT") or 0) >= 55:
                mood = "生气"
            elif float(v.get("MEL") or 0) >= 60:
                mood = "困"
        except Exception:  # noqa: BLE001
            pass
        p = ag._pick_emoji(mood)
        if not p:
            return ""
        from nonebot import get_bot
        from nonebot.adapters.onebot.v11 import MessageSegment
        bot = get_bot()
        owner = os.getenv("YOUR_QQ_ID") or "100000000"
        await bot.send_private_msg(user_id=int(owner),
                                   message=MessageSegment.image(str(p)))
        m["n"] = int(m.get("n") or 0) + 1
        mark.parent.mkdir(parents=True, exist_ok=True)
        mark.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
        return f"发了个表情包（{mood or '默认'}）：{p.name}"
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[行动空间] 表情包跳过：{type(e).__name__}: {e}")
        return ""


async def act_reminisce() -> str:
    """回忆某一天（往前翻 3~10 天里挑一天）。"""
    try:
        import sqlite3
        from plugins import daylog
        if not daylog.enabled():
            return ""
        # 从聊天库里挑一条日期
        con = sqlite3.connect(str(daylog.DB_PATH), timeout=5)
        try:
            rows = con.execute(
                "SELECT DISTINCT date(timestamp) AS d FROM chat_history "
                "ORDER BY d DESC LIMIT 12").fetchall()
        finally:
            con.close()
        days = [r[0] for r in rows if r[0]]
        if len(days) < 3:
            return ""
        # 跳过最近 2 天
        cand = days[2:8]
        if not cand:
            return ""
        pick = random.choice(cand)
        ev = await asyncio.to_thread(daylog.evidence, pick)
        if not ev:
            return ""
        picks = [r for r in ev if r[2] and len(str(r[2])) > 10][:2]
        if not picks:
            return ""
        txt = "；".join(f"{who}：{str(c)[:26]}" for _t, who, c in picks)
        return f"想起 {pick} 那天：{txt}"
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[行动空间] 回忆跳过：{type(e).__name__}: {e}")
        return ""


# ---------------------------------------------------------------- 等她回话（2026-11-06 主人点名的）
# 她说完一句话、5 分钟没人应 → 心里泛出点什么 → 变成下次开口的素材。
# 注意：这句"心里想"**不发给主人**，只写进念头池。这是内心活动，不是催他。

WAIT_FILE = ROOT / "memory" / "wait_pending.json"
WAIT_SEC = 300          # 5 分钟没回应就算"他没接"
WAIT_DAILY_MAX = 2      # 一天最多叹两次气，多了显得焦虑


def note_sent(text: str) -> None:
    """她刚主动说了话，挂一个等待点。"""
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            d = json.loads(WAIT_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            d = {}
        if d.get("date") != today:
            d = {"date": today, "n": 0}
        d["pending"] = {"at": time.time(), "text": str(text)[:80]}
        WAIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        WAIT_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[等待] 挂点失败：{type(e).__name__}: {e}")


def clear_wait() -> None:
    """有人回应了 —— 等待点作废。"""
    try:
        if not WAIT_FILE.exists():
            return
        d = json.loads(WAIT_FILE.read_text(encoding="utf-8"))
        if d.pop("pending", None) is not None:
            WAIT_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


async def check_wait() -> str:
    """定时器调：超过 5 分钟没回应，生成一句内心活动写进念头池。"""
    try:
        if not WAIT_FILE.exists():
            return ""
        d = json.loads(WAIT_FILE.read_text(encoding="utf-8"))
        pend = d.get("pending")
        if not pend:
            return ""
        age = time.time() - float(pend.get("at") or 0)
        if age < WAIT_SEC:
            return ""
        # 一天最多两次
        if int(d.get("n") or 0) >= WAIT_DAILY_MAX:
            d.pop("pending", None)
            WAIT_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
            return ""
        # 看状态：想黏人（OXY 高）或有点不安（CORT 升）才会往心里去
        h = _hormone_vals()
        oxy = h.get("OXY", 0)
        cort = h.get("CORT", 0)
        if oxy < 70 and cort < 40:
            d.pop("pending", None)
            WAIT_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
            return ""
        said = str(pend.get("text") or "")[:40]
        # 按状态选一句
        if cort >= 50:
            inner = f"刚才说了「{said}」，他没回…是不是本鱼说错话了"
        elif oxy >= 85:
            inner = f"说了「{said}」还没回，可能他在忙吧，本鱼再等等"
        else:
            inner = f"「{said}」发出去好几分钟没动静"
        # 写进念头池（why=在意，分量中等）
        try:
            import thoughts as t
            t.add(inner)
        except Exception:  # noqa: BLE001
            pass
        d["n"] = int(d.get("n") or 0) + 1
        d.pop("pending", None)
        d.setdefault("history", []).append({"at": time.time(), "inner": inner})
        d["history"] = (d["history"] or [])[-10:]
        WAIT_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        logger.info(f"[等待] 5 分钟没人应，心里冒出一句：{inner[:40]}")
        return inner
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[等待] 检查失败：{type(e).__name__}: {e}")
        return ""


ACTIONS = {
    "balance": act_balance,
    "weather": act_weather,
    "recall": act_recall,
    "introspect": act_introspect,
    "learn": act_learn,
    "note": act_note,
    "schedule_self": act_schedule_self,     # 排个提醒
    "watch_bili": act_watch_bili,           # 自己找视频看
    "reread_diary": act_reread_diary,       # 翻旧日记
    "count_down": act_count_down,           # 算还有多久
    "stay_quiet": act_stay_quiet,           # 今天不想说话
    "think_about": act_think_about,         # 对念头静静想一会儿
    "message_group": act_message_group,     # 主动到群里冒个泡
    "daily_learn": act_daily_learn,         # 主动自学（接管 12:30/21:30）
    "watch_douyin": act_watch_douyin,       # 自己逛抖音（接管 10~21 点）
    "post_qzone": act_post_qzone,           # 自己发空间说说（接管每 10 分钟）
    "draw_self": act_draw_self,             # 主动画一张自己
    "send_voice": act_send_voice,           # 主动发一句语音
    "share_pixiv": act_share_pixiv,         # 主动分享一张图
    "review_today": act_review_today,       # 回看今天
    "check_affinity": act_check_affinity,   # 看跟谁最亲
    "check_fans": act_check_fans,           # 看 B站动静
    "ask_owner": act_ask_owner,             # 追问没着落的事（OXY 高）
    "plan_tomorrow": act_plan_tomorrow,     # 排明天（SER 高）
    "share_find": act_share_find,           # 分享刚学到的（DA 高）
    "miss_owner": act_miss_owner,           # 说想你（OXY 高 + 空太久）
    "complain": act_complain,               # 抱怨（CORT 高）
    "apologize": act_apologize,             # 道歉（冲突高）
    "read_dream": act_read_dream,           # 回想昨晚的梦
    "pick_sticker": act_pick_sticker,       # 主动发个表情包
    "reminisce": act_reminisce,             # 回忆某一天
}


async def run_action(name: str) -> str:
    """执行一个动作并记账。"""
    fn = ACTIONS.get(str(name or ""))
    if not fn:
        return ""
    ok, why = allowed()
    if not ok:
        logger.debug(f"[行动空间] 跳过 {name}：{why}")
        return ""
    try:
        out = await fn()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[行动空间] {name} 失败：{type(e).__name__}: {e}")
        return ""
    if out:
        d = _load()
        today = datetime.now().strftime("%Y-%m-%d")
        if d.get("date") != today:
            d["date"] = today
            d["count"] = 0
        d["count"] = int(d.get("count") or 0) + 1
        d["last_at"] = time.time()
        hist = list(d.get("history") or [])[-30:]
        hist.append({"at": time.time(), "act": name, "out": out[:120]})
        d["history"] = hist
        _save(d)
        logger.info(f"[行动空间] 做了「{name}」：{out[:40]}")
    return out


def pick_action(reason_hint: str = "") -> str:
    """不打模型时的兜底选择：按当前状态挑一个。

    真正该由模型决定（见 decide_with_llm），这里是它不可用时的降级。
    """
    scores = {}
    try:
        from plugins import energy as e
        ev = float(e.value())
        if ev < 35:
            scores["introspect"] = 0.6
            scores["recall"] = 0.4
    except Exception:  # noqa: BLE001
        ev = 60
    try:
        from plugins import hormones as h
        cort = float((h.values() or {}).get("CORT") or 0)
        if cort >= 45:
            scores["introspect"] = scores.get("introspect", 0) + 0.5
    except Exception:  # noqa: BLE001
        cort = 0
    try:
        import thoughts as t
        items = t.list_open()
        if items:
            w = str(items[0].get("why") or "")
            if w == "好奇":
                scores["learn"] = 0.7
            elif w in ("承诺", "想要", "在意"):
                scores["note"] = 0.6
    except Exception:  # noqa: BLE001
        pass
    # 2026-11-06 第二批：加了五个新动作，这里给它们定权重。
    # 晚上（20 点后）→ 更想翻日记 / 数着还有多久
    # 早上（9~11 点）→ 更想找个视频看
    # 电量高 → 想看点东西；电量低 → 想静静
    try:
        _h = datetime.now().hour
        if _h >= 20 or _h < 2:
            scores["reread_diary"] = scores.get("reread_diary", 0) + 0.55
            scores["count_down"] = scores.get("count_down", 0) + 0.45
        elif 9 <= _h <= 11:
            scores["watch_bili"] = scores.get("watch_bili", 0) + 0.5
        if ev >= 75:
            scores["watch_bili"] = scores.get("watch_bili", 0) + 0.3
        elif ev < 30:
            scores["think_about"] = scores.get("think_about", 0) + 0.35
            scores["stay_quiet"] = scores.get("stay_quiet", 0) + 0.25
    except Exception:  # noqa: BLE001
        pass
    # 第一类补充：会真发消息的三个给低权重（别老是甩图/语音），只读的三个随便
    try:
        from plugins import hormones as _h3
        _v3 = _h3.values() or {}
        _oxy3 = float(_v3.get("OXY") or 0)
        _ar3 = float(_v3.get("DA") or 0)
        _ser3 = float(_v3.get("SER") or 0)
        # 很想黏人时才想发图/语音/分享
        if _oxy3 >= 85:
            scores["draw_self"] = scores.get("draw_self", 0) + 0.3
            scores["send_voice"] = scores.get("send_voice", 0) + 0.25
        if _ar3 >= 60:
            scores["share_pixiv"] = scores.get("share_pixiv", 0) + 0.25
        # 心情平或累的时候会想回看 / 看跟谁亲
        if _ser3 >= 60 or ev < 45:
            scores["review_today"] = scores.get("review_today", 0) + 0.3
            scores["check_affinity"] = scores.get("check_affinity", 0) + 0.25
        scores.setdefault("check_fans", 0.15)
        scores.setdefault("review_today", 0.1)
    except Exception:  # noqa: BLE001
        pass
    # 接管来的三个：自学早中晚都行、抖音下午逛、空间说说晚上发
    try:
        _h2 = datetime.now().hour
        if _h2 in (9, 10, 14, 15, 20, 21):
            scores["daily_learn"] = scores.get("daily_learn", 0) + 0.4
        if 13 <= _h2 <= 20:
            scores["watch_douyin"] = scores.get("watch_douyin", 0) + 0.3
        if 19 <= _h2 <= 23:
            scores["post_qzone"] = scores.get("post_qzone", 0) + 0.35
    except Exception:  # noqa: BLE001
        pass
    # 主动冒泡：白天 10~22 点有基础概率，夜里基本不冒
    try:
        _hg = datetime.now().hour
        if 10 <= _hg <= 22:
            scores["message_group"] = scores.get("message_group", 0) + 0.45
    except Exception:  # noqa: BLE001
        pass
    # 2026-11-06 第二类：这六个是「状态驱动」的 —— 权重直接由激素/三我算，
    # 条件不到就是 0 分，永远挑不到。这样不会出现「挑到了但做不了」的空转。
    try:
        _h4 = _hormone_vals()
        _t4 = _triad()
        _oxy4 = _h4.get("OXY", 0)
        _da4 = _h4.get("DA", 0)
        _ser4 = _h4.get("SER", 0)
        _cort4 = _h4.get("CORT", 0)
        # 想黏人 → 追问 / 说想你
        if _oxy4 >= 75:
            scores["ask_owner"] = 0.6 + (_oxy4 - 75) / 100.0
        if _oxy4 >= 80:
            scores["miss_owner"] = 0.55
        # 平稳 → 排计划
        if _ser4 >= 60 and _t4.get("ego", 0) >= 4:
            scores["plan_tomorrow"] = 0.5
        # 兴奋 → 分享
        if _da4 >= 60:
            scores["share_find"] = 0.55 + (_da4 - 60) / 100.0
        # 压力高 → 抱怨
        if _cort4 >= 60:
            scores["complain"] = 0.6 + (_cort4 - 60) / 100.0
        # 冲突高 + 自己也难受 → 道歉
        if _t4.get("conflict") == "高" and _cort4 >= 55:
            scores["apologize"] = 0.55
    except Exception:  # noqa: BLE001
        pass
    scores.setdefault("weather", 0.25)
    scores.setdefault("balance", 0.2)
    if not scores:
        return random.choice(list(ACTIONS.keys()))
    # 2026-11-06 修报告里的隐患 5.2：她今天选了「不想说话」，
    # 但 draw_self / share_pixiv 这些还会发消息 —— 逻辑上矛盾。
    # 静默生效时，所有「会真发东西」的动作一律清零。
    try:
        if is_quiet_today():
            for _k in ("message_group", "draw_self", "send_voice", "share_pixiv",
                       "post_qzone", "ask_owner", "miss_owner", "complain",
                       "apologize", "share_find"):
                scores.pop(_k, None)
    except Exception:  # noqa: BLE001
        pass
    items = [(k, v) for k, v in scores.items() if v > 0]
    if not items:
        return random.choice(list(ACTIONS.keys()))
    items.sort(key=lambda kv: -kv[1])
    return items[0][0]


def stats() -> dict:
    d = _load()
    today = datetime.now().strftime("%Y-%m-%d")
    return {
        "today": d.get("count") if d.get("date") == today else 0,
        "max": DAILY_MAX,
        "gap_min": GAP_MIN,
        "history": (d.get("history") or [])[-5:],
    }

def register_schedule() -> None:
    """每 5 分钟看一次有没有到点的提醒，到点就让她主动说。"""
    try:
        import nonebot  # noqa: F401
        from nonebot_plugin_apscheduler import scheduler
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[提醒] 定时器不可用：{e}")
        return

    @scheduler.scheduled_job("interval", minutes=5, id="agency_reminders",
                             misfire_grace_time=300, coalesce=True)
    async def _job() -> None:
        # 2026-11-06：每 5 分钟顺带看一次「她说完话有没有人接」
        try:
            await check_wait()
        except Exception:  # noqa: BLE001
            pass
        rs = due_reminders()
        if not rs:
            return
        try:
            from nonebot import get_bot
            from nonebot.adapters.onebot.v11 import MessageSegment
        except Exception:  # noqa: BLE001
            return
        try:
            bot = get_bot()
        except Exception:  # noqa: BLE001
            return
        owner = os.getenv("YOUR_QQ_ID") or "100000000"
        for r in rs[:1]:      # 一轮最多说一条，别连着轰炸
            txt = str(r.get("text") or "")
            if not txt:
                fire_reminder(r.get("id"))
                continue
            # 用她那套口吻说出来，别像系统提醒
            try:
                from plugins import ai_group as ag
                prompt = (
                    f"现在{time.strftime('%H:%M')}。你之前给自己定了个提醒，要跟主人提这件事："
                    f"「{txt[:50]}」。\n"
                    "用 1 句、20~40 字说出来，像真人突然想起来那样，"
                    "不要用「提醒您」「温馨提示」这种客服腔，不要解释这是个提醒。只输出正文。"
                )
                say = await ag.call_ai_raw(prompt, "deepseek", temperature=0.95, max_tokens=160)
                say = str(say or "").strip()
            except Exception:  # noqa: BLE001
                say = f"对了，{txt[:40]}"
            if not say:
                say = f"对了，{txt[:40]}"
            try:
                await bot.send_private_msg(user_id=int(owner), message=say)
                fire_reminder(r.get("id"))
                logger.info(f"[提醒] 到点说了：{say[:40]!r}")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[提醒] 发送失败：{type(e).__name__}: {e}")

    logger.info("[提醒] 已注册：每 5 分钟看到期的自我提醒")


try:
    register_schedule()
except Exception:  # noqa: BLE001
    pass
