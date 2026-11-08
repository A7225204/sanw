from playwright.sync_api import sync_playwright

PROF = r"D:\qq-ai-bot\data\qzone_profile_probe3"

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
    box = page.evaluate("""() => { const e=document.getElementById('feed_tab_blog'); if(!e) return null; const r=e.getBoundingClientRect(); return {x:r.x+r.width/2,y:r.y+r.height/2}; }""")
    print("BOX", box)
    if box:
        page.mouse.click(box["x"], box["y"])
    page.wait_for_timeout(6000)
    body = page.inner_text("body")
    print("HAS_WRITE", "写日志" in body)
    if "写日志" in body:
        page.get_by_text("写日志", exact=False).first.click(force=True, timeout=5000)
        page.wait_for_timeout(4000)
        print("URL2", page.url)
        info = page.evaluate("""() => ({
            inputs: [...document.querySelectorAll('input,textarea,[contenteditable=true]')].map(e=>({t:e.tagName,id:e.id,cls:(e.className||'').toString().slice(0,50),ph:e.placeholder})).slice(0,12),
            buttons: [...document.querySelectorAll('a,button')].map(e=>(e.innerText||'').trim()).filter(t=>t&&t.length<10).slice(0,20)
        })""")
        print("INFO", info)
    ctx.close()
