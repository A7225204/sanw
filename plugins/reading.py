# -*- coding: utf-8 -*-
"""让大肥鱼自己读小说：后台分章读、生成摘要/人物/金句，聊天时能和你讨论剧情。

    /读 书名        开始读（没有就导入）或继续读一章
    /书单            看有哪些书、读到哪
    /读书            当前在读的进度
    /读 书名 3       一次读 3 章

数据：memory/reading/<slug>.json
    {title, file, total_chars, chapters:[{idx,title,summary,people,quote,read_at}],
     progress, overall}

设计：
- 导入时只切章、不调模型（快）；读一章才调一次 LLM（慢，后台跑）
- 章节摘要会写进长期记忆（kind="reading"），以后聊天能引用
- 聊天时按问题关键词检索相关章摘要，注入 volatile 段（不动缓存前缀）
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
READ_DIR = ROOT / "memory" / "reading"

ENABLED = True
CHAPTER_RE = re.compile(
    r"^\s*(第[0-9零一二三四五六七八九十百千万两]+[章节回卷][^\n]{0,40}|Chapter\s+\d+[^\n]{0,40}|序章[^\n]{0,20}|尾声[^\n]{0,20})\s*$",
    re.M,
)


# ---------------------------------------------------------------- 基础
def _slug(title: str) -> str:
    s = re.sub(r"[^\w\u4e00-\u9fa5]+", "_", title).strip("_")
    return s or "book"


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gb18030", "utf-16"):
        try:
            text = raw.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
        cjk = sum(1 for ch in text[:4000] if "\u4e00" <= ch <= "\u9fff")
        if cjk >= 20 or enc in ("utf-8-sig", "utf-8"):
            return text
    return raw.decode("utf-8", "ignore")


def _split_chapters(text: str) -> list[tuple[str, str]]:
    parts = CHAPTER_RE.split(text)
    if len(parts) == 1:
        return [("正文", text)]
    out: list[tuple[str, str]] = []
    preface = parts[0].strip()
    if preface:
        out.append(("开头", preface))
    i = 1
    while i < len(parts):
        title = parts[i].strip()
        body = parts[i + 1].strip() if i + 1 < len(parts) else ""
        if title:
            out.append((title, body))
        i += 2
    return out or [("正文", text)]


def _load(slug: str) -> dict | None:
    p = READ_DIR / f"{slug}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _save(d: dict) -> None:
    READ_DIR.mkdir(parents=True, exist_ok=True)
    (READ_DIR / f"{_slug(d.get('title') or 'book')}.json").write_text(
        json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def list_books() -> list[dict]:
    out = []
    if not READ_DIR.exists():
        return out
    for p in sorted(READ_DIR.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        out.append({
            "slug": p.stem, "title": d.get("title") or p.stem,
            "chapters": len(d.get("chapters") or []),
            "progress": int(d.get("progress") or 0),
        })
    return out


# ---------------------------------------------------------------- 导入
def import_book(path: str | Path, title: str | None = None) -> dict:
    """把 txt/md 切成章节存起来（不调模型）。"""
    p = Path(path)
    if not p.exists():
        return {"ok": False, "error": f"文件不存在：{p}"}
    text = _read_text(p).replace("\r\n", "\n").replace("\r", "\n")
    title = title or p.stem
    chapters = _split_chapters(text)
    # 单章太长的切成多段（每段 ~3500 字，一次 LLM 好处理）
    rows: list[dict] = []
    for ci, (ch_title, body) in enumerate(chapters):
        body = body.strip()
        if not body:
            continue
        segs = [body[i:i + 3500] for i in range(0, len(body), 3500)]
        for si, seg in enumerate(segs):
            rows.append({
                "idx": len(rows),
                "title": ch_title if si == 0 else f"{ch_title}（续{si}）",
                "text": seg,
                "chars": len(seg),
                "summary": "",
                "people": [],
                "quote": "",
                "read_at": "",
            })
    d = {
        "title": title, "file": str(p), "created": time.strftime("%Y-%m-%d %H:%M"),
        "total_chars": sum(r["chars"] for r in rows),
        "chapters": rows, "progress": 0, "overall": "",
    }
    _save(d)
    return {"ok": True, "title": title, "chapters": len(rows), "chars": d["total_chars"]}


# ---------------------------------------------------------------- 读
async def _summarize(ch_title: str, text: str) -> dict:
    from server import memory_growth as mg
    prompt = (
        "你在读书。请把这一章读完后，用 JSON 输出：\n"
        '{"summary":"120字以内的本章剧情摘要","people":["出场人物"],"quote":"一句最有印象的原句或台词"}\n'
        "只输出 JSON。\n\n"
        f"章节：{ch_title}\n正文：\n{text[:4000]}"
    )
    raw = await mg._ask_llm(prompt)
    s = str(raw or "").strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.S)
    try:
        d = json.loads(s[s.find("{"): s.rfind("}") + 1])
    except Exception:  # noqa: BLE001
        d = {}
    return {
        "summary": str(d.get("summary") or "")[:240],
        "people": [str(x)[:12] for x in (d.get("people") or [])][:8],
        "quote": str(d.get("quote") or "")[:80],
    }


async def read_chapter(slug: str, count: int = 1) -> dict:
    """从 progress 开始读 count 章，返回读到的章节标题。"""
    d = _load(slug)
    if not d:
        return {"ok": False, "error": f"没有这本书：{slug}"}
    chapters = d.get("chapters") or []
    done: list[str] = []
    for _ in range(max(1, count)):
        idx = int(d.get("progress") or 0)
        if idx >= len(chapters):
            break
        ch = chapters[idx]
        try:
            got = await _summarize(ch.get("title") or f"第{idx + 1}节", ch.get("text") or "")
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}", "done": done}
        ch["summary"] = got["summary"]
        ch["people"] = got["people"]
        ch["quote"] = got["quote"]
        ch["read_at"] = time.strftime("%Y-%m-%d %H:%M")
        ch.pop("text", None)   # 读完不留原文，省空间
        d["progress"] = idx + 1
        done.append(ch.get("title") or f"第{idx + 1}节")
        _save(d)
    # 读完 → 总摘要
    if d.get("progress") == len(chapters) and not d.get("overall"):
        try:
            from server import memory_growth as mg
            briefs = "\n".join(f"- {c.get('title')}：{c.get('summary')}" for c in chapters)
            raw = await mg._ask_llm(
                "把这本书的章节摘要汇成 200 字总评（主题、主线、你最喜欢哪段），只输出正文：\n" + briefs[:5000])
            d["overall"] = str(raw or "").strip()[:400]
            _save(d)
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "book": d.get("title"), "done": done,
            "progress": d.get("progress"), "total": len(chapters)}


# ---------------------------------------------------------------- 给聊天用
def prompt_line() -> str:
    """最近在读什么（给 volatile 段，一行）。"""
    books = [b for b in list_books() if b["progress"] < b["chapters"]]
    if not books:
        return ""
    b = books[0]
    return f"· 你最近在读《{b['title']}》（读到 {b['progress']}/{b['chapters']} 章）"


def context_for(question: str, limit: int = 2) -> str:
    """按问题关键词找相关章节摘要，给聊天引用。"""
    q = str(question or "")
    if not q:
        return ""
    books = list_books()
    hits: list[str] = []
    words = re.findall(r"[一-龥]{2,4}", q)[:8] or [q.strip()]
    for b in books:
        d = _load(b["slug"])
        if not d:
            continue
        for ch in (d.get("chapters") or []):
            if not ch.get("summary"):
                continue
            text = f"{ch.get('title')} {ch.get('summary')} {' '.join(ch.get('people') or [])}"
            if any(w and w in text for w in words):
                hits.append(f"《{b['title']}》{ch.get('title')}：{ch.get('summary')}")
    return chr(10).join(hits[:limit])
# ---------------------------------------------------------------- QQ 命令
import asyncio as _asyncio
import logging as _logging

from nonebot import on_command
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent

_log = _logging.getLogger('reading')
read_cmd = on_command('读', priority=5, block=True)
shelf_cmd = on_command('书单', priority=5, block=False)
prog_cmd = on_command('读书', priority=5, block=False)


def _arg_of(event, name):
    txt = event.get_plaintext().strip()
    for prefix in ('/' + name, '／' + name, name):
        if txt.startswith(prefix):
            return txt[len(prefix):].strip()
    return ''


def _find_book(arg):
    books = list_books()
    if not books:
        return None
    if not arg:
        return books[0]
    for b in books:
        if arg in b['title'] or b['title'] in arg:
            return b
    return None


@read_cmd.handle()
async def _(event: MessageEvent):
    if isinstance(event, GroupMessageEvent) and not event.is_tome():
        return
    arg = _arg_of(event, '读')
    n = 1
    m = re.search(r'(\d+)\s*$', arg)
    if m:
        n = max(1, min(5, int(m.group(1))))
        arg = arg[:m.start()].strip()
    book = _find_book(arg)
    if not book:
        if arg.lower().endswith(('.txt', '.md')):
            res = import_book(arg, Path(arg).stem)
            if not res.get('ok'):
                await read_cmd.finish('导入失败：' + str(res.get('error')))
            await read_cmd.finish('《' + res['title'] + '》导入了（' + str(res['chapters']) + ' 节），发 /读 ' + res['title'] + ' 开始读')
        await read_cmd.finish('书架还空。发 /读 文件名.txt 导入')
    title = book['title']
    if book['progress'] >= book['chapters']:
        await read_cmd.finish('《' + title + '》已经读完了')

    async def _bg():
        try:
            await read_chapter(book['slug'], n)
        except Exception as e:  # noqa: BLE001
            _log.warning('[阅读] 失败：' + type(e).__name__ + ': ' + str(e))

    _asyncio.create_task(_bg())
    await read_cmd.finish('（翻到《' + title + '》第 ' + str(book['progress'] + 1) + ' 节…）读完告诉你')


@shelf_cmd.handle()
async def _(event: MessageEvent):
    books = list_books()
    if not books:
        await shelf_cmd.finish('书架还空。发 /读 文件名.txt 导入')
    lines = ['·《' + b['title'] + '》 ' + str(b['progress']) + '/' + str(b['chapters']) + ' 节' for b in books]
    await shelf_cmd.finish('书架：' + chr(10) + chr(10).join(lines))


@prog_cmd.handle()
async def _(event: MessageEvent):
    books = [b for b in list_books() if b['progress'] < b['chapters']]
    if not books:
        await prog_cmd.finish('现在没有在读的书')
    b = books[0]
    d = _load(b['slug']) or {}
    last = (d.get('chapters') or [{}])[max(0, b['progress'] - 1)]
    await prog_cmd.finish(
        '在读《' + b['title'] + '》' + str(b['progress']) + '/' + str(b['chapters']) + ' 节'
        + chr(10) + '上一节：' + str(last.get('title') or '—')
        + chr(10) + str(last.get('summary') or '（还没读）'))
