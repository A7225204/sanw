"""在同一个页面上投递 BOSS 岗位（不 reload、不逐个开详情页）。

流程：
  1. 用已知的筛选结果（boss_jobs.json 里 match=True 的岗位）当"白名单"
  2. 在搜索页左侧列表里翻卡片，卡片标题+公司命中白名单才点
  3. 点开后右侧详情面板出现 .op-btn-chat（立即沟通）就点
  4. 列表滑到底自动加载下一批

用法：
  python tools\\boss_apply_inpage.py --limit 10
  python tools\\boss_apply_inpage.py --limit 10 --kw 产品动画
"""
from __future__ import annotations

import argparse
import json
import pathlib
import random
import sys
import time
from datetime import date
from urllib.parse import quote

from playwright.sync_api import sync_playwright

CDP = "http://127.0.0.1:9222"
ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
JOBS_FILE = DATA / "boss_jobs.json"
STATE_FILE = DATA / "boss_apply_state.json"

CITY_CODE = "101280600"


def _load_targets() -> dict[tuple, dict]:
    jobs = json.loads(JOBS_FILE.read_text(encoding="utf-8")) if JOBS_FILE.exists() else []
    out = {}
    for j in jobs:
        if j.get("match"):
            out[(j["title"].strip(), j["company"].strip())] = j
    return out


def _state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"day": "", "count": 0, "applied": []}


def _save(st: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def _scroll(page, times: int = 1) -> None:
    for _ in range(times):
        try:
            page.locator(".job-list-container").first.hover()
        except Exception:  # noqa: BLE001
            pass
        page.mouse.wheel(0, 1200)
        time.sleep(1.6)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--kw", default="亚马逊设计")
    ap.add_argument("--max-scroll", type=int, default=15)
    args = ap.parse_args()

    targets = _load_targets()
    if not targets:
        print("[x] boss_jobs.json 里没有 match=True 的岗位，先跑 boss_apply.py scan")
        return 2

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
        ctx = b.contexts[0]
        page = ctx.pages[0]

        # 不主动导航：BOSS 会恢复你手动设的筛选条件，一导航反而把 query 丢掉
        if not page.url.startswith("https://www.zhipin.com/web/geek/"):
            url = f"https://www.zhipin.com/web/geek/job?query={quote(args.kw)}&city={CITY_CODE}"
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            time.sleep(3)
        # 等卡片渲染出来（最多 20 秒），别急着判断"列表为空"
        for _ in range(14):
            if page.locator(".job-card-wrap").count():
                break
            time.sleep(1.5)
        print(f"[+] 页面已就位（{page.locator('.job-card-wrap').count()} 张卡片），白名单 {len(targets)} 个岗位")

        done = 0
        seen: set[tuple] = set()
        for scroll_round in range(args.max_scroll):
            n = 0
            for _try in range(8):
                n = page.locator(".job-card-wrap").count()
                if n:
                    break
                time.sleep(1.5)
            if n == 0:
                print("[x] 列表一直空着（可能 SPA 在重渲染，稍后再跑）")
                break

            advanced = False
            for i in range(n):
                if done >= args.limit:
                    break
                try:
                    card = page.locator(".job-card-wrap").nth(i)
                    title = card.locator(".job-name").first.inner_text().strip()
                    try:
                        company = card.locator(".boss-name").first.inner_text().strip()
                    except Exception:  # noqa: BLE001
                        company = ""
                except Exception:  # noqa: BLE001
                    continue
                key = (title, company)
                if key in seen:
                    continue
                seen.add(key)
                if key not in targets:
                    continue

                print(f"\n[命中] {title} @ {company}  ({targets[key]['salary']})")
                try:
                    page.locator(".job-card-wrap").nth(i).click()
                    time.sleep(2.0)
                except Exception as e:  # noqa: BLE001
                    print(f"    [失败] 点不开卡片：{str(e)[:80]}")
                    continue

                detail = page.locator(".job-detail-box").first
                try:
                    d_title = detail.locator(".job-name").first.inner_text().strip()
                except Exception:  # noqa: BLE001
                    d_title = ""
                btn = page.locator(".job-detail-box .op-btn-chat").first
                if not btn.count():
                    print("    [跳过] 详情里没有沟通按钮")
                    continue
                txt = (btn.inner_text() or "").strip()
                if "继续" in txt:
                    print("    [跳过] 已沟通过")
                    st["applied"].append(targets[key]["url"])
                    _save(st)
                    continue
                try:
                    btn.click(timeout=8000)
                    time.sleep(3.0)
                    done += 1
                    advanced = True
                    st["applied"].append(targets[key]["url"])
                    st["count"] = int(st.get("count") or 0) + 1
                    _save(st)
                    print(f"    [ok] 已投，今日累计 {st['count']}")
                except Exception as e:  # noqa: BLE001
                    print(f"    [失败] 点沟通出错：{str(e)[:80]}")
                time.sleep(random.uniform(8, 16))

            if done >= args.limit:
                break
            if not advanced:
                # 这一屏没有命中的，往下滑加载更多
                _scroll(page, 2)

        print(f"\n本轮结束：投递 {done} 个，今日累计 {st['count']}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
