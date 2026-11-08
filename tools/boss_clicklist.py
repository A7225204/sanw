"""在当前 BOSS 搜索页点卡片投递：只用页面 DOM，不跳转不刷新。

判断条件全从右侧详情面板读（都是明文，不会被字体混淆）：
  · 标题命中 设计/渲染/建模/平面/视觉/动画/AIGC/3D/模型 且不含 运营/开发/销售…
  · 标签里有「周末双休」（页面上的双休标签就叫这个）
  · 工作地址不含「宝安」
薪资页面读不出来（自定义字体），所以交给你在 BOSS 里已经设好的薪资筛选。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import random
import sys
import time
from datetime import date

from playwright.sync_api import sync_playwright

CDP = "http://127.0.0.1:9222"
ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
STATE_FILE = DATA / "boss_apply_state.json"

TITLE_GOOD = ["设计", "美工", "渲染", "建模", "平面", "视觉", "3D", "3d", "动画",
              "Maya", "Blender", "C4D", "AIGC", "AGI", "UE", "模型", "绘图"]
TITLE_BAD = ["运营", "开发", "销售", "采购", "客服", "文员", "助理", "总监", "主管", "实习", "学徒"]
AREA_BAD = ["宝安"]
WEEKEND_TAG = "周末双休"


def _state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"day": "", "count": 0, "applied": []}


def _save(st: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def _text(loc) -> str:
    try:
        return (loc.inner_text() or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--max", type=int, default=40)
    args = ap.parse_args()

    st = _state()
    today = date.today().isoformat()
    if st.get("day") != today:
        st = {"day": today, "count": 0, "applied": []}

    with sync_playwright() as pw:
        try:
            b = pw.chromium.connect_over_cdp(CDP)
        except Exception as e:  # noqa: BLE001
            print(f"[x] 连不上 {CDP}: {e}")
            return 2
        page = b.contexts[0].pages[0]
        for _ in range(10):
            if page.locator(".job-card-wrap").count():
                break
            time.sleep(1.5)
        n = page.locator(".job-card-wrap").count()
        print(f"[+] 当前页 {n} 张卡片（不刷新，直接点）")

        applied, skipped = 0, 0
        for i in range(min(n, args.max)):
            if applied >= args.limit:
                break
            card = page.locator(".job-card-wrap").nth(i)
            title = _text(card.locator(".job-name").first)
            if not title:
                continue
            try:
                card.click()
                time.sleep(1.5)
            except Exception:  # noqa: BLE001
                continue

            detail = page.locator(".job-detail-box").first
            d_title = _text(detail.locator(".job-name").first) or title
            tags = _text(detail.locator(".job-label-list").first)
            addr = _text(detail.locator(".job-address").first)
            btn = page.locator(".job-detail-box .op-btn-chat").first
            label = _text(btn) if btn.count() else ""

            good_title = (not any(b in d_title for b in TITLE_BAD)
                          and any(g.lower() in d_title.lower() for g in TITLE_GOOD))
            weekend = WEEKEND_TAG in tags
            far = any(b in addr for b in AREA_BAD)
            already = "继续" in label

            if not (good_title and weekend) or far:
                reason = []
                if not good_title:
                    reason.append("方向不符")
                if not weekend:
                    reason.append("非双休")
                if far:
                    reason.append("宝安")
                print(f"  - skip {d_title[:24]:<24} [{','.join(reason)}]")
                skipped += 1
                continue

            if already or not btn.count():
                print(f"  - skip {d_title[:24]:<24} [已沟通过/无按钮]")
                continue

            print(f"\n[命中] {d_title} | {tags[:60]} | {addr[:40]}")
            try:
                btn.click(timeout=8000)
                time.sleep(2.2)
                applied += 1
                st["count"] = int(st.get("count") or 0) + 1
                st.setdefault("hits", []).append({"title": d_title, "ts": time.strftime("%H:%M")})
                _save(st)
                print(f"    [ok] 已点沟通，今日累计 {st['count']}")
                try:
                    page.keyboard.press("Escape")
                except Exception:  # noqa: BLE001
                    pass
            except Exception as e:  # noqa: BLE001
                print(f"    [失败] {str(e)[:70]}")
            time.sleep(random.uniform(3.0, 6.0))

        print(f"\n本轮：投 {applied}，跳过 {skipped}，今日累计 {st['count']}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
