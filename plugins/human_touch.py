"""让她打字更像真人：长度随机、偶尔敷衍、偶尔补一句。

只碰"发送层"，不改人设、不改模型、不改记忆。

三个策略（2026-10-28 用户确认）：
  ① 长度随机化：有时 1 个词，有时两三行，不再条条一样长
  ② 偶尔敷衍：约 12% 概率把回复压成几个字（"嗯""行吧""就这"）
  ③ 偶尔补一句：约 15% 概率隔一两秒再接一条短的（"对了…"/"话说…"）

全部走本地词库，不额外调模型，不花 token。
"""
from __future__ import annotations

import os
import random
import re

# ---- ① 敷衍短答（按语气分三档，随机挑） ----
TERSE_POOL = [
    "嗯", "嗯嗯", "哦", "行吧", "行", "好", "懂了", "确实", "有理", "就这", "笑死", "6",
    "啊？", "？", "……", "呃", "哈", "这样啊", "呃啊", "走", "睡觉", "不聊了",
]

# ---- ③ 补一句的开头（补的内容从回复里截，或者用固定小尾巴） ----
TERSE_CHANCE = float(os.getenv("HUMAN_TERSE", "0") or "0")   # 默认 0：不把回复压成两个字

FOLLOWUP_OPENERS = ["对了，", "话说，", "啊对，", "哦还有，", "等下，"]

FOLLOWUP_TAILS = [
    "别熬夜啊。",
    "我先忙别的了。",
    "这个回头再聊。",
    "算了，没事。",
    "你今天话挺多的。",
]

# 表情/动作前缀，比如 (*尾巴一僵*)——分层时先摘掉再判断
_ACTION_RE = re.compile(r"^[（(][^）)]{1,20}[）)]\s*")
_TAIL_PUNCT = "。！!？?，,、；;~～… "


def _strip_action(text: str) -> str:
    return _ACTION_RE.sub("", text or "").strip()


def _split_lines(text: str) -> list[str]:
    """按换行 + 标点切，尽量保持语义完整（每段 6~22 字）。"""
    text = text.replace("\r", "")
    out: list[str] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if len(line) <= 24:
            out.append(line)
            continue
        parts = re.split(r"(?<=[。！？!?…~～])", line)
        buf = ""
        for p in parts:
            if not p:
                continue
            if len(buf) + len(p) <= 24:
                buf += p
            else:
                if buf.strip():
                    out.append(buf.strip())
                buf = p
        if buf.strip():
            out.append(buf.strip())
    return out or ([text.strip()] if text.strip() else [])


def shape(text: str, *, allow_terse: bool = True, allow_merge: bool = True) -> list[str]:
    """把模型回复整形成"像人打字"的若干条。

    · 12%：敷衍成很短的一句（只在前文不是问题时，避免答非所问太明显）
    · 25%：把多段合并成 1~2 条（真人不是每次都分行）
    · 其余：正常按标点分条
    """
    body = (text or "").strip()
    if not body:
        return []

    core = _strip_action(body)
    if allow_terse and core and len(core) >= 18 and random.random() < TERSE_CHANCE:
        return [random.choice(TERSE_POOL)]

    lines = _split_lines(body)
    if not lines:
        return [body]

    if allow_merge and len(lines) > 2 and random.random() < 0.25:
        keep = random.randint(1, 2)
        merged = "".join(lines)
        if keep == 1:
            return [merged]
        cut = max(1, len(merged) // 2)
        return [merged[:cut].strip(_TAIL_PUNCT) or merged[:cut], merged[cut:].strip()]

    # 太整齐了也不好：随机把最后一条并进上一条
    if len(lines) >= 3 and random.random() < 0.2:
        lines = lines[:-2] + [lines[-2] + lines[-1]]
    return _dedupe_lines(lines)


def _dedupe_lines(lines: list[str]) -> list[str]:
    """去掉重复/互相包含的段落（她有时会把自己第一句又说一遍）。"""
    out: list[str] = []
    for ln in lines:
        core = ln.strip(_TAIL_PUNCT)
        if not core:
            continue
        dup = False
        for keep in out:
            kcore = keep.strip(_TAIL_PUNCT)
            if core == kcore or (len(core) >= 6 and core in kcore) or (len(kcore) >= 6 and kcore in core):
                dup = True
                # 保留更长的那条
                if len(core) > len(kcore):
                    out[out.index(keep)] = ln
                break
        if not dup:
            out.append(ln)
    return out


FOLLOWUP_CHANCE = float(os.getenv("HUMAN_FOLLOWUP_CHANCE", "0") or "0")


def maybe_followup(text: str, *, chance: float | None = None) -> str | None:
    """默认关闭（HUMAN_FOLLOWUP_CHANCE=0）：固定尾巴没有上下文，读起来像人机。

    需要恢复时在 .env 里写 HUMAN_FOLLOWUP_CHANCE=0.15。
    """
    if chance is None:
        chance = FOLLOWUP_CHANCE
    if chance <= 0:
        return None
    if random.random() >= chance:
        return None
    body = _strip_action(text)
    if not body or len(body) < 10:
        return None
    # 只补一句"新的话"，绝不复述刚说过的内容（之前会挑原话开头，看起来像重复发送）
    tail = random.choice(FOLLOWUP_TAILS)
    if tail in body or body[:8] in tail:
        return None
    return tail
