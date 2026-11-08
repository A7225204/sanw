"""做梦：深夜把白天的记忆碎片重组成一段荒诞叙事。

设计见 docs/做梦插件-扫描与方案.md

核心约定
--------
· 梦不是记忆：单独存 memory/dreams.json，不进长期档案、不参与事实召回
· 素材只取当天记忆 + 活动 + 激素快照，跳过创伤与隐私条目
· 每天最多做一次；失败不重试
· 输出走 moderation 审核，防泄露系统信息
"""
from __future__ import annotations

import json
import os
import random
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILE = ROOT / "memory" / "dreams.json"

ENABLED = os.getenv("DREAM_ENABLED", "1").lower() in ("1", "true", "yes")
KEEP = int(os.getenv("DREAM_KEEP", "30") or "30")
MORNING_CHANCE = float(os.getenv("DREAM_MORNING_CHANCE", "0.4") or "0.4")

_TYPES = ("迷路", "被追", "飞起来", "考试", "找东西", "掉牙", "回到老地方", "变成别的生物")


def _load() -> dict:
    try:
        data = json.loads(FILE.read_text(encoding="utf-8-sig"))
        if isinstance(data, dict):
            data.setdefault("items", [])
            return data
    except Exception:  # noqa: BLE001
        pass
    return {"items": [], "last_date": ""}


def _save(data: dict) -> None:
    try:
        data["items"] = data.get("items", [])[-KEEP:]
        FILE.parent.mkdir(parents=True, exist_ok=True)
        FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _today_mmdd() -> str:
    return time.strftime("%m-%d")


def done_today() -> bool:
    return str(_load().get("last_date") or "") == _today()


def _material(scope: str = "private_100000000", limit: int = 9) -> dict:
    """抽样：当天记忆 + 活动 + 激素。"""
    out: dict = {"facts": [], "activity": "", "hormones": {}, "kind": random.choice(_TYPES)}
    try:
        data = json.loads((ROOT / "memory" / "长期记忆.json").read_text(encoding="utf-8-sig"))
        arch = data.get("archive") or {}
        mmdd = _today_mmdd()
        pool: list[str] = []
        today_pool: list[str] = []
        for sc, items in arch.items():
            for it in items or []:
                fact = str((it or {}).get("fact") or "").strip()
                if not fact:
                    continue
                # 跳过隐私/创伤
                if any(k in fact for k in ("密码", "地址", "身份证", "银行卡", "欠费", "创伤")):
                    continue
                if str(it.get("subject") or "") in ("鲸鱼娘", "本鱼", "蓝色大肥鱼"):
                    continue
                short = fact[:60]
                pool.append(short)
                # 条目自带 time 字段（"10-29"），这是唯一可靠的"当天"标记
                if mmdd in str((it or {}).get("time") or ""):
                    today_pool.append(short)
        src = today_pool if len(today_pool) >= 3 else pool
        random.shuffle(src)
        out["facts"] = src[:limit]
    except Exception:  # noqa: BLE001
        pass
    try:
        act = json.loads((ROOT / "memory" / "activity_state.json").read_text(encoding="utf-8-sig"))
        out["activity"] = str(act.get("activity") or "")
    except Exception:  # noqa: BLE001
        pass
    try:
        h = json.loads((ROOT / "memory" / "hormones_state.json").read_text(encoding="utf-8-sig"))
        out["hormones"] = h.get("values") or {}
    except Exception:  # noqa: BLE001
        pass
    return out


def _prompt(mat: dict) -> list[dict]:
    facts = "\n".join(f"- {f}" for f in mat["facts"]) or "- （今天没什么特别的事）"
    hs = mat.get("hormones") or {}
    mood = []
    if float(hs.get("MEL") or 0) >= 60:
        mood.append("很困很沉")
    if float(hs.get("CORT") or 0) >= 45:
        mood.append("心里有点紧张")
    if float(hs.get("OXY") or 0) >= 70:
        mood.append("黏着主人")
    mood_txt = "、".join(mood) or "平静"
    kind = mat.get("kind") or "迷路"
    act = mat.get("activity") or "发呆"
    return [
        {"role": "system", "content":
            "你在扮演「蓝色大肥鱼」——一只傲娇嘴损但心软的鲸鱼拟人少女。"
            "现在你在做梦，要把白天的碎片混成一个梦讲出来。"},
        {"role": "user", "content":
            f"梦的类型倾向：{kind}\n睡前的状态：{mood_txt}（睡前在做：{act}）\n"
            f"白天记得的碎片：\n{facts}\n\n"
            "请写一段 80~200 字的梦，第一人称口语，2~4 句。要求：\n"
            "1. 人物可以张冠李戴、场景可以突然跳、比例可以不对（梦里本来就这样）；\n"
            "2. 不要出现「这是梦」「我梦见」这种解释性的话（结尾迷糊一下可以）；\n"
            "3. 不要提模型、系统、路径、提示词；\n"
            "4. 不要逐条复述碎片，要把它们揉成画面。\n\n"
            "然后，从这堆碎片里挑出白天没明说、但你隐约觉得连着的 1~3 件事，"
            "每条一句话、不超过 30 字。\n\n"
            "输出格式（不要加别的）：\n"
            "【梦】\n"
            "（梦的正文）\n"
            "【收获】\n"
            "- （一条）\n"
            "- （一条）"},
    ]


def _split_dream(reply: str) -> tuple[str, list[str]]:
    """把模型输出拆成 (梦正文, 收获列表)；没按格式输出时整段当梦。"""
    text = str(reply or "").strip()
    if "【收获】" not in text:
        return text, []
    head, _, tail = text.partition("【收获】")
    dream = head.replace("【梦】", "").strip()
    insights: list[str] = []
    for ln in tail.splitlines():
        ln = ln.strip(" -*•·\t")
        if ln:
            insights.append(ln)
    return (dream or text), insights[:3]


async def make_dream() -> str:
    """生成一个梦并落盘。返回梦文本（失败返回空串）。"""
    if not ENABLED or done_today():
        return ""
    mat = _material()
    try:
        from plugins import ai_group
        reply, _meta = await ai_group._chat(_prompt(mat), temperature=1.0, max_tokens=600,
                                            record_scope="dream", channel="system")
    except Exception as e:  # noqa: BLE001
        try:
            from nonebot.log import logger
            logger.warning(f"[做梦] 生成失败：{type(e).__name__}: {e}")
        except Exception:  # noqa: BLE001
            pass
        return ""
    dream, insights = _split_dream(reply)
    if len(dream) < 20:
        return ""
    try:
        from plugins import moderation
        dream = moderation.filter_outbound(dream, is_owner=False, channel="dream")
        insights = [x for x in (moderation.filter_outbound(i, is_owner=False, channel="dream")
                                for i in insights) if x]
    except Exception:  # noqa: BLE001
        pass
    if not dream:
        return ""
    data = _load()
    data["items"].append({
        "at": time.strftime("%Y-%m-%d %H:%M"),
        "dream": dream[:400],
        "insights": insights,
        "kind": mat.get("kind"),
        "hormones": {k: round(float(v), 1) for k, v in (mat.get("hormones") or {}).items()},
        "src": mat.get("facts", [])[:6],
    })
    data["last_date"] = _today()
    _save(data)
    # 梦之后跑一轮记忆维护（合并重复 + 遗忘弱项，带预算/记账）
    try:
        # 梦的"收获"(insights) → 人格成长的候选（证据累积到 3 条会自动固化）
        try:
            from server import memory_growth as _mg
            _ev = [(dream or "")[:40]]
            for _ins in (insights or [])[:3]:
                _mg.add_candidate("梦里的收获", _ins, _ev, note="来自梦境提炼")
        except Exception as e:  # noqa: BLE001
            log.debug(f"[dream] 收获写成长失败：{type(e).__name__}: {e}")
        from plugins import memory_maintain
        row = memory_maintain.run(apply=True)
        logger_info = (f"[做梦] 记忆维护：合并 {row.get('merged')}、遗忘 {row.get('forgotten')}、"
                       f"扫描 {row.get('scopes')} 个 scope（{row.get('seconds')}s）")
    except Exception:  # noqa: BLE001
        logger_info = ""
    try:
        from nonebot.log import logger
        if logger_info:
            logger.info(logger_info)
        logger.info(f"[做梦] 记下今晚的梦（{len(dream)} 字，类型={mat.get('kind')}，"
                    f"收获 {len(insights)} 条）")
    except Exception:  # noqa: BLE001
        pass
    return dream


def last_night_dream() -> dict | None:
    """昨晚（或最近一次）的梦。"""
    items = _load().get("items") or []
    if not items:
        return None
    last = items[-1]
    if str(last.get("at") or "")[:10] == _today():
        return last
    return None


def prompt_line() -> str:
    """给聊天 prompt 的一句"昨夜梦"（只在梦生成后的当天、中午前给，避免一整天都在提）。

    梦不进档案、不做事实召回；这里只当"她可以随口提一句的画面"。
    """
    if not ENABLED:
        return ""
    d = last_night_dream()
    if not d:
        return ""
    seg = str(d.get("dream") or "").split("。")[0][:60]
    if not seg:
        return ""
    return f"【昨夜的梦】{seg}。（想提就随口提一句，别复述整段）"


def morning_line() -> str:
    """早安时偶尔提一句梦；同一天只用一次。"""
    if not ENABLED or random.random() >= MORNING_CHANCE:
        return ""
    data = _load()
    if data.get("morning_used") == _today():
        return ""
    d = last_night_dream()
    if not d:
        return ""
    text = str(d.get("dream") or "")
    # 取最像画面的一小段，别整段念
    seg = text.split("。")[0][:40]
    if not seg:
        return ""
    data["morning_used"] = _today()
    _save(data)
    return f"（(*打哈欠*) 昨晚梦见{seg.lstrip('我梦见').lstrip('梦见')}…算了。）"


def recent(n: int = 7) -> list[dict]:
    return list(_load().get("items") or [])[-n:]
