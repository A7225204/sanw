"""长期记忆文件层：按 scope 分目录、按日/周/月/年分层的 JSON 存储。

- scope：group_{gid} / private_{user_id}
- 目录：long_memory/{scope}/{daily|weekly|monthly|yearly}/...
- 查询：detect_memory_intent() + recall()
- 群聊只读本群；私聊可读私聊记忆 + 群聊摘要（默认不读群聊明细）
"""
from __future__ import annotations

import json
import os
import re
import heapq
from collections import Counter, OrderedDict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# 路径与常量
# ---------------------------------------------------------------------------
def _default_root() -> Path:
    base = os.getenv("APPDATA")
    if not base:
        base = str(Path.home() / ".local" / "share")
    return Path(base) / "dsh-pet-standalone-webm-chat" / "long_memory"


ROOT = _default_root()
INDEX_FILE = ROOT / "_index.json"

DAILY_MAX_MESSAGES = 500
MAX_ACTIVE_GROUPS = 3
DEFAULT_MAX_CHARS = 800
# 最小堆每层保留的高光事件条数 k（借鉴银行排队最小堆：新事件分高过堆顶才替换最低分）
WEEKLY_TOP = 8
MONTHLY_TOP = 6
YEARLY_TOP = 4

_STOPWORDS = set("""
的 了 是 在 我 你 他 她 它 们 这 那 有 和 与 就 都 也 还 吗 呢 吧 啊 呀
什么 怎么 为什么 一个 一下 一些 这个 那个 这样 那样 我们 你们 他们
""".split())

_RECALL_VERBS = ["聊", "说", "讲", "发生", "记得", "回忆", "什么", "咋", "怎么样"]

_file_cache: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()
_CACHE_MAX = 64


# ---------------------------------------------------------------------------
# 时间字符串
# ---------------------------------------------------------------------------
def _today() -> datetime:
    return datetime.now()


def _date_str(d: datetime) -> str:
    return d.strftime("%Y-%m-%d")


def _week_str(d: datetime) -> str:
    iso = d.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _month_str(d: datetime) -> str:
    return d.strftime("%Y-%m")


def _year_str(d: datetime) -> str:
    return d.strftime("%Y")


def _offset_date(offset_days: int) -> datetime:
    return _today() + timedelta(days=offset_days)


def _offset_week(offset_weeks: int) -> datetime:
    return _today() + timedelta(weeks=offset_weeks)


def _offset_month(offset_months: int) -> datetime:
    now = _today()
    y, m = now.year, now.month + offset_months
    while m <= 0:
        m += 12
        y -= 1
    while m > 12:
        m -= 12
        y += 1
    return datetime(y, m, 1)


def _offset_year(offset_years: int) -> datetime:
    now = _today()
    return datetime(now.year + offset_years, 1, 1)


# 当前进行中的窗口才允许刷新；已结束窗口冻结（事件驱动：跨边界即归档，不再回写）
def _is_current_week(anchor: datetime) -> bool:
    return _week_str(anchor) == _week_str(_today())


def _is_current_month(anchor: datetime) -> bool:
    return _month_str(anchor) == _month_str(_today())


def _is_current_year(anchor: datetime) -> bool:
    return _year_str(anchor) == _year_str(_today())


# ---------------------------------------------------------------------------
# JSON 读写 + 缓存
# ---------------------------------------------------------------------------
def _read_json(path: Path) -> Optional[dict]:
    key = str(path)
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        _file_cache.pop(key, None)
        return None
    if key in _file_cache and _file_cache[key][0] == mtime:
        _file_cache.move_to_end(key)
        return _file_cache[key][1]
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    _file_cache[key] = (mtime, data)
    _file_cache.move_to_end(key)
    while len(_file_cache) > _CACHE_MAX:
        _file_cache.popitem(last=False)
    return data


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)
    key = str(path)
    try:
        _file_cache[key] = (path.stat().st_mtime, data)
        _file_cache.move_to_end(key)
        while len(_file_cache) > _CACHE_MAX:
            _file_cache.popitem(last=False)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 索引
# ---------------------------------------------------------------------------
def _empty_index() -> dict:
    return {"scopes": {}, "updated_at": ""}


def _load_index() -> dict:
    data = _read_json(INDEX_FILE)
    if not isinstance(data, dict):
        return _empty_index()
    data.setdefault("scopes", {})
    return data


def _save_index(index: dict) -> None:
    index["updated_at"] = datetime.now().isoformat(timespec="seconds")
    _write_json(INDEX_FILE, index)


def _touch_index(scope: str, level: str, key: str) -> None:
    index = _load_index()
    scopes = index.setdefault("scopes", {})
    entry = scopes.setdefault(scope, {
        "last_active": "",
        "dates": [],
        "weeks": [],
        "months": [],
        "years": [],
    })
    entry["last_active"] = datetime.now().isoformat(timespec="seconds")
    list_key = {
        "daily": "dates",
        "weekly": "weeks",
        "monthly": "months",
        "yearly": "years",
    }[level]
    lst = entry.setdefault(list_key, [])
    if key not in lst:
        lst.append(key)
        lst.sort()
    # 索引清单容量上限：只保留最近窗口，防止长期运行后索引无限膨胀
    cap = {"dates": 90, "weeks": 26, "months": 12, "years": 100}[list_key]
    if len(lst) > cap:
        del lst[: len(lst) - cap]
    _save_index(index)


# ---------------------------------------------------------------------------
# scope 目录
# ---------------------------------------------------------------------------
def scope_dir(scope: str) -> Path:
    return ROOT / scope


def daily_path(scope: str, date: str) -> Path:
    return scope_dir(scope) / "daily" / f"{date}.json"


def weekly_path(scope: str, week: str) -> Path:
    return scope_dir(scope) / "weekly" / f"{week}.json"


def monthly_path(scope: str, month: str) -> Path:
    return scope_dir(scope) / "monthly" / f"{month}.json"


def yearly_path(scope: str, year: str) -> Path:
    return scope_dir(scope) / "yearly" / f"{year}.json"


# ---------------------------------------------------------------------------
# 关键词提取（不引入 jieba，用 2-4 字中文 n-gram）
# ---------------------------------------------------------------------------
_CHINESE_RE = re.compile(r"[\u4e00-\u9fa5]+")


def _extract_keywords(texts: list[str], top_n: int = 5) -> list[str]:
    counter: Counter = Counter()
    for t in texts:
        if not t:
            continue
        for chunk in _CHINESE_RE.findall(t):
            n = len(chunk)
            if n < 2:
                continue
            for size in (2, 3, 4):
                for i in range(0, n - size + 1):
                    sub = chunk[i:i + size]
                    if sub in _STOPWORDS:
                        continue
                    counter[sub] += 1
    result: list[str] = []
    for word, cnt in counter.most_common(50):
        if cnt < 2:
            break
        if any(word in longer for longer in result):
            continue
        result.append(word)
        if len(result) >= top_n:
            break
    return result


def _build_daily_summary(messages: list[dict], stats: dict) -> str:
    total = stats.get("total", 0)
    users = len(stats.get("by_user", {}))
    keywords = stats.get("top_keywords", [])
    if total == 0:
        return ""
    parts = [f"今日共 {total} 条消息，{users} 位群友参与"]
    if keywords:
        parts.append("主要话题：" + "、".join(keywords[:4]))
    return "，".join(parts) + "。"


# ---------------------------------------------------------------------------
# 高光事件打分 + 最小堆 top-k（阶段 3：按重要性沉淀，不再按时间一刀切）
# ---------------------------------------------------------------------------
_FACT_WORDS = [
    "喜欢", "不爱", "不吃", "记住", "生日", "名字", "住址", "地址", "电话", "手机",
    "约定", "下次", "以后", "习惯", "讨厌", "想去", "老婆", "老公", "对象", "宠物",
    "猫", "狗", "计划", "决定", "换工作", "买车", "买房", "工资", "女朋友", "男朋友",
    "老家", "学校", "专业", "星座", "分手", "复合", "结婚", "怀孕", "搬家", "辞职",
]


def _score_message(msg: dict) -> float:
    """给一轮对话打重要性分（可解释、阈值可配）。约定/偏好/事实权重最高。"""
    text = (msg.get("msg") or "").strip()
    reply = (msg.get("reply") or "").strip()
    if not text and not reply:
        return 0.0
    score = 1.0
    blob = text + reply
    if any(w in blob for w in _FACT_WORDS):
        score += 3.0
    if text.endswith(("？", "?")) or "吗？" in text or "呢？" in text:
        score += 1.0
    if "！" in text or "!" in text:
        score += 0.5
    if len(text) >= 20:
        score += 0.5
    if "@" in text:
        score += 1.0
    return score


def _select_highlights(candidates: list[dict], k: int) -> list[dict]:
    """小顶堆保留 k 条最高分候选；新事件分高过堆顶最低分就替换。

    candidates: [{score, speaker, text, date?|month?}]
    """
    heap: list[tuple[float, int, dict]] = []
    for seq, c in enumerate(candidates):
        item = {
            "score": round(float(c["score"]), 2),
            "speaker": c.get("speaker", "群友"),
            "text": (c.get("text") or "")[:120],
        }
        if c.get("date"):
            item["date"] = c["date"]
        if c.get("month"):
            item["month"] = c["month"]
        if len(heap) < k:
            heapq.heappush(heap, (item["score"], seq, item))
        elif item["score"] > heap[0][0]:
            heapq.heapreplace(heap, (item["score"], seq, item))
    return [it for _, _, it in sorted(heap, key=lambda x: -x[0])]


# ---------------------------------------------------------------------------
# 写入：daily 实时追加
# ---------------------------------------------------------------------------
def save_daily(scope: str, speaker: str, user_id: str, msg: str, reply: str) -> None:
    date = _date_str(_today())
    path = daily_path(scope, date)
    data = _read_json(path) or {
        "date": date,
        "scope": scope,
        "summary": "",
        "stats": {"total": 0, "by_user": {}, "top_keywords": []},
        "messages": [],
    }
    messages = data.setdefault("messages", [])
    messages.append({
        "speaker": speaker or "",
        "user_id": str(user_id or ""),
        "msg": (msg or "")[:1000],
        "reply": (reply or "")[:1000],
        "ts": datetime.now().isoformat(timespec="seconds"),
    })
    if len(messages) > DAILY_MAX_MESSAGES:
        # 保留最近 350 条，其余按重要性打分补回前 150 条，避免重要约定被时间淘汰
        recent = messages[-350:]
        rest = messages[:-350]
        rest_sorted = sorted(rest, key=_score_message, reverse=True)
        messages = (recent + rest_sorted)[:DAILY_MAX_MESSAGES]
        data["messages"] = messages

    stats = data.setdefault("stats", {"total": 0, "by_user": {}, "top_keywords": []})
    stats["total"] = len(messages)
    by_user = stats.setdefault("by_user", {})
    uid = str(user_id or "")
    by_user[uid] = by_user.get(uid, 0) + 1
    texts = [m.get("msg", "") for m in messages]
    stats["top_keywords"] = _extract_keywords(texts, top_n=5)
    data["summary"] = _build_daily_summary(messages, stats)
    data["updated_at"] = datetime.now().isoformat(timespec="seconds")

    _write_json(path, data)
    _touch_index(scope, "daily", date)


# ---------------------------------------------------------------------------
# 聚合：weekly / monthly / yearly（懒生成）
# ---------------------------------------------------------------------------
def _week_dates(week_anchor: datetime) -> list[str]:
    monday = week_anchor - timedelta(days=week_anchor.weekday())
    return [_date_str(monday + timedelta(days=i)) for i in range(7)]


def _month_dates(month_anchor: datetime) -> list[str]:
    import calendar
    y, m = month_anchor.year, month_anchor.month
    days = calendar.monthrange(y, m)[1]
    return [f"{y:04d}-{m:02d}-{d:02d}" for d in range(1, days + 1)]


def _year_months(year_anchor: datetime) -> list[str]:
    y = year_anchor.year
    return [f"{y:04d}-{m:02d}" for m in range(1, 13)]


def _aggregate_weekly(scope: str, week_anchor: datetime) -> Optional[dict]:
    week = _week_str(week_anchor)
    path = weekly_path(scope, week)
    existing = _read_json(path)
    if existing and existing.get("summary"):
        # 已结束的周冻结；本周仍在追加，继续用最新 daily 刷新
        if not _is_current_week(week_anchor):
            return existing

    dates = _week_dates(week_anchor)
    daily_summaries = []
    all_texts = []
    candidates: list[dict] = []
    for d in dates:
        daily = _read_json(daily_path(scope, d))
        if not daily:
            continue
        s = daily.get("summary") or ""
        if s:
            daily_summaries.append({"date": d, "summary": s})
        for m in daily.get("messages", []):
            all_texts.append(m.get("msg", ""))
            sc = _score_message(m)
            if sc > 0:
                candidates.append({
                    "score": sc, "speaker": m.get("speaker", ""),
                    "text": m.get("msg", ""), "date": d,
                })
    if not daily_summaries and not all_texts:
        return None

    keywords = _extract_keywords(all_texts, top_n=5)
    highlights = _select_highlights(candidates, WEEKLY_TOP)
    summary = f"本周有 {len(daily_summaries)} 天有记录"
    if keywords:
        summary += "，主要话题：" + "、".join(keywords[:4])
    if highlights:
        summary += "；本周重点：" + "、".join(
            h["text"][:24] for h in highlights[:3] if h.get("text")
        )
    summary += "。"

    data = {
        "week": week,
        "scope": scope,
        "date_range": [dates[0], dates[-1]],
        "summary": summary,
        "daily_summaries": daily_summaries,
        "highlights": highlights,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    _write_json(path, data)
    _touch_index(scope, "weekly", week)
    return data


def _aggregate_monthly(scope: str, month_anchor: datetime) -> Optional[dict]:
    month = _month_str(month_anchor)
    path = monthly_path(scope, month)
    existing = _read_json(path)
    if existing and existing.get("summary"):
        # 已结束的月冻结；本月仍在追加，继续刷新
        if not _is_current_month(month_anchor):
            return existing

    dates = _month_dates(month_anchor)
    daily_summaries = []
    all_texts = []
    candidates: list[dict] = []
    for d in dates:
        daily = _read_json(daily_path(scope, d))
        if not daily:
            continue
        s = daily.get("summary") or ""
        if s:
            daily_summaries.append({"date": d, "summary": s})
        for m in daily.get("messages", []):
            all_texts.append(m.get("msg", ""))
            sc = _score_message(m)
            if sc > 0:
                candidates.append({
                    "score": sc, "speaker": m.get("speaker", ""),
                    "text": m.get("msg", ""), "date": d,
                })
    # 并入当月各周已沉淀的 highlights（上层结论加权 1.2）
    for wk_off in range(0, -5, -1):
        wdata = _read_json(weekly_path(scope, _week_str(_offset_week(wk_off))))
        if not wdata:
            continue
        for h in wdata.get("highlights", []):
            candidates.append({
                "score": float(h.get("score", 1.0)) * 1.2,
                "speaker": h.get("speaker", ""),
                "text": h.get("text", ""),
                "date": h.get("date", ""),
            })
    if not daily_summaries and not all_texts:
        return None

    keywords = _extract_keywords(all_texts, top_n=5)
    highlights = _select_highlights(candidates, MONTHLY_TOP)
    summary = f"本月有 {len(daily_summaries)} 天有记录"
    if keywords:
        summary += "，主要话题：" + "、".join(keywords[:4])
    if highlights:
        summary += "；本月重点：" + "、".join(
            h["text"][:24] for h in highlights[:3] if h.get("text"))
    summary += "。"

    data = {
        "month": month,
        "scope": scope,
        "daily_summaries": daily_summaries,
        "summary": summary,
        "highlights": highlights,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    _write_json(path, data)
    _touch_index(scope, "monthly", month)
    return data


def _aggregate_yearly(scope: str, year_anchor: datetime) -> Optional[dict]:
    year = _year_str(year_anchor)
    path = yearly_path(scope, year)
    existing = _read_json(path)
    if existing and existing.get("summary"):
        # 已结束的年冻结；今年仍在记录，继续刷新
        if not _is_current_year(year_anchor):
            return existing

    months = _year_months(year_anchor)
    month_summaries = []
    candidates: list[dict] = []
    for mth in months:
        m = _read_json(monthly_path(scope, mth))
        if not m:
            try:
                ma = datetime.strptime(mth + "-01", "%Y-%m-%d")
                m = _aggregate_monthly(scope, ma)
            except Exception:
                m = None
        if m and m.get("summary"):
            month_summaries.append({"month": mth, "summary": m["summary"]})
        if m:
            for h in m.get("highlights", []):
                candidates.append({
                    "score": float(h.get("score", 1.0)) * 1.3,
                    "speaker": h.get("speaker", ""),
                    "text": h.get("text", ""),
                    "month": mth,
                })
    if not month_summaries:
        return None

    highlights = _select_highlights(candidates, YEARLY_TOP)
    summary = f"全年有 {len(month_summaries)} 个月有记录。"
    if highlights:
        summary += "全年重点：" + "、".join(
            h["text"][:24] for h in highlights[:3] if h.get("text"))
    data = {
        "year": year,
        "scope": scope,
        "month_summaries": month_summaries,
        "summary": summary,
        "highlights": highlights,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    _write_json(path, data)
    _touch_index(scope, "yearly", year)
    return data


# ---------------------------------------------------------------------------
# 意图识别
# ---------------------------------------------------------------------------
_DATE_ISO_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_DATE_CN_RE = re.compile(r"(\d{1,2})月(\d{1,2})[日号]")
_MONTHS_AGO_RE = re.compile(r"(\d+|[零一二三四五六七八九十两]+)个?月前")
_WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日",
               "星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

_CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _cn_to_int(s: str) -> int:
    """把阿拉伯/中文数字（含 十、十二、二十）转成 int，失败返回 1。"""
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    if "十" in s:
        left, _, right = s.partition("十")
        tens = _CN_NUM.get(left, 1) if left else 1
        ones = _CN_NUM.get(right, 0) if right else 0
        return tens * 10 + ones
    return _CN_NUM.get(s, 1)


def _parse_explicit_date(text: str) -> Optional[str]:
    m = _DATE_ISO_RE.search(text)
    if m:
        y, mo, d = m.groups()
        try:
            return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
        except Exception:
            return None
    m = _DATE_CN_RE.search(text)
    if m:
        mo, d = m.groups()
        y = _today().year
        try:
            return f"{y:04d}-{int(mo):02d}-{int(d):02d}"
        except Exception:
            return None
    return None


def detect_memory_intent(text: str, is_private: bool = False) -> Optional[dict]:
    """识别用户是否在“回忆”，返回 intent dict 或 None。

    intent 字段：
      level: daily | weekly | monthly | yearly | fuzzy
      offset: 相对偏移
      date: 显式日期（level=daily 时）
      target: private | group | both
      query: 模糊回忆时的原始文本
      all_years: True 表示遍历所有年份
    """
    if not text:
        return None
    t = text.strip()
    if not t:
        return None

    target = "both" if is_private else "group"
    if is_private:
        if any(w in t for w in ["群里", "群友", "群聊", "那个群", "各个群"]):
            target = "group"
        elif any(w in t for w in ["我们", "我和你", "私聊", "你和我"]):
            target = "private"

    # 具体日期
    explicit = _parse_explicit_date(t)
    if explicit:
        return {"level": "daily", "date": explicit, "offset": 0, "target": target}

    # 明确过去时间：直接触发
    # N 个月前（三个月前 / 两个月前 / 10 个月前）
    ma = _MONTHS_AGO_RE.search(t)
    if ma:
        n = max(1, _cn_to_int(ma.group(1)))
        return {"level": "monthly", "offset": -n, "target": target}

    if any(w in t for w in ["昨天", "昨晚", "昨日"]):
        return {"level": "daily", "offset": -1, "target": target}
    if "大前天" in t:
        return {"level": "daily", "offset": -3, "target": target}
    if "前天" in t:
        return {"level": "daily", "offset": -2, "target": target}

    # 上周 / 上周三、上周五（落到上一周，明细由 weekly 层聚合 + fuzzy 补充）
    if any(w in t for w in ["上周", "上个星期", "上一周"]):
        return {"level": "weekly", "offset": -1, "target": target}

    if any(w in t for w in ["上个月", "上月", "最近一个月", "近一个月", "这一个月"]):
        return {"level": "monthly", "offset": -1, "target": target}
    if "前年" in t:
        return {"level": "yearly", "offset": -2, "target": target}
    if "去年" in t:
        return {"level": "yearly", "offset": -1, "target": target}
    if any(w in t for w in ["很久以前", "最早", "最开始", "几年前", "多年前", "从前"]):
        return {"level": "yearly", "offset": 0, "target": target, "all_years": True}

    # 当前时段：时段词命中即按当前层回忆最近上下文（就近回忆，成本低）
    if any(w in t for w in ["今天", "刚才", "刚刚", "今日", "今早", "今晚"]):
        return {"level": "daily", "offset": 0, "target": target}
    if any(w in t for w in ["这周", "本周", "这个星期", "最近几天", "这几天", "最近一周", "这一星期"]):
        return {"level": "weekly", "offset": 0, "target": target}
    if any(w in t for w in ["这个月", "本月"]):
        return {"level": "monthly", "offset": 0, "target": target}
    if any(w in t for w in ["今年", "这一年"]):
        return {"level": "yearly", "offset": 0, "target": target}

    # 模糊回忆（上次聊到 / 之前说过 / 那天说的 / 以前聊过 / 之前提到……）
    if any(w in t for w in ["还记得", "上次", "之前", "那天", "以前", "记不记得",
                            "上次聊到", "之前说过", "那天说的", "以前聊过", "之前提到",
                            "上次说", "说过", "聊过"]):
        return {"level": "fuzzy", "offset": 0, "target": target, "query": t}

    return None


# ---------------------------------------------------------------------------
# recall
# ---------------------------------------------------------------------------
def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 20] + "……（更多记忆已省略）"


def _read_daily(scope: str, date: str) -> Optional[dict]:
    daily = _read_json(daily_path(scope, date))
    if not daily:
        return None
    return {
        "scope": scope,
        "date": date,
        "summary": daily.get("summary") or "",
        "messages": daily.get("messages") or [],
        "stats": daily.get("stats") or {},
    }


def _read_weekly(scope: str, anchor: datetime) -> Optional[dict]:
    data = _read_json(weekly_path(scope, _week_str(anchor)))
    if not data:
        data = _aggregate_weekly(scope, anchor)
    return data


def _read_monthly(scope: str, anchor: datetime) -> Optional[dict]:
    data = _read_json(monthly_path(scope, _month_str(anchor)))
    if not data:
        data = _aggregate_monthly(scope, anchor)
    return data


def _read_yearly(scope: str, anchor: datetime) -> Optional[dict]:
    data = _read_json(yearly_path(scope, _year_str(anchor)))
    if not data:
        data = _aggregate_yearly(scope, anchor)
    return data


def _collect_for_scope(scope: str, intent: dict, include_messages: bool) -> list[str]:
    out: list[str] = []
    level = intent.get("level")
    offset = intent.get("offset", 0)

    if level == "daily":
        date = intent.get("date") or _date_str(_offset_date(offset))
        daily = _read_daily(scope, date)
        if daily:
            if daily["summary"]:
                out.append(f"【{date}】{daily['summary']}")
            if include_messages:
                for m in daily["messages"][-20:]:
                    out.append(f"  {m.get('speaker', '?')}: {m.get('msg', '')}")

    elif level == "weekly":
        data = _read_weekly(scope, _offset_week(offset))
        if data:
            if data.get("summary"):
                out.append(f"【{data.get('week', '?')}】{data['summary']}")
            if include_messages:
                for d in data.get("daily_summaries", [])[:7]:
                    out.append(f"  {d.get('date', '?')}: {d.get('summary', '')}")

    elif level == "monthly":
        data = _read_monthly(scope, _offset_month(offset))
        if data:
            if data.get("summary"):
                out.append(f"【{data.get('month', '?')}】{data['summary']}")
            if include_messages:
                for d in data.get("daily_summaries", [])[:31]:
                    out.append(f"  {d.get('date', '?')}: {d.get('summary', '')}")

    elif level == "yearly":
        if intent.get("all_years"):
            index = _load_index()
            years = index.get("scopes", {}).get(scope, {}).get("years", [])
            for y in years:
                try:
                    anchor = datetime(int(y), 1, 1)
                except Exception:
                    continue
                data = _read_yearly(scope, anchor)
                if data and data.get("summary"):
                    out.append(f"【{y}】{data['summary']}")
        else:
            data = _read_yearly(scope, _offset_year(offset))
            if data:
                if data.get("summary"):
                    out.append(f"【{data.get('year', '?')}】{data['summary']}")
                if include_messages:
                    for m in data.get("month_summaries", []):
                        out.append(f"  {m.get('month', '?')}: {m.get('summary', '')}")

    elif level == "fuzzy":
        query = (intent.get("query") or "").strip()
        if not query:
            return out
        keywords = [w for w in _CHINESE_RE.findall(query) if 2 <= len(w) <= 6][:5]
        if not keywords:
            return out
        # 同义/别名扩展：「打拳」也要能查到当年写的是「功夫」的记录（本地表，不花 token）
        try:
            import alias_map  # noqa: E402
            _expanded = set(keywords) | alias_map.expand_from_text(query, min_len=2)
        except Exception:  # noqa: BLE001
            _expanded = set(keywords)

        def _match(text: str) -> bool:
            t = text or ""
            return any(kw in t for kw in _expanded)

        # 1) 近 7 天 daily 明细
        daily_hits = 0
        for off in range(0, -7, -1):
            date = _date_str(_offset_date(off))
            daily = _read_daily(scope, date)
            if not daily:
                continue
            matched = [m for m in daily["messages"] if _match(m.get("msg", ""))]
            if not matched:
                continue
            out.append(f"【{date}】命中 {len(matched)} 条")
            if include_messages:
                for m in matched[:3]:
                    out.append(f"  {m.get('speaker', '?')}: {m.get('msg', '')}")
            daily_hits += 1
            if daily_hits >= 2:
                break

        # 2) 明细找不够 → 下钻近 3 周 weekly 摘要 / highlights
        if daily_hits < 2:
            weekly_hits = 0
            for wk in range(0, -3, -1):
                data = _read_weekly(scope, _offset_week(wk))
                if not data:
                    continue
                parts = [data.get("summary", "")]
                parts += [d.get("summary", "") for d in data.get("daily_summaries", [])]
                parts += [str(h) for h in data.get("highlights", [])]
                if not any(_match(p) for p in parts):
                    continue
                out.append(f"【{data.get('week', '?')}·本周回忆】{data.get('summary', '')}")
                if include_messages:
                    for d in data.get("daily_summaries", [])[:3]:
                        if _match(d.get("summary", "")):
                            out.append(f"  {d.get('date', '?')}: {d.get('summary', '')}")
                weekly_hits += 1
                if weekly_hits >= 2:
                    break
        else:
            weekly_hits = 0

        # 3) 周也没命中 → 下钻近 3 月 monthly 摘要
        if daily_hits < 1 and weekly_hits < 1:
            for mo in range(0, -3, -1):
                data = _read_monthly(scope, _offset_month(mo))
                if not data:
                    continue
                parts = [data.get("summary", "")]
                parts += [d.get("summary", "") for d in data.get("daily_summaries", [])]
                if not any(_match(p) for p in parts):
                    continue
                out.append(f"【{data.get('month', '?')}·本月回忆】{data.get('summary', '')}")
                break

    return out


def _active_group_scopes(limit: int = MAX_ACTIVE_GROUPS) -> list[str]:
    index = _load_index()
    scopes = index.get("scopes", {})
    groups = [
        (s, info.get("last_active", ""))
        for s, info in scopes.items()
        if s.startswith("group_")
    ]
    groups.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in groups[:limit]]


def recall(scope: str, intent: dict, allow_group: bool = False,
           max_chars: int = DEFAULT_MAX_CHARS,
           include_messages: bool = False) -> str:
    """按 scope 和 intent 读记忆。

    - 群聊：scope=group_{gid}，allow_group=False，只读本群。
    - 私聊：scope=private_{uid}，allow_group=True 时可追加群聊摘要。
    """
    if not intent:
        return ""
    target = intent.get("target", "both")
    parts: list[str] = []

    # 1) 读本 scope
    read_self = True
    if scope.startswith("private_") and target == "group" and allow_group:
        read_self = False
    if read_self:
        parts.extend(_collect_for_scope(scope, intent, include_messages))

    # 2) 私聊 + 允许读群聊时，读活跃群的摘要（不读明细）
    if allow_group and scope.startswith("private_") and target in ("group", "both"):
        for gscope in _active_group_scopes():
            gparts = _collect_for_scope(gscope, intent, include_messages=False)
            if gparts:
                gid = gscope[len("group_"):]
                parts.append(f"--- 群 {gid} 的记忆 ---")
                parts.extend(gparts)

    if not parts:
        return ""
    return _truncate("\n".join(parts), max_chars)


# ---------------------------------------------------------------------------
# 手动查询
# ---------------------------------------------------------------------------
def list_scopes() -> list[str]:
    return list(_load_index().get("scopes", {}).keys())


def get_daily_summary(scope: str, date: str) -> Optional[str]:
    daily = _read_daily(scope, date)
    return daily["summary"] if daily else None