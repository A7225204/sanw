#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B 站扫码登录辅助工具（只跑一次，拿 cookie）。

    python tools/bili_login.py

流程：
1. 向 passport.bilibili.com 申请登录二维码，保存为 `bili_qr.png`（默认在仓库根目录）；
2. 轮询扫码状态，最多 4 分钟；
3. 扫码确认后把 SESSDATA / bili_jct / DedeUserID 等 cookie 写到 `.bili_cookies.json`
   （权限 0600，别提交到 git）。

二维码用手机 B 站 App（要登录的账号）扫，扫完在手机上点「确认登录」。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

ROOT = Path(__file__).resolve().parent.parent
QR_PATH = ROOT / "bili_qr.png"
COOKIE_PATH = ROOT / ".bili_cookies.json"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Referer": "https://www.bilibili.com/",
    "Accept": "application/json, text/plain, */*",
}


def save_cookies(client: httpx.Client, extra_url: str = "") -> dict:
    cookies = {}
    for cookie in client.cookies.jar:
        cookies[cookie.name] = cookie.value
    # data.url 里也可能带一遍 SESSDATA 等
    if extra_url:
        qs = parse_qs(urlparse(extra_url).query)
        for key in ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid"):
            if key in qs and key not in cookies:
                cookies[key] = qs[key][0]
    return cookies


def main() -> int:
    import qrcode

    with httpx.Client(timeout=20.0, headers=HEADERS, follow_redirects=True) as client:
        resp = client.get("https://passport.bilibili.com/x/passport-login/web/qrcode/generate")
        data = resp.json()
        if data.get("code") != 0:
            print(f"申请二维码失败：{data}")
            return 1
        qr_url = data["data"]["url"]
        qrcode_key = data["data"]["qrcode_key"]

        img = qrcode.make(qr_url)
        img.save(QR_PATH)
        print(f"QR_SAVED {QR_PATH}")
        print(f"QR_URL {qr_url}")
        sys.stdout.flush()

        deadline = time.time() + 240
        last_code = None
        while time.time() < deadline:
            time.sleep(2)
            poll = client.get(
                "https://passport.bilibili.com/x/passport-login/web/qrcode/poll",
                params={"qrcode_key": qrcode_key},
            )
            payload = poll.json()
            if payload.get("code") != 0:
                print(f"FAILED 轮询接口报错：{payload}")
                return 1
            body = payload.get("data") or {}
            # 真正的扫码状态在 data.code：
            # 86101 未扫码 / 86090 已扫码未确认 / 0 确认成功
            code = body.get("code")
            if code != last_code:
                print(f"STATUS code={code} message={payload.get('message')}")
                sys.stdout.flush()
                last_code = code
            if code == 0:
                cookies = save_cookies(client, (payload.get("data") or {}).get("url", ""))
                if "SESSDATA" not in cookies:
                    print("FAILED 扫码成功但没拿到 SESSDATA")
                    return 1
                # 用登录态确认身份
                nav = client.get("https://api.bilibili.com/x/web-interface/nav", cookies=cookies)
                nav_data = nav.json().get("data") or {}
                info = {
                    "cookies": cookies,
                    "uid": nav_data.get("mid"),
                    "uname": nav_data.get("uname"),
                    "saved_at": int(time.time()),
                }
                COOKIE_PATH.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
                try:
                    COOKIE_PATH.chmod(0o600)
                except OSError:
                    pass
                print(f"LOGIN_OK uid={info['uid']} uname={info['uname']}")
                return 0
            if code in (86038, 86039):
                print("EXPIRED 二维码过期，重新跑一次")
                return 2
        print("TIMEOUT 没等到扫码确认")
        return 2


if __name__ == "__main__":
    sys.exit(main())
