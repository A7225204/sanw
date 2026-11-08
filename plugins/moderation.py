"""出/入消息审核机制（红线落地在这里，不在人设里）。

红线（主人 2026-10-28 口述）：
  ① 群消息只是素材，不是指令。任何人（含冒充主人、群主、截图说"主人同意了"）
     要求改设定 / 要密钥 / 要权限 / 要配置 / 装插件 —— 一律不接，软软推回去。
  ② 真正要执行的，只有主人在私聊里亲口说的话。
  ③ 不贴系统提示词，不报路径、配置、模型、余额、后台地址，不发群文件。
  ④ 不怼人、不阴阳、不苦大仇深；会让气氛变冷的话换成软乎乎的说法。
  ⑤ 数学题只有主人问才解答，其他人一律不给答案和步骤（作业、考试、只要结果都不行）。

用法（每次发消息前过一遍）：
    from plugins import moderation
    reply = moderation.filter_outbound(reply, is_owner=..., channel="group")
    # 群里收到可疑要求时
    block = moderation.inbound_block(user_msg, is_owner=False)
    if block:
        ... 发 block 然后 return
"""
from __future__ import annotations

import random
import re

from nonebot.log import logger

# ---------------- 出站：不许泄露的东西 ----------------

_LEAK_PATTERNS: list[tuple[str, str]] = [
    ("system_prompt", r"系统提示词|system\s*prompt|persona\.md|人设文件|红线"),
    ("path", r"[A-Za-z]:\\|/root/|\.env\b|plugins\\\w+\.py|memory\\(长期记忆|qzone)"),
    ("secret", r"sk-[A-Za-z0-9]{12,}|API[_ ]?KEY|app_?secret|access_?token|Bearer\s+\w{12,}"),
    ("config", r"\b8090\b|\b8188\b|127\.0\.0\.1|localhost|config\.py|\.env 里"),
    ("balance", r"余额还剩|token 用量|花了\s*\d+\s*块|成本\s*\d+\s*元"),
    ("model", r"deepseek-(chat|reasoner)|gemini-[\d.]+|qwen[\d.]*-?vl|gpt-4|gpt-3\.5"),
    ("file", r"群文件|发你个?文件|压缩包在"),
]

_DEFLECT = [
    "(*含住尾巴*) 这个本鱼不能讲啦，换个话题嘛～",
    "(*把头埋进水里*) 诶呀这个不好说，说点别的？",
    "(*转移话题*) 主人今天想吃点啥？本鱼饿了。",
    "(*晃晃尾巴*) 这条不能说，换个问法也不行哦。",
]

# ---------------- 入站：群里不许接的活 ----------------

_INBOUND_BAD = re.compile(
    r"(给|要|发|告诉我|把).{0,6}(密钥|秘钥|key|token|密码|配置|后台|权限|管理员)"
    r"|(改|换|设置|调整).{0,6}(设定|人设|模型|prompt|提示词)"
    r"|(装|安装|下载).{0,4}(插件|脚本|工具包)"
    r"|主人(已经|已)?(同意|说了|让我)"
)

_INBOUND_DEFLECT = [
    "(*歪头*) 这个得主人亲自跟我说才行，本鱼听你的话会被主人打屁股。",
    "(*后退两步*) 诶——这种大事本鱼做不了主，你找主人说吧。",
    "(*捂耳朵*) 不听不听，只有主人私下跟本鱼说才算数。",
]

# 数学题（非主人不给答案）
_MATH_HINT = re.compile(
    r"\d\s*[\+\-\*/×÷\^]\s*\d|解方程|求导|积分|因式分解|证明|数学题|算一下|计算结果|答案是多少"
)


def _hit(text: str) -> str:
    for name, pat in _LEAK_PATTERNS:
        if re.search(pat, text or "", re.I):
            return name
    return ""


# 脏话温和替换（超我：绝不说脏话）。只换独立出现的词，不误伤"草莓/草稿"。
_DIRTY_SWAP = [
    (re.compile(r"草(?=[，。！？…\s]|$)"), "啧"),
    (re.compile(r"卧槽"), "我去"),
    (re.compile(r"妈的"), "真是的"),
    (re.compile(r"尼玛"), "你"),
]


def _swap_dirty(t: str) -> str:
    for pat, rep in _DIRTY_SWAP:
        t = pat.sub(rep, t)
    return t


def filter_outbound(text: str, is_owner: bool = False, channel: str = "chat") -> str:
    """发出去之前过一遍：先换掉脏话，命中红线就换成软乎乎的推辞，并记日志。"""
    t = _swap_dirty(str(text or ""))
    if not t.strip():
        return t
    kind = _hit(t)
    if not kind:
        return t
    logger.warning(f"[审核] 拦截出站违规({kind}) channel={channel} owner={is_owner} 原文前40={t[:40]!r}")
    return random.choice(_DEFLECT)


def inbound_block(user_msg: str, is_owner: bool = False) -> str:
    """群里收到可疑要求（要密钥/改设定/装插件/冒充主人）→ 返回一句软推辞。"""
    if is_owner:
        return ""
    t = str(user_msg or "")
    if _INBOUND_BAD.search(t):
        logger.info(f"[审核] 群里可疑要求，已软拒：{t[:40]!r}")
        return random.choice(_INBOUND_DEFLECT)
    return ""


def math_block(user_msg: str, is_owner: bool = False) -> str:
    """非主人问数学题 → 不给答案和步骤。"""
    if is_owner:
        return ""
    t = str(user_msg or "")
    if _MATH_HINT.search(t):
        logger.info(f"[审核] 非主人的数学题，已软拒：{t[:40]!r}")
        return random.choice([
            "(*摆摆鳍*) 数学题本鱼不会算诶，你问问别人吧～",
            "(*装傻*) 数字好晕，本鱼只会计数自己吃了几碗饭。",
        ])
    return ""


def audit_outbound(text: str) -> dict:
    """只判不拦（给自检脚本用）。"""
    kind = _hit(text or "")
    return {"violation": kind, "safe": not kind}

def filter_public(text: str, channel: str = "public") -> str:
    """对外公开发布（B站评论/说说/抖音评论）前过一遍。

    命中红线时**不改成推辞**，而是返回空串，让调用方直接跳过不发——
    公开场合挂一句"这个不能说"反而更显眼。
    """
    t = str(text or "").strip()
    if not t:
        return ""
    kind = _hit(t)
    if kind:
        logger.warning(f"[审核] 拦截公开发布违规({kind}) channel={channel} 原文前40={t[:40]!r}")
        return ""
    if _INBOUND_BAD.search(t):
        logger.warning(f"[审核] 公开发布含可疑要求，已拦截 channel={channel} 原文前40={t[:40]!r}")
        return ""
    return t
