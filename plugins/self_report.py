"""自我报告：让她在回复末尾自评一句「这轮我感觉如何」，再剥掉不显示。

来源：LIA-Assistant 的 Psyche Engine（ADR-068）用的办法 ——
让回复的模型自己在末尾吐一个结构化标签，解析后从展示文本里删掉。
**不额外调模型**，只多二十来个输出 token，但拿到的是"这一轮她真实的感觉"，
比全局激素算出来的准（激素不知道"刚才那句话逗到她了"）。

我们的标签用双尖括号，模型不太可能在正文里自然产出，剥离也更安全：

    <<E p=0.4 a=0.2 d=0.3 e=被逗到>>

p/a/d 是 −1…1 的三个轴，e 是一个词。
解析后写进 memory/self_report.jsonl，最近 30 分钟内的会被 pad_now() 融合进 PAD。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "memory" / "self_report.jsonl"

# 双尖括号 + E 开头；容错空格、大小写、中文引号
_EVAL_RE = re.compile(r"<<\s*E\s+([^<>\n]{1,160})>>", re.I)
# 兜底：模型可能只写一半，或者用方括号
_LOOSE_RE = re.compile(r"[<＜]{1,2}\s*E\s+([^<＞>\n]{1,160})[>＞]{1,2}", re.I)

# 拼进 prompt 的格式要求（只加在 volatile 段末尾，不进 stable，不破坏前缀缓存）
EVAL_HINT = (
    "\n【收尾自评】回答完最后另起一行，用一行写你此刻的真实感觉，格式严格照抄：\n"
    "<<E p=愉悦 a=唤醒 d=掌控 e=一个词>>\n"
    "p/a/d 用 −1 到 1 的小数（这一轮你自己的感觉，不是评价我）；e 是两三个字的词（如 被逗到 / 有点酸 / 想撒娇）。\n"
    "这一行不会显示给对方，只用来记录。"
)

# 这些词说明"自评"被模型拿来评价用户了，不是它自己的感觉
_BAD_WORDS = ("你", "对方", "用户", "主人")


def _f(x: str, lo: float = -1.0, hi: float = 1.0) -> float:
    try:
        v = float(str(x).strip())
    except (TypeError, ValueError):
        return 0.0
    return max(lo, min(hi, v))


def _parse_fields(body: str) -> dict:
    """从 `p=0.4 a=0.2 d=0.3 e=被逗到` 里抠出四个值。"""
    out: dict[str, object] = {}
    for key in ("p", "a", "d"):
        m = re.search(rf"{key}\s*[=:：]\s*(-?\d+(?:\.\d+)?)", body, re.I)
        if m:
            out[key] = _f(m.group(1))
    m = re.search(r"e\s*[=:：]\s*([^\s,，;；]+)", body, re.I)
    if m:
        word = m.group(1).strip("「」\"'“” ")
        if word and not any(b in word for b in _BAD_WORDS):
            out["e"] = word[:8]
    return out


def strip(text: str) -> tuple[str, dict]:
    """从回复里剥离自评标签。返回 (干净文本, 评估dict)。

    干净文本永远可以安全展示 —— 剥离失败也不会把标签漏出去（两套正则 + 尾部清理）。
    """
    raw = str(text or "")
    got: dict = {}
    for pat in (_EVAL_RE, _LOOSE_RE):
        for m in pat.finditer(raw):
            if not got:
                parsed = _parse_fields(m.group(1))
                if parsed:
                    got = parsed
        raw = pat.sub("", raw)
    # 万一模型写成了不带符号的纯文本行，末尾再擦一遍
    if not got:
        tail = re.search(r"<<\s*E\s+(.*)$", raw, re.I)
        if tail:
            parsed = _parse_fields(tail.group(1))
            if parsed:
                got = parsed
            raw = raw[: tail.start()]
    cleaned = re.sub(r"\n{3,}", "\n\n", raw).strip()
    # 最后一道：任何残留的双尖括号整段扔掉
    cleaned = re.sub(r"<<[^<>]{0,200}>>", "", cleaned).strip()
    return cleaned, got


def note(evalu: dict, scope: str = "") -> None:
    """把一次自评写进 jsonl。"""
    if not evalu or not ({"p", "a", "d"} & set(evalu)):
        return
    try:
        row = {
            "at": time.time(),
            "p": float(evalu.get("p") or 0),
            "a": float(evalu.get("a") or 0),
            "d": float(evalu.get("d") or 0),
            "e": str(evalu.get("e") or ""),
            "scope": str(scope or ""),
        }
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def recent(minutes: float = 30.0, scope: str = "") -> dict | None:
    """最近一次自评（默认 30 分钟内）。越近权重越高由调用方处理。"""
    if not LOG.exists():
        return None
    cutoff = time.time() - minutes * 60
    best: dict | None = None
    try:
        for ln in LOG.read_text(encoding="utf-8").splitlines()[-60:]:
            if not ln.strip():
                continue
            try:
                r = json.loads(ln)
            except Exception:  # noqa: BLE001
                continue
            if float(r.get("at") or 0) < cutoff:
                continue
            if scope and str(r.get("scope")) not in ("", scope):
                continue
            best = r
    except Exception:  # noqa: BLE001
        return None
    return best


def blend(global_pad: dict, scope: str = "", weight: float = 0.35) -> dict:
    """把最近自评混进全局 PAD。

    自评比全局激素更"现场"，但它只覆盖「这一轮」，
    所以只给 weight（默认 0.35）—— 大局还是激素说了算。
    """
    base = dict(global_pad or {})
    r = recent(scope=scope)
    if not r:
        return base
    age_min = max(0.0, (time.time() - float(r.get("at") or 0)) / 60.0)
    # 30 分钟内线性衰减到 half；越久越轻
    fresh = max(0.0, 1.0 - age_min / 30.0)
    w = weight * fresh
    if w <= 0.02:
        return base
    out = {}
    for ax, key in (("p", "P"), ("a", "A"), ("d", "D")):
        g = float(base.get(key) or 0)
        s = float(r.get(ax) or 0)
        out[key] = round(g * (1 - w) + s * w, 3)
    if r.get("e"):
        out["e"] = str(r["e"])
    out["自评"] = w
    return out
