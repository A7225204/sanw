"""QQ空间扫码登录 + 保存 cookies（供自动发说说/日记使用）。"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / "data" / "qzone_profile"
OUTBOX = ROOT / "output" / "douyin" / "outbox"
COOKIES = ROOT / "data" / "qzone_cookies.json"


def main() -> None:
    PROFILE.mkdir(parents=True, exist_ok=True)
    OUTBOX.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE), channel="msedge", headless=True,
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://i.qq.com/", timeout=60000)
        page.wait_for_timeout(6000)
        qr = ROOT / "output" / "qzone_login.png"
        page.screenshot(path=str(qr))
        shutil.copy(qr, OUTBOX / "qzone_qr.png")
        print(f"QR {qr}", flush=True)

        ok = False
        for _ in range(72):
            time.sleep(5)
            cookies = ctx.cookies("https://user.qzone.qq.com")
            names = {c.get("name"): c.get("value") for c in cookies}
            if names.get("p_skey") and (names.get("uin") or names.get("skey")):
                COOKIES.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
                print("LOGIN OK", flush=True)
                ok = True
                break
            if "user.qzone.qq.com" in page.url and "login" not in page.url:
                cookies = ctx.cookies("https://user.qzone.qq.com")
                if cookies:
                    COOKIES.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
                    print("LOGIN REDIRECT OK", flush=True)
                    ok = True
                    break
        if not ok:
            print("LOGIN TIMEOUT", flush=True)
        time.sleep(5)
        ctx.close()


if __name__ == "__main__":
    main()
