"""打开可见的抖音创作者中心评论页（持久化登录态，供人工复制/发送回复）。

用的是一份独立的 profile 副本，不和后台每小时监控抢登录锁。
"""
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / "data" / "douyin_profile_view"


def main() -> None:
    PROFILE.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE), channel="msedge", headless=False,
            viewport={"width": 1360, "height": 960},
            args=["--start-maximized"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto("https://creator.douyin.com/creator-micro/interactive/comment", timeout=60000)
        except Exception:
            pass
        while True:
            try:
                page.wait_for_timeout(15000)
                if not ctx.pages:
                    break
            except Exception:
                break


if __name__ == "__main__":
    main()
