"""QQ空间同步：说说 / 日记发到空间，带开关状态。"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

try:
    from nonebot.log import logger
except Exception:  # noqa: BLE001
    import logging
    logger = logging.getLogger("qzone_sync")
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "memory" / "qzone_sync.json"
COOKIES = ROOT / "data" / "qzone_cookies.json"
PROFILE = ROOT / os.getenv("QZONE_PROFILE", "data/qzone_profile_bot")


def load_state() -> dict[str, Any]:
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("enabled", False)
            data.setdefault("last_post", "")
            data.setdefault("history", [])
            return data
    except Exception:
        pass
    return {"enabled": False, "last_post": "", "history": []}


def save_state(data: dict[str, Any]) -> None:
    """原子写：先写 .tmp 再 os.replace。面板也在读这个文件，
    直接 write_text 可能被读到半截 JSON（审计 C：dashboard ↔ bot 抢同一文件）。"""
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE)


def set_enabled(flag: bool) -> dict[str, Any]:
    data = load_state()
    data["enabled"] = bool(flag)
    save_state(data)
    return data


async def notify_login_needed(bot, owner_id: int, reason: str = "") -> None:
    """QQ空间登录失效时私聊主人一次，叫他回「扫码」。"""
    data = load_state()
    today = time.strftime("%Y-%m-%d")
    if data.get("last_notify") == today:
        return
    try:
        await bot.send_private_msg(
            user_id=owner_id,
            message=(
                "📮 QQ 空间同步没成功" + (f"：{reason}" if reason else "") + "\n"
                "看起来是登录态过期了。私聊回我一句「扫码」，我就把登录窗口弹出来给你扫。\n"
                "（扫一次能自动登录一段时间，过期了我再提醒你）"
            ),
        )
        data["last_notify"] = today
        save_state(data)
    except Exception:
        pass


def _hash33(skey: str) -> int:
    e = 0
    for ch in skey or "":
        e += (e << 5) + ord(ch)
    return e & 0x7FFFFFFF


def _cookies() -> tuple[dict[str, str], str]:
    raw = json.loads(COOKIES.read_text(encoding="utf-8"))
    jar = {c["name"]: c["value"] for c in raw if c.get("name")}
    uin = jar.get("uin") or jar.get("p_uin") or ""
    uin = uin.lstrip("o")
    return jar, uin


def _gtk(skey: str) -> int:
    h = 5381
    for ch in skey:
        h = ((h << 5) + h + ord(ch)) & 0x7FFFFFFF
    return h


def _publish_by_api(cookies: dict, text: str) -> tuple[bool, str]:
    """用 Qzone H5 发布接口直接发说说，绕过页面输入。

    参数是实测出来的：内容字段必须叫 `con`（叫 content 会返回「您未输入内容」），
    并且要带 hostuin / qzreferrer 等一组字段，否则报「参数错误 -3」。
    """
    import httpx
    skey = cookies.get("p_skey") or cookies.get("skey") or ""
    if not skey:
        return False, "缺少 p_skey"
    uin = _uin_of(cookies) or EXPECT_UIN
    url = ("https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/"
           f"cgi-bin/emotion_cgi_publish_v6?g_tk={_gtk(skey)}")
    data = {
        "con": text,                     # ← 必须是 con，不是 content
        "hostuin": uin,
        "qzreferrer": f"https://user.qzone.qq.com/{uin}/",
        "contype": "1", "feedversion": "1", "ver": "1",
        "syn_tweet_verson": "1", "paramstr": "1", "who": "1",
        "ugc_right": "1", "to_sign": "0", "code_version": "1",
        "format": "json",
    }
    try:
        r = httpx.post(url, data=data, cookies=cookies, timeout=30, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer": f"https://user.qzone.qq.com/{uin}",
            "Origin": "https://user.qzone.qq.com",
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        })
        raw = r.text
        start_i, end_i = raw.find("{"), raw.rfind("}")
        payload = json.loads(raw[start_i:end_i + 1]) if start_i >= 0 else {}
        if payload.get("code") == 0:
            return True, "接口发布成功"
        return False, f"接口返回：{str(payload)[:120]}"
    except Exception as e:  # noqa: BLE001
        return False, f"接口异常：{e}"


def delete_post(cookies: dict, tid: str) -> tuple[bool, str]:
    """删自己空间的一条说说（tid 从 msglist 拿）。"""
    import httpx
    skey = cookies.get("p_skey") or cookies.get("skey") or ""
    uin = _uin_of(cookies) or EXPECT_UIN
    url = ("https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/"
           f"cgi-bin/emotion_cgi_delete_v6?g_tk={_gtk(skey)}")
    try:
        r = httpx.post(url, data={"tid": tid, "hostuin": uin, "format": "json"},
                       cookies=cookies, timeout=25,
                       headers={"User-Agent": "Mozilla/5.0",
                                "Referer": f"https://user.qzone.qq.com/{uin}"})
        raw = r.text
        a, b = raw.find("{"), raw.rfind("}")
        payload = json.loads(raw[a:b + 1]) if a >= 0 else {}
        return payload.get("code") == 0, str(payload)[:100]
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:80]


# 真实 cookie 有两处历史落点：data/qzone_cookies.json（Playwright 导出，列表）
# 和 memory/qzone_cookies.json（本模块自写，dict）。以前只读后者，前者永远读不到。
COOKIE_FILE = ROOT / "memory" / "qzone_cookies.json"
# 2026-10-31 停用回落：memory/qzone_cookies.json 失效被清掉后，这里会立刻读回
# data/ 里 10-24 的老废票，于是每次都「看起来有票 → 发布 -3000 → 清票 → 又读回」死循环。
# 现在只认现役的 memory/qzone_cookies.json（由 qzone_window.py / keepalive 写入）。
COOKIE_FALLBACKS: tuple = ()
# 空间必须用"机器人自己的号"登录；扫成主人大号就会把她的说说到主人空间去
EXPECT_UIN = (os.getenv("QZONE_EXPECT_UIN") or "3577977427").strip()

# t9 凭据保鲜：cookie 超过这个小时数就先跑一次 profile 续期，
# 而不是等发布 -3000 才发现过期（t8：7 天 15 次失败全是登录态下游）。
COOKIE_REFRESH_AFTER_H = float(os.getenv("QZONE_COOKIE_REFRESH_HOURS", "30"))


def _uin_of(cookies: dict) -> str:
    raw = str(cookies.get("uin") or cookies.get("p_uin") or "")
    raw = raw.lstrip("oO").lstrip("0")
    return raw if raw.isdigit() else ""


def _account_ok(cookies: dict) -> tuple[bool, str]:
    uin = _uin_of(cookies)
    if not uin:
        return True, ""   # 拿不到就按旧的走，不因为这个卡住
    if uin != EXPECT_UIN:
        return False, f"登录的是 {uin}，不是机器人 {EXPECT_UIN}（别用主人大号扫）"
    return True, ""


def _load_cookies() -> dict:
    """读上次登录留下的 cookie（uin/skey/p_skey…）。

    兼容两种落盘格式：
      ① {"cookies": {name: value}, "saved_at": ...}   ← 本模块 _save_cookies 写的
      ② [{name, value, domain, ...}, ...]             ← 浏览器/Playwright 导出的列表
    以前只认 ①，列表格式会被当成"没登录"，于是 p_skey 一直取不到、空间永远发不出去。
    """
    path = COOKIE_FILE
    if not path.exists():
        for cand in COOKIE_FALLBACKS:
            if cand.exists():
                path = cand
                break
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    if isinstance(data, dict) and isinstance(data.get("cookies"), dict):
        return data
    if isinstance(data, list):
        jar = {}
        for c in data:
            if isinstance(c, dict) and c.get("name"):
                jar[str(c["name"])] = c.get("value")
        if jar:
            return {"cookies": jar, "saved_at": "（列表格式导出）"}
    if isinstance(data, dict) and data.get("p_skey"):
        # 极少数情况直接在顶层放 cookie 名
        return {"cookies": data, "saved_at": data.get("saved_at") or "（扁平格式）"}
    return {}


def _save_cookies(cookies: dict) -> None:
    try:
        if not cookies:
            return
        COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
        COOKIE_FILE.write_text(json.dumps(
            {"cookies": cookies, "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")},
            ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _drop_cookies() -> None:
    try:
        COOKIE_FILE.unlink(missing_ok=True)
        logger.warning("[空间] 保存的 cookie 失效，已清掉（下次需要扫码）")
    except Exception:  # noqa: BLE001
        pass


def _saved_age_hours(saved_at: str) -> float | None:
    """cookie 落盘时间距今多少小时；格式不认识（老列表导出）返回 None。"""
    s = str(saved_at or "")[:19]
    try:
        ts = time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S"))
    except Exception:  # noqa: BLE001
        return None
    return max(0.0, (time.time() - ts) / 3600.0)


def cookie_status() -> tuple[bool, str]:
    """本地登录态是否可用（不联网、不开浏览器，只看落盘的 cookie）。"""
    saved = _load_cookies()
    ck = saved.get("cookies") or {}
    if not ck:
        return False, "没有保存的登录态"
    ok, why = _account_ok(ck)
    if not ok:
        return False, why
    if not _uin_of(ck):
        return False, "cookie 里没有 uin"
    if not ck.get("p_skey"):
        return False, "cookie 里没有 p_skey（空间接口需要）"
    age = _saved_age_hours(str(saved.get("saved_at") or ""))
    if age is None:
        return True, f"登录态结构完整（{len(ck)} 个 cookie）"
    return True, f"登录态可用（{len(ck)} 个 cookie，{age:.1f} 小时前刷新）"


# 前置续期的最小间隔：登录态一直坏的时候，别每 10 分钟就开一次浏览器
_PREFLIGHT_REFRESH_GAP = 1800.0
_last_preflight_refresh = 0.0


async def preflight(refresh: bool = True) -> tuple[bool, str]:
    """发布前置校验（B）：发说说前先确认登录态可用。

    · 本地票缺失/结构坏了/超过 COOKIE_REFRESH_AFTER_H 小时 → 先尝试一次
      profile 续期（有扫码窗口开着就不抢 profile，直接等主人扫）
    · 返回 False 时调用方应该直接提醒主人「扫码」，不要先生成文案（白烧 LLM）
    """
    global _last_preflight_refresh
    ok, why = cookie_status()
    age = _saved_age_hours(str((_load_cookies() or {}).get("saved_at") or ""))
    fresh = age is not None and age <= COOKIE_REFRESH_AFTER_H
    if ok and fresh:
        return True, why
    if not refresh:
        return False, why
    if _login_window_open():
        return False, "扫码窗口还开着，先去窗口里完成登录"
    if _QZONE_LOCK.locked():
        return False, "上一轮空间任务还在跑，稍后再试"
    if time.time() - _last_preflight_refresh < _PREFLIGHT_REFRESH_GAP:
        return False, why
    _last_preflight_refresh = time.time()
    logger.info(f"[空间] 前置校验：{why}，先尝试用 profile 续期一次")
    ka_ok, ka_why = await keepalive()
    if ka_ok:
        return cookie_status()
    logger.warning(f"[空间] 前置续期失败：{ka_why}")
    return False, f"登录态失效：{ka_why}"


_QZONE_LOCK = asyncio.Lock()


async def keepalive() -> tuple[bool, str]:
    """用持久化 profile 打开空间主页，登录态还在就把 cookie 续期保存。

    登录态保活：只要 profile 的会话没死，跑一次就能导出新 cookie，
    发说说时不用等它过期才炸。返回 (是否还活着, 说明)。

    安全约束（t8 的 P2）：可见扫码窗口正开着时**绝不能**再开无头浏览器抢同一个
    profile —— 抢占会让窗口里的会话失效，随后 cookie 被清掉，把主人刚登录成功的票弄丢。
    """
    if _login_window_open():
        return False, "扫码窗口正开着，不抢 profile（等窗口完成登录）"
    if _QZONE_LOCK.locked():
        return False, "上一轮空间任务还在跑，跳过保活"
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            ctx = await pw.chromium.launch_persistent_context(
                str(PROFILE), channel="msedge", headless=True,
                viewport={"width": 1280, "height": 900},
            )
            try:
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                await page.goto("https://user.qzone.qq.com/", timeout=60000)
                await page.wait_for_timeout(5000)
                # 不以 URL 判断：空间页经常被重定向回 i.qq.com，但只要 cookie 里有
                # uin + p_skey，接口就还能用（2026-10-30 实测：profile 有票却被重定向，
                # 按 URL 判断会把好登录态误判成失效）。
                cookies = {c["name"]: c["value"] for c in await ctx.cookies()}
                ok, why = _account_ok(cookies)
                if not ok:
                    return False, why
                if not cookies.get("p_skey"):
                    return False, "拿不到 p_skey（需要重新扫码）"
                _save_cookies(cookies)
                return True, f"登录态已刷新（{len(cookies)} 个 cookie，url={page.url[:40]}）"
            finally:
                await ctx.close()
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


async def publish_qzone(text: str) -> tuple[bool, str]:
    """同一时间只允许一个空间发布，避免浏览器 profile 互踢。"""
    if _QZONE_LOCK.locked():
        return False, "上一轮空间发布还没结束"
    try:
        from plugins import moderation
        text = moderation.filter_public(text, channel="qzone")
        if not text:
            return False, "内容没过审核，没发"
    except Exception:  # noqa: BLE001
        pass
    async with _QZONE_LOCK:
        return await _publish_qzone_inner(text)


def _login_window_pids() -> list[int]:
    """真正在跑 qzone_window.py 的 python 进程（用 psutil，不再调 PowerShell）。

    之前用 PowerShell 查，命令自身的命令行里含 'qzone_window'，
    结果每次都把 PowerShell 自己算成"扫码窗口"，形成"永远关不掉"的死循环。
    """
    pids: list[int] = []
    try:
        import psutil
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                if proc.pid == os.getpid():
                    continue
                name = (proc.info.get("name") or "").lower()
                if "python" not in name:
                    continue
                cmd = " ".join(proc.info.get("cmdline") or [])
                if "qzone_window.py" in cmd:
                    pids.append(proc.pid)
            except Exception:  # noqa: BLE001
                continue
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[空间] 枚举扫码窗口失败：{e}")
    return pids


def _kill_login_window() -> int:
    """把残留的扫码窗口关掉（登录态已存盘，不需要一直开着）。返回关掉的数量。"""
    killed = 0
    for pid in _login_window_pids():
        try:
            import subprocess
            r = subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                               capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                killed += 1
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[空间] 关闭 {pid} 失败：{e}")
    if killed:
        logger.info(f"[空间] 已自动关闭残留扫码窗口 {killed} 个")
    return killed


def cleanup_orphan_windows() -> int:
    """启动时清理上次遗留的扫码窗口（它们占着登录目录，会让发布失败）。"""
    n = 0
    try:
        import psutil
        for proc in psutil.process_iter(["pid", "cmdline"]):
            cmd = " ".join(proc.info.get("cmdline") or [])
            if "qzone_window.py" in cmd and "python" in (proc.info.get("name") or "").lower() and proc.pid != os.getpid():
                try:
                    proc.kill()
                    n += 1
                except Exception:  # noqa: BLE001
                    pass
        if n:
            logger.info(f"[空间] 启动清理：关掉遗留扫码窗口 {n} 个")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[空间] 启动清理失败：{e}")
    return n


def _login_window_open() -> bool:
    """扫码窗口是不是开着（同一个 profile 不能被两个浏览器同时用）。"""
    return bool(_login_window_pids())


async def _wait_window_cookies(timeout: float = 30.0) -> tuple[bool, str]:
    """扫码窗口开着时，等它把登录态落盘（最多 timeout 秒）。

    以前这里直接调 keepalive()，会用同一个 profile 起第二个浏览器和可见窗口互踢，
    结果「拿不到 p_skey」→ 把刚登录好的票清掉（t8 的 P2-1）。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        await asyncio.sleep(2.0)
        ck = (_load_cookies() or {}).get("cookies") or {}
        if ck.get("p_skey") and _uin_of(ck):
            return True, "扫码窗口刚保存了登录态"
        if not _login_window_open():
            break
    return False, "扫码窗口还没保存登录态"


async def _publish_qzone_inner(text: str) -> tuple[bool, str]:
    """用已登录的 QQ 空间页面发一条说说（快捷登录 + 接口优先）。"""
    text = (text or "").strip()
    if not text:
        return False, "内容为空"
    if len(text) > 800:
        text = text[:800]
    login_name = os.getenv("QZONE_LOGIN_NAME", "蓝色大肥鱼")
    # 先用手上已存的 cookie 直接发（这是"不用每次扫码"的关键一步）
    saved = _load_cookies()
    if saved.get("cookies"):
        _ok_acc, _why = _account_ok(saved["cookies"])
        if not _ok_acc:
            logger.warning(f"[空间] 保存的登录态账号不对：{_why}；已清掉，等下重新扫码")
            _drop_cookies()
            saved = {}
        else:
            ok_api, msg_api = _publish_by_api(saved["cookies"], text)
            if ok_api:
                state = load_state()
                state["last_post"] = time.strftime("%Y-%m-%d %H:%M:%S")
                state["history"] = (state.get("history") or [])[-49:] + [
                    {"ts": state["last_post"], "text": text[:120]}
                ]
                save_state(state)
                logger.info("[空间] 用已保存的 cookie 直接发布成功（未启动浏览器）")
                return True, "已发到 QQ 空间（复用登录态）"
            # 扫码工具写盘的瞬间可能读到半截 cookie（缺 p_skey）→ 等 2 秒重读再试一次
            if any(k in str(msg_api) for k in ("缺少 p_skey", "参数错误", "缺少")):
                await asyncio.sleep(2.0)
                _s2 = _load_cookies()
                _ck2 = (_s2 or {}).get("cookies") or {}
                if _ck2:
                    ok2, msg2 = _publish_by_api(_ck2, text)
                    if ok2:
                        _st = load_state()
                        _st["last_post"] = time.strftime("%Y-%m-%d %H:%M:%S")
                        _st["history"] = (_st.get("history") or [])[-49:] + [
                            {"ts": _st["last_post"], "text": text[:120]}
                        ]
                        save_state(_st)
                        logger.info("[空间] 重读 cookie 后发布成功")
                        return True, "已发到 QQ 空间（复用登录态）"
                    msg_api = msg2
            logger.warning(f"[空间] 保存的 cookie 发布失败：{msg_api}；尝试自愈一次")
            if any(k in str(msg_api) for k in ("登录", "skey", "-3000", "1003")):
                # 扫码窗口开着时不能再用无头浏览器抢同一个 profile：会互踢，
                # 反而把刚登录好的票清掉。等窗口自己保存登录态，再重试一次。
                if _login_window_open():
                    _ka_ok, _ka_why = await _wait_window_cookies(30.0)
                else:
                    _ka_ok, _ka_why = await keepalive()
                if _ka_ok:
                    _s3 = _load_cookies()
                    _ck3 = (_s3 or {}).get("cookies") or {}
                    if _ck3:
                        ok3, msg3 = _publish_by_api(_ck3, text)
                        if ok3:
                            _st3 = load_state()
                            _st3["last_post"] = time.strftime("%Y-%m-%d %H:%M:%S")
                            _st3["history"] = (_st3.get("history") or [])[-49:] + [
                                {"ts": _st3["last_post"], "text": text[:120]}
                            ]
                            save_state(_st3)
                            logger.info("[空间] 续期后发布成功")
                            return True, "已发到 QQ 空间（续期后）"
                        logger.warning(f"[空间] 续期后仍失败：{msg3}")
                else:
                    logger.warning(f"[空间] 自愈续期失败：{_ka_why}；继续用无头浏览器试一次快捷登录")
                # 窗口开着说明主人正在登录 —— 这时候清票会把"马上要成功"的登录态弄丢
                if _login_window_open():
                    logger.info("[空间] 扫码窗口开着，先不清 cookie（等窗口完成登录）")
                else:
                    _drop_cookies()
            else:
                logger.info("[空间] 失败原因不是登录态，先保留 cookie")
    if _login_window_open():
        # 主人正在可见窗口里登录：等它存完 cookie，别自己再开无头浏览器抢同一个 profile
        for _ in range(24):          # 最多等 2 分钟
            await asyncio.sleep(5.0)
            _c = (_load_cookies() or {}).get("cookies") or {}
            if _c.get("p_skey"):
                ok3, msg3 = _publish_by_api(_c, text)
                if ok3:
                    _st3 = load_state()
                    _st3["last_post"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    _st3["history"] = (_st3.get("history") or [])[-49:] + [
                        {"ts": _st3["last_post"], "text": text[:120]}
                    ]
                    save_state(_st3)
                    return True, "已发到 QQ 空间（登录窗口刚完成登录）"
            if not _login_window_open():
                break
        return False, "登录窗口还开着/还没登录成功，先在里面登录再发"
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            ctx = await pw.chromium.launch_persistent_context(
                str(PROFILE), channel="msedge", headless=True,
                viewport={"width": 1280, "height": 900},
            )
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto("https://i.qq.com/", timeout=60000)
            await page.wait_for_timeout(6000)
            if "user.qzone.qq.com" not in page.url:
                for frame in page.frames:
                    if "ptlogin2" in frame.url:
                        try:
                            await frame.get_by_text(login_name, exact=False).first.click(timeout=5000)
                            break
                        except Exception:
                            pass
                await page.wait_for_timeout(8000)
            if "user.qzone.qq.com" not in page.url:
                await ctx.close()
                return False, "QQ空间登录态失效，快捷登录也没成功——私聊我发「扫码」登录一次"
            cookies = {c["name"]: c["value"] for c in await ctx.cookies()}
            _ok_acc, _why = _account_ok(cookies)
            if _ok_acc:
                _save_cookies(cookies)   # 存下来，下次不用再开浏览器
            else:
                logger.warning(f"[空间] 不保存登录态：{_why}")
            ok_api, msg_api = _publish_by_api(cookies, text)
            if ok_api:
                await ctx.close()
                state = load_state()
                state["last_post"] = time.strftime("%Y-%m-%d %H:%M:%S")
                state["history"] = (state.get("history") or [])[-49:] + [
                    {"ts": state["last_post"], "text": text[:120]}
                ]
                save_state(state)
                return True, "已发到 QQ 空间（接口）"
            try:
                # 新版空间：先点“说点儿什么吧”，再把文字写进可见的 c_tx2 编辑器
                try:
                    await page.get_by_text("说点儿什么吧", exact=False).first.click(force=True, timeout=6000)
                    await page.wait_for_timeout(600)
                except Exception:
                    pass
                editor = page.locator("div.c_tx2:visible").first
                if await editor.count() == 0:
                    editor = page.locator("#qz_poster_editor_v4_container").first
                await editor.click(timeout=8000)
                await page.wait_for_timeout(600)
                await page.keyboard.insert_text(text)
                await page.wait_for_timeout(800)
            except Exception as e:  # noqa: BLE001
                await ctx.close()
                return False, f"找不到说说输入框：{e}"
            await page.wait_for_timeout(800)
            try:
                if await page.locator("a.btn-post:visible").count():
                    await page.locator("a.btn-post:visible").first.click(force=True, timeout=8000)
                else:
                    await page.get_by_text("发表", exact=True).last.click(force=True, timeout=8000)
            except Exception as e:  # noqa: BLE001
                await ctx.close()
                return False, f"找不到发表按钮：{e}"
            await page.wait_for_timeout(8000)
            try:
                await page.goto("https://user.qzone.qq.com/3577977427", timeout=60000)
                await page.wait_for_timeout(6000)
            except Exception:
                pass
            body = await page.inner_text("body")
            ok = text[:12] in body
            await ctx.close()
            if not ok:
                return False, "点击发表后没在页面看到内容"
            state = load_state()
            state["last_post"] = time.strftime("%Y-%m-%d %H:%M:%S")
            state["history"] = (state.get("history") or [])[-49:] + [
                {"ts": state["last_post"], "text": text[:120]}
            ]
            save_state(state)
            return True, "已发到 QQ 空间"
    except Exception as e:  # noqa: BLE001
        return False, f"发布失败：{e}"
