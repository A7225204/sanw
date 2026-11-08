"""本地向量服务：bge-m3 常驻，给机器人提供 /embed 接口。

为什么要单独起服务
----------------
机器人进程（qq-ai-bot/.venv）里没有 torch，也不想为了 101 张表情包把
torch + bge-m3 塞进机器人进程（吃 2GB 显存、拖慢启动）。
所以用一个独立小服务：加载一次，HTTP 提供批量向量。

启动
----
    D:\\f5tts\\.venv\\Scripts\\pythonw.exe D:\\qq-ai-bot\\tools\\embed_server.py

接口
----
    POST /embed  {"texts": ["...", "..."]}  → {"vectors": [[...], ...], "dim": 1024}
    GET  /health → {"ok": true, "model": ..., "dim": ...}
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL_NAME = os.getenv("EMBED_MODEL_NAME", r"D:\models\bge-m3")
PORT = int(os.getenv("EMBED_PORT", "7801"))
HOST = os.getenv("EMBED_HOST", "127.0.0.1")

_lock = threading.Lock()
_model = None


def _get_model():
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                from sentence_transformers import SentenceTransformer
                print(f"[embed] 加载 {MODEL_NAME} …", flush=True)
                _model = SentenceTransformer(MODEL_NAME, device="cuda" if os.getenv("EMBED_GPU", "1") == "1" else "cpu")
                print(f"[embed] 就绪，维度 {_model.get_sentence_embedding_dimension()}", flush=True)
    return _model


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?")[0] == "/health":
            self._json(200, {"ok": _model is not None, "model": MODEL_NAME})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?")[0] != "/embed":
            self._json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            texts = [str(t)[:400] for t in (payload.get("texts") or []) if str(t).strip()]
        except Exception as e:  # noqa: BLE001
            self._json(400, {"error": f"bad request: {e}"})
            return
        if not texts:
            self._json(200, {"vectors": [], "dim": 0})
            return
        try:
            m = _get_model()
            vecs = m.encode(texts, normalize_embeddings=True, batch_size=16, show_progress_bar=False)
            self._json(200, {"vectors": [v.tolist() for v in vecs],
                             "dim": int(vecs.shape[1]) if len(vecs) else 0})
        except Exception as e:  # noqa: BLE001
            self._json(500, {"error": f"{type(e).__name__}: {e}"})

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        print(f"[embed] {fmt % args}", flush=True)


def main() -> int:
    _get_model()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[embed] 监听 http://{HOST}:{PORT}", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
