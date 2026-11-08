"""群聊词条库：把群里反复出现的黑话 / 昵称 / 梗 / 缩写沉淀成结构化词条卡。

对标别人机器人的「词条记忆面板」：
- 每条消息先对已有词条做子串命中，命中即 `count + 1`、刷新 `last_seen`；
- 原始闲聊攒进 pending 缓冲，攒够 `LEXICON_BATCH_SIZE` 条（或定时兜底）调一次
  方舟 LLM 抽取新词与本群语境含义；
- 主人可手动 `/改词 词条 新含义`、`/删词 词条`；手动改过的词条（manual=1）
  后续 LLM 合并不会覆盖含义。

存储
----
- 词条表 `group_lexicon`：复用 `memory_manager.shared_memory.db`（SQLite，WAL）。
- 待抽取缓冲 `memory/lexicon_pending.json`（重启不丢，攒批阈值未到也不烧 token）。

边界
----
- 只收 `group_{gid}` scope；私聊 / global_learn 不建词条。
- 通用寒暄、动词、标点、机器人自称、系统指令一律不抽。
- `LEXICON_ENABLED=false` 时所有读写短路，行为退回旧版（只在内存上下文里记闲聊）。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from config import get_config  # noqa: E402
import memory_manager as mm  # noqa: E402
import token_meter  # noqa: E402  令牌用量累计（静默容错）

cfg = get_config()

DATA_DIR = ROOT / "memory"
PENDING_FILE = DATA_DIR / "lexicon_pending.json"
DAILY_FILE = DATA_DIR / "lexicon_daily.json"
DAILY_LIMIT = 4


def _daily_can(scope: str) -> bool:
    try:
        d = json.loads(DAILY_FILE.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    today = time.strftime("%Y-%m-%d")
    if d.get("date") != today:
        return True
    return int((d.get("scopes") or {}).get(scope, 0)) < DAILY_LIMIT


def _daily_mark(scope: str) -> None:
    try:
        d = json.loads(DAILY_FILE.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    today = time.strftime("%Y-%m-%d")
    if d.get("date") != today:
        d = {"date": today, "scopes": {}}
    scopes = d.setdefault("scopes", {})
    scopes[scope] = int(scopes.get(scope, 0)) + 1
    try:
        DAILY_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

# 昵称兜底入库时的固定含义；昵称是群友身份，不占黑话名额、不参与淘汰
NICKNAME_MEANING = "群友昵称"

# 机器人自称 / 系统指令 / 通用寒暄，一律不当成群内词条
_SELF_NAMES = {"鲸鱼娘", "大肥鱼", "本鱼", "鱼宝", "肥鱼", "示例角色"}
_COMMAND_WORDS = {
    "帮助", "指令", "状态", "status", "体检", "记忆", "学习", "档案", "频率", "回复频率",
    "余额", "测试", "日报", "签到", "赛马", "决斗", "查成分", "贴标签", "好感度",
    "桌宠", "偷了", "唱歌", "语音", "语音说", "发语音", "联网", "B站", "说说", "日记",
    "画图", "表情包", "逮捕", "恶搞", "打开", "告状", "被欺负了", "词库", "词典",
    "改词", "删词", "切换大脑", "切换画笔", "联网查", "记忆",
}
# 纯通用停用字 / 单字，不构成词条
_STOP_TERM_RE = re.compile(r"^[\W_]+$", re.UNICODE)
_PUNCT_RE = re.compile(r"[@#【】\[\]()（）《》「」『』、，。！？；：,.!?;:\-_~～…·\"'“”‘’*#/\\|]+")
_AT_PREFIX_RE = re.compile(r"^@")
_CHINESE_RE = re.compile(r"[\u4e00-\u9fa5]+")


# ---------------------------------------------------------------------------
# 开关与 scope 校验
# ---------------------------------------------------------------------------
def enabled() -> bool:
    return bool(cfg.lexicon.enabled)


def is_group_scope(scope: str) -> bool:
    return str(scope).startswith("group_")


def _guard(scope: str) -> bool:
    """开关关闭或非群 scope 时短路。返回 True 表示允许继续。"""
    return enabled() and is_group_scope(scope)


# ---------------------------------------------------------------------------
# SQLite 词条表
# ---------------------------------------------------------------------------
def init_table() -> None:
    mm.init_db()
    with mm._connect() as conn:
        c = conn.cursor()
        c.execute(
            """CREATE TABLE IF NOT EXISTS group_lexicon (
                   scope TEXT NOT NULL,
                   term TEXT NOT NULL,
                   meaning TEXT DEFAULT '',
                   count INTEGER DEFAULT 1,
                   manual INTEGER DEFAULT 0,
                   first_seen TEXT,
                   last_seen TEXT,
                   PRIMARY KEY (scope, term)
               )"""
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_lex_scope_count "
            "ON group_lexicon(scope, count DESC)"
        )
        conn.commit()


# ---------------------------------------------------------------------------
# 词条清洗 / 归一化
# ---------------------------------------------------------------------------
def _norm_term(term: str) -> str:
    t = _AT_PREFIX_RE.sub("", str(term or "")).strip()
    t = _PUNCT_RE.sub("", t).strip()
    # 英文 / 缩写归一为小写；中文保留原写法
    if t.isascii():
        t = t.lower()
    return t


def _valid_term(term: str) -> bool:
    term = _norm_term(term)
    if not term or len(term) < 2 or len(term) > 12:
        return False
    if _STOP_TERM_RE.match(term):
        return False
    if term in _SELF_NAMES or term in _COMMAND_WORDS:
        return False
    # 纯数字 / 纯标点不算词条
    if term.isdigit():
        return False
    # 纯通用单字组合（如 "一个"、"我们"）交给 LLM 过滤；这里只挡明显的停用词
    if term in {"一个", "一下", "什么", "怎么", "为什么", "这个", "那个", "我们", "你们", "他们"}:
        return False
    return True


def _clean_meaning(meaning: str) -> str:
    m = str(meaning or "").strip()
    m = _PUNCT_RE.sub("", m).strip()
    if not m or m in {"-", "—", "无", "没有", "?"}:
        return "-"
    return m[: cfg.lexicon.max_meaning_chars]


# ---------------------------------------------------------------------------
# 已有词条命中计数（每条群聊消息同步调用，成本极低）
# ---------------------------------------------------------------------------
def touch_terms(scope: str, text: str) -> int:
    """消息文本命中已有词条则计数 +1、刷新 last_seen。返回命中词条数。"""
    if not _guard(scope) or not text:
        return 0
    init_table()
    hits: list[str] = []
    now = datetime.now().isoformat(timespec="seconds")
    with mm._connect() as conn:
        c = conn.cursor()
        c.execute("SELECT term FROM group_lexicon WHERE scope = ?", (scope,))
        existing = [r[0] for r in c.fetchall()]
        for term in existing:
            if not term:
                continue
            if term in text:
                hits.append(term)
        for term in hits:
            c.execute(
                "UPDATE group_lexicon SET count = count + 1, last_seen = ? "
                "WHERE scope = ? AND term = ?",
                (now, scope, term),
            )
        conn.commit()
    return len(hits)


# ---------------------------------------------------------------------------
# pending 缓冲（攒批抽词）
# ---------------------------------------------------------------------------
def _load_pending() -> dict[str, Any]:
    try:
        data = json.loads(PENDING_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("scopes", {})
            data.setdefault("meta", {})
            return data
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return {"scopes": {}, "meta": {}}


def _save_pending(data: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PENDING_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PENDING_FILE)


def push_pending(scope: str, speaker: str, text: str) -> bool:
    """把一条群聊闲聊塞进抽词缓冲。返回是否已达到触发抽取的批次阈值。"""
    if not _guard(scope):
        return False
    text = str(text or "").strip()
    if not text or text.startswith("/"):
        return False
    # 图片 / 表情占位不抽词
    if text in {"[图片]", "[表情]", "[语音]", "[视频]", "[转发]"}:
        return False

    data = _load_pending()
    scopes = data.setdefault("scopes", {})
    bucket = scopes.setdefault(scope, [])
    bucket.append({
        "speaker": str(speaker or "群友")[:30],
        "text": text[:200],
        "ts": datetime.now().isoformat(timespec="seconds"),
    })
    # 缓冲上限：阈值的 3 倍，防止长期不触发时无限膨胀
    cap = cfg.lexicon.batch_size * 3
    if len(bucket) > cap:
        del bucket[: len(bucket) - cap]
    scopes[scope] = bucket
    _save_pending(data)
    return len(bucket) >= cfg.lexicon.batch_size


def pending_count(scope: str) -> int:
    if not _guard(scope):
        return 0
    data = _load_pending()
    return len(data.get("scopes", {}).get(scope, []))


def _consume_pending(scope: str) -> list[dict[str, Any]]:
    """取出并清空某 scope 的待抽取缓冲。"""
    data = _load_pending()
    scopes = data.setdefault("scopes", {})
    bucket = scopes.pop(scope, [])
    data.setdefault("meta", {})[scope] = {
        "last_extract": datetime.now().isoformat(timespec="seconds"),
    }
    _save_pending(data)
    return bucket


# ---------------------------------------------------------------------------
# LLM 抽词（直连方舟，不经过 call_ai_raw，避免循环依赖与旧签名参数问题）
# ---------------------------------------------------------------------------
_EXTRACT_PROMPT = (
    "你是群聊黑话整理助手。下面是同一个群最近的聊天记录（格式：昵称：内容）。\n"
    "请直接从中抽取本群专属的词条：群友昵称/网名、群内黑话/梗/缩写、专属名词、反复出现的代号。\n"
    "【强制】不要先写分析、不要写英文、不要复述本指令规则、不要列候选过程，读完记录后直接输出最终 JSON。\n"
    "要求：\n"
    "1. 只抽本群语境特有的词；不要抽通用寒暄、普通动词/形容词、标点、机器人自称、系统指令。\n"
    "2. 词条 2~12 字；英文缩写保留原样小写；不要重复下面已有的词条。\n"
    "3. 含义用 12~30 字口语解释，结合本群语境说清这个词在群里指什么；没把握就写 \"-\"。\n"
    "4. 只输出一个合法 JSON 对象，不要解释、不要 markdown 代码块、不要前后寒暄；\n"
    "5. 最多输出 8 条词条，按出现频率取最高的，多的一律不要。\n"
    "   实在没有可抽的词条就输出 {{\"terms\":[]}}。\n"
    '输出格式：{{"terms":[{{"term":"...","meaning":"..."}}]}}\n\n'
    "已有词条：{existing}\n\n"
    "聊天记录：\n{batch}\n"
)


def _extract_json(s: str) -> Optional[dict]:
    if not s:
        return None
    s = re.sub(r"```(?:json)?", "", s).strip().strip("`")
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j != -1:
        try:
            return json.loads(s[i: j + 1])
        except Exception:
            pass
    return None


async def _call_llm(prompt: str) -> str:
    providers: list[tuple[str, str, str, str]] = []
    ds_key = os.getenv("DEEPSEEK_API_KEY", "")
    if ds_key:
        ds_base = (os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").rstrip("/")
        ds_model = os.getenv("DEEPSEEK_MODEL") or "deepseek-chat"
        providers.append((ds_base + "/chat/completions", ds_key, ds_model, "deepseek"))
    if cfg.ai.api_key and cfg.ai.model and os.getenv("ARK_FALLBACK_ENABLED", "false").lower() in ("1", "true", "yes"):
        providers.append((cfg.ai.endpoint(), cfg.ai.api_key, cfg.ai.model, "ark"))
    if not providers:
        raise RuntimeError("[群聊词库] 没有可用的 LLM 配置")

    last_err = ""
    async with httpx.AsyncClient(timeout=cfg.ai.timeout) as client:
        for url, key, model, provider in providers:
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "max_tokens": 400,
            }
            if provider == "ark":
                payload["thinking"] = {"type": "disabled"}
            for attempt in range(cfg.ai.max_retries):
                try:
                    r = await client.post(url, headers={"Authorization": f"Bearer {key}"}, json=payload)
                    data = r.json()
                    if isinstance(data, dict) and data.get("choices"):
                        message = data["choices"][0].get("message") or {}
                        finish = data["choices"][0].get("finish_reason")
                        usage = data.get("usage", {}) or {}
                        token_meter.record(usage, "lexicon", provider=provider)
                        content = (message.get("content") or "").strip()
                        if not content:
                            raise RuntimeError(
                                f"LLM 未产出正文（finish={finish}, usage={usage}）"
                            )
                        return content
                    last_err = f"{provider} HTTP {r.status_code} {str(data)[:160]}"
                except Exception as e:  # noqa: BLE001
                    last_err = f"{provider} {type(e).__name__}: {e}"
                await asyncio.sleep(1.0)
    raise RuntimeError(f"[群聊词库 LLM 调用失败] {last_err}")


def _existing_terms(scope: str) -> list[str]:
    init_table()
    with mm._connect() as conn:
        c = conn.cursor()
        c.execute("SELECT term FROM group_lexicon WHERE scope = ? ORDER BY count DESC", (scope,))
        return [r[0] for r in c.fetchall()]


def _upsert_term(scope: str, term: str, meaning: str, manual: bool = False) -> str:
    """写入或更新词条。返回动作：added / updated / skipped。"""
    init_table()
    now = datetime.now().isoformat(timespec="seconds")
    clean_meaning = _clean_meaning(meaning)
    action = "skipped"
    with mm._connect() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT count, manual FROM group_lexicon WHERE scope = ? AND term = ?",
            (scope, term),
        )
        row = c.fetchone()
        if row is None:
            c.execute(
                "INSERT INTO group_lexicon (scope, term, meaning, count, manual, first_seen, last_seen) "
                "VALUES (?, ?, ?, 1, ?, ?, ?)",
                (scope, term, clean_meaning, 1 if manual else 0, now, now),
            )
            action = "added"
        else:
            # 已存在：手动词条含义不被覆盖；非手动可补写含义（空含义时回填）
            was_manual = int(row[1] or 0)
            if manual:
                c.execute(
                    "UPDATE group_lexicon SET meaning = ?, manual = 1, last_seen = ? "
                    "WHERE scope = ? AND term = ?",
                    (clean_meaning, now, scope, term),
                )
                action = "updated"
            elif was_manual:
                c.execute(
                    "UPDATE group_lexicon SET last_seen = ? WHERE scope = ? AND term = ?",
                    (now, scope, term),
                )
                action = "skipped"
            else:
                c.execute(
                    "UPDATE group_lexicon SET meaning = ?, last_seen = ? "
                    "WHERE scope = ? AND term = ? AND manual = 0",
                    (clean_meaning, now, scope, term),
                )
                action = "updated"
    # 事务提交后再淘汰超上限词条，避免另开连接读不到未提交 INSERT
    if action == "added":
        _evict(scope)
    return action


def _consolidate_nicknames(scope: str, batch: list[dict[str, Any]], result: dict[str, Any]) -> None:
    """昵称兜底入库：群友昵称是确定性数据，不依赖 LLM 判断，先沉淀进词库，
    保证 AI 抽词失败时面板也不会是空的。只统计 added / updated，不重复计数 skipped。"""
    seen: set[str] = set()
    for item in batch:
        speaker = str(item.get("speaker") or "").strip()
        if not speaker:
            continue
        # 昵称里常带括号备注（如「大肥鱼（mimo版，不是本人）」），主名与整名各试一次
        base = re.split(r"[（(【\[]", speaker)[0].strip()
        for candidate in (speaker, base):
            term = _norm_term(candidate)
            if not term or term in seen:
                continue
            seen.add(term)
            if not _valid_term(term):
                continue
            action = _upsert_term(scope, term, NICKNAME_MEANING)
            if action == "added":
                result["added"] += 1
            elif action == "updated":
                result["updated"] += 1


def _evict(scope: str) -> int:
    """超出每群黑话上限时，淘汰频次最低、最久未遇见的非手动、非昵称词条。
    昵称（群友身份）与手动词条不占名额、不被淘汰。返回删除条数。"""
    limit = cfg.lexicon.max_terms_per_group
    with mm._connect() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT COUNT(*) FROM group_lexicon WHERE scope = ? AND manual = 0 AND meaning != ?",
            (scope, NICKNAME_MEANING),
        )
        total = c.fetchone()[0]
        if total <= limit:
            return 0
        drop = total - limit
        c.execute(
            "SELECT term FROM group_lexicon WHERE scope = ? AND manual = 0 AND meaning != ? "
            "ORDER BY count ASC, last_seen ASC, rowid ASC LIMIT ?",
            (scope, NICKNAME_MEANING, drop),
        )
        doomed = [r[0] for r in c.fetchall()]
        for term in doomed:
            c.execute(
                "DELETE FROM group_lexicon WHERE scope = ? AND term = ?",
                (scope, term),
            )
        conn.commit()
    return len(doomed)


async def run_extract(scope: str) -> dict[str, Any]:
    """抽取一个群缓冲里的新词条。返回 {batch, added, updated, skipped, evicted}。

    由消息触发（攒够批次）或定时兜底调用；失败时清空半批缓冲，避免反复烧 token。
    """
    result: dict[str, Any] = {"scope": scope, "batch": 0, "added": 0,
                              "updated": 0, "skipped": 0, "evicted": 0,
                              "enabled": enabled(), "error": ""}
    if not _guard(scope):
        return result
    if not _daily_can(scope):
        result["error"] = "今日抽取已达上限"
        return result

    # 前置 IO/DB 也要兜底：这几步原来在 try 之外，异常会让后台 task
    # 静默失败（审计 C：只留 "Task exception was never retrieved"）。
    try:
        batch = _consume_pending(scope)
        result["batch"] = len(batch)
        if not batch:
            return result
        _daily_mark(scope)
        # 昵称确定性兜底：无论 AI 抽词是否成功，先把本轮发言群友昵称沉淀入库。
        _consolidate_nicknames(scope, batch, result)
    except Exception as e:  # noqa: BLE001
        result["error"] = f"前置处理失败：{type(e).__name__}: {e}"
        try:
            logger.warning(f"[词库] {scope} 前置处理失败：{type(e).__name__}: {e}")
        except Exception:  # noqa: BLE001
            pass
        return result

    if not cfg.lexicon.extract_ai_enabled:
        return result

    lines = [f"{item['speaker']}：{str(item['text'])[:80]}" for item in batch][:20]
    existing = _existing_terms(scope)
    prompt = _EXTRACT_PROMPT.format(
        existing="、".join(existing[:30]) if existing else "（无）",
        batch="\n".join(lines),
    )
    try:
        content = await _call_llm(prompt)
        parsed = _extract_json(content)
        terms: list[dict[str, str]] = []
        if isinstance(parsed, dict) and isinstance(parsed.get("terms"), list):
            terms = [t for t in parsed["terms"] if isinstance(t, dict)]
        elif parsed is None:
            # 调通了但没给出合法 JSON：留痕，让定时兜底日志能看到失败原因，不再静默 0。
            result["error"] = f"LLM 未返回合法 JSON: {content[:120]!r}"
        for raw in terms:
            term = _norm_term(str(raw.get("term") or ""))
            meaning = str(raw.get("meaning") or "")
            if not _valid_term(term) or term in existing:
                result["skipped"] += 1
                continue
            action = _upsert_term(scope, term, meaning)
            if action == "added":
                result["added"] += 1
            elif action == "updated":
                result["updated"] += 1
            else:
                result["skipped"] += 1
            existing.append(term)
        result["evicted"] = _evict(scope)
    except Exception as e:  # noqa: BLE001
        result["error"] = f"{type(e).__name__}: {e}"[:200]
    return result


async def consolidate_pending() -> list[dict[str, Any]]:
    """定时兜底：遍历所有攒了缓冲的群，逐个抽词。返回各群结果列表。"""
    if not enabled():
        return []
    data = _load_pending()
    scopes = list(data.get("scopes", {}).keys())
    results: list[dict[str, Any]] = []
    for scope in scopes:
        if not is_group_scope(scope):
            continue
        # 兜底任务也只在攒够一批时抽，避免半批也调用 LLM 白烧钱
        if len(data.get("scopes", {}).get(scope) or []) < cfg.lexicon.batch_size:
            continue
        try:
            results.append(await run_extract(scope))
        except Exception as e:  # noqa: BLE001
            results.append({"scope": scope, "error": f"{type(e).__name__}: {e}"[:200]})
    return results


# ---------------------------------------------------------------------------
# 查询 / 手动改删（仅主人在指令层鉴权，模块内只做数据操作）
# ---------------------------------------------------------------------------
def list_terms(scope: str, limit: Optional[int] = None) -> list[dict[str, Any]]:
    if not _guard(scope):
        return []
    init_table()
    with mm._connect() as conn:
        c = conn.cursor()
        sql = "SELECT term, meaning, count, manual, last_seen FROM group_lexicon WHERE scope = ?"
        params: list[Any] = [scope]
        sql += " ORDER BY count DESC, term ASC"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        rows = c.execute(sql, params).fetchall()
    return [
        {
            "term": r[0],
            "meaning": r[1] or "-",
            "count": int(r[2] or 0),
            "manual": bool(r[3]),
            "last_seen": r[4] or "",
        }
        for r in rows
    ]


def get_term(scope: str, term: str) -> Optional[dict[str, Any]]:
    if not _guard(scope):
        return None
    init_table()
    key = _norm_term(term)
    with mm._connect() as conn:
        c = conn.cursor()
        row = c.execute(
            "SELECT term, meaning, count, manual, last_seen FROM group_lexicon "
            "WHERE scope = ? AND term = ?",
            (scope, key),
        ).fetchone()
    if not row:
        return None
    return {
        "term": row[0], "meaning": row[1] or "-", "count": int(row[2] or 0),
        "manual": bool(row[3]), "last_seen": row[4] or "",
    }


def set_meaning(scope: str, term: str, meaning: str) -> bool:
    """主人手动改含义，置 manual=1。返回词条是否存在。"""
    if not _guard(scope):
        return False
    init_table()
    key = _norm_term(term)
    clean = _clean_meaning(meaning)
    with mm._connect() as conn:
        c = conn.cursor()
        row = c.execute(
            "SELECT term FROM group_lexicon WHERE scope = ? AND term = ?",
            (scope, key),
        ).fetchone()
        if not row:
            return False
        c.execute(
            "UPDATE group_lexicon SET meaning = ?, manual = 1, last_seen = ? "
            "WHERE scope = ? AND term = ?",
            (clean, datetime.now().isoformat(timespec="seconds"), scope, key),
        )
        conn.commit()
    return True


def delete_term(scope: str, term: str) -> bool:
    """主人删词条。返回是否删到了条目。"""
    if not _guard(scope):
        return False
    init_table()
    key = _norm_term(term)
    with mm._connect() as conn:
        c = conn.cursor()
        cur = c.execute(
            "DELETE FROM group_lexicon WHERE scope = ? AND term = ?",
            (scope, key),
        )
        conn.commit()
        return cur.rowcount > 0


def count_terms(scope: str) -> int:
    if not _guard(scope):
        return 0
    init_table()
    with mm._connect() as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM group_lexicon WHERE scope = ?", (scope,))
        return int(c.fetchone()[0])


def top_terms(limit: int = 10, scope: Optional[str] = None) -> list[dict[str, Any]]:
    """跨群（scope=None）或单群按遇见次数倒序取 top 词条。"""
    if not enabled():
        return []
    init_table()
    with mm._connect() as conn:
        c = conn.cursor()
        if scope:
            c.execute(
                "SELECT scope, term, meaning, count, manual, last_seen "
                "FROM group_lexicon WHERE scope = ? ORDER BY count DESC, term ASC LIMIT ?",
                (scope, int(limit)),
            )
        else:
            c.execute(
                "SELECT scope, term, meaning, count, manual, last_seen "
                "FROM group_lexicon WHERE scope LIKE 'group_%' "
                "ORDER BY count DESC, term ASC LIMIT ?",
                (int(limit),),
            )
        rows = c.fetchall()
    return [
        {
            "scope": r[0], "term": r[1], "meaning": r[2] or "-",
            "count": int(r[3] or 0), "manual": bool(r[4]), "last_seen": r[5] or "",
        }
        for r in rows
    ]


def stats_all() -> dict[str, Any]:
    """全库汇总：各群词条数 / 遇见次数合计、总词条、总次数。"""
    if not enabled():
        return {"enabled": False, "groups": [], "total_terms": 0, "total_mentions": 0}
    init_table()
    groups: list[dict[str, Any]] = []
    total_terms = 0
    total_mentions = 0
    with mm._connect() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT scope, COUNT(*), COALESCE(SUM(count), 0) FROM group_lexicon "
            "WHERE scope LIKE 'group_%' GROUP BY scope ORDER BY COUNT(*) DESC"
        )
        for scope, n, mentions in c.fetchall():
            groups.append({"scope": scope, "terms": int(n), "mentions": int(mentions)})
            total_terms += int(n)
            total_mentions += int(mentions)
    return {
        "enabled": True,
        "groups": groups,
        "total_terms": total_terms,
        "total_mentions": total_mentions,
    }


# 黑话的确认度分级（2026-11-07，搬自 MaiBot 的 jargon_miner）
#   它那边 JARGON_INFERENCE_THRESHOLDS = [4, 8, 25, 100]：
#   出现 4 次才开始当回事，8 次较确定，25 次确定，100 次铁定。
# 我们原来只有 count 计数没有分级 —— 出现一次和出现一百次在 prompt 里长得一样。
# 分级解决一个真问题：新冒出来的词瞎用会显得硬凑，老词不用又不像这个群的。
CONF_LEVELS = [(100, "铁定"), (25, "确定"), (8, "较确定"), (4, "刚冒头")]


def confidence(count: int) -> str:
    """按出现次数给确认度。"""
    try:
        n = int(count or 0)
    except (TypeError, ValueError):
        n = 0
    for thr, name in CONF_LEVELS:
        if n >= thr:
            return name
    return "偶见"


def brief_hint(count: int) -> str:
    """给 prompt 的一句前缀（只有不确定的才提示，铁定的不加噪）。"""
    c = confidence(count)
    if c in ("铁定", "确定"):
        return ""
    if c == "较确定":
        return "，能用但别用力"
    return "，**刚冒头，别当成熟词用**"


def lexicon_brief(scope: str, top_n: Optional[int] = None) -> str:
    """拼进群聊 prompt 的本群黑话小抄；空库返回空串。

    2026-11-07：每条带上「确认度·次数」，刚冒头的词会提示别当成熟词用。
    """
    if not _guard(scope):
        return ""
    top = top_n or 5
    terms = [t for t in list_terms(scope, limit=top * 3)
             if "昵称" not in str(t.get("meaning") or "")][:top]
    if not terms:
        return ""
    out = []
    for t in terms:
        n = int(t.get("count") or 0)
        out.append(f"- {t['term']}：{t['meaning']}（{confidence(n)}·{n}次{brief_hint(n)}）")
    return (
        "【本群黑话词典（能用才用，别硬塞）】\n"
        "规则：一条回复最多自然用 1 个词；不要连续两条回复用同一个词；"
        "用不上就正常说话，别为了显得合群把词条堆进去；也别逐条念解释。\n"
        "括号里是这个群用过多少次 + 确认度 —— **次数少的是刚冒出来的，别当成熟词用**；"
        "次数多的可以放心自然用。\n"
        + "\n".join(out)
    )
