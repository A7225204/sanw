from playwright.sync_api import sync_playwright

PROF = r"D:\qq-ai-bot\data\qzone_profile_bot"
URLS = [
    "https://user.qzone.qq.com/3577977427/blog",
    "https://user.qzone.qq.com/3577977427/blog/list",
    "https://user.qzone.qq.com/3577977427/blog/1",
    "https://user.qzone.qq.com/3577977427/2",
]

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
    for u in URLS:
        try:
            page.goto(u, timeout=45000)
            page.wait_for_timeout(4000)
            body = page.inner_text("body")
            print("URL", u, "=>", page.url, "|", body[:120].replace("\n", " | "))
        except Exception as e:
            print("ERR", u, str(e)[:80])
    ctx.close()
