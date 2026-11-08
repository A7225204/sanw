from playwright.sync_api import sync_playwright

PROF = r"D:\qq-ai-bot\data\qzone_profile_bot"

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
    page.evaluate("document.getElementById('feed_tab_blog').click()")
    page.wait_for_timeout(5000)
    print("URL", page.url)
    print("BODY", page.inner_text("body")[:400].replace("\n", " | "))
    info = page.evaluate("""() => [...document.querySelectorAll('a,button')]
        .map(a => ({t:(a.innerText||'').trim(), id:a.id, cls:a.className}))
        .filter(x => x.t && x.t.length < 12)""")
    print("HITS", [x for x in info if ("日志" in x["t"] or "写" in x["t"] or "发表" in x["t"])][:10])
    ctx.close()
