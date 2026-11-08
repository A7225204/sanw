"""生成「蓝色背景 + 大肥鱼说话」的竖屏视频（嘴动 / 眨眼 / 字幕）。

用法：
    python tools/make_bili_video.py "要念的文字" -o out.mp4
    python tools/make_bili_video.py --file 稿子.txt -o out.mp4

流程：
    TTS(9881, IndexTTS2) -> 音频
    音频 RMS 逐帧 -> 嘴张开幅度
    15fps 逐帧画：蓝底渐层 + 角色（白底抠掉） + 嘴 + 眨眼 + 底部字幕
    ffmpeg 合成 H.264 + AAC
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CHAR = ROOT / "表情包" / "开心" / "keai.png"
OUT_DIR = ROOT / "data" / "bili_video"
FONT = [r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf"]
W, H = 1920, 1080
FPS = 15
# 角色图（1254²）上的关键点，按比例换算




def _ffmpeg() -> str:
    for c in (os.getenv("DSH_FFMPEG_PATH"), str(ROOT / "data" / "bin" / "ffmpeg.exe"),
              r"D:/FormatFactory/ffmpeg.exe"):
        if c and os.path.exists(c):
            return c
    return "ffmpeg"


def _ffprobe() -> str:
    p = _ffmpeg()
    return p[:-4] + "ffprobe.exe" if p.endswith(".exe") else "ffprobe"


def _font(size: int) -> ImageFont.FreeTypeFont:
    for p in FONT:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:  # noqa: BLE001
                continue
    return ImageFont.load_default()


def _dur(path: Path) -> float:
    ff = _ffmpeg()
    probe = ff[:-4] + "ffprobe.exe" if ff.endswith(".exe") else "ffprobe"
    try:
        out = subprocess.run([probe, "-v", "error", "-show_entries", "format=duration",
                              "-of", "default=nw=1:nk=1", str(path)],
                             capture_output=True, text=True, timeout=60).stdout.strip()
        return float(out or 0)
    except Exception:  # noqa: BLE001
        return 0.0


def tts(text: str) -> Path:
    """单段 TTS（<=110 字），走本地 9881。"""
    import json
    import urllib.request
    out = Path(tempfile.mkdtemp(prefix="bili_tts_")) / "a.mp3"
    body = json.dumps({"text": text[:110], "out": out.as_posix()}, ensure_ascii=False).encode()
    req = urllib.request.Request("http://127.0.0.1:9881/tts", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        d = json.loads(r.read())
    p = Path(d.get("path") or "")
    if not p.exists():
        raise RuntimeError(f"TTS 失败：{d}")
    return p


def split_chunks(text: str, limit: int = 110) -> list[str]:
    """按标点切句，再把句子拼成不超过 limit 字的块（避免 TTS 截断）。"""
    import re as _re
    sents = [s.strip() for s in _re.split(r"(?<=[。！？!?；;])", text) if s.strip()]
    chunks, cur = [], ""
    for s in sents:
        while len(s) > limit:            # 超长句硬切
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(s[:limit])
            s = s[limit:]
        if len(cur) + len(s) <= limit:
            cur += s
        else:
            if cur:
                chunks.append(cur)
            cur = s
    if cur:
        chunks.append(cur)
    return chunks or [text[:limit]]


def tts_spans(text: str, gap: float = 0.35):
    """分段合成 + 拼接，返回 (合并后的 wav, [(start, end, 文本)])。"""
    chunks = split_chunks(text)
    parts = []
    for i, ch in enumerate(chunks):
        mp3 = tts(ch)
        parts.append((mp3, ch, _dur(mp3)))
    # 用 ffmpeg concat 拼成一条 wav（中间补 gap 静音）
    tmp = Path(tempfile.mkdtemp(prefix="bili_join_"))
    sil = tmp / "sil.mp3"
    subprocess.run([_ffmpeg(), "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", f"anullsrc=r=24000:cl=mono", "-t", str(gap),
                    "-c:a", "libmp3lame", str(sil)], check=False)
    seq, spans, cur = [], [], 0.0
    for i, (mp3, ch, dur) in enumerate(parts):
        seq.append(mp3)
        spans.append((cur, cur + dur, ch))
        cur += dur
        if i < len(parts) - 1:
            seq.append(sil)
            cur += gap
    lst = tmp / "list.txt"
    lst.write_text("".join(f"file '{p.as_posix()}'" + chr(10) for p in seq), encoding="utf-8")
    wav = tmp / "all.wav"
    subprocess.run([_ffmpeg(), "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                    "-i", str(lst), "-ar", "24000", "-ac", "1", str(wav)], check=False)
    if not wav.exists():
        raise RuntimeError("音频拼接失败")
    return wav, spans


def to_wav(src: Path) -> Path:
    ff = _ffmpeg()
    wav = src.with_suffix(".wav")
    subprocess.run([ff, "-y", "-loglevel", "error", "-i", str(src),
                    "-ar", "16000", "-ac", "1", str(wav)], check=False)
    return wav


def envelope(wav: Path, frames: int) -> list[float]:
    """按帧数算 RMS 包络，归一到 0~1。"""
    with wave.open(str(wav)) as w:
        sr = w.getframerate()
        data = w.readframes(w.getnframes())
    import array
    arr = array.array("h")
    arr.frombytes(data)
    per = max(1, len(arr) // max(1, frames))
    out = []
    for i in range(frames):
        chunk = arr[i * per:(i + 1) * per]
        if not chunk:
            out.append(0.0)
            continue
        rms = math.sqrt(sum(x * x for x in chunk) / len(chunk)) / 32768.0
        out.append(rms)
    mx = max(out) or 1e-6
    return [min(1.0, v / mx) for v in out]


def _cut_white(im: Image.Image, thr: int = 238) -> Image.Image:
    """只抠「从边缘连进来的白底」，不碰裙子/围裙内部的白（洪水填充）。"""
    from collections import deque
    im = im.convert("RGBA")
    w, h = im.size
    small = im.convert("RGB").resize((320, 320), Image.BILINEAR)
    sx, sy = small.size
    px = small.load()
    white = [[False] * sx for _ in range(sy)]
    for y in range(sy):
        for x in range(sx):
            r, g, b = px[x, y]
            white[y][x] = r > thr and g > thr and b > thr
    bg = [[False] * sx for _ in range(sy)]
    q = deque()
    for x in range(sx):
        for y in (0, sy - 1):
            if white[y][x] and not bg[y][x]:
                bg[y][x] = True
                q.append((x, y))
    for y in range(sy):
        for x in (0, sx - 1):
            if white[y][x] and not bg[y][x]:
                bg[y][x] = True
                q.append((x, y))
    while q:
        x, y = q.popleft()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < sx and 0 <= ny < sy and white[ny][nx] and not bg[ny][nx]:
                bg[ny][nx] = True
                q.append((nx, ny))
    mask_small = Image.new("L", (sx, sy), 255)
    mp = mask_small.load()
    for y in range(sy):
        for x in range(sx):
            if bg[y][x]:
                mp[x, y] = 0
    mask = mask_small.resize((w, h), Image.BILINEAR)
    im.putalpha(mask)
    return im


def _bg() -> Image.Image:
    """深藏青渐变 + 四周暗角（对齐参考图那种深夜感）。"""
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    for y in range(H):
        t2 = y / H
        r = int(14 + 6 * t2)
        g = int(30 + 8 * t2)
        b = int(52 + 10 * t2)
        d.line([(0, y), (W, y)], fill=(r, g, b))
    # 四角压暗（用两条线性渐变叠，避免椭圆暗角在宽屏上出现横带）
    dark = Image.new("RGB", (W, H), (3, 8, 16))
    hz = Image.new("L", (W, H), 0)
    hd = ImageDraw.Draw(hz)
    for y in range(H):
        k = abs(y / H - 0.5) * 2
        v = int(46 * max(0.0, k - 0.35))
        hd.line([(0, y), (W, y)], fill=v)
    hz = hz.filter(ImageFilter.GaussianBlur(60))
    vt = Image.new("L", (W, H), 0)
    vd = ImageDraw.Draw(vt)
    for x in range(W):
        k = abs(x / W - 0.5) * 2
        v = int(40 * max(0.0, k - 0.45))
        vd.line([(x, 0), (x, H)], fill=v)
    vt = vt.filter(ImageFilter.GaussianBlur(80))
    m = Image.merge("L", ImageChops.lighter(hz, vt)) if False else None
    from PIL import ImageChops
    m = ImageChops.lighter(hz, vt)
    return Image.composite(dark, img, m)


def _day_index() -> int:
    """DAY N：从最早一条聊天记录算起（找不到就从 1 开始）。"""
    try:
        import sqlite3
        db = Path(os.environ.get("APPDATA") or "") / "dsh-pet-standalone-webm-chat" / "shared_memory.db"
        con = sqlite3.connect(str(db), timeout=5)
        row = con.execute("select min(timestamp) from chat_history").fetchone()
        if row and row[0]:
            from datetime import date
            first = date.fromisoformat(str(row[0])[:10])
            return max(1, (date.today() - first).days + 1)
    except Exception:  # noqa: BLE001
        pass
    return 1


def _header(d: ImageDraw.ImageDraw, day: int) -> None:
    """左上角 DAY N + 细线。"""
    font = _font(44)
    txt = f"DAY {day}"
    d.text((96, 74), txt, font=font, fill=(126, 176, 226))
    d.line([(96, 140), (96 + 300, 140)], fill=(78, 124, 178), width=3)


def _caption(draw: ImageDraw.ImageDraw, text: str) -> None:
    """左下角左对齐字幕（参考图那种排版）。"""
    font = _font(46)
    x0, y0 = 96, H - 190
    line, lines = "", []
    for ch in text:
        if font.getlength(line + ch) > int(W * 0.42) and line:
            lines.append(line)
            line = ch
        else:
            line += ch
    if line:
        lines.append(line)
    # 避免最后一行只剩一两个字（把上一行末尾挪下来）
    if len(lines) >= 2 and len(lines[-1]) <= 2 and len(lines[-2]) > 3:
        lines[-1] = lines[-2][-1] + lines[-1]
        lines[-2] = lines[-2][:-1]
    for i, ln in enumerate(lines[:3]):
        y = y0 + i * 64
        draw.text((x0 + 2, y + 2), ln, font=font, fill=(0, 0, 0))
        draw.text((x0, y), ln, font=font, fill=(240, 246, 255))


LF_BASE = "http://127.0.0.1:7860/api/v1/lf"


def _lf(method: str, path: str, payload=None, timeout=120, raw=False):
    import json as _json
    import urllib.parse
    import urllib.request
    url = LF_BASE + path
    data = None
    headers = {}
    if payload is not None:
        data = _json.dumps(payload, ensure_ascii=False).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
    return body if raw else _json.loads(body)


def _srt_spans(srt_text: str) -> list:
    """解析 SRT → [(start, end, 文本)]。"""
    def _secs(s: str) -> float:
        h, m, rest = s.split(":")
        sec, ms = rest.replace(".", ",").split(",")
        return int(h) * 3600 + int(m) * 60 + int(sec) + int(ms) / 1000.0
    spans, cur = [], None
    for line in srt_text.replace(chr(13)+chr(10), chr(10)).split(chr(10)):
        if "-->" in line:
            a, b = line.split("-->")
            cur = [_secs(a.strip()), _secs(b.strip()), ""]
        elif cur is not None and line.strip() and not line.strip().isdigit():
            if cur[2]:
                cur[2] += " " + line.strip()
            else:
                cur[2] = line.strip()
        elif cur is not None and not line.strip():
            if cur[2]:
                spans.append(tuple(cur))
            cur = None
    if cur and cur[2]:
        spans.append(tuple(cur))
    return spans


def _ensure_ref() -> str:
    """拿到长篇工作台的音色 ref：优先本地缓存，其次上传 prompts 里的音色。"""
    import json as _json
    import time as _t
    import urllib.request
    cfg = ROOT / "memory" / "bili_tts_ref.json"
    rid = ""
    try:
        rid = str((_json.loads(cfg.read_text(encoding="utf-8")) or {}).get("ref") or "")
    except Exception:  # noqa: BLE001
        rid = ""
    if rid:
        try:
            _lf("GET", "/refs/%s/info" % rid, timeout=10)
            return rid
        except Exception:  # noqa: BLE001
            rid = ""
    import os as _os
    dirs = [ROOT / "prompts", Path(_os.getenv("LONGFORM_DIR") or r"D:/index-tts") / "prompts"]
    cands = []
    for d in dirs:
        if d.is_dir():
            cands += sorted(d.glob("*.wav"))
    env_wav = _os.getenv("BILI_REF_WAV")
    if env_wav and Path(env_wav).exists():
        cands = [Path(env_wav)] + cands
    if not cands:
        raise RuntimeError("没有可用音色文件（prompts/*.wav）")
    src = cands[0]
    boundary = "----bili" + str(int(_t.time()))
    CRLF = bytes([13, 10])
    body = b""
    body += ("--" + boundary).encode() + CRLF
    body += ('Content-Disposition: form-data; name="file"; filename="' + src.name + '"').encode() + CRLF
    body += b"Content-Type: audio/wav" + CRLF + CRLF + src.read_bytes() + CRLF
    body += ("--" + boundary).encode() + CRLF
    body += b'Content-Disposition: form-data; name="name"' + CRLF + CRLF + "大肥鱼".encode() + CRLF
    body += ("--" + boundary + "--").encode() + CRLF
    req = urllib.request.Request(LF_BASE + "/refs", data=body, method="POST",
                                 headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    with urllib.request.urlopen(req, timeout=120) as r:
        d = _json.loads(r.read())
    rid = str(((d.get("ref") or {}).get("id")) or "")
    if not rid:
        raise RuntimeError("上传音色失败：%s" % d)
    cfg.write_text(_json.dumps({"ref": rid}, ensure_ascii=False), encoding="utf-8")
    return rid


def tts_spans_longform(text: str):
    """走长篇工作台：分段渲染 → 导出 wav + srt → 按 SRT 时间轴返回字幕段。"""
    import json as _json
    import tempfile
    import time as _time
    import urllib.parse

    import re as _re
    sents = [s.strip() for s in _re.split(r"(?<=[。！？!?；;])", text) if s.strip()]
    lined = chr(10).join(sents) if sents else text      # 每句一行 → SRT 就是逐句字幕
    sid = _lf("POST", "/scripts", {"text": lined, "title": "B站口播",
                                   "max_chars": 200, "mode": "sentence"})["id"]
    rid = _ensure_ref()
    _lf("PATCH", "/scripts/" + sid, {"defaults": {"ref": rid}})
    job = (_lf("POST", "/scripts/%s/render" % sid, {"mode": "all"}).get("render") or {})
    job_id = job.get("id") or ""
    deadline = _time.time() + 900
    while _time.time() < deadline:
        _time.sleep(3)
        r = (_lf("GET", "/scripts/%s/render" % sid).get("render") or {})
        if job_id and r.get("id") and r.get("id") != job_id:
            continue                     # 还在看上一轮的任务，别被旧状态误导
        st = r.get("state")
        if st == "done":
            break
        if st in ("error", "aborted"):
            raise RuntimeError("长篇渲染失败：%s" % (r.get("error") or st))
    else:
        raise RuntimeError("长篇渲染超时")
    _time.sleep(1.0)                      # 等导出清单刷完，别读到上一版 srt
    info = _lf("POST", "/scripts/%s/export" % sid, {"format": "wav"})
    name = info.get("file") or ""
    if not name:
        raise RuntimeError("导出没拿到文件名")
    tmp = Path(tempfile.mkdtemp(prefix="bili_lf_"))
    wav = tmp / "all.wav"
    wav.write_bytes(_lf("GET", "/scripts/%s/export/%s" % (sid, urllib.parse.quote(name)),
                        raw=True, timeout=300))
    srt_name = name.rsplit(".", 1)[0] + ".srt"
    spans = []
    try:
        srt = _lf("GET", "/scripts/%s/export/%s" % (sid, urllib.parse.quote(srt_name)),
                  raw=True, timeout=120).decode("utf-8", "replace")
        spans = _srt_spans(srt)
    except Exception:  # noqa: BLE001
        spans = []
    if not spans:
        raise RuntimeError("没拿到 SRT")
    print(f"[B站视频] 长篇工作台：{len(spans)} 句，音频 {round(float(info.get('duration') or 0), 1)}s")
    return wav, spans


def _mouth_region(img: Image.Image) -> tuple[int, int, int, int] | None:
    """在人物立绘里找嘴（偏深的红/粉色），取最大簇的 bbox。"""
    im = img.convert("RGBA")
    w, h = im.size
    px = im.load()
    pts = []
    for y in range(int(h * 0.40), int(h * 0.62)):
        for x in range(int(w * 0.25), int(w * 0.75)):
            r, g, b, a = px[x, y]
            # 嘴是暖红/鲑鱼色：红远高于绿蓝，且不是皮肤那种高亮
            # 只要嘴腔那一块（深红/暗粉），不要腮红和皮肤
            if a > 200 and 140 < r < 240 and g < 180 and (r - g) > 55 and (r - b) > 45:
                pts.append((x, y))
    if len(pts) < 60:
        return None
    # 用中位数 + 1.2 倍标准差圈出主簇，排除边缘腮红
    xs = sorted(p[0] for p in pts)
    ys = sorted(p[1] for p in pts)
    mx, my = xs[len(xs) // 2], ys[len(ys) // 2]
    keep = [(x, y) for x, y in pts if abs(x - mx) < 70 and abs(y - my) < 45]
    if len(keep) < 60:
        keep = pts
    xs = [p[0] for p in keep]
    ys = [p[1] for p in keep]
    pad_x = 6
    pad_y = 6
    return (max(0, min(xs) - pad_x), max(0, min(ys) - pad_y),
            min(w, max(xs) + pad_x), min(h, max(ys) + pad_y))


def _mouth_layer(img: Image.Image):
    """返回 (嘴部贴片, 羽化遮罩, 位置)；说话时把贴片纵向拉伸就是"真张嘴"。"""
    box = _mouth_region(img)
    if not box:
        return None
    patch = img.crop(box).convert("RGBA")
    mask = Image.new("L", patch.size, 0)
    d = ImageDraw.Draw(mask)
    d.ellipse((-patch.size[0] * 0.1, -patch.size[1] * 0.2,
               patch.size[0] * 1.1, patch.size[1] * 1.2), fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(max(2, patch.size[1] // 6)))
    return patch, mask, (box[0], box[1])


def _speak(frame: Image.Image, mouth, amp: float) -> None:
    """把嘴贴片按 amp（0~1）纵向拉伸后贴回去，模拟张嘴。"""
    if not mouth:
        return
    patch, mask, (mx, my) = mouth
    nh = max(1, int(patch.size[1] * (1.0 + 0.55 * max(0.0, min(1.0, amp)))))
    p2 = patch.resize((patch.size[0], nh), Image.BICUBIC)
    m2 = mask.resize((patch.size[0], nh), Image.BICUBIC)
    frame.paste(p2, (mx, my), m2)


def build(text: str, out: Path, min_seconds: float = 0.0) -> Path:
    """合成视频：分段 TTS → 拼接音频 → 逐帧直接喂给 ffmpeg（不落 PNG，省磁盘）。"""
    import os as _os
    wav = None
    spans = []
    if _os.getenv("BILI_LONGFORM", "1") != "0":
        try:
            wav, spans = tts_spans_longform(text)
        except Exception as _e:  # noqa: BLE001
            print(f"[B站视频] 长篇工作台不可用（{type(_e).__name__}: {_e}），回退分段 TTS")
    if not wav:
        wav, spans = tts_spans(text)
    with wave.open(str(wav)) as w:
        dur = w.getnframes() / w.getframerate()
    if min_seconds and dur < min_seconds:
        need = int(min_seconds * 5.4)
        print(f"[B站视频] ⚠ 时长只有 {dur:.0f}s，未到 {min_seconds:.0f}s；"
              f"这个文案约 {len(text)} 字，至少写 {need} 字才够")
    n = max(1, int(dur * FPS))
    env = envelope(wav, n)

    char = _cut_white(Image.open(CHAR))
    _bb = char.getbbox()
    if _bb:
        char = char.crop(_bb)          # 裁掉透明边，嘴/眼坐标按内容框算才准
    scale = int(H * 0.98)
    _cw, _ch = char.size
    char = char.resize((scale, int(scale * _ch / max(1, _cw))), Image.LANCZOS)
    cx = W - scale + int(scale * 0.10)
    cy = H - scale + int(scale * 0.07)
    k = scale / 1254.0
    base = _bg()
    char_layer = base.copy()
    char_layer.paste(char, (cx, cy), char)
    mouth = _mouth_layer(char_layer) if _os.getenv("BILI_LIPSYNC", "0") != "0" else None
    day = _day_index()

    out.parent.mkdir(parents=True, exist_ok=True)
    ff = _ffmpeg()
    cmd = [ff, "-y", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
           "-i", str(wav),
           "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-crf", "23",
           "-c:a", "aac", "-b:a", "128k", "-shortest", str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for i in range(n):
        t0 = i / FPS
        img = char_layer.copy()
        _speak(img, mouth, env[i])
        d = ImageDraw.Draw(img)
        _header(d, day)
        _cur = None
        for a, b, s in spans:
            if a <= t0:
                _cur = s
            if a <= t0 < b:
                break
        if _cur:
            _caption(d, _cur)
        try:
            proc.stdin.write(img.tobytes())
        except BrokenPipeError:
            break
    try:
        proc.stdin.close()
    except Exception:  # noqa: BLE001
        pass
    proc.wait()
    if not out.exists() or out.stat().st_size < 10000:
        raise RuntimeError("合成失败")
    print(f"[B站视频] {out}  {out.stat().st_size // 1024}KB  {dur:.1f}s  {n} 帧  {len(spans)} 段")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("text", nargs="?", default="")
    ap.add_argument("--file")
    ap.add_argument("-o", "--out", default=str(OUT_DIR / "feiyu.mp4"))
    ap.add_argument("--min-seconds", type=float, default=120.0)
    a = ap.parse_args()
    txt = Path(a.file).read_text(encoding="utf-8").strip() if a.file else a.text
    if not txt:
        raise SystemExit("要一句话或 --file")
    build(txt, Path(a.out), min_seconds=a.min_seconds)


if __name__ == "__main__":
    main()
