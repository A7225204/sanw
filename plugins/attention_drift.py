"""注意力漂移：她会被群里某个细节勾走一句，然后回到正题。

来源：MaiBot（Mai-with-u/MaiBot，6885 stars）的 `src/maisaka/attention_drift.py`。
它把「走神」拆成三个可调旋钮：

    drift_level     subtle / active / scattered / wild   —— 能飘多远
    anchor_policy   strict / balanced / loose            —— 飘完回不回来
    reaction_style  reserved / natural / ...             —— 开头用不用短反应

**它那四条防跑偏的设计值得照抄**：

    - 每次漂移都必须能从最近消息里找到明确触发点
    - 漂移不是换话题，是先抓触发点，再用很短的拐弯制造「脑子突然亮了一下」
    - 表现活跃联想，不要真降效（别为了显得分心而拖延/忽略任务）
    - 不要医学化，不要自称 ADHD，不要声明自己分心

**我们跟它的差别**：它是配置出来的，**我们的档位由三我和情绪推** ——
本我强 + 开心高 + 不累 → 飘得远；超我压着 + 累 → 老实待着。
同一套数据，`express_depth()` 用来决定「说多长」，这里用来决定「飘多远」。

对外：
    level()          当前档位（三我/情绪推的）
    line()           给 prompt 的一段（空串=这轮不飘）
"""
from __future__ import annotations

# ── 四档漂移 ─────────────────────────────────────────────
DRIFT = {
    "subtle": "漂移档位：轻微。只在最近消息里有非常自然的触发点时，轻轻联想一句；大多数时候继续当前话题。",
    "active": "漂移档位：活跃。可以主动抓新鲜、好笑、反差强的细节接话，但回复仍要短、要能被最近消息解释。",
    "scattered": "漂移档位：明显发散。可以被支线、关键词、熟人语气勾走，**先接住那个点再回到正题**；允许一次可理解的突然拐弯。",
    "wild": "漂移档位：强烈跳跃。可以先被最有趣的细节劫走一下，短促插话、突然联想；**每轮最多一次明显跳跃**，不能无视明确提问。",
}

# ── 三档回钩 ─────────────────────────────────────────────
ANCHOR = {
    "strict": "回钩策略：严格。联想或短反应之后，立刻回到当前话题或被回复对象。",
    "balanced": "回钩策略：自然。可以短暂沿支线说一句，但结尾或主要意思要回到当前聊天。",
    "loose": "回钩策略：宽松。可以保留更自由的相关联想，但不能凭空换话题、不能无视明确提问。",
}

# ── 短反应风格 ───────────────────────────────────────────
REACTION = {
    "reserved": "短反应：克制。只有特别适合接话时，才用一句很短的「啊」「草」开头。",
    "natural": "短反应：自然。可以偶尔先用短句、吐槽或语气词接住话题，再继续正常说。",
}

BASE = (
    "【可以走神】\n"
    "- 你可以短暂被群里新鲜、好笑、反差强或熟悉的人和梗吸引，"
    "但每次走神都得能从最近消息里找到明确触发点。\n"
    "- 走神不是换话题：先抓那个点，再用很短的拐弯，制造「脑子突然亮了一下」的感觉。\n"
    "- 别为了显得分心而故意拖延、忽略事情、或者把话拆散。\n"
    "- 不要医学化，不要自称 ADHD，也不要说自己在分心。"
)


def _state() -> dict:
    """读当前状态。失败返回空 dict。"""
    out: dict = {}
    try:
        from plugins import hormones as h
        out["emo"] = h.derive_emotions()
    except Exception:  # noqa: BLE001
        out["emo"] = {}
    try:
        from server import soul_aggregation as sa
        f = sa.view().get("forces") or {}
        out["本我"] = float(f.get("本我") or 0.4)
        out["超我"] = float(f.get("超我") or 0.4)
        out["自我"] = float(f.get("自我") or 0.5)
        out["冲突"] = str(sa.conflict_level() or "低")
    except Exception:  # noqa: BLE001
        out["本我"], out["超我"], out["自我"], out["冲突"] = 0.4, 0.4, 0.5, "低"
    return out


def level() -> tuple[str, str, str]:
    """算这一轮的 (漂移档, 回钩档, 短反应档)。

    判据（和 express_depth 同一套思路，但管的是另一件事）：
        本我强 + 开心/情欲高 + 不累 → 飘得远（scattered / wild）
        超我压着 + 累 + 丧 → 老实待着（subtle + strict）
        冲突高 → active + balanced（想跑题又不敢跑远）
    """
    s = _state()
    e = s.get("emo") or {}
    idf, sef = s["本我"], s["超我"]
    conflict = s["冲突"]

    happy = float(e.get("开心") or 0)
    ky = float(e.get("情欲") or 0)
    tired = float(e.get("累") or 0)
    sad = float(e.get("丧") or 0)
    annoy = float(e.get("烦") or 0)

    # 活络度：想不想飘
    live = (idf - sef + 0.5) * 100 * 0.45 + happy * 0.35 + ky * 0.20
    live -= tired * 0.42 + sad * 0.30 + annoy * 0.15

    # 2026-11-07 调阈值：原来 58/44/30，实测「开心 88、情欲 47」也只有 23 分（subtle），
    # 因为丧+烦+累 三项一起扣了 37 分 —— 结果她基本永远 subtle，四档形同虚设。
    # 往下挪一档：50/38/24。
    if live >= 50:
        drift = "wild"
    elif live >= 38:
        drift = "scattered"
    elif live >= 24:
        drift = "active"
    else:
        drift = "subtle"

    # 回钩：超我严就勒紧，冲突高就松半档
    margin = sef - idf
    if margin >= 0.15:
        anchor = "strict"
    elif margin <= -0.15 or conflict == "高":
        anchor = "loose" if margin <= -0.25 else "balanced"
    else:
        anchor = "balanced"

    reaction = "natural" if live >= 40 else "reserved"
    return drift, anchor, reaction


def line() -> str:
    """给 prompt 的一段。**低活络度时不注入**（那轮她压根不飘，写了反而让她硬演）。"""
    drift, anchor, reaction = level()
    if drift == "subtle" and anchor == "strict":
        return ""      # 老实待着的状态，不给走神指令
    return "\n".join([BASE, "- " + DRIFT[drift], "- " + ANCHOR[anchor], "- " + REACTION[reaction]])


def brief() -> str:
    """一句话（调试/面板用）。"""
    d, a, r = level()
    return f"走神：{d} / 回钩：{a} / 短反应：{r}"


def stats() -> dict:
    d, a, r = level()
    s = _state()
    return {"drift": d, "anchor": a, "reaction": r,
            "本我": s["本我"], "超我": s["超我"], "冲突": s["冲突"]}
