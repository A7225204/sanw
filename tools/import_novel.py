# -*- coding: utf-8 -*-
"""把下载好的小说（txt / md，后面再扩 epub）转成互动小说剧本。

    python tools/import_novel.py 小说.txt
    python tools/import_novel.py 小说.txt --title 雨夜书店 --chunk 700

产出：memory/stories/<书名>.json，节点是一条线性链：
    每段 ~700 字挂一个节点，选项「接着往下读」/「跳一段」，
    想停下来聊这段就直接打字——引擎里自由输入不推进剧情，她会当场接话。

编码依次试 utf-8-sig / utf-8 / gb18030 / utf-16；章节按「第X章 / 第X节 / Chapter N」切。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STORY_DIR = ROOT / "memory" / "stories"

CHAPTER_RE = re.compile(
    r"^\s*(第[0-9零一二三四五六七八九十百千万两]+[章节回卷][^\n]{0,40}|Chapter\s+\d+[^\n]{0,40}|序章[^\n]{0,20}|尾声[^\n]{0,20})\s*$",
    re.M,
)
SENT_RE = re.compile(r"(?<=[。！？!?…」』”\"])\s*")


def read_text(path: Path) -> str:
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


def clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_chapters(text: str) -> list[tuple[str, str]]:
    parts = CHAPTER_RE.split(text)
    if len(parts) == 1:
        return [("正文", text)]
    chapters: list[tuple[str, str]] = []
    # split() 会把标题放在索引 1,3,5…，正文在 2,4,6…
    preface = parts[0].strip()
    if preface:
        chapters.append(("开头", preface))
    i = 1
    while i < len(parts):
        title = parts[i].strip()
        body = parts[i + 1].strip() if i + 1 < len(parts) else ""
        if title:
            chapters.append((title, body))
        i += 2
    return chapters or [("正文", text)]


def chunk(body: str, size: int) -> list[str]:
    out: list[str] = []
    buf = ""
    for para in body.split("\n"):
        para = para.strip()
        if not para:
            continue
        if len(buf) + len(para) + 1 <= size:
            buf = f"{buf}\n{para}".strip()
            continue
        if buf:
            out.append(buf)
        # 单段超长就按句号切
        while len(para) > size:
            cut = para.rfind("。", 0, size)
            cut = cut + 1 if cut > size // 2 else size
            out.append(para[:cut].strip())
            para = para[cut:].strip()
        buf = para
    if buf:
        out.append(buf)
    return [c for c in out if c]


def build(title: str, chapters: list[tuple[str, str]], size: int) -> dict:
    nodes: dict[str, dict] = {}
    order: list[str] = []
    meta: dict[str, str] = {}

    for ci, (ctitle, body) in enumerate(chapters, 1):
        pieces = chunk(body, size) or [""]
        for k, piece in enumerate(pieces, 1):
            nid = f"c{ci:03d}_{k:02d}"
            scene = (f"【{ctitle}】\n{piece}" if k == 1 else piece)
            nodes[nid] = {"scene": scene, "choices": []}
            order.append(nid)
            meta[nid] = ctitle

    for idx, nid in enumerate(order):
        node = nodes[nid]
        nxt = order[idx + 1] if idx + 1 < len(order) else None
        skip = order[idx + 2] if idx + 2 < len(order) else nxt
        if nxt:
            node["choices"].append({"text": "接着往下读", "next": nxt})
            if skip and skip != nxt:
                node["choices"].append({"text": "跳一段", "next": skip})
        else:
            node["ending"] = True
            node["choices"].append({"text": "合上书", "next": None, "result": "（合上书，尾巴晃了晃）念完了。"})

    total = sum(len(n["scene"]) for n in nodes.values())
    return {
        "id": "novel-" + re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "-", title).strip("-"),
        "title": title,
        "mode": "read",
        "context": (
            f"你正在给主人念小说《{title}》。念的时候用你自己的嘴：平时嘴损、偶尔吐槽，"
            "关键处会认真，但不要改剧情、不要替作者总结大道理。"
        ),
        "intro": f"《{title}》，{len(chapters)} 章 / {len(nodes)} 段 / 约 {total} 字。我会一段一段念给你听。",
        "start": order[0],
        "nodes": nodes,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="txt / md 文件")
    ap.add_argument("--title", default="", help="书名（默认用文件名）")
    ap.add_argument("--chunk", type=int, default=700, help="每段字数，默认 700")
    ap.add_argument("--outdir", default=str(STORY_DIR))
    args = ap.parse_args()

    src = Path(args.path)
    if not src.exists():
        print(f"找不到文件：{src}")
        return 2

    title = (args.title or src.stem).strip()
    text = clean(read_text(src))
    if len(text) < 200:
        print(f"太短了（{len(text)} 字），不像一本小说")
        return 2

    chapters = split_chapters(text)
    story = build(title, chapters, max(200, args.chunk))

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    out = outdir / f"{re.sub(r'[\\\\/:*?\"<>|]', '_', title)}.json"
    out.write_text(json.dumps(story, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"写好：{out}")
    print(f"  书名 {title} · {len(chapters)} 章 · {len(story['nodes'])} 段 · 约 {len(text)} 字 · 每段 ≤{args.chunk}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
