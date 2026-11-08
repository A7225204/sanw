"""视频分镜：把一段视频压成「一张宫格图 + 带时间码的文本清单」。

灵感来自 dsh-video-storyboard（一张 384 token 的宫格图替代几十张关键帧），
这里用纯 Python 实现（cv2 解码 + PIL 拼图），不依赖 ffmpeg。

对外只有一个函数：
    build_sheet(video_path, start, end, cells=9, out_dir=None)
        → (png_path, cells_meta)
    build_overview(video_path, window=300, cells=9, out_dir=None)
        → [(png_path, cells_meta), ...]   长视频按窗口切，一分钟都不漏

设计取舍
--------
· 切点检测用 64x36 缩图的 HSV 差异（色相按环形距离、按饱和度降权），
  缩图上的一帧差异 ≈ 换镜头，不是"画面动了一下"。
· 格位按"信息密度"等分：切点密集的时段自动分到更短的格子。
· 清单只给原材料（时间、刀数），不下结论，判断交给模型。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw

try:
    from nonebot.log import logger
except Exception:  # noqa: BLE001
    import logging
    logger = logging.getLogger("storyboard")

W, H = 64, 36          # 切点检测用的缩略尺寸
SAMPLE_FPS = 3.0       # 每秒采样帧数（切点检测）
CUT_THRESHOLD = 26.0   # HSV 变化分阈值（0~255 尺度）
MAX_CUTS = 2000


# ---------------------------------------------------------------- 基础信息
def probe(path: str) -> dict:
    cap = cv2.VideoCapture(path)
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        dur = frames / fps if fps else 0.0
        return {"fps": round(fps, 2), "frames": int(frames), "w": w, "h": h,
                "duration": round(dur, 2)}
    finally:
        cap.release()


def _frame_at(path: str, t: float):
    cap = cv2.VideoCapture(path)
    try:
        cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, t) * 1000.0)
        ok, frame = cap.read()
        if not ok:
            return None
        return frame
    finally:
        cap.release()


# ---------------------------------------------------------------- 切点检测
def _ffmpeg() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return "ffmpeg"


_SCENE_THRESHOLD = float(os.getenv("STORYBOARD_SCENE_TH", "0.38") or "0.38")


def scan_cuts(path: str, start: float = 0.0, end: float | None = None,
              sample_fps: float = 0.0) -> list[float]:
    """用 ffmpeg 的 scene 分数找切点，返回秒数。

    以前自己算 HSV 差异：压缩噪点 + 运动也会被当成切点（200 秒测出 572 个刀），
    而且逐帧解码要跑一分钟。ffmpeg 的 scene 分是现成的、带关键帧跳的，快得多。
    """
    info = probe(path)
    dur = min(end if end is not None else info["duration"], info["duration"] or 0.0)
    if dur <= 0:
        return []
    ss = max(0.0, start)
    args = [_ffmpeg(), "-hide_banner", "-nostats", "-loglevel", "info"]
    if ss > 0:
        args += ["-ss", f"{ss:.3f}"]
    args += ["-t", f"{max(0.5, dur - ss):.3f}", "-i", path,
             "-vf", f"select='gt(scene,{_SCENE_THRESHOLD})',showinfo",
             "-an", "-f", "null", "-"]
    cuts: list[float] = []
    try:
        import subprocess
        proc = subprocess.run(args, capture_output=True, text=True, timeout=600)
        for line in (proc.stderr or "").splitlines():
            if "pts_time:" not in line:
                continue
            try:
                t = float(line.split("pts_time:")[1].split()[0])
            except Exception:  # noqa: BLE001
                continue
            cuts.append(round(t + ss, 2))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[分镜] ffmpeg 切点检测失败：{type(e).__name__}: {e}")
    cuts.sort()
    return cuts[:MAX_CUTS]


# ---------------------------------------------------------------- 格位分配
def alloc_cells(cuts: list[float], start: float, end: float, cells: int) -> list[tuple[float, float]]:
    """按信息密度（切点数）把 [start,end] 等分成 cells 格。"""
    cells = max(1, int(cells))
    if end <= start:
        return [(start, end)]
    total = end - start
    bounds = [start] + [c for c in cuts if start < c < end] + [end]
    weights = [(bounds[i + 1] - bounds[i]) ** 1.0 * (1.0 + 1.2 * (1 if i < len(bounds) - 2 else 0))
               for i in range(len(bounds) - 1)]
    wsum = sum(weights) or 1.0
    acc = 0.0
    segs: list[tuple[float, float]] = []
    cur = start
    target = wsum / cells
    for i, w in enumerate(weights):
        acc += w
        if acc >= target - 1e-9 and len(segs) < cells - 1:
            nxt = bounds[i + 1]
            if nxt - cur > 0.2:
                segs.append((round(cur, 2), round(nxt, 2)))
                cur = nxt
            acc = 0.0
    if end - cur > 0.2:
        segs.append((round(cur, 2), round(end, 2)))
    while len(segs) > cells:
        segs.pop()
    while len(segs) < cells:  # 补足：把最后一格再劈开
        a, b = segs[-1] if segs else (start, end)
        mid = (a + b) / 2
        if b - mid < 0.2:
            break
        segs[-1] = (a, round(mid, 2))
        segs.append((round(mid, 2), b))
    return segs


# ---------------------------------------------------------------- 拼图
def _fmt(t: float) -> str:
    t = max(0.0, t)
    return f"{int(t // 60):02d}:{t % 60:05.2f}"


def compose_sheet(frames: list[Image.Image], labels: list[str],
                  cols: int = 3, cell_w: int = 400, out_path: str | None = None) -> str:
    n = len(frames)
    cols = max(1, min(cols, n))
    rows = (n + cols - 1) // cols
    ratio = frames[0].height / max(1, frames[0].width) if frames else 0.5625
    cell_h = int(cell_w * (ratio or 0.5625))
    sheet_w, sheet_h = cols * cell_w, rows * cell_h
    sheet = Image.new("RGB", (sheet_w, sheet_h), (12, 14, 20))
    draw = ImageDraw.Draw(sheet)
    for i, im in enumerate(frames):
        r, c = divmod(i, cols)
        thumb = im.resize((cell_w, cell_h), Image.LANCZOS)
        sheet.paste(thumb, (c * cell_w, r * cell_h))
        label = labels[i] if i < len(labels) else str(i + 1)
        # 左下角黑底白字，直接烧进图里（时间码在文本清单里更精确）
        pad = 3
        text = f"{i + 1}  {label}"
        try:
            tw = draw.textlength(text, font=None)
        except Exception:  # noqa: BLE001
            tw = len(text) * 6
        draw.rectangle([c * cell_w + 4, r * cell_h + cell_h - 18,
                        c * cell_w + 10 + tw, r * cell_h + cell_h - 4], fill=(0, 0, 0))
        draw.text((c * cell_w + 6, r * cell_h + cell_h - 17), text, fill=(255, 235, 120))
    if out_path is None:
        out_path = os.path.join(tempfile.mkdtemp(prefix="storyboard_"), "sheet.png")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path, format="PNG", optimize=True)
    return out_path


def _quotes(subs: list[dict] | None, a: float, b: float, limit: int = 110) -> str:
    """把字幕里落在 [a,b] 的台词拼起来（按时间顺序，超长截断）。"""
    if not subs:
        return ""
    picked = [s for s in subs if float(s.get("to") or 0) > a and float(s.get("from") or 0) < b]
    txt = " ".join(str(s.get("text") or "") for s in picked).strip()
    return txt[:limit]


def build_sheet(path: str, start: float, end: float, cells: int = 9,
                out_path: str | None = None,
                cuts: list[float] | None = None,
                subs: list[dict] | None = None) -> tuple[str, list[dict], list[str]]:
    """一段视频 → 宫格图 + 每格元数据 + 文本清单行。"""
    info = probe(path)
    end = min(end, info["duration"] or end)
    start = max(0.0, start)
    if cuts is None:
        cuts = scan_cuts(path, start, end)
    segs = alloc_cells(cuts, start, end, cells)
    frames: list[Image.Image] = []
    meta: list[dict] = []
    lines: list[str] = []
    for i, (a, b) in enumerate(segs):
        mid = (a + b) / 2
        raw = _frame_at(path, mid)
        if raw is None:
            continue
        img = Image.fromarray(cv2.cvtColor(raw, cv2.COLOR_BGR2RGB))
        frames.append(img)
        n_cuts = sum(1 for c in cuts if a <= c < b)
        quote = _quotes(subs, a, b)
        meta.append({"index": i + 1, "start": a, "end": b,
                     "frame_at": round(mid, 2), "cuts": n_cuts, "quote": quote})
        lines.append(f"cell {i + 1} = {_fmt(a)}-{_fmt(b)}  cuts {n_cuts}")
        if quote:
            lines.append(f'    "{quote}"')
    if not frames:
        raise RuntimeError("没取到任何帧")
    png = compose_sheet(frames, [f"{_fmt(m['start'])}" for m in meta], cols=3, out_path=out_path)
    return png, meta, lines


def _coverage(sheets: list[dict], duration: float) -> tuple[int, float]:
    """采样帧数 + 最大盲区（两帧之间最多多少秒没人看过）。"""
    stamps = sorted(float(m["frame_at"]) for s in sheets for m in (s.get("meta") or []))
    if not stamps:
        return 0, 0.0
    prev = 0.0
    gap = 0.0
    for t in stamps:
        gap = max(gap, t - prev)
        prev = t
    gap = max(gap, float(duration or prev) - prev)
    return len(stamps), round(gap, 2)


def build_sheets(path: str, window: float = 300.0, cells: int = 9,
                 out_dir: str | None = None, max_sheets: int = 40,
                 subs: list[dict] | None = None) -> list[dict]:
    """长视频按 window 秒切窗，每窗一张宫格。返回 [{png, meta, lines, start, end}]。"""
    info = probe(path)
    dur = info["duration"] or 0.0
    if dur <= 0:
        return []
    if dur <= window:
        png, meta, lines = build_sheet(path, 0.0, dur, cells,
                                       out_path=os.path.join(out_dir, "sheet_0.png") if out_dir else None,
                                       subs=subs)
        out = [{"png": png, "meta": meta, "lines": lines, "start": 0.0, "end": dur}]
        n, gap = _coverage(out, dur)
        for sh in out:
            sh["samples"], sh["max_gap"] = n, gap
        return out
    # 整片只扫一次切点（长片按窗口反复扫会很慢）
    try:
        all_cuts = scan_cuts(path, 0.0, dur)
    except Exception:  # noqa: BLE001
        all_cuts = []
    out: list[dict] = []
    t = 0.0
    while t < dur - 1.0 and len(out) < max_sheets:
        e = min(dur, t + window)
        try:
            seg_cuts = [c for c in all_cuts if t <= c < e]
            png, meta, lines = build_sheet(path, t, e, cells,
                                           out_path=os.path.join(out_dir, f"sheet_{int(t)}.png")
                                           if out_dir else None,
                                           cuts=seg_cuts, subs=subs)
            out.append({"png": png, "meta": meta, "lines": lines,
                        "start": round(t, 2), "end": round(e, 2)})
        except Exception as e2:  # noqa: BLE001
            logger.warning(f"[分镜] {t:.0f}s 段失败：{type(e2).__name__}: {e2}")
        t = e
    n, gap = _coverage(out, dur)
    for sh in out:
        sh["samples"], sh["max_gap"] = n, gap
    return out


def sheet_text(sheets: list[dict], duration: float) -> str:
    """给模型的文本清单：整体结构 + 每格时间与刀数。"""
    head = f"storyboard: {len(sheets)} 张宫格，覆盖 {_fmt(duration)}，每格 9 格"
    if sheets:
        head += (f"；采样 {int(sheets[0].get('samples') or 0)} 帧，"
                 f"最大盲区 {float(sheets[0].get('max_gap') or 0):.1f} 秒")
    out = [head]
    for s in sheets:
        out.append(f"--- 段 {_fmt(s['start'])}-{_fmt(s['end'])} ---")
        out.extend(s["lines"])
    return "\n".join(out)
