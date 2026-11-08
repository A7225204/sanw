"""静态自检：配置加载 + 联网规则判定单测。不发起真实联网调用。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from config import get_config

cfg = get_config()
w = cfg.web
print("=== WebConfig ===")
print("enabled:", w.enabled, "| scope:", w.scope, "| max_keywords:", w.max_keywords)
print("force_words:", w.force_words)
print("learn_enabled:", w.learn_enabled, "| learn_times:", w.learn_times,
      "| learn_items:", w.learn_items, "| source:", w.learn_source)
print("topics_file:", w.topics_file, "exists:", w.topics_file.exists())

from plugins import web_search as ws

CASES = [
    # (文本, 是否私聊, 期望联网)
    ("你好", True, False),
    ("在吗", False, False),
    ("哈哈哈哈", True, False),
    ("今天深圳天气怎么样", True, True),
    ("现在比特币多少钱", False, True),
    ("最新的原神版本是几号发布的", True, True),
    ("最近有什么好看的电影", False, True),
    ("联网 查一下 DeepSeek 最新模型", True, True),
    ("搜一下 2026 年世界杯赛程", False, True),
    ("查查附近哪里有好吃的", True, True),
    ("你这个笨蛋", False, False),
    ("V我50", True, False),
    ("晚安", False, False),
    ("切换大脑 谷歌", True, False),
    ("画图 一只猫", False, False),
    ("唱歌 青藏高原", True, False),
    ("签到", False, False),
    ("逮捕 张三", True, False),
]

print("\n=== 规则判定 ===")
fail = 0
for text, is_priv, expect in CASES:
    got = ws.needs_web_search(text, is_priv)
    ok = got == expect
    if not ok:
        fail += 1
    print(f"{'PASS' if ok else 'FAIL'}  private={is_priv!s:<5} expect={expect!s:<5} got={got!s:<5}  {text}")

print("\n=== 结果 ===")
print("FAIL COUNT:", fail)
sys.exit(1 if fail else 0)
