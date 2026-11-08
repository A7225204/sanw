"""本鱼每天睡前写一篇日记：只存档、不主动推送私聊，主人发指令回看。

设计口径（与主人确认）：
* 定时：默认每天 23:30 生成当天日记，触发后随机延迟 0~10 分钟；开关可在私聊切换；
* 推送：定时生成后 **不发** 主人私聊，只落独立日记文件；指令应答才回一条简短确认；
* 素材：当天私聊互动 + 当天群聊互动 + B 站回评记录 + 心情/好感度/饥饿/精力状态；
  不联网搜新闻，只复盘当天真实发生的事，没发生的小节不硬编；
* 存档：`memory/diary/YYYY-MM-DD.json`（素材摘要 + 正文 + 状态快照），本地保留最近 N 篇；
* 记忆：日记正文独立存档，不写共享记忆库、不入长期档案，避免流水被反复合并污染。

配置（.env，改完重启 bot 生效）：
* DAILY_DIARY_ENABLED          定时总开关，默认 true
* DAILY_DIARY_TIME             写日记时刻 HH:MM，默认 "23:30"
* DAILY_DIARY_DELAY_MIN/MAX    触发后随机延迟分钟，默认 0 / 10
* DAILY_DIARY_MAX_CHARS        日记正文字数上限，默认 600
* DAILY_DIARY_KEEP             本地保留最近多少篇，默认 30
* DAILY_DIARY_MATERIAL_PRIVATE 素材里最多取多少条当天私聊，默认 12
* DAILY_DIARY_MATERIAL_GROUP   素材里最多取多少条当天群聊，默认 16

主人私聊指令：
* `/日记`                日记面板（开关 / 定时 / 已存篇数 / 指令清单）
* `/写日记`（`/日记 写`、`/日记 今天`）  立即写当天日记并存档（已写过则提示）
* `/日记 重写`           强制覆盖重写当天日记
* `/日记历史`（`/日记记录`）  回看最近几篇日期 + 首行预览
* `/日记 2026-10-22`     回看指定日期日记全文
* `/日记开关 开|关`      切换定时写日记开关，落盘状态文件

状态文件：`memory/diary/state.json`（定时开关）。
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import sqlite3
import sys
import time
from datetime import datetime
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
from plugins import reply_log  # noqa: E402
from memory_manager import (  # noqa: E402
    DB_PATH,
    get_today_memory,
    get_affinity,
    get_tag,
    init_db,
)

cfg = get_config()
logger = nonebot.logger

OWNER_QQ_ID = str(os.getenv("OWNER_QQ_ID", cfg.daily_diary.owner_qq or "100000000"))
DIARY_DIR = ROOT / "memory" / "diary"
STATE_FILE = DIARY_DIR / "state.json"
BILI_STATE_FILE = ROOT / ".bili_watch_state.json"

_RUNNING = False  # 任务互斥：防止两轮定时 / 手动并发写同一篇


# ------------------------------------------------------------------ 状态持久化

def _default_state() -> dict:
    return {"enabled": cfg.daily_diary.enabled}


def _load_state() -> dict:
    try:
        if STATE_FILE.exists():
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("enabled", cfg.daily_diary.enabled)
                if not isinstance(data["enabled"], bool):
                    data["enabled"] = cfg.daily_diary.enabled
                return data
    except (OSError, ValueError) as e:
        logger.warning(f"[每日日记] 状态读取失败，用默认值：{e}")
    return _default_state()


def _save_state(state: dict) -> None:
    try:
        DIARY_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as e:
        logger.error(f"[每日日记] 状态保存失败：{e}")


def _enabled(state: dict | None = None) -> bool:
    state = state or _load_state()
    return bool(state.get("enabled", cfg.daily_diary.enabled))


def _today_key() -> str:
    return time.strftime("%Y-%m-%d")


def _diary_path(date_key: str) -> Path:
    return DIARY_DIR / f"{date_key}.json"


# ------------------------------------------------------------------ 素材采集

def _is_pet(speaker: str | None) -> bool:
    return str(speaker or "").strip() == cfg.persona.nickname


def _fmt_line(speaker: str | None, message: str, reply: str = "", limit: int = 80) -> str:
    who = "本鱼" if _is_pet(speaker) else (str(speaker) or "群友")
    out: list[str] = []
    msg = (message or "").strip()
    if msg:
        out.append(f"{who}说：{msg[:limit]}")
    if reply and reply.strip():
        out.append(f"本鱼回：{reply.strip()[:limit]}")
    return "\n".join(out)


def _collect_private() -> list[dict]:
    """当天与主人的私聊记录（speaker/message；reply 在库内另列，这里补取最近私聊含 reply）。"""
    owner_scope = f"private_{OWNER_QQ_ID}"
    rows = get_today_memory(limit=cfg.daily_diary.material_private, scope=owner_scope)
    return [
        {"speaker": r.get("speaker", ""), "message": (r.get("message") or "").strip()}
        for r in rows
        if (r.get("message") or "").strip()
    ]


def _collect_group() -> dict[str, list[dict]]:
    """当天群聊记录：按群 scope 分组；直连记忆库取 message + reply。失败返回空。"""
    grouped: dict[str, list[dict]] = {}
    try:
        init_db()
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL;")
        c = conn.cursor()
        c.execute(
            "SELECT scope, speaker, message, reply FROM chat_history "
            "WHERE date(timestamp, 'localtime') = date('now', 'localtime') "
            "AND scope LIKE 'group_%' ORDER BY id DESC LIMIT ?",
            (cfg.daily_diary.material_group,),
        )
        rows = c.fetchall()
        conn.close()
        for scope, speaker, message, reply in reversed(rows):
            if not (message or "").strip():
                continue
            grouped.setdefault(scope, []).append(
                {"speaker": speaker or "", "message": message.strip(), "reply": (reply or "").strip()}
            )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[每日日记] 群聊素材读取失败：{type(e).__name__}: {e}")
    # 每组最多留 6 条，避免单个群刷屏挤掉其他群
    for scope, items in grouped.items():
        grouped[scope] = items[-6:]
    return grouped


def _collect_bili() -> list[dict]:
    """今天本鱼在 B 站回评 / 被 @ 的记录摘要。"""
    out: list[dict] = []
    try:
        if not BILI_STATE_FILE.exists():
            return out
        data = json.loads(BILI_STATE_FILE.read_text(encoding="utf-8"))
        replies = data.get("recent_replies") or []
        today_prefix = time.strftime("%Y-%m-%d")
        for r in reversed(replies):
            at = r.get("at_time") or 0
            try:
                if datetime.fromtimestamp(float(at)).strftime("%Y-%m-%d") != today_prefix:
                    continue
            except (TypeError, ValueError, OSError):
                continue
            out.append(
                {
                    "nickname": r.get("nickname") or "群友",
                    "title": (r.get("title") or "").strip(),
                    "source": r.get("source") or "",
                    "url": r.get("url") or "",
                }
            )
    except (OSError, ValueError) as e:
        logger.warning(f"[每日日记] B 站素材读取失败：{type(e).__name__}: {e}")
    return out[-10:]


BILI_DAILY_FILE = ROOT / "memory" / "bili_daily.json"


def _collect_bili_watched() -> list[dict]:
    """今天她在 B 站**看了哪些视频**（bili_daily.json 的 history，按日期筛）。

    2026-11-08：原来日记只收「别人找她互动」（.bili_watch_state.json），
    **不收她看了什么** —— 实测 11-07 那篇正文里只有一句含糊的
    「B站那摊子事儿本来卡着的」，当天真看的《42 龙游的风》《弧光猎人》一个字没提。
    数据明明在 bili_daily.json 里躺着，只是没喂进去。
    """
    out: list[dict] = []
    try:
        if not BILI_DAILY_FILE.exists():
            return out
        data = json.loads(BILI_DAILY_FILE.read_text(encoding="utf-8"))
        today = time.strftime("%Y-%m-%d")
        for h in (data.get("history") or []):
            if not str(h.get("at") or "").startswith(today):
                continue
            out.append({
                "title": str(h.get("title") or "").strip()[:80],
                "summary": str(h.get("summary") or "").strip()[:160],
                "by": str(h.get("by") or ""),
                "bvid": str(h.get("bvid") or ""),
            })
    except (OSError, ValueError) as e:
        logger.warning(f"[每日日记] B 站观看记录读取失败：{type(e).__name__}: {e}")
    return out[-12:]


def _pet_state_snapshot() -> dict:
    """心情 / 饥饿 / 精力 / 今日人设 / 好感度 / 标签快照；ai_group 不可用时降级默认值。"""
    snap = {
        "mood": "开心", "mood_val": 50, "hunger": None, "energy": None,
        "body_desc": "", "daily_mood": "", "affinity": 50, "tag": "",
    }
    try:
        affinity = get_affinity(OWNER_QQ_ID)
        tag = get_tag(OWNER_QQ_ID)
        snap["affinity"] = affinity
        snap["tag"] = tag or ""
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[每日日记] 好感度读取失败：{type(e).__name__}: {e}")

    try:
        from plugins import ai_group as ag  # 惰性导入，避免导入期循环依赖
        mood, mood_val = ag._pick_pet_mood()
        snap["mood"] = mood or "开心"
        snap["mood_val"] = int(mood_val) if mood_val is not None else 50
        hunger, energy, desc = ag._refresh_body_cache()
        snap["hunger"] = int(hunger)
        snap["energy"] = int(energy)
        snap["body_desc"] = desc or ""
        snap["daily_mood"] = getattr(ag, "CURRENT_DAILY_MOOD", "") or ""
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[每日日记] 心情/身体状态读取失败，用默认值：{type(e).__name__}: {e}")
    return snap


def _collect_materials() -> dict:
    private = _collect_private()
    group = _collect_group()
    bili = _collect_bili()
    bili_watched = _collect_bili_watched()
    state = _pet_state_snapshot()
    group_n = sum(len(v) for v in group.values())
    return {
        "private": private,
        "group": group,
        "bili": bili,
        "bili_watched": bili_watched,
        "state": state,
        "stats": {
            "private_n": len(private),
            "group_n": group_n,
            "bili_n": len(bili),
        },
    }


# ------------------------------------------------------------------ 素材文本拼装

def _materials_text(mats: dict) -> str:
    parts: list[str] = []

    priv = mats["private"]
    if priv:
        lines = [_fmt_line(r["speaker"], r["message"]) for r in priv if r["message"]]
        parts.append("【今天和主人的私聊】\n" + "\n".join(lines[-cfg.daily_diary.material_private:]))
    else:
        parts.append("【今天和主人的私聊】\n（今天私聊没留下记录）")

    grp = mats["group"]
    if grp:
        gblocks: list[str] = []
        for scope, items in grp.items():
            gid = scope.removeprefix("group_")
            glines = [_fmt_line(r["speaker"], r["message"], r.get("reply", "")) for r in items]
            gblocks.append(f"· 群 {gid}：\n" + "\n".join(glines))
        parts.append("【今天群里聊到的】\n" + "\n\n".join(gblocks))
    else:
        parts.append("【今天群里聊到的】\n（今天群里没什么动静）")

    bili = mats["bili"]
    if bili:
        blines = [
            f"· 在 B 站 {b['source'] or '评论区'} 遇到 {b['nickname']}（视频：{b['title'] or '未记标题'}）"
            for b in bili
        ]
        parts.append("【今天在 B 站的互动】\n" + "\n".join(blines))
    else:
        parts.append("【今天在 B 站的互动】\n（今天 B 站没人找本鱼）")

    watched = mats.get("bili_watched") or []
    if watched:
        wlines = [f"· 《{w['title']}》——{w['summary'] or '（看完没留下感想）'}" for w in watched]
        parts.append("【今天在 B 站看了什么】" + NL + NL.join(wlines))
    else:
        parts.append("【今天在 B 站看了什么】" + NL + "（今天没看视频）")

    s = mats["state"]
    body_bits = []
    if s["daily_mood"]:
        body_bits.append(f"今日人设：{s['daily_mood']}")
    body_bits.append(f"心情：{s['mood']}（{s['mood_val']} 分）")
    if s["hunger"] is not None:
        body_bits.append(f"饥饿度：{s['hunger']}%")
    if s["energy"] is not None:
        body_bits.append(f"精力值：{s['energy']}%")
    if s["body_desc"]:
        body_bits.append(f"身体状态：{s['body_desc']}")
    body_bits.append(f"对主人好感度：{s['affinity']}")
    if s["tag"]:
        body_bits.append(f"主人在本鱼心里的标签：{s['tag']}")
    parts.append("【本鱼今天的状态】\n" + "；".join(body_bits))

    return "\n\n".join(parts)


# ------------------------------------------------------------------ 文案生成与清洗

# 提示词复述 / 推理草稿特征词（命中即判定不是日记正文，不入库）
_DIARY_DRAFT_WORDS = (
    "要求", "指令", "标题前缀", "编号", "markdown", "动作描写", "硬性要求",
    "复述素材", "不要编号", "300~500", "素材清单",
)
_DIARY_DRAFT_PREFIX_RE = re.compile(
    r"^(需要写|应该输出|我们需要|让我来|任务[:：]|角色[:：]|分析一下|先思考)"
)


def _is_diary_draft(text: str) -> bool:
    if not text:
        return True
    if _DIARY_DRAFT_PREFIX_RE.match(text):
        return True
    return any(w in text.lower() for w in _DIARY_DRAFT_WORDS)


def _clean_diary(text: str, max_chars: int) -> str | None:
    """保留分段换行；去代码围栏 / 方括号表情 / Markdown 加粗 / 首尾引号；超限在标点处截断。"""
    if not text:
        return None
    text = text.strip()
    text = re.sub(r"```[^\n]*", "", text)
    text = text.replace("```", "")
    text = re.sub(r"\[表情[:：][^\]]*\]", "", text)
    text = text.replace("**", "").replace("__", "")
    text = text.strip(" \t\r\n\"'“”‘’-—·*")
    # 折叠行内多空格，但保留段落换行
    text = "\n".join(" ".join(ln.split()) for ln in text.splitlines())
    text = re.sub(r"^(主人|本鱼|日记)[:：]\s*", "", text).strip()
    if not text or len(text) < 20:
        return None
    # 指令复述 / 推理草稿直接判无效
    if _is_diary_draft(text):
        return None
    if len(text) > max_chars:
        cut = text[:max_chars]
        m = list(re.finditer(r"[。！？…～~!?]", cut))
        if m:
            cut = cut[:m[-1].start() + 1]
        text = cut.strip()
    if len(text) < 20:
        return None
    return text


def _build_prompt(mats: dict, materials_text: str) -> str:
    date_str = time.strftime("%Y 年 %m 月 %d 日")
    return (
        f"你是「蓝色大肥鱼」，圆滚滚的蓝色鲸鱼拟人少女，傲娇、嘴损、黏主人，现在是 {date_str} 深夜，"
        "你在写只有自己看的日记。\n"
        f"下面是你今天真实经历过的素材（私聊 / 群聊 / B 站 / 状态），只能基于这些事实写，不许编：\n\n"
        f"{materials_text}\n\n"
        "【怎么写】\n"
        "1. 第一人称，自称「本鱼」；短句为主、口语化，像手机备忘录里随手写的，不是作文；\n"
        "2. 2~3 段，300~500 字，段落之间空一行；开头不要用「今天」硬起，"
        "也不要用「夜色渐深」「窗外的风」这种套话；\n"
        "3. 写具体的：谁说了哪句话、你当时怎么想、身体什么感觉（困、饿、想被揉）——"
        "用感受代替数值，别把饥饿度/好感度/精力抄进正文；\n"
        "4. 允许情绪化：不爽就损两句（不脏话）、开心就炫耀、想主人就直说，"
        "允许有一句示弱或反悔；\n"
        "5. 禁止：自称 AI/程序/模型、解释你在写日记、逐条复述素材、排比鸡汤、结尾升华；\n"
        "6. 【今天在 B 站看了什么】那节有内容时，**必须至少写一条**：说清片名 + 一个具体细节"
        "（某句台词、某个画面、字幕断在哪、你当时什么反应），不许只写「B站那摊子事儿」这种含糊话；"
        "没看就一个字不提，别硬编；" + NL +
        "7. 只输出日记正文，不要标题、不要前后寒暄、不要额外说明。"
    )


async def _generate_body(mats: dict) -> str | None:
    materials_text = _materials_text(mats)
    # 今天几乎没什么素材就不生成，省一次调用
    if len(materials_text.strip()) < 40:
        logger.info("[每日日记] 今天素材太少，跳过不写")
        return None
    prompt = _build_prompt(mats, materials_text)
    for attempt in range(2):
        try:
            raw = await call_ai_raw(prompt, "deepseek", temperature=0.9, max_tokens=1200)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[每日日记] 第 {attempt + 1} 次大模型调用异常：{type(e).__name__}: {e}")
            continue
        if not raw or not raw.strip():
            # byte 通道只取 content，推理占满额度时正文为空串（非兜底句），需单独判失败并重试
            logger.warning(f"[每日日记] 第 {attempt + 1} 次返回空正文（推理占满额度），重试")
            continue
        if raw.startswith("（本鱼"):  # call_ai_raw 失败兜底句
            logger.warning("[每日日记] 大模型返回失败兜底句，本篇不存档")
            return None
        body = _clean_diary(raw, cfg.daily_diary.max_chars)
        if body:
            return body
        logger.warning(f"[每日日记] 第 {attempt + 1} 次草稿清洗后为空，重试")
    logger.warning("[每日日记] 2 次重试均未生成可用日记正文")
    return None


# ------------------------------------------------------------------ 存档与清理

DIARY_TXT_DIR = ROOT / "日记"
_WEEKDAY = ("一", "二", "三", "四", "五", "六", "日")


def export_diary_txt(date_key: str, body: str, state: dict | None = None) -> Path | None:
    """把一篇日记导出成 TXT：文件名直接写"2026年10月25日.txt"，方便在资源管理器里翻。"""
    try:
        y, m, d = date_key.split("-")
        title = f"{y}年{int(m)}月{int(d)}日"
        try:
            wd = _WEEKDAY[datetime.strptime(date_key, "%Y-%m-%d").weekday()]
            title += f"　星期{wd}"
        except Exception:  # noqa: BLE001
            pass
        bits = []
        st = state or {}
        if st.get("daily_mood"):
            bits.append(f"心情 {st.get('daily_mood')}")
        if st.get("affinity") is not None:
            bits.append(f"好感 {st.get('affinity')}")
        if st.get("energy") is not None:
            bits.append(f"精力 {st.get('energy')}")
        head = title + ("　（" + " · ".join(str(b) for b in bits) + "）" if bits else "")
        text = f"{head}\n{'─' * 34}\n\n{body.strip()}\n"
        DIARY_TXT_DIR.mkdir(parents=True, exist_ok=True)
        path = DIARY_TXT_DIR / f"{title.split('　')[0]}.txt"
        path.write_text(text, encoding="utf-8-sig")  # 带 BOM，记事本打开不乱码
        return path
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[每日日记] 导出 TXT 失败：{type(e).__name__}: {e}")
        return None


def export_all_txt() -> int:
    """把 memory/diary 里现有的日记全部导出成 TXT（可重复执行）。"""
    n = 0
    for path in sorted(DIARY_DIR.glob("????-??-??.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        body = str(data.get("body") or "").strip()
        if body and export_diary_txt(data.get("date") or path.stem, body, data.get("state")):
            n += 1
    return n


def _save_diary(date_key: str, mats: dict, body: str) -> None:
    DIARY_DIR.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "date": date_key,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "state": mats["state"],
        "stats": mats["stats"],
        "body": body,
    }
    path = _diary_path(date_key)
    path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    export_diary_txt(date_key, body, mats.get("state"))
    _prune_old()


def _prune_old() -> None:
    """本地只保留最近 cfg.daily_diary.keep 篇日记，旧文件删除。"""
    try:
        files = sorted(
            (p for p in DIARY_DIR.glob("????-??-??.json")),
            key=lambda p: p.name,
        )
        for p in files[:-cfg.daily_diary.keep] if len(files) > cfg.daily_diary.keep else []:
            p.unlink(missing_ok=True)
    except OSError as e:
        logger.warning(f"[每日日记] 清理旧日记失败：{type(e).__name__}: {e}")


def _load_diary(date_key: str) -> dict | None:
    path = _diary_path(date_key)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _list_diaries(limit: int = 10) -> list[dict]:
    if not DIARY_DIR.exists():
        return []
    files = sorted(
        (p for p in DIARY_DIR.glob("????-??-??.json")),
        key=lambda p: p.name,
        reverse=True,
    )
    out: list[dict] = []
    for p in files[:limit]:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            body = (data.get("body") or "").strip()
            first = body.splitlines()[0] if body else ""
            out.append({
                "date": data.get("date") or p.stem,
                "chars": len(body),
                "preview": first[:40],
                "created_at": data.get("created_at", ""),
            })
        except (OSError, ValueError):
            continue
    return out


# ------------------------------------------------------------------ 写日记主流程

async def _write_diary(force: bool = False) -> tuple[str | None, str]:
    """生成当天日记并存档。返回 (body, status)；status: saved / existed / failed / empty。"""
    global _RUNNING
    if _RUNNING:
        logger.info("[每日日记] 上一轮还在写，本轮跳过")
        return None, "running"

    date_key = _today_key()
    existing = _load_diary(date_key)
    if existing and not force:
        return existing.get("body"), "existed"

    _RUNNING = True
    set_activity("写日记中", 300)
    try:
        mats = await asyncio.to_thread(_collect_materials)
        body = await _generate_body(mats)
        try:
            from plugins.humanize import strip_cliches
            _hz = strip_cliches(body)
            if _hz and len(_hz) >= 20:
                body = _hz
        except Exception:  # noqa: BLE001
            pass
        if not body:
            reply_log.record(
                group_id=0, speaker="主人", user_msg="定时写日记", reply="",
                kind="daily_diary", error="生成正文为空或失败",
            )
            logger.warning("[每日日记] 没生成出可用日记，本篇不存档")
            return None, "failed"

        await asyncio.to_thread(_save_diary, date_key, mats, body)
        try:
            from plugins import qzone_sync
            if qzone_sync.load_state().get("enabled"):
                # 按主人决定：日记不写 QQ 日志，只存本地/网页日历
                logger.info("[每日日记·QQ空间] 已关闭：日记只存本地，不写 QQ 日志")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[每日日记·QQ空间] 异常：{e}")
        reply_log.record(
            group_id=0, speaker="主人", user_msg="定时写日记",
            reply=body[:200], kind="daily_diary",
        )
        logger.info(
            f"[每日日记] {date_key} 日记已存档（{len(body)} 字；"
            f"私聊 {mats['stats']['private_n']} 条 / 群聊 {mats['stats']['group_n']} 条 / "
            f"B 站 {mats['stats']['bili_n']} 条）"
        )
        return body, "saved"
    except Exception as e:  # noqa: BLE001
        reply_log.record(
            group_id=0, speaker="主人", user_msg="定时写日记", reply="",
            kind="daily_diary", error=f"{type(e).__name__}: {e}"[:200],
        )
        logger.warning(f"[每日日记] 写日记失败：{type(e).__name__}: {e}")
        return None, "failed"
    finally:
        _RUNNING = False


# ------------------------------------------------------------------ 定时任务

async def _scheduled_run() -> None:
    state = _load_state()
    if not _enabled(state):
        logger.info("[每日日记] 定时开关关闭，跳过今天的日记")
        return
    existing = _load_diary(_today_key())
    if existing:
        logger.info("[每日日记] 今天日记已写过，定时任务跳过")
        return

    delay = random.uniform(cfg.daily_diary.delay_min, cfg.daily_diary.delay_max)
    if delay > 0:
        await asyncio.sleep(delay * 60)
    state = _load_state()
    if not _enabled(state):
        logger.info("[每日日记] 等待延迟后开关已关，取消本轮")
        return
    if _load_diary(_today_key()):
        logger.info("[每日日记] 等待延迟后发现今天已写，取消")
        return

    await _write_diary()


def _register_jobs() -> None:
    if scheduler is None:
        logger.warning("[每日日记] apscheduler 不可用，定时任务未注册")
        return
    if not cfg.daily_diary.enabled:
        logger.info("[每日日记] DAILY_DIARY_ENABLED=false，未注册定时任务")
        return
    hh, mm = cfg.daily_diary.time
    scheduler.add_job(
        _scheduled_run, "cron", hour=hh, minute=mm,
        id="daily_diary_night", replace_existing=True, misfire_grace_time=600,
    )
    logger.info(f"[每日日记] 已注册定时任务 {hh:02d}:{mm:02d}（只存档不推送）")


@nonebot.get_driver().on_startup
async def _daily_diary_startup() -> None:
    _register_jobs()
    # 补写：如果今天已经过了写日记的时间但这篇还没写（比如 23:30 时机器在重启），
    # 启动后过一会儿补一篇，不然那天就永远缺了。
    try:
        hh, mm = cfg.daily_diary.time
        now = datetime.now()
        due = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        if now < due:
            return
        if _load_diary(_today_key()):
            return
        if not _enabled():
            return
        logger.info("[每日日记] 今天的日记还没写（启动补写），60 秒后开始")

        async def _catch_up() -> None:
            await asyncio.sleep(60)
            try:
                if _load_diary(_today_key()):
                    return
                await _write_diary(force=False)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[每日日记] 启动补写失败：{type(e).__name__}: {e}")

        asyncio.create_task(_catch_up())
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[每日日记] 启动补写检查异常：{type(e).__name__}: {e}")


# ------------------------------------------------------------------ 主人私聊指令

cmd = on_message(priority=21, block=False)


def _is_owner(user_id: str) -> bool:
    return str(user_id) == str(OWNER_QQ_ID)


def _state_panel() -> str:
    state = _load_state()
    on_off = "开" if _enabled(state) else "关"
    hh, mm = cfg.daily_diary.time
    diaries = _list_diaries(limit=100)
    latest = diaries[0]["date"] if diaries else "（还没写过）"
    return (
        f"🐟 **每日日记面板**\n"
        f"定时开关：{on_off}\n"
        f"定时：{hh:02d}:{mm:02d}（延迟 {cfg.daily_diary.delay_min}~{cfg.daily_diary.delay_max} 分钟，只存档不推送）\n"
        f"已存日记：{len(diaries)} 篇；最近一篇：{latest}\n"
        f"指令：`/写日记` 立即写 · `/日记 重写` 覆盖 · `/日记历史` 翻旧篇 · "
        f"`/日记 YYYY-MM-DD` 看某天 · `/日记开关 开/关`"
    )


@cmd.handle()
async def _(event: PrivateMessageEvent):
    if not _is_owner(str(event.user_id)):
        return
    text = event.get_plaintext().strip()
    if not text:
        return

    # 只接管日记相关前缀，避免拦截其他私聊指令
    if not (text.startswith("/日记") or text == "/写日记"):
        return

    state = _load_state()
    parts = text.split()

    # 开关
    if text.startswith("/日记开关"):
        arg = parts[-1].lower() if len(parts) > 1 else ""
        if arg in ("开", "on", "true", "1", "yes"):
            state["enabled"] = True
            _save_state(state)
            await cmd.finish("(*点头*) 日记定时打开啦，到点本鱼就偷偷写好存起来～")
            return
        if arg in ("关", "off", "false", "0", "no"):
            state["enabled"] = False
            _save_state(state)
            await cmd.finish("(*委屈*) 好吧，先不自动写日记；想开就发 `/日记开关 开`")
            return
        await cmd.finish(f"用法：`/日记开关 开` 或 `/日记开关 关`\n当前：{'开' if _enabled(state) else '关'}")
        return

    # 历史列表
    if text in ("/日记历史", "/日记记录"):
        diaries = _list_diaries(limit=10)
        if not diaries:
            await cmd.finish("(*挠头*) 还没写过日记捏，先发 `/写日记` 试试嘛～")
            return
        lines = [f"📔 {d['date']}（{d['chars']} 字）\n{d['preview']}" for d in diaries]
        await cmd.finish("🐟 **最近日记**\n" + "\n\n".join(lines))
        return

    # 指定日期回看：/日记 YYYY-MM-DD
    if text.startswith("/日记 ") and len(parts) == 2 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[1]):
        data = _load_diary(parts[1])
        if not data:
            await cmd.finish(f"(*翻遍本子*) {parts[1]} 这天没写日记捏…")
            return
        await cmd.finish(f"📔 **{data.get('date', parts[1])} 的日记**\n{data.get('body', '')}")
        return

    # 立即写 / 重写
    if text in ("/写日记", "/日记 写", "/日记 今天"):
        body, status = await _write_diary(force=False)
        if status == "existed":
            await cmd.finish(
                "(*抱本子*) 今天的日记已经写好啦，发 `/日记` 看面板，想看全文直接说日期；"
                "想让本鱼重写就发 `/日记 重写`"
            )
            return
        if status == "saved":
            await cmd.finish("(*把本子合上塞进抽屉*) 今天的日记写好存起来啦，主人想看随时叫本鱼拿出来～")
            return
        if status == "running":
            await cmd.finish("(*埋头写字中*) 本鱼正在写今天的日记，主人稍等一下嘛…")
            return
        await cmd.finish("(*耷拉尾巴*) 这次没写出来，主人稍后再发 `/写日记` 试试…")
        return

    if text == "/日记 重写":
        body, status = await _write_diary(force=True)
        if status == "saved":
            await cmd.finish("(*擦掉重写*) 今天的日记已经覆盖好啦～")
            return
        if status == "running":
            await cmd.finish("(*埋头写字中*) 本鱼正在写，主人稍等…")
            return
        await cmd.finish("(*耷拉尾巴*) 重写没成功，主人稍后再试…")
        return

    # 裸指令 /日记 给面板
    if text == "/日记":
        await cmd.finish(_state_panel())
        return

    await cmd.finish(_state_panel())
