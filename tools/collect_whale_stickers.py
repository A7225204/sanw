#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给「蓝色大肥鱼」收集鲸鱼/鱼主题表情包，按 7 种情绪各补 10 张。

流程：Bing 图片搜索 -> 下载 -> 视觉模型打标（是否表情包/主体/情绪/水印/拼图）
      -> 去重 -> 每个情绪挑 10 张 -> 复制进 D:\\qq-ai-bot\\表情包\\<情绪>\\

用法：
    python tools/collect_whale_stickers.py            # 收集并安装
    python tools/collect_whale_stickers.py --dry-run  # 只打分不复制
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import httpx
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
EMOJI_ROOT = Path(r"D:\qq-ai-bot\表情包")
STAGE = ROOT / "_whale_stage"
MANIFEST = STAGE / "selected.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
BAD_HOSTS = ("bing.com",)

MOODS = {
    "开心": ["鲸鱼 开心 表情包", "蓝鲸 开心 表情包", "鲸鱼 表情包 大笑", "鱼 开心 表情包", "鲸鱼 表情包"],
    "无语": ["鲸鱼 无语 表情包", "鲸鱼 表情包 流汗", "鱼 无语 表情包", "鲸鱼 翻白眼 表情包", "鲸鱼 表情包"],
    "生气": ["鲸鱼 生气 表情包", "鲸鱼 表情包 愤怒", "鱼 生气 表情包", "鲨鱼 生气 表情包", "鲸鱼 表情包"],
    "卖萌": ["鲸鱼 可爱 表情包", "蓝鲸 可爱 表情包", "鲸鱼 卖萌 表情包", "鱼 可爱 表情包", "鲸鱼 表情包"],
    "得意": ["鲸鱼 得意 表情包", "鲸鱼 表情包 骄傲", "鱼 得意 表情包", "鲸鱼 墨镜 表情包", "鲸鱼 表情包"],
    "委屈": ["鲸鱼 委屈 表情包", "鲸鱼 表情包 哭", "鱼 委屈 表情包", "鲸鱼 可怜 表情包", "鲸鱼 表情包"],
    "震惊": ["鲸鱼 震惊 表情包", "鲸鱼 表情包 惊讶", "鱼 震惊 表情包", "鲸鱼 目瞪口呆 表情包", "鲸鱼 表情包"],
}


def env(key: str) -> str:
    for line in ENV.read_text(encoding="utf-8").splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    return ""


KEY, BASE, MODEL = env("AI_API_KEY"), env("AI_BASE_URL").rstrip("/"), env("AI_MODEL")


def md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def existing_hashes() -> set[str]:
    out = set()
    for p in EMOJI_ROOT.rglob("*"):
        if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png", ".gif"):
            try:
                out.add(md5(p))
            except OSError:
                pass
    return out


def bing_urls(query: str) -> list[str]:
    urls: list[str] = []
    for first in (0, 35, 70):
        qs = httpx.QueryParams({"q": query, "first": first, "count": 35, "mmasync": 1})
        u = "https://www.bing.com/images/async?" + str(qs)
        try:
            r = httpx.get(u, headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"},
                          timeout=25, follow_redirects=True)
        except Exception:
            continue
        for m in re.finditer(r"murl&quot;:&quot;(.*?)&quot;", r.text):
            raw = m.group(1).replace("&amp;", "&")
            host = urlparse(raw).hostname or ""
            if any(b in host for b in BAD_HOSTS):
                continue
            urls.append(raw)
        time.sleep(0.2)
    return list(dict.fromkeys(urls))


def download(url: str, dest: Path) -> Path | None:
    referer = "https://www.bilibili.com/" if "hdslb" in url else "https://www.bing.com/"
    try:
        r = httpx.get(url, headers={"User-Agent": UA, "Referer": referer}, timeout=25, follow_redirects=True)
        if r.status_code != 200 or not (5000 < len(r.content) < 4 * 1024 * 1024):
            return None
        img = Image.open(io.BytesIO(r.content))
        if getattr(img, "n_frames", 1) > 1:
            img.seek(0)
        w, h = img.size
        if w < 140 or h < 140 or w > 1600 or h > 1600:
            return None
        fmt = (img.format or "").upper()
        ext = {"JPEG": ".jpg", "PNG": ".png", "GIF": ".gif"}.get(fmt)
        if ext is None:
            return None
        tmp = dest.with_suffix(ext)
        if fmt in ("GIF", "PNG"):
            # 保留原文件：GIF 动图、PNG 透明底
            tmp.write_bytes(r.content)
        else:
            img.convert("RGB").save(tmp, quality=92)
        return tmp
    except Exception:
        return None


PROMPT = """你是表情包素材审核员。看这张图，只输出一个 JSON（不要 markdown、不要解释），字段：
{"is_sticker": true/false, "subject": "鲸鱼/鱼/猫/狗/人物/卡通角色/文字/其他",
 "mood": "开心/无语/生气/卖萌/得意/委屈/震惊/其他",
 "has_watermark": true/false, "is_collage": true/false, "caption": "20 字以内中文描述"}"""


def data_url(path: Path) -> str:
    img = Image.open(path)
    if getattr(img, "n_frames", 1) > 1:
        img.seek(0)
    img = img.convert("RGB")
    img.thumbnail((640, 640))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def tag(path: Path) -> dict:
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": PROMPT},
            {"type": "image_url", "image_url": {"url": data_url(path)}},
        ]}],
        "max_tokens": 900,
    }
    r = httpx.post(BASE + "/chat/completions",
                   headers={"Authorization": f"Bearer {KEY}"}, json=body, timeout=120)
    msg = r.json()["choices"][0]["message"]
    text = (msg.get("content") or "").strip()
    if not text:
        raise RuntimeError("empty")
    start, end = text.find("{"), text.rfind("}")
    data = json.loads(text[start:end + 1])
    data["file"] = str(path)
    return data


def pick_for_mood(mood: str, files: list[Path], hashes: set[str], dry: bool) -> list[dict]:
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(tag, f): f for f in files}
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception:
                pass
    good = []
    for r in results:
        if not r.get("is_sticker") or r.get("has_watermark") or r.get("is_collage"):
            continue
        if r.get("mood") != mood:
            continue
        subject = r.get("subject") or ""
        score = 0
        if subject in ("鲸鱼", "鱼"):
            score += 3
        elif subject == "卡通角色":
            score += 1
        cap = r.get("caption") or ""
        if any(k in cap for k in ("鲸", "鱼", "蓝")):
            score += 2
        if any(k in cap for k in ("水印", "logo", "网址", "公众号")):
            continue
        r["score"] = score
        good.append(r)
    good.sort(key=lambda x: -x["score"])
    picked = []
    for r in good:
        p = Path(r["file"])
        try:
            h = md5(p)
        except OSError:
            continue
        if h in hashes:
            continue
        hashes.add(h)
        picked.append(r)
        if len(picked) >= 10:
            break
    return picked


def install(mood: str, picked: list[dict]) -> list[dict]:
    target = EMOJI_ROOT / mood
    target.mkdir(parents=True, exist_ok=True)
    max_idx = 0
    for p in target.iterdir():
        m = re.match(r"^whale_(\d+)$", p.stem)
        if m:
            max_idx = max(max_idx, int(m.group(1)))
    done = []
    for i, r in enumerate(picked, 1):
        src = Path(r["file"])
        idx = max_idx + i
        dest = target / f"whale_{idx:02d}{src.suffix.lower()}"
        dest.write_bytes(src.read_bytes())
        r["installed"] = str(dest)
        done.append(r)
    return done


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--target", type=int, default=10)
    args = ap.parse_args()

    STAGE.mkdir(exist_ok=True)
    hashes = existing_hashes()
    manifest = []

    for mood, queries in MOODS.items():
        print(f"=== {mood} ===", flush=True)
        urls = []
        for q in queries:
            urls.extend(bing_urls(q))
        urls = list(dict.fromkeys(urls))
        print(f"  candidates: {len(urls)}", flush=True)
        mood_dir = STAGE / mood
        mood_dir.mkdir(exist_ok=True)
        files = []
        with ThreadPoolExecutor(max_workers=6) as pool:
            futs = {}
            for i, u in enumerate(urls[:50], 1):
                futs[pool.submit(download, u, mood_dir / f"c{i:03d}")] = u
            for fut in as_completed(futs):
                p = fut.result()
                if p:
                    files.append(p)
        print(f"  downloaded: {len(files)}", flush=True)
        picked = pick_for_mood(mood, files, hashes, args.dry_run)
        print(f"  picked: {len(picked)}", flush=True)
        if not args.dry_run and picked:
            picked = install(mood, picked)
        for r in picked:
            r["mood"] = mood
        manifest.extend(picked)

    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"done: {len(manifest)} -> {MANIFEST}")


if __name__ == "__main__":
    main()

