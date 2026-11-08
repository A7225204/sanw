"""手动触发一轮主动自学，验证：热搜 → 联网提炼 → global_learn 档案落盘 → 可被 prompt 读取。"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from plugins import web_search as ws
from plugins import long_memory as ltm


async def main() -> None:
    print("=== 触发 daily_learn() ===")
    stats = await ws.daily_learn()
    print("stats:", json.dumps(stats, ensure_ascii=False, indent=2))

    print("\n=== global_learn 档案条目 ===")
    text = ltm.archive_text(
        "global_learn",
        "【本鱼自学知识】以下是本鱼自己上网学到的公开常识 / 热点：",
    )
    print(text if text else "(空)")

    print("\n=== 长期记忆.json 落盘检查 ===")
    data = json.loads(ltm.ARCHIVE_FILE.read_text(encoding="utf-8"))
    archive = data.get("archive", {})
    pend = data.get("pending", {})
    print("scope 列表:", list(archive.keys()))
    gl = archive.get("global_learn") or []
    print(f"global_learn 档案 {len(gl)} 条；pending {len(pend.get('global_learn') or [])} 条")
    for it in gl[:6]:
        print("  -", it.get("fact", "")[:80])


asyncio.run(main())
