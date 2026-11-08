"""手动生成一个梦（不等 2:15 定时）。

独立脚本里不能走 ai_group._chat（需要 bot 上下文），这里直接调
SiliconFlow / DeepSeek，并兼容两种 messages 格式（字符串 / 带 type 的数组）。
"""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, r"D:\qq-ai-bot")

from plugins import dream                      # noqa: E402
from server.memory_growth import _env          # noqa: E402

ROOT = Path(r"D:\qq-ai-bot")


async def ask(messages: list) -> str:
    import httpx
    env = _env()
    cands = []
    if env.get("SILICONFLOW_API_KEY"):
        cands.append((env.get("SILICONFLOW_BASE_URL") or "https://api.siliconflow.cn/v1",
                      env.get("SILICONFLOW_MODEL") or "deepseek-ai/DeepSeek-V4.1-Flash",
                      env["SILICONFLOW_API_KEY"]))
    if env.get("DEEPSEEK_API_KEY"):
        cands.append((env.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com",
                      env.get("DEEPSEEK_MODEL") or "deepseek-chat",
                      env["DEEPSEEK_API_KEY"]))

    last = ""
    for base, model, key in cands:
        for fmt in ("as-is",):
            try:
                async with httpx.AsyncClient(timeout=120) as cli:
                    r = await cli.post(
                        base.rstrip("/") + "/chat/completions",
                        headers={"Authorization": f"Bearer {key}"},
                        json={"model": model, "temperature": 1.0, "messages": messages},
                    )
                    data = r.json()
                if isinstance(data, dict) and data.get("choices"):
                    out = str((data.get("choices") or [{}])[0].get("message", {}).get("content") or "")
                    if out.strip():
                        return out
                last = f"{base}({fmt}) HTTP {r.status_code}: {str(data)[:120]}"
            except Exception as e:  # noqa: BLE001
                last = f"{base}({fmt}) {type(e).__name__}: {e}"
    raise RuntimeError(last)


def main() -> None:
    mat = dream._material()
    print("素材 facts:", len(mat.get("facts") or []), "| kind:", mat.get("kind"))
    text = (asyncio.run(ask(dream._prompt(mat))) or "").strip()   # _prompt 返回 messages 数组
    dream_text, insights = dream._split_dream(text)
    if len(dream_text) < 20:
        print("生成失败（太短）：", text[:100])
        return
    f = ROOT / "memory" / "dreams.json"
    try:
        d = json.loads(f.read_text(encoding="utf-8-sig"))
    except Exception:
        d = {"items": [], "last_date": ""}
    d.setdefault("items", []).append({
        "at": time.strftime("%Y-%m-%d %H:%M"),
        "dream": dream_text[:400],
        "insights": insights,
        "kind": mat.get("kind"),
        "hormones": {k: round(float(v), 1) for k, v in (mat.get("hormones") or {}).items()},
        "src": mat.get("facts", [])[:6],
    })
    d["last_date"] = time.strftime("%Y-%m-%d")
    f.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    print("已写入梦境：", dream_text[:150])
    print("梦的收获：", insights)


if __name__ == "__main__":
    main()
