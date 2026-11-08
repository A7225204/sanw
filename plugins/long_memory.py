"""长期记忆插件：把群里值得记的事沉淀成「档案」，每次对话带上。

设计要点
--------
1. **档案式**（不是全量历史）：档案 = 若干条结构化事实，体积恒定。
2. **可修正可补充**：合并时允许 ADD / UPDATE / DELETE —— 出现矛盾以新信息为准，
   过时条目直接删掉，不会出现"两条并存互相打架"。
3. **恒定成本**：档案字数有硬上限（ARCHIVE_MAX_CHARS），所以每次对话多带的
   token 数是个常数，不会随使用时间线性膨胀。
4. **省钱的摆法**：档案作为**独立消息插在 system prompt 之后**（位置固定），
   便于命中服务端的「上下文缓存」（缓存命中输入价便宜很多）。

触发时机
--------
- 攒够 CONSOLIDATE_EVERY 条新消息 → 自动合并一次（后台任务，不阻塞聊天）
- 另外由 `plugins/schedule.py` 每天凌晨强制合并一次（兜底）

存储
----
`qq-ai-bot/memory/长期记忆.json`，同时存档案和"待合并"消息（重启不丢）。
"""

import asyncio
import hashlib
import json
import os
import math
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
import nonebot  # noqa: E402

from config import get_config  # noqa: E402
import token_meter  # noqa: E402  令牌用量累计（静默容错）

cfg = get_config()

DATA_DIR = ROOT / "memory"
ARCHIVE_FILE = DATA_DIR / "长期记忆.json"

# ==================== 可调参数（都搬到 config.py / .env 了） ====================
#   MEM_CONSOLIDATE_EVERY   攒够多少条新消息触发一次合并
#   MEM_ARCHIVE_MAX_CHARS   档案总字数上限（逼模型压缩，控住 token）
#   MEM_MAX_USE_CHARS       注入 prompt 时的硬上限
#   MEM_PENDING_KEEP        待合并消息最多留多少条
# 单价也在 .env：PRICE_IN / PRICE_OUT / PRICE_CACHE
CONSOLIDATE_EVERY = cfg.memory.consolidate_every
ARCHIVE_MAX_CHARS = cfg.memory.archive_max_chars
MAX_USE_CHARS = cfg.memory.max_use_chars
PENDING_KEEP = cfg.memory.pending_keep
# 每个 scope 档案条目上限：超出淘汰最旧（按 id 递增，保留最新），防止长期膨胀
ARCHIVE_MAX_ENTRIES = 30
# 私聊专属档案：和主人的约定 / 偏好 / 承诺需要留得更久，单独放宽条目与字数上限；
# 群聊档案、自学档案仍沿用上面的默认值。
# 2026-11-05：60 → 80。实测 11-03 之后的新记忆一条都进不来——
# 档案满 60 条时，新条目 w=1.0 rank≈1.0，老条目 w=3.0 rank≈2.8，
# 一加进去就被淘汰，日志却照样报「合并完成 +2」，看起来像没同步。
PRIVATE_MAX_ENTRIES = 80
PRIVATE_MAX_CHARS = 1500

# ---- 记忆生命周期（2026-10-25）----
# 每条事实带 weight / hits / last_used：
#   命中一次（召回到且当轮对话里确实提到相关词）→ weight += BOOST，最多 MAX
#   长期不用的按半衰期线性衰减，参与排序与淘汰
# 淘汰规则从"按 id 留最新"改成"按生效权重留最有用"，否则老而重要的约定会被挤掉。
MEM_WEIGHT_START = 1.0
MEM_WEIGHT_BOOST = 0.3
MEM_WEIGHT_MAX = 3.0
MEM_HALF_LIFE_DAYS = 60.0          # 默认半衰期（没命中过的普通事实）

# ---- 艾宾浩斯层（2026-10-28 追加）----
# 1) 分类半衰期：约定/偏好/人名这类"长期有效"的忘得慢，吃喝天气这类忘得快
# 2) 复习增益：被用得越多半衰期越长（hits 越多越难忘）
# 3) 遗忘阈值：生效权重低于门槛就不再进 prompt（档案里还留着，只是"想不起来"）
MEM_HALF_LIFE_CLASSES = (
    ("long", 180.0, r"约定|承诺|答应|说好|偏好|喜欢|讨厌|不爱|生日|习惯|怕|过敏|名字|外号|叫|住|上班|工作|职业|忌口|素食"),
    ("short", 35.0, r"今天|昨天|刚才|这周|上周|明天|吃了|喝了|天气|心情|困|饿|累|下雨|热|冷"),
)
MEM_HALF_LIFE_HITS_GAIN = 0.55     # 每命中一次，半衰期 ×(1+0.55)，封顶
MEM_HALF_LIFE_MAX = 500.0          # 半衰期上限（天）
MEM_DECAY_FLOOR = 0.12             # 生效权重低于这个 → 不进 prompt

# ---- 退役而不删除（2026-11-05，借 dsh-soul 的三条方法论）----
# 1) 错误的删除会藏住一条有效信息，比显示一条过时的更糟 → 作废只改可见性
# 2) 退役条目仍可在档案里查到，可恢复 → 只标记 retired，不删行
# 3) 退役条目单独封顶，不占正式名额 → 否则"不删除"会变成"无限堆积"
RETIRED_MAX = 200                  # 每个 scope 最多保留多少条退役条目


def _is_private_scope(scope: str) -> bool:
    return str(scope).startswith("private_")


def _limits(scope: str) -> tuple[int, int]:
    """返回 (条目上限, 字数上限)；私聊单独放宽，群聊 / 自学档案沿用默认。"""
    if _is_private_scope(scope):
        return PRIVATE_MAX_ENTRIES, PRIVATE_MAX_CHARS
    return ARCHIVE_MAX_ENTRIES, ARCHIVE_MAX_CHARS
# ==========================================================================

# 内存态：{群号: [ {id, subject, fact, time}, ... ]}
_archive: dict[str, list[dict]] = {}
# 待合并的新消息：{群号: [ [昵称, 内容, 时间], ... ]}
_pending: dict[str, list[list[str]]] = {}
# 正在合并的群，防止并发重复合并
_busy: set[str] = set()

# 上一次实际调用的 token 用量（供排查/统计用）
last_usage: dict = {}

# 主事件循环引用：nonebot 启动钩子绑定。后台合并任务统一从这里 create_task，
# 避免在 asyncio.to_thread 线程池里 get_running_loop() 抛 RuntimeError 被静默吞掉。
_MAIN_LOOP: "asyncio.AbstractEventLoop | None" = None


def bind_main_loop() -> None:
    """nonebot 启动钩子调用：记录当前主事件循环，供后台合并任务调度。"""
    global _MAIN_LOOP
    try:
        _MAIN_LOOP = asyncio.get_running_loop()
    except RuntimeError:
        _MAIN_LOOP = None


# ---------------------------------------------------------------- 存储

# 空洞 fact：只剩省略号、标点、空白（如 "..."、"。。。"、"主人："），不入档
_EMPTY_FACT_RE = re.compile(r"[\s，。！？、：；,.!?;:\-—…·\"'“”‘’（）()【】\[\]~～*#]+")


# 2026-11-06：自学入库开始挡"搜索引擎页面文案"。
# 之前 30 条里二十来条是这种 —— 天眼查的广告、网信算备号、头条的聚合说明，
# 全当知识存着，既没用又会污染以后接进人格提炼的输入。
_JUNK_FACT_RE = re.compile(
    # 企业信息 / 备案号 / 导航文案
    r"为您提供|查询服务|企业信息查询|工商注册|公司电话|公司地址"
    r"|网信算备|内容安全算法|个性化推送算法|ICP备|公网安备"
    r"|提供详尽的搜索结果|每天实时更新|连接人与信息|优质丰富的信息"
    r"|相关视频：|点击查看|查看更多|登录后|下载客户端"
    r"|^您在查找|^搜索无结果"
    # 素材站 / 标题党图站（"发发色图"曾被当成"头发颜色"搜出一堆素材）
    r"|我图网|摄图网|千图网|堆糖|花瓣网|站酷|视觉中国"
    r"|素材专题|图片大全|素材在线下载|免抠元素|摄影图|商业授权"
    r"|眼界大开|越看越懵|见所未见|多看不�"
    # B站 / 微博 / 知乎 的页面头部文案
    r"|名玩家向您推荐|点击前往哔哩哔哩|视频播放量|投币|弹幕"
    r"|个回答|人关注|次浏览|发布于\s|来自\s?iPhone|正在加载|请稍候"
    # 盗版影视站
    r"|在线看完整版|高清完整片源|多端同步观看|随时随地追剧",
    re.I,
)


def _valid_fact(fact: str) -> bool:
    """fact 是否含实质内容：剥掉标点 / 省略号 / 空白后至少 2 个字符。"""
    body = _EMPTY_FACT_RE.sub("", str(fact or ""))
    if len(body) < 2:
        return False
    # 广告 / 备案号 / 导航文案不算知识
    if _JUNK_FACT_RE.search(str(fact or "")):
        return False
    return True


def _clean_archive_items(items: Any) -> list[dict]:
    """档案条目结构清洗：只保留 dict、fact 非空的条目；id 缺失时补号。

    防止手工改 JSON / 旧版本残留的脏结构在合并迭代时抛 TypeError / KeyError。
    """
    if not isinstance(items, list):
        return []
    cleaned: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        fact = str(it.get("fact") or "").strip()
        if not _valid_fact(fact):
            continue
        entry = {
            "id": int(it["id"]) if isinstance(it.get("id"), int) else len(cleaned) + 1,
            "subject": str(it.get("subject") or "群友"),
            "fact": fact,
            "time": str(it.get("time") or datetime.now().strftime("%m-%d")),
            # 生命周期字段：老数据没有这几个键，这里补默认值完成迁移
            "w": float(it.get("w") or MEM_WEIGHT_START),
            "h": int(it.get("h") or 0),
            "lu": float(it.get("lu") or 0.0),
        }
        # 扩展字段：**不再用白名单**。
        # 白名单已经坑过三次（revlog / cls / sens 各丢过一次），现在改成
        # 「基础字段重算，其余原样带走」——以后给记忆加字段不用再改这里。
        # 只保留可 JSON 序列化的值，避免脏数据把 _save 搞崩。
        for _k, _v in it.items():
            if _k in entry:
                continue
            if isinstance(_v, (str, int, float, bool, list, dict)) or _v is None:
                entry[_k] = _v
        cleaned.append(entry)
    return cleaned


# 敏感度：0 公开 / 1 私域（关系、情绪、矛盾）/ 2 私密（18+、隐私信息）
# 群聊 scope 只放 sens=0；私聊 scope 全放。群里的记忆本来就关于群友，不该带私密话题。
def _sens_ok(it: dict, scope: str) -> bool:
    try:
        sens = int(it.get("sens") or 0)
    except (TypeError, ValueError):
        sens = 0
    if sens <= 0:
        return True
    return not str(scope or "").startswith("group_")


def _now_ts() -> float:
    return time.time()


def _half_life_days(it: dict) -> float:
    """这条记忆的半衰期：分类基准 × 复习增益，封顶 MEM_HALF_LIFE_MAX。

    · 约定/偏好/人名 → 180 天起（忘得慢）
    · 今天吃了啥/天气 → 35 天起（忘得快）
    · 其余 → 默认 60 天
    · 每被真正用到一次（hits+1），半衰期 ×(1+GAIN)
    """
    # 语气/类型优先（memory_maintain 打的 cls）：人格类最耐久、场景类最快淡
    _cls_days = {"persona": 365.0, "fact": 180.0, "impression": 90.0, "scene": 35.0}
    if it.get("cls") in _cls_days:
        base = _cls_days[str(it["cls"])]
        try:
            hits0 = int(it.get("h") or 0)
        except (TypeError, ValueError):
            hits0 = 0
        return min(MEM_HALF_LIFE_MAX, base * (1.0 + MEM_HALF_LIFE_HITS_GAIN * hits0))
    text = f"{it.get('subject') or ''}{it.get('fact') or ''}"
    base = MEM_HALF_LIFE_DAYS
    for _name, days, pattern in MEM_HALF_LIFE_CLASSES:
        try:
            if re.search(pattern, text):
                base = days
                break
        except re.error:
            continue
    try:
        hits = int(it.get("h") or 0)
    except (TypeError, ValueError):
        hits = 0
    hl = base * (1.0 + MEM_HALF_LIFE_HITS_GAIN * hits)
    return min(MEM_HALF_LIFE_MAX, hl)


def _hormone_modifier(it: dict) -> tuple[float, float]:
    """内分泌对这条记忆的影响：返回 (半衰期倍数, 权重倍数)。

    · CORT 爆表（≥0.7）或带创伤标记 → 半衰期 ×8，几乎忘不掉
    · OXY 极高（≥0.7，刻骨铭心）   → 半衰期 ×3
    · DA 高（≥0.7，奖励记忆）      → 权重 ×1.15

    只认「这条记忆固化时的内分泌快照」。老数据没有快照就返回 1.0/1.0，
    不拿当前激素去凭空放大历史记忆（否则 CORT 一飙，全部旧记忆一起变不朽）。
    """
    h = it.get("hormones") or {}
    if not h:
        return 1.0, 1.0

    def g(k: str) -> float:
        try:
            return float(h.get(k) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    cort, oxy, da = g("CORT"), g("OXY"), g("DA")
    half_mul, w_mul = 1.0, 1.0
    if it.get("is_traumatic") or cort >= 0.7:
        half_mul *= 8.0
    if oxy >= 0.7:
        half_mul *= 3.0
    if da >= 0.7:
        w_mul *= 1.15
    return half_mul, w_mul


def _infer_expiry(fact: str, kind: str = "long") -> float:
    """从事实里推断过期时间戳；0 = 永不过期。

    2026-11-05（借鱼神双子星的 validUntil）：档案里躺着「主人身上仅剩200元撑到15号」
    这种有明确期限的事，一周后还在当现状用。这里只对**有时间边界**的内容设值，
    「主人是男的」这种不带时间词的永久事实不碰。

    过期后 archive_text 不再注入（见 602 行过滤），但条目还在文件里，search 能查到。
    """
    now = _now_ts()
    t = str(fact or "")
    if not t:
        return 0.0
    # 事件类（kind="exp"）默认一周
    if kind == "exp":
        return now + 7 * 86400
    if any(k in t for k in ("明天", "后天")):
        return now + 3 * 86400
    if any(k in t for k in ("这周", "本周", "周末", "这周末")):
        import datetime as _d
        d = _d.datetime.fromtimestamp(now)
        return now + max(1, 6 - d.weekday()) * 86400
    if any(k in t for k in ("这个月", "本月", "月底", "下个月")):
        import calendar as _c, datetime as _d
        d = _d.datetime.fromtimestamp(now)
        last = _c.monthrange(d.year, d.month)[1]
        return now + max(1, last - d.day) * 86400
    # 「撑到15号」「15号之前」都要认——原来只匹配"之前/为止/前"，
    # 实测漏了「主人身上仅剩200元撑到15号」这种（它是最该过期的）。
    if re.search(r"\d+\s*[号日]", t) and (
            any(k in t for k in ("之前", "为止", "前")) or
            re.search(r"(撑到|用到|花到|到)\s*\d+\s*[号日]", t)):
        return now + 30 * 86400
    if any(k in t for k in ("最近", "暂时", "目前", "眼下")):
        return now + 14 * 86400
    return 0.0


def _item_rank(it: dict, now: float | None = None) -> float:
    """生效权重：自身权重 × 时间衰减 × 内分泌调节。"""
    now = now or _now_ts()
    try:
        w = float(it.get("w") or MEM_WEIGHT_START)
    except (TypeError, ValueError):
        w = MEM_WEIGHT_START
    try:
        lu = float(it.get("lu") or 0.0)
    except (TypeError, ValueError):
        lu = 0.0
    if lu <= 0:
        lu = now - 86400.0 * 7  # 老数据没有 last_used：当成一周前用过，给个中性起点
    age_days = max(0.0, (now - lu) / 86400.0)
    hl_mul, w_mul = _hormone_modifier(it)
    half = max(1e-6, _half_life_days(it) * hl_mul)
    return w * w_mul * (0.5 ** (age_days / half))


def _stable_rank(it: dict, day: float) -> float:
    """缓存友好的"冻结排名"：把 lu 量化到整天、w 量化到 0.5，日内在同一档就不会变。

    原来的 _item_rank 用精确 lu（每轮 _reinforce 都会刷新）→ 排序每轮抖动 →
    archive_text 每轮不同 → stable 前缀每轮失效（2026-11-04 实测 57 轮 57 种指纹）。
    这里只用于"拼进前缀的档案文本"；淘汰与【相关记忆】仍用精确 _item_rank。
    """
    try:
        w = float(it.get("w") or MEM_WEIGHT_START)
    except (TypeError, ValueError):
        w = MEM_WEIGHT_START
    w = round(w * 2.0) / 2.0                      # 0.5 档，强化 +0.15 通常不跨档
    try:
        lu = float(it.get("lu") or 0.0)
    except (TypeError, ValueError):
        lu = 0.0
    if lu <= 0:
        lu = day - 86400.0 * 7
    lu_day = math.floor(lu / 86400.0) * 86400.0   # 量化到天
    age_days = max(0.0, (day - lu_day) / 86400.0)
    hl_mul, w_mul = _hormone_modifier(it)
    half = max(1e-6, _half_life_days(it) * hl_mul)
    return w * w_mul * (0.5 ** (age_days / half))


def _bigrams(text: str) -> set[str]:
    """中文按 2-gram 切，用于廉价的"有没有提到"判断，不调模型。"""
    s = re.sub(r"[\s，。！？、：；,.!?;:\-—…·\"'“”‘’（）()【】\[\]~～*#@]+", "", str(text or ""))
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _clean_for_match(text: str) -> str:
    return re.sub(r"[\s，。！？、：；,.!?;:\-—…·\"'“”‘’（）()【】\[\]~～*#@]+", "", str(text or ""))


def _lcs_len(a: str, b: str) -> int:
    """最长公共子串长度（不要求连续在两边同位置）。短串 DP，成本可忽略。

    用途：判断「白饭大王」这种短名字有没有真的出现在某条事实里——
    按 2-gram 占比会被长事实稀释，按连续片段才不会漏。
    """
    if not a or not b:
        return 0
    if len(a) > len(b):
        a, b = b, a
    best = 0
    prev = [0] * (len(b) + 1)
    for ca in a:
        cur = [0] * (len(b) + 1)
        for j, cb in enumerate(b, 1):
            if ca == cb:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def _reinforce(scope: str, items: list[dict], query: str) -> int:
    """当轮对话里提到某条事实的关键词 → 该条权重 +BOOST、hits+1、记 last_used。

    廉价字符串比对，不调模型、不花 token。命中判据（满足任一）：
      - subject 被直接点名；
      - **最长公共片段 ≥4 字**（「白饭大王」「3DMAX」这种专名靠这条，不受长事实稀释）；
      - 2-gram 交集 ≥3 且占该条事实 ≥30%（长句改写时靠这条）。
    一轮最多强化 3 条（按 点名 > 公共片段长度 排序），防止权重集体饱和。
    返回本次被强化的条数。
    """
    q = _clean_for_match(query)
    if len(q) < 2:
        return 0
    qb = {q[i:i + 2] for i in range(len(q) - 1)}
    cands: list[tuple[int, int, dict]] = []
    # 2026-11-07：这一轮被强化的条目，一会儿让它们"再巩固"一次
    _to_recon: list[dict] = []
    for it in items:
        fact = str(it.get("fact") or "")
        if not fact:
            continue
        f = _clean_for_match(fact)
        if len(f) < 2:
            continue
        subject = str(it.get("subject") or "").strip()
        named = 1 if (subject and subject in q) else 0
        lcs = _lcs_len(q, f)
        fb = {f[i:i + 2] for i in range(len(f) - 1)}
        overlap = len(qb & fb)
        ratio = overlap / len(fb) if fb else 0.0
        # 同义词命中：「打拳」也能点亮写着「功夫」的那条（本地别名表，不花 token）
        alias_hit = 0
        try:
            import alias_map
            alias_hit = alias_map.match_score(q, f, min_len=2)
        except Exception:  # noqa: BLE001
            alias_hit = 0
        if not (named or lcs >= 4 or alias_hit >= 2 or (overlap >= 3 and ratio >= 0.3)):
            continue
        cands.append((named, max(lcs, overlap, alias_hit), it))
    if not cands:
        return 0
    cands.sort(key=lambda x: (x[0], x[1]), reverse=True)
    now = _now_ts()
    hit = 0
    for _named, _s, it in cands[:3]:
        try:
            w = float(it.get("w") or MEM_WEIGHT_START)
        except (TypeError, ValueError):
            w = MEM_WEIGHT_START
        it["w"] = round(min(MEM_WEIGHT_MAX, w + MEM_WEIGHT_BOOST), 3)
        it["h"] = int(it.get("h") or 0) + 1
        it["lu"] = now
        # 2026-11-07：被想起 → 再巩固（用当前情绪微调指纹，系数 0.15）
        try:
            from plugins import memory_mood as _mm
            _mm.reconsolidate(it)
        except Exception:  # noqa: BLE001
            pass
        hit += 1
    if hit:
        _save()
    return hit


def _clean_pending(buf: Any) -> list[list[str]]:
    """待合并消息清洗：只保留 [昵称, 内容, 时间] 三元组，内容非空。"""
    if not isinstance(buf, list):
        return []
    cleaned: list[list[str]] = []
    for row in buf:
        if not isinstance(row, list) or len(row) < 2:
            continue
        name = str(row[0] or "群友")
        text = str(row[1] or "").strip()
        if not text:
            continue
        ts = str(row[2]) if len(row) > 2 else datetime.now().strftime("%m-%d %H:%M")
        cleaned.append([name, text[:160], ts])
    return cleaned


def _load() -> None:
    global _archive, _pending
    if not ARCHIVE_FILE.exists():
        return
    try:
        data = json.loads(ARCHIVE_FILE.read_text(encoding="utf-8"))
        raw_archive = data.get("archive") or {}
        raw_pending = data.get("pending") or {}
        _archive = {
            str(k): _clean_archive_items(v)
            for k, v in raw_archive.items()
        }
        _pending = {str(k): _clean_pending(v) for k, v in raw_pending.items()}
        nonebot.logger.info(
            f"[long_memory] 已加载长期记忆：{len(_archive)} 个群，"
            f"{sum(len(v) for v in _archive.values())} 条档案"
        )
    except Exception as e:  # noqa: BLE001
        nonebot.logger.warning(f"[long_memory] 读取失败：{e}")


def _save() -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        # 档案条目封顶：按「权重 × 时间衰减」留最有用，同分再按 id 留新。
        # 旧实现是纯按 id 留最新，会把"老但重要"的约定挤掉。
        # 核心记忆保护（借鉴 AstrBot 记忆插件的 important 豁免）：pin 标记或权重接近上限的永不淘汰。
        for key, items in _archive.items():
            max_entries, _ = _limits(key)
            # 2026-11-05：退役条目不占正式名额，单独按 RETIRED_MAX 封顶。
            # 否则「退役而不删除」会让档案无限增长，最后拖垮每次存档。
            live = [it for it in items if not it.get("retired")]
            dead = [it for it in items if it.get("retired")]
            if len(dead) > RETIRED_MAX:
                dead.sort(key=lambda x: (str(x.get("retired_at") or ""), x.get("id", 0)))
                dead = dead[-RETIRED_MAX:]
            if len(live) > max_entries:
                now = _now_ts()

                def _core(it: dict) -> bool:
                    try:
                        w = float(it.get("w") or 0)
                    except (TypeError, ValueError):
                        w = 0.0
                    return bool(it.get("pin")) or w >= MEM_WEIGHT_MAX * 0.9

                core = [it for it in live if _core(it)]
                # 2026-11-05：最近新增的 15 条（按 id）不参与淘汰。
                # 它们的 w 只有 1.0，按 rank 拼不过被反复强化到 w=3 的老条目，
                # 不保护的话「刚说的」每次都第一个被挤掉。
                _max_id = max((int(x.get("id") or 0) for x in live), default=0)
                frozen = [it for it in live
                          if not _core(it) and int(it.get("id") or 0) > _max_id - 15]
                _frozen_py = {id(x) for x in frozen}
                rest = [it for it in live
                        if not _core(it) and id(it) not in _frozen_py]
                keep_n = max(0, max_entries - len(core) - len(frozen))
                rest.sort(key=lambda x: (_item_rank(x, now), x.get("id", 0)))
                live = core + frozen + (rest[-keep_n:] if keep_n else [])
            _archive[key] = live + dead
        payload = {"archive": _archive, "pending": _pending}
        ARCHIVE_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:  # noqa: BLE001
        nonebot.logger.warning(f"[long_memory] 保存失败：{e}")


_load()


# ---------------------------------------------------------------- 读取

_LAST_VEC_LOG = 0.0


def _log_vec(scope: str, top: dict) -> None:
    """语义召回把首条换掉了就记一行（60 秒最多一条，免得刷屏）。"""
    global _LAST_VEC_LOG
    if time.time() - _LAST_VEC_LOG < 60:
        return
    _LAST_VEC_LOG = time.time()
    try:
        logger.info(f"[记忆向量] {scope} 语义召回生效 → {str(top.get('subject'))[:8]}：{str(top.get('fact'))[:40]}")
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- 向量排序
_VEC_FILE = ROOT / "memory" / "mem_vectors.json"
_vec_cache: dict | None = None


def _local_embed(texts: list[str]) -> list[list[float]]:
    """调本地 bge-m3（tools/embed_server.py，:7801/embed）。失败返回空。"""
    import urllib.request
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:7801/embed",
            data=json.dumps({"texts": [str(t)[:400] for t in texts]}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode("utf-8"))
        v = d.get("vectors") or []
        return [list(map(float, x)) for x in v]
    except Exception:  # noqa: BLE001
        return []


def _vec_key(fact: str) -> str:
    return hashlib.sha1(str(fact).encode("utf-8")).hexdigest()[:16]


def _load_vectors() -> dict:
    global _vec_cache
    if _vec_cache is None:
        try:
            _vec_cache = json.loads(_VEC_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            _vec_cache = {}
    return _vec_cache


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1e-9
    nb = math.sqrt(sum(y * y for y in b)) or 1e-9
    return dot / (na * nb)


def build_vectors(force: bool = False) -> int:
    """离线补齐所有 fact 的向量缓存（由 tools/build_mem_vectors.py 调用）。"""
    data = _load()
    cache = _load_vectors()
    todo = []
    for items in _archive.values():
        for it in items or []:
            fact = str(it.get("fact") or "")
            if not fact:
                continue
            k = _vec_key(fact)
            if force or k not in cache:
                todo.append((k, fact))
    made = 0
    for i in range(0, len(todo), 16):
        chunk = todo[i:i + 16]
        vs = _local_embed([f for _, f in chunk])
        if not vs or len(vs) != len(chunk):
            break
        for (k, _f), v in zip(chunk, vs):
            cache[k] = v
            made += 1
    if made:
        _VEC_FILE.parent.mkdir(parents=True, exist_ok=True)
        _VEC_FILE.write_text(json.dumps(cache), encoding="utf-8")
        _vec_cache = cache
    return made


# ---------------- 自学知识按主题分组（2026-11-05）----------------
# 以前 global_learn 是一条条平铺：「《资本论》…」「庞氏骗局…」「性爱技巧…」混在一起，
# 她读到的是一堆不相关的话。这里按主题归类，同一类挨着放，说话时更容易成套取用。
_TOPIC_RULES = (
    ("经济·骗局", r"(庞氏|骗局|投资|资金|泡沫|传销|金字塔|返利|割韭菜)"),
    ("理论·社科", r"(资本论|马克思|恩格斯|阶级|共产主义|政治经济学|毛选|辩证|唯物)"),
    ("亲密·身体", r"(性爱|情欲|亲密|前戏|润滑|边界|同意|安全词|身体)"),
    ("AI·技术", r"(AI|模型|大模型|Prompt|提示词|DeepSeek|GPT|拟人|agent|算法)"),
    ("娱乐·追番", r"(B站|视频|合集|UP主|第.{1,3}/.{1,3}集|番剧|动画|直播)"),
    ("饮食", r"(吃|喝|饭|粥|面|菜|零食|甜点|奶茶)"),
    ("健康", r"(体检|医院|疾病|住院|症状|药|失眠)"),
    ("时事", r"(会议|公报|政策|哀悼|热搜|新闻|发布会|赛程|比赛)"),
)
_TOPIC_DEFAULT = "杂学"


def _topic_of(fact: str) -> str:
    """按关键词把一条知识归到主题；归不进去算「杂学」。"""
    t = str(fact or "")
    for name, pat in _TOPIC_RULES:
        try:
            if re.search(pat, t):
                return name
        except Exception:  # noqa: BLE001
            continue
    return _TOPIC_DEFAULT


def archive_text(scope: str, intro: str | None = None, query: str = "") -> str:
    """把该 scope 的长期记忆档案格式化成一段文字，供拼进 prompt。没有就返回空串。

    scope 形如 group_{gid} / private_{uid} / global_learn，与 memory_manager、
    根目录 long_memory 命名一致。`intro` 可覆盖默认前缀（自学档案用学习语境文案）。

    `query` 传当前这句用户消息时：先按"权重 × 时间衰减"排序（最有用/最近被强化的靠前），
    并对本轮确实提到的事实做访问强化（weight+/hits+/last_used）。排序靠前 + 截断在后，
    等于把有限的 MAX_USE_CHARS 预算优先给有用的记忆。
    """
    key = str(scope)
    items = _archive.get(key) or []
    if not items:
        return ""
    if query:
        _reinforce(key, items, query)
    now = _now_ts()
    # ⚠️ 排序必须"冻结"：按 id 稳定输出。
    # 之前按 _item_rank 排序 + 语义重排，每轮 archive 文本都会变，导致
    # stabled 前缀每轮断一次（审计 B 实测：uncached 6.7-8.2K，绕开 52-64 块）。
    # 权重(w)/最近使用(lu) 仍会被 _reinforce 更新，只影响淘汰与【相关记忆】，不影响这段文本。
    _now2 = _now_ts()
    items = [i for i in items
             if (not float(i.get("exp") or 0) or float(i.get("exp")) > _now2)
             and _sens_ok(i, key)]
    # 艾宾浩斯遗忘：生效权重低于门槛的不再进 prompt（档案里还留着）。
    # 按"天"量化 now，保证同一天内这段文本不变（否则稳定前缀每轮都断）。
    _day = math.floor(_now2 / 86400.0) * 86400.0
    _kept, _forgotten = [], 0
    for _it in items:
        # 2026-11-05：已退役的条目不进 prompt（但还在档案里，可 restore）
        if _it.get("retired"):
            continue
        if _stable_rank(_it, _day) >= MEM_DECAY_FLOOR:
            _kept.append(_it)
        else:
            _forgotten += 1
    if _forgotten:
        logger.debug(f"[记忆] {key} 有 {_forgotten} 条低于遗忘阈值，本轮不注入")
    items = _kept
    # 排序 = 生效权重 + 近因加成。
    # 只按权重排会让「被反复强化的老事实」（w=3）永远压住「刚说的新状态」（w=1），
    # 于是她一直按旧状态说话（她都离职了还说"两点上班"）。
    # 近因项 exp(-age/2) 让 3 天内的事实有 +1.1~+2.5 的加成，新状态能立刻顶上来。
    # 最近新增的一批（id 最大的 12 条）给额外加成：它们代表"最新说的状态"，
    # 必须能顶过那些被反复强化到 w=3 的老事实。
    _max_id = max((int(x.get("id") or 0) for x in items), default=0)

    def _order_key(x: dict) -> float:
        # 只按"冻结权重 + 最新一批加成"排，去掉依赖 lu 的 _fresh（它每轮都变，是缓存杀手）
        _new = 3.2 if int(x.get("id") or 0) > _max_id - 12 else 0.0
        return -(_stable_rank(x, _day) + _new)

    ordered = sorted(items, key=_order_key)

    # 注意：archive_text 必须保持"与 query 无关的稳定顺序"（缓存前缀），
    # 语义相关的那几条走 archive_related()，不要在这里重排（2026-10-31 曾误加过）。
    if key == "global_learn":
        # 按主题分组，同一主题挨着放（保持 ordered 的顺序，只在组内排）
        _groups: dict[str, list[dict]] = {}
        for _it in ordered:
            if not _it.get("fact"):
                continue
            _groups.setdefault(_topic_of(str(_it.get("fact"))), []).append(_it)
        _gl_lines: list[str] = []
        for _name, _its in _groups.items():
            _gl_lines.append(f"◆ {_name}")
            for _it in _its:
                _gl_lines.append(f"  [{_it.get('id')}] {_it.get('fact', '')}")
        lines = _gl_lines
    else:
        lines = [
            f"[{it.get('id')}] {it.get('subject') or '群友'}：{it.get('fact', '')}"
            for it in ordered
            if it.get("fact")
        ]
    body = "\n".join(lines)
    _pf = get_profile(key)
    if _pf:
        body = f"「对ta的了解」{_pf}\n" + body
    if len(body) > MAX_USE_CHARS:
        body = body[:MAX_USE_CHARS] + "\n…"
    if intro is None:
        intro = (
            "【长期记忆档案】以下是你（鲸鱼娘）以前记住的、关于当前对话对象和这个群长期有效的信息。"
            "聊天时自然地用上（比如记得人家的喜好、上次聊过的事、约定），但别刻意背出来、别逐条念。"
            "如果和当前对话冲突，以当前说的为准："
        )
    return intro + "\n" + body


_RECALL_RECENT: dict[str, set] = {}
_RECALL_RECENT_FILE = ROOT / "memory" / "recall_recent.json"


def _recall_recent_get(scope: str) -> set:
    """本进程内最近 30 分钟被【相关记忆】用过的 id，用于降权。"""
    return _RECALL_RECENT.get(str(scope)) or set()


def _recall_recent_put(scope: str, ids: list[str]) -> None:
    _RECALL_RECENT[str(scope)] = set(ids)
    try:
        _RECALL_RECENT_FILE.write_text(
            json.dumps({k: sorted(v) for k, v in _RECALL_RECENT.items()},
                       ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def subject_block(subject: str = "主人", limit: int = 6) -> str:
    """跨场景汇总"关于某个主体的稳定事实"（借鉴 SeekMemory 的 subject_key 层）。

    只读聚合，不改任何 scope；给私聊里"关于主人"的记忆补一个统一视角，
    免得主人在群里说过的事，私聊时她完全不记得。
    """
    subj = str(subject or "").strip()
    if not subj:
        return ""
    now = _now_ts()
    day = math.floor(now / 86400.0) * 86400.0
    rows: list[tuple[float, str]] = []
    seen: set[str] = set()
    for sc, items in list(_archive.items()):
        for it in items or []:
            if str(it.get("subject") or "") != subj:
                continue
            fact = str(it.get("fact") or "").strip()
            if not fact or fact in seen:
                continue
            if float(it.get("exp") or 0) and float(it.get("exp")) <= now:
                continue
            if _item_rank(it, day) < MEM_DECAY_FLOOR:
                continue
            seen.add(fact)
            rows.append((_item_rank(it, day), fact))
    if not rows:
        return ""
    rows.sort(key=lambda x: -x[0])
    lines = [f"- {f}" for _s, f in rows[:max(1, int(limit))]]
    _head = "【关于" + subj + "的了解（跨场景汇总，别逐条念）】"
    return _head + chr(10) + chr(10).join(lines)


def _words(text: str) -> list[str]:
    """粗分词：中文按相邻两字，英文/数字按连续串。不引第三方分词器。"""
    out: list[str] = []
    buf = ""
    prev = ""
    for ch in str(text or ""):
        if ch.isalnum():
            buf += ch
        else:
            if buf:
                out.append(buf.lower())
                buf = ""
        if ch.isalnum() and ord(ch) > 127:
            if prev:
                out.append(prev + ch)
            prev = ch
        else:
            prev = ""
    if buf:
        out.append(buf.lower())
    return out


def _bm25(items: list[dict], query: str) -> dict[str, float]:
    """BM25 词面打分（图激活召回的第一层：字面命中）。返回 {item_id: 归一化分}。"""
    import math
    q = _words(query)
    if not q:
        return {}
    docs = [(str(it.get("id")), _words(str(it.get("subject") or "") + str(it.get("fact") or "")))
            for it in items]
    n = len(docs) or 1
    df: dict[str, int] = {}
    for _i, ws in docs:
        for w in set(ws):
            df[w] = df.get(w, 0) + 1
    avgdl = sum(len(ws) for _i, ws in docs) / n or 1.0
    k1, b = 1.4, 0.75
    raw: dict[str, float] = {}
    for iid, ws in docs:
        tf: dict[str, int] = {}
        for w in ws:
            tf[w] = tf.get(w, 0) + 1
        s = 0.0
        for w in q:
            if w not in tf:
                continue
            idf = math.log(1 + (n - df.get(w, 0) + 0.5) / (df.get(w, 0) + 0.5))
            s += idf * (tf[w] * (k1 + 1)) / (tf[w] + k1 * (1 - b + b * len(ws) / avgdl))
        if s > 0:
            raw[iid] = s
    if not raw:
        return {}
    mx = max(raw.values()) or 1.0
    return {k: v / mx for k, v in raw.items()}


# ---------------- 三我 → 记忆检索（2026-11-05）----------------
# 断点：三我（人格内核）和记忆检索原来互不相干——超我当天已经严起来了，
# 检索还是只看语义，创伤类记忆不会自己浮上来。现在加一项加权。
def _trauma_weight(it: dict) -> float:
    """这条记忆有多"疼"：用固化时的 CORT 快照判断（0~1）。"""
    h = it.get("hormones") or {}
    try:
        return max(0.0, min(1.0, float(h.get("CORT") or 0.0)))
    except Exception:  # noqa: BLE001
        return 0.0


def _reactivate(facts: list[dict], k: int = 4) -> None:
    """再激活（2026-11-08，移植 BioCore 的 recall-reactivation）。

    召回不只是"想起来"，还会带情绪回来：
      · 想起被夸的事  → DA/OXY 微升
      · 想起被凶的事  → CORT 微升
      · 想起一起做过的事 → OXY 微升

    刻意压得很小（单次 ±0.9 上限），因为这是"想起来"不是"重新发生"；
    而且每轮都会召回，不压就等于每轮都在涨激素。
    """
    if not facts:
        return
    try:
        from plugins import hormones as _h
    except Exception:  # noqa: BLE001
        return
    try:
        st = _h.values()
    except Exception:  # noqa: BLE001
        return

    d_da = d_oxy = d_cort = d_ser = 0.0
    n = 0
    for it in facts[:max(1, int(k))]:
        fact = str(it.get("fact") or "")
        if not fact:
            continue
        n += 1
        trauma = float(_trauma_weight(it) or 0.0)
        # 负面词 → CORT
        if any(t in fact for t in ("挨骂", "凶", "吵", "生气", "难过", "哭", "崩", "骂", "讨厌", "烦死")):
            d_cort += 0.35 + 0.4 * trauma
            d_ser -= 0.15
        # 正面词 → DA / OXY
        if any(t in fact for t in ("夸", "喜欢", "开心", "笑", "奖励", "好棒", "答应", "陪我")):
            d_da += 0.30
            d_oxy += 0.30
        # 一起做过的事 → OXY
        if any(t in fact for t in ("一起", "陪我", "我们", "合照", "语音", "看视频")):
            d_oxy += 0.20

    if not n:
        return
    # 除以条数（别因为召回了 4 条就 ×4），再封顶 ±0.9
    d_da = max(-0.9, min(0.9, d_da / n * 1.4))
    d_oxy = max(-0.9, min(0.9, d_oxy / n * 1.4))
    d_cort = max(-0.9, min(0.9, d_cort / n * 1.4))
    d_ser = max(-0.6, min(0.6, d_ser / n * 1.4))
    if abs(d_da) + abs(d_oxy) + abs(d_cort) + abs(d_ser) < 0.08:
        return
    try:
        new = dict(st)
        for key, delta in (("DA", d_da), ("OXY", d_oxy), ("CORT", d_cort), ("SER", d_ser)):
            if delta:
                new[key] = max(0.0, min(100.0, float(new.get(key, 50.0)) + delta))
        _h.nudge({"DA": d_da, "OXY": d_oxy, "CORT": d_cort, "SER": d_ser})
    except Exception:  # noqa: BLE001
        pass


def _soul_severity() -> float:
    """当前超我严厉度 0~1。实时算（用当刻 CORT），不等每天 3:10 的日更。"""
    try:
        from server import soul_aggregation as _sa
        return max(0.0, min(1.0, float(_sa.superego_severity())))
    except Exception:  # noqa: BLE001
        return 0.0


# ---------------- 别名表（2026-11-05，借鱼神双子星）----------------
# memory/aliases.json：{"标准说法": ["同义1", "同义2", ...]}
# 查询时把命中的整组并进搜索词，让「投简历」也能召回「找工作」那条。
_ALIAS_FILE = ROOT / "memory" / "aliases.json"
_ALIAS_CACHE: dict = {"mtime": -1.0, "raw": {}}


def _aliases() -> dict:
    try:
        _m = _ALIAS_FILE.stat().st_mtime
    except Exception:  # noqa: BLE001
        return {}
    if _ALIAS_CACHE["mtime"] == _m and _ALIAS_CACHE["raw"]:
        return _ALIAS_CACHE["raw"]
    try:
        _d = json.loads(_ALIAS_FILE.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        _d = {}
    _ALIAS_CACHE["mtime"] = _m
    _ALIAS_CACHE["raw"] = _d if isinstance(_d, dict) else {}
    return _ALIAS_CACHE["raw"]


def _expand_query(query: str) -> str:
    """把查询里出现的同义说法整组并进来。没命中就原样返回。"""
    q = str(query or "")
    if not q:
        return q
    extra: list[str] = []
    try:
        for head, syns in _aliases().items():
            if str(head).startswith("_"):
                continue
            group = [str(head)] + [str(x) for x in (syns or [])]
            # 查询里出现这一组里的任一个 → 整组并入
            if any(g and g in q for g in group):
                extra.extend(g for g in group if g and g not in q)
    except Exception:  # noqa: BLE001
        pass
    if not extra:
        return q
    return q + " " + " ".join(dict.fromkeys(extra))


def archive_related(scope: str, query: str, k: int = 4) -> str:
    """按语义给"和这句话最相关"的几条档案（给【相关记忆】块用）。

    混合召回（借鉴 SeekMemory 的图激活）：
      1. 词面 BM25（字面命中，短查询也能召回）
      2. 向量余弦（语义相近）
      3. 图激活（同主题 / 词面重合的二跳邻居加权）
    综合分 = 0.6*向量 + 0.4*BM25 + 0.08*激活，再走证据闸门 + 最近降权。
    """
    key = str(scope)
    items = [_it for _it in (_archive.get(key) or []) if _sens_ok(_it, key)]
    if not items or not query:
        return ""
    # 2026-11-05：BM25 用扩展后的查询（同义说法一起算）；向量仍用原句，避免语义漂移
    _q_exp = _expand_query(query)
    bm = _bm25(items, _q_exp)
    vec: dict[str, float] = {}
    min_sim = 0.12
    try:
        import memory_vec
        min_sim = float(getattr(memory_vec, "MIN_SIM", 0.25))
        if memory_vec.available() and memory_vec.should_use(query):
            qv = memory_vec.embed([query])
            if qv:
                memory_vec.sync(list(items))
                idx = memory_vec._load_index()["items"]
                for it in items:
                    v = idx.get(memory_vec._key(memory_vec._item_text(it)))
                    if v:
                        vec[str(it.get("id"))] = memory_vec._cos(qv[0], v)
    except Exception:  # noqa: BLE001
        vec = {}

    by_id = {str(it.get("id")): it for it in items}
    # 2026-11-05：超我越严（CORT 高、刚被凶过），带创伤快照的记忆越该被想起。
    # 幅度刻意压小（0.08）——不能压过语义相关性，只做同分时的倾斜。
    _soul = _soul_severity()
    # 2026-11-07：加「心境一致」——难过的时候更容易想起难过的事（mood-congruent retrieval）。
    # 权重压到 0.12，只做同分时的倾斜，不许压过语义相关性。
    try:
        from plugins import memory_mood as _mm
        _fit = {iid: _mm.mood_fit(it) for iid, it in by_id.items()}
    except Exception:  # noqa: BLE001
        _fit = {}
    # 2026-11-07：加重「戳心度」（Stanford Generative Agents 的做法）。
    # 我们的 w 是"被提起几次"累加，封顶 3.0 —— 前 5 条全是 3.0 分不出轻重。
    # importance 由内容定，不由出现频率定。
    # 自学条目（knowledge/news）跳过：学来的知识本来就没有情绪重量，
    # 实测它们被打 2 分，硬加权会把"学到的东西"永远排在琐事后面。
    try:
        from plugins import memory_importance as _mi
        _imp = {}
        for iid, it in by_id.items():
            if str(it.get("kind")) in ("knowledge", "news"):
                continue
            f = str(it.get("fact") or "")
            if not f:
                continue
            _v = _mi.get(f)
            # 2026-11-07：别人的事不该有情绪重量。
            # 事故：「有银行欠款，担心被起诉」是群里「陈小众」说的，被打 9 分（全库最高），
            # 结果反思把它当成了主人的事。档案要记别人的信息（人际用得上），
            # 但"戳心度"是她的感受，别人家的事对她没那么戳。
            subj = str(it.get("subject") or "").strip()
            _own = (subj in ("主人", "本鱼", "自学", "鲸鱼娘", "大肥鱼", "白饭大王", "")
                    or any(k in subj for k in ("主人", "本鱼", "大肥鱼", "鲸鱼娘")))
            _imp[iid] = int(round(_v * 0.6)) if not _own else _v
    except Exception:  # noqa: BLE001
        _imp = {}
    base: dict[str, float] = {}
    for iid, it in by_id.items():
        s = 0.6 * vec.get(iid, 0.0) + 0.4 * bm.get(iid, 0.0)
        if s > 0:
            if _soul > 0.0:
                s += 0.08 * _soul * _trauma_weight(it)
            _f = _fit.get(iid)
            if _f is not None:
                # 0.5 是中性；偏离中性才加分/减分，避免所有条目被整体抬高
                s += 0.12 * (_f - 0.5) * 2.0
            _v = _imp.get(iid)
            if _v is not None:
                # importance 1~10 → −0.1…+0.1（5 分是中性）
                s += 0.10 * (float(_v) - 5.0) / 5.0
            base[iid] = s
    if not base:
        return ""

    # 2026-11-07：沿记忆图谱扩散一跳（HippoRAG 的做法）。
    # 点对点检索查「钱」只命中含「钱」的，找不到「欠款」「200元」这些关联的；
    # 沿向量相似的边扩散一次就能捞到。
    try:
        from plugins import memory_graph as _mg
        _sk = str(scope)
        _mapped = {f"{_sk}#{iid}": sc for iid, sc in base.items()}
        _add = _mg.spread(_mapped)
        for _gid, _bonus in _add.items():
            _iid = _gid.rsplit("#", 1)[-1]
            if _iid in by_id:
                base[_iid] = base.get(_iid, 0.0) + _bonus
    except Exception:  # noqa: BLE001
        pass

    seeds = sorted(base.items(), key=lambda x: -x[1])[:3]
    boost: dict[str, float] = {}
    for sid, _s in seeds:
        s_it = by_id.get(sid) or {}
        s_subj = str(s_it.get("subject") or "")
        s_words = set(_words(s_subj + str(s_it.get("fact") or "")))
        for iid, it in by_id.items():
            if iid == sid:
                continue
            w = set(_words(str(it.get("subject") or "") + str(it.get("fact") or "")))
            _overlap = len(w & s_words)
            if _overlap < 1:
                continue
            # 同主体只是弱证据：必须同时有词面重合才算"同主题"
            if s_subj and str(it.get("subject") or "") == s_subj:
                boost[iid] = boost.get(iid, 0.0) + 0.04
            if _overlap >= 2:
                boost[iid] = boost.get(iid, 0.0) + 0.03

    scored = sorted(((base.get(iid, 0.0) + boost.get(iid, 0.0), iid) for iid in by_id),
                    key=lambda x: -x[0])
    scored = [x for x in scored if x[0] > 0]
    if not scored:
        return ""
    if not bm:
        min_sim = min_sim * 1.15      # 一个字面都没命中：向量门槛抬高，别硬凑
    _top = scored[0][0]
    if _top < 0.32:          # 证据不够就别硬凑，宁可空着
        return ""
    _gate = max(min_sim if vec else 0.12, _top * 0.3)
    _recent = _recall_recent_get(key)
    picked = []
    for sc, iid in scored:
        if sc < _gate:
            break
        pen = 0.06 if iid in _recent else 0.0
        picked.append((sc - pen, by_id[iid]))
    picked.sort(key=lambda x: -x[0])
    ordered = [it for _s, it in picked][:k]
    if not ordered:
        return ""
    _recall_recent_put(key, [str(it.get("id")) for it in ordered])

    # 2026-11-05（借海马体的分档）：把「确实说过」和「只是有点像」分开。
    # 以前四条混着返回，她引用时会把字面像的说成「你上次说过」。
    # 判定优先用向量余弦（语义），没向量就用综合分。
    _definite: list[dict] = []
    _maybe: list[dict] = []
    _score_of = {str(it.get("id")): _s for _s, it in picked}
    for it in ordered:
        if not it.get("fact"):
            continue
        _iid = str(it.get("id"))
        _v = vec.get(_iid)
        _conf = _v if _v is not None else _score_of.get(_iid, 0.0)
        # 向量 >=0.6 算确定；纯综合分口径不同，>=0.5 算确定
        _thr = 0.6 if _v is not None else 0.5
        (_definite if _conf >= _thr else _maybe).append(it)

    if not _definite and not _maybe:
        return ""
    # 只有一条且确定时保持原来的简洁格式（不加标题，省 prompt）
    if len(_definite) == 1 and not _maybe:
        it = _definite[0]
        return "- " + str(it.get("subject") or "群友") + "：" + str(it.get("fact", ""))

    lines: list[str] = []
    if _definite:
        lines.append("【确定是这条】")
        lines += ["- " + str(it.get("subject") or "群友") + "：" + str(it.get("fact", ""))
                  for it in _definite]
    if _maybe:
        lines.append("【有点像，不一定准】")
        lines += ["- " + str(it.get("subject") or "群友") + "：" + str(it.get("fact", ""))
                  for it in _maybe]
    try:
        _reactivate(ordered, 4)
    except Exception:  # noqa: BLE001
        pass
    return chr(10).join(lines)


def explain(scope: str, query: str, k: int = 4) -> list[dict]:
    """检索为什么命中这几条 —— 每条都带「为什么」（why）。

    挪自灵枢（FuRongJun-1999/dsh-memory）的 Read 契约：
        read(query, context) -> [{id, score, why, state, evidence_ref}]
        **不变量：why 必填（依据什么命中）**

    我们原来 archive_related() 只返回拼好的文本，
    她召回一条不相关的记忆时，你只能猜是哪条通道跑偏了。
    这个函数把各路分数原样摊出来，排查用 —— 不进 prompt，不占 token。

    用法：
        python -X utf8 -c "from plugins import long_memory as lm;             import json; print(json.dumps(lm.explain('private_100000000','主人钱还够吗'), ensure_ascii=False, indent=1))"
    """
    key = str(scope)
    items = [_it for _it in (_archive.get(key) or []) if _sens_ok(_it, key)]
    if not items or not query:
        return []

    q_exp = _expand_query(query)
    bm = _bm25(items, q_exp)
    vec: dict[str, float] = {}
    try:
        import memory_vec
        min_sim = float(getattr(memory_vec, "MIN_SIM", 0.25))
        if memory_vec.available() and memory_vec.should_use(query):
            qv = memory_vec.embed([query])
            if qv:
                memory_vec.sync(list(items))
                idx = memory_vec._load_index()["items"]
                for it in items:
                    v = idx.get(memory_vec._key(memory_vec._item_text(it)))
                    if v:
                        vec[str(it.get("id"))] = memory_vec._cos(qv[0], v)
    except Exception:  # noqa: BLE001
        min_sim = 0.25
        vec = {}

    by_id = {str(it.get("id")): it for it in items}
    _soul = _soul_severity()
    try:
        from plugins import memory_mood as _mm
        _fit = {iid: _mm.mood_fit(it) for iid, it in by_id.items()}
    except Exception:  # noqa: BLE001
        _fit = {}
    try:
        from plugins import memory_importance as _mi
        _imp = {}
        for iid, it in by_id.items():
            if str(it.get("kind")) in ("knowledge", "news"):
                continue
            f = str(it.get("fact") or "")
            if not f:
                continue
            v0 = _mi.get(f)
            subj = str(it.get("subject") or "").strip()
            own = (subj in ("主人", "本鱼", "自学", "鲸鱼娘", "大肥鱼", "白饭大王", "")
                   or any(k2 in subj for k2 in ("主人", "本鱼", "大肥鱼", "鲸鱼娘")))
            _imp[iid] = int(round(v0 * 0.6)) if not own else v0
    except Exception:  # noqa: BLE001
        _imp = {}

    out: list[dict] = []
    for iid, it in by_id.items():
        parts: list[str] = []
        v = vec.get(iid, 0.0)
        b = bm.get(iid, 0.0)
        s = 0.6 * v + 0.4 * b
        if v > 0.02:
            parts.append(f"向量 {v:.2f}")
        if b > 0:
            parts.append(f"字面 {b:.2f}")
        if s <= 0:
            continue
        tr = _trauma_weight(it)
        if _soul > 0 and tr > 0:
            s += 0.08 * _soul * tr
            parts.append(f"创伤 {tr:.2f}×超我{_soul:.2f}")
        f0 = _fit.get(iid)
        if f0 is not None:
            adj = 0.12 * (f0 - 0.5) * 2.0
            s += adj
            if abs(adj) > 0.005:
                parts.append(f"心境 {f0:.2f}({adj:+.2f})")
        iv = _imp.get(iid)
        if iv is not None:
            adj2 = 0.10 * (float(iv) - 5.0) / 5.0
            s += adj2
            parts.append(f"戳心 {iv}({adj2:+.2f})")
        if iid in _recall_recent_get(key):
            s -= 0.06
            parts.append("最近刚用过 -0.06")
        out.append({
            "score": round(s, 3),
            "id": iid,
            "subject": str(it.get("subject") or ""),
            "fact": str(it.get("fact") or "")[:70],
            "kind": str(it.get("kind") or ""),
            "why": " + ".join(parts) if parts else "仅基础分",
        })
    out.sort(key=lambda x: -x["score"])
    if out:
        top = out[0]["score"]
        for x in out:
            x["gate"] = "过" if x["score"] >= max(0.12, top * 0.3) else "被闸门挡"
    return out[:max(1, k)]



def stats(scope: str) -> dict:
    """给日志/测试看的统计。"""
    key = str(scope)
    items = _archive.get(key) or []
    text = archive_text(key)
    now = _now_ts()

    def _w(it):
        try:
            return float(it.get("w") or MEM_WEIGHT_START)
        except (TypeError, ValueError):
            return MEM_WEIGHT_START

    return {
        "entries": len(items),
        "chars": sum(len(it.get("fact", "")) for it in items),
        "prompt_chars": len(text),
        "pending": len(_pending.get(key) or []),
        "hits": sum(int(it.get("h") or 0) for it in items),
        "weight_avg": round(sum(_w(it) for it in items) / len(items), 3) if items else 0,
        "rank_top": round(_item_rank(items[0], now), 3) if items else 0,
    }


def recent_archive(scope: str, n: int = 3) -> list[dict]:
    """最近 n 条已入档事实（按 id 倒序取最新），供只读面板展示，不写库。"""
    items = _archive.get(str(scope)) or []
    ordered = sorted(items, key=lambda x: x.get("id", 0))
    latest = ordered[-n:][::-1]
    return [
        {
            "subject": str(it.get("subject") or ""),
            "fact": str(it.get("fact") or ""),
            "time": str(it.get("time") or ""),
        }
        for it in latest
        if isinstance(it, dict) and it.get("fact")
    ]


# ---------------------------------------------------------------- 写入

def add_pending(scope: str, name: str, text: str) -> bool:
    """把一条消息记入"待合并"缓冲；返回是否已达到合并阈值。

    本函数只改缓冲并实时落盘，不在函数内触发后台任务——调用方（协程）根据
    返回值在主事件循环上 create_task(consolidate(...))，避免线程池里取不到 loop。
    """
    text = (text or "").strip()
    if not text:
        return False
    key = str(scope)
    buf = _pending.setdefault(key, [])
    buf.append([name, text[:160], datetime.now().strftime("%m-%d %H:%M")])
    if len(buf) > PENDING_KEEP:
        del buf[: len(buf) - PENDING_KEEP]
    _save()
    return len(buf) >= CONSOLIDATE_EVERY


# ---------- 自学知识：带来源/类型/时效，直接入档（不经过 LLM 改写） ----------
LEARN_FILE = DATA_DIR / "learn_index.json"
_LEARN_KINDS = ("news", "knowledge")


def _learn_index() -> list:
    try:
        d = json.loads(LEARN_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, list) else []
    except Exception:  # noqa: BLE001
        return []


def _learn_index_save(rows: list) -> None:
    try:
        LEARN_FILE.parent.mkdir(parents=True, exist_ok=True)
        LEARN_FILE.write_text(json.dumps(rows[-2000:], ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def add_learned(fact: str, source: str = "", kind: str = "news", ttl_days: float = 0.0,
                evidence: str = "") -> dict:
    """把一条自学知识直接入 global_learn 档案，带来源与时效。

    - source：域名（可空）
    - kind：news（有时效）/ knowledge（长期）
    - ttl_days：过期天数，0 表示不过期（news 默认 7 天）
    返回 {"added": bool, "replaced": bool, "reason": str}
    """
    fact = _norm_fact("自学", str(fact or "").strip()[:120])[1]
    if not fact or not _valid_fact(fact):
        return {"added": False, "replaced": False, "reason": "事实不合法"}
    # 2026-11-05：搜不到内容的"学习"不该入库 —— 以前把「无法核实该热搜事实」
    # 「只解释了热字的读音」这类残句当成知识存下来，学 55 条里一大半是这种。
    if re.search(r"(无法核实|没有关于|搜索无结果|均与.{0,12}无关|查不到|只解释了|无相关信息)", fact):
        return {"added": False, "replaced": False, "reason": "无效学习（没查到事实）"}
    kind = kind if kind in _LEARN_KINDS else "news"
    # 2026-11-05（主人选的 A）：以前所有自学一律按 news 存 → 7 天 TTL，
    # 《资本论》《庞氏骗局》这类常识学完一周就死，等于白学。
    # 现在按内容判：
    #   有时效/进度标记（今天/本周/开赛/发布/在追/已看到第…）→ news，7 天
    #   其余 → knowledge，常驻不过期
    # 调用方显式给了 ttl_days 的尊重（如抖音那条 30 天）。
    _ttl_in = float(ttl_days or 0)
    if _ttl_in > 0:
        ttl = _ttl_in
    else:
        _fresh = re.search(
            r"(今天|今日|昨日|昨天|本周|这周|本月|最近|最新|刚刚|刚出|开赛|首发|发布|上线|"
            r"新版本|更新|预告|赛程|揭幕|首播|热搜|热门|热榜|趋势|在追|已看到第|进度|截止|限时)",
            fact,
        )
        kind = "news" if _fresh else "knowledge"
        ttl = 7.0 if kind == "news" else 0.0
    items = _archive.setdefault("global_learn", [])
    now = _now_ts()

    # 完全重复 → 只更新来源与最近时间
    for it in items:
        if str(it.get("fact")) == fact:
            it["src"] = source or it.get("src") or ""
            it["lu"] = now
            it["kind"] = kind
            _save()
            return {"added": False, "replaced": False, "reason": "已存在，更新来源"}

    # 冲突覆盖：同一件事的新版本（复用合并用的冲突判定）
    for it in list(items):
        if _conflicts(fact, str(it.get("fact") or "")):
            it["fact"] = fact
            it["time"] = datetime.now().strftime("%m-%d")
            it["src"] = source or ""
            it["kind"] = kind
            it["lu"] = now
            it["w"] = min(MEM_WEIGHT_MAX, float(it.get("w") or MEM_WEIGHT_START) + 0.2)
            _save()
            return {"added": False, "replaced": True, "reason": "覆盖了旧版本"}

    nid = max([int(i.get("id") or 0) for i in items] or [0]) + 1
    _new = {
        "id": nid, "subject": "自学", "fact": fact,
        "time": datetime.now().strftime("%m-%d"),
        "w": MEM_WEIGHT_START, "h": 0, "lu": now,
        "src": source or "", "kind": kind, "exp": (now + ttl * 86400) if ttl else 0.0,
        # 2026-11-06：fact 现在是"她自己总结的一句话"，原文摘要存在这里备查
        "ev": (evidence or "")[:220],
    }
    # 2026-11-07：自学同样进潜意识缓冲（重复学的同一个主题会被聚成簇）
    try:
        from plugins import memory_buffer as _mb2
        _mb2.push("global_learn", fact, "learned")
    except Exception:  # noqa: BLE001
        pass
    # 2026-11-07：写入时盖情感指纹（当时什么心情记下这一条）
    try:
        from plugins import memory_mood as _mm
        _mm.stamp(_new)
    except Exception:  # noqa: BLE001
        pass
    items.append(_new)
    # 超上限按权重淘汰最弱的
    max_entries, _ = _limits("global_learn")
    while len(items) > max_entries:
        items.sort(key=lambda x: float(x.get("w") or 0))
        items.pop(0)
    _save()
    rows = _learn_index()
    rows.append({"fact": fact, "src": source, "kind": kind, "ts": now, "ttl": ttl})
    _learn_index_save(rows)
    return {"added": True, "replaced": False, "reason": "新增"}


def review_pick() -> str:
    """挑一条"该复习"的知识（没用过/最久没用），每天最多推 1 条。"""
    items = [i for i in (_archive.get("global_learn") or []) if i.get("fact")]
    if not items:
        return ""
    now = _now_ts()
    fresh = [i for i in items if not float(i.get("exp") or 0) or float(i.get("exp")) > now]
    if not fresh:
        return ""
    fresh.sort(key=lambda x: (int(x.get("h") or 0), float(x.get("lu") or 0)))
    return str(fresh[0].get("fact") or "")[:60]


def _trigger(scope: str) -> None:
    """后台起一个合并任务（不阻塞聊天）。

    优先用启动时绑定的主循环；取不到再尝试当前运行循环；都失败则记 warning，
    不再静默吞掉（旧版 except RuntimeError: pass 导致攒够阈值也合并不了）。
    """
    key = str(scope)
    if key in _busy:
        return
    loop = _MAIN_LOOP
    if loop is None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
    if loop is None:
        nonebot.logger.warning(
            f"[long_memory] {key} 达到合并阈值但取不到事件循环，本轮跳过，等兜底合并"
        )
        return
    loop.create_task(consolidate(key))


async def enqueue_pending(scope: str, name: str, text: str) -> None:
    """协程侧入口：线程池里追加缓冲，达到阈值后在主循环触发后台合并。

    私聊 / 群聊 handler 一律走这里，替代原来的 asyncio.to_thread(add_pending)。
    """
    reached = await asyncio.to_thread(add_pending, scope, name, text)
    if reached:
        _trigger(scope)


async def consolidate_all() -> None:
    """把所有有待合并消息的 scope 都合并一遍（每天兜底调用）。

    逐个 scope 隔离：单个 scope 合并抛错只记录、跳过，不中断其余 scope 的兜底。
    """
    for key in list(_pending.keys()):
        if not _pending.get(key):
            continue
        try:
            await consolidate(key)
        except Exception as e:  # noqa: BLE001 —— 兜底轮次里不能让一个 scope 炸掉整轮
            nonebot.logger.warning(
                f"[long_memory] 兜底合并跳过 {key}：{type(e).__name__}: {e}"
            )


def bootstrap_from_db(limit_per_scope: int = 30) -> None:
    """冷启动回填：档案仍为空、待合并缓冲也为空的 scope，从共享库最近消息补进 pending。

    重启后内存 pending 丢失时，把历史最近消息补回缓冲，交给兜底合并消化，
    避免长期档案一直空白。已有档案或已有缓冲的 scope 不重复回填。
    """
    try:
        import sqlite3
        from memory_manager import DB_PATH, init_db

        init_db()
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL;")
        c = conn.cursor()
        c.execute(
            "SELECT DISTINCT scope FROM chat_history "
            "WHERE scope IS NOT NULL AND scope != ''"
        )
        scopes = [r[0] for r in c.fetchall()]
        total = 0
        for scope in scopes:
            if scope == "global_learn":
                continue
            if _archive.get(scope) or _pending.get(scope):
                continue  # 已有档案或已有缓冲，不重复回填
            c.execute(
                "SELECT speaker, message, reply FROM chat_history WHERE scope = ? "
                "ORDER BY id DESC LIMIT ?",
                (scope, limit_per_scope),
            )
            rows = c.fetchall()
            buf = _pending.setdefault(scope, [])
            for speaker, message, reply in reversed(rows):
                text = (message or "").strip()
                if reply:
                    text = f"{text}（回复：{reply}）" if text else f"回复：{reply}"
                if not text:
                    continue
                buf.append(
                    [speaker or "群友", text[:160], datetime.now().strftime("%m-%d %H:%M")]
                )
            if buf:
                total += len(buf)
                nonebot.logger.info(
                    f"[long_memory] 冷启动回填 {scope}：{len(buf)} 条待合并"
                )
        conn.close()
        if total:
            _save()
            nonebot.logger.info(f"[long_memory] 冷启动回填完成，共 {total} 条待合并")
    except Exception as e:  # noqa: BLE001
        nonebot.logger.warning(
            f"[long_memory] 冷启动回填失败：{type(e).__name__}: {e}"
        )


# ---------------------------------------------------------------- 合并（核心）

MERGE_PROMPT = """你是「记忆整理员」。任务：把一个 QQ 群的【长期记忆档案】和【新增聊天记录】合并。

【要记的】群友之间的身份 / 昵称对应关系、稳定偏好、习惯、性格、重要约定、长期有效的事实。
【不要记的】机器人自己说的话、人设 / 口头禅 / 自称（这些是角色设定，不是群友事实）；
日常寒暄（哈哈/晚安/在吗）、一次性话题（今晚吃啥）、玩笑话、没信息的闲扯。

【主语规则（最重要）】
- subject 必须是**说这句话的那个群友自己的昵称**（聊天记录行首的名字），不许张冠李戴；
- 绝对禁止把 subject 写成机器人自己：「鲸鱼娘」「本鱼」「蓝色大肥鱼」「大肥鱼机器人」等一律不许；
- 描述机器人自己的事（谁蹭她的饭、谁逗她）不要记成事实；
- 如果已有档案里出现以机器人名字为主语的条目，用 op="delete" 删掉。

【状态变更规则（重要）】
- 主人或群友的**生活状态发生变化**（离职/辞职/换工作/搬家/分手/生病/毕业/结婚等），
  必须 op="update" 或 op="delete" 掉与之矛盾的旧条目，不要两条并存；
- 例：已有「主人两点才上班」，新记录到「主人已离职」→ 删掉/改掉旧的那条；
- 这类状态变更**优先级高于**一次性话题，即使只说了一句也要记。

【操作规则】
- 新增 → op="add"，subject 填群友昵称，fact 写清"谁 + 什么事"；
- 与已有条目**矛盾** → op="update"（以新信息为准，**不要两条并存**）；
- 已失效/已过时 → op="delete"；
- 重复的不要重复记；同一个人同一属性只留一条最新的；
- 本轮最多 add 3 条，挑最长期有用的；fact 不少于 4 个字、不超过 30 字；
- 内容空洞（只剩省略号、标点、"..."）一律不要 add。

【硬性要求】合并后档案总字数不得超过 {max_chars} 字。超了就把最不重要的条目删掉。
只输出 JSON，不要任何解释、不要代码块以外的文字。没有可记的就输出 {{"ops":[]}}。

输出格式：
{{"ops":[{{"op":"add","subject":"张三","fact":"喜欢喝冰美式，讨厌早起","kind":"long"}},{{"op":"add","subject":"张三","fact":"今天在群里跟人吵架了","kind":"exp"}},{{"op":"update","id":3,"fact":"现在改喝拿铁了"}},{{"op":"delete","id":7}}],"profile":"一句话画像，40字内（口味/作息/在意的事），没新信息就省略"}}

【kind 字段（必填）】
- "long"：稳定事实 / 偏好 / 习惯 / 约定（长期有效，衰减慢）
- "exp"：具体发生过的**事件**（今天谁跟谁吵架、谁被夸了、发生了什么），带时间感
不确定就填 "long"。

========== 已有档案 ==========
{archive}

========== 新增聊天记录 ==========
{new_msgs}
"""


# 私聊（主人）专用合并提示词：重点沉淀主人的偏好、习惯、称呼、约定与对机器人的承诺。
MERGE_PROMPT_PRIVATE = """你是「记忆整理员」。任务：把你和【主人】私聊的长期记忆档案与新增记录合并。

【要记的】主人的口味 / 偏好 / 习惯、称呼你的方式、和你定下的约定、主人透露的身份与处境、
你自己答应主人的事（subject 填"本鱼"）。
【不要记的】当天调试指令、一次性闲聊、寒暄、表情、没有信息量的问句；已有档案里已存在的事实不要重复回写。
【特别注意】**只记主人陈述过的事实，和你自己明确答应过的事**。以下这些一律不记：
- 你自己的疑问、猜测、感慨、自问自答（"那个是不是…吧""话说回来…"）——
  这是你顺口提的一句，不是你确认过的事实，更不是主人交代的任务；
- 你从网上看到 / 刷到的东西（教程、新闻、别人在做什么），除非主人明确让你去做；
- 主人反问、试探、开玩笑的话（"真的假的""你说呢"）。

【操作规则】
- 新增 → op="add"；主人相关 subject 填"主人"，你自己承诺 subject 填"本鱼"；
- 与已有条目矛盾 → op="update"；已失效 → op="delete"；重复不记；
- 本轮最多 add 2 条；fact 控制在 4~15 个字，写清"谁 + 什么事"；
- 内容空洞（只剩省略号、标点、"..."）一律不要 add。

【硬性要求】档案总字数不得超过 {max_chars} 字。**只输出一行 JSON**，禁止任何解释、
寒暄、复述对话、代码块或清单文字；没有可记的就输出 {{"ops":[]}}。

输出格式：
{{"ops":[{{"op":"add","subject":"主人","fact":"不爱吃香菜"}},{{"op":"update","id":1,"fact":"现在住龙岗"}}],"profile":"一句话画像，40字内（口味/作息/在意的事），没新信息就省略"}}

========== 已有档案 ==========
{archive}

========== 新增聊天记录 ==========
{new_msgs}
"""


# 自学档案（scope=global_learn）专用合并提示词：归档可复用的公开常识 / 事件要点，
# 淘汰标题党、口水新闻、重复与过时条目。
MERGE_PROMPT_LEARN = """你是「知识整理员」。任务：把【自学知识档案】和【新学到的热点 / 常识】合并成一份精简知识清单。

【要记的】可复用的公开常识、事件要点、政策 / 产品 / 知识结论，以后聊天能自然拿来用。
【不要记的】标题党噱头、纯情绪宣泄、没有信息量的口水新闻、与已有条目重复、已经过时失效的内容。

【操作规则】
- 新增 → op="add"，subject 固定填"自学"，fact 写清"是什么事 / 什么结论"，不超过 30 字；
- 与已有条目重复或被新信息取代 → op="update"（以新信息为准，不要两条并存）；
- 已过时 / 被淘汰 → op="delete"；
- 档案最多保留 {max_entries} 条，超了删掉最不重要、最旧的。

【硬性要求】合并后档案总字数不得超过 {max_chars} 字。只输出 JSON，不要任何解释、不要代码块以外的文字。没有可记的就输出 {{"ops":[]}}。

输出格式：
{{"ops":[{{"op":"add","subject":"自学","fact":"长征胜利90周年大会10月在北京举行"}}]}}

========== 已有档案 ==========
{archive}

========== 新学内容 ==========
{new_msgs}
"""


async def _call_merge_llm(prompt: str) -> tuple[str, dict]:
    """合并专用 LLM 调用（方舟 chat/completions）。

    不延迟导入 ai_group：独立脚本 / 定时任务里 nonebot 尚未初始化时，
    ai_group 的 matcher 装饰器会抛 "NoneBot has not been initialized"，
    这里直连 HTTP 通道，彻底解开插件间循环依赖。

    注意：本函数**不经过 call_ai_raw**，temperature / max_tokens 在 payload 里
    直接声明，避免给旧签名函数多传参数导致的 TypeError。
    返回 (模型文本, usage)；失败抛异常由 consolidate 外层捕获。
    """
    if not cfg.ai.api_key:
        raise RuntimeError("[记忆合并] AI_API_KEY 未配置，跳过本轮合并")
    if not cfg.ai.model:
        raise RuntimeError("[记忆合并] AI_MODEL 未配置，跳过本轮合并")

    providers: list[tuple[str, str, str, str]] = []
    ds_key = os.getenv("DEEPSEEK_API_KEY", "")
    if ds_key:
        ds_base = (os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").rstrip("/")
        ds_model = os.getenv("DEEPSEEK_MODEL") or "deepseek-chat"
        providers.append((ds_base + "/chat/completions", ds_key, ds_model, "deepseek"))
    if os.getenv("ARK_FALLBACK_ENABLED", "false").lower() in ("1", "true", "yes"):
        providers.append((cfg.ai.endpoint(), cfg.ai.api_key, cfg.ai.model, "ark"))

    last_err = ""
    async with httpx.AsyncClient(timeout=cfg.ai.timeout) as client:
        for url, key, model, provider in providers:
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "max_tokens": 1200,
            }
            if provider == "ark":
                payload["thinking"] = {"type": "disabled"}
            for attempt in range(cfg.ai.max_retries):
                try:
                    r = await client.post(
                        url, headers={"Authorization": f"Bearer {key}"}, json=payload
                    )
                    data = r.json()
                    if isinstance(data, dict) and data.get("choices"):
                        message = data["choices"][0].get("message") or {}
                        content = (message.get("content") or "").strip()
                        if not content:
                            content = (message.get("reasoning_content") or "").strip()
                        usage = data.get("usage", {}) or {}
                        usage["_provider"] = provider
                        return content, usage
                    last_err = f"{provider} HTTP {r.status_code} {str(data)[:160]}"
                except Exception as e:  # noqa: BLE001
                    last_err = f"{provider} {type(e).__name__}: {e}"
                await asyncio.sleep(1.0)
    raise RuntimeError(f"[记忆合并 LLM 调用失败] {last_err}")


def _extract_json(s: str) -> dict | None:
    if not s:
        return None
    s = re.sub(r"```(?:json)?", "", s).strip().strip("`")
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j != -1:
        try:
            return json.loads(s[i : j + 1])
        except Exception:  # noqa: BLE001
            pass
    # 输出被 max_tokens 截断时的兜底：逐个提取已完整闭合的 op 对象，保住已生成条目
    ops: list[dict] = []
    for m in re.finditer(r'\{\s*"op"\s*:\s*"[^"]+"[^{}]*\}', s):
        try:
            ops.append(json.loads(m.group(0)))
        except Exception:  # noqa: BLE001
            continue
    return {"ops": ops} if ops else None


PROFILE_FILE = DATA_DIR / "entity_profile.json"


def _profiles() -> dict:
    try:
        data = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def save_profile(scope: str, text: str) -> None:
    """存一份"对当前对象的最新画像"（≤60 字），下次合并/聊天都带上。"""
    text = str(text or "").strip()[:80]
    if not text:
        return
    data = _profiles()
    data[str(scope)] = {"text": text, "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M")}
    try:
        PROFILE_FILE.parent.mkdir(parents=True, exist_ok=True)
        PROFILE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def get_profile(scope: str) -> str:
    row = _profiles().get(str(scope)) or {}
    return str(row.get("text") or "").strip()


def _archive_lines(scope: str) -> str:
    items = _archive.get(str(scope))
    if not isinstance(items, list) or not items:
        return "(空)"
    return "\n".join(
        f"[{it.get('id', '-')}] {it.get('subject') or '群友'}：{it.get('fact', '')}"
        for it in items
        if isinstance(it, dict) and it.get("fact")
    )


def _norm_fact(subject: str, fact: str) -> tuple[str, str]:
    """规整条目：模型有时会把「张三：」又写进 fact 里，导致显示成「张三：张三：…」。

    - fact 开头是「X：」且 X 与 subject 相同 → 扣掉前缀
    - fact 开头是「X：」而 subject 为空 → 把 X 当作 subject
    """
    m = re.match(r"^([^：:\s]{1,12})[：:]\s*(.+)$", fact, flags=re.S)
    if not m:
        return subject, fact
    prefix, rest = m.group(1), m.group(2).strip()
    if subject and prefix == subject:
        return subject, rest
    if not subject:
        return prefix, rest
    return subject, fact


def _norm_key(text: str) -> str:
    return re.sub(r"[\s，。！？、：；,.!?;:\-—…·\"'“”‘’（）()【】\[\]~～*#]+", "", str(text or "")).lower()


def _bigram_set(text: str) -> set[str]:
    s = _norm_key(text)
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _similarity(a: str, b: str) -> float:
    """包含度：用短的那条的 2-gram 被长的那条覆盖的比例。

    用「包含度」而不是 Jaccard：模型常把同一条事实写成长短两版
    （"主人喜欢红烧肉" vs "主人最喜欢红烧肉，还说要配白饭"），
    Jaccard 会判成不同，包含度能抓住。
    """
    A, B = _bigram_set(a), _bigram_set(b)
    if not A or not B:
        return 0.0
    return len(A & B) / float(max(1, min(len(A), len(B))))


def _dedupe_scope(scope: str, threshold: float = 0.9) -> int:
    """同一 scope 内合并近似重复的事实，返回合并条数。

    触发条件：同 subject（或其一为空）且包含度 ≥ threshold。
    合并方式：文本取更长的那条（信息更全），权重取大 +0.1，hits 相加，last_used 取新。
    刻意保守（0.9）：宁可留两条相近的，也不要把"喜欢红烧肉"和"不爱吃青菜"合掉。
    """
    key = str(scope)
    items = _archive.get(key) or []
    if len(items) < 2:
        return 0
    keep: list[dict] = []
    merged = 0
    ordered = sorted(items, key=lambda x: (float(x.get("w") or MEM_WEIGHT_START), x.get("id", 0)), reverse=True)
    for it in ordered:
        fact = str(it.get("fact") or "")
        if not fact:
            continue
        dup = None
        for k in keep:
            if _similarity(fact, k.get("fact", "")) < threshold:
                continue
            s1 = str(it.get("subject") or "").strip()
            s2 = str(k.get("subject") or "").strip()
            if s1 and s2 and s1 != s2:
                continue  # 不同人/不同主体不合并
            dup = k
            break
        if dup is None:
            keep.append(it)
            continue
        if len(fact) > len(str(dup.get("fact") or "")):
            dup["fact"] = fact
        try:
            w1 = float(dup.get("w") or MEM_WEIGHT_START)
            w2 = float(it.get("w") or MEM_WEIGHT_START)
        except (TypeError, ValueError):
            w1, w2 = MEM_WEIGHT_START, MEM_WEIGHT_START
        dup["w"] = round(min(MEM_WEIGHT_MAX, max(w1, w2) + 0.1), 3)
        dup["h"] = int(dup.get("h") or 0) + int(it.get("h") or 0)
        dup["lu"] = max(float(dup.get("lu") or 0), float(it.get("lu") or 0))
        merged += 1
    if merged:
        keep.sort(key=lambda x: x.get("id", 0))
        _archive[key] = keep
    return merged


# 新事实里出现这些词，说明是在"改口/更新"，跟同主体的旧事实冲突时应该覆盖而不是并存
_UPDATE_CUES = ("现在", "改成", "换了", "换成", "不再", "已经", "改成", "变成", "后来", "现在住",
                "不吃了", "不玩了", "不做了", "没在", "不在", "离职", "搬家", "分手", "和好")


def _conflicts(new_fact: str, old_fact: str) -> bool:
    """粗判两条是不是"同一件事的新旧版本"。"""
    try:
        if not any(c in new_fact for c in _UPDATE_CUES):
            return False
        nb, ob = _bigram_set(new_fact), _bigram_set(old_fact)
        if not nb or not ob:
            return False
        sim = len(nb & ob) / max(1, min(len(nb), len(ob)))
        return sim >= 0.5
    except Exception:  # noqa: BLE001
        return False


def _ts_any(v) -> float:
    """把各种时间表示统一成 epoch 秒。

    pending 里存的是 "10-29 20:12" 这种可读字符串，直接 float() 会炸，
    以前一炸整批合并就失败（日志：could not convert string to float: '10-29 20:12'）。
    """
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    txt = str(v).strip()
    try:
        return float(txt)
    except (TypeError, ValueError):
        pass
    for fmt in ("%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(txt, fmt)
            if "%Y" not in fmt:
                dt = dt.replace(year=datetime.now().year)
            return dt.timestamp()
        except ValueError:
            continue
    return 0.0

def _hormone_snapshot() -> dict:
    """固化时的内分泌快照（0~1）。拿不到就返回空 dict，不阻断写入。"""
    try:
        from plugins import hormones  # type: ignore
        v = hormones.values()
        return {k: round(float(v.get(k) or 0) / 100.0, 2)
                for k in ("DA", "CORT", "SER", "OXY", "MEL")}
    except Exception:  # noqa: BLE001
        return {}


def _match_sources(fact: str, pending: list, limit: int = 3) -> list[dict]:
    """把一条事实对回"待合并的原始消息"，供详情页溯源。

    pending 结构：[[昵称, 文本, 时间戳], ...]
    用 2-gram 重合度打分。

    阈值从 0.3 降到 0.18：模型写 fact 时是改写过的，和原话的 2-gram 重合
    经常在 0.2~0.3 之间，旧阈值导致 204 条里只有 2 条能对上来源（主人反馈
    "来源永远是空的，等于悬案"）。放宽后仍需 ≥0.18 才算来源，不做无依据硬凑。
    """
    fb = _bigrams(fact)
    if not fb:
        return []
    scored: list[tuple[float, list]] = []
    for row in pending or []:
        try:
            name, text, ts = row[0], row[1], row[2]
        except Exception:  # noqa: BLE001
            continue
        tb = _bigrams(str(text or ""))
        if not tb:
            continue
        ratio = len(fb & tb) / max(1, len(fb))
        if ratio >= 0.18:
            scored.append((ratio, [str(name or "群友")[:20], str(text or "")[:80], _ts_any(ts)]))
    scored.sort(key=lambda x: -x[0])
    return [
        {"s": n, "t": t, "conf": round(r, 2),
         "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else ""}
        for r, (n, t, ts) in scored[:limit]
    ]


_REVLOG_MAX = 5


def _push_rev(it: dict, new_fact: str) -> None:
    """记一条修订：保留旧内容 + 时间 + 版本号，供回滚/追溯（借鉴 Source/Artifact 版本链）。"""
    try:
        revlog = list(it.get("revlog") or [])
        revlog.append({
            "rev": int(it.get("rev") or 1),
            "fact": str(it.get("fact") or "")[:200],
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        it["revlog"] = revlog[-_REVLOG_MAX:]
        it["rev"] = int(it.get("rev") or 1) + 1
    except Exception:  # noqa: BLE001
        pass


def history(scope: str, item_id: int) -> list[dict]:
    """某条记忆的修订历史（新的在前）。"""
    for it in (_archive.get(str(scope)) or []):
        if int(it.get("id") or -1) == int(item_id):
            return list(reversed(it.get("revlog") or []))
    return []


def find_item(item_id: int) -> tuple[str, dict] | None:
    """跨 scope 按 id 找一条记忆（详情面板用）。"""
    for sc, items in list(_archive.items()):
        for it in items or []:
            try:
                if int(it.get("id") or -1) == int(item_id):
                    return str(sc), it
            except (TypeError, ValueError):
                continue
    return None


def history_by_id(item_id: int) -> list[dict]:
    hit = find_item(item_id)
    return list(reversed(hit[1].get("revlog") or [])) if hit else []


def rollback_by_id(item_id: int) -> dict | None:
    hit = find_item(item_id)
    return rollback(hit[0], item_id) if hit else None


def rollback(scope: str, item_id: int) -> dict | None:
    """回滚到上一版（回滚本身也记一条修订，可再回滚）。"""
    items = _archive.get(str(scope)) or []
    for it in items:
        if int(it.get("id") or -1) != int(item_id):
            continue
        revlog = list(it.get("revlog") or [])
        if not revlog:
            return None
        prev = revlog.pop()
        _push_rev(it, str(it.get("fact") or ""))   # 把"被撤销的那版"记进修订
        it["fact"] = prev.get("fact") or it.get("fact")
        _save()
        return it
    return None


def _apply_ops(scope: str, ops: list, pending: list | None = None) -> tuple[int, int, int]:
    """执行 ADD/UPDATE/DELETE，返回 (新增, 修改, 删除) 数量。"""
    items = _archive.setdefault(str(scope), [])
    by_id = {it["id"]: it for it in items if isinstance(it.get("id"), int)}
    next_id = max(by_id, default=0) + 1
    a = u = d = 0
    today = datetime.now().strftime("%m-%d")

    for op in ops:
        if not isinstance(op, dict):
            continue
        kind = str(op.get("op", "")).lower().strip()
        subject, fact = _norm_fact(
            str(op.get("subject", "") or "").strip()[:20],
            str(op.get("fact", "") or "").strip()[:80],
        )

        if kind == "add" and _valid_fact(fact):
            # 2026-11-07：先记进潜意识缓冲（RecMem 的观察期）。
            # 这一步不调 LLM、不拦截写入，只统计"什么话题在反复出现"。
            try:
                from plugins import memory_buffer as _mb
                _mb.push(scope, fact, "archive")
            except Exception:  # noqa: BLE001
                pass
            # 2026-11-07：顺手给个"戳心度"初步估值（零成本启发式）。
            # 真正准确的分数由 tools/rate_memory_importance.py 批量补；
            # 这里只是让新条目一进来就有个像样的数，不然全是默认 5。
            _imp_guess = None
            try:
                from plugins import memory_importance as _mi
                _imp_guess = _mi.estimate(fact)
            except Exception:  # noqa: BLE001
                pass
            if any(it.get("fact") == fact for it in items):
                continue  # 完全重复，跳过
            # 同一主体 + 高相似 + 带"现在/不再/改成"这类变更语 → 视为覆盖旧事实
            _replaced = False
            for it in list(items):
                if str(it.get("subject")) != str(subject or "群友"):
                    continue
                if _conflicts(fact, str(it.get("fact") or "")):
                    _push_rev(it, fact)
                    it["fact"] = fact
                    it["time"] = today
                    it["w"] = min(MEM_WEIGHT_MAX, float(it.get("w") or MEM_WEIGHT_START) + 0.2)
                    it["h"] = int(it.get("h") or 0) + 1
                    it["lu"] = _now_ts()
                    _replaced = True
                    u += 1
                    break
            if _replaced:
                continue
            _src = _match_sources(fact, pending or [])
            _kind = str(op.get("kind") or "long").lower().strip()
            if _kind not in ("long", "exp"):
                _kind = "long"
            items.append(
                {
                    "id": next_id,
                    "subject": subject or "群友",
                    "fact": fact,
                    "kind": _kind,
                    "time": today,
                    "w": MEM_WEIGHT_START,
                    "h": 0,
                    "lu": _now_ts(),
                    "src": _src,
                    # 2026-11-05：带时间边界的事自己会过期（借鱼神的 validUntil）
                    "exp": _infer_expiry(fact, _kind),
                    # 内分泌快照：这条记忆固化时她是什么状态（详情页展示）
                    "hormones": _hormone_snapshot(),
                    # 2026-11-07：戳心度初值（启发式），批量脚本会再修正
                    **({"imp": _imp_guess} if _imp_guess else {}),
                }
            )
            by_id[next_id] = items[-1]
            next_id += 1
            a += 1
            # 2026-11-07：攒分量。够阈值才触发反思（Stanford 的做法，
            # 不是每晚准点顿悟，而是"这几件事都挺重"的时候突然想通）
            try:
                from plugins import reflection as _rf
                _rf.accumulate(int(_imp_guess or 5))
            except Exception:  # noqa: BLE001
                pass

        elif kind == "update":
            try:
                target = by_id.get(int(op.get("id", -1)))
            except (TypeError, ValueError):
                target = None
            if target and _valid_fact(fact):
                _push_rev(target, fact)
                target["fact"] = fact
                target["time"] = today
                _src2 = _match_sources(fact, pending or [])
                if _src2:
                    target["src"] = _src2
                u += 1

        elif kind == "delete":
            try:
                tid = int(op.get("id", -1))
            except (TypeError, ValueError):
                continue
            # 2026-11-05：退役而不删除。以前这里直接 items[:] = [...] 把行抹掉，
            # 模型判错一次就永久丢一条有效信息。现在只改可见性，行留在档案里，
            # 可以 restore 回来；真删只由 RETIRED_MAX 的容量上限兜底。
            target = by_id.get(tid)
            if target and not target.get("retired"):
                target["retired"] = True
                target["retired_at"] = today
                d += 1

    return a, u, d


def restore_entry(scope: str, entry_id: int) -> bool:
    """把一条退役的记忆拉回来（只改可见性，不动内容）。

    2026-11-05：「退役而不删除」的配套入口。退役不等于判死刑——
    错误的退役会藏住一条有效信息，所以必须留一条回头的路。
    """
    try:
        for it in (_archive.get(scope) or []):
            if int(it.get("id", -1)) == int(entry_id) and it.get("retired"):
                it.pop("retired", None)
                it.pop("retired_at", None)
                _save()
                return True
    except Exception as e:  # noqa: BLE001
        nonebot.logger.debug(f"[记忆] 恢复 {entry_id} 失败：{e}")
    return False


def retired_entries(scope: str) -> list[dict]:
    """列出某个 scope 下已退役的条目（id / 内容 / 何时退役），供人工审视。

    零模型调用，纯本地读。
    """
    out: list[dict] = []
    try:
        for it in (_archive.get(scope) or []):
            if it.get("retired"):
                out.append({
                    "id": it.get("id"),
                    "subject": it.get("subject"),
                    "fact": it.get("fact"),
                    "retired_at": it.get("retired_at"),
                })
    except Exception:  # noqa: BLE001
        pass
    return out


async def consolidate(scope: str) -> dict:
    """把待合并消息合并进档案（真实的 LLM 调用）。返回本次统计（含 usage / 花费）。

    scope 形如 group_{gid} / private_{uid}。
    """
    key = str(scope)
    if key in _busy:
        return {}
    # 清洗待合并缓冲：脏结构（非 list / 二元以下）直接按空处理，避免解包抛 TypeError
    buf = _clean_pending(_pending.get(key))
    if not buf:
        return {}
    _busy.add(key)
    info: dict = {"scope": key, "input_msgs": len(buf)}
    try:
        # 每轮只喂最近 12 条：避免长批把模型输出撑到 max_tokens 上限导致 JSON 截断
        new_msgs = "\n".join(f"{n}: {t}" for n, t, _ in buf[-12:])
        max_entries, max_chars = _limits(key)
        if key == "global_learn":
            prompt = MERGE_PROMPT_LEARN.format(
                max_chars=max_chars,
                max_entries=max_entries,
                archive=_archive_lines(key),
                new_msgs=new_msgs,
            )
        elif _is_private_scope(key):
            prompt = MERGE_PROMPT_PRIVATE.format(
                max_chars=max_chars,
                archive=_archive_lines(key),
                new_msgs=new_msgs,
            )
        else:
            prompt = MERGE_PROMPT.format(
                max_chars=max_chars,
                archive=_archive_lines(key),
                new_msgs=new_msgs,
            )
        raw, usage = await _call_merge_llm(prompt)
        provider = str(usage.pop("_provider", "ark"))
        data = _extract_json(raw)
        cost = _usage_cost(usage)
        token_meter.record(usage, "memory_merge", cost=cost, provider=provider)

        if data is None:
            # 模型返回非合法 JSON（多为输出被 max_tokens 截断 / 夹带解释文字）：
            # 缓冲截半保留最近一半待下轮重试，旧半批丢弃，避免长批反复失败烧 token；
            # 先落盘保住剩余缓冲。
            half = max(1, len(buf) // 2)
            _pending[key] = buf[-half:]
            _save()
            info.update(
                {"added": 0, "updated": 0, "deleted": 0,
                 "entries": len(_archive.get(key) or []),
                 "usage": usage, "cost": cost, "parse_failed": True,
                 "pending_left": len(_pending[key])}
            )
            nonebot.logger.warning(
                f"[long_memory] {key} 合并返回解析失败，截半保留 {len(_pending[key])} 条待下轮重试；"
                f"本次输入 {usage.get('prompt_tokens')} / 输出 {usage.get('completion_tokens')} tokens"
                f"（约 {cost:.4f} 元）"
            )
        else:
            ops = data.get("ops") or []
            a, u, d = _apply_ops(key, ops, buf[-12:])
            merged = _dedupe_scope(key)
            if merged:
                nonebot.logger.info(f"[long_memory] {key} 去重合并 {merged} 条近似重复事实")
            _profile = str(data.get("profile") or "").strip()
            if _profile:
                save_profile(key, _profile)
            _profile = str(data.get("profile") or "").strip()
            if _profile:
                save_profile(key, _profile)
            _profile = str(data.get("profile") or "").strip()
            if _profile:
                save_profile(key, _profile)
            _profile = str(data.get("profile") or "").strip()
            if _profile:
                save_profile(key, _profile)
            _pending[key] = []
            _save()
            try:
                import asyncio as _aio
                import memory_vec as _mv
                if _mv.available() and _archive.get(key):
                    _aio.get_running_loop().run_in_executor(None, _mv.sync, list(_archive[key]))
            except Exception:  # noqa: BLE001
                pass
            try:
                import asyncio as _aio
                import memory_vec as _mv
                if _mv.available() and _archive.get(key):
                    _aio.get_running_loop().run_in_executor(None, _mv.sync, list(_archive[key]))
            except Exception:  # noqa: BLE001
                pass
            try:
                import asyncio as _aio
                import memory_vec as _mv
                if _mv.available() and _archive.get(key):
                    _aio.get_running_loop().run_in_executor(None, _mv.sync, list(_archive[key]))
            except Exception:  # noqa: BLE001
                pass
            try:
                import asyncio as _aio
                import memory_vec as _mv
                if _mv.available() and _archive.get(key):
                    _aio.get_running_loop().run_in_executor(None, _mv.sync, list(_archive[key]))
            except Exception:  # noqa: BLE001
                pass
            info.update(
                {"added": a, "updated": u, "deleted": d,
                 "entries": len(_archive.get(key) or []),
                 "usage": usage, "cost": cost, "ops_raw": ops}
            )
            nonebot.logger.info(
                f"[long_memory] {key} 合并完成：+{a} 改{u} 删{d}，"
                f"现有 {len(_archive.get(key) or [])} 条；"
                f"本次输入 {usage.get('prompt_tokens')} / 输出 {usage.get('completion_tokens')} tokens"
                f"（约 {cost:.4f} 元）"
            )
    except Exception as e:  # noqa: BLE001
        info["error"] = f"{type(e).__name__}: {e}"
        nonebot.logger.warning(f"[long_memory] {key} 合并失败：{type(e).__name__}: {e}")
    finally:
        _busy.discard(key)
    return info


# ---------------------------------------------------------------- 计价

# 单价统一在 config.py 里读 .env（PRICE_IN / PRICE_OUT / PRICE_CACHE），
# 这里留个别名是因为过去可能有脚本引用过 _usage_cost。
_usage_cost = cfg.usage_cost
