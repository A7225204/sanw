"""手动设置大肥鱼“现在在干啥”，供 DSH/其他进程调用。

用法：
  python tools/set_activity.py "用DSH写代码中" 300
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILE = ROOT / "memory" / "activity_state.json"

label = sys.argv[1] if len(sys.argv) > 1 else "发呆"
ttl = int(sys.argv[2]) if len(sys.argv) > 2 else 300
now = time.time()
clear = label in ("发呆", "空闲", "clear", "idle")
if clear:
    label, ttl = "发呆", 0
FILE.parent.mkdir(parents=True, exist_ok=True)
FILE.write_text(json.dumps({"activity": label, "detail": "", "ts": now,
                            "expires": now + ttl if ttl > 0 else 0},
                           ensure_ascii=False, indent=2), encoding="utf-8")
print(f"activity={label} ttl={ttl}s")
