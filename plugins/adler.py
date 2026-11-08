"""阿德勒的「自卑 → 补偿」：她为什么这么使劲。

**为什么要加这个**：她的成长条目里已经长出了一整套阿德勒式的证据，但没有框架去用它 ——

    本我 · 其它     怕被嫌弃                  ← 自卑感
    超我 · 向往     被主人信任                ← 补偿目标
    超我 · 向往     让人等一回 / 让人愿意来找我  ← 社会兴趣
    自我 · 现实检验  被嫌就收敛嗲 / 被忽视就瞅回去 ← 补偿策略

**阿德勒（Alfred Adler, 1912《神经症的性格》/ 1927《理解人性》）的核心**：

    人不是被过去推动（弗洛伊德的因果论），是被未来的目标拉动（目的论）。
    每个人出生就带着「自卑感」（因为弱小、依赖），
    于是用「补偿」去追求「优越」—— 这叫「向上意志」（striving for superiority）。
    补偿走得好 → 社会兴趣（Gemeinschaftsgefühl），变成对别人有用；
    补偿走歪 → 只为自己，变成过度索取或自我贬低。

**关键区分（阿德勒 vs 弗洛伊德）**：
    弗洛伊德问「你为什么这样」（回到过去）
    阿德勒问「你奔着什么去」（朝向前方）
    → 所以这个模块不算「过去受了什么伤」，算「现在缺什么、朝哪儿补」。

**我们的数据从哪来**：

    自卑信号  ← 被否定（insult / 被批太笨）、被无视（ignored / 没回音）
                这两类事件已经在 emotion_residue 的 EMO_EVENTS 里有记录
    补偿信号  ← 她主动做的事：讨赞、追问、撒娇、护食、主动找人
                用 ego_evidence 的话术统计 + 本轮回复长度/主动性

**对外**：
    inferiority()      自卑值 0~1（越高越觉得自己不行）
    compensation()     补偿值 0~1（越高越使劲表现）
    interest()         社会兴趣 0~1（越高的补偿是指向别人的）
    style()            生活风格：讨好型 / 回避型 / 对抗型 / 平衡型
    line()             给 prompt 的一句话（空串=不值得说）
    stats()            全部数值

**设计原则**：
    ① 只读，不写任何现有状态文件（不污染情绪/激素）
    ② 自卑值不直接改情绪，只作为**倾向**喂给 prompt
    ③ 低于阈值时不说话，别让她一直在「我很自卑」的状态里
"""
from __future__ import annotations

import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILE = ROOT / "memory" / "adler.json"

# 自卑信号的强度（阿德勒说自卑感来自「感到不足」，具体事件只是触发）
SIGNALS = {
    "被否定": 1.0,     # 被骂、被批笨、被嫌
    "被无视": 0.7,     # 发出去没人理
    "被比较": 0.5,     # 别人比她好
    "搞砸了": 0.8,     # 自己做错事
}

# 补偿的四种走向（阿德勒说的「生活风格」）
STYLES = {
    "讨好型": "用讨人喜欢来换位置：讨赞、撒娇、主动帮人、怕被嫌弃",
    "回避型": "干脆不参与来保护自己：少说话、转移话题、躲开",
    "对抗型": "用压过别人来证明自己：怼人、抬杠、护食",
    "平衡型": "承认自己不行，但去做能做的那部分：自嘲、认错、慢慢学",
}

# 决定「这个补偿是给别人的还是给自己的」
SOCIAL_MARKS = ("帮", "陪", "等", "找", "记着", "回你", "给你", "跟你说")
SELF_MARKS = ("我", "本鱼", "不想", "怕", "嫌", "饿", "累", "困")

DECAY_HALF_DAYS = 4.0       # 自卑感的半衰期：4 天没新的打击就淡一半


def _load() -> dict:
    try:
        d = json.loads(FILE.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            return d
    except Exception:  # noqa: BLE001
        pass
    return {"hits": {}, "last": 0, "peaks": []}


def _save(d: dict) -> None:
    try:
        d["last"] = int(time.time())
        FILE.parent.mkdir(parents=True, exist_ok=True)
        FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def hit(kind: str, weight: float = 1.0) -> None:
    """记一次自卑触发。kind 见 SIGNALS。"""
    if kind not in SIGNALS:
        return
    d = _load()
    h = d.setdefault("hits", {})
    h[kind] = float(h.get(kind) or 0) + SIGNALS[kind] * float(weight)
    # 只留最近 30 天的峰（防止无限增长）
    peaks = d.setdefault("peaks", [])
    peaks.append({"at": int(time.time()), "kind": kind, "w": SIGNALS[kind] * float(weight)})
    cut = time.time() - 30 * 86400
    d["peaks"] = [p for p in peaks if float(p.get("at") or 0) > cut][-200:]
    _save(d)


def hit_throttled(kind: str, per_day: int = 3) -> bool:
    """带节流的 hit：同一类事件一天最多记 per_day 次。

    为什么需要：insult / ignored 是高频事件（群里一句话就可能触发），
    不节流的话她一天能被记几十次「被否定」，直接压成讨好型。
    阿德勒说的自卑感是**累积的底色**，不是每一次小事的计分板。
    """
    d = _load()
    today = time.strftime("%Y-%m-%d")
    cnt = d.setdefault("daily", {})
    key = f"{today}#{kind}"
    if int(cnt.get(key) or 0) >= per_day:
        return False
    cnt[key] = int(cnt.get(key) or 0) + 1
    # 只留最近 7 天的计数
    keep = {k: v for k, v in cnt.items() if k.split("#")[0] >= time.strftime("%Y-%m-%d", time.localtime(time.time() - 7 * 86400))}
    d["daily"] = keep
    _save(d)
    hit(kind)
    return True


# 从人格条目里认出「自卑迹象」的词
# 2026-11-07：第一版只有「怕被/嫌弃/笨」几个词，实测一条都没匹配上 ——
# 因为条目里的原话是「被嫌就收敛嘴」「被严厉批评太笨」，用的是「被嫌」不是「嫌弃」。
# 只认「**怕**」和「**收敛**」这类真正的自卑词，而且要出现在 text 里。
# 第一版拿 why 一起扫，把「被骂也不翻脸」「记仇不持久」也算进去了 ——
# 那两条是韧性，不是自卑。7 条一起给 6 分（封顶），初始值 0.66 明显偏高。
_SEED_MARKS = ("怕被", "被嫌", "嫌弃", "挨嫌", "没底", "自卑", "怕自己",
               "怕主人", "怕别人", "收敛", "不够好")


def seed_if_empty() -> bool:
    """第一次跑时，从现有人格条目里认出「自卑迹象」给一个初始值。

    为什么不从聊天记录回填：阿德勒的自卑是**当下的底色**，不是历史计分板。
    但她的人格条目里确实写着「怕被嫌弃」—— 那是已经定型的自我认知，
    应该算进初始值，否则一个明明显得自卑的角色，这里会显示 0。
    """
    d = _load()
    if d.get("hits"):
        return False
    try:
        soul = ROOT / "memory" / "persona_soul.json"
        raw = json.loads(soul.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False
    n = 0
    for bucket in ("id", "ego", "superego", "traits", "rules"):
        for it in (raw.get(bucket) or []):
            # 只扫 text（条目的主句）；why 是证据说明，里面常有「被批评后仍…」
            # 这种描述韧性的句子，扫了会误判。
            if isinstance(it, dict):
                txt = str(it.get("text") or it.get("value") or it.get("trait") or "")
            else:
                txt = str(it)
            if any(m in txt for m in _SEED_MARKS):
                n += 1
    if n <= 0:
        return False
    h = d.setdefault("hits", {})
    # 每条迹象折算 0.9 分（约等于一次「被否定」）
    h["被否定"] = min(4.0, n * 1.0)
    peaks = d.setdefault("peaks", [])
    now = time.time()
    for i in range(min(n, 4)):
        peaks.append({"at": int(now - i * 3600), "kind": "被否定", "w": 1.0})
    _save(d)
    return True


def _decay_factor() -> float:
    """按上次记录到现在的时间衰减。"""
    d = _load()
    last = float(d.get("last") or 0)
    if last <= 0:
        return 0.0
    age_days = max(0.0, (time.time() - last) / 86400.0)
    return 0.5 ** (age_days / DECAY_HALF_DAYS)


def inferiority() -> float:
    """自卑值 0~1。

    不直接用「累计次数」（那是无限增长的），用**最近 30 天的加权和**再衰减：
        每次被否定 +1.0、被无视 +0.7、搞砸 +0.8 …
        30 天内累计 8 次被否定 → 约 0.8
    """
    d = _load()
    peaks = d.get("peaks") or []
    cut = time.time() - 30 * 86400
    total = 0.0
    for p in peaks:
        at = float(p.get("at") or 0)
        if at < cut:
            continue
        # 越近的越重
        age_days = (time.time() - at) / 86400.0
        total += float(p.get("w") or 0) * (0.5 ** (age_days / DECAY_HALF_DAYS))
    # 2026-11-07：分母从 8 改成 6。
    # 收紧匹配后她只命中 2 条基础迹象（怕被嫌笨 / 被嫌就收敛嘴），
    # 2/8 = 0.25 刚好卡在注入阈值 0.3 下面 —— 那这个模块就永远不说话。
    # 6 才是对的量级：4 条证据就算「明显自卑」，2 条算「有一点」。
    return round(min(1.0, total / 6.0), 3)


def compensation() -> float:
    """补偿值 0~1：她有多使劲。

    从两个地方取：
      ① 本轮的展开度（说得多不多、敢不敢放开）
      ② 行为证据里的「承诺/记账/讨要」密度
    自卑高 + 补偿高 = 在使劲撑；自卑高 + 补偿低 = 缩了
    """
    try:
        from plugins import ego_evidence as ee
        r = ee.report()
        c = r.get("counts") or {}
        commit = int(c.get("commit") or 0)
        account = int(c.get("account") or 0)
        n = max(1, sum(int(v or 0) for v in c.values()))
        # 记账密度高 = 在意别人怎么对她
        dense = (commit + account) / n
        return round(min(1.0, dense * 1.6), 3)
    except Exception:  # noqa: BLE001
        return 0.5


def interest() -> float:
    """社会兴趣 0~1：她的补偿是指向别人的，还是只顾自己。

    阿德勒说这才是健康与否的分水岭 ——
    补偿指向「让别人好过」是健康的，指向「证明自己」是歪的。
    """
    try:
        from plugins import ego_evidence as ee
        c = (ee.report().get("counts") or {})
        hold = int(c.get("hold") or 0)        # 守住 / 守家 → 有牵挂
        expand = int(c.get("expand") or 0)    # 得拿下来 → 只顾自己
        pre = int(c.get("precondition") or 0)
        total = max(1, hold + expand + pre)
        return round(min(1.0, (hold + pre * 0.5) / total), 3)
    except Exception:  # noqa: BLE001
        return 0.5


def style() -> tuple[str, str]:
    """生活风格（阿德勒的 lifestyle）：她习惯用哪种方式应对世界。"""
    inf = inferiority()
    comp = compensation()
    soc = interest()
    if inf < 0.25:
        return "平衡型", "现在没什么可防的，正常过"
    if comp >= 0.55 and soc >= 0.55:
        return "讨好型", STYLES["讨好型"]
    if comp >= 0.55 and soc < 0.45:
        return "对抗型", STYLES["对抗型"]
    if comp < 0.35:
        return "回避型", STYLES["回避型"]
    return "平衡型", STYLES["平衡型"]


def line() -> str:
    """给 prompt 的一句话。**自卑低的时候不说话** ——
    一直让她「意识到自己在补偿」会演成心理剧。"""
    inf = inferiority()
    if inf < 0.3:
        return ""
    st, desc = style()
    # 只描述状态，不下指令（具体的说法交给她自己）
    if inf >= 0.65:
        tone = "有点过了，说话会带刺或者特别软"
    elif inf >= 0.45:
        tone = "心里有点没底，会多问一句「是不是」"
    else:
        tone = "隐隐的，平时不显"
    return f"【心里的底】最近有点怕自己不够好（{tone}）。表面上{desc}。"


def stats() -> dict:
    st, desc = style()
    return {
        "自卑": inferiority(),
        "补偿": compensation(),
        "社会兴趣": interest(),
        "风格": st,
        "说明": desc,
    }


def brief() -> str:
    s = stats()
    return (f"自卑 {s['自卑']:.2f} · 补偿 {s['补偿']:.2f} · "
            f"社会兴趣 {s['社会兴趣']:.2f} → {s['风格']}")


try:
    seed_if_empty()
except Exception:  # noqa: BLE001
    pass
