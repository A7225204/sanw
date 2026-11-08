"""打开可见的 QQ 空间页面，并用快捷登录自动进入主人空间（不用扫码）。"""
from __future__ import annotations

import os
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / os.getenv("QZONE_PROFILE", "data/qzone_profile_bot")
COOKIE_FILE = ROOT / "memory" / "qzone_cookies.json"
# 候选账号名（快捷登录要点的那个头像），逗号分隔，按顺序试
LOGIN_NAMES = [x.strip() for x in os.getenv(
    "QZONE_LOGIN_NAME", "蓝色大肥鱼,枷锁内的示例,示例"
).split(",") if x.strip()]


def _log(msg: str) -> None:
    try:
        (ROOT / "logs").mkdir(exist_ok=True)
        with (ROOT / "logs" / "qzone_login.log").open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:  # noqa: BLE001
        pass


def already_running() -> bool:
    try:
        import psutil
        n = 0
        for p in psutil.process_iter(["pid", "name", "cmdline"]):
            if p.pid == os.getpid():
                continue
            if "python" not in (p.info.get("name") or "").lower():
                continue
            cmd = " ".join(p.info.get("cmdline") or [])
            if "qzone_window.py" in cmd:
                n += 1
        return n > 0
    except Exception:
        return False


def main() -> None:
    PROFILE.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE), channel="msedge", headless=False,
            viewport={"width": 1360, "height": 920},
            args=["--start-maximized"],
        )
        # persistent profile 可能恢复出多个标签，只保留一个，避免"开了两个网页"
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        for _extra in ctx.pages[1:]:
            try:
                _extra.close()
            except Exception:
                pass
        try:
            page.goto("https://i.qq.com/", timeout=60000)
            page.wait_for_timeout(5000)
            if "user.qzone.qq.com" not in page.url:
                for frame in page.frames:
                    if "ptlogin2" in frame.url:
                        for _name in LOGIN_NAMES:
                            try:
                                frame.get_by_text(_name, exact=False).first.click(timeout=4000)
                                _log(f"快捷登录点击账号：{_name}")
                                break
                            except Exception:
                                continue
                        break
                page.wait_for_timeout(8000)
        except Exception:
            pass
        _saved = False
        _start = time.time()
        MAX_OPEN = float(os.getenv("QZONE_WINDOW_MAX_SEC", "360"))
        while True:
            try:
                # 已经登录成功 → 存完 cookie 就自动关窗
                if _saved:
                    _log("登录态已保存，窗口自动关闭")
                    break
                # 开太久还没登录 → 自动关，避免一直占着登录目录
                if time.time() - _start > MAX_OPEN:
                    _log(f"窗口开了 {MAX_OPEN:.0f} 秒，准备关闭（关闭前抢救一次登录态）")
                    if not _saved:
                        try:
                            cks = {c["name"]: c["value"] for c in ctx.cookies()}
                            if cks.get("uin") and cks.get("p_skey"):
                                COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
                                COOKIE_FILE.write_text(__import__("json").dumps(
                                    {"cookies": cks, "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                                    ensure_ascii=False, indent=1), encoding="utf-8")
                                _log("超时前抢救保存成功")
                        except Exception as _e:
                            _log(f"超时抢救失败：{_e}")
                    break
                page.wait_for_timeout(800)   # 主人登录完立刻关窗，5s 太慢会错过保存
                if not _saved:
                    try:
                        # 不再要求 URL 必须含 user.qzone.qq.com：登录后经常停在 i.qq.com
                        # 或跳转中，按 URL 判断会漏存（t8 诊断指出的 P1）。只要 cookie 齐全就存。
                        logged = True
                        if logged:
                            import time as _t
                            cks = {c["name"]: c["value"] for c in ctx.cookies()}
                            # 空间页刚打开时 uin/p_skey 可能还没写进来，存了也是残缺登录态
                            # （2026-10-30 就因此存了个 uin=None，等于没登录）
                            if not cks.get("uin") or not cks.get("p_skey"):
                                _log(f"cookie 还不全（uin={cks.get('uin')} p_skey={bool(cks.get('p_skey'))}），继续等")
                            else:
                                COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
                                COOKIE_FILE.write_text(__import__("json").dumps(
                                    {"cookies": cks, "saved_at": _t.strftime("%Y-%m-%d %H:%M:%S")},
                                    ensure_ascii=False, indent=1), encoding="utf-8")
                                _saved = True
                                try:
                                    (ROOT / "logs").mkdir(exist_ok=True)
                                    with (ROOT / "logs" / "qzone_login.log").open("a", encoding="utf-8") as f:
                                        f.write(f"{_t.strftime('%Y-%m-%d %H:%M:%S')} 已保存登录态 uin={cks.get('uin')}，可以关掉这个窗口了\n")
                                except Exception:
                                    pass
                    except Exception:
                        pass
                if not ctx.pages:
                    # 窗口被主人手动关了 → 关之前抢救一次登录态（否则这轮登录白做）
                    if not _saved:
                        try:
                            cks = {c["name"]: c["value"] for c in ctx.cookies()}
                            if cks.get("uin") and cks.get("p_skey"):
                                COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
                                COOKIE_FILE.write_text(__import__("json").dumps(
                                    {"cookies": cks, "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                                    ensure_ascii=False, indent=1), encoding="utf-8")
                                _log("窗口关闭前抢救保存成功")
                        except Exception as _e:
                            _log(f"窗口关闭前抢救失败：{_e}")
                    break
                try:
                    pages = []
                    for pg in ctx.pages:
                        try:
                            detail = pg.evaluate("""() => ({
                              inputs: [...document.querySelectorAll('input,textarea')].map(e=>({tag:e.tagName,id:e.id,cls:(e.className||'').toString().slice(0,60),ph:e.placeholder,val:(e.value||'').slice(0,60),vis:e.offsetWidth>0})).slice(0,30),
                              editables: [...document.querySelectorAll('[contenteditable=true]')].map(e=>({cls:(e.className||'').toString().slice(0,60),txt:(e.innerText||'').slice(0,60),vis:e.offsetWidth>0})).slice(0,20),
                              clicks: [...document.querySelectorAll('a,button,span')].filter(e=>e.offsetWidth>0).map(e=>(e.innerText||'').trim()).filter(t=>t&&t.length<12).slice(0,60)
                            })""")
                            pages.append({"url": pg.url, "body": pg.inner_text("body")[:3000], "detail": detail})
                        except Exception:
                            pass
                    info = {"pages": pages}
                    (ROOT / "output" / "qzone_live.json").write_text(
                        __import__("json").dumps(info, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                except Exception:
                    pass
            except Exception:
                break


if __name__ == "__main__":
    main()
