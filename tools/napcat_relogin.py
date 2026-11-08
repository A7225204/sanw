"""NapCat 登录页监控：发现掉登录就自动尝试快速登录，失败则保存二维码。"""
from __future__ import annotations

import os
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "logs" / "napcat_relogin.log"
QR = ROOT / "output" / "napcat_qr_auto.png"
TOKEN = os.getenv("NAPCAT_WEBUI_TOKEN", "d0ba63f9368d")
ACCOUNT = os.getenv("NAPCAT_ACCOUNT", "3577977427")
INTERVAL = int(os.getenv("NAPCAT_RELOGIN_INTERVAL", "120"))


def _rotate_daily() -> None:
    """跨天时把 napcat_relogin.log 改名归档，避免长期 append 混在一起或被重建覆盖。"""
    try:
        if not LOG.exists() or LOG.stat().st_size == 0:
            return
        mtime_day = time.strftime("%Y%m%d", time.localtime(LOG.stat().st_mtime))
        if mtime_day >= time.strftime("%Y%m%d"):
            return
        dest = LOG.with_name(f"{LOG.stem}.{mtime_day}{LOG.suffix}")
        if dest.exists():
            dest = LOG.with_name(f"{LOG.stem}.{mtime_day}_{time.strftime('%H%M%S')}{LOG.suffix}")
        LOG.replace(dest)
    except Exception:  # noqa: BLE001
        pass


def log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    _rotate_daily()
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")


def _already_running() -> bool:
    lock = ROOT / "memory" / "napcat_relogin.lock"
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return False
    except FileExistsError:
        try:
            pid = int(lock.read_text(encoding="utf-8").strip() or "0")
        except Exception:
            pid = 0
        try:
            os.kill(pid, 0)
            return True
        except Exception:
            try:
                lock.unlink()
            except Exception:
                pass
            return _already_running()
    except Exception:
        return False


def _looks_like_qr(path: Path) -> bool:
    """粗验是不是二维码：黑白高对比 + 接近正方形。

    之前这里没有校验，`img,canvas` 里最后一个「宽度 > 100」的元素正好是页头 112px 的
    NapCat logo，于是每次掉线都存了一张头像让人去扫。加这道闸门。
    """
    try:
        from PIL import Image
        im = Image.open(path).convert("L")
        w, h = im.size
        if w < 150 or abs(w - h) > max(20, w * 0.2):
            return False
        hist = im.histogram()
        dark = sum(hist[:64])
        light = sum(hist[192:])
        return (dark + light) / float(w * h) > 0.6
    except Exception:  # 没装 PIL 就不拦
        return True


def _grab_qr(page, out: Path) -> Path | None:
    """抓真正的二维码：优先方形 canvas/img/svg/背景图，兜底按「扫描/二维码」文字锚点向上裁。"""
    js = """() => {
      const ok = r => r && r.width >= 150 && r.width <= 420 && Math.abs(r.width - r.height) <= 40;
      const out = [];
      document.querySelectorAll('img,canvas,svg,div').forEach(e => {
        const r = e.getBoundingClientRect();
        if (!ok(r)) return;
        const bg = getComputedStyle(e).backgroundImage || '';
        const isImg = e.tagName === 'IMG' && e.src && !String(e.src).includes('/assets/');
        const isCanvas = e.tagName === 'CANVAS';
        const isSvg = String(e.tagName).toLowerCase() === 'svg';
        const isBg = bg.includes('data:image') || bg.includes('blob:');
        if (isImg || isCanvas || isSvg || isBg) {
          out.push({x: r.x, y: r.y, w: Math.round(r.width), h: Math.round(r.height)});
        }
      });
      return out.sort((a, b) => b.w - a.w);
    }"""
    try:
        cands = page.evaluate(js) or []
    except Exception:  # noqa: BLE001
        cands = []
    for c in cands[:4]:
        try:
            page.screenshot(path=str(out), clip={"x": c["x"], "y": c["y"],
                                                 "width": c["w"], "height": c["h"]})
            if _looks_like_qr(out):
                return out
        except Exception:  # noqa: BLE001
            continue
    for anchor in ("扫描", "二维码", "QQ或者TIM"):
        try:
            box = page.get_by_text(anchor, exact=False).first.bounding_box()
        except Exception:  # noqa: BLE001
            box = None
        if not box:
            continue
        side = 320
        x = max(0, (box["x"] + box["width"] / 2) - side / 2)
        y = max(0, box["y"] - side - 10)
        try:
            page.screenshot(path=str(out), clip={"x": x, "y": y, "width": side, "height": side})
            if _looks_like_qr(out):
                return out
        except Exception:  # noqa: BLE001
            continue
    return None


def check_once() -> None:
    with sync_playwright() as p:
        b = p.chromium.launch(channel="msedge", headless=True)
        page = b.new_page(viewport={"width": 1000, "height": 760})
        page.goto(f"http://127.0.0.1:6099/webui?token={TOKEN}", timeout=20000)
        page.wait_for_timeout(2500)
        if "qq_login" not in page.url:
            b.close()
            return
        log("检测到登录页，尝试快速登录")
        try:
            sel = page.locator("select").first
            opts = sel.locator("option").all_text_contents() if sel.count() else []
            if ACCOUNT not in " ".join(opts):
                log(f"快速登录列表里没有 {ACCOUNT}，直接等扫码")
            else:
                sel.select_option(ACCOUNT)
                page.wait_for_timeout(600)
                page.get_by_text("登录", exact=True).last.click(force=True, timeout=5000)
                page.wait_for_timeout(8000)
        except Exception as e:  # noqa: BLE001
            log(f"快速登录失败：{type(e).__name__}: {e}")
        if "qq_login" not in page.url:
            log("快速登录成功")
            b.close()
            return
        # 还停在登录页：保存二维码，等主人扫
        try:
            clicked = False
            for sel in ('button[id$="tab-qrcode"]', 'button[id*="qrcode"]', '#tab-qrcode'):
                try:
                    if page.locator(sel).count():
                        page.locator(sel).first.click(timeout=4000)
                        clicked = True
                        break
                except Exception:  # noqa: BLE001
                    continue
            if not clicked:
                try:
                    page.get_by_text("扫码登录", exact=False).first.click(timeout=4000)
                    clicked = True
                except Exception:  # noqa: BLE001
                    pass
            page.wait_for_timeout(6000)
            got = _grab_qr(page, QR)
            if got:
                log(f"快速登录未成功，二维码已保存：{got}（{got.stat().st_size} 字节）")
            else:
                log("快速登录未成功，且没截到二维码（页面结构可能又变了）")
        except Exception as e:  # noqa: BLE001
            log(f"生成二维码失败：{type(e).__name__}: {e}")
        b.close()


def main() -> None:
    if _already_running():
        log("已有登录守护在跑，本实例退出")
        return
    log("监控启动")
    while True:
        try:
            check_once()
        except Exception as e:  # noqa: BLE001
            log(f"检查异常：{type(e).__name__}: {e}")
        # 二维码刚生成时多等一会，别每 2 分钟反复顶登录
        wait = INTERVAL
        try:
            if QR.exists() and time.time() - QR.stat().st_mtime < 600:
                wait = max(wait, 300)
        except Exception:
            pass
        time.sleep(wait)


if __name__ == "__main__":
    main()
