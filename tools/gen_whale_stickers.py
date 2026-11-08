#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给「蓝色大肥鱼」批量生成鲸鱼主题表情包：7 种情绪各 10 张，视觉模型验收后入库。

用法：
    python tools/gen_whale_stickers.py             # 生成并安装
    python tools/gen_whale_stickers.py --dry-run   # 只生成不复制
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.collect_whale_stickers import STAGE, tag  # noqa: E402

EMOJI_ROOT = Path(r"D:\qq-ai-bot\表情包")
OUT = STAGE / "generated"
OUT.mkdir(parents=True, exist_ok=True)
MANIFEST = OUT / "manifest.json"

ENV = ROOT / ".env"


def env(key: str) -> str:
    for line in ENV.read_text(encoding="utf-8").splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    return ""


KEY = env("DOUBAO_DRAW__API_KEY") or env("AI_API_KEY")
MODEL = env("DOUBAO_DRAW__MODEL")
API = "https://ark.cn-beijing.volces.com/api/v3/images/generations"

BASE = ("一只圆滚滚的蓝色小鲸鱼（外号蓝色大肥鱼）卡通形象，{action}，白色纯色背景，"
        "Q版贴纸风格，线条干净，圆润可爱，适合 QQ 聊天表情包，画面里不要任何文字，不要水印")

ACTIONS = {
    "开心": ["开心大笑", "眼睛弯成月牙地笑", "蹦跳欢呼", "抱着白米饭开心", "拍手大笑",
             "吐着舌头开心", "开心到转圈", "比耶大笑", "笑到流眼泪", "尾巴翘起来欢呼"],
    "无语": ["翻白眼", "流着汗一脸无语", "抬头望天无语", "摊手无语", "嘴角抽搐",
             "默默叹气", "石化僵住", "斜眼无语", "低头扶额", "一脸麻木"],
    "生气": ["鼓着腮帮子生气", "头顶冒火", "叉腰瞪眼", "拍桌子生气", "炸毛",
             "握拳皱眉", "气到脸红", "跺脚", "尾巴拍地", "瞪着眼睛怒视"],
    "卖萌": ["歪头卖萌", "双手比心", "眨眼 wink", "双手托腮", "抱着自己的尾巴",
             "撒娇蹭蹭", "吐小舌头", "举着一颗小心心", "脸红卖萌", "撒娇打滚"],
    "得意": ["戴着墨镜得意", "叉腰仰头", "挑眉得意", "仰着鼻孔看人", "翘着腿",
             "甩尾巴得意", "戴着王冠", "比大拇指", "叼着玫瑰得意", "仰天大笑"],
    "委屈": ["眼泪汪汪", "瘪着嘴要哭", "抱着膝盖蹲角落", "小声抽泣", "低头委屈",
             "含着眼泪看人", "缩成一团", "擦眼泪", "委屈巴巴张嘴", "躲到石头后面"],
    "震惊": ["张大嘴震惊", "瞪圆眼睛", "头顶冒出感叹号", "吓得后退一步", "惊掉下巴",
             "双手抱头", "瞳孔地震", "僵在原地", "吓得跳起来", "震惊到变色"],
}


def generate(prompt: str) -> Path:
    body = {"model": MODEL, "prompt": prompt, "size": "1024x1024",
            "response_format": "url", "watermark": False}
    r = httpx.post(API, headers={"Authorization": f"Bearer {KEY}"}, json=body, timeout=180)
    r.raise_for_status()
    url = r.json()["data"][0]["url"]
    data = httpx.get(url, timeout=120).content
    path = OUT / f"g_{int(time.time() * 1000)}_{abs(hash(prompt)) % 10000}.jpg"
    path.write_bytes(data)
    return path


def make_one(mood: str, action: str) -> dict | None:
    for attempt in range(3):
        prompt = BASE.format(action=action)
        if attempt == 1:
            prompt += f"，表情要明显是「{mood}」"
        if attempt == 2:
            prompt = f"表情包：蓝色小鲸鱼{mood}地{action}，白色背景，卡通贴纸，无文字无水印"
        try:
            path = generate(prompt)
            info = tag(path)
            if (info.get("is_sticker") and not info.get("has_watermark") and not info.get("is_collage")
                    and info.get("mood") == mood
                    and info.get("subject") in ("鲸鱼", "鱼", "卡通角色")):
                info["file"] = str(path)
                info["prompt"] = prompt
                return info
        except Exception:
            pass
    return None


def install(mood: str, items: list[dict]) -> None:
    target = EMOJI_ROOT / mood
    target.mkdir(parents=True, exist_ok=True)
    max_idx = 0
    for p in target.iterdir():
        m = re.match(r"^whale_(\d+)$", p.stem)
        if m:
            max_idx = max(max_idx, int(m.group(1)))
    for i, item in enumerate(items, 1):
        src = Path(item["file"])
        dest = target / f"whale_{max_idx + i:02d}.jpg"
        dest.write_bytes(src.read_bytes())
        item["installed"] = str(dest)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--per-mood", type=int, default=10)
    args = ap.parse_args()

    manifest = []
    for mood, actions in ACTIONS.items():
        print(f"=== {mood} ===", flush=True)
        need = args.per_mood
        accepted: list[dict] = []
        with ThreadPoolExecutor(max_workers=3) as pool:
            futs = {pool.submit(make_one, mood, a): a for a in actions}
            for fut in as_completed(futs):
                res = fut.result()
                if res:
                    accepted.append(res)
        accepted = accepted[:need]
        print(f"  accepted {len(accepted)}/{need}", flush=True)
        if len(accepted) < need:
            # 用同一批动作再补几轮
            for extra in range(2):
                if len(accepted) >= need:
                    break
                with ThreadPoolExecutor(max_workers=3) as pool:
                    futs = {pool.submit(make_one, mood, a): a for a in actions}
                    for fut in as_completed(futs):
                        if len(accepted) >= need:
                            break
                        res = fut.result()
                        if res:
                            accepted.append(res)
        accepted = accepted[:need]
        if not args.dry_run and accepted:
            install(mood, accepted)
        for item in accepted:
            item["mood"] = mood
        manifest.extend(accepted)

    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"done: {len(manifest)} -> {MANIFEST}")


if __name__ == "__main__":
    main()
