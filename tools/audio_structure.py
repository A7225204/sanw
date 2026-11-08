"""音频结构分析：BPM / 段落切分 / 能量曲线 / 人声-音乐比例。

灵感来自 dsh-soundtry（npm 上没有包，这里用 librosa 本地实现同一类能力）。

用法：
    python tools/audio_structure.py 音频或视频.mp3
    python tools/audio_structure.py https://www.bilibili.com/video/BVxxxx    # 有 yt-dlp 时自动下音轨
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
SR = 22050


def _ffmpeg() -> str:
    p = ROOT / "data" / "bin" / "ffmpeg.exe"
    return str(p) if p.exists() else "ffmpeg"


def load_audio(src: str) -> tuple[np.ndarray, str]:
    """把任意音视频转成单声道 wav 再读；URL 走 yt-dlp。"""
    import librosa

    tmp = Path(tempfile.mkdtemp(prefix="struct_"))
    if src.startswith("http"):
        try:
            import yt_dlp
        except Exception as e:  # noqa: BLE001
            raise SystemExit(f"没有 yt-dlp，无法下 URL：{e}")
        out = tmp / "a.%(ext)s"
        with yt_dlp.YoutubeDL({"format": "bestaudio/best", "outtmpl": str(out),
                               "quiet": True, "noplaylist": True}) as ydl:
            info = ydl.extract_info(src, download=True)
            path = Path(ydl.prepare_filename(info))
            title = info.get("title") or src
    else:
        path = Path(src)
        title = path.name
        if not path.exists():
            raise SystemExit(f"找不到文件：{path}")
    wav = tmp / "a.wav"
    subprocess.run([_ffmpeg(), "-y", "-loglevel", "error", "-i", str(path),
                    "-ac", "1", "-ar", str(SR), str(wav)], check=False)
    if not wav.exists():
        raise SystemExit("音频解码失败")
    y, _ = librosa.load(str(wav), sr=SR, mono=True)
    return y, title


def analyze(src: str) -> dict:
    import librosa

    y, title = load_audio(src)
    dur = len(y) / SR
    if dur < 3:
        raise SystemExit("太短了（<3 秒）")

    # BPM 与节拍
    tempo, beats = librosa.beat.beat_track(y=y, sr=SR, units="time")

    # 能量曲线（0.5 秒一格）
    hop = int(SR * 0.5)
    rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=hop)[0]
    rms_n = rms / (rms.max() or 1.0)

    # 段落切分：MFCC + 凝聚式聚类（比固定窗口更贴近“段落”）
    # 短音频聚类常常只切出一段，这里补一道「能量谷底」兜底
    try:
        mfcc = librosa.feature.mfcc(y=y, sr=SR, n_mfcc=13, hop_length=hop)
        k = max(2, min(8, int(round(dur / 25))))      # 每 ~25 秒一段，2~8 段
        bounds = librosa.segment.agglomerative(mfcc, k)
        times = librosa.frames_to_time(bounds, sr=SR, hop_length=hop)
    except Exception:  # noqa: BLE001
        times = np.linspace(0, dur, 3)

    def _sections_from(ts):
        out = []
        ts = [float(x) for x in ts]
        for i, t0 in enumerate(ts):
            t1 = ts[i + 1] if i + 1 < len(ts) else dur
            if t1 - t0 < 1.0:
                continue
            a, b = int(t0 / 0.5), max(int(t1 / 0.5), int(t0 / 0.5) + 1)
            lvl = float(np.mean(rms_n[a:b])) if len(rms_n[a:b]) else 0.0
            out.append({"start": round(float(t0), 1), "end": round(float(t1), 1),
                        "energy": round(lvl, 2)})
        return out

    sections = _sections_from(times)
    if len(sections) < 2 and dur > 8:
        # 在中间 20%~80% 找能量最低点当切点
        w = rms_n[int(len(rms_n) * 0.2):int(len(rms_n) * 0.8)]
        if len(w) > 2:
            cut_frame = int(len(rms_n) * 0.2) + int(np.argmin(w))
            cut_t = cut_frame * 0.5
            sections = _sections_from([0.0, float(cut_t)])
    # 短的低能量碎片（句间停顿）并进前一段，避免出现一堆 1 秒的“安静”
    merged = []
    for s in sections:
        if merged and (s["end"] - s["start"]) < 3.0 and s["energy"] < 0.12:
            merged[-1]["end"] = s["end"]
            continue
        merged.append(s)
    sections = merged

    # 段落标签：能量相对高低
    if sections:
        hi = max(s["energy"] for s in sections)
        for s in sections:
            ratio = s["energy"] / (hi or 1.0)
            s["label"] = "高能" if ratio > 0.75 else ("过渡" if ratio > 0.45 else "安静")

    # 人声/音乐粗判：光谱平坦度高 → 接近噪声/人声，低 → 调性音乐
    flat = float(np.mean(librosa.feature.spectral_flatness(y=y)))
    zcr = float(np.mean(librosa.feature.zero_crossing_rate(y)[0]))
    kind = "说话为主" if flat > 0.06 and zcr > 0.08 else ("唱/音乐为主" if flat < 0.03 else "混合")

    # 2026-11-07：补两样原来没有的 —— 调性、和弦。
    # 来源还是 dsh-soundtry 那套「六件套」（它是转写/节奏/旋律/和弦/结构），
    # 我们原来只做了节奏和结构，调和和弦一直是空的。
    # **诚实报**：根音可信度、品质可信度分开说，别硬给一个漂亮的调名。
    key_info: dict = {}
    try:
        chroma = librosa.feature.chroma_cqt(y=y, sr=SR)
        prof = chroma.mean(axis=1)
        srt = np.sort(prof)[::-1]
        if float(srt[0]) > 0:
            root_i = int(np.argmax(prof))
            margin = float((srt[0] - srt[1]) / max(1e-6, srt[0]))
            NOTE = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
            # 只留大/小三。属七模板（4 个音）比三和音容易命中，实测全报 C7/A7/D7，去掉。
            TPL = {"": (0, 4, 7), "m": (0, 3, 7)}
            names: dict[str, int] = {}
            root_hit = 0
            for i in range(0, chroma.shape[1], 2):
                v = chroma[:, i]
                best, bs = "", -9.9
                for r in range(12):
                    for suf, ivs in TPL.items():
                        idx = [(r + k) % 12 for k in ivs]
                        # 除以模板音数归一化，否则音多的模板天然占优
                        sc = float(v[idx].sum() / len(idx)
                                   - v[[j for j in range(12) if j not in idx]].sum() * 0.5 / (12 - len(idx)))
                        if sc > bs:
                            best, bs = NOTE[r] + suf, sc
                if best:
                    names[best] = names.get(best, 0) + 1
                    if best[0] in NOTE[root_i]:
                        root_hit += 1
            total = max(1, sum(names.values()))
            key_info = {
                "key": NOTE[root_i],
                # 最强音比第二强音高多少 —— 差距小就是「听不出主音」
                "key_confidence": ("可信" if margin > 0.12 else "较可信" if margin > 0.05 else "不可信"),
                "chords": [{"name": k, "pct": round(v / total * 100, 1)}
                           for k, v in sorted(names.items(), key=lambda x: -x[1])[:4]],
                # 大/小三容易混音骗，单独报可信度
                "quality_confidence": ("可信" if root_hit / total > 0.45
                                       else "较可信" if root_hit / total > 0.25 else "不可信（只听根音）"),
            }
    except Exception:  # noqa: BLE001
        key_info = {}

    return {
        "title": title,
        "duration": round(dur, 1),
        "bpm": round(float(tempo if np.isscalar(tempo) else tempo[0]), 1),
        "beats": int(len(beats)),
        "kind": kind,
        "spectral_flatness": round(flat, 3),
        "zero_crossing": round(zcr, 3),
        "loudness_mean": round(float(np.mean(rms)) * 100, 1),
        "sections": sections,
        **key_info,
    }


def summarize(r: dict) -> str:
    _k = f"  调性 {r.get('key')}（{r.get('key_confidence')}）" if r.get("key") else ""
    _c = ""
    if r.get("chords"):
        _c = "  和弦 " + "/".join(f"{x['name']} {x['pct']:.0f}%" for x in r["chords"][:3])
    lines = [f"《{r['title']}》 {r['duration']}s  BPM {r['bpm']}  节拍 {r['beats']} 下  类型：{r['kind']}{_k}{_c}"]
    for s in r["sections"]:
        lines.append(f"  {s['start']:>6.1f}-{s['end']:<6.1f}s  {s['label']:<3} 能量 {s['energy']}")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    r = analyze(a.src)
    print(json.dumps(r, ensure_ascii=False, indent=1) if a.json else summarize(r))


if __name__ == "__main__":
    main()
