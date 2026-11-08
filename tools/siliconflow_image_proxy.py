# -*- coding: utf-8 -*-
"""硅基流动 → OpenAI 生图格式的小代理。

dsh-image-studio 的 openai-images 通道只认 {data:[{url|b64_json}]}，
硅基流动返回的是 {images:[{url}]}——这里转换一下，不用改插件源码。

    python tools/siliconflow_image_proxy.py --port 8850
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import base64
import httpx


def _parse_multipart(raw: bytes, ct: str) -> dict:
    """解析 multipart/form-data，返回 {name: [bytes]}。"""
    from email.parser import BytesParser
    from email.policy import default
    msg = BytesParser(policy=default).parsebytes(("Content-Type: " + ct + chr(13) + chr(10) + chr(13) + chr(10)).encode() + raw)


    out: dict = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        out.setdefault(name, []).append(part.get_payload(decode=True) or b"")
    return out


def _env() -> dict:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        from server import memory_growth as mg
        return mg._env()
    except Exception:  # noqa: BLE001
        return {}


ENV = _env()
KEY = str(ENV.get("SILICONFLOW_API_KEY") or "")
BASE = str(ENV.get("SILICONFLOW_BASE_URL") or "https://api.siliconflow.cn/v1").rstrip("/")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 安静
        pass

    def _json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") in ("/v1/models", "/models"):
            try:
                r = httpx.get(BASE + "/models", headers={"Authorization": f"Bearer {KEY}"}, timeout=20)
                return self._json(r.status_code, r.json())
            except Exception as e:  # noqa: BLE001
                return self._json(500, {"error": str(e)})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            if self.path.endswith("/images/generations"):
                body = json.loads(raw or b"{}")
                # 硅基流动用 image_size，OpenAI 用 size
                if "size" in body and "image_size" not in body:
                    body["image_size"] = body.pop("size")
                body.pop("response_format", None)
                body.setdefault("batch_size", body.pop("n", 1) or 1)
                r = httpx.post(BASE + "/images/generations",
                               headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
                               json=body, timeout=240)
                if r.status_code != 200:
                    return self._json(r.status_code, {"error": r.text[:400]})
                d = r.json()
                imgs = d.get("images") or d.get("data") or []
                return self._json(200, {"data": [{"url": x.get("url")} for x in imgs if x.get("url")],
                                        "created": d.get("created")})
            if self.path.endswith("/images/edits"):
                # 硅基流动没有 /images/edits：把 multipart 转成 generations + image(base64)，
                # 模型换成支持参考图的 Qwen-Image-Edit
                ct = self.headers.get("Content-Type") or ""
                parts = _parse_multipart(raw, ct)
                prompt = (parts.get("prompt") or [b""])[0].decode("utf-8", "ignore")
                imgs = parts.get("image") or parts.get("image[]") or []
                if not imgs:
                    return self._json(400, {"error": "没有参考图"})
                b = imgs[0]
                ext = "png" if b[:4] == bytes([137]) + b"PNG" else "jpeg"
                body = {
                    "model": "Qwen/Qwen-Image-Edit",
                    "prompt": prompt,
                    "image": f"data:image/{ext};base64," + base64.b64encode(b).decode(),
                    "image_size": "1024x1024",
                }
                r = httpx.post(BASE + "/images/generations",
                               headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
                               json=body, timeout=300)
                if r.status_code != 200:
                    return self._json(r.status_code, {"error": r.text[:400]})
                d = r.json()
                got = d.get("images") or d.get("data") or []
                return self._json(200, {"data": [{"url": x.get("url")} for x in got if x.get("url")]})
        except Exception as e:  # noqa: BLE001
            return self._json(500, {"error": f"{type(e).__name__}: {e}"})
        self._json(404, {"error": "not found"})


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8850)
    a = ap.parse_args()
    print(f"[proxy] siliconflow → {BASE} | key={'有' if KEY else '无'} | port={a.port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()
