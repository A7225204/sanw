"""深查：verify.py 查不到的那类问题 —— 「她的表现还正常吗」。

**两者分工**：

    verify.py（33 秒）  机制能不能跑通 —— 语法、链路、消气、检索、自检、前端
    deep_check.py       跑通了，但**表现对不对** —— 说话走没走样、记忆召不召得回来、
                       哪个通道在烧钱、人格在往哪长

**四项检查，纯脚本零成本**：

    ① 回复走样    低置信标记比例、平均长度、重复句、AI 腔词
    ② 记忆召回    抽 N 条真实记忆，拿它自己的 fact 当 query，看能不能召回自己
    ③ token 通道  近 24h 各 channel 用量，找异常暴涨
    ④ 人格固化    pending 积压、证据分布、最近固化的内容

**加 `--llm` 会多一步**：把上面四项的**统计结果**交给模型，让它说「哪里不对劲」。
只用结果不用原文，约 0.03~0.05 元。

**用法**：
    python tools/deep_check.py               # 纯脚本，约 8 秒
    python tools/deep_check.py --llm         # 加 LLM 判读
    python tools/deep_check.py --hours 48    # 看最近 48 小时的回复
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DB = Path.home() / "AppData" / "Roaming" / "dsh-pet-standalone-webm-chat" / "shared_memory.db"
REPLIES = ROOT / "logs" / "replies.jsonl"

# 低置信标记（ActKV 那篇论文的判据，中文对应）
LOW_CONF = {
    "等一下/等等": r"等一下|等下|等等",
    "不确定": r"不确定|不太清楚|说不好|不敢说|记不清",
    "让我想": r"让我想想|我想想|让我看看",
    "好像口气": r"好像|大概是|应该是|可能是|估计",
}

# AI 腔（她不该这么说话）
AI_TONE = {
    "作为AI": r"作为一?个?(AI|人工智能|语言模型)",
    "首先其次": r"首先[，,].{0,20}其次|第一[，,.].{0,20}第二[，,.]",
    "分点罗列": r"^\s*(\d+[.、)]|[-*•])\s",
    "总结性": r"综上所述|总的来说|总之",
    "过度礼貌": r"希望对你有帮助|还有什么可以帮|很高兴为你",
}


def _db() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True)


# ══════════════════════════════════════════════════════════
# ① 回复走样
# ══════════════════════════════════════════════════════════
def check_replies(hours: int = 24) -> dict:
    since = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() - hours * 3600))
    rows: list[str] = []
    try:
        c = _db()
        for (r,) in c.execute(
            "select reply from chat_history where timestamp >= ? and reply is not null "
            "and length(reply) > 5 and reply not like '[AI 出错%'", (since,)).fetchall():
            rows.append(r)
        c.close()
    except Exception:  # noqa: BLE001
        pass
    # 补 logs/replies.jsonl（如果 db 覆盖不到）
    if REPLIES.exists():
        for line in REPLIES.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                d = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            r = str(d.get("reply") or "")
            if len(r) > 5 and not r.startswith("[AI 出错"):
                rows.append(r)

    if not rows:
        return {"n": 0, "note": "没有样本"}

    # 2026-11-07：「[本鱼发了一张图：…实际画面是「…」]」这种条目里嵌的是 VLM 的描述，
    # 不是她说的话。原来把它们算进「AI 腔」，灯就一直黄 —— 那是误报。
    talk = [r for r in rows if not r.lstrip().startswith("[本鱼发了一张图")]
    n = len(rows)
    out: dict = {"n": n, "avg_len": round(sum(len(r) for r in rows) / max(1, n), 1)}

    low = {k: sum(1 for r in rows if re.search(p, r)) for k, p in LOW_CONF.items()}
    out["low_conf"] = {k: f"{v} ({v / n * 100:.1f}%)" for k, v in low.items() if v}
    # 真不确定（不含"好像"这种口气词）
    real = sum(1 for r in rows if re.search(LOW_CONF["不确定"], r))
    out["real_uncertain_pct"] = round(real / n * 100, 2)

    tone = {}
    for k, p in AI_TONE.items():
        cnt = sum(1 for r in talk if re.search(p, r, re.M))
        if cnt:
            tone[k] = f"{cnt} ({cnt / max(1, len(talk)) * 100:.1f}%)"
    out["ai_tone"] = tone or {}
    out["talk_n"] = len(talk)

    # 重复句：同一条回复里重复出现的 8 字以上片段
    dup = 0
    for r in rows:
        segs = [r[i:i + 10] for i in range(0, max(1, len(r) - 10))]
        cc = Counter(segs)
        if cc and cc.most_common(1)[0][1] >= 3:
            dup += 1
    out["dup_pct"] = round(dup / n * 100, 1)
    return out


# ══════════════════════════════════════════════════════════
# ② 记忆召回：拿她自己的 fact 当 query，看能不能召回自己
# ══════════════════════════════════════════════════════════
def check_recall(sample: int = 12) -> dict:
    try:
        import nonebot
        try:
            nonebot.get_driver()
        except Exception:  # noqa: BLE001
            nonebot.init()
        from plugins import long_memory as lm
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}

    arch = getattr(lm, "_archive", {})
    picked: list[tuple[str, dict]] = []
    for scope, arr in arch.items():
        if not scope.startswith(("private_", "group_")):
            continue
        for it in (arr or []):
            if isinstance(it, dict) and it.get("fact") and len(str(it["fact"])) > 12:
                picked.append((scope, it))
    if not picked:
        return {"error": "没有可测的记忆"}

    # 均匀抽，不取前几条（前面的权重都最高，测不出问题）
    step = max(1, len(picked) // sample)
    hits = 0
    misses: list[str] = []
    for scope, it in picked[::step][:sample]:
        fact = str(it["fact"])[:24]
        try:
            rows = lm.explain(scope, fact, 3)
        except Exception:  # noqa: BLE001
            misses.append(f"{fact[:16]}… 抛错")
            continue
        ids = [r["id"] for r in rows]
        if str(it.get("id")) in ids:
            hits += 1
        else:
            top = f"{rows[0]['score']:.2f}" if rows else "空"
            misses.append(f"{fact[:16]}… (top {top})")
    return {
        "sample": min(sample, len(picked[::step])),
        "self_recall": hits,
        "rate": round(hits / max(1, min(sample, len(picked[::step]))) * 100, 1),
        "misses": misses[:4],
    }


# ══════════════════════════════════════════════════════════
# ③ token 通道
# ══════════════════════════════════════════════════════════
def check_token(hours: int = 24) -> dict:
    since = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() - hours * 3600))
    try:
        c = _db()
        rows = c.execute(
            "select channel, count(*), sum(prompt_tokens), sum(completion_tokens), sum(cached_tokens) "
            "from token_usage where ts >= ? group by channel order by 3 desc", (since,)).fetchall()
        c.close()
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}

    total = {"calls": 0, "in": 0, "out": 0, "cache": 0}
    channels: dict[str, dict] = {}
    for ch, n, p, co, ca in rows:
        p, co, ca = int(p or 0), int(co or 0), int(ca or 0)
        channels[str(ch)] = {"calls": n, "in": p, "out": co, "cache": ca}
        total["calls"] += n
        total["in"] += p
        total["out"] += co
        total["cache"] += ca
    miss = total["in"] - total["cache"]
    cost = miss / 1e6 * 1.0 + total["cache"] / 1e6 * 0.1 + total["out"] / 1e6 * 2.0
    # 平均单次输入；>5000 说明上下文塞太满
    # 2026-11-07：qq 通道排除 —— 它是聊天主干，17k 输入里大部分是前缀缓存命中，
    # 实际付费很少。把它算成「heavy」会让灯永远黄着，久了没人看。
    # qq = 聊天主干（前缀缓存命中率高）；internal:* = 内部任务（记忆日志 19k 是它该有的量）
    SKIP = {"qq"}
    heavy = {k: v for k, v in channels.items()
             if k not in SKIP and not k.startswith("internal:")
             and v["calls"] and v["in"] / v["calls"] > 5000
             and v["in"] / v["calls"] - v["cache"] / v["calls"] > 3000}
    # ── 2026-11-07 加：utility 暴涨告警 ──
    # 那天 03:11~03:36 有 352 次 utility 调用、105 万 token、命中率 0.3%，
    # 而历史是 34~56 次/整天。查了两小时没定到来源（那时还没有 caller 标记）。
    # 这里给一条「当天次数/单次大小明显超标就报」的判据，
    # 配合 _caller_scope() 记的 scope，下次能一眼定位。
    _util = {k: v for k, v in channels.items() if k.startswith("internal:utility")}
    _u_calls = sum(v["calls"] for v in _util.values())
    _u_in = sum(v["in"] for v in _util.values())
    _u_cache = sum(v["cache"] for v in _util.values())
    _u_avg = (_u_in / _u_calls) if _u_calls else 0
    _u_hit = (_u_cache / _u_in * 100) if _u_in else 0
    # 2026-11-07：24h 窗口会把「已经停掉的爆发」反复报成异常（441 次全在 09:15 之前，
    # 之后归零，面板却一直亮黄灯）。先看最近 60 分钟还在不在调：不在就是历史，不报。
    _recent60 = 0
    try:
        _c4 = _db()
        _rr = _c4.execute(
            "select count(*) from token_usage where "
            "ts >= datetime('now','localtime','-60 minutes') "
            "and channel like 'internal:utility%'").fetchone()
        _recent60 = int(_rr[0] or 0) if _rr else 0
        _c4.close()
    except Exception:  # noqa: BLE001
        _recent60 = 0
    util_spike = ""
    # 只有「最近 1 小时还在跑」才算当前异常
    # 24 小时窗口下：>150 次 或 单次平均 >1500 token 就算异常（历史 34~56 次、267~432 token）
    # 2026-11-07：阈值从 5 提到 40。
    # 原来 recent_60m 只要 >5 就报 —— 但正常波动就在 4~6 之间，
    # 而 _u_calls 是 24h 累计（凌晨那批 441 次历史还要在窗口里躺一整天），
    # 于是每轮体检都报「utility 451 次」，实际最近一小时只有 6 次。
    # 误报比不报更坏 —— 会把真问题淹掉。真正的异常是每分钟十几次（recent_60m 上百）。
    if _recent60 > 40 and _u_calls > 150:
        util_spike = f"近期 utility 调用密集：最近 60 分钟 {_recent60} 次（24h 累计 {_u_calls}）"
    elif _recent60 > 40 and _u_avg > 1500:
        util_spike = f"utility 单次平均 {_u_avg:.0f} token（平时 267~432）"
    # 哪个 scope 最大 —— 有 caller 标记之后能直接指出来
    by_scope: list[str] = []
    if util_spike:
        try:
            c2 = _db()
            for sc, n in c2.execute(
                "select scope, count(*) from token_usage where ts >= ? "
                "and channel like 'internal:utility%' group by scope "
                "order by 2 desc limit 3", (since,)).fetchall():
                # 纯 'utility' 是加 caller 标记之前的数据，看不出是谁调的
                _lbl = "未知来源" if str(sc) == "utility" else str(sc).replace("utility:", "")
                by_scope.append(f"{_lbl}×{n}")
            c2.close()
        except Exception:  # noqa: BLE001
            pass

    return {"hours": hours, "total": total, "cost_yuan": round(cost, 2),
            "channels": dict(sorted(channels.items(), key=lambda x: -x[1]["in"])[:6]),
            "heavy": list(heavy),
            "utility": {"calls": _u_calls, "avg_in": round(_u_avg), "hit_pct": round(_u_hit, 1),
                        "recent_60m": _recent60,
                        "spike": util_spike, "by_scope": by_scope}}


# ══════════════════════════════════════════════════════════
# ④ 人格固化
# ══════════════════════════════════════════════════════════
def check_growth() -> dict:
    p = ROOT / "memory" / "persona_growth.json"
    if not p.exists():
        return {"error": "persona_growth.json 不存在"}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}
    items = [x for x in (d.get("items") or []) if isinstance(x, dict)]
    st = Counter(x.get("status") or "?" for x in items)
    # 证据数分布
    evd = Counter(len([e for e in (x.get("evidence") or []) if str(e).strip()]) for x in items)
    recent = sorted(items, key=lambda x: x.get("solidifiedAt") or "", reverse=True)[:3]
    return {
        "total": len(items),
        "status": dict(st),
        "evidence_dist": dict(sorted(evd.items())),
        "recent": [f"{x.get('trait')}={str(x.get('value'))[:26]}" for x in recent],
        # pending 积压太多说明没人在看
        "pending_backlog": st.get("pending", 0),
    }


# ══════════════════════════════════════════════════════════
# 可选：LLM 判读
# ══════════════════════════════════════════════════════════
LRU_PROMPT = """你是一个 AI 角色的运行状态审阅者。下面是她最近一段时间的**统计结果**（不是原文）。

请只回答三个问题，每个一到两句，不要客套：
1. 有哪一项数字明显不正常？（给出具体数字和你的判断依据）
2. 如果只能改一处，改哪里收益最大？
3. 哪项看着正常、其实可能是假象？

统计：
{stats}
"""


async def _llm_read(stats: dict) -> str:
    try:
        from plugins.ai_group import call_ai_raw
    except Exception as e:  # noqa: BLE001
        return f"（LLM 不可用：{type(e).__name__}）"
    prompt = LRU_PROMPT.format(stats=json.dumps(stats, ensure_ascii=False, indent=1))
    try:
        return await call_ai_raw(prompt, "deepseek", temperature=0.3, max_tokens=700)
    except Exception as e:  # noqa: BLE001
        return f"（LLM 调用失败：{type(e).__name__}: {e}）"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24, help="看最近几小时（默认 24）")
    ap.add_argument("--llm", action="store_true", help="加 LLM 判读（约 0.03~0.05 元）")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    args = ap.parse_args()

    t0 = time.perf_counter()
    stats = {
        "回复": check_replies(args.hours),
        "召回": check_recall(),
        "token": check_token(args.hours),
        "人格": check_growth(),
    }
    el = time.perf_counter() - t0

    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=1))
    else:
        print(f"\n深查（最近 {args.hours}h）  用时 {el:.1f}s\n" + "=" * 52)

        r = stats["回复"]
        if r.get("n"):
            print(f"\n① 回复走样  {r['n']} 条，平均 {r['avg_len']} 字")
            print(f"   真不确定 {r['real_uncertain_pct']}%  |  重复句 {r['dup_pct']}%")
            if r.get("low_conf"):
                print("   低置信：" + "，".join(f"{k} {v}" for k, v in r["low_conf"].items()))
            if r.get("ai_tone"):
                print("   ⚠️ AI 腔：" + "，".join(f"{k} {v}" for k, v in r["ai_tone"].items()))
            else:
                print("   ✅ 没有 AI 腔")
        else:
            print(f"\n① 回复走样  {r.get('note', '无样本')}")

        rec = stats["召回"]
        if rec.get("error"):
            print(f"\n② 记忆召回  ❌ {rec['error']}")
        else:
            flag = "✅" if rec["rate"] >= 70 else "⚠️ "
            print(f"\n② 记忆召回  {flag} 自召回 {rec['self_recall']}/{rec['sample']} = {rec['rate']}%")
            for m in rec.get("misses") or []:
                print(f"   漏：{m}")

        tk = stats["token"]
        if tk.get("error"):
            print(f"\n③ token  ❌ {tk['error']}")
        else:
            t = tk["total"]
            print(f"\n③ token（{tk['hours']}h）  {t['calls']} 次，约 {tk['cost_yuan']} 元")
            for ch, v in (tk.get("channels") or {}).items():
                avg = v["in"] // max(1, v["calls"])
                print(f"   {ch[:26]:28s} {v['calls']:4d} 次  均 {avg:5d} 输入")
            if tk.get("heavy"):
                print(f"   ⚠️ 单次输入超 5000 的通道：{', '.join(tk['heavy'])}")

        g = stats["人格"]
        if g.get("error"):
            print(f"\n④ 人格固化  ❌ {g['error']}")
        else:
            print(f"\n④ 人格固化  {g['total']} 条 | {g['status']}")
            print(f"   证据分布 {g['evidence_dist']}")
            if g["pending_backlog"] > 20:
                print(f"   ⚠️ pending 积压 {g['pending_backlog']} 条（没人看？）")
            for x in g.get("recent") or []:
                print(f"   最近：{x}")

    if args.llm:
        print("\n" + "=" * 52)
        print("LLM 判读：\n")
        import asyncio
        print(asyncio.run(_llm_read(stats)))

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
