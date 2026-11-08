"""统一配置中心 + 启动自检。

为什么有这个文件
----------------
以前参数散在四个地方（`bot.py` 的端口、`ai_group.py` 的模型三件套和上下文参数、
`long_memory.py` 的记忆参数、`schedule.py` 的群号和课表），改一个值要先想半天它在哪，
写错了也只有跑到那一步才炸。现在统一到这里：

- **来源**：`.env`（凭据和旋钮）+ 仓库根目录的 JSON/Markdown 数据文件（人设、课表、人设红线）
- **校验**：类型不对或缺字段立刻报错，报错信息指明「哪个文件哪一项」
- **自检**：`python check.py` 能在**不起机器人**的情况下把环境查一遍

用法
----
```python
from config import get_config, redact
cfg = get_config()
cfg.ai.api_key          # 模型凭据
cfg.context.history_use # 上下文窗口
cfg.persona.prompt      # 人设 prompt
cfg.schedule.timetable  # 课表
```
改配置后**必须重启后端**；人设 / 红线 / 课表这三类数据文件 checkout 后同样要重启。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

try:  # tools/ 下的脚本不依赖 dotenv 也能跑出可读报错
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None  # type: ignore

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
PERSONA_FILE = ROOT / "persona.md"
PERSONA_RULES_FILE = ROOT / "persona_rules.json"
TIMETABLE_FILE = ROOT / "timetable.json"
EMOJI_DIR = ROOT / "表情包"
MEMORY_DIR = ROOT / "memory"
LOG_DIR = ROOT / "logs"

MOODS = ("开心", "无语", "生气", "卖萌", "得意", "委屈", "震惊")
IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


class ConfigError(RuntimeError):
    """配置读不出来或写错时抛出，说明里带着「去哪儿改」"""


# ------------------------------------------------------------------ 取值helper

def _raw(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or "").strip()


def _required_str(name: str) -> str:
    v = _raw(name)
    return v


def _as_int(name: str, default: int, lo: int | None = None, hi: int | None = None) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        v = int(raw.strip())
    except ValueError as e:
        raise ConfigError(f"环境变量 {name} 不是整数：{raw!r}（改 .env）") from e
    if lo is not None and v < lo:
        raise ConfigError(f"环境变量 {name}={v} 小于允许的下限 {lo}（改 .env）")
    if hi is not None and v > hi:
        raise ConfigError(f"环境变量 {name}={v} 大于允许的上限 {hi}（改 .env）")
    return v


def _as_float(name: str, default: float, lo: float | None = None, hi: float | None = None) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        v = float(raw.strip())
    except ValueError as e:
        raise ConfigError(f"环境变量 {name} 不是数字：{raw!r}（改 .env）") from e
    if lo is not None and v < lo:
        raise ConfigError(f"环境变量 {name}={v} 小于允许的下限 {lo}（改 .env）")
    if hi is not None and v > hi:
        raise ConfigError(f"环境变量 {name}={v} 大于允许的上限 {hi}（改 .env）")
    return v


def _as_bool(name: str, default: bool) -> bool:
    raw = _raw(name).lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on", "y"):
        return True
    if raw in ("0", "false", "no", "off", "n"):
        return False
    raise ConfigError(f"环境变量 {name}={raw!r} 不是布尔值（写 true/false，改 .env）")


def _as_group_set(name: str) -> set[int]:
    raw = _raw(name)
    return {int(x) for x in re.split(r"[,\s]+", raw) if x} if raw else set()


def _load_json(path: Path, what: str) -> dict:
    if not path.exists():
        raise ConfigError(f"缺少{what}文件：{path.name}（从原文件改回来，或新建一份）")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path.name} 不是合法 JSON：第 {e.lineno} 行 {e.msg}") from e


# ------------------------------------------------------------------ 配置块

@dataclass(frozen=True)
class AIConfig:
    api_key: str
    base_url: str
    model: str
    temperature: float
    timeout: float
    max_retries: int
    price_in: float
    price_out: float
    price_cache: float

    def endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"


@dataclass(frozen=True)
class ContextConfig:
    history_max: int
    history_use: int
    max_len_per_msg: int
    at_window: int


@dataclass(frozen=True)
class MemoryConfig:
    consolidate_every: int
    archive_max_chars: int
    max_use_chars: int
    pending_keep: int


@dataclass(frozen=True)
class LexiconConfig:
    """群聊词条库（黑话 / 昵称 / 梗 / 缩写 / 专属名词）。

    enabled             词库总开关；
    group_learn_enabled 非 @ 群聊消息是否也进学习缓冲（档案合并 + 词条攒批）；
    batch_size          攒多少条群聊消息触发一次 LLM 抽词；
    max_terms_per_group 每个群词条上限，超出按频次淘汰最旧低频；
    top_in_prompt       拼进群聊 prompt 的高频词条条数；
    extract_ai_enabled  是否调 LLM 抽取新词条与含义；false 时只对已有词条计数；
    max_meaning_chars   词条含义字数硬上限。
    """

    enabled: bool
    group_learn_enabled: bool
    batch_size: int
    max_terms_per_group: int
    top_in_prompt: int
    extract_ai_enabled: bool
    max_meaning_chars: int


@dataclass(frozen=True)
class PersonaConfig:
    nickname: str
    prompt: str
    prompt_file: Path
    rules: dict
    rules_file: Path


@dataclass(frozen=True)
class EmojiConfig:
    dir: Path
    moods: tuple[str, ...]
    exts: set[str]

    def count(self, mood: str | None = None) -> int:
        folder = self.dir if mood is None else self.dir / mood
        if not folder.is_dir():
            return 0
        return sum(1 for f in folder.iterdir() if f.suffix.lower() in self.exts and f.is_file())

    def count_all(self) -> int:
        """各情绪子文件夹里的总数（根目录散着的图不算进去）。"""
        return sum(self.count(m) for m in self.moods)


@dataclass(frozen=True)
class ScheduleConfig:
    target_group: int
    remind_before: int
    period_start: dict[int, str]
    timetable: dict[int, list[dict]]
    morning: tuple[int, int]
    night: tuple[int, int]


@dataclass(frozen=True)
class ReplyLogConfig:
    path: Path
    max_bytes: int


@dataclass(frozen=True)
class ReplyConfig:
    """回复怎么发出去。人设要求「祝福语正经一句 + 皮一句要分成两句」，
    所以带换行的回复按行拆成多条发（更像真人打字），行与行之间停一下。

    `mention=False`（默认）= 回复不带 @ 对方——被 @ 才会回，但回的时候不艾特，
    更像群里的人搭话；想退回「@某人 + 内容」的老样子就设 `REPLY_MENTION=true`。
    """

    split_lines: bool
    line_gap: float
    mention: bool


@dataclass(frozen=True)
class WebConfig:
    """联网搜索（聊天按需问答）+ 主动定时自学。

    enabled      总开关；scope=both/private/group 控制在哪些会话触发按需联网；
    force_words  消息里出现这些词就强制联网（不管规则判定）；
    learn_enabled 主动自学开关；learn_times 每天两次时刻 (HH:MM)；
    learn_items  每次自学抓几条热搜；learn_source=hot(热搜)/topics(预设清单)；
    learn_ai_enabled 额外一路 AI 活人感主题自学开关；learn_ai_items 每轮学几条；
    learn_ai_topics 轮换主题角度清单（联网检索知乎 / 全网公开讨论）。
    """

    enabled: bool
    scope: str
    force_words: tuple[str, ...]
    max_keywords: int
    learn_enabled: bool
    learn_times: tuple[tuple[int, int], ...]
    learn_items: int
    learn_source: str
    topics_file: Path
    learn_ai_enabled: bool
    learn_ai_items: int
    learn_ai_topics: tuple[str, ...]


@dataclass(frozen=True)
class BiliWatchConfig:
    """B 站消息轮询（有人在评论区 @ 本鱼 / 回复本鱼评论时回应对话）。

    enabled          轮询总开关；
    interval         轮询间隔秒数；
    reply_enabled    是否自动回评：false=只把新消息和拟回复文案推给主人预览（dry-run），true=自动回评；
    notify_owner     有新消息时是否把摘要推给主人私聊报备；
    max_per_run      单轮最多处理几条，防止一次性刷屏；
    watch_reply_feed 是否监听「别人回复本鱼发过的评论」；false 时只监听 @ 本鱼；
    reply_chance     命中事件后随机回复的概率（0~1），未命中静默不回，避免每条都接被风控。
    """

    enabled: bool
    interval: int
    reply_enabled: bool
    notify_owner: bool
    max_per_run: int
    watch_reply_feed: bool
    reply_chance: float


@dataclass(frozen=True)
class BiliTechConfig:
    """主动监测 B 站 AI 陪伴 / 拟人方向新投稿，命中即留言报备，主人 QQ 拍板是否学。

    enabled      模块总开关；
    interval     扫描间隔秒（2 小时起步，防 412 风控）；
    keywords     监测关键词，逗号分隔；
    max_per_run  单轮最多发现几条新线索；
    max_per_day  每天主动留言条数上限（保守额度）；
    notify_owner 发现线索是否 QQ 私聊报备主人；
    fresh_hours  只收最近多少小时内的新投稿，旧视频不重复打扰。
    """

    enabled: bool
    interval: int
    keywords: tuple[str, ...]
    max_per_run: int
    max_per_day: int
    notify_owner: bool
    fresh_hours: int


@dataclass(frozen=True)
class DailyMoodConfig:
    """每天定时给主人私聊发的「动态 / 说说」（不发 QQ 空间）。

    enabled        总开关；owner_qq 接收的主人 QQ（默认读 OWNER_QQ_ID）；
    times          每天定时时刻列表 (HH,MM)，默认 08:00 / 13:00 / 21:00；
    delay_min/max  触发后随机延迟分钟数（防风控、像真人节奏）；
    max_chars      每条动态字数上限；history_keep  去重保留最近多少条已发文案。
    """

    enabled: bool
    owner_qq: str
    times: tuple[tuple[int, int], ...]
    delay_min: int
    delay_max: int
    max_chars: int
    history_keep: int


@dataclass(frozen=True)
class DailyDiaryConfig:
    """本鱼每天睡前写一篇日记，只存档不主动推送私聊，主人发指令回看。

    enabled           定时写日记总开关；owner_qq 主人 QQ；
    time              每天写日记时刻 (HH,MM)，默认 23:30；
    delay_min/max     触发后随机延迟分钟数（像真人节奏）；
    max_chars         日记正文字数上限；keep 本地日记文件保留最近多少篇；
    material_private  素材里最多取多少条当天私聊；material_group 素材里最多取多少条当天群聊。
    """

    enabled: bool
    owner_qq: str
    time: tuple[int, int]
    delay_min: int
    delay_max: int
    max_chars: int
    keep: int
    material_private: int
    material_group: int


@dataclass(frozen=True)
class NetworkConfig:
    """反向 WS 监听地址：NapCat 里填的对端 URL 必须能对上（`ws://host:port/onebot/v11/ws`）。"""

    host: str
    port: int

    @property
    def ws_url(self) -> str:
        h = self.host if self.host not in ("0.0.0.0", "") else "127.0.0.1"
        return f"ws://{h}:{self.port}/onebot/v11/ws"


@dataclass(frozen=True)
class Config:
    ai: AIConfig
    context: ContextConfig
    memory: MemoryConfig
    persona: PersonaConfig
    emoji: EmojiConfig
    schedule: ScheduleConfig
    reply_log: ReplyLogConfig
    reply: ReplyConfig
    network: NetworkConfig
    web: WebConfig
    bili_watch: BiliWatchConfig
    bili_tech: BiliTechConfig
    daily_mood: DailyMoodConfig
    daily_diary: DailyDiaryConfig
    lexicon: LexiconConfig
    allow_groups: frozenset[int] = field(default_factory=frozenset)
    env_file: Path = ENV_FILE

    def usage_cost(self, usage: dict) -> float:
        """按当前单价算一次调用的价钱（元）。"""
        if not usage:
            return 0.0
        cached = ((usage.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0
        pin = (usage.get("prompt_tokens") or 0) - cached
        pout = usage.get("completion_tokens") or 0
        return (pin * self.ai.price_in + cached * self.ai.price_cache
                + pout * self.ai.price_out) / 1_000_000


# ------------------------------------------------------------------ 加载

def _build_ai() -> AIConfig:
    key = _required_str("AI_API_KEY")
    base = _raw("AI_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3").rstrip("/")
    model = _raw("AI_MODEL", "deepseek-v4-flash-ga-260731")
    return AIConfig(
        api_key=key,
        base_url=base,
        model=model,
        temperature=_as_float("AI_TEMPERATURE", 0.95, 0.0, 2.0),
        timeout=_as_float("AI_TIMEOUT", 90.0, 1.0, 600.0),
        max_retries=_as_int("AI_MAX_RETRIES", 3, 0, 10),
        price_in=_as_float("PRICE_IN", 3.0, 0.0),
        price_out=_as_float("PRICE_OUT", 9.0, 0.0),
        price_cache=_as_float("PRICE_CACHE", 0.1, 0.0),
    )


def _build_context() -> ContextConfig:
    hmax = _as_int("HISTORY_MAX", 20, 1, 500)
    huse = _as_int("HISTORY_USE", 8, 1, 500)
    if huse > hmax:
        raise ConfigError(f"HISTORY_USE({huse}) 不能大于 HISTORY_MAX({hmax})（改 .env）")
    return ContextConfig(
        history_max=hmax,
        history_use=huse,
        max_len_per_msg=_as_int("MAX_LEN_PER_MSG", 120, 10, 2000),
        at_window=_as_int("AT_WINDOW", 20, 0, 3600),
    )


def _build_memory() -> MemoryConfig:
    archive_max = _as_int("MEM_ARCHIVE_MAX_CHARS", 1200, 100, 100_000)
    # 2026-11-05：1000 → 1500。实测 60 条的私聊档案约 1107 字，1000 的预算会截掉尾巴，
    # 「明天开始找工作」「到家约需一小时」这类刚说的新状态正好被切在外面。
    use_max = _as_int("MEM_MAX_USE_CHARS", 1500, 100, 100_000)
    if archive_max > use_max:
        raise ConfigError(
            f"MEM_ARCHIVE_MAX_CHARS({archive_max}) 应不大于 MEM_MAX_USE_CHARS({use_max})（改 .env）"
        )
    return MemoryConfig(
        consolidate_every=_as_int("MEM_CONSOLIDATE_EVERY", 30, 2, 1000),
        archive_max_chars=archive_max,
        max_use_chars=use_max,
        pending_keep=_as_int("MEM_PENDING_KEEP", 200, 10, 100_000),
    )


def _build_lexicon() -> LexiconConfig:
    return LexiconConfig(
        enabled=_as_bool("LEXICON_ENABLED", True),
        group_learn_enabled=_as_bool("LEXICON_GROUP_LEARN_ENABLED", True),
        batch_size=_as_int("LEXICON_BATCH_SIZE", 12, 2, 200),
        max_terms_per_group=_as_int("LEXICON_MAX_TERMS_PER_GROUP", 200, 10, 5000),
        top_in_prompt=_as_int("LEXICON_TOP_IN_PROMPT", 8, 1, 30),
        extract_ai_enabled=_as_bool("LEXICON_EXTRACT_AI_ENABLED", True),
        max_meaning_chars=_as_int("LEXICON_MAX_MEANING_CHARS", 30, 4, 120),
    )


def _build_persona() -> PersonaConfig:
    nickname = _raw("BOT_NICKNAME", "鲸鱼娘")
    if not PERSONA_FILE.exists():
        raise ConfigError(f"缺少人设文件：{PERSONA_FILE.name}（仓库根目录）")
    prompt = PERSONA_FILE.read_text(encoding="utf-8").strip()
    if len(prompt) < 50:
        raise ConfigError(f"{PERSONA_FILE.name} 内容太短（{len(prompt)} 字），是不是被清空了？")
    if nickname not in prompt:
        raise ConfigError(
            f"{PERSONA_FILE.name} 里没有出现昵称「{nickname}」；"
            f"改了 BOT_NICKNAME 就要同步改人设文件"
        )
    rules = _load_json(PERSONA_RULES_FILE, "人设红线")
    return PersonaConfig(
        nickname=nickname, prompt=prompt, prompt_file=PERSONA_FILE,
        rules=rules, rules_file=PERSONA_RULES_FILE,
    )


def _build_schedule() -> ScheduleConfig:
    data = _load_json(TIMETABLE_FILE, "课表")
    try:
        period_start = {int(k): str(v) for k, v in (data.get("period_start") or {}).items()}
        timetable: dict[int, list[dict]] = {}
        for wd, items in (data.get("timetable") or {}).items():
            w = int(wd)
            if not 1 <= w <= 7:
                raise ValueError(f"星期 {w} 越界（1=周一 … 7=周日）")
            rows = []
            for it in items:
                p = int(it["period"])
                if p not in period_start:
                    raise ValueError(f"星期{w} 的第 {p} 节在 period_start 里没有上课时间")
                if not str(it.get("name", "")).strip():
                    raise ValueError(f"星期{w} 第{p}节 缺课程名")
                rows.append({"period": p, "name": it["name"], "location": it.get("location", "")})
            timetable[w] = rows
        for hhmm in period_start.values():
            if not re.fullmatch(r"\d{2}:\d{2}", hhmm):
                raise ValueError(f"上课时间格式应为 HH:MM，收到 {hhmm}")
    except (KeyError, TypeError, ValueError) as e:
        raise ConfigError(f"{TIMETABLE_FILE.name} 内容有问题：{e}") from e

    return ScheduleConfig(
        target_group=_as_int("TARGET_GROUP", 0, 0),
        remind_before=_as_int("REMIND_BEFORE", int(data.get("remind_before", 20)), 0, 240),
        period_start=period_start,
        timetable=timetable,
        morning=(int(data.get("morning_hour", 6)), int(data.get("morning_minute", 0))),
        night=(int(data.get("night_hour", 23)), int(data.get("night_minute", 0))),
    )


def _build_web() -> WebConfig:
    scope = _raw("WEB_SEARCH_SCOPE", "both").lower()
    if scope not in ("both", "private", "group"):
        raise ConfigError(f"WEB_SEARCH_SCOPE={scope!r} 只能是 both / private / group（改 .env）")
    source = _raw("LEARN_SOURCE", "hot").lower()
    if source not in ("hot", "topics"):
        raise ConfigError(f"LEARN_SOURCE={source!r} 只能是 hot / topics（改 .env）")

    times: list[tuple[int, int]] = []
    for chunk in re.split(r"[,\s]+", _raw("LEARN_TIMES", "12:30,21:30")):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", chunk)
        if not m:
            raise ConfigError(f"LEARN_TIMES 格式应为 HH:MM，收到 {chunk!r}（改 .env）")
        hh, mm = int(m.group(1)), int(m.group(2))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ConfigError(f"LEARN_TIMES 时刻越界：{chunk!r}（改 .env）")
        times.append((hh, mm))
    if not times:
        raise ConfigError("LEARN_TIMES 至少要填一个时刻（改 .env）")

    force = tuple(x.strip() for x in re.split(r"[,，]", _raw("WEB_SEARCH_FORCE_WORDS", "联网,搜一下,查查,查一下,百度一下,搜搜")) if x.strip())

    default_ai_topics = (
        "AI 活人感 拟人化 最新做法",
        "AI 聊天机器人 情绪反应 真人感 聊天节奏",
        "AI 角色扮演 人设 长期记忆 社区讨论",
        "AI 助手 自然对话 不像机器人 技巧",
        "AI 陪伴 记忆 情感连接 公开讨论",
    )
    ai_topics = tuple(
        x.strip() for x in re.split(r"[,，]", _raw("LEARN_AI_TOPICS", "")) if x.strip()
    ) or default_ai_topics

    return WebConfig(
        enabled=_as_bool("WEB_SEARCH_ENABLED", True),
        scope=scope,
        force_words=force,
        max_keywords=_as_int("WEB_SEARCH_MAX_KEYWORDS", 3, 1, 10),
        learn_enabled=_as_bool("LEARN_ENABLED", True),
        learn_times=tuple(times),
        learn_items=_as_int("LEARN_ITEMS_PER_RUN", 3, 1, 20),
        learn_source=source,
        topics_file=ROOT / (_raw("LEARN_TOPICS_FILE", "learn_topics.txt") or "learn_topics.txt"),
        learn_ai_enabled=_as_bool("LEARN_AI_ENABLED", True),
        learn_ai_items=_as_int("LEARN_AI_ITEMS_PER_RUN", 1, 1, 10),
        learn_ai_topics=ai_topics,
    )


def _build_bili_watch() -> BiliWatchConfig:
    return BiliWatchConfig(
        enabled=_as_bool("BILI_WATCH_ENABLED", True),
        interval=_as_int("BILI_WATCH_INTERVAL", 60, 15, 600),
        reply_enabled=_as_bool("BILI_WATCH_REPLY_ENABLED", False),
        notify_owner=_as_bool("BILI_WATCH_NOTIFY_OWNER", True),
        max_per_run=_as_int("BILI_WATCH_MAX_PER_RUN", 2, 1, 5),
        watch_reply_feed=_as_bool("BILI_WATCH_REPLY_FEED", True),
        reply_chance=_as_float("BILI_WATCH_REPLY_CHANCE", 0.5, 0.0, 1.0),
    )


def _build_bili_tech() -> BiliTechConfig:
    default_keywords = ("AI 陪伴机器人", "AI 拟人", "AI 角色对话")
    keywords = tuple(
        x.strip() for x in re.split(r"[,，]", _raw("BILI_TECH_KEYWORDS", "")) if x.strip()
    ) or default_keywords
    return BiliTechConfig(
        enabled=_as_bool("BILI_TECH_ENABLED", True),
        interval=_as_int("BILI_TECH_INTERVAL", 7200, 300, 86400),
        keywords=keywords,
        max_per_run=_as_int("BILI_TECH_MAX_PER_RUN", 2, 1, 5),
        # 2026-11-07 主人：「她想留言就留言吧，不需要我确认，告诉我就行」
        # 原来默认 1（一天只能主动留 1 条），第 2 条就「额度已用满只报备」。
        # 提到 10，和 bilibili_account.KIND_DAILY_CAP["tech_comment"]=10 对齐；
        # 真正的风控余量靠那边（10 分钟最多 4 条、每天总互动 20 条、同视频 3 次）。
        max_per_day=_as_int("BILI_TECH_MAX_PER_DAY", 10, 0, 20),
        notify_owner=_as_bool("BILI_TECH_NOTIFY_OWNER", True),
        fresh_hours=_as_int("BILI_TECH_FRESH_HOURS", 48, 1, 168),
    )


def _build_daily_mood() -> DailyMoodConfig:
    times: list[tuple[int, int]] = []
    for chunk in re.split(r"[,\s]+", _raw("DAILY_MOOD_TIMES", "08:00,13:00,21:00")):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", chunk)
        if not m:
            raise ConfigError(f"DAILY_MOOD_TIMES 格式应为 HH:MM，收到 {chunk!r}（改 .env）")
        hh, mm = int(m.group(1)), int(m.group(2))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ConfigError(f"DAILY_MOOD_TIMES 时刻越界：{chunk!r}（改 .env）")
        times.append((hh, mm))
    if not times:
        raise ConfigError("DAILY_MOOD_TIMES 至少要填一个时刻（改 .env）")

    delay_min = _as_int("DAILY_MOOD_DELAY_MIN", 0, 0, 120)
    delay_max = _as_int("DAILY_MOOD_DELAY_MAX", 20, 0, 120)
    if delay_max < delay_min:
        raise ConfigError("DAILY_MOOD_DELAY_MAX 不能小于 DAILY_MOOD_DELAY_MIN（改 .env）")

    return DailyMoodConfig(
        enabled=_as_bool("DAILY_MOOD_ENABLED", True),
        owner_qq=_raw("OWNER_QQ_ID", "100000000"),
        times=tuple(times),
        delay_min=delay_min,
        delay_max=delay_max,
        max_chars=_as_int("DAILY_MOOD_MAX_CHARS", 40, 10, 200),
        history_keep=_as_int("DAILY_MOOD_HISTORY_KEEP", 20, 5, 200),
    )


def _build_daily_diary() -> DailyDiaryConfig:
    raw_time = _raw("DAILY_DIARY_TIME", "23:30")
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", raw_time.strip())
    if not m:
        raise ConfigError(f"DAILY_DIARY_TIME 格式应为 HH:MM，收到 {raw_time!r}（改 .env）")
    hh, mm = int(m.group(1)), int(m.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ConfigError(f"DAILY_DIARY_TIME 时刻越界：{raw_time!r}（改 .env）")

    delay_min = _as_int("DAILY_DIARY_DELAY_MIN", 0, 0, 120)
    delay_max = _as_int("DAILY_DIARY_DELAY_MAX", 10, 0, 120)
    if delay_max < delay_min:
        raise ConfigError("DAILY_DIARY_DELAY_MAX 不能小于 DAILY_DIARY_DELAY_MIN（改 .env）")

    return DailyDiaryConfig(
        enabled=_as_bool("DAILY_DIARY_ENABLED", True),
        owner_qq=_raw("OWNER_QQ_ID", "100000000"),
        time=(hh, mm),
        delay_min=delay_min,
        delay_max=delay_max,
        max_chars=_as_int("DAILY_DIARY_MAX_CHARS", 600, 100, 3000),
        keep=_as_int("DAILY_DIARY_KEEP", 30, 5, 365),
        material_private=_as_int("DAILY_DIARY_MATERIAL_PRIVATE", 12, 1, 50),
        material_group=_as_int("DAILY_DIARY_MATERIAL_GROUP", 16, 1, 80),
    )


def load_config(env_file: Path | None = None, reload: bool = False) -> Config:
    """读 `.env` 和数据文件，做类型/范围/交叉校验，返回不可变 Config。"""
    global _CACHE
    if _CACHE is not None and not reload:
        return _CACHE

    path = env_file or ENV_FILE
    if load_dotenv is not None and path.exists():
        load_dotenv(path, override=reload or _CACHE is None)
    elif not path.exists():
        print(f"⚠️  没有找到 {path.name}（照 .env.example 复制一份填好即可继续）")

    max_bytes = _as_int("REPLY_LOG_MAX_BYTES", 5 * 1024 * 1024, 10_000)
    cfg = Config(
        ai=_build_ai(),
        context=_build_context(),
        memory=_build_memory(),
        persona=_build_persona(),
        emoji=EmojiConfig(dir=EMOJI_DIR, moods=MOODS, exts=IMG_EXTS),
        schedule=_build_schedule(),
        reply_log=ReplyLogConfig(path=LOG_DIR / "replies.jsonl", max_bytes=max_bytes),
        reply=ReplyConfig(split_lines=_as_bool("REPLY_SPLIT_LINES", True),
                          line_gap=_as_float("REPLY_LINE_GAP", 0.6, 0.0, 10.0),
                          mention=_as_bool("REPLY_MENTION", False)),
        network=NetworkConfig(host=_raw("HOST", "127.0.0.1") or "127.0.0.1",
                              port=_as_int("PORT", 8080, 1, 65535)),
        web=_build_web(),
        bili_watch=_build_bili_watch(),
        bili_tech=_build_bili_tech(),
        daily_mood=_build_daily_mood(),
        daily_diary=_build_daily_diary(),
        lexicon=_build_lexicon(),
        allow_groups=frozenset(_as_group_set("ALLOW_GROUPS")),
        env_file=path,
    )
    _CACHE = cfg
    return cfg


_CACHE: Config | None = None


def get_config() -> Config:
    """第一次调用时加载，之后复用。改了 `.env` 就重启，或用 `reload_config()`。"""
    return _CACHE or load_config()


def reload_config() -> Config:
    return load_config(reload=True)


def redact(text: str, cfg: Config | None = None) -> str:
    """日志/报错发出去之前先把凭据抹掉。任何进 nonebot logger 的模型返回都该过一遍。"""
    if not text:
        return text
    cfg = cfg or get_config()
    out = text
    if cfg.ai.api_key:
        out = out.replace(cfg.ai.api_key, "***")
    return re.sub(r"(Bearer\s+)[A-Za-z0-9_\-\.]{8,}", r"\1***", out)


# ------------------------------------------------------------------ 自检

OK, WARN, FAIL = "OK", "WARN", "FAIL"


@dataclass
class Check:
    name: str
    level: str
    detail: str

    @property
    def icon(self) -> str:
        return {OK: "✅", WARN: "⚠️ ", FAIL: "❌"}[self.level]


def _gitignore_has(token: str) -> bool:
    gi = ROOT / ".gitignore"
    if not gi.exists():
        return False
    return token in gi.read_text(encoding="utf-8")


def check_gitignore() -> Check:
    missing = [t for t in (".env", "memory/", "logs/", ".venv/") if not _gitignore_has(t)]
    return Check(
        "敏感文件已忽略",
        FAIL if missing else OK,
        f"未忽略：{', '.join(missing)} ← 密钥/聊天记录会进版本库" if missing
        else ".env / memory/ / logs/ / .venv/ 都写进了 .gitignore",
    )


def check_api_key(cfg: Config) -> Check:
    key = cfg.ai.api_key
    if not key:
        return Check("AI_API_KEY", FAIL, "没读到。检查 .env 里 AI_API_KEY 有没有填、有没有多打空格")
    if len(key) < 12:
        return Check("AI_API_KEY", FAIL, f"长度只有 {len(key)}，不像有效密钥")
    if re.search(r"(你的|示例|xxx)", key, re.I):
        return Check("AI_API_KEY", FAIL, "还是占位符，没换成真密钥")
    masked = key[:6] + "*" * max(0, len(key) - 10) + key[-4:]
    return Check("AI_API_KEY", OK, f"已加载（{masked}）")


def check_base_url(cfg: Config) -> Check:
    url = cfg.ai.base_url
    if not url.startswith(("http://", "https://")):
        return Check("AI_BASE_URL", FAIL, f"{url} 不是合法 URL")
    if url.startswith("http://"):
        return Check("AI_BASE_URL", WARN, "用的 http，密钥会明文过网")
    return Check("AI_BASE_URL", OK, url)


def check_model(cfg: Config) -> Check:
    model = cfg.ai.model
    if not model:
        return Check("AI_MODEL", FAIL, "空")
    if "volces.com" in cfg.ai.base_url and "deepseek-v4-flash" in model and "-ga-" not in model:
        return Check("AI_MODEL", FAIL, f"{model} 少了 -ga- 段（方舟会报 404）")
    return Check("AI_MODEL", OK, model)


def check_persona(cfg: Config) -> Check:
    try:
        rules = cfg.persona.rules.get("rules", [])
        density = cfg.persona.rules.get("density_rules", [])
        scenes = cfg.persona.rules.get("scene_rules", [])
        bad = []
        for r in list(rules) + list(density):
            try:
                re.compile(r["pattern"])
            except re.error as e:
                bad.append(f"{r.get('id')}: {e}")
        for r in scenes:
            for key in ("trigger", "bless", "rough"):
                try:
                    re.compile(r[key])
                except (KeyError, re.error) as e:
                    bad.append(f"{r.get('id')}.{key}: {e}")
        if bad:
            return Check("人设红线", FAIL, "正则写错：" + "; ".join(bad))
        off = sum(1 for r in rules if str(r.get("level", "")).lower() == "off")
        return Check("人设", OK,
                     f"{cfg.persona.prompt_file.name} {len(cfg.persona.prompt)} 字，"
                     f"{cfg.persona.rules_file.name} 逐句红线 {len(rules) - off} 条"
                     f"（放宽 {off}）+ 密度规则 {len(density)} 条 + 场景规则 {len(scenes)} 条")
    except (KeyError, TypeError) as e:
        return Check("人设红线", FAIL, f"{cfg.persona.rules_file.name} 结构不对：{e}")


def check_emoji(cfg: Config) -> Check:
    if not cfg.emoji.dir.is_dir():
        return Check("表情包目录", FAIL, f"不存在：{cfg.emoji.dir}")
    empty = [m for m in cfg.emoji.moods if cfg.emoji.count(m) == 0]
    total = cfg.emoji.count_all()
    if empty:
        return Check("表情包", WARN, f"共 {total} 张，但这些情绪是空的：{'/'.join(empty)}")
    return Check("表情包", OK, f"{total} 张，{len(cfg.emoji.moods)} 种情绪齐全")


def check_writable(d: Path, label: str) -> Check:
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return Check(f"{label}可写", OK, str(d))
    except OSError as e:
        return Check(f"{label}可写", FAIL, str(e))


def check_timetable(cfg: Config) -> Check:
    tt = cfg.schedule.timetable
    n = sum(len(v) for v in tt.values())
    if not n:
        return Check("课表", WARN, f"{TIMETABLE_FILE.name} 是空的，上课提醒不会触发")
    days = "/".join(f"周{w}" for w in sorted(tt))
    return Check("课表", OK, f"{n} 节课（{days}），提前 {cfg.schedule.remind_before} 分钟提醒")


def check_target_group(cfg: Config) -> Check:
    gid = cfg.schedule.target_group
    if not gid:
        return Check("推送群号 TARGET_GROUP", OK, "未配置（早晚安/上课提醒已取消，不再推送）")
    known: set[int] = set()
    f = MEMORY_DIR / "长期记忆.json"
    if f.exists():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            known = {int(k) for k in (data.get("archive") or {})}
        except Exception:  # noqa: BLE001
            known = set()
    if known and gid not in known:
        return Check("推送群号 TARGET_GROUP", WARN,
                     f"{gid}，但长期记忆里出现过的群是 {sorted(known)} —— 群号填错了吗？")
    return Check("推送群号 TARGET_GROUP", OK, str(gid))


def check_port(cfg: Config) -> Check:
    import socket

    host = cfg.network.host if cfg.network.host not in ("0.0.0.0", "") else "127.0.0.1"
    port = cfg.network.port

    # 2026-11-07：原来只看"能不能 bind"—— 但机器人正在跑时端口当然被自己占着，
    # 于是每次运行时自检都报 ❌「起不来，多半是上一个后端没关」，把真的失败都淹了。
    # 先看有没有人在 listen：有人在听说明服务活着，再判断那是谁。
    try:
        probe = socket.socket()
        probe.settimeout(0.4)
        if probe.connect_ex((host, port)) == 0:
            probe.close()
            return Check("监听端口", OK,
                         f"{host}:{port} 已在监听（后端/前端正在跑，正常）；"
                         f"NapCat 反向 WS 填 {cfg.network.ws_url}")
        probe.close()
    except Exception:  # noqa: BLE001
        pass

    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((host, port))
        return Check("监听端口", OK, f"{host}:{port} 空闲；NapCat 反向 WS 填 {cfg.network.ws_url}")
    except OSError as e:
        return Check("监听端口", WARN,
                     f"{host}:{port} 连不上也绑不了（{e.strerror}）——可能被别的进程占着，"
                     f"或权限不足")
    finally:
        s.close()


def check_lexicon(cfg: Config) -> Check:
    lx = cfg.lexicon
    if not lx.enabled:
        return Check("群聊词库", WARN, "LEXICON_ENABLED=false，不会记录群聊黑话词条")
    detail = (
        f"每群最多 {lx.max_terms_per_group} 条；每 {lx.batch_size} 条抽词；"
        f"prompt 带 top {lx.top_in_prompt}"
    )
    if not lx.group_learn_enabled:
        detail += "；非 @ 闲聊不入学习缓冲（仅 @ 对话入档）"
    if not lx.extract_ai_enabled:
        detail += "；只计数已有词条，不调模型抽新词"
    return Check("群聊词库", OK, detail)


def check_web(cfg: Config) -> Check:
    w = cfg.web
    if not w.enabled:
        return Check("联网搜索", WARN, "WEB_SEARCH_ENABLED=false，聊天不会联网")
    scope_name = {"both": "私聊+群聊", "private": "仅私聊", "group": "仅群聊"}.get(w.scope, w.scope)
    detail = f"范围 {scope_name}；强制词 {len(w.force_words)} 个"
    if w.learn_enabled:
        times = "/".join(f"{h:02d}:{m:02d}" for h, m in w.learn_times)
        src = "当日热搜" if w.learn_source == "hot" else f"预设清单({w.topics_file.name})"
        detail += f"；主动自学 {times}（{src}，每次 {w.learn_items} 条）"
        if w.learn_ai_enabled:
            detail += f"；AI活人感主题每轮 {w.learn_ai_items} 条（联网检索知乎/全网）"
        if w.learn_source == "topics" and not w.topics_file.exists():
            return Check("联网搜索", WARN, detail + f"；但主题清单 {w.topics_file.name} 不存在")
    else:
        detail += "；主动自学已关闭"
    return Check("联网搜索", OK, detail)


def check_bili_watch(cfg: Config) -> Check:
    w = cfg.bili_watch
    if not w.enabled:
        return Check("B站@轮询", WARN, "BILI_WATCH_ENABLED=false，不会监听 B 站评论区 @")
    mode = "自动回评" if w.reply_enabled else "仅预览报备（不自动发评论）"
    scope = "@我 + 回复我评论" if w.watch_reply_feed else "仅 @我"
    detail = (
        f"每 {w.interval} 秒轮询（{scope}）；{mode}；"
        f"主人必回，其他人概率 {int(w.reply_chance * 100)}%；单轮最多 {w.max_per_run} 条"
    )
    detail += "；新消息会报备主人私聊" if w.notify_owner else "；不主动报备主人"
    return Check("B站@轮询", OK, detail)


def check_bili_tech(cfg: Config) -> Check:
    w = cfg.bili_tech
    if not w.enabled:
        return Check("B站技术监测", WARN, "BILI_TECH_ENABLED=false，不主动搜新投稿留言报备")
    minutes = w.interval // 60
    detail = (
        f"每 {minutes} 分钟扫一轮（关键词 {len(w.keywords)} 个）；"
        f"每轮最多 {w.max_per_run} 条新线索；每天主动留言 ≤{w.max_per_day} 条；"
        f"只收最近 {w.fresh_hours} 小时新投稿"
    )
    detail += "；线索 QQ 报备主人" if w.notify_owner else "；不主动报备"
    return Check("B站技术监测", OK, detail)


def check_daily_diary(cfg: Config) -> Check:
    d = cfg.daily_diary
    if not d.enabled:
        return Check("每日日记", WARN, "DAILY_DIARY_ENABLED=false，定时写日记已关闭（仍可手动指令写）")
    hh, mm = d.time
    detail = (
        f"每天 {hh:02d}:{mm:02d} 定时写日记，只存档不推送私聊；"
        f"正文 ≤{d.max_chars} 字；本地保留最近 {d.keep} 篇；"
        f"素材：私聊 ≤{d.material_private} 条、群聊 ≤{d.material_group} 条、B站回评、心情好感度"
    )
    return Check("每日日记", OK, detail)


def check_deps() -> Check:
    # 用 find_spec 而不是真 import：nonebot_plugin_apscheduler 这种插件模块
    # 一 import 就要取 driver，没 init 会直接抛 ValueError，伪装成"依赖缺失"。
    import importlib.util

    missing = []
    for mod in ("nonebot", "httpx", "dotenv", "nonebot_plugin_apscheduler",
                "nonebot.adapters.onebot.v11"):
        try:
            if importlib.util.find_spec(mod) is None:
                missing.append(mod)
        except (ImportError, ValueError) as e:
            missing.append(f"{mod}({e})")
    return Check("依赖已安装", FAIL if missing else OK,
                 f"缺：{', '.join(missing)} → 在 .venv 里 pip install -r requirements.txt" if missing
                 else "nonebot / onebot 适配器 / httpx / dotenv / apscheduler 都在")


def ping_provider(cfg: Config) -> Check:
    """真连一次（GET /models），验证凭据可用。默认不跑，加 --ping。"""
    try:
        import httpx
    except ImportError:
        return Check("模型连通性", WARN, "httpx 没装，跳过联网检查")
    try:
        r = httpx.get(
            f"{cfg.ai.base_url}/models",
            headers={"Authorization": f"Bearer {cfg.ai.api_key}"},
            timeout=15,
        )
    except Exception as e:  # noqa: BLE001
        return Check("模型连通性", FAIL, redact(f"{type(e).__name__}: {e}", cfg))
    if r.status_code == 401:
        return Check("模型连通性", FAIL, "401：密钥不对或没权限")
    if r.status_code >= 400:
        return Check("模型连通性", WARN, f"HTTP {r.status_code}：该厂商可能不支持 /models，忽略即可")
    return Check("模型连通性", OK, f"HTTP {r.status_code}，凭据可用")


def run_checks(ping: bool = False, cfg: Config | None = None) -> list[Check]:
    checks: list[Check] = []
    if cfg is None:
        try:
            cfg = get_config()
        except ConfigError as e:
            return [Check("配置加载", FAIL, str(e))]
    checks.append(Check("配置文件", OK if cfg.env_file.exists() else FAIL,
                        f"已加载 {cfg.env_file.name}" if cfg.env_file.exists()
                        else f"缺 {cfg.env_file.name}"))
    checks.append(check_gitignore())
    checks.append(check_api_key(cfg))
    checks.append(check_base_url(cfg))
    checks.append(check_model(cfg))
    checks.append(check_persona(cfg))
    checks.append(check_emoji(cfg))
    checks.append(check_timetable(cfg))
    checks.append(check_target_group(cfg))
    checks.append(check_writable(MEMORY_DIR, "memory 目录"))
    checks.append(check_writable(LOG_DIR, "logs 目录"))
    checks.append(check_port(cfg))
    checks.append(check_web(cfg))
    checks.append(check_bili_watch(cfg))
    checks.append(check_bili_tech(cfg))
    checks.append(check_daily_diary(cfg))
    checks.append(check_lexicon(cfg))
    checks.append(check_deps())
    if ping:
        checks.append(ping_provider(cfg))
    return checks


def report(checks: Iterable[Check]) -> str:
    items = list(checks)
    lines = [f"{c.icon} {c.name}：{c.detail}" for c in items]
    failed = sum(1 for c in items if c.level == FAIL)
    warned = sum(1 for c in items if c.level == WARN)
    lines.append("")
    lines.append(f"— 失败 {failed} · 警告 {warned} · 通过 {len(items) - failed - warned} "
                 f"@ {datetime.now():%Y-%m-%d %H:%M}")
    return "\n".join(lines)
