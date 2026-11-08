"""按岗位链接逐个投递（最稳的一条路：详情页直接有「立即沟通」）。

不依赖列表点击（BOSS 列表点击不切右侧面板），直接开详情页点按钮。
每个岗位之间随机等待，失败不重试，全程写日志到 data/boss_run.log。
"""
from __future__ import annotations

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
JOBS_FILE = DATA / "boss_jobs.json"
STATE_FILE = DATA / "boss_apply_state.json"
RUNLOG = DATA / "boss_run.log"


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with RUNLOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


def state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"day": "", "count": 0, "applied": []}


def save(st: dict) -> None:
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    jobs = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
    hits = [j for j in jobs if j.get("match")]
    st = state()
    today = date.today().isoformat()
    if st.get("day") != today:
        st = {"day": today, "count": 0, "applied": []}
    done = set(st.get("applied") or [])

    todo = [j for j in hits if j["url"] not in done]
    log(f"待投 {len(todo)} 个（已投过 {len(hits) - len(todo)} 个）")
    if not todo:
        return 0

    ok = 0
    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp(CDP)
        ctx = b.contexts[0]
        page = ctx.pages[0]
        for j in todo:
            log(f"→ {j['title']} | {j['salary']} | {j['company']}")
            try:
                page.goto(j["url"], wait_until="domcontentloaded", timeout=45000)
                time.sleep(2.0)
                try:
                    page.wait_for_selector(".btn-startchat, .btn-startchat-wrap", timeout=15000)
                except Exception:  # noqa: BLE001
                    pass
                btn = page.locator(".btn-startchat, .btn-startchat-wrap").first
                if not btn.count():
                    body = page.inner_text("body")[:120].replace("\n", " ")
                    log(f"   ✗ 没有沟通按钮（{body[:60]}）")
                    continue
                label = (btn.inner_text() or "").strip()
                if "继续" in label:
                    log("   - 已沟通过，跳过")
                    st["applied"].append(j["url"])
                    save(st)
                    continue
                btn.click(timeout=8000)
                time.sleep(3.0)
                ok += 1
                st["applied"].append(j["url"])
                st["count"] = int(st.get("count") or 0) + 1
                save(st)
                log(f"   ✓ 已投，今日累计 {st['count']}")
            except Exception as e:  # noqa: BLE001
                log(f"   ✗ {type(e).__name__}: {str(e)[:100]}")
            time.sleep(random.uniform(20, 40))

    log(f"完成：成功 {ok} 个，今日累计 {st['count']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
