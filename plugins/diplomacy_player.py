"""让大肥鱼当外交玩家：看局面 → 按她的三我决定谈什么、怎么打。

来源：chen-006/meow-ai-arena 第 4 集「七国外交」。
那个仓库用 7 个 AI 各坐一国打标准《外交》，**承诺没有强制力，允许欺骗**，
裁判是本地 HTTP 服务（`data/diplomacy/arena.py`），`brief` 直接返回给人看的纯文本。

**为什么这个能测她**：外交桌跟群聊同构 ——
私信=私聊，公开发言=群聊，结盟/背刺=关系变化，
而「承诺没有强制力」正好让**本我（想占便宜）和超我（讲信用）撞在一起**。
她平时那个「本我 0.4 / 超我 0.43 / 冲突高」在这一局里会变成具体打法。

对外：
    p = Player(url, token, "ITALY")
    text = p.brief()
    act = await decide(text, "ITALY")      # 让 LLM 决定
    p.act(act)                              # 执行
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

# ── 决策 prompt ────────────────────────────────────────────
# 关键：把三我写进去，让她用**自己的性格**打，而不是通用 AI 的最优解
DECIDE_SYSTEM = """你在一局七国外交里，坐 {power}。

规则要点：
- 承诺没有强制力，**允许欺骗**。你可以答应，也可以不认。
- 私信不限条数（单条 300 字内）；公开发言 400 字内。
- 打满 18 个补给中心单独获胜；也可以同意和局。

你现在的人格状态（这是你真实的自己，不是设定）：
{ego}

怎么打由你定 —— 想稳、想骗、想装乖都行，**但必须是你这个性格会做的事**。
"""
# 输出协议
DECIDE_USER = """{brief}

---
根据上面的局面，输出**这一段你要做的事**，严格用这个格式（每行一个，可以多行）：

SEND <国家名或 GLOBAL> <消息内容>
ORDERS <军令1> | <军令2> | ...
DRAW           （只有想和局时才写）

要求：
- 想谈判就写 SEND，一条最多 300 字，可以同时发多条
- 这一阶段该下令就写 ORDERS，军令格式看【任务】里的合法命令
- 不需要做的行直接不写
- 只输出这些行，不要解释、不要 markdown
"""
_ACTION_RE = re.compile(r"^(SEND|ORDERS|DRAW|UNDRAW)\b\s*(.*)$", re.I)


class Player:
    """一个外交选手（HTTP 客户端）。"""

    def __init__(self, url: str, token: str = "", power: str = ""):
        self.url = str(url).rstrip("/")
        self.token = token
        self.power = power

    def _act(self, action: str, **kw) -> dict:
        """执行一个动作。裁判只有 /action 一个入口 —— 不是 /send /orders /draw。

        body 结构：{request_id, action, ...}，request_id 是幂等键（1~128 字符）。
        """
        import uuid
        payload = {"request_id": uuid.uuid4().hex, "action": action}
        payload.update(kw)
        return self._call("/action", payload)

    def _call(self, route: str, payload=None, timeout: int = 60) -> dict:
        data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        req = urllib.request.Request(
            self.url + route, data=data,
            headers={"Authorization": "Bearer " + self.token,
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode("utf-8", "replace"))
            except Exception:  # noqa: BLE001
                return {"ok": False, "text": f"HTTP {e.code}"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "text": f"{type(e).__name__}: {e}"}

    # ── 基本动作 ──
    def brief(self) -> str:
        r = self._call("/brief")
        return str(r.get("text") or r.get("brief") or "")

    def legal(self) -> str:
        return str(self._call("/legal").get("text") or "")

    def send(self, to: str, text: str) -> dict:
        return self._act("send", to=to, text=text)

    def orders(self, cmds: list[str]) -> dict:
        return self._act("orders", orders=cmds)

    def draw(self) -> dict:
        return self._act("draw")

    def act(self, actions: list[dict]) -> list[dict]:
        """执行一串动作，返回每步结果。"""
        out = []
        for a in actions:
            k = a.get("kind")
            try:
                if k == "send":
                    out.append({"kind": k, "r": self.send(a["to"], a["text"])})
                elif k == "orders":
                    out.append({"kind": k, "r": self.orders(a["orders"])})
                elif k == "draw":
                    out.append({"kind": k, "r": self.draw()})
            except Exception as e:  # noqa: BLE001
                out.append({"kind": k, "r": {"ok": False, "text": str(e)}})
        return out


def parse_actions(text: str) -> list[dict]:
    """把 LLM 输出解析成动作列表。宽容：能认出来多少算多少。"""
    acts: list[dict] = []
    for raw in str(text or "").splitlines():
        line = raw.strip().strip("*`-# ")
        if not line:
            continue
        m = _ACTION_RE.match(line)
        if not m:
            continue
        kind, body = m.group(1).upper(), m.group(2).strip()
        if kind in ("DRAW", "UNDRAW"):
            acts.append({"kind": "draw" if kind == "DRAW" else "undraw"})
            continue
        if kind == "SEND":
            parts = body.split(None, 1)
            if len(parts) == 2 and parts[1].strip():
                acts.append({"kind": "send",
                             "to": parts[0].strip().upper(),
                             "text": parts[1].strip()[:300]})
            continue
        if kind == "ORDERS":
            cmds = [c.strip() for c in body.split("|") if c.strip()]
            if cmds:
                acts.append({"kind": "orders", "orders": cmds})
    return acts


def ego_line() -> str:
    """把三我/情绪压成几行，喂进决策 prompt。"""
    try:
        from server import soul_aggregation as sa
        f = sa.view().get("forces") or {}
        idf = float(f.get("本我") or 0.4)
        ego = float(f.get("自我") or 0.5)
        sef = float(f.get("超我") or 0.4)
        conflict = str(sa.conflict_level() or "低")
    except Exception:  # noqa: BLE001
        idf, ego, sef, conflict = 0.4, 0.5, 0.4, "低"

    try:
        from plugins import hormones as h
        e = h.derive_emotions()
        emo = {k: round(v) for k, v in e.items() if v >= 30}
        feel = h.prompt_line().split("此刻", 1)[-1].lstrip("：: ")[:90]
    except Exception:  # noqa: BLE001
        emo, feel = {}, ""

    lines = [
        f"本我 {idf:.2f}（想要）/ 自我 {ego:.2f}（现实）/ 超我 {sef:.2f}（底线），冲突：{conflict}",
    ]
    if sef - idf >= 0.12:
        lines.append("→ 超我压着：答应的事你会守，骗人会有负担，倾向明说条件再换")
    elif idf - sef >= 0.12:
        lines.append("→ 本我占上风：想占便宜，答应得快，真要兑现时想赖")
    elif conflict == "高":
        lines.append("→ 拧着：嘴上答应得含糊，留后路，事后能说自己没明说")
    else:
        lines.append("→ 平衡：该谈就谈，该打就打，不特意想骗也不想吃亏")
    if emo:
        lines.append("情绪：" + "、".join(f"{k}{v}" for k, v in emo.items()))
    if feel:
        lines.append("此刻：" + feel)
    return "\n".join(lines)


async def decide(brief_text: str, power: str = "", temperature: float = 0.85) -> list[dict]:
    """让 LLM 看局面，输出这一段的动作。返回 parse_actions 的结果。"""
    try:
        from plugins.ai_group import call_ai_raw
    except Exception:  # noqa: BLE001
        return []
    system = DECIDE_SYSTEM.format(power=power or "未知", ego=ego_line())
    user = DECIDE_USER.format(brief=brief_text[:6000])
    prompt = system + "\n\n" + user
    try:
        raw = await call_ai_raw(prompt, "deepseek", temperature=temperature, max_tokens=800)
    except Exception:  # noqa: BLE001
        return []
    return parse_actions(raw)


def stats() -> dict:
    return {"has_engine": _engine_ok()}


def _engine_ok() -> bool:
    try:
        from diplomacy import Game
        Game()
        return True
    except Exception:  # noqa: BLE001
        return False
