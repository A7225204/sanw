from playwright.sync_api import sync_playwright

PROF = r"D:\qq-ai-bot\data\qzone_profile_owner"
TEXT = "【测试】蓝色大肥鱼同步测试，稍后删除"

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(PROF, channel="msedge", headless=True,
                                                viewport={"width": 1280, "height": 900})
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto("https://i.qq.com/", timeout=60000)
    page.wait_for_timeout(6000)
    for f in page.frames:
        if "ptlogin2" in f.url:
            try:
                f.get_by_text("枷锁内的示例", exact=False).first.click(timeout=5000)
                break
            except Exception:
                pass
    page.wait_for_timeout(8000)
    page.get_by_text("说点儿什么吧", exact=False).first.click(force=True, timeout=5000)
    page.wait_for_timeout(1500)
    page.evaluate("() => { const e=document.querySelector('div.richTextBox'); if(e){e.focus();} }")
    page.keyboard.insert_text(TEXT)
    page.wait_for_timeout(500)
    val = page.evaluate("() => { const e=document.querySelector('div.richTextBox'); return e?e.innerText:''; }")
    print("EDITOR", repr(val[:50]))
    page.evaluate("() => { const b=document.querySelector('a.btn-post'); if(b){b.click();} }")
    page.wait_for_timeout(7000)
    print("POSTED", TEXT in page.inner_text("body"))
    ctx.close()
