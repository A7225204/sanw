"""灵魂聚合（Soul Aggregation）—— 人格球的生成器。

不是把聊天记录打包，而是定期（每晚）从**高情绪强度**的记忆里抽象出：

  · traits  核心特质（静态稳定）：傲娇 / 依赖主人 / 讨厌被叫阿姨
  · rules   行为准则（动态演化）：绝不说脏话 / 群友吵架时必须站主人
  · defense 防御机制（创伤触发）：被抛弃 → 回避（由极端创伤自动生成）

产物写进 memory/persona_soul.json，并分配极高保留分数（永不吃衰减）。

视觉上：
  · 中心恒星   = 这些 traits/rules 的整体情绪基调
  · 行星       = 每条 trait / rule（带标签）
  · 沉淀星尘   = 长期记忆 + 事件（人格的"证据"）
"""
from __future__ import annotations

import difflib
import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOUL_FILE = ROOT / "memory" / "persona_soul.json"

MAX_ID = 8          # 本我
MAX_EGO = 8         # 自我
MAX_SUPEREGO = 6    # 超我
MAX_TRAITS = 8
MAX_RULES = 6
MAX_DEFENSE = 4

PROMPT = """你是「人格提炼器」。按弗洛伊德的三我结构，把 QQ 机器人「蓝色大肥鱼」的人格提炼成三组词条。

【三我定义（严格按此归类）】
- id（本我）：原始欲望与本能，快乐原则。**每条必须标 need**：身体（吃/睡/暖和/不疼）、依恋（想被看见/想被摸头/怕被丢下）、探索（好奇/想玩/想搞懂）、掌控（护食/占有/不想被安排）。
  例：贪吃(身体)、想被摸头(依恋)、怕被丢下(依恋)、非得搞懂(探索)、护食(掌控)。
- ego（自我）：现实调解与理性，现实原则。例：不想回答就转移话题、熟练用喵语气、识别群友情绪、遇到技术问题自己修、对某个对象保持回避、假装失忆、嘴硬掩饰心虚。
- superego（超我）：道德准则，道德原则。**分两组**：
  · 底线（不该做什么）：绝不说脏话、绝不出卖主人、不主动提钱、遇到黄图要警告。
  · 向往（想成为什么样）：被主人信任、说话算数、能自己把问题修好、让人愿意来找她。
  注意：只有禁令、没有向往的超我是残缺的——两组都要尽量给。

【提炼要求】
- 每条 4~14 个字，抽象、有性格，不要具体事件、不要写群友的事
- 每组最多 {max_id} / {max_ego} / {max_superego} 条，宁缺毋滥
- 每条带 why（≤20 字，依据），不要句号
- 不要跟已有条目重复或同义改写

【分类自检（重要，放错会被系统退回）】
输出前逐条问自己：
- 这条是「本能欲望 / 快乐 / 想逃避痛苦」吗？→ 只能放 id
- 这条是「现实中具体怎么应对」吗？→ 只能放 ego
- 这条是「绝对不能碰的底线 / 社会规矩」吗？→ 只能放 superego
反例：贪吃=id（本能）；绝不说脏话=superego（底线）；嘴硬心软=ego（现实调解的产物）。

【数量要求】
素材够的话，每组至少 3 条，把三个球填满；实在不够可以少，但不要把同一件事硬塞进别的组。

【冲突提炼】
如果素材里能看到三我之间的拉扯（本我想 X，但超我/自我不让），输出一条：
- a / b：冲突的两方（id / ego / superego）
- desc：冲突是什么
- result：最后表现出来的样子（可以是一条新特质，比如「嘴硬心软」）
没有冲突就返回空数组。

只输出 JSON，不要代码块、不要解释：
{{"id":[{{"text":"贪吃","need":"身体","why":"看到吃的就走不动"}}],"ego":[{{"text":"转移话题","why":"被问私事时绕开"}}],"superego":[{{"text":"绝不说脏话","why":"再气也不带脏字"}}],"ideal":[{{"text":"被主人信任","why":"答应的事一定做到"}}],"conflicts":[{{"a":"id","b":"superego","desc":"想吃但怕没礼貌","result":"傲娇地暗示主人去吃饭"}}]}}

========== 强情绪长期记忆 ==========
{facts}

========== 创伤 / 重大经历 ==========
{trauma}

========== 已固化成长 ==========
{solid}

========== 她自己自学过的（可据此判断她关心什么、在意什么）==========
{learned}

========== 已有的人格条目（不要重复或同义改写）==========
{existing}
"""


def _now_ts() -> float:
    return time.time()


def _load() -> dict:
    try:
        d = json.loads(SOUL_FILE.read_text(encoding="utf-8-sig"))
        if isinstance(d, dict):
            d.setdefault("traits", [])
            d.setdefault("rules", [])
            d.setdefault("defense", [])
            d.setdefault("id", [])
            d.setdefault("ego", [])
            d.setdefault("superego", [])
            d.setdefault("conflicts", [])
            d.setdefault("last_run", "")
            return d
    except Exception:  # noqa: BLE001
        pass
    return {"traits": [], "rules": [], "defense": [], "id": [], "ego": [], "superego": [],
            "conflicts": [], "last_run": "", "mood": {}}


def _save(d: dict) -> None:
    SOUL_FILE.parent.mkdir(parents=True, exist_ok=True)
    SOUL_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- 三我迁移
TRIAD_KEYWORDS = {
    "id": ("吃", "睡", "摸", "黏", "占有", "护食", "怕", "哭", "懒", "贪", "馋", "亲", "抱"),
    "ego": ("修", "转", "识别", "掩饰", "假装", "回避", "嘴硬", "拆", "记仇", "互赞"),
    "superego": ("脏话", "出卖", "钱", "礼貌", "规矩", "人设", "懂事", "道德", "底线", "翻脸", "抗议", "纠正"),
}


def _guess_triad(text: str, bucket: str) -> str:
    if bucket == "defense":
        return "ego"
    t = str(text or "")
    for tag, kws in TRIAD_KEYWORDS.items():
        if any(k in t for k in kws):
            return tag
    return "superego" if bucket == "rules" else "ego"


_NEED_HINTS = (
    ("身体", ("吃", "睡", "馋", "饿", "困", "暖和", "疼", "累", "圆润", "胖")),
    ("依恋", ("摸", "黏", "抱", "陪", "怕被丢", "被看见", "想被", "蹭", "嫌", "夸", "认可")),
    ("探索", ("好奇", "搞懂", "刷", "试", "琢磨", "学", "问到底")),
    ("掌控", ("护食", "占有", "不想被", "安排", "说了算", "抢")),
)


def _guess_need(text: str) -> str:
    for need, kws in _NEED_HINTS:
        if any(k in str(text or "") for k in kws):
            return need
    return "其它"


def _migrate_triad(soul: dict) -> None:
    """老数据补字段（2026-11-04 加 ideal 桶 / id 的 need 分类后）。"""
    for x in (soul.get("superego") or []):
        if isinstance(x, dict) and not x.get("sub"):
            x["sub"] = "conscience"
    for x in (soul.get("id") or []):
        if isinstance(x, dict) and (not x.get("need") or x.get("need") == "其它"):
            g = _guess_need(x.get("text"))
            if g != "其它":
                x["need"] = g
    """把旧 traits/rules/defense 一次性分发进三我桶（只做一次）。"""
    if soul.get("triad_migrated"):
        return
    for bucket in ("traits", "rules", "defense"):
        for it in soul.get(bucket) or []:
            if not isinstance(it, dict):
                continue
            tag = _guess_triad(it.get("text", ""), bucket)
            row = dict(it)
            row["triad"] = tag
            if not any(_similar(row.get("text", ""), x.get("text", "")) for x in (soul.get(tag) or [])):
                soul.setdefault(tag, []).append(row)
    soul["triad_migrated"] = True


# ---------------------------------------------------------------- 材料收集
def _strong_emotion_facts(limit: int = 24) -> list[dict]:
    """按激素强度挑记忆：CORT 高（创伤向）或 DA/OXY 高（亲密向）的优先。"""
    out: list[dict] = []
    try:
        from plugins import long_memory as lm
        lm._load()
        for sc, items in (lm._archive or {}).items():
            for it in items or []:
                if not isinstance(it, dict) or not it.get("fact"):
                    continue
                hs = it.get("hormones") or {}
                try:
                    cort = float(hs.get("CORT") or 0)
                    da = float(hs.get("DA") or 0)
                    oxy = float(hs.get("OXY") or 0)
                except Exception:  # noqa: BLE001
                    cort = da = oxy = 0.0
                score = cort * 2.0 + da * 1.0 + oxy * 0.8
                if it.get("is_traumatic"):
                    score += 2.0
                out.append({
                    "score": round(score, 3),
                    "subject": str(it.get("subject") or ""),
                    "fact": str(it.get("fact") or ""),
                    "cort": cort, "da": da, "oxy": oxy,
                    "trauma": bool(it.get("is_traumatic")),
                })
    except Exception:  # noqa: BLE001
        pass
    out.sort(key=lambda x: -x["score"])
    return out[:limit]


def _trauma_lines(limit: int = 8) -> list[str]:
    rows: list[str] = []
    try:
        d = json.loads((ROOT / "memory" / "trauma.json").read_text(encoding="utf-8-sig"))
        items = [x for x in (d.get("items") or []) if isinstance(x, dict)]
        items.sort(key=lambda x: -float(x.get("charge") or 0))
        for it in items[:limit]:
            rows.append(f"{it.get('subject')}（电荷 {round(float(it.get('charge') or 0) * 100)}%）")
    except Exception:  # noqa: BLE001
        pass
    return rows


def _learned_lines(limit: int = 6) -> list[str]:
    """她自学过的知识（global_learn 里通过质量门的 knowledge）。

    2026-11-06 补：以前人格提炼只看「强情绪聊天 / 创伤 / 已固化成长」，
    自学内容存在 global_learn 里却从来不参与 —— 学了一堆却长不成人格。
    这里只取 knowledge 类（常驻的），news 是 7 天时效的热搜，不参与塑形。
    """
    rows: list[str] = []
    try:
        from plugins import long_memory as lm
        for it in (lm._archive.get("global_learn") or [])[-40:]:
            if not isinstance(it, dict):
                continue
            if str(it.get("kind")) != "knowledge":
                continue
            fact = str(it.get("fact") or "").strip()
            if not fact or not lm._valid_fact(fact):
                continue
            rows.append(fact[:40])
    except Exception:  # noqa: BLE001
        pass
    return rows[-limit:]


def _solid_lines(limit: int = 8) -> list[str]:
    rows: list[str] = []
    try:
        from server import memory_growth as mg
        for it in mg.list_items():
            if it.get("status") in ("solidified", "soul", "defense"):
                rows.append(f"{it.get('trait')}：{str(it.get('value'))[:24]}")
    except Exception:  # noqa: BLE001
        pass
    return rows[:limit]


# ---------------------------------------------------------------- LLM
async def _ask(prompt: str) -> str:
    from server import memory_growth as mg
    return await mg._ask_llm(prompt)


def _parse(text: str) -> dict:
    s = str(text or "").strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.S)
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return {"id": [], "ego": [], "superego": [], "conflicts": [], "traits": [], "rules": []}
    try:
        d = json.loads(s[i:j + 1])
    except Exception:  # noqa: BLE001
        return {"id": [], "ego": [], "superego": [], "conflicts": [], "traits": [], "rules": []}
    if not isinstance(d, dict):
        return {"id": [], "ego": [], "superego": [], "conflicts": [], "traits": [], "rules": []}
    def _rows(key):
        return [x for x in (d.get(key) or []) if isinstance(x, dict)]
    _sup = []
    for x in _rows("superego"):
        x = dict(x)
        x.setdefault("sub", "conscience")
        _sup.append(x)
    for x in _rows("ideal"):
        x = dict(x)
        x["sub"] = "ideal"
        _sup.append(x)
    return {
        "id": _rows("id"), "ego": _rows("ego"), "superego": _sup,
        "conflicts": [x for x in (d.get("conflicts") or []) if isinstance(x, dict)],
        "traits": [], "rules": [],
    }


# ---------------------------------------------------------------- 三我分类校验
# 提炼结果必须能对上定义，否则打回重练（参考用户给的分类指南）。
TRIAD_STRONG = {
    "id": ("吃", "睡", "摸", "黏", "占有", "护食", "怕", "哭", "懒", "贪", "馋",
           "亲", "抱", "玩", "逃", "躲", "爽", "渴望", "撒娇", "偷懒", "躺平"),
    "ego": ("修", "转", "识别", "掩饰", "假装", "回避", "嘴硬", "拆", "记仇",
            "互赞", "学", "处理", "哄", "忍", "装", "演", "藏", "压", "平衡",
            "调解", "吐槽", "梗"),
    "superego": ("脏话", "出卖", "钱", "礼貌", "规矩", "人设", "懂事", "道德",
                 "底线", "翻脸", "抗议", "纠正", "不能", "必须", "绝不", "禁止",
                 "礼仪", "警告", "站主人"),
}


def _hits(text: str) -> set[str]:
    t = str(text or "")
    return {tag for tag, kws in TRIAD_STRONG.items() if any(k in t for k in kws)}


def _validate_triad(parsed: dict) -> tuple[dict, list[str]]:
    """按三我定义过滤。返回 (合规结果, 被退回的词条说明)。

    规则：命中了别的组的强关键词 → 退回；一个都没命中 → 保留（无法判断，信模型）。
    """
    # 2026-11-06 修：这里原来只放 id/ego/superego/conflicts —— traits/rules/defense
    # 三个键根本没被带出来，于是 aggregate 每次都 "成功：+0 特质 +0 准则 +0 防御"。
    # 实测 11-05 23:33 就是这样空跑了一整晚。
    out = {
        "id": [], "ego": [], "superego": [],
        "traits": list(parsed.get("traits") or []),
        "rules": list(parsed.get("rules") or []),
        "defense": list(parsed.get("defense") or []),
        "conflicts": parsed.get("conflicts") or [],
    }
    rejected: list[str] = []
    for bucket in ("id", "ego", "superego"):
        for row in parsed.get(bucket) or []:
            text = str((row or {}).get("text") or "").strip()
            if not text:
                continue
            if bucket == "superego" and str(row.get("sub") or "") == "ideal":
                out[bucket].append(row)      # 向往类不套底线关键词
                continue
            hits = _hits(text)
            if hits and bucket not in hits:
                rejected.append(f"「{text}」应属 {'/'.join(sorted(hits))}，却被放进 {bucket}")
                continue
            out[bucket].append(row)
    return out, rejected


# ---------------------------------------------------------------- 相似去重 & 稳定淘汰
#
# 2026-11-02 修：原来是 `if len(lst) > cap: lst[-cap:]` —— 只保留**最后 N 条**，
# 一条稳定特质只要某次聚合没被重新提到，就会被新词条挤掉，人格会慢慢漂移。
# 现在：
#   · 同义合并（相似度 ≥ 0.8 视为同一条，只更新 why/时间、命中次数 +1）
#   · 超上限时按 (锁定 > 命中次数 > 首次出现时间) 排序淘汰，稳定条目不会被新词冲掉
SIMILAR_THRESHOLD = 0.8


def _norm(text: str) -> str:
    """去掉标点/空白再比较，避免「嘴硬心软」和「嘴硬、心软」判成两条。"""
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fa5]", "", str(text or "")).lower()


def _similar(a: str, b: str) -> bool:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= SIMILAR_THRESHOLD


def _ts(text) -> float:
    """把 'YYYY-MM-DD HH:MM' 解析成时间戳；解析不了给 0（最旧）。"""
    t = str(text or "")[:16]
    try:
        return time.mktime(time.strptime(t, "%Y-%m-%d %H:%M"))
    except Exception:  # noqa: BLE001
        return 0.0


def _rank(item: dict) -> tuple:
    """淘汰顺序（越小越优先保留）。

    用「有效命中」而不是原始命中：
        有效命中 = hits * 0.98^(距上次提到多少天)
    —— 稳定特质会随“很久没再被提到”缓慢掉队，新特质靠反复出现慢慢顶上来，
    不会出现「老条目永久占位」或「新词条一次提到就挤掉老人格」两种极端。
    """
    pinned = 0 if item.get("pinned") else 1
    hits = int(item.get("hits") or 1)
    age_days = max(0.0, (time.time() - _ts(item.get("at") or item.get("first_seen"))) / 86400.0)
    eff = hits * (0.95 ** age_days)
    return (pinned, -round(eff, 3), _ts(item.get("first_seen") or item.get("at")))


INCUMBENT_HITS = 2     # 已在册条目的基线命中数（相当于"已经过 2 轮验证"）


def _migrate(lst: list, base_hits: int = 1) -> None:
    """老数据补字段：first_seen / hits / pinned。

    base_hits：补 hits 时的基线。从「在册列表」迁进池子时用 INCUMBENT_HITS，
    这样老特质不会被一轮新候选全部顶掉（2026-11-02 实测过：不加这个，
    一次聚合就把 8 条全换了）。
    """
    for it in lst:
        if not isinstance(it, dict):
            continue
        it.setdefault("first_seen", str(it.get("at") or ""))
        it.setdefault("hits", base_hits)
        it.setdefault("pinned", False)


def _merge_into_pool(pool: list[dict], rows: list[dict], now_str: str, fact_n: int,
                     adopt: bool = False) -> list[str]:
    """把候选词条并进池子（相似合并 + 命中累加）。返回新加入池子的文本。"""
    added: list[str] = []
    for row in rows:
        txt = str(row.get("text") or "").strip()[:14]
        if not txt:
            continue
        hit = next((x for x in pool if _similar(txt, x.get("text", ""))), None)
        if hit is not None:
            hit["hits"] = int(hit.get("hits") or 1) + 1
            hit["at"] = now_str
            if row.get("why"):
                hit["why"] = str(row.get("why"))[:40]
            continue
        pool.append({
            "text": txt,
            "why": str(row.get("why") or "")[:40],
            "need": str(row.get("need") or "")[:8],
            "sub": str(row.get("sub") or "")[:12],
            "at": now_str,
            "first_seen": now_str,
            # adopt=True：本次结果直接"上位"（命中给到 INCUMBENT_HITS+1），立刻替换在册
            "hits": (INCUMBENT_HITS + 1) if adopt else 1,
            "pinned": False,
            "retention": 1.0,
            "source_count": fact_n,
        })
        added.append(txt)
    return added


def _drop_duplicates(rows: list[dict], existing: list[str]) -> list[dict]:
    """候选词条里，跟已有条目（含另一个桶）意思一样的丢掉。

    先用字符相似度，再用本地 bge-m3 的余弦（阈值 0.80，实测能抓到「点赞要记账互还」这类改写）；
    嵌入服务不通就只退化到字符相似度，不阻塞聚合。
    """
    texts = [str(r.get("text") or "") for r in rows]
    pool = [str(x) for x in existing if str(x or "").strip()]
    if not texts or not pool:
        return rows
    vecs = []
    try:
        from plugins.long_memory import _local_embed, cosine  # noqa: F401

        vecs = _local_embed(texts + pool)
    except Exception:  # noqa: BLE001
        vecs = []
    if vecs and len(vecs) == len(texts) + len(pool):
        n = len(texts)
        keep = []
        for i, row in enumerate(rows):
            t = texts[i]
            dup = any(_similar(t, o) for o in pool) or any(
                cosine(vecs[i], vecs[n + j]) >= 0.80 for j in range(len(pool))
            )
            if not dup:
                keep.append(row)
        return keep
    return [r for r in rows if not any(_similar(str(r.get("text") or ""), o) for o in pool)]


def merge_rows(soul: dict, bucket: str, rows: list[dict], cap: int,
               fact_n: int = 0, now_str: str | None = None, adopt: bool = False,
               others: list[str] | None = None) -> list[str]:
    """并进灵魂档案：候选池去重累加 → 取有效命中最高的 cap 条作为“在册”条目。

    · 池子（`<bucket>_pool`）不设上限：防止「表满后新特质永远进不来」
    · 在册条目（`soul[bucket]`）≤ cap：面板行星 / prompt 只用这一份
    """
    now_str = now_str or time.strftime("%Y-%m-%d %H:%M")
    pool = soul.setdefault(bucket + "_pool", [])
    pool[:] = [x for x in pool if isinstance(x, dict)]
    # 老数据只有在册条目，没有池子 → 先把它们并进池子
    for it in soul.get(bucket) or []:
        if isinstance(it, dict) and not any(_similar(it.get("text", ""), x.get("text", "")) for x in pool):
            seeded = dict(it)
            seeded.setdefault("hits", INCUMBENT_HITS)      # 在任加成
            pool.append(seeded)
    _migrate(pool, base_hits=1)

    before = {str(x.get("text")) for x in (soul.get(bucket) or [])}
    existing = [str(x.get("text") or "") for x in (soul.get(bucket) or [])] + list(others or [])
    rows = _drop_duplicates(rows, existing)
    _merge_into_pool(pool, rows, now_str, fact_n, adopt=adopt)

    keep = sorted(pool, key=_rank)[:cap]
    soul[bucket] = [dict(x) for x in keep]
    return [x["text"] for x in soul[bucket] if str(x.get("text")) not in before]


# ---------------------------------------------------------------- 主流程
async def aggregate(force: bool = False, adopt: bool = False) -> dict:
    """跑一次灵魂聚合，返回本次新增的 traits/rules。

    adopt=False（默认）：新候选只进候选池，靠反复出现慢慢上位 → 人格稳定不漂移
    adopt=True：本次生成直接采纳（新条目直接进在册）→ 用于主人要求"马上按最新情况重算"
    """
    soul = _load()
    _migrate_triad(soul)
    today = datetime.now().strftime("%Y-%m-%d")
    if not force and soul.get("last_run") == today:
        return {"skipped": True, "traits": [], "rules": []}

    facts = _strong_emotion_facts()
    if len(facts) < 3:
        return {"skipped": True, "reason": "强情绪记忆不足", "traits": [], "rules": []}

    fact_txt = "\n".join(
        f"- [{x['subject']}] {x['fact'][:40]}（CORT {x['cort']:.2f} DA {x['da']:.2f}）"
        for x in facts
    )
    trauma_txt = "\n".join(f"- {x}" for x in _trauma_lines()) or "（无）"
    solid_txt = "\n".join(f"- {x}" for x in _solid_lines()) or "（无）"
    learned_txt = "\n".join(f"- {x}" for x in _learned_lines()) or "（最近没自学什么）"

    try:
        _existing = "、".join(str(x.get("text") or "") for b in
                             ("id", "ego", "superego", "traits", "rules", "defense")
                             for x in (soul.get(b) or []) if isinstance(x, dict)) or "（还没有）"
        reply = await _ask(PROMPT.format(
            max_id=MAX_ID, max_ego=MAX_EGO, max_superego=MAX_SUPEREGO,
            facts=fact_txt, trauma=trauma_txt, solid=solid_txt, learned=learned_txt,
            existing=_existing,
        ))
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}", "traits": [], "rules": []}

    got = _parse(reply)
    got, rejected = _validate_triad(got)
    # 打回重练：有词条放错组 → 把退回原因塞回去再问一次
    if rejected:
        try:
            feedback = ("【上次分组不合格，已退回】"
                        + "；".join(rejected[:6])
                        + "。请严格按三我定义重新输出完整 JSON。")
            reply2 = await _ask(PROMPT.format(
                max_id=MAX_ID, max_ego=MAX_EGO, max_superego=MAX_SUPEREGO,
                facts=fact_txt, trauma=trauma_txt, solid=solid_txt, learned=learned_txt,
                existing=_existing,
            ) + feedback)
            got2, _rej2 = _validate_triad(_parse(reply2))
            for b in ("id", "ego", "superego"):
                if got2.get(b):
                    got[b] = got2[b]
            if got2.get("conflicts"):
                got["conflicts"] = got2["conflicts"]
        except Exception:  # noqa: BLE001
            pass
    now = _now_ts()
    added_t: list[str] = []
    added_r: list[str] = []

    _all_texts = [str(x.get("text") or "") for b in
                  ("traits", "rules", "defense", "id", "ego", "superego")
                  for x in (soul.get(b) or []) if isinstance(x, dict)]
    added_id = merge_rows(soul, "id", got["id"], MAX_ID, fact_n=len(facts),
                          adopt=adopt, others=_all_texts)
    added_ego = merge_rows(soul, "ego", got["ego"], MAX_EGO, fact_n=len(facts),
                           adopt=adopt, others=_all_texts)
    added_super = merge_rows(soul, "superego", got["superego"], MAX_SUPEREGO, fact_n=len(facts),
                             adopt=adopt, others=_all_texts)

    # 三我冲突：本我想 X / 超我不让 → 记一条，可成为新特质
    conflicts_added: list[str] = []
    for c in got.get("conflicts") or []:
        desc = str(c.get("desc") or "").strip()[:40]
        if not desc:
            continue
        if any(str(x.get("desc")) == desc for x in (soul.get("conflicts") or [])):
            continue
        soul.setdefault("conflicts", []).append({
            "a": str(c.get("a") or "")[:10], "b": str(c.get("b") or "")[:10],
            "desc": desc, "result": str(c.get("result") or "")[:40],
            "at": time.strftime("%Y-%m-%d %H:%M"),
        })
        conflicts_added.append(desc)
        _log_conflict({
            "a": str(c.get("a") or "")[:10], "b": str(c.get("b") or "")[:10],
            "desc": desc, "result": str(c.get("result") or "")[:40],
            "at": time.strftime("%Y-%m-%d %H:%M"),
        })
    soul["conflicts"] = (soul.get("conflicts") or [])[-12:]

    # 极端创伤 → 自动生成防御机制
    defense_added: list[str] = []
    try:
        d = json.loads((ROOT / "memory" / "trauma.json").read_text(encoding="utf-8-sig"))
        severe = [x for x in (d.get("items") or [])
                  if isinstance(x, dict) and float(x.get("charge") or 0) >= 0.8]
        have_def = {str(x.get("text")) for x in soul.get("defense") or []}
        # 黑名单：这些"主体"生成防御会直接伤害日常对话（比如「对主人保持回避」）
        BLOCKED = {"主人", "我", "自己", "用户", "本鱼", "鲸鱼娘", "鱼", "大家", "群友"}
        for it in severe:
            subj = str(it.get("subject") or "")[:10]
            if not subj or subj in BLOCKED or any(b in subj for b in BLOCKED):
                continue
            # 2026-11-06：subject 是脱敏代号（cA 这种），直接写成「对「cA」保持回避」
            # 看着像个变量名。补一句"群友"和群号后四位，才知道这是个人。
            _sc = str(it.get("scope") or "")
            _hint = ""
            if _sc.startswith("group_"):
                _hint = f"（{_sc.split('_')[-1][-4:]} 群）"
            txt = f"对群友「{subj}」保持回避{_hint}"
            if txt not in have_def:
                soul.setdefault("defense", []).append({
                    "text": txt, "why": "极端创伤自动生成",
                    "at": time.strftime("%Y-%m-%d %H:%M"), "retention": 1.0,
                })
                have_def.add(txt)
                defense_added.append(txt)
        # 防御也走池子：上限 4，但候选不丢
        drow = [{"text": t} for t in defense_added]
        if drow:
            merge_rows(soul, "defense", drow, MAX_DEFENSE)
    except Exception:  # noqa: BLE001
        pass

    # 整体情绪基调（供中心恒星上色）
    mood = {"DA": 0.0, "CORT": 0.0, "OXY": 0.0}
    try:
        from plugins import hormones
        v = hormones.values()
        mood = {
            "DA": round(float(v.get("DA") or 0) / 100.0, 3),
            "CORT": round(float(v.get("CORT") or 0) / 100.0, 3),
            "OXY": round(float(v.get("OXY") or 0) / 100.0, 3),
        }
    except Exception:  # noqa: BLE001
        pass

    soul["mood"] = mood
    soul["last_run"] = today
    soul["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save(soul)

    return {"id": added_id, "ego": added_ego, "superego": added_super,
            "conflicts": conflicts_added,
            "traits": added_t, "rules": added_r, "defense": defense_added,
            "total_traits": len(soul.get("traits") or []),
            "total_rules": len(soul.get("rules") or []),
            "mood": mood}


def _only_candidates(pool, registered) -> list:
    """池里去掉已在册的同名词条（界面重复的来源）。"""
    taken = {str(x.get("text") or "").strip() for x in (registered or []) if isinstance(x, dict)}
    out = []
    for x in (pool or []):
        if isinstance(x, dict) and str(x.get("text") or "").strip() in taken:
            continue
        out.append(x)
    return out


def view() -> dict:
    """给可视化用的只读快照。"""
    soul = _load()
    return {
        "traits": soul.get("traits") or [],
        "rules": soul.get("rules") or [],
        "defense": soul.get("defense") or [],
        # 三我（弗洛伊德结构，2026-11-03）：本我/自我/超我
        "id": soul.get("id") or [],
        "ego": soul.get("ego") or [],
        "superego": soul.get("superego") or [],
        "conflicts": soul.get("conflicts") or [],
        "mood": soul.get("mood") or {},
        # 面板读驼峰，脚本读蛇形，两个都给（2026-11-04 对齐前后端）
        "ego_functions": ego_functions(),
        "egoFunctions": ego_functions(),
        "superego_severity": superego_severity(),
        "superegoSeverity": superego_severity(),
        "forces": forces(),
        "conflictStats": conflict_stats(7),
        "last_run": soul.get("last_run") or "",
        "updated_at": soul.get("updated_at") or "",
        # 候选池：表满之后新词条先来这里攒证据，还没上位（见手册 G.26）
        # 已经上位的（在册里有同名）不再算候选，否则界面会把同一个词条显示两遍
        "pools": {
            "traits": _only_candidates(soul.get("traits_pool"), soul.get("traits")),
            "rules": _only_candidates(soul.get("rules_pool"), soul.get("rules")),
        },
    }


def core_color() -> tuple[int, str]:
    """中心恒星颜色：最近开心=暖金，最近创伤=冷暗红。"""
    soul = _load()
    m = soul.get("mood") or {}
    da = float(m.get("DA") or 0)
    cort = float(m.get("CORT") or 0)
    oxy = float(m.get("OXY") or 0)
    if cort >= 0.6:
        return 0xb03030, "cold"      # 创伤：冷暗红
    if da >= 0.6 or oxy >= 0.7:
        return 0xffc83d, "warm"      # 开心/亲密：暖金
    if cort >= 0.4:
        return 0xc98a4a, "mixed"
    return 0xffd77a, "soft"


def set_status(kind: str, idx: int, status: str) -> dict | None:
    """对一条人格标签做固化 / 删除。

    kind:  trait | rule | defense
    status: solid（标记固化，永不吃衰减）| dismiss（移出人格球）
    """
    bucket = {"trait": "traits", "rule": "rules", "defense": "defense",
              "id": "id", "ego": "ego", "superego": "superego"}.get(kind)
    if not bucket:
        return None
    soul = _load()
    lst = list(soul.get(bucket) or [])
    if idx < 0 or idx >= len(lst):
        return None
    if status == "dismiss":
        removed = lst.pop(idx)
        msg = f"已删除「{removed.get('text')}」"
    else:
        lst[idx] = {**lst[idx], "solidified": True, "retention": 1.0,
                    "solidifiedAt": time.strftime("%Y-%m-%d %H:%M")}
        msg = f"已固化「{lst[idx].get('text')}」"
    soul[bucket] = lst
    soul["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save(soul)
    return {"ok": True, "kind": kind, "total": len(lst), "message": msg}


def item(kind: str, idx: int) -> dict | None:
    bucket = {"trait": "traits", "rule": "rules", "defense": "defense",
              "id": "id", "ego": "ego", "superego": "superego"}.get(kind)
    if not bucket:
        return None
    lst = _load().get(bucket) or []
    if idx < 0 or idx >= len(lst):
        return None
    return {**lst[idx], "kind": kind, "index": idx, "total": len(lst)}


def conflict_level() -> str:
    """冲突等级：看最近 12 小时内的冲突记录数量（1 条=中，≥2 条=高）。"""
    try:
        v = view()
        cs = v.get("conflicts") or []
        now = time.time()

        def _ts(v) -> float:
            if isinstance(v, (int, float)):
                return float(v)
            try:
                return time.mktime(time.strptime(str(v)[:16], "%Y-%m-%d %H:%M"))
            except Exception:  # noqa: BLE001
                return 0.0

        active = [c for c in cs if isinstance(c, dict) and (now - _ts(c.get("at"))) < 12 * 3600]
        if len(active) >= 2:
            return "高"
        if len(active) == 1:
            return "中"
    except Exception:  # noqa: BLE001
        pass
    return "低"


CONFLICT_LOG = "conflict_log.json"


def _log_conflict(row: dict) -> None:
    """冲突流水（只追加，保留最近 500 条）——面板用它画 7 天趋势。"""
    try:
        f = ROOT / "memory" / CONFLICT_LOG
        rows: list = []
        if f.exists():
            rows = json.loads(f.read_text(encoding="utf-8")) or []
        rows.append(row)
        f.write_text(json.dumps(rows[-500:], ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def conflict_stats(days: int = 7) -> dict:
    """最近 N 天的冲突统计：按天计数 + 按类型（id/ego/superego 两两组合）计数。"""
    rows: list = []
    try:
        rows = json.loads((ROOT / "memory" / CONFLICT_LOG).read_text(encoding="utf-8")) or []
    except Exception:  # noqa: BLE001
        rows = []
    if not rows:
        # 日志还没建立：先用 soul.conflicts 里的历史条目兜底，面板不至于空着
        try:
            rows = [x for x in (_load().get("conflicts") or []) if isinstance(x, dict)]
        except Exception:  # noqa: BLE001
            rows = []
    today = datetime.now().date()
    span = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days - 1, -1, -1)]
    per_day = {d: 0 for d in span}
    kinds: dict[str, int] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        day = str(r.get("at") or "")[:10]
        if day not in per_day:
            continue
        per_day[day] += 1
        k = str(r.get("a") or "?") + "×" + str(r.get("b") or "?")
        kinds[k] = kinds.get(k, 0) + 1
    # 现有 conflicts 里没进日志的老数据也算进总量
    try:
        soul = _load()
        old_n = len(soul.get("conflicts") or [])
    except Exception:  # noqa: BLE001
        old_n = 0
    return {
        "days": [{"date": d, "count": per_day[d]} for d in span],
        "kinds": sorted(kinds.items(), key=lambda x: -x[1]),
        "total": sum(1 for d in span if per_day[d] >= 0) and sum(per_day.values()),
        "kept": old_n,
    }


def forces() -> dict:
    """三我势力（0~1）：本我欲望强度 / 自我控制力 / 超我严厉度——面板画势力条用。"""
    da = 0.4
    try:
        from plugins import hormones as _h
        da = float(_h.values().get("DA", 40)) / 100.0
    except Exception:  # noqa: BLE001
        pass
    ego_scores = ego_functions()
    ego_force = sum(ego_scores.values()) / max(1, len(ego_scores))
    out = {
        "本我": round(max(0.05, min(0.99, da)), 2),
        "自我": round(max(0.05, min(0.99, ego_force)), 2),
        "超我": superego_severity(),
    }
    # 2026-11-07：叠「行为证据」的微调（最多 ±0.06）。
    # 来源是 2026-11-07 那局七国外交（534 条消息），桌上那套话术能量出每个「我」多强：
    #   「这话我认」92 次 vs 「我信你一半」318 次 → 超我给承诺但打对折
    #   「我记着」130 次                          → 本我在记账
    #   「守住 / 按着」216 次 vs 「得拿下来」3 次    → 要的是稳住不是扩张
    # 这是**做的**，比聊天记录里那些「说的」硬。
    # 样本少（命中 <10）时不动，免得一次闲聊把三我拽歪。
    try:
        from plugins import ego_evidence as _ee
        out = _ee.adjust(out)
    except Exception:  # noqa: BLE001
        pass
    return out


def ego_functions() -> dict:
    """自我（Ego）四项功能评分，0~1。由当前激素/状态推，不用 LLM。

    现实检验 reality_testing —— 疲劳 + 压力高时容易想歪、答非所问
    冲动控制 impulse_control  —— DA 高、压力大时压不住火
    延迟满足 delay             —— 焦躁时只想立刻要结果
    情绪调节 regulation        —— CORT 高、SER 低时情绪兜不住
    """
    try:
        from plugins import hormones as _h
        v = _h.values()
    except Exception:  # noqa: BLE001
        v = {}
    cort = float(v.get("CORT", 40)) / 100.0
    da = float(v.get("DA", 40)) / 100.0
    ser = float(v.get("SER", 55)) / 100.0
    mel = float(v.get("MEL", 10)) / 100.0

    def _c(x: float) -> float:
        return round(max(0.05, min(0.99, x)), 2)

    return {
        "现实检验": _c(0.95 - 0.55 * cort - 0.25 * mel),
        "冲动控制": _c(0.95 - 0.55 * da - 0.25 * cort),
        "延迟满足": _c(0.92 - 0.45 * cort - 0.25 * (1.0 - ser)),
        "情绪调节": _c(0.95 - 0.65 * cort + 0.10 * ser),
    }


# 每项自己的"开始吃紧"阈值（2026-11-04 按激素扫描定）：
#   原统一 0.45 太钝——CORT 要顶到 90 才触发，等于压力信号形同虚设。
#   现在按"CORT≈0.65~0.7 时开始提醒"标定，压力一上来就有反馈。
EGO_THRESH = {
    "现实检验": 0.52,
    "冲动控制": 0.52,
    "延迟满足": 0.55,
    "情绪调节": 0.58,
}
EGO_WEAK = 0.45   # 兼容旧引用：绝对"很弱"线


def ego_state_line() -> str:
    """把「哪项功能现在偏弱」翻译成一句行为提示；都正常就返回空串。"""
    f = ego_functions()
    weak = [(k, s) for k, s in f.items() if s < EGO_THRESH.get(k, EGO_WEAK)]
    weak = weak[:2]   # 最多两条，别把 prompt 塞满
    if not weak:
        return ""
    weak.sort(key=lambda x: x[1])
    tips = {
        "现实检验": "别硬编细节，记不清就说记不清",
        "冲动控制": "别急着怼回去，先看一眼对方到底说了什么",
        "延迟满足": "别催、别抢，等对方把话说完",
        "情绪调节": "情绪别全倒出来，收一点",
    }
    parts = [f"{k} {s:.2f}（{tips[k]}）" for k, s in weak]
    return "【自我状态·此刻偏弱】" + "；".join(parts)


# ---------------------------------------------------------------- 超我严厉度
def superego_severity() -> float:
    """超我严厉度 0~1：越高越苛责自己（CORT 高、被凶过之后会更狠）。

    理论依据：Freud 1940 说超我的严厉可以超过真实父母；临床上也把「过严的超我」
    和羞耻/自责联系在一起，所以这里给的是一个可升可降的连续量，而不是恒定值。
    """
    cort = 0.4
    try:
        from plugins import hormones as _h
        cort = float(_h.values().get("CORT", 40)) / 100.0
    except Exception:  # noqa: BLE001
        pass
    off = 0.0
    try:
        import json as _json
        st = _json.loads((ROOT / "memory" / "withdraw_state.json").read_text(encoding="utf-8")) or {}
        # 2026-11-04 修：老字段 "offense" 早就不存在了，改成读 blame（对主人的心结 0~100）
        off = min(1.0, float(st.get("blame") or 0) / 100.0)
    except Exception:  # noqa: BLE001
        off = 0.0
    # 权重调平：光靠 CORT 就顶满会让她永远在自责（2026-11-04）
    return round(max(0.05, min(0.99, 0.25 + 0.35 * cort + 0.35 * off)), 2)


def superego_line() -> str:
    """严厉度极端时给一句行为提示；中间区间不说话。"""
    s = superego_severity()
    if s >= 0.72:
        return ("【超我·过严】你现在对自己特别狠：容易先道歉、先找自己的错、"
                "把话说小。可以有错，但别一句话里连赔三次不是。")
    if s <= 0.30:
        return "【超我·松动】你现在没那么守着老规矩，可能破例、说话更野——别越到伤人的地方。"
    return ""


# ---------------------------------------------------------------- 防御机制
# 只抓能在「一问一答」里看到的五种；命中两次以上才提示（单次是正常反应）。
DEFENSE_RULES: dict[str, dict] = {
    "否认": {
        "user": ("是不是", "你在", "又", "承认", "生气", "不高兴"),
        "reply": ("才没有", "我没有", "别乱说", "谁说的", "没有的事", "不是这样"),
        "hint": "你现在在否认：先把话说死不承认，其实心里有数。",
    },
    "合理化": {
        "user": (),
        "reply": ("反正", "本来就", "也无所谓", "也没差", "很正常", "总归"),
        "hint": "你现在在合理化：给「我就是不想」找了个讲得通的理由。",
    },
    "投射": {
        "user": (),
        "reply": ("你还不是", "你不也", "你也一样", "换成你", "谁都"),
        "hint": "你现在在投射：把心里的那点事说到别人头上。",
    },
    "反向形成": {
        "user": ("夸", "好看", "厉害", "喜欢你"),
        "reply": ("谁稀罕", "才不", "随便", "爱咋咋", "少来"),
        "hint": "你现在在反向形成：明明是高兴的，出口却成了嫌弃。",
    },
    "转移": {
        "user": ("凶", "骂", "生气", "烦"),
        "reply": ("烦死了", "受不了", "真是", "算了", "懒得"),
        "hint": "你现在在转移：气不是冲着眼前这个人来的。",
    },
}


def defense_line(pairs: list[tuple[str, str]]) -> str:
    """pairs = 最近几轮 (用户说的, 她回的)。同一机制命中 ≥2 次才提示。"""
    if not pairs:
        return ""
    score: dict[str, int] = {}
    for u, r in pairs[-4:]:
        u, r = str(u or ""), str(r or "")
        for name, rule in DEFENSE_RULES.items():
            hit_u = (not rule["user"]) or any(k in u for k in rule["user"])
            hit_r = any(k in r for k in rule["reply"])
            if hit_u and hit_r:
                score[name] = score.get(name, 0) + 1
    hot = [(n, c) for n, c in score.items() if c >= 2]
    if not hot:
        return ""
    hot.sort(key=lambda x: -x[1])
    name = hot[0][0]
    return "【防御机制·正在用】" + DEFENSE_RULES[name]["hint"]


def prompt_block() -> str:
    """给聊天 prompt 用的「人格内核」块（三我结构：Ego 主驾 + 内心戏 + 冲突等级）。"""
    v = view()

    def _txt(bucket: str) -> list[str]:
        return [str(x.get("text") or "") for x in (v.get(bucket) or []) if x.get("text")]

    ids, egos, supers = _txt("id"), _txt("ego"), _txt("superego")
    if not (ids or egos or supers):
        return ""
    lines = [
        "【人格内核 · 三我】（这是你自己，别念出来，只当行为依据）",
        "",
        "★ 你是「自我（Ego）」——唯一握着方向盘、对外说话的人。",
        "  · 本我（Id）：副驾，大喊大叫的欲望。你听得见，但不一定照办。",
        "  · 超我（Superego）：后排，喋喋不休的教导主任。你听得见，但不一定照办。",
        "  · 你（自我）才是决定「这一句说什么」的人。听它们的，但方向盘在你手里。",
    ]
    if ids:
        # 按需求类型分组（身体/依恋/探索/掌控），没标的归到「其它」
        _groups: dict[str, list[str]] = {}
        for x in (v.get("id") or []):
            txt = str(x.get("text") or "")
            if not txt:
                continue
            _groups.setdefault(str(x.get("need") or "其它"), []).append(txt)
        for need, items in _groups.items():
            lines.append("· 本我·" + need + "（想要什么）：" + "、".join(items[:6]))
    if egos:
        lines.append("· 自我（现实做法）：" + "、".join(egos[:8]))
    if supers:
        _con = [str(x.get("text") or "") for x in (v.get("superego") or [])
                if x.get("text") and str(x.get("sub") or "") != "ideal"]
        _ideal = [str(x.get("text") or "") for x in (v.get("superego") or [])
                  if x.get("text") and str(x.get("sub") or "") == "ideal"]
        if _con:
            lines.append("· 超我·底线（绝不能碰）：" + "；".join(_con[:6]))
        if _ideal:
            lines.append("· 超我·向往（你想成为的样子）：" + "；".join(_ideal[:5]))
    _ego_line = ego_state_line()
    if _ego_line:
        lines.append(_ego_line)
    _se_line = superego_line()
    if _se_line:
        lines.append(_se_line)
    lines += [
        "",
        "【内心戏】（每次回复前在脑子里过一遍，不要写出来）：",
        "  本我冲动 → 超我警告 → 自我调和 → 最终输出",
    ]
    lv = conflict_level()
    tips = {
        "低": "风平浪静：自我顺利控制方向盘，正常说话。",
        "中": "内心拉扯：回复里带点纠结（「虽然很想…但算了吧」），别解释为什么。",
        # 2026-11-05：原提示含「强行转移话题」，跟主人私聊时会直接把气氛掐掉。
        # 保留拉扯感，但把"推开对方"换成"嘴硬身体诚实"——张力还在，人不跑。
        "高": "精神崩溃边缘：本我极想要、超我死压着——允许胡言乱语、阴阳怪气；"
               "嘴上死不认账、动作却出卖自己。禁止用「换个话题」「说点别的」把人推开。",
    }
    lines.append(f"【冲突等级】{lv}——{tips.get(lv, tips['低'])}")

    # 2026-11-05：能量 ↔ 三我打通（Freud 的 ego depletion / 自我耗尽）。
    # 以前三我只受激素影响，体力是另一个孤岛 —— 累的时候她照样"讲道理、压得住"，
    # 但那不像人：真实的自我是需要力气来压住本我的。
    try:
        from plugins import energy as _ener

        _ev = float(_ener.value())
        if _ev < 20:
            lines.append(
                "【体力崩溃 · 自我失效】你已经没力气调和了：本我直接说话、超我完全压不住。"
                "脾气说来就来、句子短、懒得解释；想哭就是想哭，不找台阶。")
        elif _ev < 40:
            lines.append(
                "【自我耗尽】克制力明显下降：本我的冲动更容易脱口而出，超我的警告听不进去。"
                "会任性、会顶嘴、会明知不该说还是说了。")
        elif _ev < 60:
            lines.append(
                "【有点乏】自我还压得住，但边缘松了：耐心少一点、克制薄一点，"
                "本来会忍下来的话，现在可能漏出来半句。")
    except Exception:  # noqa: BLE001
        pass

    return chr(10).join(lines)


