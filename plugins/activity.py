"""大肥鱼“现在在干啥”状态写入（供各插件共用）。"""
from __future__ import annotations

import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILE = ROOT / "memory" / "activity_state.json"


def set_activity(label: str, ttl: int = 120, detail: str = "") -> None:
    try:
        now = time.time()
        FILE.parent.mkdir(parents=True, exist_ok=True)
        FILE.write_text(
            json.dumps({"activity": label, "detail": detail, "ts": now,
                        "expires": now + ttl if ttl > 0 else 0},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass
