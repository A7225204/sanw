"""人物卡 + 关系记忆。

两类东西分开存（主人 2026-10-30 定的边界）：

  memory/character_cards.json  —— 静态客观档案（KV，像字典）
      称呼 / 性别 / 职业 / 作息 / 兴趣 / 语言习惯 / 性格
      不会因为今天聊两句就变；只在有新素材时增量刷新。

  memory/relation_memory.json  —— 动态主观感受
      好感 / 恩怨 / 互动模式
      随聊天和事件变化；截图里「对鲱鱼娘的态度：从嫌弃变关心」属于这里。

提炼素材：shared_memory.db 的 chat_history 中该 QQ 说过的话 + nick_names 的当前昵称。
一次提炼同时产出 card 和 relation，两个文件各自落盘。
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CARD_FILE = ROOT / "memory" / "character_cards.json"
REL_FILE = ROOT / "memory" / "relation_memory.json"
NICK_FILE = ROOT / "memory" / "nick_names.json"
DB = Path(r"C:\Users\Administrator\AppData\Roaming\dsh-pet-standalone-webm-chat\shared_memory.db")

CARD_FIELDS = ("称呼", "性别", "职业", "作息", "兴趣", "语言习惯", "性格")
MAX_MSGS = 60


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _load(path: Path) -> dict:
    try:
        d = json.loads(path.read_text(encoding="utf-8-sig"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save(path: Path, d: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def get_card(uid: str) -> dict | None:
    return _load(CARD_FILE).get(str(uid))


def get_relation(uid: str) -> dict | None:
    return _load(REL_FILE).get(str(uid))


def nickname_of(uid: str) -> str:
    """从 nick_names 里找这个 QQ 最近用的昵称（跨群取出现最多的）。"""
    best = ""
    try:
        data = _load(NICK_FILE)
    except Exception:  # noqa: BLE001
        return ""
    seen_best = -1
    for _scope, people in data.items():
        if not isinstance(people, dict):
            continue
        rec = people.get(str(uid))
        if isinstance(rec, dict) and int(rec.get("seen") or 0) > seen_best:
            seen_best = int(rec.get("seen") or 0)
            best = str(rec.get("current") or rec.get("first") or "")
    return best


def _messages_of(uid: str, limit: int = MAX_MSGS) -> list[str]:
    """该 QQ 最近说过的话（带 scope 前缀，方便判断语境）。"""
    if not DB.is_file():
        return []
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        rows = con.execute(
            "select scope, message from chat_history where user_id=? "
            "and message is not null and length(message) > 1 order by id desc limit ?",
            (str(uid), limit),
        ).fetchall()
        con.close()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for scope, msg in rows:
        who = "私聊" if str(scope).startswith("private") else "群"
        out.append(f"[{who}] {str(msg)[:120]}")
    return out


async def extract(uid: str) -> dict:
    """提炼一个人的人物卡 + 关系记忆。返回 {ok, card, relation, error}。"""
    from server import memory_growth as mg

    uid = str(uid).strip()
    if not uid:
        return {"ok": False, "error": "uid 为空"}
    msgs = _messages_of(uid)
    if not msgs:
        return {"ok": False, "error": "没有这个人的聊天记录"}
    nick = nickname_of(uid) or f"QQ{uid}"

    prompt = (
        f"下面是 QQ 用户「{nick}」（QQ {uid}）在群里/私聊里说过的话。\n"
        "请做两件事：\n\n"
        "一、提炼他的人物档案（客观、稳定的事实，像字典条目）\n"
        "  字段：称呼、性别、职业、作息、兴趣、语言习惯、性格\n"
        "  要求：只写有证据的；不确定的字段留空字符串；兴趣/语言习惯用数组。\n\n"
        "二、判断「蓝色大肥鱼」（一个傲娇嘴损的鲸鱼少女 bot）对他的关系感受\n"
        "  字段：好感（0-100 整数）、恩怨（数组，写具体事件/过节，没有就空数组）、"
        "互动模式（一句话，比如：互怼式、撒娇式、客气疏远）\n"
        "  要求：好感按互动语气判断，被骂/被无视就低，被夸/被投喂就高。\n\n"
        "只输出 JSON 对象：\n"
        '{"card":{"称呼":"","性别":"","职业":"","作息":"","兴趣":[],"语言习惯":[],"性格":""},'
        '"relation":{"好感":50,"恩怨":[],"互动模式":""}}\n\n'
        "他最近说的话：\n" + "\n".join(msgs)
    )
    raw = await mg._ask_llm(prompt)
    m = re.search(r"\{[\s\S]*\}", raw or "")
    if not m:
        return {"ok": False, "error": f"模型没输出 JSON：{str(raw)[:120]}"}
    try:
        data = json.loads(m.group(0))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"JSON 解析失败：{e}"}

    card_in = data.get("card") or {}
    rel_in = data.get("relation") or {}

    card = {k: card_in.get(k) or ([] if k in ("兴趣", "语言习惯") else "") for k in CARD_FIELDS}
    card.update({"uid": uid, "nickname": nick, "updatedAt": _now(),
                 "sourceCount": len(msgs)})
    cards = _load(CARD_FILE)
    cards[uid] = card
    _save(CARD_FILE, cards)

    try:
        aff = int(rel_in.get("好感") or 50)
    except Exception:  # noqa: BLE001
        aff = 50
    rel = {
        "uid": uid,
        "好感": max(0, min(100, aff)),
        "恩怨": [str(x)[:80] for x in (rel_in.get("恩怨") or [])][:8],
        "互动模式": str(rel_in.get("互动模式") or "")[:80],
        "updatedAt": _now(),
    }
    rels = _load(REL_FILE)
    rels[uid] = rel
    _save(REL_FILE, rels)
    return {"ok": True, "card": card, "relation": rel}


def card_summary(uid: str) -> str:
    """给星海节点用的一句话摘要。"""
    c = get_card(uid)
    if not c:
        return ""
    bits = [c.get("性格") or "", c.get("职业") or ""]
    bits = [b for b in bits if b]
    return "·".join(bits)[:40]
