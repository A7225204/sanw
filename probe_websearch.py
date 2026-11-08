"""一次性探针：实测两条联网通道当前是否可用（不回显 Key）。
1) 方舟 Responses API + web_search 工具
2) Gemini generateContent + google_search grounding
问题取时效性最强的天气，便于判断是否真的触发了检索。
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import httpx

Q = "2026年10月22日深圳现在天气多少度？湿度多少？联网查最新实况再回答，3句话以内。"

print("=" * 20, "1) 方舟 Responses + web_search", "=" * 20)
try:
    body = {
        "model": os.getenv("AI_MODEL"),
        "tools": [{"type": "web_search", "max_keyword": 3}],
        "input": [{"role": "user", "content": [{"type": "input_text", "text": Q}]}],
    }
    r = httpx.post(
        "https://ark.cn-beijing.volces.com/api/v3/responses",
        headers={"Authorization": f"Bearer {os.getenv('AI_API_KEY')}", "content-type": "application/json"},
        json=body, timeout=60,
    )
    print("STATUS:", r.status_code)
    j = r.json()
    if j.get("error"):
        print("ERROR:", json.dumps(j["error"], ensure_ascii=False)[:300])
    else:
        print("MODEL:", j.get("model"), "| usage:", json.dumps(j.get("usage", {}), ensure_ascii=False))
        search_hits = 0
        msg_text = ""
        for b in j.get("output", []):
            t = b.get("type", "")
            if "web_search" in t:
                results = b.get("results") or []
                search_hits = len(results)
                if results:
                    print("  搜索命中标题:", " | ".join((x.get("title") or "")[:40] for x in results[:3]))
            elif t == "message":
                msg_text = "\n".join(c.get("text", "") for c in b.get("content", []))
        print("联网命中条数:", search_hits)
        print("回复:", msg_text[:300])
except Exception as e:
    print("方舟异常:", type(e).__name__, str(e)[:200])

print()
print("=" * 20, "2) Gemini + google_search grounding", "=" * 20)
try:
    gkey = os.getenv("GEMINI_API_KEY")
    gmodel = os.getenv("GEMINI_CHAT_MODEL")
    body = {
        "contents": [{"role": "user", "parts": [{"text": Q}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.7},
    }
    r = httpx.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{gmodel}:generateContent?key={gkey}",
        json=body, timeout=60,
    )
    print("STATUS:", r.status_code)
    j = r.json()
    if j.get("error"):
        print("ERROR:", json.dumps(j["error"], ensure_ascii=False)[:300])
    else:
        cands = j.get("candidates", [])
        if cands:
            parts = cands[0].get("content", {}).get("parts", [])
            txt = "\n".join(x.get("text", "") for x in parts)
            gm = cands[0].get("groundingMetadata") or {}
            print("检索引用数:", len(gm.get("groundingChunks", [])))
            print("回复:", txt[:300])
        else:
            print("RAW:", json.dumps(j, ensure_ascii=False)[:300])
except Exception as e:
    print("Gemini 异常:", type(e).__name__, str(e)[:200])
