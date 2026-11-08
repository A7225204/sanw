import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\plugins\long_memory.py")
s = p.read_text(encoding="utf-8")

# 1) 通用时间解析：epoch / "MM-DD HH:MM" / "YYYY-MM-DD HH:MM:SS" 都能吃
if "def _ts_any(" not in s:
    helper = '''

def _ts_any(v) -> float:
    """把各种时间表示统一成 epoch 秒。

    pending 里存的是 "10-29 20:12" 这种可读字符串，直接 float() 会炸，
    以前一炸整批合并就失败（日志：could not convert string to float: '10-29 20:12'）。
    """
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    txt = str(v).strip()
    try:
        return float(txt)
    except (TypeError, ValueError):
        pass
    for fmt in ("%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(txt, fmt)
            if "%Y" not in fmt:
                dt = dt.replace(year=datetime.now().year)
            return dt.timestamp()
        except ValueError:
            continue
    return 0.0
'''
    i = s.find("def _match_sources(")
    s = s[:i] + helper.lstrip("\n") + "\n" + s[i:]

# 2) _match_sources 用安全解析
s = s.replace(
    'scored.append((ratio, [str(name or "群友")[:20], str(text or "")[:80], float(ts or 0)]))',
    'scored.append((ratio, [str(name or "群友")[:20], str(text or "")[:80], _ts_any(ts)]))')

s = s.replace(
    '''    return [
        {"s": n, "t": t, "ts": __import__("time").strftime("%Y-%m-%d %H:%M:%S", __import__("time").localtime(ts)) if ts else ""}
        for _r, (n, t, ts) in scored[:limit]
    ]''',
    '''    return [
        {"s": n, "t": t,
         "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else ""}
        for _r, (n, t, ts) in scored[:limit]
    ]''')

p.write_text(s, encoding="utf-8")
print("long_memory: 时间解析已加固")
