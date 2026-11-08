"""语音样本「频谱指纹」索引（2026-11-05）。

为什么用它：699 个满穗样本，靠试听挑一个要听半小时，还记不住哪个是哪个。
这里把每个样本的物理特征量出来存成索引，之后按「目标情绪」查表找最像的，不用再盲听。

三个指标（从频谱图的形态直接来）：
    hf_ratio   高频能量占比（>4kHz）。声音「打开」程度——喜悦/激动高，低落/虚弱低
    pause_ratio 静音帧占比。语流连贯度——平静/正常低，迟缓/断续高
    cv         能量变异系数（RMS 标准差/均值）。动态起伏——演戏/激动高，平铺低
    f0_var     基频方差（音高抖动）。情绪起伏的旁证

用法：
    python tools/voice_fingerprint.py index              # 扫描 data/voice_ref/manzui 建索引
    python tools/voice_fingerprint.py index --dir <路径>
    python tools/voice_fingerprint.py find --target calm      # 找最平静的
    python tools/voice_fingerprint.py find --target excited   # 找最激动的
    python tools/voice_fingerprint.py find --target sad
    python tools/voice_fingerprint.py stats              # 看索引统计
"""
from __future__ import annotations

import json
import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "memory" / "voice_fingerprints.json"
DEFAULT_DIR = ROOT / "data" / "voice_ref" / "manzui"

# 各指标在「目标情绪」里的权重（正数=越大越像，负数=越小越像）
TARGETS = {
    "calm":    {"hf_ratio": -1.0, "pause_ratio": 0.3, "cv": -1.2, "f0_var": -1.0},
    "excited": {"hf_ratio": 1.2, "pause_ratio": -0.5, "cv": 1.0, "f0_var": 1.0},
    "sad":     {"hf_ratio": -1.2, "pause_ratio": 0.8, "cv": -0.6, "f0_var": -0.8},
    "angry":   {"hf_ratio": 0.8, "pause_ratio": -0.6, "cv": 1.2, "f0_var": 1.1},
}


def fingerprint(path: Path) -> dict | None:
    """量一个 wav 的物理特征。读不了就返回 None。"""
    try:
        w = wave.open(str(path))
        sr, ch = w.getframerate(), w.getnchannels()
        raw = w.readframes(w.getnframes())
        w.close()
    except Exception:
        return None
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if len(x) < sr // 2:          # 太短（<0.5s）不要
        return None
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    dur = len(x) / sr

    hop = max(256, sr // 100)      # ~10ms 一帧
    n = len(x) // hop
    if n < 10:
        return None
    frames = x[: n * hop].reshape(n, hop)

    # ① 能量与停顿
    rms = np.sqrt((frames ** 2).mean(axis=1) + 1e-12)
    cv = float(rms.std() / (rms.mean() + 1e-9))
    thr = max(rms.mean() * 0.25, 1e-4)
    pause_ratio = float((rms < thr).mean())

    # ② 高频能量占比（4kHz 以上）
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    freqs = np.fft.rfftfreq(len(x), 1.0 / sr)
    total = float(spec.sum()) + 1e-9
    hf_ratio = float(spec[freqs >= 4000].sum() / total)

    # ③ 基频变化（自相关粗估）
    # 2026-11-05 修：原来用 hop（480 样本）当窗，但 70Hz 对应的自相关延迟要 685，
    # 全部落在窗长之外被跳过，f0_var 恒为 0。改用 100ms 长窗。
    win = min(len(x), sr // 10)
    step = max(1, win // 2)
    lo, hi = sr // 500, sr // 70          # 70~500Hz
    f0s: list[float] = []
    for start in range(0, len(x) - win, step):
        seg = x[start:start + win]
        if float(np.sqrt((seg ** 2).mean())) < thr:
            continue
        seg = seg - seg.mean()
        ac = np.correlate(seg, seg, mode="full")[len(seg) - 1:]
        if hi >= len(ac):
            continue
        peak = int(np.argmax(ac[lo:hi])) + lo
        if peak > 0 and ac[peak] > 0:
            f0s.append(sr / peak)
    f0_var = float(np.std(f0s) / np.mean(f0s)) if len(f0s) >= 3 else 0.0

    return {
        "file": path.name,
        "dur": round(dur, 2),
        "sr": sr,
        "hf_ratio": round(hf_ratio, 4),
        "pause_ratio": round(pause_ratio, 3),
        "cv": round(cv, 3),
        "f0_var": round(f0_var, 4),
    }


def build_index(src: Path) -> int:
    files = sorted(src.glob("*.wav")) + sorted(src.glob("*.mp3"))
    rows = []
    for i, f in enumerate(files, 1):
        fp = fingerprint(f)
        if fp:
            rows.append(fp)
        if i % 50 == 0:
            print(f"  {i}/{len(files)}…", flush=True)
    INDEX.parent.mkdir(parents=True, exist_ok=True)
    INDEX.write_text(json.dumps({"source": str(src), "count": len(rows), "items": rows},
                                ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"✓ 索引 {len(rows)}/{len(files)} 条 → {INDEX}")
    return 0


def load_index() -> list[dict]:
    try:
        return json.loads(INDEX.read_text(encoding="utf-8")).get("items") or []
    except Exception:
        return []


def find(target: str, top: int = 8, min_dur: float = 0.0) -> int:
    items = load_index()
    if not items:
        print("索引是空的，先跑 index")
        return 2
    # 2026-11-05：短样本（<1s）当音色模板会抖，先按最短时长筛掉
    if min_dur > 0:
        items = [x for x in items if float(x.get("dur") or 0) >= min_dur]
        if not items:
            print(f"没有时长 >= {min_dur}s 的样本")
            return 2
    weights = TARGETS.get(target)
    if not weights:
        print(f"未知目标 {target}，可选：{', '.join(TARGETS)}")
        return 2
    # 各指标先归一化到 0~1，再按权重打分
    keys = list(weights)
    norm: dict[str, list[float]] = {}
    for k in keys:
        vals = [float(x.get(k) or 0) for x in items]
        lo, hi = min(vals), max(vals)
        span = (hi - lo) or 1.0
        norm[k] = [(v - lo) / span for v in vals]
    scored = []
    for i, it in enumerate(items):
        s = sum(weights[k] * norm[k][i] for k in keys)
        scored.append((s, it))
    scored.sort(key=lambda x: -x[0])
    # 转成 0~100 的可读分：最好的 100，最差的 0
    lo_s, hi_s = scored[-1][0], scored[0][0]
    span = (hi_s - lo_s) or 1.0
    scored = [((s2 - lo_s) / span * 100.0, it) for s2, it in scored]
    _dtag = f"，时长 ≥{min_dur:g}s" if min_dur > 0 else ""
    print(f"=== 目标「{target}」最匹配 {top} 条（100=最像{_dtag}）===")
    print(f"{'分':>6} {'时长':>5} {'高频':>7} {'停顿':>6} {'起伏':>6} {'音高':>7}  文件")
    for s, it in scored[:top]:
        print(f"{s:6.2f} {it['dur']:5.1f} {it['hf_ratio']:7.4f} "
              f"{it['pause_ratio']:6.3f} {it['cv']:6.3f} {it['f0_var']:7.4f}  {it['file']}")
    return 0


def stats() -> int:
    items = load_index()
    if not items:
        print("索引是空的")
        return 0
    print(f"索引 {len(items)} 条")
    for k in ("dur", "hf_ratio", "pause_ratio", "cv", "f0_var"):
        vals = sorted(float(x.get(k) or 0) for x in items)
        n = len(vals)
        print(f"  {k:12s} 最小 {vals[0]:.4f} / 中位 {vals[n//2]:.4f} / 最大 {vals[-1]:.4f}")
    return 0


def main() -> int:
    argv = sys.argv[1:]
    cmd = argv[0] if argv else "stats"
    if cmd == "index":
        src = DEFAULT_DIR
        if "--dir" in argv:
            src = Path(argv[argv.index("--dir") + 1])
        if not src.is_dir():
            print(f"目录不存在：{src}")
            return 2
        return build_index(src)
    if cmd == "find":
        target = argv[argv.index("--target") + 1] if "--target" in argv else "calm"
        md = float(argv[argv.index("--min-dur") + 1]) if "--min-dur" in argv else 0.0
        return find(target, min_dur=md)
    if cmd == "stats":
        return stats()
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
