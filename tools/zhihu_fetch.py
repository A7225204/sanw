"""知乎抓取：拿 cookie 之后能搜、能看回答、能存成 markdown。

**为什么需要 cookie**：知乎 2023 年后所有 API 都要登录态，
匿名请求直接 403（实测 `search_v3` 空 cookie 返回 `{"HitLabels":null}`，无 UA 时 403）。

**cookie 从哪来**（两条路）：
1. `memory/zhihu_cookies.json` —— 手动导出的 {name: value}
2. 环境变量 `ZHIHU_Z_C0` —— 只要登录凭证这一个就够

关键 cookie 就一个：**`z_c0`**（登录态）。`_xsrf` 有时要，`d_c0` 是设备 id。

用法：
    python tools/zhihu_fetch.py search "深圳 三维设计 工资"
    python tools/zhihu_fetch.py answer 123456789      # 按回答 id
    python tools/zhihu_fetch.py question 12345678     # 按问题 id
    python tools/zhihu_fetch.py user <url_token>      # 用户主页
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

COOKIE_FILE = ROOT / "memory" / "zhihu_cookies.json"
CACHE_DIR = ROOT / "memory" / "zhihu_cache"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/158.0.0.0 Safari/537.36")

BASE_HEADERS = {
    "User-Agent": UA,
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.zhihu.com/",
    "x-requested-with": "fetch",
}


def load_cookies() -> dict:
    """读 cookie。文件优先，其次环境变量。"""
    ck: dict[str, str] = {}
    if COOKIE_FILE.exists():
        try:
            ck = json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            ck = {}
    z = os.getenv("ZHIHU_Z_C0", "").strip()
    if z:
        ck["z_c0"] = z
    return ck


def check() -> tuple[bool, str]:
    """能不能用。返回 (ok, 说明)。"""
    ck = load_cookies()
    if not ck.get("z_c0"):
        return False, "没有 z_c0 —— 知乎所有 API 都要登录态"
    return True, f"有 {len(ck)} 个 cookie（含 z_c0）"


def _get(url: str, params: dict | None = None, timeout: int = 15) -> dict | None:
    import httpx
    ck = load_cookies()
    if not ck.get("z_c0"):
        return None
    try:
        r = httpx.get(url, params=params, headers=BASE_HEADERS, cookies=ck,
                      timeout=timeout, follow_redirects=True)
    except Exception as e:  # noqa: BLE001
        print(f"[zhihu] 请求失败 {type(e).__name__}: {e}", file=sys.stderr)
        return None
    if r.status_code != 200:
        print(f"[zhihu] HTTP {r.status_code}  {url}", file=sys.stderr)
        return None
    try:
        return r.json()
    except Exception:  # noqa: BLE001
        return None


def _strip_html(s: str) -> str:
    s = re.sub(r"<br\s*/?>", "\n", str(s or ""))
    s = re.sub(r"</p>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&")
          .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def search(q: str, limit: int = 10) -> list[dict]:
    """搜知乎。返回 [{type, title, excerpt, url, votes}]。"""
    d = _get("https://www.zhihu.com/api/v4/search_v3",
             {"t": "general", "q": q, "correction": 1, "offset": 0, "limit": limit})
    if not d:
        return []
    out: list[dict] = []
    for item in (d.get("data") or []):
        obj = item.get("object") or {}
        typ = item.get("type") or obj.get("type") or ""
        title = obj.get("title") or (obj.get("question") or {}).get("name") or ""
        excerpt = _strip_html(obj.get("excerpt") or obj.get("content") or "")
        url = ""
        if obj.get("id"):
            if typ == "answer":
                url = f"https://www.zhihu.com/answer/{obj['id']}"
            elif typ == "article":
                url = f"https://zhuanlan.zhihu.com/p/{obj['id']}"
            elif typ == "question":
                url = f"https://www.zhihu.com/question/{obj['id']}"
        out.append({
            "type": typ,
            "title": _strip_html(title)[:90],
            "excerpt": excerpt[:400],
            "url": url,
            "votes": obj.get("voteup_count") or 0,
        })
    return out


def question(qid: str | int, limit: int = 10) -> dict | None:
    """取问题 + 前几个回答。"""
    q = _get(f"https://www.zhihu.com/api/v4/questions/{qid}",
             {"include": "title,detail,answer_count,follower_count"})
    a = _get(f"https://www.zhihu.com/api/v4/questions/{qid}/answers",
             {"include": "content,voteup_count,author,created_time",
              "limit": limit, "offset": 0, "order_by": "default"})
    if not q:
        return None
    ans = []
    for it in ((a or {}).get("data") or []):
        au = it.get("author") or {}
        ans.append({
            "author": au.get("name") or "",
            "votes": it.get("voteup_count") or 0,
            "text": _strip_html(it.get("content") or "")[:3000],
        })
    return {
        "title": q.get("title") or "",
        "detail": _strip_html(q.get("detail") or "")[:1500],
        "answer_count": q.get("answer_count") or 0,
        "answers": ans,
    }


def answer(aid: str | int) -> dict | None:
    d = _get(f"https://www.zhihu.com/api/v4/answers/{aid}",
             {"include": "content,voteup_count,author,question,created_time"})
    if not d:
        return None
    au = d.get("author") or {}
    qu = d.get("question") or {}
    return {
        "question": qu.get("name") or "",
        "author": au.get("name") or "",
        "votes": d.get("voteup_count") or 0,
        "text": _strip_html(d.get("content") or "")[:6000],
    }


def _save(name: str, text: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w\u4e00-\u9fa5-]+", "_", name)[:60]
    p = CACHE_DIR / f"{safe}_{time.strftime('%Y%m%d_%H%M')}.md"
    p.write_text(text, encoding="utf-8")
    return p


def cmd_search(q: str, n: int = 10) -> None:
    hits = search(q, n)
    if not hits:
        print("没搜到（或 cookie 失效）")
        return
    lines = [f"# 知乎搜索：{q}", ""]
    for i, h in enumerate(hits, 1):
        lines.append(f"## {i}. {h['title'] or '(无题)'}")
        lines.append(f"- {h['type']} · {h['votes']} 赞 · {h['url']}")
        if h["excerpt"]:
            lines.append("")
            lines.append(h["excerpt"])
        lines.append("")
    txt = "\n".join(lines)
    print(txt[:2500])
    p = _save(f"搜索_{q}", txt)
    print(f"\n→ 存到 {p}")


def cmd_question(qid: str, n: int = 10) -> None:
    d = question(qid, n)
    if not d:
        print("取不到（或 cookie 失效）")
        return
    lines = [f"# {d['title']}", "", f"> {d['answer_count']} 个回答", ""]
    if d["detail"]:
        lines += [d["detail"], ""]
    for i, a in enumerate(d["answers"], 1):
        lines += [f"## 回答 {i}（{a['votes']} 赞 · {a['author']}）", "", a["text"], ""]
    txt = "\n".join(lines)
    print(txt[:2500])
    p = _save(f"问题_{d['title']}", txt)
    print(f"\n→ 存到 {p}")


def cmd_answer(aid: str) -> None:
    d = answer(aid)
    if not d:
        print("取不到（或 cookie 失效）")
        return
    txt = f"# {d['question']}\n\n> {d['votes']} 赞 · {d['author']}\n\n{d['text']}"
    print(txt[:2500])
    p = _save(f"回答_{d['question']}", txt)
    print(f"\n→ 存到 {p}")


def main() -> None:
    ok, msg = check()
    print(f"[cookie] {msg}")
    if not ok:
        print("\n需要 z_c0：浏览器打开知乎 → F12 → Application → Cookies → https://www.zhihu.com → 复制 z_c0 的值")
        print("然后二选一：")
        print("  ① 存进 memory/zhihu_cookies.json：{\"z_c0\": \"值\"}")
        print("  ② 设环境变量 ZHIHU_Z_C0=值")
        return
    args = sys.argv[1:]
    if not args:
        print("用法: search <词> | question <id> | answer <id>")
        return
    cmd = args[0]
    if cmd == "search" and len(args) > 1:
        cmd_search(" ".join(args[1:]))
    elif cmd == "question" and len(args) > 1:
        cmd_question(args[1], int(args[2]) if len(args) > 2 else 10)
    elif cmd == "answer" and len(args) > 1:
        cmd_answer(args[1])
    else:
        print("用法: search <词> | question <id> | answer <id>")


if __name__ == "__main__":
    main()
