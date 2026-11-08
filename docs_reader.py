"""文档阅读：让 QQ 里发来的 PDF / Word / Excel / 文本也能被她看懂。

之前群里发文件等于没看见（`file` 段不处理）。这里补上：
    下载 → 按扩展名抽文字 → 截断 → 当成"某人发来一份文件，内容是……"进正常聊天流程

设计约束
--------
- 纯本地解析（pypdf / python-docx / openpyxl），**不调模型、不花 token**；
- 只抽文字，不解图片（扫描版 PDF 抽不出字，会明确回报"抽不到文字"）；
- 单份文件最多 3000 字，避免长文档把上下文撑爆；
- 任何失败返回空串，不编造内容。
"""
from __future__ import annotations

import asyncio
import re
import tempfile
from pathlib import Path

MAX_CHARS = 3000
SUPPORTED = {".pdf", ".docx", ".doc", ".xlsx", ".xlsm", ".csv", ".txt", ".md", ".json", ".log", ".py", ".html", ".htm"}


def _clean(text: str) -> str:
    t = re.sub(r"[ \t\u00a0]+", " ", str(text or ""))
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _extract_pdf(path: Path) -> str:
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    out = []
    for i, page in enumerate(reader.pages):
        if i >= 20:
            out.append("…（文档较长，后面省略）")
            break
        try:
            out.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001
            continue
    return "\n".join(out)


def _extract_docx(path: Path) -> str:
    import docx
    doc = docx.Document(str(path))
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    # 表格也抽一下（群友发的表格常有内容）
    for table in doc.tables[:5]:
        for row in table.rows[:20]:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def _extract_xlsx(path: Path) -> str:
    import openpyxl
    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    parts = []
    for ws in wb.worksheets[:3]:
        parts.append(f"【表 {ws.title}】")
        for row in ws.iter_rows(max_row=40, values_only=True):
            cells = [str(c) for c in row if c is not None]
            if cells:
                parts.append(" | ".join(cells))
    try:
        wb.close()
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(parts)


def extract_text(path: Path) -> str:
    """同步抽文字（调用方放 to_thread 里跑）。"""
    ext = path.suffix.lower()
    try:
        if ext == ".pdf":
            raw = _extract_pdf(path)
        elif ext in (".docx", ".doc"):
            raw = _extract_docx(path)
        elif ext in (".xlsx", ".xlsm"):
            raw = _extract_xlsx(path)
        elif ext in (".txt", ".md", ".json", ".log", ".py", ".html", ".htm", ".csv"):
            raw = path.read_text(encoding="utf-8", errors="ignore")
        else:
            return ""
        return _clean(raw)[:MAX_CHARS]
    except Exception:  # noqa: BLE001
        return ""


async def _fetch_file(data: dict, event=None) -> tuple[bytes, str]:
    """把 file 段落到本地：URL 优先，其次本地路径，最后问 NapCat。返回 (字节, 文件名)。"""
    name = str(data.get("file") or data.get("name") or "file").strip()
    url = str(data.get("url") or "").strip()
    if url.startswith("http"):
        try:
            import httpx
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as cli:
                r = await cli.get(url, headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code == 200 and r.content:
                    return r.content, name
        except Exception:  # noqa: BLE001
            pass
    for key in ("path", "file"):
        p = str(data.get(key) or "").strip()
        if p and Path(p).exists() and Path(p).is_file():
            try:
                return Path(p).read_bytes(), Path(p).name
            except Exception:  # noqa: BLE001
                pass
    if event is not None:
        try:
            info = await event.bot.call_api("get_file", file=str(data.get("file") or ""))
            p = str((info or {}).get("file") or "")
            if p and Path(p).exists():
                return Path(p).read_bytes(), Path(p).name
        except Exception:  # noqa: BLE001
            pass
    return b"", name


async def read_from_event(event) -> str:
    """把一个消息事件里的文件转成可读文字；没有文件或失败返回空串。"""
    try:
        segs = [s for s in event.get_message() if getattr(s, "type", "") == "file"]
    except Exception:  # noqa: BLE001
        return ""
    if not segs:
        return ""
    chunks: list[str] = []
    for seg in segs[:2]:
        data = getattr(seg, "data", {}) or {}
        try:
            raw, name = await _fetch_file(data, event)
            if not raw:
                continue
            suffix = Path(name).suffix.lower()
            if suffix not in SUPPORTED:
                chunks.append(f"（收到文件 {name}，这种格式暂时读不了）")
                continue
            tmpdir = Path(tempfile.mkdtemp(prefix="doc_"))
            fp = tmpdir / f"in{suffix}"
            fp.write_bytes(raw)
            text = await asyncio.to_thread(extract_text, fp)
            if not text:
                chunks.append(f"（收到文件 {name}，但没抽出文字，可能是扫描件或加密文档）")
                continue
            head = text if len(text) <= 900 else text[:900] + "…"
            chunks.append(f"（收到文件 {name}，节选如下）\n{head}")
        except Exception:  # noqa: BLE001
            continue
    return "\n\n".join(chunks)[:MAX_CHARS]
