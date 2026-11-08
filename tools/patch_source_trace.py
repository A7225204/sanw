import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\plugins\long_memory.py")
s = p.read_text(encoding="utf-8")

if "_match_sources" in s:
    print("已支持来源溯源")
    raise SystemExit

# 1) 新增来源匹配助手：用 2-gram 重合把事实对回原始聊天消息
helper = '''

def _match_sources(fact: str, pending: list, limit: int = 3) -> list[dict]:
    """把一条事实对回"待合并的原始消息"，供详情页溯源。

    pending 结构：[[昵称, 文本, 时间戳], ...]
    用 2-gram 重合度打分，≥0.3 才算来源，最多留 limit 条。
    """
    fb = _bigrams(fact)
    if not fb:
        return []
    scored: list[tuple[float, list]] = []
    for row in pending or []:
        try:
            name, text, ts = row[0], row[1], row[2]
        except Exception:  # noqa: BLE001
            continue
        tb = _bigrams(str(text or ""))
        if not tb:
            continue
        ratio = len(fb & tb) / max(1, len(fb))
        if ratio >= 0.3:
            scored.append((ratio, [str(name or "群友")[:20], str(text or "")[:80], float(ts or 0)]))
    scored.sort(key=lambda x: -x[0])
    return [
        {"s": n, "t": t, "ts": __import__("time").strftime("%Y-%m-%d %H:%M:%S", __import__("time").localtime(ts)) if ts else ""}
        for _r, (n, t, ts) in scored[:limit]
    ]

'''
anchor = "def _apply_ops(scope: str, ops: list) -> tuple[int, int, int]:"
i = s.find(anchor)
if i < 0:
    raise SystemExit("_apply_ops 未找到")
s = s[:i] + helper.lstrip("\n") + "\n" + s[i:]

# 2) _apply_ops 接收 pending，并把 src 写进条目
s = s.replace("def _apply_ops(scope: str, ops: list) -> tuple[int, int, int]:",
              "def _apply_ops(scope: str, ops: list, pending: list | None = None) -> tuple[int, int, int]:", 1)

s = s.replace('''            items.append(
                {
                    "id": next_id,
                    "subject": subject or "群友",
                    "fact": fact,
                    "time": today,
                    "w": MEM_WEIGHT_START,
                    "h": 0,
                    "lu": _now_ts(),
                }
            )''',
'''            _src = _match_sources(fact, pending or [])
            items.append(
                {
                    "id": next_id,
                    "subject": subject or "群友",
                    "fact": fact,
                    "time": today,
                    "w": MEM_WEIGHT_START,
                    "h": 0,
                    "lu": _now_ts(),
                    "src": _src,
                }
            )''', 1)

# update 分支也记来源
s = s.replace('''            if target and _valid_fact(fact):
                target["fact"] = fact
                target["time"] = today
                u += 1''',
'''            if target and _valid_fact(fact):
                target["fact"] = fact
                target["time"] = today
                _src2 = _match_sources(fact, pending or [])
                if _src2:
                    target["src"] = _src2
                u += 1''', 1)

# 3) consolidate 调用处把待合并缓冲传进去
s = s.replace("a, u, d = _apply_ops(key, ops)",
              "a, u, d = _apply_ops(key, ops, buf[-12:])", 1)

p.write_text(s, encoding="utf-8")
print("已加来源溯源：_match_sources + _apply_ops(src) + consolidate 传缓冲")
