"""联网搜索 + 主动自学。

两条能力线：
1. **聊天按需联网**：私聊 / 群聊里问到实时、外部、时效问题时，走方舟 Responses
   API 的 `web_search` 工具（复用现有 `AI_API_KEY`，不新注册第三方 Key）；
   通道失败自动回退普通 `_chat` 大脑，不影响既有闲聊。
2. **主动定时自学**：定时抓当日热搜（或预设主题清单），逐条联网核实并提炼成
   事实短句，存进全局档案 scope=`global_learn`，再由 system prompt 在私聊 / 群聊
   统一带出；静默入记忆，不主动往群里播报。

配置项见 `config.py` 的 `WebConfig`（.env 里 `WEB_SEARCH_*` / `LEARN_*`）。
"""
from __future__ import annotations

import asyncio
import html
import json
import os
import random
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
import nonebot  # noqa: E402

from config import get_config  # noqa: E402
from plugins import long_memory as ltm  # noqa: E402
import token_meter  # noqa: E402  令牌用量累计（静默容错）

cfg = get_config()
logger = nonebot.logger

# 方舟 Responses 端点（注意：不是 /chat/completions，联网工具只挂在 Responses 上）
RESPONSES_URL = "https://ark.cn-beijing.volces.com/api/v3/responses"

# 新闻时事词：命中即判定需要联网（问今天发生了什么 / 新闻 / 热搜 / 大事这类）。
# 天气、价格、电影、招聘、工资、股价、票房、赛程、门票、政策、地址等非新闻实时问题，
# 必须带强制词（联网 / 搜一下 / 查查 / 查一下 / 百度一下 / 搜搜）才搜，避免闲聊随口一提
# 就误烧搜索额度、拖慢回复。"今天 / 现在 / 最近"这类时间锚点单独出现不触发（如"现在几点"
# "最近忙吗"），只配合上面的新闻词语义使用。
# 注意：平台名（谷歌/百度/知乎/B站/微博/小红书）不列入——指令或闲聊不该触发搜索。
_NEWS_TOPIC_KEYWORDS = [
    "新闻", "热搜", "热点", "时事", "头条", "资讯", "大事",
    "发生了什么", "发生什么", "事件", "爆料", "官宣", "今天发生",
]

# 已有指令分支词：命中直接否决联网（双保险；这些在 ai_group 里本就提前 return 到不了这里）
_COMMAND_PREFIXES = (
    "切换大脑", "切换画笔", "画图", "逮捕", "恶搞", "签到", "唱歌", "桌宠",
    "偷了", "存入表情包", "/status", "/余额", "/help", "/指令", "/帮助",
    "/日报", "决斗", "赛马", "查成分", "贴标签", "做个表情包", "/测试",
)

# 纯打招呼 / 情绪短句，命中即否决联网（避免每条寒暄都烧搜索次数）
_NOISE_PATTERNS = [
    r"^(你好|在吗|在干嘛|吃了吗|晚安|早上好|晚上好|早安|嗨|哈喽|嗯+|哦+|啊+|哈+|[？?！!。~～\s]+)$",
]

# 主动自学防重入：同一时刻只跑一轮，避免定时任务叠在一起烧搜索额度
_learn_busy = asyncio.Lock()


# ------------------------------------------------------------------ 触发判定

def _scope_allowed(is_private: bool) -> bool:
    if not cfg.web.enabled:
        return False
    if cfg.web.scope == "both":
        return True
    if cfg.web.scope == "private":
        return is_private
    return not is_private


_INTERNAL_BG_END = "就够了。"
# withdraw 注入的"被凶前因"标题（新旧两种写法都认）
_INTERNAL_BG_HEADERS = ("## 群里刚才发生了什么", "## 群里刚才的几句")


def _strip_internal_bg(t: str) -> str:
    """剥掉 withdraw 注入的"被凶前因"块，只留用户真说的话。

    2026-11-03 事故：那段前因以「## 群里刚才发生了什么」开头，命中新闻词
    "发生了什么"，整段被当查询词搜出百度百科"主人"词条。
    只剥"整块前言"这一种形态；用户自己问"群里刚才发生了什么"由下面的群内话题否决拦住。
    """
    if not any(h in t for h in _INTERNAL_BG_HEADERS):
        return t
    i = t.find(_INTERNAL_BG_END)
    return t[i + len(_INTERNAL_BG_END):].strip() if i >= 0 else ""


def needs_web_search(text: str, is_private: bool) -> bool:
    """判定这条消息是否需要走联网通道。

    调用方必须先排除：指令分支、图片消息、冷却 / 拦截分支（这些在 ai_group 里
    已经 return 掉，到不了这里）。
    """
    if not _scope_allowed(is_private):
        return False
    if not text or len(text.strip()) < 3:
        return False
    t = _strip_internal_bg(text.strip())
    if not t or len(t) < 3:
        return False

    # 问的是"群里发生了什么"= 群内八卦，该翻聊天记录，不是时事 → 否决联网
    if "群里" in t and re.search(r"发生(了)?什么", t):
        return False

    # 指令类消息直接否决（引擎切换 / 画图 / 游戏 / 语音等已有专门分支处理，不烧搜索次数）
    if any(cmd in t for cmd in _COMMAND_PREFIXES):
        return False

    # 强制指令：含强制词直接联网（去掉指令词后仍要有实质问题才搜）
    for w in cfg.web.force_words:
        if w in t:
            rest = t
            for w2 in cfg.web.force_words:
                rest = rest.replace(w2, "")
            return len(rest.strip()) >= 2

    # 噪声 / 纯情绪短句否决
    for pat in _NOISE_PATTERNS:
        if re.fullmatch(pat, t):
            return False

    # 新闻 / 今日事关键词命中才自动联网；非新闻实时问题走强制词
    return any(k in t for k in _NEWS_TOPIC_KEYWORDS)


# ------------------------------------------------------------------ 联网通道

def _to_input(messages: list) -> tuple[list[dict], bool]:
    """把 OpenAI 风格 messages 转换成 Responses `input` 结构；返回 (blocks, has_image)。

    Responses 通道只收 `input_text`，图片不支持——检测到图片时调用方应回退普通大脑。
    """
    blocks: list[dict] = []
    has_image = False
    for m in messages:
        role = m.get("role")
        if role not in ("system", "user", "assistant"):
            continue
        content = m.get("content")
        if isinstance(content, str):
            blocks.append({"role": role, "content": [{"type": "input_text", "text": content}]})
        elif isinstance(content, list):
            texts: list[str] = []
            for item in content:
                if item.get("type") == "text":
                    texts.append(item.get("text", ""))
                elif item.get("type") == "image_url":
                    has_image = True
            joined = "\n".join(x for x in texts if x)
            if joined:
                blocks.append({"role": role, "content": [{"type": "input_text", "text": joined}]})
    return blocks, has_image


def _domains(items) -> list[str]:
    """从搜索结果里抽域名，作为知识来源。"""
    out = []
    for it in (items or []):
        try:
            href = it[2] if len(it) > 2 else ""
            d = re.sub(r"^https?://", "", str(href)).split("/")[0]
            if d and "." in d and d not in out:
                out.append(d)
        except Exception:  # noqa: BLE001
            continue
    return out[:3]


_SERP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120 Safari/537.36")


def _parse_serp(html_text: str, limit: int = 6) -> list[tuple[str, str, str]]:
    """从通用搜索结果页里抓 标题 + 摘要 + 链接。

    2026-11-05：Bing 改了结构（`b_algo` 匹配不到，实测 h3=0），所以不再写死某一家的
    选择器 —— 按 <h3> 切块，标题取 h3 里的纯文本，摘要取紧随其后的第一个 <p>/<div>。
    搜狗、百度、Bing 都能吃。
    """
    out: list[tuple[str, str, str]] = []
    blocks = re.split(r"(?=<h3)", html_text)
    for b in blocks:
        m = re.search(r"<h3[^>]*>(.*?)</h3>", b, re.S)
        if not m:
            continue
        title = html.unescape(re.sub("<[^>]+>", "", m.group(1))).strip()
        title = re.sub(r"\s+", " ", title)
        href = re.search(r'<a[^>]+href="(https?://[^"]+)"', m.group(1)) or \
            re.search(r'<a[^>]+href="(https?://[^"]+)"', b[m.end():m.end() + 600])
        rest = b[m.end():m.end() + 1500]
        pm = re.search(r"<(?:p|div|span)[^>]*>(.{20,320}?)</(?:p|div|span)>", rest, re.S)
        snippet = html.unescape(re.sub("<[^>]+>", "", pm.group(1))).strip() if pm else ""
        snippet = re.sub(r"\s+", " ", snippet)
        # 标题和摘要都太短的块是导航/噪声，丢掉（实测搜狗会混进「量子纠缠」这种纯标题条）
        if len(title) < 6 and len(snippet) < 20:
            continue
        url = href.group(1) if href else ""
        out.append((title, snippet, url))
        if len(out) >= limit:
            break
    return out


async def _serp(query: str, url: str, params: dict, limit: int,
                name: str) -> list[tuple[str, str, str]]:
    try:
        async with httpx.AsyncClient(timeout=18, follow_redirects=True) as client:
            r = await client.get(url, params=params, headers={"User-Agent": _SERP_UA})
        if r.status_code != 200 or len(r.text) < 2000:
            logger.debug(f"[联网] {name} 无效响应 {r.status_code} len={len(r.text)}")
            return []
        rows = _parse_serp(r.text, limit)
        if not rows:
            logger.debug(f"[联网] {name} 解析不出结果（可能改版/验证码）")
        return rows
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[联网] {name} 失败：{type(e).__name__}: {e}")
        return []


_BRAVE_KEY = os.getenv("BRAVE_SEARCH_KEY", "").strip()
_TAVILY_KEY = os.getenv("TAVILY_API_KEY", "").strip()


async def _brave_results(query: str, limit: int = 6) -> list[tuple[str, str, str]]:
    """Brave Search API（免费 2000 次/月，需 BRAVE_SEARCH_KEY）。"""
    if not _BRAVE_KEY:
        return []
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": limit},
                headers={"Accept": "application/json", "X-Subscription-Token": _BRAVE_KEY},
            )
        if r.status_code != 200:
            logger.warning(f"[联网] Brave {r.status_code}")
            return []
        items = ((r.json() or {}).get("web") or {}).get("results") or []
        return [(str(x.get("title") or ""), str(x.get("description") or ""),
                 str(x.get("url") or "")) for x in items[:limit]
                if x.get("title") or x.get("description")]
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[联网] Brave 失败：{type(e).__name__}: {e}")
        return []


async def _tavily_results(query: str, limit: int = 6) -> list[tuple[str, str, str]]:
    """Tavily Search API（免费 1000 次/月，专为 AI 设计，需 TAVILY_API_KEY）。"""
    if not _TAVILY_KEY:
        return []
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            r = await client.post(
                "https://api.tavily.com/search",
                json={"api_key": _TAVILY_KEY, "query": query,
                      "max_results": limit, "search_depth": "basic"},
            )
        if r.status_code != 200:
            logger.warning(f"[联网] Tavily {r.status_code}")
            return []
        items = (r.json() or {}).get("results") or []
        return [(str(x.get("title") or ""), str(x.get("content") or ""),
                 str(x.get("url") or "")) for x in items[:limit]]
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[联网] Tavily 失败：{type(e).__name__}: {e}")
        return []


async def _free_web_results(query: str, limit: int = 6) -> list[tuple[str, str, str]]:
    """多源搜索：有 key 的 API 优先，其余走结果页抓取，谁先出结果用谁。

    2026-11-05 改多源：原来只抓 Bing 的 `b_algo`，Bing 一改版就全线空
    （实测 h3=0，好奇心学习学了空）。现在 Brave/Tavily（有 key 时）→ Bing →
    搜狗 → 百度，逐个回退，任一成功即返回。
    """
    chain = [
        ("Brave", _brave_results),
        ("Tavily", _tavily_results),
        ("Bing", lambda q, n: _serp(q, "https://www.bing.com/search", {"q": q}, n, "Bing")),
        ("搜狗", lambda q, n: _serp(q, "https://www.sogou.com/web", {"query": q}, n, "搜狗")),
        ("百度", lambda q, n: _serp(q, "https://www.baidu.com/s", {"wd": q}, n, "百度")),
    ]
    for name, fn in chain:
        try:
            rows = await fn(query, limit)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[联网] {name} 异常：{type(e).__name__}")
            continue
        if rows:
            logger.info(f"[联网] {name} 拿到 {len(rows)} 条（{query[:14]}）")
            return rows
    logger.warning(f"[联网] 所有免费源都没结果：{query[:20]}")
    return []


async def _free_web_snippets(query: str, limit: int = 6) -> list[tuple[str, str]]:
    results = await _free_web_results(query, limit)
    return [(t, s) for t, s, _ in results]


# ---------------------------------------------------------------- 好奇心驱动学习
# 2026-11-05（主人选的 ②）：以前只有「定时抓热搜」才会学，聊天里遇到不懂的词
# 她不会主动去查。这里补一条：私聊里出现明确的"这是什么"类提问 →
# 联网查一次 → 摘要直接注入本轮（她能用上新信息回话）+ 提炼一句存进知识库。
_CURIOSITY_RE = re.compile(
    r"(?:什么是|啥是|是啥|是谁|是什么|干嘛的|干什么的|怎么用|怎么理解|为什么|为啥|哪年|多少钱|什么时候|啥时候)")
_CURIOSITY_SKIP = ("这个", "那个", "这些", "那些", "东西", "回事", "意思", "情况", "问题", "样子")

_CURIOSITY_TS: list[float] = []      # 最近触发时间（滑动窗口限速）
_CURIOSITY_MAX_PER_HOUR = 3


def _curiosity_entity(text: str) -> str:
    """从问句里粗抽一个实体词；抽不准就返回空。"""
    t = _CURIOSITY_RE.sub("", text or "")
    t = re.sub(r"[？?！!。，,、\s「」《》（）()]+", "", t)
    t = re.sub(r"(本鱼|请问|帮我|告诉我|一下|知道|说说|讲讲|你|我|吗|呢|啊)", "", t)
    t = t.strip()
    if len(t) < 2 or len(t) > 16:
        return ""
    # 2026-11-05 修：原来把「就来问安装没」这种整句当实体去搜，搜回来一堆垃圾
    # （"查看此网页的中文翻译"），还把它当上下文喂给模型。
    # 实体里不该出现虚词/动词 —— 有就说明抽错了。
    if re.search(r"(吗|呢|吧|了|的|是|在|有|没|不|就|都|也|还|要|会|能|说|问|装|弄|搞|做|来|去|给|把|被|让|咋|啥|怎么|什么)", t):
        return ""
    # 指向不明的词（这个/那个/东西/意思…）搜了也没用
    if any(k in t for k in ("这个", "那个", "这些", "那些", "东西", "回事", "意思", "情况", "样子")):
        return ""
    return t


def needs_curiosity(text: str) -> str:
    """要不要为了这句话主动去学？返回实体词（空串 = 不学）。"""
    now = time.time()
    _CURIOSITY_TS[:] = [t for t in _CURIOSITY_TS if now - t < 3600]
    if len(_CURIOSITY_TS) >= _CURIOSITY_MAX_PER_HOUR:
        return ""
    ent = _curiosity_entity(text)
    if not ent:
        return ""
    # 已经学过就不再查（同一实体不重复烧搜索）
    try:
        from plugins import long_memory as _ltm
        for it in (_ltm._archive.get("global_learn") or []):
            if ent in str(it.get("fact") or ""):
                return ""
    except Exception:  # noqa: BLE001
        pass
    return ent


# ================================================================
# 2026-11-06：搜索词改造。
# 以前是拿念头里的口语原句直接当查询词 ——
#   「发发色图」   → 被搜成"头发 颜色"，出来一堆素材站
#   「方便从后面」 → 搜出色情段落
#   「那描述下」   → 搜出一堆无关页面
# 现在先判断"这句到底是不是想搞懂什么"，再把它提炼成搜索主题。
# 性/情欲话题不搜原句，走固定的中性主题（技巧、沟通、事后），
# 搜出来是两性科普而不是色情站。
# ================================================================
_SEX_HINT = re.compile(
    r"色|性爱|性交|做爱|情欲|前戏|后入|体位|高潮|插|舔|摸|亲|脱|裸"
    r"|发情|想要|硬|湿|射|口交|乳|腿间|床",
    re.I,
)
# 要图 / 要资源 / 指代不明 —— 不是"学知识"，直接不搜
_NOT_A_QUERY = re.compile(
    r"^(发|给|来|看看?|下载|找).{0,4}(图|片|视频|资源|链接)"
    r"|(发|给|来).{0,3}(色图|涩图|黄图)"
    r"|^(那|这个|那个|这样|那样)|^\.\.\.",
    re.I,
)

# 性相关不搜原句，从这些中性主题里挑 —— 搜出来是科普/技巧/沟通类内容
# 2026-11-06 从 5 条扩到 16 条：分「技巧 / 沟通 / 同意 / 安全 / 关系 / 常见困扰」六组，
# 每组都配有触发意图，见 _sex_topic_of()。
_SEX_TOPIC_BANK = [
    # 技巧
    "性爱技巧 前戏方法",
    "两性关系 节奏配合 身体接触",
    "如何增加亲密感 日常触碰",
    # 沟通
    "亲密时怎么沟通 性需求表达",
    "性爱后怎么聊天 事后温存",
    "怎么跟伴侣说自己的喜好 沟通方式",
    # 同意与边界
    "如何询问对方意愿 性同意边界",
    "怎么拒绝而不伤感情 亲密关系",
    "亲密关系里的边界感 怎么建立",
    # 安全
    "安全措施 避孕方式 科普",
    "性健康 常见误区 科普",
    # 关系
    "长期关系 亲密感变淡 怎么办",
    "异地恋 维持亲密 方法",
    "第一次 注意事项 心理准备",
    # 常见困扰
    "性欲差异 伴侣怎么办",
    "紧张焦虑 亲密关系 缓解方法",
]

# 每个知识域：命中关键词 → 套哪种查询句式。
# 2026-11-06 加：以前所有非情欲话题都丢给模型自由发挥，问"X是什么"和问"X怎么做"
# 出来的搜索词质量参差。这里给每类一个句式模板，模型只在没有模板时兜底。
# 顺序有讲究：具体域在前，"名词/做法"这类通用句式放最后，
# 否则「这季番结局是啥」会先被「是啥」抓走，套成光秃秃的作品名。
_DOMAIN_RULES: list[tuple[str, "re.Pattern[str]", str]] = [
    # (域名, 命中正则, 查询模板；{t} 是剥掉口语后的主体)
    ("时事", re.compile(r"新闻|热搜|最新|发生|怎么样?了|进展|宣布|发布|开会|会谈|当选"), "{t} 最新进展"),
    ("技术", re.compile(r"代码|程序|安装|配置|报错|接口|api|脚本|部署|插件|模型|显卡|显存", re.I), "{t} 解决方法"),
    ("娱乐", re.compile(r"番|剧|动画|漫画|小说|电影|游戏|角色|剧情|结局|第.集|更新"), "{t} 剧情 评价"),
    ("健康", re.compile(r"疼|痛|失眠|发烧|感冒|头晕|过敏|医院|吃药|症状|体检|调理"), "{t} 原因 怎么办"),
    ("理论", re.compile(r"理论|主义|学派|哲学|心理|效应|定律|概念"), "{t} 通俗解释"),
    ("原因", re.compile(r"为什么|为啥|怎么会|原因"), "{t} 原因"),
    ("做法", re.compile(r"(怎么|如何|咋).{0,4}(做|弄|搞|用|学|办|解决)"), "{t} 方法步骤"),
    ("名词", re.compile(r"(是什么|什么意思|啥意思|是啥|指什么)"), "{t}"),
]

# 剥掉这些口语尾巴，剩下的才是主体
_TAIL_RE = re.compile(
    r"(吗|呢|吧|啊|呀|哦|嘛|哈|来着|的话|怎么办|咋办|怎么说|怎么弄|是什么|什么意思|"
    r"怎么|如何|咋|为什么|为啥|能不能|可不可以|有没有|是不是)$"
)

_TOPIC_PROMPT = (
    "把下面这句话变成一个适合中文搜索引擎的查询词，6~16 个字，只输出查询本身。\n"
    "规则：\n"
    "1. 只取其中真正想搞懂的「主题」，不要照抄口语，不要带「本鱼/主人/咋/嘛/呢」这类词。\n"
    "2. 不要输出解释、不要加引号、不要加书名号。\n"
    "3. 如果这句话其实不是想搞懂什么（只是抱怨、撒娇、要东西、闲聊、重复语气），输出 NONE。\n"
    "句子：{t}\n查询："
)


def _strip_tail(t: str) -> str:
    """剥掉口语尾巴，留下主体。"""
    x = str(t or "").strip()
    for _ in range(3):
        y = _TAIL_RE.sub("", x).strip()
        if y == x:
            break
        x = y
    return x


def _sex_topic_of(t: str) -> str:
    """情欲类念头 → 中性搜索主题（按意图挑，不随机乱配）。"""
    if re.search(r"聊|说话|话题|尴尬|气氛|说什么|讲什么", t):
        if re.search(r"事后|之后|完了|结束|完事|第二天", t):
            return "性爱后怎么聊天 事后温存"
        return "亲密时怎么沟通 性需求表达"
    if re.search(r"拒绝|不想|不要|停|不舒服|边界|同意|意愿", t):
        if re.search(r"拒绝|不想|不要|停", t):
            return "怎么拒绝而不伤感情 亲密关系"
        return "如何询问对方意愿 性同意边界"
    if re.search(r"安全|怀孕|避孕|套|措施|健康|洗", t):
        return "安全措施 避孕方式 科普"
    if re.search(r"第一次|紧张|害怕|焦虑|不知道怎么办", t):
        return "第一次 注意事项 心理准备"
    if re.search(r"久了|变淡|没兴趣|冷淡|异地|维持", t):
        return "长期关系 亲密感变淡 怎么办"
    if re.search(r"想要|表达|说出口|喜好|喜欢什么", t):
        return "怎么跟伴侣说自己的喜好 沟通方式"
    if re.search(r"前戏|技巧|怎么弄|体位|节奏|持久|舒服", t):
        return "性爱技巧 前戏方法"
    return random.choice(_SEX_TOPIC_BANK)


async def _topic_query(thought: str) -> str:
    """把念头变成能搜到知识的查询词。返回空串表示"这不是查资料的念头"。"""
    t = str(thought or "").strip()
    if len(t) < 2:
        return ""
    if _NOT_A_QUERY.search(t):
        logger.info(f"[好奇] 「{t[:18]}」是要东西、不是查资料，跳过")
        return ""
    if _SEX_HINT.search(t):
        q = _sex_topic_of(t)
        logger.info(f"[好奇] 情欲类念头「{t[:16]}」→ 不搜原句，改搜主题「{q}」")
        return q
    # 非情欲：先看能不能套现成的知识域模板，模型只在没有模板时兜底
    _core = _strip_tail(t)
    for _name, _pat, _tpl in _DOMAIN_RULES:
        if _pat.search(t) and len(_core) >= 2:
            q = _tpl.format(t=_core)[:24]
            logger.info(f"[好奇] 「{t[:16]}」命中【{_name}】域 → 查询「{q}」")
            return q
    try:
        from plugins.ai_group import call_ai_raw
        raw = await call_ai_raw(_TOPIC_PROMPT.format(t=t[:60]), "deepseek",
                                temperature=0.2, max_tokens=40)
        q = re.sub(r"[「」\"'“”‘’【】《》。，,：:；;\n]", "", str(raw or "")).strip()
        q = re.sub(r"^(查询|答案|输出)[:：]?", "", q).strip()
        if q and q.upper() != "NONE" and q != "无" and 2 <= len(q) <= 20:
            logger.info(f"[好奇] 念头「{t[:16]}」→ 提炼查询「{q}」")
            return q
        if q.upper() == "NONE" or q == "无":
            logger.info(f"[好奇] 「{t[:18]}」不是查资料的念头，跳过")
            return ""
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[好奇] 提炼搜索词失败：{type(e).__name__}")
    # 回退：剥掉口语尾巴再搜
    _fb = _strip_tail(t)
    return _fb if len(_fb) >= 2 else ""


_DIGEST_PROMPT = (
    "你刚查到这些资料：\n{body}\n\n"
    "用你自己的话写一句「我学到的」，20~45 字，像突然想通了在嘟囔：\n"
    "- 不要念来源，不要说「根据搜索」，不要照抄原句\n"
    "- 说人话，可以带一点自己的口气，但别加表情符号\n"
    "- 如果这些资料其实什么都没讲清楚，就输出 NONE\n"
    "只输出这一句。"
)


async def _digest_learned(query: str, lines: list[str]) -> str:
    """把搜来的两三条摘要嚼成一句话（她自己的说法）。失败返回空串。"""
    if not lines:
        return ""
    try:
        from plugins.ai_group import call_ai_raw
        body = "\n".join("- " + x for x in lines[:3])
        raw = await call_ai_raw(_DIGEST_PROMPT.format(body=body), "deepseek",
                                temperature=0.5, max_tokens=90)
        txt = re.sub(r"[\n\r]+", " ", str(raw or "")).strip()
        txt = re.sub(r"^[「\"'“”]+|[」\"'“”]+$", "", txt).strip()
        txt = re.sub(r"^(我学到的|学到的|总结)[:：]?", "", txt).strip()
        if not txt or txt.upper() == "NONE" or txt == "无":
            return ""
        # 太长截断；太短说明没嚼出东西
        return txt[:60] if len(txt) >= 8 else ""
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[好奇] 总结失败，退回原文：{type(e).__name__}")
        return ""


async def curiosity_learn(entity: str) -> str:
    """查一次、提炼成事实存库；返回可直接拼进 prompt 的上下文（失败返回空串）。

    传入的是「念头原话」，不是搜索词 —— 这里先把它变成真正的查询（见 _topic_query）。
    """
    if not entity:
        return ""
    query = await _topic_query(entity)
    if not query:
        return ""
    _CURIOSITY_TS.append(time.time())
    try:
        rows = await _free_web_results(query, limit=4)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[好奇] 搜索失败：{type(e).__name__}: {e}")
        return ""
    if not rows:
        logger.info(f"[好奇] {query} 没搜到东西")
        return ""
    rows = sorted(rows, key=lambda r: len(r[1] or ""), reverse=True)
    lines = []
    for title, snippet, _url in rows[:3]:
        piece = (snippet or title or "").strip()
        # 去掉 Bing 摘要自带的日期前缀（「2017年6月16日 · 」这种），念出来很怪
        piece = re.sub(r"^\d{4}年\d{1,2}月\d{1,2}日\s*[·・.]\s*", "", piece)
        if len(piece) < 12:
            continue
        lines.append(piece[:110])
        if len(lines) >= 2:
            break
    if not lines:
        return ""
    logger.info(f"[好奇] {query} → 学到 {len(lines)} 条")
    try:
        from plugins import long_memory as _ltm
        # 2026-11-06：不再把搜索摘要原样塞进库 —— 先让她用自己的话嚼一遍。
        # 存的是「她学到的」，原文摘要留作 evidence 备查。
        _raw_body = "；".join(lines)[:200]
        _digest = await _digest_learned(query, lines)
        if _digest and _ltm._valid_fact(_digest):
            _ltm.add_learned(f"{query}：{_digest}"[:120],
                             source=(rows[0][2] or "bing"), kind="knowledge",
                             evidence=_raw_body)
            logger.info(f"[好奇] {query} → 自己总结成：{_digest[:40]}")
        else:
            # 总结失败就退回原文（至少不丢）
            for piece in lines:
                _ltm.add_learned(f"{query}：{piece}"[:120],
                                 source=(rows[0][2] or "bing"), kind="knowledge",
                                 evidence=_raw_body)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[好奇] 入库失败：{type(e).__name__}")
    body = "\n".join("- " + x for x in lines)
    return ("【刚查到】（你刚才说到「" + query + "」，这是现查的，可以自然用上，"
            "别念来源列表、别说「根据搜索」）：\n" + body)


async def web_chat(messages: list) -> tuple[str, dict] | None:
    """方舟 Responses + web_search。

    成功返回 `(reply_text, meta)`；失败 / 空回答 / 含图片返回 `None`，由调用方
    回退普通 `_chat` 大脑。`meta` 里带搜索次数、查询词、usage 供日志排查。
    """
    blocks, has_image = _to_input(messages)
    if has_image or not blocks:
        return None

    # 优先 Google Gemini 免费额度 + 搜索 grounding
    if os.getenv("GEMINI_SEARCH_ENABLED", "false").lower() in ("1", "true", "yes"):
        query = ""
        for m in reversed(messages):
            if str(m.get("role")) != "user":
                continue
            content = m.get("content")
            if isinstance(content, str):
                query = content
            elif isinstance(content, list):
                query = "".join(str(p.get("text") or "") for p in content if isinstance(p, dict))
            if query.strip():
                break
        if query.strip() and "【搜索词】" in query:
            # 学习/长提示词里显式给搜索词，避免把整段说明丢给搜索引擎
            query = query.split("【搜索词】", 1)[1].splitlines()[0].strip()[:80]
        elif query.strip() and len(query) > 80:
            # 没给搜索词的长提示词：只拿第一行，别把整段说明丢给搜索引擎
            _first = query.splitlines()[0].strip()
            if len(_first) >= 4:
                query = _first[:80]
        if query.strip():
            try:
                from plugins import gemini
                items = await _free_web_snippets(query)
                if items:
                    material = "\n".join(
                        f"{i + 1}. {t}：{s}" for i, (t, s) in enumerate(items)
                    )
                    reply = await gemini.gemini_text(
                        "根据下面的搜索结果，用简洁中文回答问题，末尾附来源标题。\n"
                        f"问题：{query[:300]}\n搜索结果：\n{material[:2500]}"
                    )
                    if reply:
                        logger.info(f"[联网] 免费搜索+Gemini：{query[:40]}")
                        return reply, {"web_search": 1, "results": len(items),
                                       "queries": [query[:80]], "sources": _domains(locals().get("items") or []), "provider": "gemini"}
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[联网] Gemini 搜索失败，改用 DeepSeek：{e}")
            # Gemini 不通时用 DeepSeek 总结同样的 Bing 结果，不再回退火山
            try:
                items = locals().get("items") or []
                if items:
                    material = "\n".join(f"{i + 1}. {t}：{s}" for i, (t, s) in enumerate(items))
                    prompt = (
                        "根据下面的搜索结果，用简洁中文回答问题，末尾附来源标题。\n"
                        f"问题：{query[:300]}\n搜索结果：\n{material[:2500]}"
                    )
                    from plugins.ai_group import call_ai_raw
                    reply = await call_ai_raw(prompt, "deepseek")
                    if reply:
                        logger.info(f"[联网] 免费搜索+DeepSeek：{query[:40]}")
                        return reply, {"web_search": 1, "results": len(items),
                                       "queries": [query[:80]], "sources": _domains(locals().get("items") or []), "provider": "deepseek"}
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[联网] DeepSeek 总结失败：{e}")
            return None

    return None  # 不再回退火山搜索

    body = {
        "model": cfg.ai.model,
        "tools": [{"type": "web_search", "max_keyword": cfg.web.max_keywords}],
        "input": blocks,
    }

    try:
        async with httpx.AsyncClient(timeout=90) as client:
            r = await client.post(
                RESPONSES_URL,
                headers={
                    "Authorization": f"Bearer {cfg.ai.api_key}",
                    "content-type": "application/json",
                },
                json=body,
            )
        if r.status_code != 200:
            logger.warning(f"[联网] Responses HTTP {r.status_code}: {r.text[:200]}")
            return None
        data = r.json()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[联网] Responses 异常：{type(e).__name__}: {e}")
        return None

    reply_parts: list[str] = []
    search_queries: list[str] = []
    result_count = 0
    for block in data.get("output", []):
        btype = block.get("type")
        if btype == "message":
            for c in block.get("content", []):
                if c.get("type") in ("output_text", "text"):
                    reply_parts.append(c.get("text", ""))
        elif btype == "web_search_call":
            action = block.get("action") or {}
            if action.get("query"):
                search_queries.append(str(action["query"]))
            result_count += len(block.get("results") or [])
        # reasoning / reasoning_summary 块跳过，不进最终回复

    reply = "\n".join(x for x in reply_parts if x).strip()
    if not reply:
        logger.warning("[联网] Responses 返回空回答，回退普通大脑")
        return None

    usage = data.get("usage", {}) or {}
    token_meter.record(usage, "web_search")
    meta = {
        "web_search": len(search_queries),
        "results": result_count,
        "queries": search_queries,
        "usage": usage,
    }
    return reply, meta


# ------------------------------------------------------------------ 主动自学

async def fetch_hotlist(limit: int = 10) -> list[str]:
    """抓百度实时热搜标题（本机实测可达；微博 403 / 知乎 401 / 头条 404，不纳入）。

    失败返回空列表，由调用方回退预设主题清单。
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    }
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get(
                "https://top.baidu.com/api/board?platform=wise&tab=realtime", headers=headers
            )
        if r.status_code != 200:
            logger.warning(f"[自学] 百度热搜 HTTP {r.status_code}")
            return []
        data = r.json()
        cards = data.get("data", {}).get("cards") or []
        if not cards:
            return []
        inner: list = cards[0].get("content") or []
        # component=tabTextList 时 content 是嵌套列表：[{content: [条目...]}]
        if inner and isinstance(inner[0], dict) and "content" in inner[0]:
            inner = inner[0]["content"]
        titles = [str(x.get("word") or x.get("query") or "").strip() for x in inner]
        return [t for t in titles if t][:limit]
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[自学] 百度热搜抓取失败：{type(e).__name__}: {e}")
        return []


def _load_topics_file(limit: int) -> list[str]:
    if not cfg.web.topics_file.exists():
        return []
    topics = [
        ln.strip()
        for ln in cfg.web.topics_file.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    return topics[:limit]


# ------------------------------------------------------------------ AI 活人感主题轮换状态

LEARN_STATE_FILE = ROOT / ".learn_state.json"


def _load_learn_state() -> dict:
    try:
        data = json.loads(LEARN_STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_learn_state(state: dict) -> None:
    try:
        LEARN_STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        logger.warning(f"[自学] 轮换状态写入失败：{exc}")


def _next_ai_topics(n: int) -> list[str]:
    """按轮次环形取 AI 活人感主题，避免每轮都从第一条开始。"""
    topics = list(cfg.web.learn_ai_topics)
    if not topics or n <= 0:
        return []
    state = _load_learn_state()
    idx = int(state.get("ai_topic_index", 0)) % len(topics)
    picked = [topics[(idx + i) % len(topics)] for i in range(n)]
    state["ai_topic_index"] = (idx + n) % len(topics)
    _save_learn_state(state)
    return picked


LEARN_CAP_PER_DAY = int(os.getenv("LEARN_DAILY_CAP", "16") or "16")


def _learn_today() -> tuple[str, int]:
    st = _load_learn_state()
    day = time.strftime("%Y-%m-%d")
    if st.get("day") != day:
        return day, 0
    return day, int(st.get("count") or 0)


def _learn_count() -> None:
    st = _load_learn_state()
    day = time.strftime("%Y-%m-%d")
    st["day"] = day
    st["count"] = int(st.get("count") or 0) + 1
    _save_learn_state(st)


async def daily_learn() -> dict:
    """定时主动自学：热搜 / 主题 → 联网核实 → 事实短句入 `global_learn` 档案；
    额外一路 AI 活人感主题，联网检索知乎 / 全网公开讨论，沉淀进同一档案。

    静默执行，不主动往群里 / 私聊播报；学完由 system prompt 在后续聊天自然带出。
    """
    stats: dict = {
        "learned_hot": 0,
        "learned_ai": 0,
        "skipped": 0,
        "queries_hot": [],
        "queries_ai": [],
        "errors": [],
    }
    if not cfg.web.learn_enabled:
        return stats
    _day, _done = _learn_today()
    if _done >= LEARN_CAP_PER_DAY:
        logger.info(f"[自学] 今日已学 {_done} 条，达到上限 {LEARN_CAP_PER_DAY}，跳过本轮")
        return stats

    async with _learn_busy:
        # ---------- 第一路：当日热搜 / 预设清单 ----------
        if cfg.web.learn_source == "hot":
            topics = await fetch_hotlist(cfg.web.learn_items * 2)
            topics = topics[:cfg.web.learn_items]
            if not topics:  # 热搜失败回退预设清单，保证这一轮不白跑
                topics = _load_topics_file(cfg.web.learn_items)
        else:
            topics = _load_topics_file(cfg.web.learn_items)

        for topic in topics:
            try:
                prompt = (
                    f"热搜话题：{topic}\n"
                    "请联网核实后，用 1~2 句大白话讲清这件事的核心事实（主体、时间、进展 / 结果）。"
                    "只给事实：不要评论、不要新闻腔调、不要口癖、不要链接、不要分点列表，60 字以内。"
                    f"\n【搜索词】{topic}"
                )
                res = await web_chat([{"role": "user", "content": prompt}])
                if not res:
                    stats["skipped"] += 1
                    continue
                fact, _meta = res
                fact = fact.strip().replace("\n", " ")
                if len(fact) < 6:
                    stats["skipped"] += 1
                    continue
                _meta2 = _meta or {}
                _src = (_meta2.get("sources") or [""])[0]
                _r = ltm.add_learned(f"{topic}：{fact}"[:120], source=_src, kind="news")
                if _r.get("added") or _r.get("replaced"):
                    stats["learned_hot"] += 1
                    _learn_count()
                stats["queries_hot"].append(topic)
            except Exception as e:  # noqa: BLE001
                stats["errors"].append(f"{topic}: {type(e).__name__}: {e}"[:160])
                logger.warning(f"[自学] 热搜主题「{topic}」提炼失败：{type(e).__name__}: {e}")

        # ---------- 第二路：AI 活人感主题（联网检索知乎 / 全网公开讨论） ----------
        if cfg.web.learn_ai_enabled:
            ai_topics = _next_ai_topics(cfg.web.learn_ai_items)
            for topic in ai_topics:
                try:
                    prompt = (
                        f"最近在知乎、B 站、技术博客等公开社区讨论的方向：{topic}\n"
                        "请联网检索最近的相关讨论，用 1~2 句大白话总结当前大家在追求哪些具体做法 / 新方向"
                        "（语气、情绪、记忆、反应节奏、人设关系等任选相关的讲）。"
                        "只总结公开讨论里出现的事实和做法：不要评论站队、不要推荐具体产品、不要新闻腔调、"
                        "不要链接、不要分点列表，60 字以内。"
                        f"\n【搜索词】{topic} 讨论"
                    )
                    res = await web_chat([{"role": "user", "content": prompt}])
                    if not res:
                        stats["skipped"] += 1
                        continue
                    fact, _meta = res
                    fact = fact.strip().replace("\n", " ")
                    if len(fact) < 6:
                        stats["skipped"] += 1
                        continue
                    _meta3 = _meta or {}
                    _r3 = ltm.add_learned(f"{topic}：{fact}"[:120],
                                          source=(_meta3.get("sources") or [""])[0], kind="knowledge")
                    if _r3.get("added") or _r3.get("replaced"):
                        _learn_count()
                        stats["learned_ai"] += 1
                    stats["queries_ai"].append(topic)
                except Exception as e:  # noqa: BLE001
                    stats["errors"].append(f"{topic}: {type(e).__name__}: {e}"[:160])
                    logger.warning(f"[自学] AI活人感主题「{topic}」提炼失败：{type(e).__name__}: {e}")

        if not stats["queries_hot"] and not stats["queries_ai"]:
            logger.warning("[自学] 本轮两路都没有可学的主题")
            return stats

        # 这一轮攒完直接合并进档案（global_learn 独立 scope，不占群消息合并队列）
        try:
            await ltm.consolidate("global_learn")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[自学] global_learn 合并失败：{type(e).__name__}: {e}")

    logger.info(
        f"[自学] 本轮完成：热搜 {stats['learned_hot']} 条，AI活人感 {stats['learned_ai']} 条，"
        f"跳过 {stats['skipped']}；热搜主题={stats['queries_hot']}；活人感主题={stats['queries_ai']}"
    )
    return stats
