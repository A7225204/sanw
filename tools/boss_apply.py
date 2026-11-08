"""BOSS 直聘求职助手（半自动）：按主人的条件筛岗位 + 投递。

筛选条件（主人 2026-10-30 定的）：
  · 城市：深圳
  · 双休（welfareList 含「双休」或标题含双休）
  · 月薪下限 ≥ 10K（"10-15K" 取 10；面议跳过）
  · 方向：亚马逊设计 / 渲染 / 建模 / 平面 / 视觉 / 美工 / 3D 动画
  · 排除：运营、开发、销售、总监/主管、采购、客服
  · 地区：排除宝安区（太远），其他区都投

用法：
  chrome.exe --remote-debugging-port=9222 --user-data-dir=D:\\qq-ai-bot\\data\\boss_profile
  python tools\\boss_apply.py scan
  python tools\\boss_apply.py apply --limit 6
"""
from __future__ import annotations

import argparse
import json
import pathlib
import random
import re
import sys
import time
from datetime import date
from urllib.parse import quote

from playwright.sync_api import sync_playwright

CDP = "http://127.0.0.1:9222"
API = "https://www.zhipin.com/wapi/zpgeek/search/joblist.json"
DETAIL = "https://www.zhipin.com/job_detail/{}.html"
LIST_URL = "https://www.zhipin.com/web/geek/job?query=%E4%BA%9A%E9%A9%AC%E9%80%8A%E8%AE%BE%E8%AE%A1&city=101280600"

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
JOBS_FILE = DATA / "boss_jobs.json"
STATE_FILE = DATA / "boss_apply_state.json"

CITY = "深圳"
CITY_CODE = "101280600"

KEYWORDS = ["亚马逊设计", "亚马逊平面", "产品动画", "建模",
            "渲染", "AIGC设计师", "平面设计"]

TITLE_GOOD = ["设计", "美工", "渲染", "建模", "平面", "视觉", "3D", "3d",
              "动画", "Maya", "maya", "Blender", "blender", "C4D", "c4d",
              "AIGC", "Aigc", "AGI", "UE", "模型"]
TITLE_BAD = ["运营", "开发", "销售", "采购", "客服", "文员", "助理",
             "总监", "主管", "经理", "实习", "学徒"]

SALARY_MIN_K = 10.0
AREA_BAD = ["宝安"]

MAX_PER_RUN = 20


# ---------------------------------------------------------------- 状态
def _state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"day": "", "count": 0, "applied": []}


def _save_state(st: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def _connect(pw):
    try:
        browser = pw.chromium.connect_over_cdp(CDP)
    except Exception as e:  # noqa: BLE001
        print(f"[x] 连不上 {CDP}: {e}")
        print("    先运行：chrome.exe --remote-debugging-port=9222 --user-data-dir=D:\\qq-ai-bot\\data\\boss_profile")
        raise SystemExit(2)
    return browser, (browser.contexts[0] if browser.contexts else browser.new_context())


# ---------------------------------------------------------------- 规则
def salary_low(text: str) -> float | None:
    s = (text or "").replace(" ", "")
    m = re.search(r"(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)K", s)
    if m:
        return float(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)K以上", s)
    if m:
        return float(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)K", s)
    if m:
        return float(m.group(1))
    return None


def _match(n: dict) -> tuple[bool, str]:
    title = n["title"]
    welfare = "".join(n.get("welfare") or [])
    if "双休" not in welfare and "双休" not in title:
        return False, "非双休"
    low = salary_low(n["salary"])
    if low is None:
        return False, "薪资面议/无法解析"
    if low < SALARY_MIN_K:
        return False, f"薪资低于{SALARY_MIN_K:.0f}K"
    if any(b in title for b in TITLE_BAD):
        return False, "岗位方向不对"
    if not any(g.lower() in title.lower() for g in TITLE_GOOD):
        return False, "不是设计/渲染/建模/平面"
    if any(b in (n.get("area") or "") for b in AREA_BAD):
        return False, "宝安太远"
    if n.get("city") and "深圳" not in n["city"]:
        return False, "不在深圳"
    return True, "ok"


# ---------------------------------------------------------------- 抓取
def _fetch(ctx, kw: str, page: int, size: int = 30) -> dict:
    url = (f"{API}?scene=1&query={quote(kw)}&city={CITY_CODE}"
           f"&page={page}&pageSize={size}")
    r = ctx.request.get(url, headers={"x-requested-with": "XMLHttpRequest",
                                      "referer": "https://www.zhipin.com/web/geek/job"})
    if r.status != 200:
        return {}
    try:
        return r.json()
    except Exception:  # noqa: BLE001
        return {}


def _norm(j: dict, kw: str) -> dict:
    welfare = [str(w) for w in (j.get("welfareList") or [])]
    title = str(j.get("jobName") or "").strip()
    return {
        "title": title,
        "salary": str(j.get("salaryDesc") or ""),
        "company": str(j.get("brandName") or ""),
        "city": str(j.get("cityName") or ""),
        "area": f"{j.get('areaDistrict') or ''}·{j.get('businessDistrict') or ''}".strip("·"),
        "exp": str(j.get("jobExperience") or ""),
        "degree": str(j.get("jobDegree") or ""),
        "skills": list(j.get("skills") or [])[:10],
        "welfare": welfare[:14],
        "weekend": ("双休" in "".join(welfare)) or ("双休" in title),
        "boss": str(j.get("bossName") or ""),
        "url": DETAIL.format(j.get("encryptJobId") or ""),
        "keyword": kw,
    }


def collect(ctx, kw: str, pages: int = 3) -> list[dict]:
    out: dict[tuple, dict] = {}
    for p in range(1, pages + 1):
        data = _fetch(ctx, kw, p)
        jobs = ((data.get("zpData") or {}).get("jobList") or [])
        if not jobs:
            break
        for j in jobs:
            n = _norm(j, kw)
            if not n["url"] or n["url"].endswith("/.html"):
                continue
            out.setdefault((n["title"], n["company"]), n)
        time.sleep(random.uniform(1.2, 2.2))
    return list(out.values())


# ---------------------------------------------------------------- scan
def cmd_scan(args) -> int:
    with sync_playwright() as pw:
        _b, ctx = _connect(pw)
        all_jobs: dict[tuple, dict] = {}
        for kw in (args.kw or KEYWORDS):
            print(f"[扫描] {kw}")
            for n in collect(ctx, kw, args.pages):
                all_jobs.setdefault((n["title"], n["company"]), n)

        jobs = list(all_jobs.values())
        hits, reasons = [], {}
        for n in jobs:
            ok, why = _match(n)
            n["match"], n["reason"] = ok, why
            if ok:
                hits.append(n)
            else:
                reasons[why] = reasons.get(why, 0) + 1

        hits.sort(key=lambda x: -(salary_low(x["salary"]) or 0))
        DATA.mkdir(parents=True, exist_ok=True)
        JOBS_FILE.write_text(json.dumps(jobs, ensure_ascii=False, indent=2), encoding="utf-8")

        print(f"\n共抓 {len(jobs)} 个，符合条件 {len(hits)} 个")
        print("淘汰原因:", ", ".join(f"{k}×{v}" for k, v in reasons.items()))
        print("=" * 92)
        for i, n in enumerate(hits, 1):
            print(f"{i:>2}. {n['title'][:30]:<30} {n['salary']:<12} {n['company'][:16]:<16} {n['area']:<12}")
        print(f"\n列表：{JOBS_FILE}")
        return 0


# ---------------------------------------------------------------- apply
def _safe_goto(page, url: str, retries: int = 3) -> bool:
    for i in range(retries):
        try:
            page.goto(url, wait_until="commit", timeout=30000)
            time.sleep(1.5)
            return True
        except Exception as e:  # noqa: BLE001
            if "interrupted" not in str(e).lower() and "Navigation" not in str(e):
                return False
            time.sleep(2.0 + i)
    return False


def _find_apply_button(page):
    """BOSS 详情页的沟通按钮是 .btn-startchat；用文本匹配会被「感兴趣 立即沟通」干扰。"""
    for sel in (".btn-startchat", ".btn-startchat-wrap", "a[href*='chat']", ".op-btn-chat"):
        try:
            loc = page.locator(sel).first
            if not loc.count():
                continue
            try:
                loc.scroll_into_view_if_needed(timeout=3000)
            except Exception:  # noqa: BLE001
                pass
            return loc
        except Exception:  # noqa: BLE001
            continue
    for txt in ("立即沟通", "继续沟通"):
        try:
            loc = page.get_by_text(txt, exact=True)
            if loc.count():
                return loc.first
        except Exception:  # noqa: BLE001
            pass
    try:
        area = page.locator(".btn-container, .job-detail-box").first
        if area.count() and ("已投递" in area.inner_text() or "继续沟通" in area.inner_text()):
            return "applied"
    except Exception:  # noqa: BLE001
        pass
    return None


def cmd_apply(args) -> int:
    st = _state()
    today = date.today().isoformat()
    if st.get("day") != today:
        st = {"day": today, "count": 0, "applied": []}

    jobs = json.loads(JOBS_FILE.read_text(encoding="utf-8")) if JOBS_FILE.exists() else []
    todo = [j for j in jobs if j.get("match") and j["url"] not in st["applied"]]
    todo.sort(key=lambda x: -(salary_low(x["salary"]) or 0))
    todo = todo[: min(int(args.limit or MAX_PER_RUN), MAX_PER_RUN)]
    if not todo:
        print("[i] 没有可投的（先 scan，或都投过了）")
        return 0

    with sync_playwright() as pw:
        _b, ctx = _connect(pw)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        for j in todo:
            for extra in list(ctx.pages):
                if extra is not page:
                    try:
                        extra.close()
                    except Exception:  # noqa: BLE001
                        pass
            print(f"\n[投递] {j['title']} @ {j['company']} {j['salary']} {j['area']}")
            try:
                if not _safe_goto(page, j["url"]):
                    print("    [失败] 详情页打不开")
                    continue
                time.sleep(1.5)
                if page.url.startswith("about:"):
                    print("    [停] 被重定向，可能触发风控")
                    break
                try:
                    page.wait_for_selector(".btn-startchat, .btn-startchat-wrap, .op-btn-chat",
                                           timeout=15000)
                except Exception:  # noqa: BLE001
                    pass
                btn = _find_apply_button(page)
                if btn is None:
                    try:
                        page.mouse.wheel(0, 800)
                        time.sleep(1.5)
                        btn = _find_apply_button(page)
                    except Exception:  # noqa: BLE001
                        pass
                if btn is None:
                    print("    [跳过] 没找到沟通按钮")
                    continue
                if btn == "applied":
                    print("    [跳过] 显示已投递过")
                    st["applied"].append(j["url"])
                    continue
                btn.click(timeout=8000)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(4.0)
                st["applied"].append(j["url"])
                st["count"] = int(st.get("count") or 0) + 1
                print(f"    [ok] 已投，今日累计 {st['count']}")
            except Exception as e:  # noqa: BLE001
                print(f"    [失败] {type(e).__name__}: {str(e)[:110]}")
            _save_state(st)
            time.sleep(random.uniform(30, 70))
    return 0


def cmd_status(args) -> int:
    st = _state()
    today = date.today().isoformat()
    n = int(st.get("count") or 0) if st.get("day") == today else 0
    jobs = json.loads(JOBS_FILE.read_text(encoding="utf-8")) if JOBS_FILE.exists() else []
    hits = [j for j in jobs if j.get("match")]
    print(f"今天({today})已投 {n} | 累计 {len(st.get('applied') or [])} | 符合条件 {len(hits)}")
    for j in [x for x in hits if x["url"] not in st["applied"]][:10]:
        print(f"  待投: {j['title'][:26]} {j['salary']} @ {j['company'][:12]}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan")
    s.add_argument("--kw", action="append")
    s.add_argument("--pages", type=int, default=3)
    s.set_defaults(func=cmd_scan)

    a = sub.add_parser("apply")
    a.add_argument("--limit", type=int, default=MAX_PER_RUN)
    a.set_defaults(func=cmd_apply)

    t = sub.add_parser("status")
    t.set_defaults(func=cmd_status)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
