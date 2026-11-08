"""抖音扫码登录 + 评论页抓取探针（B 半自动方案第一步）。

打开 Edge（持久化 profile），把登录二维码截图放到 outbox 让机器人发主人；
主人扫码后保存会话，并打开创作者中心评论页截图/文本，供下一步解析。
"""
from __future__ import annotations

import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / "data" / "douyin_profile"
OUTBOX = ROOT / "output" / "douyin" / "outbox"
OUT = ROOT / "output" / "douyin"


def main() -> None:
    PROFILE.mkdir(parents=True, exist_ok=True)
    OUTBOX.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE), channel="msedge", headless=False,
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://creator.douyin.com/creator-micro/home", timeout=60000)
        page.wait_for_timeout(6000)
        qr = OUTBOX / "douyin_qr.png"
        page.screenshot(path=str(qr))
        print(f"QR SAVED {qr}", flush=True)

        logged = False
        for _ in range(60):
            time.sleep(5)
            try:
                txt = page.inner_text("body")
            except Exception:
                txt = ""
            if "扫码登录" not in txt and "验证码登录" not in txt:
                logged = True
                break
        if not logged:
            print("LOGIN TIMEOUT", flush=True)
            ctx.close()
            return
        print("LOGIN OK", flush=True)

        try:
            page.goto("https://creator.douyin.com/creator-micro/interactive/comment", timeout=60000)
        except Exception as e:  # noqa: BLE001
            print(f"COMMENT GOTO ERR {e}", flush=True)
        page.wait_for_timeout(7000)
        shot = OUTBOX / "douyin_comment_page.png"
        page.screenshot(path=str(shot))
        body = page.inner_text("body")
        (OUT / "douyin_comment_page.txt").write_text(
            page.url + "\n\n" + body, encoding="utf-8"
        )
        print(f"COMMENT URL {page.url}", flush=True)
        print("COMMENT BODY " + body[:2000].replace("\n", " | "), flush=True)
        time.sleep(40)
        ctx.close()


if __name__ == "__main__":
    main()
