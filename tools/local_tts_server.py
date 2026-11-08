"""本地 TTS 服务（F5-TTS 克隆音色），给机器人用。

跑在 D:\\f5tts\\.venv 里（那边装了 f5_tts + torch），模型常驻显存，避免每次加载。
    D:\\f5tts\\.venv\\Scripts\\python.exe D:\\qq-ai-bot\\tools\\local_tts_server.py

HTTP:
    POST /tts   {"text": "...", "out": "D:/.../x.mp3"}  -> {"ok": true, "path": "..."}
    GET  /health -> {"ok": true, "model": "F5-TTS", "ref": "..."}
"""
from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(r"D:\GPT-SoVITS\dataset\gpt_sovits_ready")
OUT_DIR = Path(r"D:\qq-ai-bot\data\tts_local")
PORT = int(os.getenv("LOCAL_TTS_PORT", "9881"))
HOST = "127.0.0.1"

# 引擎：f5（默认，老链路）| indextts2（IndexTTS2 Studio，7860）
ENGINE = os.getenv("TTS_ENGINE", "indextts2").strip().lower()
INDEXTTS_URL = os.getenv("INDEXTTS_URL", "http://127.0.0.1:7860").rstrip("/")
INDEXTTS_DIR = Path(os.getenv("INDEXTTS_DIR", r"D:\index-tts"))
INDEXTTS_CLIENT = INDEXTTS_DIR / "skills" / "indextts2-tts" / "scripts" / "tts_client.py"
# 2026-11-05：音色换成满穗（穗-585）。选它是因为频谱指纹里 hf_ratio 最低（0.170），
# 声音最"收"，情绪向量有最大发挥空间。旧参考 dwg_0002 已退役。
INDEXTTS_VOICE = os.getenv("INDEXTTS_VOICE",
                           str(Path(__file__).resolve().parent.parent / "data" / "voice_ref" / "voice_ref_main.wav"))
# 情绪权重 0~1：原来是硬编码 0.6，情绪只发挥六成，写死不便于调。
# 2026-11-05 提到 0.85 —— 情欲的区分度全押在「惊喜 + 平静」两轴上，权重低就听不出来。
EMO_WEIGHT = os.getenv("INDEXTTS_EMO_WEIGHT", "0.85").strip()
EMOTION_FILE = Path(r"D:\qq-ai-bot\memory\emotion_state.json")
# 8 轴顺序（IndexTTS2）：喜悦/愤怒/悲伤/恐惧/厌恶/低落/惊喜/平静
EMO_KEYS = ("开心", "怒", "丧", "害怕", "烦", "累", None, None)


def emotion_vector() -> list[float]:
    """把她的情绪值映射成 IndexTTS2 的 8 轴向量（0~1，读不到就全 0 = 同音色）。"""
    try:
        vals = (json.loads(EMOTION_FILE.read_text(encoding="utf-8")) or {}).get("values") or {}
    except Exception:  # noqa: BLE001
        return [0.0] * 8
    vec = []
    for k in EMO_KEYS:
        if k is None:
            vec.append(0.0)
        else:
            v = float(vals.get(k) or 0.0)
            if k == "烦":   # 烦 → 厌恶 / 愤怒各分一半
                v = v * 0.5
            vec.append(max(0.0, min(1.0, v / 100.0)))
    vec[7] = 0.15   # 平静底噪
    return [round(x, 3) for x in vec]

_LOCK = threading.Lock()
_F5 = None
_REF: tuple[Path, str] | None = None


def _ffmpeg() -> str:
    import shutil
    cands = ["D:/FormatFactory/ffmpeg.exe"]
    try:
        import imageio_ffmpeg
        cands.append(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:  # noqa: BLE001
        pass
    for c in cands:
        if c and os.path.exists(c):
            return c
    w = shutil.which("ffmpeg")
    return w or "ffmpeg"


def pick_reference() -> tuple[Path, str]:
    rows = []
    meta = ROOT / "metadata.csv"
    with meta.open(encoding="utf-8", errors="ignore") as f:
        for r in csv.DictReader(f):
            wav = ROOT / r["file_name"]
            text = (r.get("text") or "").strip()
            if wav.exists() and 6 <= len(text) <= 40:
                rows.append((wav, text))
    if not rows:
        raise SystemExit("没有合适的参考音频")
    # 优先挑靠前的（通常音质最好）
    return rows[0]


_LOAD_ERR = ""


def load():
    global _F5, _REF, _LOAD_ERR
    if _F5 is not None:
        return
    try:
        from f5_tts.api import F5TTS
        t0 = time.time()
        _F5 = F5TTS()
        _REF = pick_reference()
        print(f"[tts] 模型加载完成 {time.time()-t0:.0f}s，参考={_REF[0].name}", flush=True)
    except Exception as e:  # noqa: BLE001
        _LOAD_ERR = f"{type(e).__name__}: {e}"[:300]
        print(f"[tts] 模型加载失败: {_LOAD_ERR}", flush=True)
        raise


def synth(text: str, out_path: Path) -> Path:
    load()
    assert _F5 is not None and _REF is not None
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_wav = out_path.with_suffix(".wav")
    t0 = time.time()
    with _LOCK:
        _F5.infer(ref_file=str(_REF[0]), ref_text=_REF[1], gen_text=text, file_wave=str(tmp_wav))
    dt = time.time() - t0
    ff = _ffmpeg()
    try:
        subprocess.run([ff, "-y", "-i", str(tmp_wav), "-b:a", "128k", str(out_path)],
                       capture_output=True, timeout=180)
        if out_path.exists() and out_path.stat().st_size > 1000:
            tmp_wav.unlink(missing_ok=True)
        else:
            out_path = tmp_wav
    except Exception:  # noqa: BLE001
        out_path = tmp_wav
    print(f"[tts] 合成 {len(text)} 字 {dt:.1f}s → {out_path}", flush=True)
    return out_path


VOICE_ROOT = (Path(__file__).resolve().parent.parent / "data" / "voice_ref").resolve()


def _safe_voice(p) -> Path | None:
    """把调用方给的参考音频限制在 data/voice_ref/ 里，禁止任意路径。

    2026-11-05：情欲场景要换带气息的 ref（见 memory/tts_emotion_recipes.json 核心规律第 5 条
    「气息是生理反应不是情绪，向量做不出来，只能靠参考音频本身带」）。
    """
    if not p:
        return None
    try:
        q = Path(str(p))
        if not q.is_absolute():
            q = VOICE_ROOT / q
        q = q.resolve()
        if q != VOICE_ROOT and VOICE_ROOT not in q.parents:
            return None
        if q.suffix.lower() not in (".wav", ".mp3", ".flac"):
            return None
        return q if q.exists() and q.stat().st_size > 20000 else None
    except Exception:  # noqa: BLE001
        return None


def synth_indextts2(text: str, out_path: Path, vec: list | None = None,
                    voice: str | Path | None = None) -> Path:
    """走 IndexTTS2 Studio：子进程调自带 skill 客户端。

    2026-11-05：情绪向量优先用调用方传进来的（ai_group 才有实时的 hormones 状态）。
    没传时才回退本地 emotion_vector()——注意那份读的是 emotion_state.json，
    那个文件在情绪改由激素派生后就不更新了，只在老调用路径上兜底。
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wav = out_path.with_suffix(".wav")
    if vec and len(vec) == 8:
        vec_s = ",".join(f"{float(x):.2f}" for x in vec)
    else:
        vec_s = ",".join(f"{x:.2f}" for x in emotion_vector())
    # 2026-11-05：参考音频可由调用方指定（情欲高时换成带气息的样本），默认仍是主音色
    _vp = _safe_voice(voice) or Path(INDEXTTS_VOICE)
    cmd = [sys.executable, str(INDEXTTS_CLIENT), "synth",
           "--text", text, "--voice", str(_vp),
           "--emo-mode", "2", "--emo-vector", vec_s,
           "--emo-weight", EMO_WEIGHT,
           "--out", str(wav), "--no-start"]
    t0 = time.time()
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=420,
                       encoding="utf-8", errors="replace", env=env)
    if not wav.exists() or wav.stat().st_size < 1000:
        raise RuntimeError((r.stdout or "")[-300:] + " | " + (r.stderr or "")[-300:])
    dt = time.time() - t0
    ff = _ffmpeg()
    try:
        subprocess.run([ff, "-y", "-i", str(wav), "-b:a", "128k", str(out_path)],
                       capture_output=True, timeout=120)
        if out_path.exists() and out_path.stat().st_size > 1000:
            wav.unlink(missing_ok=True)
        else:
            out_path = wav
    except Exception:  # noqa: BLE001
        out_path = wav
    print(f"[tts] IndexTTS2 合成 {len(text)} 字 {dt:.1f}s emo={vec_s} ref={_vp.name} → {out_path}", flush=True)
    return out_path


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # noqa: D401
        pass

    def _send(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/health"):
            if ENGINE == "indextts2":
                self._send(200, {"ok": True, "model": "IndexTTS2", "engine": ENGINE,
                                 "ref": Path(INDEXTTS_VOICE).name,
                                 "loading": False, "error": ""})
                return
            self._send(200, {"ok": _F5 is not None, "model": "F5-TTS", "engine": ENGINE,
                             "ref": _REF[0].name if _REF else "",
                             "loading": _F5 is None, "error": _LOAD_ERR})
        else:
            self._send(404, {"ok": False})

    def do_POST(self):
        if not self.path.startswith("/tts"):
            self._send(404, {"ok": False})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(n) or b"{}")
            text = (data.get("text") or "").strip()
            if not text:
                self._send(400, {"ok": False, "error": "empty text"})
                return
            out = Path(data.get("out") or (OUT_DIR / f"tts_{int(time.time())}.mp3"))
            # 2026-11-05：调用方可以带 8 轴情绪向量过来（主进程有实时激素状态）
            _ev = data.get("emo_vector")
            if isinstance(_ev, list) and len(_ev) == 8:
                try:
                    _ev = [max(0.0, min(1.0, float(x))) for x in _ev]
                except (TypeError, ValueError):
                    _ev = None
            else:
                _ev = None
            p = (synth_indextts2(text[:400], out, _ev, data.get("voice")) if ENGINE == "indextts2"
                 else synth(text[:400], out))
            _vused = _safe_voice(data.get("voice")) or Path(INDEXTTS_VOICE)
            self._send(200, {"ok": True, "path": str(p), "engine": ENGINE,
                             "emo_vector": _ev,
                             "ref": _vused.name if ENGINE == "indextts2" else ""})
        except Exception as e:  # noqa: BLE001
            # 2026-11-07：原来只回 str(e)，查「情绪语音失效」那晚拿不到位置。
            # 现在把 traceback 末尾也带上（只 800 字），服务端同时打全量。
            import traceback as _tb
            _full = _tb.format_exc()
            print("[tts] 合成失败：" + _full, flush=True)
            self._send(500, {"ok": False, "error": f"{type(e).__name__}: {e}"[:300],
                             "tb": _full[-800:]})


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    if ENGINE == "indextts2":
        print(f"[tts] 服务已启动 http://{HOST}:{PORT}（引擎=IndexTTS2 @ {INDEXTTS_URL}）", flush=True)
    else:
        print(f"[tts] 服务已启动 http://{HOST}:{PORT}（模型后台加载中）", flush=True)
        threading.Thread(target=_bg_load, daemon=True).start()
    srv.serve_forever()


def _bg_load():
    try:
        load()
    except Exception as e:  # noqa: BLE001
        print(f"[tts] 后台加载失败: {type(e).__name__}: {e}", flush=True)


if __name__ == "__main__":
    main()
