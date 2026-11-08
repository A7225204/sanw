"""去 AI 味（借鉴 guizang Humanizer-zh 的思路，规则精简版）。

只做减法：
  1. 删句首套话（"值得注意的是""总的来说""在当今…的背景下"）
  2. 去掉模板化连接词（不仅/而且/更重要的是）
  3. 整句丢掉总结式收尾与喊话（"这是一次有意义的尝试""让我们…"）
  4. 保留事实、数字、专名、作者自己的语气词

用法：
    text = await humanize(text, chat)   # 模型润色（失败原样返回）
    hard = strip_cliches(text)          # 纯规则版，零成本
"""
from __future__ import annotations

import re

# 只在句首出现的套话
_LEAD = [
    "值得注意的是", "总的来说", "综上所述", "总而言之", "毋庸置疑", "不难发现",
    "换句话说", "显而易见", "需要指出的是", "值得一提的是", "首先", "其次", "最后",
]
_LEAD_RE = re.compile(
    r"^(?:在当今[^。]{0,14}(?:时代|背景|环境)[^。]{0,4}下|随着[^。]{0,14}(?:发展|进步|普及))[，,]?"
)
_SUB = [
    (r"的的确确", "确实"),
    (r"进行了([\u4e00-\u9fa5]{1,4})", r"\1"),
    (r"起到了([\u4e00-\u9fa5]{1,6})的作用", r"\1"),
    (r"由于", "因为"),
    (r"不仅", ""),
    (r"而且", ""),
    (r"更重要的是", ""),
]
# 整句丢掉（总结式收尾 / 喊话）
_DROP_SENT = [
    re.compile(r"^\s*这(?:无疑)?是一次[^。]{0,16}(?:尝试|探索|实践)[。！？!?]?$"),
    re.compile(r"^\s*让我们[^。]{0,24}[。！？!?]?$"),
    re.compile(r"^\s*(?:希望|愿)[^。]{0,20}(?:越来越好|有所收获)[。！？!?]?$"),
]
_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;\n])")


def strip_cliches(text: str) -> str:
    """逐句清洗：句首套话删掉；纯套话整句丢；剩核心字太少也丢。"""
    t = str(text or "").strip()
    if not t:
        return ""
    keep: list[str] = []
    for seg in _SENT_SPLIT.split(t):
        seg = seg.strip()
        if not seg:
            continue
        _stripped = False
        for w in _LEAD:
            if seg.startswith(w):
                seg = seg[len(w):].lstrip("，, ")
                _stripped = True
                break
        seg = _LEAD_RE.sub("", seg).strip()
        if any(p.match(seg) for p in _DROP_SENT):
            continue
        for a, b in _SUB:
            seg = re.sub(a, b, seg)
        seg = seg.strip()
        core = re.sub(r"[\s\W_]+", "", seg)
        # 只有「删过套话前缀」或「本来就是套话」的短句才丢；
        # 她自己的短句（“哼，算他有点眼光”）必须留着 —— 这是语气
        if not core:
            continue
        if len(core) < 8 and _stripped:
            continue
        if not seg.endswith(("。", "！", "？", "；", "\n")):
            seg += "。"
        keep.append(seg)
    out = "".join(keep).strip()
    return out or _LEAD_RE.sub("", t).strip()


async def humanize(text: str, chat=None) -> str:
    """model 润色版；不传 chat 就只做规则清洗。"""
    base = strip_cliches(text)
    if chat is None or not base:
        return base
    prompt = (
        "把下面这段中文改得更像人随手写的，只做减法不做加法：\n"
        "1) 删掉空话、套话、总结式收尾；2) 拆掉整齐排比和三段式；\n"
        "3) 保留全部事实/数字/专名；4) 保留原作者的称呼和口癖；\n"
        "5) 不要新增观点，不要解释你改了什么。只输出改好的正文。\n\n" + base[:3000]
    )
    try:
        out = await chat([{"role": "user", "content": prompt}])
        out = (out or "").strip()
        return out or base
    except Exception:  # noqa: BLE001
        return base


# ===========================================================================
# 人味加法层（2026-11-05，借 dsh-fish-tone 的方法，数据来自 1190 万字语料）
#
# 我们原来的 humanize 只做减法（删套话）。这一层做加法：
#   · 微标记：也 / 还 / 好像 / 根本不 / 全都 / 特别 —— 不加信息，只加"关系"和"留白"
#   · 结构腔扎堆检测：不看总数，只看 100 字窗口里出现几种不同句式
#   · 语病配额：真人每 606 字一处手滑，干净的稿子读起来像校对过的
#
# 阈值全部按她自己的回复长度校准过（83 条样本，中位数 38 字，68% 在 50 字以内）。
# ===========================================================================

# 结构腔句式（真人/AI 密度实测：先…再… 41×、最…的是 37×、破折号 16×、三段并列 11×）
_STRUCT_PATTERNS = {
    "not_but": re.compile(r"不是[^。！？\n]{1,20}[，,]?\s*而是"),
    "most_is": re.compile(r"最[^。！？\n]{1,12}的是"),
    "first_then": re.compile(r"先[^。！？\n]{1,14}[，,]?\s*再"),
    "triple": re.compile(r"[^。！？\n]{2,10}、[^。！？\n]{2,10}、[^。！？\n]{2,10}"),
    "dash": re.compile(r"——"),
}

# 微标记表（真人/千字 vs AI/千字 的倍数来自语料实测）
_MARK_RELATION = ("也", "还")          # 挂第一二人称后面：「我试过」→「我也试过」
_MARK_HEDGE = ("好像", "感觉", "可能")   # 留白：AI 不猜，真人猜（感觉 3×、可能 3×、好像 2.7×）
_MARK_STRONG = (("很", "特别"), ("非常", "特别"))   # 加强：特别 7×、简直 8.5×

# 明显/不明显的语病模板（配额只在长回复上放）
_TYPO_LIGHT = (
    (re.compile(r"(?<![。！？])。$"), "。"),      # 句号连打（轻微）
)


def structure_flood(text: str, window: int = 100) -> tuple[int, int]:
    """返回 (最多命中几种句式, 那个窗口的位置)。

    真人重复自己的习惯（同种来两次），AI 轮换工具箱（三种各来一次）。
    所以判据不是总数，而是「同一个窗口里出现了几种**不同**的句式」。
    """
    t = text or ""
    if len(t) < 30:
        return 0, 0
    if len(t) <= window:
        # 短于一屏就直接看全文（38 字的一句话里也能塞三种句式）
        return sum(1 for p in _STRUCT_PATTERNS.values() if p.search(t)), 0
    best, best_at = 0, 0
    for i in range(0, max(1, len(t) - window + 1), 20):
        chunk = t[i:i + window]
        kinds = sum(1 for p in _STRUCT_PATTERNS.values() if p.search(chunk))
        if kinds > best:
            best, best_at = kinds, i
    return best, best_at


def add_marks(text: str) -> str:
    """加微标记，不改意思、不加句子、不碰代码和路径。"""
    t = text or ""
    if not t or not re.search(r"[\u4e00-\u9fa5]", t):
        return t
    # 代码块 / 路径 / 命令行整行跳过
    if "```" in t or re.search(r"[A-Za-z]:[\\/]|https?://|--[a-z]", t):
        return t
    out = t
    # ① 关系类（「我也试过」）**默认不做**：没有上下文没法判断对方到底试没试过，
    #   插错一个「也」比不加更像机器。留白和加强这两类不依赖语境，安全。
    # ② 留白类：评价副词前面补「好像」（AI 不猜，这是最大的差距之一）
    if not any(k in out for k in _MARK_HEDGE):
        out = re.sub(r"(挺|有点|蛮|太)([\u4e00-\u9fa5]{1,4})", r"好像\1\2", out, count=1)
    # ③ 加强类：程度往上抬一格（人一激动就往下加词）
    for src, dst in _MARK_STRONG:
        if src in out and dst not in out:
            out = out.replace(src, dst, 1)
            break
    return out


def typo_quota(chars_so_far: int, this_len: int) -> bool:
    """这一条回复该不该留一处「不明显的手滑」。

    真人评论/弹幕都是每 606 字左右一处；但她的回复中位数只有 38 字，
    短句里留错字会非常刺眼 —— 所以 <200 字的回复一律不留，跟 fish-tone 的
    300 字阈值同向但更保守。
    """
    if this_len < 200:
        return False
    # 每攒满 800 字放一处：跨到下一个 800 的整数倍就算
    next_at = (chars_so_far // 800 + 1) * 800
    return chars_so_far + this_len >= next_at


def apply_typo(text: str) -> str:
    """加一处最不刺眼的手滑：句尾句号连打。只在长回复上用。"""
    t = (text or "").rstrip()
    if not t or t.endswith("。。"):
        return t
    if t.endswith("。"):
        return t[:-1] + "。。"
    return t


def humanize_add(text: str, *, chars_so_far: int = 0, allow_typo: bool = True) -> str:
    """对外总入口：加微标记 + （长回复时）一处手滑。不动事实、不动数字。"""
    t = text or ""
    if not t:
        return t
    if len(t) > 800:                       # 太长的不动，避免改出病句
        return t
    out = add_marks(t)
    if allow_typo and typo_quota(chars_so_far, len(out)):
        out = apply_typo(out)
    return out


# ===========================================================================
# 自我重复检测（2026-11-05，第五次遇到"抓着一件事不放"）
#
# 现象：她自己在回复里起一个话头（「不过话说回来，那个欢迎还是自动发的吧…」），
# 这句进历史，下一轮读到自己说过，当成"还没聊完的茬"接着展开，几乎照抄。
# 实测：19:58 两轮里「欢迎还是自动发」说了两遍，中间主人只说了「？」和「保存了没」。
#
# 治法：只删她**自起话头**且**和最近说过的高度重复**的那一句，主体回复不动。
# ===========================================================================

_SELF_TOPIC_MARK = ("不过话说回来", "话说回来", "对了，", "对了,", "说起来",
                    "说到这个", "顺带一提", "顺便说", "另外，")

# 中文没有分词，用 2 字滑窗当"词"，过滤高频虚词组合
_SHINGLE_STOP = {
    "本鱼", "主人", "那个", "这个", "就是", "好像", "有点", "还是", "不过",
    "话说", "回来", "一下", "什么", "怎么", "因为", "所以", "但是", "而且",
    "自己", "现在", "时候", "这样", "那样", "真的", "已经", "可以", "没有",
}


def _shingles(text: str) -> set[str]:
    t = re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9]", "", text or "")
    return {t[i:i + 2] for i in range(max(0, len(t) - 1))} - _SHINGLE_STOP


def _split_sents(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[。！？!?])", text or "") if s.strip()]


def strip_self_repeat(text: str, recent_sents: list[str],
                      min_shared: int = 5) -> tuple[str, str]:
    """删掉「自起话头 + 和最近说过的高度重复」的句子。返回 (新文本, 被删的句子)。

    recent_sents 是最近几条回复切出来的句子（调用方维护），不需要是完整原文。
    """
    t = text or ""
    if not t or not recent_sents:
        return t, ""
    sents = _split_sents(t)
    if len(sents) <= 1:
        return t, ""            # 只有一句就别删，删完什么都没了
    kept, dropped = [], []
    _last_dropped_seg = ""
    for seg in sents:
        is_self_topic = any(m in seg for m in _SELF_TOPIC_MARK)
        if not is_self_topic:
            # 2026-11-05：上一句刚因为话题闭合/重复被删，这一句如果还接着说同一件事，一起删。
            # 实测「……还是自动发的吧？」被删了，紧接着「新人一句话没说它就招呼上了」会漏下来。
            # 词面重合判断在这里不管用（「欢迎自动发」和「新人一句话没说就招呼」
            # 语义相同但 2 字词交集是 0）。改成：自起话头被删后，紧跟的一句直接跟着删。
            # 她的句式就是「话说回来，X 吧？—— [解释]」，解释句单独留着会变成断头话。
            if _last_dropped_seg and any(m in _last_dropped_seg for m in _SELF_TOPIC_MARK):
                dropped.append(seg.strip())
                _last_dropped_seg = ""
                continue
            _last_dropped_seg = ""
            kept.append(seg)
            continue
        # 2026-11-05：这条话题在执念池里已经闭合了 → 直接删，不用等它重复。
        # 「欢迎还是自动发的吧」实测能跨过 closed 状态再说一遍，就是缺这一道。
        try:
            from plugins import obsessions as _obs_ck
            if _obs_ck.is_closed(seg):
                dropped.append(seg.strip())
                _last_dropped_seg = seg
                continue
        except Exception:  # noqa: BLE001
            pass
        mine = _shingles(seg)
        if not mine:
            kept.append(seg)
            continue
        shared_max = 0
        for old in recent_sents:
            shared = len(mine & _shingles(old))
            if shared > shared_max:
                shared_max = shared
        if shared_max >= min_shared:
            dropped.append(seg.strip())
            _last_dropped_seg = seg
            continue
        kept.append(seg)
    out = "".join(kept).strip()
    # 2026-11-05：原来要求保留 35% 长度，导致「话说回来…」一删就触底、整条回退原样，
    # 等于白删。改成只保底「别删空」——剩一句也认，总比让那茬一直挂着强。
    if not out or len(out.strip()) < 4:
        return t, ""
    return out, " ".join(dropped)[:60]
