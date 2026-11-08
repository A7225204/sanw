from playwright.sync_api import sync_playwright

PROF = r"D:\qq-ai-bot\data\qzone_profile_probe4"

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(PROF, channel="msedge", headless=True,
                                               viewport={"width": 1280, "height": 900})
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto("https://i.qq.com/", timeout=60000)
    page.wait_for_timeout(5000)
    for f in page.frames:
        if "ptlogin2" in f.url:
            try:
                f.get_by_text("蓝色大肥鱼", exact=False).first.click(timeout=5000)
                break
            except Exception:
                pass
    page.wait_for_timeout(8000)
    for idx, f in enumerate(page.frames):
        try:
            txt = f.inner_text("body")
        except Exception:
            continue
        if "写日志" in txt or "日志" in txt:
            print("FRAME", idx, f.url[:100], "|", txt[:300].replace("\n", " | "))
    ctx.close()
