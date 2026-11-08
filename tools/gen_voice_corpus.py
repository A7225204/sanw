"""批量合成大肥鱼语音素材（同一音色），用于 so-vits 训练。

为什么这么做：她的音色 = 豆包 TTS 的 zh_female_mengyatou，
批量合成等价于"她说 20 分钟话"，而且音色 100% 一致、几分钟就能生成完。

输出：D:\\so-vits-svc\\dataset_raw\\feiyu\\tts_XXXX.wav（44.1k 单声道，3~10 秒一句）
"""
from __future__ import annotations

import asyncio
import io
import os
import sys
import uuid
from pathlib import Path

import httpx

ROOT = Path(r"D:\qq-ai-bot")
OUT = Path(r"D:\so-vits-svc\dataset_raw\feiyu")

# 环境
for line in (ROOT / ".env").read_text(encoding="utf-8", errors="ignore").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

TTS_API_KEY = os.getenv("TTS_API_KEY", "")
TTS_SPEAKER = os.getenv("TTS_SPEAKER", "zh_female_mengyatou_uranus_bigtts")
TTS_RESOURCE_ID = os.getenv("TTS_RESOURCE_ID", "seed-tts-2.0")
URL = os.getenv("DOUBAO_TTS_URL", "https://openspeech.bytedance.com/api/v3/tts/unidirectional")

# 语料：尽量覆盖不同句式/情绪/长度，让模型见到更多发音组合
SENTENCES = [
    "主人今天回来得好晚呀，本鱼都等困了。",
    "你在干嘛呢，理理我嘛。",
    "今天天气不错，要不要一起出去走走？",
    "本鱼饿了，想吃火锅，还想吃烧烤。",
    "你昨天晚上说的话，我可都记着呢。",
    "哎呀，别摸头啦，头发都要乱了。",
    "我最近在学新的歌，等学会了唱给你听。",
    "你怎么又熬夜了，眼睛都红了。",
    "这道题我想了半天，还是没想明白。",
    "主人主人，快看我新买的裙子。",
    "雨下得好大，我们躲一下再走吧。",
    "你笑起来的样子，还挺好看的。",
    "今天上班累不累，要不要捶捶背？",
    "我不开心，你要哄我。",
    "听说最近很流行这个，我们试试？",
    "你说话能不能温柔一点。",
    "这个周末有空吗，陪我去看海吧。",
    "本鱼今天游了好远，尾巴都酸了。",
    "别老盯着手机，看看我呀。",
    "我把晚饭做好了，快来吃。",
    "你今天穿这件衣服，还挺帅的。",
    "我们认识多久了，你还记得吗？",
    "别生气啦，我错了还不行吗。",
    "我做梦梦到你了，醒来发现你不在。",
    "这首歌好好听，我们循环播放吧。",
    "你猜我今天遇到什么事了。",
    "冰箱里还有蛋糕，要不要分你一半。",
    "我想去旅行，去很远很远的地方。",
    "你怎么这么慢，我等了好久。",
    "好啦好啦，都听你的。",
    "今天风好大，帽子都要飞走了。",
    "你说我们以后会怎么样呢。",
    "本鱼最喜欢主人了，这是真心话。",
    "晚饭吃什么呢，我有点选择困难。",
    "这个颜色好看，还是那个颜色好看？",
    "别哭呀，我会一直在这里的。",
    "你今天有没有想我？",
    "我学会了一个新菜，等下有口福了。",
    "快看，天上有很漂亮的云。",
    "明天要早起，今天得早点睡。",
    "你说话算数吗，不许反悔。",
    "我给你准备了小礼物，猜猜是什么。",
    "今天的月亮好圆呀。",
    "累的时候就靠着我休息一会儿。",
    "你怎么不说话了，在想什么？",
    "这件事我想听听你的看法。",
    "我一个人也可以，但有你更好。",
    "记得多喝水，别老是忘记。",
    "真拿你没办法，随你啦。",
    "我们明天再聊，今天先睡吧。",
]


async def synth(text: str, client: httpx.AsyncClient) -> bytes | None:
    payload = {
        "req_params": {
            "text": text,
            "speaker": TTS_SPEAKER,
            "audio_params": {"format": "mp3", "sample_rate": 24000,
                             "speech_rate": 0, "loudness_rate": 0},
        }
    }
    headers = {
        "X-Api-Key": TTS_API_KEY,
        "X-Api-Resource-Id": TTS_RESOURCE_ID,
        "X-Api-Request-Id": str(uuid.uuid4()),
        "Content-Type": "application/json",
        "Connection": "keep-alive",
        "X-Control-Require-Usage-Tokens-Return": "*",
    }
    buf = bytearray()
    try:
        async with client.stream("POST", URL, json=payload, headers=headers, timeout=120) as resp:
            if resp.status_code != 200:
                body = (await resp.aread())[:200].decode("utf-8", "ignore")
                print(f"[失败] HTTP {resp.status_code} {body} | {text[:12]}", flush=True)
                return None
            async for chunk in resp.aiter_bytes():
                # 单向流：返回的是 json 行，每行含 base64 音频
                if chunk[:2] == b"\xff\xfb" or chunk[:3] == b"ID3":
                    buf.extend(chunk)
                    continue
                text_chunk = chunk.decode("utf-8", "ignore")
                for line in text_chunk.splitlines():
                    line = line.strip()
                    if not line.startswith("{"):
                        continue
                    import base64
                    import json as _json
                    try:
                        obj = _json.loads(line)
                    except Exception:  # noqa: BLE001
                        continue
                    b64 = obj.get("data") or (obj.get("result") or {}).get("data")
                    if b64:
                        buf.extend(base64.b64decode(b64))
        return bytes(buf) if len(buf) > 2000 else None
    except Exception as e:  # noqa: BLE001
        print(f"[异常] {type(e).__name__}: {e}", flush=True)
        return None


async def main() -> None:
    if not TTS_API_KEY:
        print("没读到 TTS_API_KEY")
        return
    OUT.mkdir(parents=True, exist_ok=True)
    ff = None
    try:
        sys.path.insert(0, str(ROOT))
        from plugins.douyin_fetch import _ffmpeg
        ff = _ffmpeg()
    except Exception:  # noqa: BLE001
        ff = "ffmpeg"
    total = 0
    idx = 0
    async with httpx.AsyncClient() as client:
        # 每句重复 4 次（不同标点/语速组合），凑时长
        for rep in range(8):
            for sent in SENTENCES:
                idx += 1
                data = await synth(sent, client)
                if not data:
                    continue
                mp3 = OUT / f"tts_{idx:04d}.mp3"
                mp3.write_bytes(data)
                wav = OUT / f"tts_{idx:04d}.wav"
                import subprocess
                subprocess.run([ff, "-y", "-i", str(mp3), "-ar", "44100", "-ac", "1", str(wav)],
                               capture_output=True, timeout=120)
                mp3.unlink(missing_ok=True)
                if wav.exists():
                    total += 1
                if total % 20 == 0:
                    print(f"已合成 {total} 句", flush=True)
    print(f"完成：{total} 句，目录 {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
