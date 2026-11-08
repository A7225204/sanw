"""定时任务插件：早安 / 晚安 / 上课提醒。

依赖：nonebot-plugin-apscheduler（已在 .venv 安装）
文案由大模型按人设实时生成（非固定语句）。

配置都在别处了，别在这里改：
- 推送群号 `TARGET_GROUP`、提前提醒分钟 `REMIND_BEFORE` → `.env`
- 推送时刻、节次时间、课表 → 仓库根目录 `timetable.json`
"""
import asyncio
import datetime
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import nonebot  # noqa: E402

from nonebot_plugin_apscheduler import scheduler  # noqa: E402

from nonebot.adapters.onebot.v11 import Message, MessageSegment  # noqa: E402

from config import get_config  # noqa: E402
from plugins.ai_group import call_ai_raw, pick_emoji, split_reply  # noqa: E402
from plugins import long_memory as ltm  # noqa: E402
from plugins import group_lexicon as glx  # 群聊词条库：兜底消化攒批抽词缓冲  # noqa: E402
from plugins import reply_log  # noqa: E402
from plugins import web_search  # 主动自学（定时抓热搜入 global_learn 档案）  # noqa: E402

cfg = get_config()


async def call_ai(prompt: str) -> str:
    """定时推送默认走方舟(byte)大脑，兼容旧调用签名（旧版 call_ai(prompt)）。"""
    return await call_ai_raw(prompt, "deepseek")

# ==================== 配置（来自 config.py） ====================

TARGET_GROUP = cfg.schedule.target_group   # 群号（发早安/晚安/上课提醒到这个群）
REMIND_BEFORE = cfg.schedule.remind_before  # 课前提前多少分钟提醒

# 节次 → 上课时间（在 timetable.json 里改）
PERIOD_START = cfg.schedule.period_start

# 课表：星期(1=周一 ... 7=周日) → {节次: (课程名, 地点)}
TIMETABLE = {
    weekday: {row["period"]: (row["name"], row["location"]) for row in rows}
    for weekday, rows in cfg.schedule.timetable.items()
}

MORNING_H, MORNING_M = cfg.schedule.morning
NIGHT_H, NIGHT_M = cfg.schedule.night

# ===============================================================

WEEK = ["", "周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# 定时推送总开关：主人 2026-11-02 决定「TARGET_GROUP 不填 → 早晚安/上课提醒整个取消」。
# 之前是先生成文案再发现没群号，每天白花 token；现在没群号直接不开工。
PUSH_ENABLED = bool(TARGET_GROUP)
if not PUSH_ENABLED:
    nonebot.logger.info("[schedule] 未配置 TARGET_GROUP：早安/晚安/上课提醒 已取消（不生成、不发送）")


async def _push(text: str, tag: str = "推送") -> None:
    """把生成好的文案发到目标群（若含 [表情:情绪] 标记，则附一张表情包）。

    带换行的文案按行拆成多条发，和群聊回复一个规则（见 `cfg.reply.split_lines`）。
    """
    if not PUSH_ENABLED:
        return
    mood = ""
    m = re.search(r"\[表情[:：]\s*([^\]]*)\]", text)
    if m:
        mood = m.group(1).strip()
        text = re.sub(r"\[表情[:：]\s*[^\]]*\]", "", text).strip()

    lines = split_reply(text)
    chunks: list[Message] = [Message(ln) for ln in lines] or [Message("")]
    if mood:
        img = pick_emoji(mood)
        if img:
            chunks[-1].append(MessageSegment.image(img))

    try:
        bot = nonebot.get_bot()
        for i, msg in enumerate(chunks):
            await bot.send_group_msg(group_id=TARGET_GROUP, message=msg)
            if i < len(chunks) - 1:
                await asyncio.sleep(cfg.reply.line_gap)
        reply_log.record(group_id=TARGET_GROUP, speaker="", user_msg=tag, reply=text,
                         mood=mood, kind="scheduled")
    except Exception as e:  # noqa: BLE001
        nonebot.logger.warning(f"[schedule] 发送失败：{e}")
        reply_log.record(group_id=TARGET_GROUP, speaker="", user_msg=tag, reply=text,
                         mood=mood, kind="scheduled",
                         error=f"{type(e).__name__}: {e}"[:200])


@scheduler.scheduled_job("cron", hour=MORNING_H, minute=MORNING_M, id="daily_morning")
async def morning() -> None:
    if not PUSH_ENABLED:
        return
    text = await call_ai(
        "（现在是早上 6 点，群里刚醒。请你以群里沙雕群友的口吻发一句早安，"
        "随口扯一句就行，1~2 句话，别用固定模板、别喊口号）"
    )
    await _push(text, "早安")


@scheduler.scheduled_job("cron", hour=NIGHT_H, minute=NIGHT_M, id="daily_night")
async def night() -> None:
    if not PUSH_ENABLED:
        return
    text = await call_ai(
        "（现在是晚上 11 点，该睡了。请你以群里沙雕群友的口吻跟大伙说声晚安，"
        "随口扯一句，1~2 句话，别用固定模板）"
    )
    await _push(text, "晚安")


@scheduler.scheduled_job("cron", hour=4, minute=30, id="daily_memory",
                         misfire_grace_time=86400, coalesce=True)
async def daily_memory() -> None:
    """每天凌晨 4:30 把群里攒的消息合并进「长期记忆档案」（兜底）。

    平时攒够 MEM_CONSOLIDATE_EVERY 条就会自动合并一次，这里是保底，防止消息一直没攒够。
    """
    await ltm.consolidate_all()
    await _heal_vectors()


async def _heal_vectors() -> int:
    """档案新增后补向量：只补缓存里没有的 fact（增量，通常 1~3 秒）。

    2026-11-07 默认停用（原因见下）：
    这套 build_vectors 写的是 memory/mem_vectors.json，
    但全项目**没有任何召回路径读它** —— _load_vectors() 只在 build 内部用来判断"哪些已算过"。
    真正的语义召回走的是 memory_vec.py + memory/vec_index.json（同一个 sha1 算法，只是截断长度不同）。
    等于每 2 小时调一次 embedding，把同一批 fact 再算一遍，存进一个没人读的文件。
    函数保留（tools/build_mem_vectors.py 还能手动跑），只是不再定时补。
    要恢复：MEM_VECTORS_AUTOBUILD=1
    """
    import os as _os
    if str(_os.getenv("MEM_VECTORS_AUTOBUILD", "0")).lower() not in ("1", "true", "on"):
        return 0
    try:
        n = await asyncio.to_thread(ltm.build_vectors)
        if n:
            nonebot.logger.info(f"[向量] 自动补齐 {n} 条（新固化的记忆）")
        return n
    except Exception as e:  # noqa: BLE001
        nonebot.logger.warning(f"[向量] 自动补齐失败：{type(e).__name__}: {e}")
        return 0


@scheduler.scheduled_job("cron", hour="*/2", minute=15, id="memory_consolidate_recheck")
async def memory_consolidate_recheck() -> None:
    """每 2 小时兜底合并一轮：白天多次重启后 pending 已落盘，靠这里消化，
    不再只等凌晨 4:30 一次，防止档案长期空白。"""
    await ltm.consolidate_all()
    await _heal_vectors()


@scheduler.scheduled_job("interval", minutes=30, id="headless_cleanup",
                         misfire_grace_time=300, coalesce=True, max_instances=1)
async def headless_cleanup() -> None:
    """清理残留的无头 Chrome。

    背景：面板/页面验证开的 `--headless --enable-unsafe-swiftshader` 没有 GPU，
    Three.js 页面全靠 CPU 软件渲染，验证脚本异常退出后进程会残留并持续吃 CPU
    （实测堆到 11 个主进程 → 整机 CPU 49%）。只杀带 --headless 的，不碰正常浏览器。
    """
    try:
        ps = "powershell"
        script = str(ROOT / "tools" / "clean_headless_chrome.ps1")
        proc = await asyncio.create_subprocess_exec(
            ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=90)
        txt = (out or b"").decode("utf-8", "ignore")
        m = re.search(r"已结束\s*(\d+)\s*个进程", txt)
        if m and int(m.group(1)) > 0:
            nonebot.logger.info(f"[清理] 已结束 {m.group(1)} 个残留无头浏览器进程")
    except Exception:  # noqa: BLE001 —— 维护任务，失败不打扰
        pass


@scheduler.scheduled_job("interval", minutes=20, id="starfield_heal",
                         misfire_grace_time=300, coalesce=True, max_instances=1)
async def starfield_heal() -> None:
    """星海坐标自愈：档案比坐标缓存新 → 跑一次离线布局。

    背景：新增记忆/新增群会让 /api/memory/starfield 返回 202「生成中」并一直转圈
    （缓存里没有这些节点的坐标）。这里定期检查，缺了就补，主人不用手动跑脚本。
    """
    try:
        arch = ROOT / "memory" / "长期记忆.json"
        star = ROOT / "memory" / "memory_starfield.json"
        if not arch.exists():
            return
        if star.exists() and arch.stat().st_mtime <= star.stat().st_mtime + 30:
            return
        nonebot.logger.info("[星海] 坐标缓存落后于档案，开始补齐…")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(ROOT / "jobs" / "starfield_layout.py"), "--all",
            cwd=str(ROOT), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.wait(), timeout=600)
        nonebot.logger.info("[星海] 坐标补齐完成")
    except Exception as e:  # noqa: BLE001
        nonebot.logger.warning(f"[星海] 坐标自愈失败：{type(e).__name__}: {e}")


@scheduler.scheduled_job("cron", hour="*/2", minute=45, id="lexicon_consolidate_recheck")
async def lexicon_consolidate_recheck() -> None:
    """每 2 小时兜底消化各群词条攒批缓冲（未达批次阈值也定时抽一轮）；
    LEXICON_ENABLED=false 时模块内直接返回空列表。"""
    try:
        results = await glx.consolidate_pending()
        for r in results:
            if r.get("error"):
                nonebot.logger.warning(f"[schedule] 词条抽词失败 {r.get('scope')}: {r['error']}")
            elif r.get("batch"):
                nonebot.logger.info(
                    f"[schedule] 词条抽词完成 {r.get('scope')}: "
                    f"本批 {r.get('batch')} 条，新增 {r.get('added')}，更新 {r.get('updated')}，淘汰 {r.get('evicted')}"
                )
    except Exception as e:
        nonebot.logger.warning(f"[schedule] 词条兜底抽词异常: {type(e).__name__}: {e}")


@nonebot.get_driver().on_startup
async def _memory_startup() -> None:
    """启动时绑定主事件循环、冷启动回填历史消息；合并放后台任务，不阻塞 WS 连接。"""
    ltm.bind_main_loop()
    ltm.bootstrap_from_db()
    glx.init_table()
    asyncio.create_task(ltm.consolidate_all())
    asyncio.create_task(_catchup_dream())


async def _catchup_dream() -> None:
    """启动补做：凌晨 2:15 那次做梦如果因为进程不在而错过，启动后补一次。

    APScheduler 重启后不会补跑错过的 cron，所以 2:15 前后重启会漏掉当天的梦。
    这里在启动 2 分钟后检查：已过 2:15、今天还没做过，就补做。
    """
    await asyncio.sleep(120)
    try:
        from plugins import dream
        if not dream.ENABLED:
            return
        now = datetime.datetime.now()
        if now.hour < 2 or (now.hour == 2 and now.minute < 15):
            return  # 还没到今晚做梦的点，交给 2:15 的定时任务
        if dream.done_today():
            return
        text = await dream.make_dream()
        if text:
            nonebot.logger.info(f"[做梦] 启动补做：{text[:40]}…")
    except Exception as e:  # noqa: BLE001
        nonebot.logger.warning(f"[做梦] 启动补做失败：{type(e).__name__}: {e}")


@scheduler.scheduled_job("cron", minute="*", id="class_reminder",
                         misfire_grace_time=300, coalesce=True)
async def class_reminder() -> None:
    if not PUSH_ENABLED:
        return
    now = datetime.datetime.now()
    today = TIMETABLE.get(now.isoweekday(), {})
    hm = now.strftime("%H:%M")
    for period, (name, loc) in today.items():
        start = PERIOD_START.get(period)
        if not start:
            continue
        sh, sm = map(int, start.split(":"))
        remind_at = datetime.datetime(now.year, now.month, now.day, sh, sm) - datetime.timedelta(
            minutes=REMIND_BEFORE
        )
        if remind_at.strftime("%H:%M") == hm:
            where = f"，地点在{loc}" if loc else ""
            text = await call_ai(
                f"（提醒群里的同学：还有 {REMIND_BEFORE} 分钟要上「{name}」{where}。"
                "请你以群里沙雕群友的口吻提醒一句，1~2 句话，别太正经）"
            )
            await _push(text, f"上课提醒·{name}")


# ==================== 主动自学（每天两次，静默入记忆，不群播报） ====================

# 2026-11-06 按主人要求：去掉 12:30 / 21:30 的固定两班。
# 改成行动空间的 act_daily_learn —— 她想学的时候才学。
# web_search.daily_learn 本体保留，随时可调。
if cfg.web.learn_enabled:
    nonebot.logger.info("[schedule] 主动自学已改为按需触发（行动空间负责），不再定时")
else:
    nonebot.logger.info("[schedule] LEARN_ENABLED=false，跳过多余检查")

# ==================== 内分泌层：每分钟推进激素 + 每天衰减创伤 ====================
@scheduler.scheduled_job("interval", seconds=60, id="hormones_tick")
async def _hormones_tick() -> None:
    try:
        from plugins import hormones
        if hormones.ENABLED:
            await asyncio.to_thread(hormones.tick)
    except Exception as e:  # noqa: BLE001
        nonebot.logger.debug(f"[内分泌] tick 失败：{type(e).__name__}: {e}")


@scheduler.scheduled_job("cron", hour=4, minute=10, id="trauma_decay",
                         misfire_grace_time=86400, coalesce=True)
async def _trauma_decay() -> None:
    try:
        from plugins import trauma_mem
        await asyncio.to_thread(trauma_mem.tick)
        nonebot.logger.info("[创伤] 每日衰减完成")
    except Exception as e:  # noqa: BLE001
        nonebot.logger.warning(f"[创伤] 衰减失败：{type(e).__name__}: {e}")

@scheduler.scheduled_job("cron", hour=2, minute=15, id="night_dream",
                         misfire_grace_time=86400, coalesce=True)
async def _night_dream() -> None:
    """深夜做梦：一天一次，失败不重试。"""
    try:
        from plugins import dream
        if not dream.ENABLED or dream.done_today():
            return
        text = await dream.make_dream()
        if text:
            # 2026-11-06 修：这里原本写的 logger，但 schedule.py 没定义过 logger
            # （全文其它 28 处都用 nonebot.logger）——于是梦生成成功后被 NameError 打死，
            # 只在 apscheduler 里留一条 ERROR，表面上「做梦正常」。实测 11-05 02:15 中招。
            nonebot.logger.info(f"[做梦] 今晚的梦：{text[:40]}…")
    except Exception as e:  # noqa: BLE001
        nonebot.logger.warning(f"[做梦] 失败：{type(e).__name__}: {e}")


@scheduler.scheduled_job("interval", seconds=60, id="dream_force_check",
                         misfire_grace_time=120, coalesce=True)
async def _dream_force_check() -> None:
    """面板「强制入梦」按钮只是写 memory/dream_force.json，这里负责消费它。

    之前这个标记没有任何读者，按钮等于摆设。
    """
    try:
        from plugins import dream
        flag = dream.ROOT / "memory" / "dream_force.json"
        if not flag.exists():
            return
        if dream.done_today():
            flag.unlink(missing_ok=True)
            return
        text = await dream.make_dream()
        if text:
            flag.unlink(missing_ok=True)
            nonebot.logger.info(f"[做梦] 手动补做：{text[:40]}…")
    except Exception as e:  # noqa: BLE001
        nonebot.logger.warning(f"[做梦] 强制入梦失败：{type(e).__name__}: {e}")


@scheduler.scheduled_job("cron", hour=12, minute=0, id="pixiv_daily",
                         misfire_grace_time=3600, coalesce=True)
async def _pixiv_daily() -> None:
    """每天中午挖一轮 Pixiv，挖完同步进表情库 / 生图参考库。"""
    try:
        from plugins import pixiv_duty
        await pixiv_duty.run_daily()
    except Exception as e:  # noqa: BLE001
        nonebot.logger.warning(f"[挖图] 定时失败：{type(e).__name__}: {e}")


@scheduler.scheduled_job("cron", hour=23, minute=30, id="soul_aggregate",
                         misfire_grace_time=86400, coalesce=True)
async def _soul_aggregate() -> None:
    """灵魂聚合（三我提炼）：23:30 从当天强情绪记忆里提炼本我/自我/超我词条。

    原来定在凌晨 3:10，主人睡觉了看不到结果；改到睡前 23:30，
    当天素材刚好攒完，跑完就能在面板看。
    """
    try:
        from server import soul_aggregation as sa
    except Exception:
        try:
            import sys
            sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
            from server import soul_aggregation as sa
        except Exception as e:  # noqa: BLE001
            nonebot.logger.warning(f"[灵魂聚合] 载入失败：{e}")
            return
    try:
        r = await sa.aggregate()
        if r.get("skipped"):
            nonebot.logger.info(f"[灵魂聚合] 跳过：{r.get('reason') or '今天已跑过'}")
        else:
            nonebot.logger.info(f"[灵魂聚合] 成功：+{len(r.get('traits') or [])} 特质 / "
                        f"+{len(r.get('rules') or [])} 准则 / +{len(r.get('defense') or [])} 防御")
    except Exception as e:  # noqa: BLE001
        nonebot.logger.warning(f"[灵魂聚合] 失败：{type(e).__name__}: {e}")


@scheduler.scheduled_job("cron", hour=23, minute=50, id="cache_health",
                         misfire_grace_time=86400, coalesce=True)
async def _cache_health() -> None:
    """每天记一次缓存命中率；低于 70% 告警（多半是频繁重启或前缀被改）。"""
    try:
        import sqlite3
        from pathlib import Path as _P
        import os as _os
        db = _P(_os.environ.get("APPDATA") or str(_P.home())) / "dsh-pet-standalone-webm-chat" / "shared_memory.db"
        con = sqlite3.connect(str(db), timeout=10)
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        row = con.execute(
            "select sum(prompt_tokens), sum(cached_tokens), sum(calls) from token_usage where date=?",
            (today,)).fetchone()
        con.close()
        p_tok, c_tok, calls = (row[0] or 0), (row[1] or 0), (row[2] or 0)
        if p_tok <= 0:
            return
        hit = c_tok / p_tok
        msg = f"[缓存] {today} 命中率 {hit:.1%}（prompt {p_tok} / cached {c_tok} / {calls} 次）"
        if hit < 0.70:
            nonebot.logger.warning(msg + " ← 低于 70%，检查是否频繁重启或稳定前缀被改动")
        else:
            nonebot.logger.info(msg)
    except Exception as e:  # noqa: BLE001
        nonebot.logger.warning(f"[缓存] 健康检查失败：{type(e).__name__}: {e}")
