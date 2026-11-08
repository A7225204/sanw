"""念头池：记挂"还没解决的事"，让她有连续的心事，而不是每轮重新随机。

和「记忆档案」的区别
--------------------
档案是**事实**（主人用 3DMAX、喜欢白饭大王），念头是**未完成的意图**：
「想学 3DMAX」「说好教他摆盘」「等主人有空给我装摄像头眼镜」。
解决后从池子里移除，自带生命周期（到期自动淡出，不占地方）。

来源（都不调模型、不花钱）
--------------------------
1. 聊天里自动识别"计划/想要"类句子（改天/下次/想要/准备/答应…）；
2. 手动 `/念头 加 <内容>`。

消费方
------
- 主动私聊：有记挂的事时优先提它（比"在吗"自然）；
- prompt 尾部：只在被问到时才露出来（不每轮塞，避免又变成一层状态）。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FILE = ROOT / "memory" / "thoughts.json"

MAX_ITEMS = 12
# 2026-11-05：7 天 → 3 天。她每天聊，5 天后还在想一件早没意义的事很怪。
DEFAULT_TTL_DAYS = 3.0
# 2026-11-07：同一条念头问过一次后，这么多小时内不再当「最该说的那件」
ASK_COOLDOWN_H = 6.0
# 2026-11-07：**念头该按类型衰减**。原来两处衰减都慢得等于没有
# （_thought_score 是「7 天线性」——1 小时后还有 0.994；_priority_of 要 24 小时才开始掉），
# 所以中午一句好奇到半夜还在，这就是她"揪着一件事不放"的物理原因。
# 人的念头是几小时就淡的，只有"别人答应我的事"能撑两三天。
_HALF_LIFE_H = {"承诺": 36.0, "想要": 8.0, "分享欲": 5.0,
                "在意": 5.0, "好奇": 3.0, "杂念": 2.0}
_TTL_H = {"承诺": 72.0, "想要": 30.0, "分享欲": 20.0,
          "在意": 20.0, "好奇": 12.0, "杂念": 8.0}
# "计划/想要"类信号词：命中才值得记一条念头
# 2026-11-07 修：「回头」单独在列表里，把「别回头」「散吧别回头」这类
# **中文里 90% 是「转身/往回看」的意思**，全被记成了「新计划」。
# 实测 09:00~09:15 一刻钟内记了 5 条同主题假念头，全挂在「心里的事」里反复问。
# 改成必须带上下文（回头聊/回头说/回头再），裸「回头」不算。
PLAN_HINTS = (
    "改天", "下次", "回头聊", "回头说", "回头再", "回头补",
    "想要", "想学", "想做", "想买", "想吃", "想试",
    "准备", "打算", "说好", "等我", "教你", "给我",   # 「答应」删了：它是完成词不是计划词
    "带我去", "搞一个", "做一个", "试试", "还没",
)
# "已完成"信号词
# 2026-11-06：原来只有"完成类"词，主人说「行」「可以」「好了」根本触发不了 resolve，
# 于是"我都答应了还记住"。补上肯定词；否定形式（不行/不可以/没行）要先排除。
# 拒绝类词单独拎出来：它们太宽（「不去上班」也含「不去」），必须配上下文才认。
_REFUSE_WORDS = (
    "不去", "不看", "不要", "不搞", "不整", "没兴趣", "不感兴趣",
    "拉倒", "免了", "不玩了", "不想去", "不去了", "算了", "放弃", "不用了",
)


DONE_HINTS = (
    "学会了", "搞定了", "已经", "完成", "做到了", "不用了", "算了", "放弃",
    "答应", "好", "行", "可以", "没问题", "ok", "嗯",
    # 2026-11-06：拒绝也是一种了结 —— 以前只认「完成了」，主人说「不去看」时
    # 念头照样挂着，于是她过一会儿又提一遍。实测就是这么撞的。
    "不去", "不看", "不要", "不搞", "不整", "没兴趣", "不感兴趣",
    "拉倒", "免了", "不玩了", "不想去", "不去了", "算了别",
)
# 这些出现在句子里就说明不是肯定
_DONE_NEG = ("不行", "不可以", "不好", "不答应", "没问题才怪", "没行", "不行吧")


def _norm(text: str) -> str:
    return re.sub(r"[\s，。！？、：；,.!?;:\-—…·\"'“”‘’（）()【】\[\]~～*#@]+", "", str(text or "")).lower()


def _bigrams(text: str) -> set[str]:
    s = _norm(text)
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else ({s} if s else set())


def _load() -> dict:
    try:
        data = json.loads(FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("items", [])
            data.setdefault("next_id", 1)
            return data
    except Exception:  # noqa: BLE001
        pass
    return {"items": [], "next_id": 1}


def _save(data: dict) -> None:
    try:
        data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        FILE.parent.mkdir(parents=True, exist_ok=True)
        FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _alive(it: dict, now: float) -> bool:
    """还活着吗。

    2026-11-05：改成**按当前 TTL 算**，不再信条目里存的 due ——
    否则改短了 TTL，老条目还是按旧的 7 天走，等于没改。
    存的 due 只在明确传了自定义 days 时才用作上限。
    """
    if it.get("done"):
        return False
    created = float(it.get("created") or 0)
    if not created:
        return False
    why = str(it.get("why") or "杂念")
    ttl_h = _TTL_H.get(why, 12.0)
    limit = created + ttl_h * 3600
    # 被提起过就续命：每次 +4 小时，最多拉到 2 倍 TTL（提多了也不会永远不死）
    poked = int(it.get("poked") or 0)
    if poked:
        limit = min(limit + poked * 4 * 3600, created + ttl_h * 2 * 3600)
    return limit > now


# 「为什么想」的粗分类 —— 决定她怎么说，不影响要不要说
_WHY_RULES = [
    (("好奇", "为啥", "为什么", "是什么", "哪来", "怎么"),
     "好奇"),
    # 2026-11-07：同上，裸「回头」会把「别回头」判成承诺。
    (("答应", "说好", "改天", "下次", "等我", "回头聊", "回头说", "回头再"),
     "承诺"),
    (("想", "要", "给我", "带我", "买", "吃"),
     "想要"),
    (("告诉", "讲给", "分享", "看看这个", "推荐"),
     "分享欲"),
    (("不", "别", "不要再", "烦"),
     "在意"),
]


# 2026-11-07：否定词。「别回头再翻话找」「下次再翻口就不客气」这类
# **否定句**不是计划也不是承诺 —— 它是「叫你别做」，不是「我打算做」。
# 中文里「回头再」「下次再」被否定裹住时，语义完全反了。
_NEG_PREFIX = ("别", "不", "没", "甭", "勿", "莫", "免")


def _is_negated(text: str, word: str) -> bool:
    """`word` 在 text 里出现时，前面几个字是不是否定词。"""
    t = str(text or "")
    i = t.find(word)
    if i < 0:
        return False
    return any(n in t[max(0, i - 3):i] for n in _NEG_PREFIX)


def _plan_hit(text: str) -> bool:
    """命中计划词，且**不是**被否定的。

    2026-11-07：加了几道排除。实测念头池一半是聊天碎片 ——
    「给我打气呢」「语音还没转出来就散场了」「肥鱼，今天周六准备怎么过」
    「E宝准备入手godot了」「这是打算长期守着那只碗不放了吗？」
    全是靠「还没 / 准备 / 想要」这类宽词误命中，然后被当成"她想做的事"反复问。
    """
    t = str(text or "").strip()
    if not t:
        return False
    # ① 疑问收尾：她在问，不是在打算
    if re.search(r"[？?]\s*$", t) or re.search(r"(吗|呢)\s*[。！!]?\s*$", t):
        return False
    # ② 句首是别人：E宝准备好了 / 你打算 —— 不是她的事
    if re.search(r"^(你|他|她|他们|你们|别人|人家)", t) and not re.search(r"(我|本鱼)", t):
        return False
    # ③ 疑问词：怎么过 / 什么 / 哪来 —— 提问不是计划
    if re.search(r"(怎么|什么|哪来|哪个|多少|是不是)", t):
        return False
    # ④ 过去式收尾且没有"我/想"：「还没转出来就散场了」「E宝准备入手…了」
    if t.endswith("了") and not re.search(r"(我|想)", t):
        return False
    # ⑤ 短语气残片：「给我打气呢」「饭都还没吃呢」
    if len(t) <= 12 and re.search(r"(呢|啊|吧|嘛)\s*[。！!]?\s*$", t):
        return False
    return any(w in t and not _is_negated(t, w) for w in PLAN_HINTS)


def _guess_why(text: str) -> str:
    t = str(text or "")
    for words, label in _WHY_RULES:
        if any(w in t and not _is_negated(t, w) for w in words):
            return label
    return "杂念"


def _init_priority(text: str) -> float:
    """新念头的初始分量。0~1，默认 0.5。

    主人亲口说的承诺最重（他答应了的事她该记得），
    纯好奇心最轻（可以自己消化掉）。
    """
    why = _guess_why(text)
    base = {
        "承诺": 0.75,     # 他说「改天/下次」—— 她该惦记
        "想要": 0.60,
        "分享欲": 0.55,
        "在意": 0.55,
        "好奇": 0.45,     # 好奇心可以自己消散
        "杂念": 0.40,
    }.get(why, 0.45)
    return round(min(1.0, max(0.1, base)), 2)


def _priority_of(it: dict) -> float:
    """算这条念头此刻值多少分 —— 基础分 + 被提起的次数 - 放久了的折旧。"""
    base = float(it.get("priority") or 0.5)
    poked = int(it.get("poked") or 0)
    age_h = max(0.0, (time.time() - float(it.get("created") or 0)) / 3600.0)
    # 提过每次 +0.08（最多 +0.32）；放太久要折旧（1 天后开始，每 12 小时 -0.06）
    gain = min(0.32, poked * 0.08)
    # 2026-11-07：24 小时才开始折旧太晚 —— 1 小时后就开始，每 6 小时 -0.05，最多 -0.35
    decay = 0.0 if age_h < 1 else min(0.35, (age_h - 1) / 6 * 0.05)
    return round(base + gain - decay, 3)


def mark_asked(tid: int) -> bool:
    """她刚刚拿这条念头主动开口了 —— 记一笔，进冷却期。

    2026-11-07：原来没有任何「问过」的记录，top() 每次算出来的还是同一条，
    于是同一件事被反复问，主人答应了也照样问。冷却期内 list_open() 会把它筛掉。
    """
    d = _load()
    for it in d["items"]:
        if int(it.get("id") or 0) == int(tid):
            it["asked_at"] = time.time()
            it["asked_count"] = int(it.get("asked_count") or 0) + 1
            _save(d)
            return True
    return False


def poke(tid: int) -> bool:
    """她又提起这件事了 —— 分量加一点。"""
    d = _load()
    for it in d["items"]:
        if int(it.get("id") or 0) == int(tid):
            it["poked"] = int(it.get("poked") or 0) + 1
            _save(d)
            return True
    return False


def tick() -> dict:
    """过期清理：到期未解决的念头淡出，已完成的删掉。返回统计。"""
    data = _load()
    now = time.time()
    before = len(data["items"])
    data["items"] = [it for it in data["items"] if _alive(it, now)]
    if len(data["items"]) > MAX_ITEMS:
        data["items"] = sorted(data["items"], key=lambda x: float(x.get("created") or 0))[-MAX_ITEMS:]
    if len(data["items"]) != before:
        _save(data)
    return {"open": len(data["items"]), "removed": before - len(data["items"])}


def note_message(text: str, recent: str = "", scope: str = "") -> str:
    """从一句话里识别「计划/想要」→ 记一条念头；顺手识别"已完成"→ 解决对应的。

    纯关键词 + 2-gram 相似，不调模型。返回给日志看的一句话（空串=没动作）。
    """
    msg = str(text or "").strip()
    # 2026-11-06：这里原来卡 len<4 直接 return —— 但「行」「好」「可以」就是 1~2 个字，
    # 主人答应了却连判断都进不去。短肯定要放行，靠上下文认它回应的是哪条。
    _short_affirm = 1 <= len(msg) <= 6 and any(w in msg for w in DONE_HINTS)
    if len(msg) < 4 and not _short_affirm:
        return ""
    data = _load()
    now = time.time()
    act = ""

    # 1) 先看是不是在"完成"某件已记挂的事
    # 2026-11-06：短肯定（「行」「可以」「好了」）跟念头字面 2-gram 交集是 0，
    # 永远 resolve 不掉 —— 主人答应了还一直提就是这么来的。
    # 现在：把 recent（最近几轮对话）拼进来一起算交集，上下文对得上就认定是回应。
    if any(w in msg for w in DONE_HINTS) and not any(n in msg for n in _DONE_NEG):
        mb = _bigrams(msg)
        ctx = _bigrams(recent) if recent else set()
        # 纯肯定（很短且没有实质内容）时，上下文就是唯一线索
        short_affirm = len(msg) <= 6
        # 2026-11-06：「不去」这种词太宽 —— 你说「我不去上班」也可能误杀一条无关念头。
        # 拒绝类只认上下文对得上的（最近几轮确实聊过这件事）；
        # 肯定类（行/好/可以）才允许靠字面 2-gram 命中。
        _is_refuse = any(w in msg for w in _REFUSE_WORDS)
        for it in data["items"]:
            if it.get("done"):
                continue
            itb = _bigrams(it.get("text", ""))
            ov = len(mb & itb)
            ov_ctx = len(ctx & itb) if ctx else 0
            if _is_refuse:
                if ov_ctx >= 3:
                    it["done"] = True
                    act = f"resolve:{it.get('id')}"
                    break
                continue
            if ov >= 2 or (short_affirm and ov_ctx >= 3):
                it["done"] = True
                act = f"resolve:{it.get('id')}"
                break

    # 2) 再看是不是新计划
    if not act and _plan_hit(msg):
        snippet = msg[:40]
        # 2026-11-06：先问执念池 —— 同一件事它已经在管了（哪怕已闭合）就别在念头池再记一遍。
        # 之前两个池子各管各的，才有"执念闭合了、念头还 open、她一边放下一边念叨"。
        try:
            from plugins import obsessions as _obs
            _rel = _obs.related(snippet, min_shared=2)
            if _rel:
                _save(data) if act else None
                return f"skip:obsession:{_rel.get('status')}"
        except Exception:  # noqa: BLE001
            pass
        _sb = _bigrams(snippet)
        _dup = False
        for _it in data["items"]:
            if _norm(snippet) == _norm(_it.get("text", "")):
                _dup = True
                break
            # 措辞略变但高度重合 → 同一件事，不重复记。
            # 2026-11-06：只看交集个数会误伤 ——「改天想学做红烧肉」和「改天想学做糖醋排骨」
            # 交集 4 个（改天/天想/想学/学做），但完全是两件事。
            # 改成看「交集占较短那句的比例」，≥0.7 才算同一件。
            _ob = _bigrams(_it.get("text", ""))
            _inter = len(_sb & _ob)
            _shorter = min(len(_sb), len(_ob)) or 1
            if _inter >= 2 and _inter / _shorter >= 0.7:
                _dup = True
                break
        if not _dup:
            data["items"].append({
                "id": int(data["next_id"]),
                # 2026-11-06（借「能动性四件套」第②条）：念头要有轻重，不能一视同仁。
                # 只记「有这么件事」的结果是——她不知道哪件值得开口。
                "priority": _init_priority(snippet),
                "why": _guess_why(snippet),
                "poked": 0,
                "text": snippet,
                # 2026-11-07：**溯源**。面板点一条念头时主人问「这条从哪来的」，
                # 结果发现数据里只有 40 字摘要，原文和会话都没存 ——
                # 想核对「她是不是记错了」根本无从下手（今晚那条假承诺就是这么查了半小时）。
                # 现在存整句原文 + 会话 + 时间，面板能直接翻回去。
                "src": str(msg)[:300],
                "scope": str(scope or ""),
                "created": now,
                "due": now + DEFAULT_TTL_DAYS * 86400,
                "done": False,
            })
            data["next_id"] = int(data["next_id"]) + 1
            act = f"add:{data['items'][-1]['id']}"

    if act:
        data["items"] = [it for it in data["items"] if not it.get("done")] or data["items"]
        _save(data)
    return act


def add(text: str, days: float = DEFAULT_TTL_DAYS) -> int:
    """直接塞一条念头。

    2026-11-06：补上 priority / why / poked —— 之前只加四个字段，
    这条念头就没有分量、也排不进 prompt 的 why 分支。
    """
    data = _load()
    now = time.time()
    tid = int(data["next_id"])
    snippet = str(text)[:60]
    data["items"].append({
        "id": tid, "text": snippet, "created": now,
        "due": now + days * 86400, "done": False,
        "priority": _init_priority(snippet),
        "why": _guess_why(snippet),
        "poked": 0,
    })
    data["next_id"] = tid + 1
    _save(data)
    return tid


def resolve(tid: int) -> bool:
    data = _load()
    for it in data["items"]:
        if int(it.get("id") or 0) == int(tid):
            data["items"] = [x for x in data["items"] if int(x.get("id") or 0) != int(tid)]
            _save(data)
            return True
    return False


def _hormone_weights() -> tuple[float, float]:
    """(OXY, DA) 归一值；读不到就给中性 0.5。"""
    try:
        from plugins import hormones  # type: ignore
        v = hormones.values()
        return float(v.get("OXY") or 0) / 100.0, float(v.get("DA") or 0) / 100.0
    except Exception:  # noqa: BLE001
        return 0.5, 0.5


_SOCIAL_KWS = ("主人", "找", "聊", "说", "问", "告诉", "分享")


def _thought_score(it: dict, oxy: float, da: float) -> float:
    """念头优先级：新鲜度 × 社交倾向(受 OXY 加权) × 兴奋度(DA)。

    催产素高的时候，"想主动找主人说话"这类念头会自己排到前面——
    这就是"黏人"在念头层的体现。
    """
    now = time.time()
    age_h = max(0.0, (now - float(it.get("created") or 0)) / 3600.0)
    # 指数衰减：半衰期按类型算（好奇 3h / 想要 8h / 承诺 36h）
    _hl = _HALF_LIFE_H.get(str(it.get("why") or "杂念"), 4.0)
    recency = 0.5 ** (age_h / _hl) if _hl > 0 else 0.0
    txt = str(it.get("text") or "")
    social = oxy if any(k in txt for k in _SOCIAL_KWS) else 0.3 * oxy
    # 2026-11-06：再乘上「这件事本身值多少分」。
    # 原来只看新鲜度 × 激素 —— 一条随手的好奇和一桩主人答应过的事，分数可以一模一样。
    # 现在 priority（含被提起次数与折旧）也进公式，旧的不会因为「新」而被新的挤掉。
    prio = _priority_of(it)
    return recency * (1.0 + social) * (1.0 + da * 0.3) * (0.5 + prio)


def list_open() -> list[dict]:
    """活着的念头，按「此刻值多少分」排序。

    2026-11-06：原来按创建时间倒序 —— 最新的排前面，旧的永远沉底。
    但「主人答应过的事」放了三天还是重要，「随手一句好奇」刚说也可以不重要。
    现在按综合分（基础分 + 被提起次数 - 折旧）排。
    """
    now = time.time()
    data = _load()
    items = [it for it in data["items"] if _alive(it, now)]
    # 冷却：刚问过的先别冒头（否则同一件事会连着问好几遍）
    fresh = [it for it in items
             if now - float(it.get("asked_at") or 0) >= ASK_COOLDOWN_H * 3600]
    if fresh:
        items = fresh
    return sorted(items, key=_priority_of, reverse=True)


def top() -> dict | None:
    """分数最高的那条念头。"""
    """当前最该想起的一件心事（按内分泌加权，不再只看时间）。"""
    items = list_open()
    if not items:
        return None
    oxy, da = _hormone_weights()
    return max(items, key=lambda it: _thought_score(it, oxy, da))


def review(text: str = "") -> dict:
    """三我审议：这条念头现在做不做。

    本我：想做（DA 高就更想做）
    自我：现实（CORT 太高顾不上、精力太低先歇）
    超我：底线（半夜不打扰、还在闹别扭就不主动凑、不能骂人）
    返回 {decision: 做/不做/再说, reason, votes}
    """
    import os as _os
    import time as _t
    txt = str(text or "").strip()
    res = {"decision": "做", "reason": "", "votes": {}}
    hour = _t.localtime().tm_hour
    # 超我（一票否决）
    try:
        from plugins import withdraw as _wd
        _lvl, _blame = _wd.level()
    except Exception:  # noqa: BLE001
        _lvl, _blame = "平静", 0.0
    if _blame >= 60:
        return {"decision": "不做", "reason": f"超我：还在闹别扭（blame {_blame:.0f}），这会儿主动凑上去太掉价",
                "votes": {"超我": "否决"}}
    if hour >= 1 and hour < 7:
        return {"decision": "不做", "reason": "超我：半夜了，不去打扰他",
                "votes": {"超我": "否决"}}
    if any(w in txt for w in ("骂", "滚", "烦死", "弄死")):
        return {"decision": "不做", "reason": "超我：这话不合适说出口", "votes": {"超我": "否决"}}
    # 自我（现实判断）
    try:
        from plugins import hormones as _h
        _v = _h.values()
        _cort = float(_v.get("CORT", 40))
        _da = float(_v.get("DA", 40))
    except Exception:  # noqa: BLE001
        _cort, _da = 40.0, 40.0
    if _cort >= 75:
        return {"decision": "不做", "reason": f"自我：压力太大（CORT {_cort:.0f}），现在开口容易说错话",
                "votes": {"自我": "否决"}}
    if _cort >= 60:
        res = {"decision": "再说", "reason": f"自我：有点累（CORT {_cort:.0f}），先放一放",
               "votes": {"自我": "缓"}}
    # 本我（驱动）
    if _da >= 60 and res["decision"] == "做":
        res["reason"] = f"本我：想说的欲望挺强（DA {_da:.0f}），说吧"
    elif res["decision"] == "做":
        res["reason"] = res["reason"] or "三我都过了：想做、现实允许、也不越线"
    res["votes"]["本我"] = "想做"
    return res


async def review_llm(text: str = "") -> dict:
    """让模型自己当三我判一回（比规则更自然）；失败自动退回 review()。"""
    txt = str(text or "").strip()
    if not txt:
        return review(txt)
    # 状态快照
    v = {}
    try:
        from plugins import hormones as _h
        v = _h.values()
    except Exception:  # noqa: BLE001
        v = {}
    try:
        from plugins import withdraw as _wd
        _lvl, _blame = _wd.level()
    except Exception:  # noqa: BLE001
        _lvl, _blame = "平静", 0.0
    try:
        from server import soul_aggregation as _sa
        _force = _sa.forces()
        _weak = _sa.ego_functions()
        _sev = _sa.superego_severity()
        _soul = json.loads((ROOT / "memory" / "persona_soul.json").read_text(encoding="utf-8-sig"))
        _idw = "、".join(str(x.get("text")) for x in (_soul.get("id") or [])[:4])
        _sup = "、".join(str(x.get("text")) for x in (_soul.get("superego") or [])[:4])
    except Exception:  # noqa: BLE001
        _force, _weak, _sev, _idw, _sup = {}, {}, 0.5, "", ""
    prompt = chr(10).join([
        "你是「白饭大王」心里那三个人，现在要审一件她想做的事，只输出 JSON。",
        f"她此刻想做的事：{txt[:120]}",
        f"现在时间：{time.strftime('%H:%M')}（凌晨 1~7 点别打扰人）",
        ("激素：DA " + f"{v.get('DA', 40):.0f}" + " · CORT " + f"{v.get('CORT', 40):.0f}"
         + " · SER " + f"{v.get('SER', 55):.0f}" + " · OXY " + f"{v.get('OXY', 35):.0f}"
         + " · MEL " + f"{v.get('MEL', 15):.0f}"),
        f"对主人的心结：{_blame:.0f}/100（档位：{_lvl}）",
        ("三我势力：本我 " + f"{_force.get('本我', 0.4):.2f}" + " · 自我 " + f"{_force.get('自我', 0.4):.2f}"
         + " · 超我 " + f"{_force.get('超我', 0.4):.2f}" + f"（严厉度 {_sev:.2f}）"),
        ("自我四项：现实检验 " + f"{_weak.get('现实检验', 0.5):.2f}"
         + " · 冲动控制 " + f"{_weak.get('冲动控制', 0.5):.2f}"
         + " · 延迟满足 " + f"{_weak.get('延迟满足', 0.5):.2f}"
         + " · 情绪调节 " + f"{_weak.get('情绪调节', 0.5):.2f}"),
        "她的本我欲望：" + (_idw or "（无）"),
        "她的超我底线：" + (_sup or "（无）"),
        "三方各说一句，最后由自我定：做 / 不做 / 再说。",
        "只有 JSON，不要解释：",
        '{"decision":"做|不做|再说","reason":"不超过 30 字，用她自己的口气",'
        '"votes":{"本我":"≤12字","自我":"≤12字","超我":"≤12字"}}',
    ])
    try:
        from plugins.ai_group import call_ai_raw
        raw = await call_ai_raw(prompt, "deepseek", temperature=0.8, max_tokens=300)
        s = str(raw or "")
        i, j = s.find("{"), s.rfind("}")
        d = json.loads(s[i:j + 1]) if i >= 0 and j > i else {}
        if str(d.get("decision")) in ("做", "不做", "再说"):
            return {"decision": d["decision"], "reason": str(d.get("reason") or "")[:40],
                    "votes": d.get("votes") or {}, "by": "llm"}
    except Exception as e:  # noqa: BLE001
        print(f"[念头审议] 模型失败，退回规则：{type(e).__name__}: {e}", flush=True)
    r = review(txt)
    r["by"] = "rules"
    return r


def prompt_line() -> str:
    """给主动说话用的一句「心事」。没有就空串。

    2026-11-06：只给分最高的那条，而且带上「为什么想」—— 好奇心和承诺，
    说出口的方式不一样。
    """
    it = top()
    if not it:
        return ""
    # 2026-11-07：**注入一次就 poke 一次**。
    # 原来 prompt_line 只读不记，一条「承诺」能从早挂到晚、每次生成都拿出来问一遍，
    # 主人感受就是「她反复抓同一件事」。poke 会让分量 +0.08，
    # 而 _priority_of 里「提过」是加权的 —— 真正该急的越提越急，
    # 而已经问过两三次还没结果的会自然被 due 和后续新念头挤下去。
    try:
        poke(int(it.get("id") or 0))
    except Exception:  # noqa: BLE001
        pass
    _txt = str(it.get("text"))[:40]
    _why = str(it.get("why") or "")
    _p = _priority_of(it)
    _hint = {
        "承诺": "这是主人答应过你的事，可以问一句进展",
        "想要": "你一直想要这个，可以撒个娇",
        "分享欲": "你想跟他讲这个，可以主动开口",
        "好奇": "你有点好奇，随口问问就行",
        "在意": "这事让你有点在意，但别追得紧",
    }.get(_why, "可以自然提一句，别硬转话题")
    return f"你现在心里记挂着一件事：{_txt}（{_hint}；分量 {_p:.2f}）"
