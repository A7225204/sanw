#!/usr/bin/env python3
"""「像不像人说话」的量化指标（从《人是怎么说话的》§9 的差距表来的）。

AI 相对人类的典型偏差：
  非正式表达少 54~72%、网络用语少 32~84%、确定性词多 111~152%、正面情绪多 70~133%、
  长度全挤在均值附近（人是散开的）、词汇更重复、听觉词少 20~63%。

这里给出四个最容易量、也最能看出「平不平」的指标：
  1) 语气体 / 非正式词密度（越高越像人话）
  2) 确定性词密度（越高越像 AI）
  3) 句长离散度（越小越像 AI：全挤在均值附近）
  4) 具体指称密度（时间/地点/数字，越高越像人）

用法：
    python tools/human_metrics.py                 # 最近 200 条回复
    python tools/human_metrics.py --last 50
    python tools/human_metrics.py --text "您好，请问有什么我可以帮您？"
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPLIES = ROOT / "logs" / "replies.jsonl"

_MOOD_WORDS = ("呗", "嘛", "啊", "啦", "呢", "哈", "嗯", "哎", "喂", "呐", "咯", "哟", "哇", "诶")
_NET_WORDS = ("整活", "绷不住", "破防", "离谱", "绝了", "yyds", "xswl", "666", "栓Q", "笑死", "草")
_CERTAIN = ("一定", "必然", "肯定", "显然", "毫无疑问", "绝对", "务必", "必须", "一定要")
_SPECIFIC_TIME = ("今天", "昨天", "前天", "明天", "周二", "周一", "周三", "周四", "周五", "周六", "周日",
                   "早上", "中午", "下午", "晚上", "半夜", "这会儿", "刚才")
_SENT_SPLIT = re.compile(r"[。！？!?；;\n]+")


def _per_100(text: str, words) -> float:
    n = sum(text.count(w) for w in words)
    return round(n * 100 / max(1, len(text)), 2)


def metrics(text: str) -> dict:
    text = str(text or "").strip()
    if not text:
        return {}
    sents = [s for s in _SENT_SPLIT.split(text) if s.strip()]
    lens = [len(s) for s in sents] or [len(text)]
    return {
        "字数": len(text),
        "句数": len(sents),
        "语气体密度/百字": _per_100(text, _MOOD_WORDS),
        "网络词密度/百字": _per_100(text, _NET_WORDS),
        "确定性密度/百字": _per_100(text, _CERTAIN),
        "具体时间词密度/百字": _per_100(text, _SPECIFIC_TIME),
        "句长均值": round(statistics.mean(lens), 1),
        "句长标准差": round(statistics.pstdev(lens), 1) if len(lens) > 1 else 0.0,
    }


def _load(last: int) -> list[str]:
    if not REPLIES.exists():
        return []
    rows = []
    for line in REPLIES.read_text(encoding="utf-8", errors="replace").splitlines()[-last:]:
        try:
            d = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        r = str(d.get("reply") or "").strip()
        if r and not r.startswith("[AI 出错]"):
            rows.append(r)
    return rows


def _avg(rows: list[dict]) -> dict:
    if not rows:
        return {}
    keys = [k for k in rows[0] if k not in ("字数", "句数")]
    return {k: round(statistics.mean(r[k] for r in rows if k in r), 2) for k in keys}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--last", type=int, default=200)
    ap.add_argument("--text")
    a = ap.parse_args()

    if a.text:
        m = metrics(a.text)
        for k, v in m.items():
            print(f"{k}: {v}")
        return

    rows = _load(a.last)
    if not rows:
        print("logs/replies.jsonl 里没有可用的回复")
        return
    ms = [metrics(r) for r in rows]
    avg = _avg(ms)
    print(f"样本：{len(rows)} 条回复")
    for k, v in avg.items():
        print(f"  {k}: {v}")
    low = [r for r, m in zip(rows, ms) if m.get("句长标准差", 0) < 3]
    print(f"\n句长标准差 < 3 的（读起来最「平」）: {len(low)} 条")
    for r in low[:3]:
        print("  -", r[:60])


if __name__ == "__main__":
    main()
