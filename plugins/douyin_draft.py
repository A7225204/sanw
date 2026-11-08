"""把生成好的竖屏视频传到抖音创作者中心，存成草稿，由主人手动点发布。"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

PROFILE = r"D:\qq-ai-bot\data\douyin_profile_view"
UPLOAD_URL = "https://creator.douyin.com/creator-micro/content/upload"


def _upload_sync(path: str, title: str, caption: str, tags: str) -> tuple[bool, str]:
    from playwright.sync_api import sync_playwright

    p = Path(path)
    if not p.exists() or p.stat().st_size < 100_000:
        return False, f"视频文件不存在或太小：{path}"

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            PROFILE, channel="msedge", headless=True,
            viewport={"width": 1400, "height": 900},
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(UPLOAD_URL, timeout=90000)
            page.wait_for_timeout(5000)
            if "login" in page.url or "passport" in page.url:
                return False, "创作者中心未登录，需要先扫码"
            if page.locator("input[type=file]").count() == 0:
                return False, "上传页没找到文件输入框"
            page.locator("input[type=file]").first.set_input_files(str(p))
            # 等进入编辑页
            deadline = time.time() + 120
            title_box = page.locator('input[placeholder*="填写作品标题"]').first
            while time.time() < deadline:
                if title_box.count() and title_box.is_visible():
                    break
                page.wait_for_timeout(2000)
            if not title_box.count():
                return False, "上传后没进入发布编辑页"
            page.wait_for_timeout(3000)
            try:
                title_box.fill(str(title)[:30])
            except Exception:
                pass
            body = str(caption)[:900]
            try:
                editor = page.locator("div.zone-container[contenteditable=true]").first
                if editor.count():
                    editor.click()
                    page.keyboard.insert_text(body)
                    page.wait_for_timeout(800)
                    # 逐个打成蓝色话题标：输入 #话题 后回车，让编辑器生成 data-mention
                    tag_list = []
                    for t in ("蓝色大肥鱼", "DeepSeek", "鲸鱼娘", "上热门"):
                        if t not in tag_list:
                            tag_list.append(t)
                    for t in str(tags).replace("，", " ").split():
                        t = t.lstrip("#").strip()
                        if t and t not in tag_list:
                            tag_list.append(t)
                    for t in tag_list[:5]:
                        page.keyboard.type(" #" + t)
                        page.wait_for_timeout(1800)
                        page.keyboard.press("Enter")
                        page.wait_for_timeout(400)
            except Exception:
                pass
            # 存草稿并离开
            clicked = False
            for text in ("暂存离开", "存草稿", "保存草稿"):
                btn = page.get_by_text(text, exact=True)
                if btn.count():
                    btn.last.click(force=True, timeout=8000)
                    clicked = True
                    break
            if not clicked:
                return False, "没找到存草稿按钮"
            page.wait_for_timeout(3500)
            for text in ("暂存", "保存", "确认", "确定", "存草稿"):
                btn = page.get_by_text(text, exact=True)
                if btn.count():
                    try:
                        btn.last.click(force=True, timeout=3000)
                        break
                    except Exception:
                        continue
            page.wait_for_timeout(3000)
            return True, "已存创作者中心草稿"
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"[:200]
        finally:
            try:
                ctx.close()
            except Exception:
                pass


async def upload_draft(path: str, title: str, caption: str, tags: str = "") -> tuple[bool, str]:
    return await asyncio.to_thread(_upload_sync, path, title, caption, tags)
