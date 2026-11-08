"""入口：先做一遍自检，再起服务。

    python bot.py            # 正常启动（自检有 FAIL 会拦下来）
    python check.py          # 只想体检、不起服务时用这个

自检不通过就别启动了——起一半再炸，排查成本比这高得多。
"""
import sys
from pathlib import Path

# 自检报告里有 ✅/❌/⚠️ 等字符：stdout 被重定向到文件时，Windows 默认用 GBK，
# 2026-10-23 就因为 '\u2705' 编码失败直接起不来。这里统一强制 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import nonebot  # noqa: E402
from nonebot.adapters.onebot.v11 import Adapter as ONEBOT_V11Adapter  # noqa: E402

from config import FAIL, get_config, report, run_checks  # noqa: E402


def main() -> int:
    try:
        cfg = get_config()
    except Exception as e:  # noqa: BLE001
        print(f"❌ 配置加载失败：{type(e).__name__}: {e}")
        return 1

    checks = run_checks(cfg=cfg)
    print("===== 启动自检 =====")
    print(report(checks))
    print("===================\n")

    failed = [c for c in checks if c.level == FAIL]
    if failed:
        print("❌ 自检没过，为解决上面的问题后再启动。")
        return 1

    # 2026-11-05：L0 护栏 —— 核心文件被改坏时先报警，别等机器人跑起来才发现
    # （今天已经发生过两次改坏导致她不回话；这里只告警不拦启动，避免误报把她弄下线）
    try:
        _tools = str(__import__("pathlib").Path(__file__).resolve().parent / "tools")
        if _tools not in sys.path:
            sys.path.insert(0, _tools)
        import l0_guard  # noqa: E402

        _l0_ok, _l0_probs = l0_guard.verify(verbose=False)
        if not _l0_ok:
            print("\n" + "=" * 64)
            print("🔴 L0 护栏：核心文件有问题（机器人仍会启动，但可能行为异常）")
            for _p in _l0_probs:
                print(f"   · {_p}")
            print("   修好：python tools/l0_guard.py rollback --file <相对路径>")
            print("   应急：设 L0_GUARD=off 跳过校验")
            print("=" * 64 + "\n")
    except Exception:  # noqa: BLE001
        pass

    nonebot.init()
    driver = nonebot.get_driver()
    driver.register_adapter(ONEBOT_V11Adapter)
    nonebot.load_plugins("plugins")
    def _banner_name() -> str:
        try:
            import json as _j
            from pathlib import Path as _P
            d = _j.loads((_P(__file__).resolve().parent / "memory" / "persona_name.json").read_text(encoding="utf-8-sig"))
            return str(d.get("name") or "").strip() or cfg.persona.nickname
        except Exception:  # noqa: BLE001
            return cfg.persona.nickname

    print(f"🤖 {_banner_name()} 起来了：{cfg.network.ws_url}")
    nonebot.run(host=cfg.network.host, port=cfg.network.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
