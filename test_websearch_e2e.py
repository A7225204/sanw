"""端到端实测：方舟 Responses web_search 通道 + 图片回退 + 热搜抓取。"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from plugins import web_search as ws


async def main() -> None:
    print("=== 1) 真实联网问答（深圳天气） ===")
    messages = [
        {"role": "system", "content": "你是傲娇少女蓝色大肥鱼，回答简短口语化。"},
        {"role": "user", "content": "今天深圳天气怎么样？多少度？"},
    ]
    res = await ws.web_chat(messages)
    if res is None:
        print("❌ web_chat 返回 None（通道失败，会回退普通大脑）")
    else:
        reply, meta = res
        print("搜索次数:", meta.get("web_search"), "| 结果条数:", meta.get("results"))
        print("查询词:", meta.get("queries"))
        u = meta.get("usage") or {}
        print("usage:", {k: u.get(k) for k in ("input_tokens", "output_tokens", "total_tokens")})
        print("回复:\n", reply[:300])

    print("\n=== 2) 含图片消息 → 应返回 None（回退普通大脑，不打 Responses） ===")
    img_msgs = [
        {"role": "system", "content": "你是大肥鱼。"},
        {"role": "user", "content": [
            {"type": "text", "text": "这张图怎么样"},
            {"type": "image_url", "image_url": {"url": "https://example.com/a.jpg"}},
        ]},
    ]
    res2 = await ws.web_chat(img_msgs)
    print("返回:", "None（符合预期）" if res2 is None else f"❌ 非 None: {res2[0][:60]}")

    print("\n=== 3) 热搜抓取（百度实时热搜） ===")
    topics = await ws.fetch_hotlist(5)
    print("抓到条数:", len(topics))
    for t in topics:
        print("  -", t[:50])


asyncio.run(main())
