"""说话方式画像：不是学「用什么词」，是学「怎么说话」。

2026-11-08 移植自 MaiBot 的「模仿其他人的说话风格」。

和 group_lexicon 的区别：
  group_lexicon  词条（YYDS、绷不住了）—— 学"词"
  speech_style   句式（短句、不用标点、爱问句）—— 学"怎么说"

用法：
    from plugins import speech_style as ss
    ss.observe(user_id, "还在不")          # 每条消息喂一次（轻量，纯统计）
    ss.brief(user_id)                       # 拼进 prompt 的一句话
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILE = ROOT / "memory" / "speech_style.json"
MAX_SAMPLES = 6          # 每个人留几句典型原话
MIN_N = 8                # 少于这么多条不画像（样本太薄）

_STATE: dict | None = None


def _load() -> dict:
    global _STATE
    if _STATE is not None:
        return _STATE
    try:
        _STATE = json.loads(FILE.read_text(encoding="utf-8"))
        if not isinstance(_STATE, dict):
            _STATE = {}
    except Exception:  # noqa: BLE001
        _STATE = {}
    return _STATE


def _save() -> None:
    try:
        d = _load()
        # 只留 200 个最近活跃的人
        if len(d) > 200:
            keep = sorted(d.items(), key=lambda kv: kv[1].get("last") or 0, reverse=True)[:200]
            d = dict(keep)
            global _STATE
            _STATE = d
        FILE.parent.mkdir(parents=True, exist_ok=True)
        FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


_WRAP_RE = re.compile(r"^【当前说话人：[^】]*】\s*(Ta说：)?\s*", re.UNICODE)


def _clean(text: str) -> str:
    """去掉群聊包装前缀（【当前说话人：xxx】
Ta说：），只留真正的原话。"""
    return _WRAP_RE.sub("", str(text or "").strip())


def _stats(text: str) -> dict:
    """一条消息的形态特征。"""
    t = _clean(text)
    n = len(t)
    if not n:
        return {}
    return {
        "len": n,
        "q": t.count("?") + t.count("？"),
        "excl": t.count("!") + t.count("！"),
        "ell": t.count("...") + t.count("…"),
        "comma": t.count(",") + t.count("，"),
        "period": t.count(".") + t.count("。"),
        "no_punct": 1 if (t.count("，") + t.count("？") + t.count("。") + t.count("！") + t.count(",")) == 0 else 0,
        "emoji": 1 if re.search(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", t) else 0,
    }


def observe(user_id: str, text: str) -> None:
    """每条用户消息喂一次。纯本地统计，不调模型、不打日志。"""
    uid = str(user_id or "").strip()
    st = _stats(text)
    if not uid or not st:
        return
    d = _load()
    e = d.get(uid) or {
        "n": 0, "sum_len": 0, "q": 0, "excl": 0, "ell": 0,
        "no_punct": 0, "emoji": 0, "samples": [], "last": 0,
    }
    e["n"] = int(e.get("n") or 0) + 1
    e["sum_len"] = int(e.get("sum_len") or 0) + st["len"]
    for k in ("q", "excl", "ell", "no_punct", "emoji"):
        if st.get(k):
            e[k] = int(e.get(k) or 0) + 1
    e["last"] = time.time()
    # 典型原话：只留 2~40 字的（太短没信息，太长不像日常）
    if 2 <= st["len"] <= 40 and len(e.get("samples") or []) < MAX_SAMPLES:
        s = _clean(text)
        if s not in (e.get("samples") or []):
            e.setdefault("samples", []).append(s)
    d[uid] = e
    # 每 10 条落一次盘，别每条都写
    if e["n"] % 10 == 0:
        _save()


def profile(user_id: str) -> dict | None:
    """算出一个人的画像；样本不足返回 None。"""
    d = _load()
    e = d.get(str(user_id or ""))
    if not e or int(e.get("n") or 0) < MIN_N:
        return None
    n = max(1, int(e["n"]))
    return {
        "n": n,
        "avg_len": round(int(e.get("sum_len") or 0) / n, 1),
        "q_rate": round(int(e.get("q") or 0) / n, 2),
        "excl_rate": round(int(e.get("excl") or 0) / n, 2),
        "ell_rate": round(int(e.get("ell") or 0) / n, 2),
        "no_punct_rate": round(int(e.get("no_punct") or 0) / n, 2),
        "emoji_rate": round(int(e.get("emoji") or 0) / n, 2),
        "samples": (e.get("samples") or [])[-3:],
    }


def describe(p: dict) -> str:
    """把人话说成一句画像描述。"""
    if not p:
        return ""
    tags: list[str] = []
    al = p["avg_len"]
    if al <= 8:
        tags.append("极短句（常常四五个字）")
    elif al <= 16:
        tags.append("短句")
    elif al <= 30:
        tags.append("中等长度")
    else:
        tags.append("爱说长句")
    if p["no_punct_rate"] >= 0.5:
        tags.append("几乎不用标点，用空格断句")
    elif p["no_punct_rate"] >= 0.25:
        tags.append("经常省标点")
    if p["q_rate"] >= 0.25:
        tags.append("爱用问号")
    if p["excl_rate"] >= 0.2:
        tags.append("爱用感叹号")
    if p["ell_rate"] >= 0.15:
        tags.append("爱用省略号")
    if p["emoji_rate"] >= 0.3:
        tags.append("常带表情")
    if not tags:
        tags.append("普通口语")
    return "、".join(tags)


def brief(user_id: str, name: str = "对方") -> str:
    """拼进 prompt 的一段。样本不足返回空串。"""
    p = profile(user_id)
    if not p:
        return ""
    s = describe(p)
    out = [f"【{name}怎么说话】{s}（样本 {p['n']} 条，均长 {p['avg_len']} 字）"]
    if p["samples"]:
        quoted = "」/「".join(str(x)[:28] for x in p["samples"])
        out.append(f"原话参考：「{quoted}」")
    out.append(
        "规则：跟着这个节奏走——他短你就别长，他不带标点你也别端着；"
        "但别学他说脏话或模仿内容，只学长短和标点。"
    )
    return "\n".join(out)
