"""人格成长：从最近记忆里提炼「她这段时间的变化」。

流程
----
一次提炼产 1~3 条 pending 变化，每条带证据记忆 id。
  pending    → 亮球（待确认）
  solidified → 固化：写进成长层，会拼进 prompt（绿球）
  dismissed  → 删除：保留记录但不发光（灰白球）

数据落在 memory/persona_growth.json，和 long_memory 一样是纯 JSON，可回滚。
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILE = ROOT / "memory" / "persona_growth.json"

MAX_ITEMS = 60          # 最多存这么多条，超了淘汰最老的
MAX_PER_RUN = 3         # 一次提炼最多出几条
MAX_SOLID = 12          # 固化上限，避免 prompt 膨胀
EXTRACT_LIMIT = 40      # 喂给模型的记忆条数
# 人格升级门槛（借鉴 SeekMemory 的 Self Impression：≥3 条独立证据才配升级）
MIN_EVIDENCE = int(os.getenv("GROWTH_MIN_EVIDENCE", "3") or "3")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _load() -> dict:
    try:
        d = json.loads(FILE.read_text(encoding="utf-8-sig"))
        if isinstance(d, dict):
            d.setdefault("items", [])
            return d
    except Exception:  # noqa: BLE001
        pass
    return {"version": 1, "items": []}


def _save(d: dict) -> None:
    d["updatedAt"] = _now()
    FILE.parent.mkdir(parents=True, exist_ok=True)
    FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def get(item_id: str) -> dict | None:
    """按 id 取一条（给接口返回 gate 原因用）。"""
    return _find(_load(), item_id)


def list_items() -> list[dict]:
    return list(_load().get("items") or [])


def _find(d: dict, item_id: str) -> dict | None:
    for it in d.get("items") or []:
        if it.get("id") == item_id:
            return it
    return None


# 2026-11-07：人格成长的候选必须是「关于她自己 / 主人」的。
# 事故：群里有个用户叫「小白」，他说"名字是大哥起的，不跟着改名，怕大哥叫不着"，
# 这条被提炼成了人格成长「怕改名后大哥在群里喊不着我」—— 大哥是小白的大哥，跟她无关。
# 这里加一道内容自检：洞察里出现"别人的名字 + 别人的关系"就拒。
_FOREIGN_RE = re.compile(
    # 别人的家人/关系（她只认主人）
    r"(他|她|ta)的(大哥|大姐|老板|爸|妈|老婆|老公|对象|儿子|女儿|兄弟|姐妹)"
    # 明说"别人"的
    r"|群友.{0,6}(改名|大哥|家里|老板)"
    , re.I)


def _is_foreign_insight(value: str, evidence: list[str]) -> tuple[bool, str]:
    """这条洞察是不是在说别人。返回 (是否外来, 原因)。"""
    v = str(value or "")
    if _FOREIGN_RE.search(v):
        return True, "洞察里出现别人的关系"
    # 证据里要是全都指向某个人（不是主人/本鱼），也拒
    if evidence:
        ev = " ".join(str(x) for x in evidence)
        if ("大哥" in ev or "大姐" in ev) and "主人" not in ev and "本鱼" not in ev:
            return True, "证据指向别人家的关系"
    return False, ""


def add_candidate(trait: str, value: str, evidence: list[str], note: str = "") -> dict | None:
    """追加一条"待定成长"（梦/念头这类内部素材产出的自我认识）。

    同一个 trait + value 已存在 → 合并证据；否则新建 pending。
    之后由 auto_solidify（证据≥3）或面板手动固化。

    2026-11-07：加内容自检 —— 关于别人的事不许进人格成长。
    """
    trait = str(trait or "").strip()[:20]
    value = str(value or "").strip()[:120]
    if not value:
        return None
    try:
        foreign, why = _is_foreign_insight(value, evidence)
        if foreign:
            from loguru import logger as _lg
            _lg.info(f"[成长] 拒绝一条候选（{why}）：{value[:36]}")
            return None
    except Exception:  # noqa: BLE001
        pass
    d = _load()
    ev = [str(x).strip()[:80] for x in (evidence or []) if str(x).strip()]
    for it in d.get("items") or []:
        if it.get("trait") == trait and it.get("value") == value:
            merged = list(dict.fromkeys(list(it.get("evidence") or []) + ev))[-6:]
            it["evidence"] = merged
            _save(d)
            return it
    import hashlib as _h
    item = {
        "id": "growth:" + _h.md5((trait + value).encode("utf-8")).hexdigest()[:10],
        "trait": trait, "value": value, "evidence": ev,
        "status": "pending", "since": _now(), "t": int(time.time() * 1000),
        "note": str(note or "")[:80],
    }
    d.setdefault("items", []).append(item)
    _save(d)
    return item


def solidify(item_id: str) -> dict | None:
    d = _load()
    it = _find(d, item_id)
    if not it:
        return None
    # 证据门槛：只做"提醒"，不拦手动固化（主人点了就是他的决定）；
    # 想强制拦截把 GROWTH_GATE_ENFORCE 设为 1（自动升级路径用）。
    ev = [str(x).strip() for x in (it.get("evidence") or []) if str(x).strip()]
    _enforce = os.getenv("GROWTH_GATE_ENFORCE", "0") in ("1", "true", "on")
    if len(ev) < MIN_EVIDENCE and _enforce:
        it["gate"] = f"证据不足（{len(ev)}/{MIN_EVIDENCE} 条），先多观察几轮"
        _save(d)
        return None
    if len(ev) < MIN_EVIDENCE:
        it["gate_warn"] = f"证据偏少（{len(ev)}/{MIN_EVIDENCE} 条）"
    it.pop("gate", None)
    # 版本链：固化前留一份旧内容（同一 trait 再次固化时能追溯/回滚）
    try:
        revlog = list(it.get("revlog") or [])
        if it.get("value"):
            revlog.append({"rev": int(it.get("rev") or 1), "value": str(it["value"])[:300],
                           "trait": str(it.get("trait") or ""), "ts": it.get("solidifiedAt") or _now()})
        it["revlog"] = revlog[-5:]
        it["rev"] = int(it.get("rev") or 1) + 1
    except Exception:  # noqa: BLE001
        pass
    solid = [x for x in d["items"] if x.get("status") == "solidified" and x.get("id") != item_id]
    if len(solid) >= MAX_SOLID:
        # 淘汰最老的固化项，并把它的状态标成 archived
        solid.sort(key=lambda x: x.get("solidifiedAt") or "")
        oldest = solid[0]
        oldest["status"] = "archived"
    it["status"] = "solidified"
    it["solidifiedAt"] = _now()
    _save(d)
    return it


def auto_solidify(min_evidence: int | None = None) -> list[dict]:
    """证据够的 pending 自动固化（主人要求：证据多的就自动上，不用点）。

    规则：证据 ≥ AUTO_SOLID_EVIDENCE（默认 3）且状态还是 pending → 直接固化。
    手动点按钮不受影响（手动永远生效）。
    """
    need = int(min_evidence if min_evidence is not None else
               os.getenv("GROWTH_AUTO_SOLID_EVIDENCE", "3") or 3)
    d = _load()
    done: list[dict] = []
    for it in list(d.get("items") or []):
        if it.get("status") != "pending":
            continue
        ev = [str(x).strip() for x in (it.get("evidence") or []) if str(x).strip()]
        if len(ev) < need:
            continue
        it["status"] = "solidified"
        it["solidifiedAt"] = _now()
        it["auto"] = True
        done.append(it)
    if done:
        # 固化上限：超了把最老的 auto 固化项归档
        solid = [x for x in d["items"] if x.get("status") == "solidified"]
        if len(solid) > MAX_SOLID:
            solid.sort(key=lambda x: x.get("solidifiedAt") or "")
            for old in solid[:len(solid) - MAX_SOLID]:
                if old.get("auto"):
                    old["status"] = "archived"
        _save(d)
        # 2026-11-07：固化时在记忆仓库留一条 commit。
        # 起因是 Agentero 的「审批入库」—— 我们现在是「证据满 3 条自动上，不用人点」，
        # 这是主人要的便捷，但今晚连续出了三次归属错误（大哥/欠款/CS程序员），
        # 事后只能手工翻 JSON 找是哪次写进去的。
        # 加一条 commit 之后：mg.history() 就能看到「哪天、根据什么、变成什么」。
        try:
            _commit_growth(done)
        except Exception as e:  # noqa: BLE001
            try:
                from nonebot import logger
                logger.debug(f"[人格固化] git 记账失败：{type(e).__name__}: {e}")
            except Exception:  # noqa: BLE001
                pass
    return done


def _commit_growth(done: list[dict]) -> None:
    """把这次固化的内容写成一条 git commit（在 memory/ 那个分离式仓库里）。"""
    if not done:
        return
    from plugins import memory_git as _mg
    if not _mg.ready():
        return
    parts = []
    for it in done[:5]:
        trait = str(it.get("trait") or "?")
        value = str(it.get("value") or "?")[:40]
        n = len([x for x in (it.get("evidence") or []) if str(x).strip()])
        parts.append(f"{trait}={value}({n}条证据)")
    tail = " 等" if len(done) > 5 else ""
    _mg.snapshot(f"人格固化 {len(done)} 条：{'；'.join(parts)}{tail}")


def history(item_id: str) -> list[dict]:
    """成长条目的修订历史（新的在前）。"""
    it = _find(_load(), item_id)
    return list(reversed((it or {}).get("revlog") or []))


def rollback(item_id: str) -> dict | None:
    """回滚成长条目到上一版 value；回滚本身也记一条修订。"""
    d = _load()
    it = _find(d, item_id)
    if not it:
        return None
    revlog = list(it.get("revlog") or [])
    if not revlog:
        return None
    prev = revlog.pop()
    try:
        revlog.append({"rev": int(it.get("rev") or 1), "value": str(it.get("value") or "")[:300],
                       "trait": str(it.get("trait") or ""), "ts": _now()})
        it["revlog"] = revlog[-5:]
        it["rev"] = int(it.get("rev") or 1) + 1
    except Exception:  # noqa: BLE001
        pass
    it["value"] = prev.get("value") or it.get("value")
    it["rolledBackAt"] = _now()
    _save(d)
    return it


def dismiss(item_id: str) -> dict | None:
    d = _load()
    it = _find(d, item_id)
    if not it:
        return None
    it["status"] = "dismissed"
    it["dismissedAt"] = _now()
    _save(d)
    return it


def solid_layer() -> str:
    """给 prompt 用的成长层文本（只含已固化项）。"""
    rows = [x for x in list_items() if x.get("status") == "solidified"]
    if not rows:
        return ""
    lines = [f"- {r.get('trait')}：{r.get('value')}" for r in rows[:MAX_SOLID]]
    return "【最近的成长】\n" + "\n".join(lines)


# ---------------------------------------------------------------- 提炼

def _env() -> dict:
    out: dict[str, str] = {}
    try:
        for line in (ROOT / ".env").read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except Exception:  # noqa: BLE001
        pass
    return out


def _recent_facts(limit: int = EXTRACT_LIMIT) -> list[dict]:
    """最近发生 / 最重的记忆，按时间倒序。"""
    try:
        from server import memory_viz as mv
        arch = mv._archive()
    except Exception:  # noqa: BLE001
        return []
    rows: list[dict] = []
    for scope, items in arch.items():
        for it in items or []:
            fact = str(it.get("fact") or "").strip()
            if not fact:
                continue
            rows.append({
                "scope": scope,
                "fact": fact[:100],
                "mid": f"mem:{scope}:{it.get('id')}",
                "t": int(mv._ms(it.get("lu")) or mv._ms(it.get("time")) or 0),
                "w": float(it.get("w") or 1),
            })
    rows.sort(key=lambda x: -x["t"])
    return rows[:limit]


async def _ask_llm(prompt: str) -> str:
    """按「硅基 → DeepSeek 官方」顺序试，前者没额度/挂了自动换后者。"""
    import httpx
    env = _env()
    cands: list[tuple[str, str, str]] = []
    if env.get("SILICONFLOW_API_KEY"):
        cands.append((
            env.get("SILICONFLOW_BASE_URL") or "https://api.siliconflow.cn/v1",
            env.get("SILICONFLOW_MODEL") or "deepseek-ai/DeepSeek-V4.1-Flash",
            env["SILICONFLOW_API_KEY"],
        ))
    if env.get("DEEPSEEK_API_KEY"):
        cands.append((
            env.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com",
            env.get("DEEPSEEK_MODEL") or "deepseek-chat",
            env["DEEPSEEK_API_KEY"],
        ))
    # 2026-11-06 修：本地 Qwen3-8B 原来排第一 —— 但这个函数是给「记忆提炼 / 三我提炼」
    # 用的，那两个要的是理解力，8B 明显不如 DeepSeek，排第一等于拿质量换免费。
    # 现在放到最后：云端全挂了才拿它兜底，保证提炼链不会整条断掉。
    if env.get("LOCAL_LLM_BASE", "http://127.0.0.1:11434/v1"):
        cands.append((
            env.get("LOCAL_LLM_BASE") or "http://127.0.0.1:11434/v1",
            env.get("LOCAL_LLM_MODEL") or "qwen3:8b",
            "ollama",                      # 本地不要 key，占位
        ))
    if not cands:
        raise RuntimeError("没有可用的模型 key（SILICONFLOW_API_KEY / DEEPSEEK_API_KEY）")

    last = ""
    for base, model, key in cands:
        try:
            async with httpx.AsyncClient(timeout=90) as cli:
                _hd = {} if key == "ollama" else {"Authorization": f"Bearer {key}"}
                r = await cli.post(
                    base.rstrip("/") + "/chat/completions",
                    headers=_hd,
                    json={"model": model, "temperature": 0.4,
                          "messages": [{"role": "user", "content": prompt}]},
                )
                data = r.json()
            if isinstance(data, dict) and data.get("choices"):
                return str((data.get("choices") or [{}])[0].get("message", {}).get("content") or "")
            last = f"{base} HTTP {r.status_code}: {str(data)[:140]}"
        except Exception as e:  # noqa: BLE001
            last = f"{base} {type(e).__name__}: {e}"
    raise RuntimeError(f"所有模型通道都失败：{last}")


def _parse_json(text: str) -> list[dict]:
    m = re.search(r"\[[\s\S]*\]", text or "")
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return []
    return [x for x in arr if isinstance(x, dict)][:MAX_PER_RUN]


async def extract() -> dict:
    """跑一次提炼，返回 {added: n, items: [...]}。"""
    facts = _recent_facts()
    if not facts:
        return {"added": 0, "items": [], "error": "没有可用的记忆"}
    d = _load()
    existing = "；".join(f"{x.get('trait')}:{x.get('value')}" for x in d.get("items") or []
                        if x.get("status") in ("solidified", "pending")) or "（无）"
    lines = [f"- {f['fact']}" for f in facts]
    prompt = (
        "你是记忆分类器。下面是虚拟人格「蓝色大肥鱼」最近的记忆片段，"
        "请判断哪些构成「人格成长」，并按子类归档。\n\n"
        "【准入标准】必须同时满足至少 2 条：\n"
        "1. 非特异性：不针对某一个具体的人或群，而是泛化到所有交互\n"
        "2. 行为模式改变：说话方式、响应策略发生改变\n"
        "3. 价值观/偏好迭代：对某类事物（某种梗、某种话题）的看法改变\n"
        "4. 教训总结：因为某次被纠正，形成了长期的避坑规则\n\n"
        "【正例（应该归类到人格成长）】\n"
        '- "通过多次群聊，学会了用更幽默的二次元语气回答，而不是像严肃客服"（风格）\n'
        '- "发现群友不喜欢刷屏，以后对话必须精简，不能连续发三段话"（行为约束）\n'
        '- "曾以为下雨天很无聊，现在觉得下雨天适合聊伤感话题"（偏好泛化）\n\n'
        "【反例（绝不能被归类到人格成长）】\n"
        '- "对某人的态度：从嫌弃变成一边骂她一边主动关心" → character_relation（特定人物关系）\n'
        '- "学会了怎么打英雄联盟" → experience（技能/经历）\n'
        '- "群主说今天要发红包" → short_term（事件）\n'
        '- "口癖/自称变了" → style 之外的语言习惯，不收\n\n'
        "【已收录，不要重复】\n" + existing + "\n\n"
        "【输出】最多 3 条，没有就输出 []。只输出 JSON 数组，每项：\n"
        '{"trait": "抽象方面（如：幽默感/耐心/边界感）", '
        '"value": "具体变化（一句话，不针对特定对象）", '
        '"category": "style|value|behavior", '
        '"evidence": ["记忆原文片段"]}\n'
        "category 取值：style=表达风格适应、value=价值观/偏好迭代、behavior=行为约束规则\n\n"
        "最近记忆：\n" + "\n".join(lines)
    )
    text = await _ask_llm(prompt)
    got = _parse_json(text)

    added = []
    _VALID_CAT = {"style", "value", "behavior"}
    for x in got:
        trait = str(x.get("trait") or "").strip()[:12]
        value = str(x.get("value") or "").strip()[:120]
        if not trait or not value:
            continue
        # 硬拦：人物关系 / 语言习惯 / 事件，一律不进人格成长
        if re.search(r"态度|关系|好感|印象|口癖|称呼|昵称|学会了打|今天|刚才", trait + " " + value[:24]):
            continue
        cat = str(x.get("category") or "").strip().lower()
        if cat not in _VALID_CAT:
            cat = "style"
        item = {
            "id": "growth:" + uuid.uuid4().hex[:10],
            "trait": trait,
            "value": value,
            "category": cat,          # style | value | behavior
            "evidence": [str(e)[:80] for e in (x.get("evidence") or [])][:4],
            "status": "pending",
            "since": _now(),
            "t": int(time.time() * 1000),
        }
        d["items"].append(item)
        added.append(item)

    # 淘汰：总量超上限时，先丢最老的 dismissed
    items = d.get("items") or []
    if len(items) > MAX_ITEMS:
        items.sort(key=lambda x: (x.get("status") != "dismissed", x.get("t") or 0))
        d["items"] = items[-MAX_ITEMS:]
    _save(d)
    auto = auto_solidify()
    if auto:
        return {"added": len(added), "items": added,
                "auto_solidified": [x.get("trait") for x in auto]}
    return {"added": len(added), "items": added}
