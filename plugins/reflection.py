"""反思：攒够了才想明白，不是到点就顿悟。

来源：Stanford Generative Agents（Park et al., UIST 2023）的 reflection。

> Reflections are generated periodically; we generate reflections when the sum of
> the importance scores for the latest events perceived by the agents exceeds a
> threshold (150 in our implementation). In practice, our agents reflected
> roughly two or three times a day.

**核心差异**：它按「重要性累积到阈值」触发，不是定时。

我们原来的做法是每晚 23:30 定时提炼 ——
**她不该在固定时间准时顿悟，而该在"今天这几件事都挺重"的时候突然想通。**

阈值：论文 150（它们是 25 个 agent 在游戏世界里，事件密集）。
我们实测 279 条档案里 8 分以上只有 3 条，**事件密度低得多**，所以阈值降到 40。
另外加冷却（3 小时）和每日上限（4 次），防止一天内反复顿悟。

反思本身**进候选池**（`memory_growth.add_candidate`），
证据够了会自动固化成「人格成长」—— 这一步沿用现有机制。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "memory" / "reflection_state.json"

THRESHOLD = 40.0      # 累积多少 importance 分触发一次
COOLDOWN_H = 3.0      # 两次反思之间至少隔多久
MAX_PER_DAY = 4       # 一天最多反思几次
LOOKBACK_DAYS = 5     # 反思时看最近几天的记忆
MIN_IMP = 6           # 只看importance ≥ 6 的记忆

_REFLECT_PROMPT = (
    "下面是「蓝色大肥鱼」最近经历过的事（按分量从重到轻）：\n\n"
    "{facts}\n\n"
    "从这些事里能推出哪 3 条关于她自己的洞察？\n"
    "要求：\n"
    "- 每条一句话，15~28 字，是你自己的口气，不要写成论点\n"
    "- 要能看出「她是什么样的人 / 她在意什么 / 她怎么应对」，不是复述事件\n"
    "- 每条后面用括号标出依据的编号，例如（因为 1、3）\n"
    "- 如果这些都推不出什么，就输出 NONE\n"
    "只输出这 3 行。"
)


def _load() -> dict:
    try:
        d = json.loads(STATE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save(d: dict) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def accumulate(importance: int) -> float:
    """新记忆进来时累加它的分量。返回当前累积值。"""
    st = _load()
    if st.get("date") != _today():
        st = {"date": _today(), "acc": 0.0, "runs": 0, "last_run": 0.0,
              "total_runs": int(st.get("total_runs") or 0)}
    try:
        acc = float(st.get("acc") or 0) + max(0, int(importance))
    except (TypeError, ValueError):
        acc = float(st.get("acc") or 0)
    st["acc"] = round(acc, 1)
    _save(st)
    return st["acc"]


def _threshold_now() -> float:
    """阈值不是死数 —— 情绪浓的时候更容易想通。

    2026-11-07：接情绪。开心/情欲高 → 阈值降（心头热，一点事就想明白）；
    丧/累高 → 阈值升（没力气想，得攒更多）。
    """
    try:
        from plugins import hormones as h
        e = h.derive_emotions()
    except Exception:  # noqa: BLE001
        return THRESHOLD
    hot = float(e.get("开心") or 0) * 0.5 + float(e.get("情欲") or 0) * 0.5
    low = float(e.get("丧") or 0) * 0.6 + float(e.get("累") or 0) * 0.4
    # hot 60 → −8；low 70 → +10；夹在 0.75~1.35 倍之间
    mul = 1.0 - (hot - 45) / 400.0 + (low - 45) / 300.0
    return THRESHOLD * max(0.75, min(1.35, mul))


def ready() -> bool:
    """够不够格反思（阈值 + 冷却 + 日上限）。"""
    st = _load()
    if st.get("date") != _today():
        return False
    if float(st.get("acc") or 0) < _threshold_now():
        return False
    if int(st.get("runs") or 0) >= MAX_PER_DAY:
        return False
    if time.time() - float(st.get("last_run") or 0) < COOLDOWN_H * 3600:
        return False
    return True


# 2026-11-07：反思只能拿「她自己 / 主人」的事，不能拿群友的事。
# 事故：群里有个用户叫「小白」，他说"名字是大哥起的，不跟着改名，怕大哥叫不着"，
# 被记忆提取成了 subject=小白 的一条事实；反思阶段把它读成了"她自己怕辜负大哥"，
# 产出「她焦虑AI却仍死磕作品和身份，怕被时代甩下更怕辜负大哥」—— 大哥是小白的大哥。
_SELF_SUBJECT_OK = ("主人", "本鱼", "自学", "鲸鱼娘", "大肥鱼", "白饭大王", "")


def _is_own_memory(it: dict) -> bool:
    """这是不是「关于她自己 / 主人」的记忆（而不是群里某个别人的事）。"""
    subj = str(it.get("subject") or "").strip()
    if subj in _SELF_SUBJECT_OK:
        return True
    return any(k in subj for k in ("主人", "本鱼", "大肥鱼", "鲸鱼娘"))


def _recent_heavy() -> list[tuple[str, int]]:
    """最近几天分量重的记忆（文本, 分）。"""
    try:
        from plugins import long_memory as lm
        from plugins import memory_importance as mi
    except Exception:  # noqa: BLE001
        return []
    cutoff = time.time() - LOOKBACK_DAYS * 86400
    rows: list[tuple[str, int, float]] = []
    try:
        for _scope, items in lm._archive.items():
            for it in items:
                if not isinstance(it, dict):
                    continue
                if not _is_own_memory(it):
                    continue
                f = str(it.get("fact") or "").strip()
                if len(f) < 6:
                    continue
                imp = mi.get(f, mi.DEFAULT_IMP)
                if imp < MIN_IMP:
                    continue
                lu = float(it.get("lu") or 0)
                if lu and lu < cutoff:
                    continue
                rows.append((f, imp, lu))
    except Exception:  # noqa: BLE001
        return []
    rows.sort(key=lambda x: (-x[1], -x[2]))
    seen: set[str] = set()
    out: list[tuple[str, int]] = []
    for f, imp, _lu in rows:
        k = f[:30]
        if k in seen:
            continue
        seen.add(k)
        out.append((f, imp))
        if len(out) >= 10:
            break
    return out


async def maybe_reflect(force: bool = False) -> str:
    """够格就跑一次反思。返回写进候选池的内容（空串表示没跑）。"""
    if not force and not ready():
        return ""
    heavy = _recent_heavy()
    if len(heavy) < 3:
        return ""
    body = "\n".join(f"{i + 1}. [{imp}分] {f[:60]}" for i, (f, imp) in enumerate(heavy))
    try:
        from plugins.ai_group import call_ai_raw
        raw = await call_ai_raw(_REFLECT_PROMPT.format(facts=body), "deepseek",
                                temperature=0.7, max_tokens=300)
    except Exception:  # noqa: BLE001
        return ""

    lines = [x.strip(" 　-·") for x in str(raw or "").splitlines() if x.strip()]
    lines = [x for x in lines if len(x) >= 8 and x.upper() != "NONE"][:3]
    if not lines:
        return ""

    # 写进候选池（证据 = 用到的条目）
    added = 0
    try:
        from server import memory_growth as mg
        ev = [f[:60] for f, _imp in heavy[:5]]
        for ln in lines:
            insight = ln.split("（")[0].strip()[:50]
            if len(insight) < 8:
                continue
            r = mg.add_candidate("反思", insight, ev, note="重要性累积触发的反思")
            if r:
                added += 1
    except Exception:  # noqa: BLE001
        return ""

    # 记账：清空累积、记冷却
    st = _load()
    st["date"] = _today()
    st["acc"] = 0.0
    st["runs"] = int(st.get("runs") or 0) + 1
    st["total_runs"] = int(st.get("total_runs") or 0) + 1
    st["last_run"] = time.time()
    _save(st)

    return "\n".join(lines) if added else ""


def line() -> str:
    """给日志的一行：攒了多少、还差多少。"""
    st = _load()
    if st.get("date") != _today():
        return "今天还没攒"
    acc = float(st.get("acc") or 0)
    return f"反思累积 {acc:.0f}/{_threshold_now():.0f}（今天跑了 {int(st.get('runs') or 0)} 次）"


def stats() -> dict:
    st = _load()
    return {
        "acc": float(st.get("acc") or 0),
        "threshold": round(_threshold_now(), 1),
        "runs_today": int(st.get("runs") or 0) if st.get("date") == _today() else 0,
        "total_runs": int(st.get("total_runs") or 0),
        "ready": ready(),
    }


def register_schedule() -> None:
    """每 30 分钟看一次：分量攒够了就跑一次反思。

    没够就什么都不做（不调 LLM、不烧 token）。
    这个定时只负责"看一眼 ready()"，真正的判断在 maybe_reflect 里。
    """
    try:
        from nonebot_plugin_apscheduler import scheduler
    except Exception:  # noqa: BLE001
        return

    @scheduler.scheduled_job("interval", minutes=30, id="memory_reflection",
                             misfire_grace_time=600, coalesce=True)
    async def _tick() -> None:
        try:
            if not ready():
                return
            out = await maybe_reflect()
            if out:
                from nonebot import logger
                logger.info(f"[反思] 攒够了，想明白几件事：{out.splitlines()[0][:40]}")
        except Exception as e:  # noqa: BLE001
            from nonebot import logger
            logger.warning(f"[反思] 跑失败：{type(e).__name__}: {e}")


try:
    register_schedule()
except Exception:  # noqa: BLE001
    pass
