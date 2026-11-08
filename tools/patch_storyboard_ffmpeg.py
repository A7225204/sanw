import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\plugins\storyboard.py")
s = p.read_text(encoding="utf-8")

# 1) 切点检测换成 ffmpeg 的 scene 分数（快且稳定），cv2 只用来取帧
start = s.find("def _hsv_diff(")
end = s.find("# ---------------------------------------------------------------- 格位分配")
if start > 0 and end > start:
    new = '''def _ffmpeg() -> str:
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


'''
    s = s[:start] + new + s[end:]

# 2) build_sheet 支持传入已算好的切点，避免重复扫描
s = s.replace(
    '''def build_sheet(path: str, start: float, end: float, cells: int = 9,
                out_path: str | None = None) -> tuple[str, list[dict], list[str]]:
    """一段视频 → 宫格图 + 每格元数据 + 文本清单行。"""
    info = probe(path)
    end = min(end, info["duration"] or end)
    start = max(0.0, start)
    cuts = scan_cuts(path, start, end)''',
    '''def build_sheet(path: str, start: float, end: float, cells: int = 9,
                out_path: str | None = None,
                cuts: list[float] | None = None) -> tuple[str, list[dict], list[str]]:
    """一段视频 → 宫格图 + 每格元数据 + 文本清单行。"""
    info = probe(path)
    end = min(end, info["duration"] or end)
    start = max(0.0, start)
    if cuts is None:
        cuts = scan_cuts(path, start, end)''')

p.write_text(s, encoding="utf-8")
print("切点检测换成 ffmpeg scene；build_sheet 支持复用切点")
