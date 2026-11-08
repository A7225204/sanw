"""大肥鱼 · BOSS 投递（新流程：Kimi WebBridge + dsh-plugin-boss-zhipin）。

不再使用旧的 tools/boss_apply*.py（CDP 直连，会被 BOSS 前端刁难）。

技能目录：C:\\Users\\Administrator\\.dsh\\profiles\\web\\node_modules\\dsh-plugin-boss-zhipin\\skill
工作目录：C:\\Users\\Administrator\\dsh-boss-run（画像在这里）

命令（仅主人私聊）：
    /投简历          环境自检 + 初始化 + 拉一批岗位做初筛，发列表
    /投简历 投 N     对已筛出的岗位逐条读 JD → 决策 → 投递
    /投简历 状态     进度
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from nonebot import on_message
from nonebot.adapters.onebot.v11 import PrivateMessageEvent
from nonebot.log import logger
from nonebot.rule import Rule

SKILL = Path(r"C:\Users\Administrator\.dsh\profiles\web\node_modules\dsh-plugin-boss-zhipin\skill")
RUN_PS1 = SKILL / "scripts" / "run.ps1"
WORK = Path(os.environ.get("BOSS_WORK_DIR") or Path.home() / "dsh-boss-run")
PROFILE = WORK / "user_profile.json"
CACHE = WORK / "surface_cache.json"
JD = WORK / "current_jd.json"
DECISION = WORK / "decision.json"
OWNER_UID = os.getenv("FEISHU_OWNER_UID", "100000000").strip()

_LOCK = asyncio.Lock()


def _run(step: str | None = None, script: str | None = None,
         index: int | None = None, keyword: str | None = None,
         timeout: float = 240.0) -> tuple[int, str]:
    """调用技能的 run.ps1（不要直接 python 调，它负责 UTF-8 和 PYTHONPATH）。"""
    if not RUN_PS1.exists():
        return 1, f"技能没装好：找不到 {RUN_PS1}"
    env = dict(os.environ)
    env["DSH_PYTHON"] = sys.executable
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    args = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(RUN_PS1), "-WorkDir", str(WORK)]
    if script:
        args += ["-Script", script]
    if step:
        args += ["-Step", step]
    if index is not None:
        args += ["-Index", str(index)]
    if keyword:
        args += ["-Keyword", keyword]
    try:
        p = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, timeout=timeout)
        return p.returncode, (p.stdout or "") + (("\n" + p.stderr) if p.stderr else "")
    except subprocess.TimeoutExpired:
        return -1, "技能脚本超时（可能卡在页面操作）"


def _profile() -> dict:
    try:
        return json.loads(PROFILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _cache() -> list[dict]:
    try:
        data = json.loads(CACHE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else list(data.values())
    except Exception:  # noqa: BLE001
        return []


def _prefilter(jobs: list[dict], prof: dict) -> list[dict]:
    """粗筛：标题关键词 + 排除词 + 薪资下限。细活交给 LLM 决策那一步。

    surface_cache 会跨批次累积（同一 page_order 在多个页面重复），
    所以先按 jobId 去重、只取最近 120 条（最新批次），避免投到重复岗位。
    """
    jobs = list(jobs)[-120:]
    seen: set[str] = set()
    uniq: list[dict] = []
    for j in jobs:
        key = str(j.get("jobId") or j.get("title") or "")
        if key and key in seen:
            continue
        seen.add(key)
        uniq.append(j)
    jobs = uniq
    prefer = [str(x) for x in (prof.get("prefer") or [])]
    avoid = [str(x) for x in (prof.get("avoid") or [])]
    min_k = float(prof.get("min_salary_k") or 0)
    out = []
    for j in jobs:
        title = str(j.get("title") or "")
        if any(a and a in title for a in avoid):
            continue
        if prefer and not any(p and p in title for p in prefer):
            continue
        try:
            if min_k and float(j.get("salary_max_k") or 0) < min_k:
                continue
        except Exception:  # noqa: BLE001
            pass
        out.append(j)
    return out


async def _decide_with_llm(jd: dict, prof: dict) -> dict:
    """让大肥鱼自己的模型按画像判 APPROVE / REJECT。"""
    try:
        from plugins import ai_group
        prompt = (
            "你是求职投递助手。根据用户画像判断这个岗位要不要投，只输出 JSON：\n"
            '{"decision":"APPROVE"或"REJECT","reason":"20字内"}\n\n'
            f"画像：{json.dumps(prof, ensure_ascii=False)[:2000]}\n\n"
            f"JD：{json.dumps(jd, ensure_ascii=False)[:3000]}\n\n"
            "规则：只投深圳、非宝安；设计/渲染/建模/平面/亚马逊电商方向；"
            "薪资下限≥10K 才投；双休优先（JD 没写明也先投，面试确认）；"
            "组长/设计主管/经理岗也可投（带过最多12人）；不确定就 REJECT。"
        )
        text = await ai_group.call_ai_raw(prompt, "deepseek") or ""
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            data = json.loads(text[start:end + 1])
            if data.get("decision") in ("APPROVE", "REJECT"):
                return data
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[投简历] LLM 决策失败：{type(e).__name__}: {e}")
    return {"decision": "REJECT", "reason": "决策失败，保守跳过"}


async def _is_boss_cmd(event) -> bool:
    if str(getattr(event, "user_id", "")) != str(OWNER_UID):
        return False
    _t = (event.get_plaintext() or "").strip()
    # 2026-11-05：裸 /全部投、/换一批 也接（主人习惯这么发）
    return (_t.startswith("/投简历") or _t.startswith("/全部投")
            or _t.startswith("/换一批"))


boss_cmd = on_message(rule=Rule(_is_boss_cmd), priority=3, block=True)


@boss_cmd.handle()
async def _(event: PrivateMessageEvent):
    _raw_txt = (event.get_plaintext() or "").strip()
    if _raw_txt.startswith("/全部投"):
        arm = "全部投"
    elif _raw_txt.startswith("/换一批"):
        arm = "换一批"
    else:
        arm = _raw_txt[len("/投简历"):].strip()

    if arm in ("", "扫", "扫描", "换一批"):
        if _LOCK.locked():
            await boss_cmd.finish("上一条还在跑")
        _is_swap = (arm == "换一批")
        async with _LOCK:
            if not _is_swap:
                await boss_cmd.send("检查环境…")
                rc, out = await asyncio.to_thread(_run, None, "env_check.py", None, None, 150)
                if rc != 0:
                    tail = "\n".join(out.strip().splitlines()[-10:])
                    await boss_cmd.finish("环境没就绪（要装 Kimi WebBridge）：\n" + tail[:900])
                await boss_cmd.send("初始化 + 拉一批岗位…")
                rc, out = await asyncio.to_thread(_run, "init", None, None, None, 180)
                if rc != 0:
                    await boss_cmd.finish("init 失败：\n" + out[-500:])
            else:
                await boss_cmd.send("换一批：翻下一页…")
            rc, out = await asyncio.to_thread(_run, "next-batch", None, None, None, 240)
            if rc != 0 and _is_swap:
                # 换一批失败常见于会话/页面状态过期，补跑一次 init 再试
                await boss_cmd.send("页面状态过期，重新初始化…")
                rc2, _o2 = await asyncio.to_thread(_run, "init", None, None, None, 180)
                if rc2 == 0:
                    rc, out = await asyncio.to_thread(_run, "next-batch", None, None, None, 240)
            if rc != 0:
                await boss_cmd.finish("拉岗位失败：\n" + out[-500:])
            prof = _profile()
            jobs = _prefilter(_cache(), prof)
            if not jobs:
                await boss_cmd.finish("这批没有符合画像的岗位，晚点再试或改画像")
            picks = jobs[:10]
            jd_list = [{"page_order": j.get("page_order"), "jobId": j.get("jobId"),
                        "title": j.get("title"), "salary": j.get("salary"),
                        "salary_max_k": j.get("salary_max_k"), "company": j.get("company"),
                        "industry": j.get("industry")} for j in picks]
            WORK.mkdir(parents=True, exist_ok=True)
            (WORK / "jd_to_read.json").write_text(
                json.dumps(jd_list, ensure_ascii=False, indent=2), encoding="utf-8")
            lines = [f"{i+1}. {j['title'][:24]} {j.get('salary','')} {j.get('company','')[:12]}"
                     for i, j in enumerate(picks)]
            await boss_cmd.finish(
                "初筛出 {} 个（待逐条读 JD 确认）：\n{}\n\n"
                "/全部投 全投 · /投简历 投 5 挑着投 · /换一批 翻下一页".format(
                    len(picks), "\n".join(lines)[:1000]))
        return

    # 2026-11-05：/全部投 —— 把这一批待读清单全部投掉（上限 20）
    if arm in ("全部投", "全投", "全部", "all"):
        try:
            _n = len(json.loads((WORK / "jd_to_read.json").read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            _n = 10
        arm = "投 " + str(max(1, min(_n, 20)))
        await boss_cmd.send(f"这批共 {_n} 个，本轮全投（上限 20）")

    if arm.startswith("投"):
        raw = arm[1:].strip() or "3"
        try:
            n = max(1, min(int(raw), 20))
        except ValueError:
            n = 3
        if _LOCK.locked():
            await boss_cmd.finish("上一条还在跑")
        async with _LOCK:
            prof = _profile()
            await boss_cmd.send(f"开始投 {n} 个（读 JD → 决策 → 投递）…")
            ok = 0
            for idx in range(1, n + 1):
                rc, out = await asyncio.to_thread(_run, "read", None, idx, None, 120)
                if rc != 0 or not JD.exists():
                    logger.warning(f"[投简历] read {idx} 失败：{out[-150:]}")
                    break
                jd = json.loads(JD.read_text(encoding="utf-8"))
                d = await _decide_with_llm(jd, prof)
                DECISION.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
                rc, out = await asyncio.to_thread(_run, "decide", None, idx, None, 180)
                if d.get("decision") == "APPROVE" and rc == 0:
                    ok += 1
                await boss_cmd.send(f"{idx}. {str(jd.get('title'))[:20]} → {d.get('decision')}（{d.get('reason','')}）")
            await boss_cmd.finish(f"本轮投递 {ok} 个。发「/投简历 状态」看进度")
        return

    if arm in ("状态", "st"):
        rc, out = await asyncio.to_thread(_run, "status", None, None, None, 60)
        await boss_cmd.finish((out or "").strip()[-800:] or "没拿到状态")
        return

    await boss_cmd.finish(
        "用法：/投简历（自检+拉一批） · /投简历 投 5 · /投简历 全部投 · /投简历 状态")
