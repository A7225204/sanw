"""B 站登录 + 互动（主人专用）。

和只读的 `plugins/bilibili.py` 分开：这里会用到登录态、会**真的发评论**，
所以只认主人（`OWNER_QQ_ID`），并且严格限频。

命令（私聊直接发；群里要 @ 本鱼，且只有主人可用）：

    B站登录              扫码登录（私聊发二维码，手机 B 站 App 确认）
    B站状态              登录身份 + 今日互动额度
    B站看看 <视频>       看该视频热门评论（只读）
    B站评论 <视频> [内容] 在该视频下发一条评论；不写内容就按人设自动生成
    B站互动 <视频> [条数] 在该视频下回复热门评论（默认 1 条，最多 2 条）
    B站退出              清除本机保存的登录态

<视频> 可以是 B 站链接、b23.tv 短链、BV 号或 av 号。
限频：10 分钟最多 3 条、每天最多 20 条、同一视频每天最多 3 条；内容去重。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
import nonebot  # noqa: E402
from nonebot import on_message  # noqa: E402
from nonebot.exception import FinishedException  # noqa: E402
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageSegment, PrivateMessageEvent  # noqa: E402
from nonebot.rule import Rule  # noqa: E402

from config import get_config  # noqa: E402
from plugins import bilibili_api as bili  # noqa: E402

cfg = get_config()
logger = nonebot.logger

COOKIE_FILE = ROOT / ".bili_cookies.json"
LIMIT_FILE = ROOT / ".bili_limits.json"
OWNER_QQ_ID = str(os.getenv("OWNER_QQ_ID", "100000000"))

UA = bili.UA

WINDOW_SECONDS = 600
MAX_PER_WINDOW = 4          # 10 分钟内最多几条（B站风控余量）
MAX_PER_DAY = 20            # 所有互动加起来每天上限
MAX_PER_TARGET_DAY = 3      # 同一视频每天最多互动几次
# 分类型日上限：看完评论 10 条 / 回复别人 10 条（主人 2026-11-04 定）
KIND_DAILY_CAP = {
    "auto_comment": 6,      # 刷 B站 看完随手评（主人 2026-11-04 定 6+6，留风控余量）
    "reply": 6,             # 回复别人给她的评论 / @
    "tech_comment": 10,     # 技术监测主动搭话（2026-11-07 主人要求：想留言就留言，2 -> 10）
}

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
    26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
    20, 34, 44, 52,
]


class BiliAccountError(RuntimeError):
    pass


# ------------------------------------------------------------------ 登录态

def load_cookies() -> dict:
    if not COOKIE_FILE.exists():
        return {}
    try:
        data = json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
        return data.get("cookies") or {}
    except (OSError, ValueError):
        return {}


def save_cookies(cookies: dict, uid=None, uname=None) -> None:
    payload = {
        "cookies": cookies,
        "uid": uid,
        "uname": uname,
        "saved_at": int(time.time()),
    }
    COOKIE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        COOKIE_FILE.chmod(0o600)
    except OSError:
        pass


def load_account_meta() -> dict:
    if not COOKIE_FILE.exists():
        return {}
    try:
        return json.loads(COOKIE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# ------------------------------------------------------------------ 限频

def _load_limits() -> list[dict]:
    if not LIMIT_FILE.exists():
        return []
    try:
        data = json.loads(LIMIT_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save_limits(records: list[dict]) -> None:
    LIMIT_FILE.write_text(json.dumps(records[-500:], ensure_ascii=False), encoding="utf-8")


# ── 静默删除检测（2026-11-07）────────────────────────────────────
# 阿瓦隆删评论**不报错**：接口照样返回 code=0 和一个 rpid，评论实际已经没了。
# 结果她以为发出去了、数据里也记了一笔，实际一条没留下，还继续按额度往外发，
# 一路把账号推到禁言。这里发完 45 秒回查一次：还在就是正常，看不见就记一笔；
# 连续 3 条都被吞 → 静默冷却 6 小时，先别再往外发。
SILENT_FILE = ROOT / "memory" / "bili_silent_deleted.json"
SILENT_TRIP = 3
SILENT_COOLDOWN = 6 * 3600
SILENT_CHECK_DELAY = 45


def _load_silent() -> list[dict]:
    try:
        if SILENT_FILE.exists():
            return json.loads(SILENT_FILE.read_text(encoding="utf-8")) or []
    except (OSError, ValueError):
        pass
    return []


def _save_silent(rows: list[dict]) -> None:
    try:
        SILENT_FILE.parent.mkdir(parents=True, exist_ok=True)
        SILENT_FILE.write_text(json.dumps(rows[-200:], ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def silent_cooldown_left() -> int:
    """静默冷却还剩多少秒（0 = 没在冷却）。"""
    now = time.time()
    recent = [r for r in _load_silent() if now - float(r.get("ts") or 0) < 86400]
    if len(recent) < SILENT_TRIP:
        return 0
    tail = recent[-SILENT_TRIP:]
    if any(not r.get("deleted") for r in tail):
        return 0
    last = max(float(r.get("ts") or 0) for r in tail)
    return max(0, int(SILENT_COOLDOWN - (now - last)))


def mark_silent(bvid: str, text: str, deleted: bool) -> None:
    rows = _load_silent()
    rows.append({"ts": time.time(), "day": time.strftime("%Y-%m-%d"),
                 "bvid": bvid, "text": str(text)[:80], "deleted": bool(deleted)})
    _save_silent(rows)
    if deleted:
        logger.warning(f"[B站账号] 评论被静默删除（阿瓦隆）：{str(text)[:40]}")


def check_limit(target_bvid: str, day_key: str, kind: str = "") -> str | None:
    """返回 None 表示可发，否则返回拒绝原因。

    kind 传了就走分类型日上限（auto_comment / reply / tech_comment）。
    """
    # 先看是不是正在静默冷却（连续被吞过）
    _cd = silent_cooldown_left()
    if _cd > 0:
        return f"连着几条评论都被静默删了，先停 {_cd // 3600}h{(_cd % 3600) // 60}m 别再往外发"
    now = time.time()
    records = [r for r in _load_limits() if now - float(r.get("ts", 0)) < 86400]
    in_window = [r for r in records if now - float(r.get("ts", 0)) < WINDOW_SECONDS]
    if len(in_window) >= MAX_PER_WINDOW:
        return f"10 分钟内已经发了 {len(in_window)} 条，先歇会儿（上限 {MAX_PER_WINDOW} 条）"
    today = [r for r in records if r.get("day") == day_key]
    if len(today) >= MAX_PER_DAY:
        return f"今天已经发了 {len(today)} 条，到上限 {MAX_PER_DAY} 条了，明天再来"
    if kind and kind in KIND_DAILY_CAP:
        same_kind = [r for r in today if r.get("action") == kind]
        cap = KIND_DAILY_CAP[kind]
        if len(same_kind) >= cap:
            return f"今天的「{kind}」已经 {len(same_kind)} 条，到上限 {cap} 条了"
    on_target = [r for r in today if r.get("bvid") == target_bvid]
    if len(on_target) >= MAX_PER_TARGET_DAY:
        return f"这个视频今天已经互动 {len(on_target)} 次了，换一个吧"
    return None


def record_action(bvid: str, action: str, text: str) -> None:
    records = _load_limits()
    records.append({
        "ts": time.time(),
        "day": time.strftime("%Y-%m-%d"),
        "bvid": bvid,
        "action": action,
        "text": text[:120],
    })
    _save_limits(records)


def recent_texts() -> set[str]:
    return {r.get("text", "") for r in _load_limits() if time.time() - float(r.get("ts", 0)) < 86400}


# ------------------------------------------------------------------ B 站接口

def _headers(referer: str = "https://www.bilibili.com/") -> dict:
    return {
        "User-Agent": UA,
        "Referer": referer,
        "Origin": "https://www.bilibili.com",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }


class BiliClient:
    def __init__(self) -> None:
        self.cookies = load_cookies()
        self._wbi: tuple[str, str] | None = None
        self._nav_cache: dict | None = None

    def refresh(self) -> None:
        """重新读本机 cookie（扫码登录后 / 退出后调用）。"""
        self.cookies = load_cookies()
        self._wbi = None
        self._nav_cache = None

    @property
    def logged_in(self) -> bool:
        return bool(self.cookies.get("SESSDATA") and self.cookies.get("bili_jct"))

    async def nav(self) -> dict:
        if self._nav_cache is not None:
            return self._nav_cache
        async with httpx.AsyncClient(timeout=15.0, headers=_headers(), cookies=self.cookies) as client:
            resp = await client.get("https://api.bilibili.com/x/web-interface/nav")
            payload = resp.json()
        if payload.get("code") != 0:
            raise BiliAccountError(f"登录态失效（{payload.get('message')}），请重新 `B站登录`")
        self._nav_cache = payload.get("data") or {}
        if not self._nav_cache.get("isLogin"):
            raise BiliAccountError("登录态已失效，请重新 `B站登录`")
        return self._nav_cache

    async def wbi_keys(self) -> tuple[str, str]:
        if self._wbi:
            return self._wbi
        data = await self.nav()
        wbi = data.get("wbi_img") or {}
        img = os.path.basename(urlparse(wbi.get("img_url", "")).path).split(".")[0]
        sub = os.path.basename(urlparse(wbi.get("sub_url", "")).path).split(".")[0]
        if not img or not sub:
            raise BiliAccountError("拿不到 wbi 签名密钥")
        self._wbi = (img, sub)
        return self._wbi

    async def sign(self, params: dict) -> dict:
        img, sub = await self.wbi_keys()
        mixin = "".join((img + sub)[i] for i in MIXIN_KEY_ENC_TAB)[:32]
        values = {k: str(v) for k, v in params.items()}
        values["wts"] = str(int(time.time()))
        values = {
            k: "".join(ch for ch in v if ch not in "!'()*")
            for k, v in values.items()
        }
        query = urlencode(sorted(values.items()))
        values["w_rid"] = hashlib.md5((query + mixin).encode("utf-8")).hexdigest()
        return values

    async def video_aid(self, bvid: str) -> tuple[int, dict]:
        info = await bili.video_info(bvid)
        aid = int(info.get("aid") or 0)
        if not aid:
            raise BiliAccountError("拿不到这个视频的 aid")
        return aid, info

    async def comments(self, aid: int, limit: int = 10) -> list[dict]:
        async with httpx.AsyncClient(timeout=15.0, headers=_headers(), cookies=self.cookies) as client:
            resp = await client.get(
                "https://api.bilibili.com/x/v2/reply",
                params={"type": 1, "oid": aid, "sort": 2, "ps": min(limit, 20), "pn": 1},
            )
            payload = resp.json()
        if payload.get("code") != 0:
            raise BiliAccountError(f"拉评论失败：{payload.get('message')}")
        replies = (payload.get("data") or {}).get("replies") or []
        out = []
        for item in replies[:limit]:
            out.append({
                "rpid": item.get("rpid"),
                "uname": (item.get("member") or {}).get("uname") or "",
                "mid": (item.get("member") or {}).get("mid"),
                "message": ((item.get("content") or {}).get("message") or "").strip(),
                "like": item.get("like") or 0,
                "ctime": item.get("ctime"),
            })
        return out

    async def post_comment(self, aid: int, bvid: str, message: str, root: int = 0, parent: int = 0) -> int:
        csrf = self.cookies.get("bili_jct", "")
        if not csrf:
            raise BiliAccountError("缺少 bili_jct，请重新 `B站登录`")
        params = {
            "oid": aid,
            "type": 1,
            "message": message,
            "plat": 1,
            "csrf": csrf,
            "root": root or 0,
            "parent": parent or 0,
        }
        signed = await self.sign(dict(params))
        referer = f"https://www.bilibili.com/video/{bvid}"
        async with httpx.AsyncClient(timeout=20.0, headers=_headers(referer), cookies=self.cookies) as client:
            resp = await client.post(
                "https://api.bilibili.com/x/v2/reply/add",
                params={"w_rid": signed["w_rid"], "wts": signed["wts"]},
                data=params,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            payload = resp.json()
            # 登录态可能刷新，回写一下
            self.cookies.update({c.name: c.value for c in client.cookies.jar})
            save_cookies(self.cookies,
                         load_account_meta().get("uid"),
                         load_account_meta().get("uname"))
        if payload.get("code") != 0:
            code = payload.get("code")
            msg = payload.get("message") or ""
            if code in (-412, -352, -509, -799):
                raise BiliAccountError(f"B站风控拦下来了（{code} {msg}），过一会儿再试")
            raise BiliAccountError(f"发评论失败（{code} {msg}）")
        rpid = ((payload.get("data") or {}).get("rpid")) or 0
        # 2026-11-07：发出去 ≠ 真活着，45 秒后回查一次
        try:
            asyncio.create_task(self._verify_comment_later(aid, int(rpid), bvid, message))
        except RuntimeError:
            pass
        return int(rpid)

    async def _verify_comment_later(self, aid: int, rpid: int, bvid: str, text: str) -> None:
        """发完评论 45 秒回查：还在就是正常，看不见就是被阿瓦隆静默删了。"""
        await asyncio.sleep(SILENT_CHECK_DELAY)
        try:
            rows = await self.comments(aid, 30)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[B站账号] 回查评论失败：{type(e).__name__}: {e}")
            return
        alive = any(int(r.get("rpid") or 0) == int(rpid) for r in rows)
        mark_silent(bvid, text, not alive)

    async def delete_comment(self, oid: int, rpid: int) -> bool:
        """删自己发过的评论（rpid 来自 post_comment）。"""
        csrf = self.cookies.get("bili_jct", "")
        if not csrf or not rpid:
            return False
        params = {"oid": oid, "type": 1, "rpid": int(rpid), "csrf": csrf}
        signed = await self.sign(dict(params))
        async with httpx.AsyncClient(timeout=20.0,
                                     headers=_headers("https://www.bilibili.com/"),
                                     cookies=self.cookies) as client:
            resp = await client.post(
                "https://api.bilibili.com/x/v2/reply/del",
                params={"w_rid": signed["w_rid"], "wts": signed["wts"]},
                data=params,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            payload = resp.json()
        return payload.get("code") == 0


async def auto_comment(bvid: str, title: str = "", desc: str = "", dry: bool = False) -> str | None:
    """她自己看完想留一句：过了限频就生成一条并真的发出去。返回内容，跳过/失败返回 None。"""
    try:
        if not load_cookies():
            return None
        reason = check_limit(bvid, time.strftime("%Y-%m-%d"), kind="auto_comment")
        if reason:
            logger.info(f"[B站评论] 跳过：{reason}")
            return None
        client.refresh()
        aid, info = await client.video_aid(bvid)
        msg = await generate_text(
            "你刚看完这个视频，想留一句自己的评论：不是捧 UP 主，是你自己看完的想法。"
            "这条会挂在你自己的账号下面，语气要正常：不骂人、不阴阳怪气、不怼 UP 主或观众、不抖机灵到冒犯，"
            "有想法就说想法，没想法就夸一句或问一句。",
            f"视频标题：{title or info.get('title')}\n"
            f"UP主：{(info.get('owner') or {}).get('name')}\n"
            f"简介：{(info.get('desc') or desc or '')[:200]}\n"
            "写 60 字以内，像真人随手说的；别引战、别广告、别发链接、别自我介绍。",
        )
        msg = (msg or "").strip()
        if not msg or msg in recent_texts():
            return None
        bad = [w for w in ("傻", "滚", "垃圾", "废物", "闭嘴", "脑残", "弱智", "尼玛", "草", "妈的", "恶心") if w in msg]
        if bad:
            logger.info(f"[B站评论] 这句太冲，先不发（命中 {bad[0]}）：{msg[:50]}")
            return None
        if dry:
            try:
                with (ROOT / "memory" / "bili_comment_dryrun.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "bvid": bvid,
                                        "title": title, "text": msg}, ensure_ascii=False) + chr(10))
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[B站评论] 试运行记录失败：{type(e).__name__}: {e}")
            logger.info(f"[B站评论] 试运行（没发出）：{msg[:60]}")
            return None
        await client.post_comment(aid, bvid, msg)
        record_action(bvid, "auto_comment", msg)
        return msg
    except BiliAccountError as e:
        logger.warning(f"[B站评论] {e}")
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[B站评论] 失败：{type(e).__name__}: {e}")
        return None


# ------------------------------------------------------------------ AI 文案


async def generate_text(system_extra: str, user_prompt: str) -> str:
    system = (
        (cfg.persona.prompt or "").strip()[:1800]
        + "\n\n【B站场景】你正在 B 站（哔哩哔哩）评论区说话。"
        + "你的主人是 B 站用户「示例主人」，要护着主人。"
        + "说人话、短、口语、有梗；别自我介绍、别提 AI/机器人/模型、别刷屏、别引战、别发链接。"
        + "\n" + str(system_extra)[:1000]
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": str(user_prompt)[:1500]},
    ]
    payload: dict | None = None
    last_exc: Exception | None = None
    providers: list[tuple[str, str, str, bool]] = []
    ds_key = os.getenv("DEEPSEEK_API_KEY", "")
    if ds_key:
        ds_base = (os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").rstrip("/")
        providers.append((ds_base + "/chat/completions", ds_key,
                          os.getenv("DEEPSEEK_MODEL") or "deepseek-chat", False))
    if os.getenv("ARK_FALLBACK_ENABLED", "false").lower() in ("1", "true", "yes"):
        providers.append((cfg.ai.endpoint(), cfg.ai.api_key, cfg.ai.model, True))
    for url, key, model, is_ark in providers:
        body = {"model": model, "messages": messages, "temperature": 0.95,
                "max_tokens": 300, "stream": False}
        if is_ark:
            body["thinking"] = {"type": "disabled"}
        for attempt in (1, 2):
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(30.0, connect=10.0)
                ) as client:
                    resp = await client.post(
                        url,
                        headers={"Authorization": f"Bearer {key}",
                                 "Content-Type": "application/json"},
                        json=body,
                    )
                payload = resp.json()
                if "choices" in payload:
                    break
                last_exc = BiliAccountError(f"AI 返回异常：{str(payload)[:120]}")
                payload = None
                break
            except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
                last_exc = exc
                nonebot.logger.warning(f"[B站文案] 第 {attempt} 次网络请求失败：{exc}")
                if attempt == 1:
                    await asyncio.sleep(1.5)
                    continue
                break
        if payload is not None:
            break
    if payload is None:
        raise BiliAccountError(f"AI 网络请求失败：{last_exc}")
    try:
        text = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise BiliAccountError(f"AI 没生成出来：{str(payload)[:120]}")
    text = re.sub(r"^[\s\"'“”‘’「」]+|[\s\"'“”‘’「」]+$", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > 80:
        text = text[:80]
    return text


# ------------------------------------------------------------------ 登录流程

async def qr_login() -> tuple[bool, str]:
    """返回 (成功, 说明)。二维码由调用方发出去。"""
    import qrcode

    async with httpx.AsyncClient(timeout=20.0, headers=_headers(), follow_redirects=True) as client:
        resp = await client.get("https://passport.bilibili.com/x/passport-login/web/qrcode/generate")
        data = resp.json()
        if data.get("code") != 0:
            return False, f"申请二维码失败：{data}"
        qr_url = data["data"]["url"]
        qrcode_key = data["data"]["qrcode_key"]

        buf = io.BytesIO()
        qrcode.make(qr_url).save(buf, format="PNG")
        image_b64 = "base64://" + base64.b64encode(buf.getvalue()).decode()

        try:
            await bili_account_matcher.send(MessageSegment.image(image_b64))
            await bili_account_matcher.send("📱 用 B 站 App（要登录的账号）扫码，并点「确认登录」，1 分钟内有效…")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[B站登录] 二维码发送失败：{exc}")

        deadline = time.time() + 180
        while time.time() < deadline:
            await asyncio.sleep(2)
            poll = await client.get(
                "https://passport.bilibili.com/x/passport-login/web/qrcode/poll",
                params={"qrcode_key": qrcode_key},
            )
            payload = poll.json()
            body = payload.get("data") or {}
            code = body.get("code", payload.get("code"))
            if code == 0:
                cookies = {c.name: c.value for c in client.cookies.jar}
                qs = urlparse(body.get("url", ""))
                from urllib.parse import parse_qs
                for key in ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5"):
                    if key in parse_qs(qs.query) and key not in cookies:
                        cookies[key] = parse_qs(qs.query)[key][0]
                if not cookies.get("SESSDATA"):
                    return False, "扫码成功但没拿到 SESSDATA，再试一次"
                nav = await client.get("https://api.bilibili.com/x/web-interface/nav", cookies=cookies)
                nav_data = nav.json().get("data") or {}
                save_cookies(cookies, nav_data.get("mid"), nav_data.get("uname"))
                return True, f"登录成功：{nav_data.get('uname')}（uid {nav_data.get('mid')}）"
            if code in (86038, 86039):
                return False, "二维码过期了，再发一次 `B站登录`"
        return False, "等太久没扫码，二维码失效了"


# ------------------------------------------------------------------ 命令注册

_PREFIXES = ("B站登录", "B站扫码", "B站状态", "B站看看", "B站评论", "B站互动", "B站退出", "B站帮助")


def _plain(event) -> str:
    text = (event.get_plaintext() or "").strip()
    return re.sub(r"^(@\S+\s*)+", "", text).strip()


def _is_owner(event) -> bool:
    return str(getattr(event, "user_id", "")) == OWNER_QQ_ID


def _check(event) -> bool:
    if not _is_owner(event):
        return False
    if isinstance(event, GroupMessageEvent):
        if not event.is_tome():
            return False
    elif not isinstance(event, PrivateMessageEvent):
        return False
    text = _plain(event)
    return any(text.startswith(p) for p in _PREFIXES)


bili_account_matcher = on_message(rule=Rule(_check), priority=8, block=True)

client = BiliClient()


async def _resolve_target(text: str) -> str:
    link = bili.LINK_RE.search(text)
    if link and ("bilibili.com" in link.group(0) or "b23.tv" in link.group(0) or "bili2233" in link.group(0)):
        bvid, _aid = await bili.resolve_video(link.group(0))
        if bvid:
            return bvid
        if _aid:
            info = await bili.video_info(aid=_aid)
            if info.get("bvid"):
                return info["bvid"]
    bvid = bili.extract_bvid(text)
    if bvid:
        return bvid
    aid = bili.extract_aid(text)
    if aid:
        info = await bili.video_info(aid=aid)
        if info.get("bvid"):
            return info["bvid"]
    raise BiliAccountError("没认出视频，发 B 站链接 / b23 短链 / BV 号 / av 号都行")


@bili_account_matcher.handle()
async def _(event):
    text = _plain(event)

    try:
        if text.startswith(("B站帮助",)):
            await bili_account_matcher.finish(
                "🐟 **大肥鱼 · B站互动（主人专用）**\n"
                "• `B站登录` → 扫码登录\n"
                "• `B站状态` → 身份 + 今日额度\n"
                "• `B站看看 <视频>` → 看热门评论\n"
                "• `B站评论 <视频> [内容]` → 发评论（不写内容就自动生成）\n"
                "• `B站互动 <视频> [条数]` → 回复热门评论（默认 1，最多 2）\n"
                "• `B站退出` → 清除本机登录态\n"
                f"限频：10 分钟 ≤{MAX_PER_WINDOW} 条、每天 ≤{MAX_PER_DAY} 条、同视频 ≤{MAX_PER_TARGET_DAY} 条"
            )

        if text.startswith(("B站登录", "B站扫码")):
            if isinstance(event, GroupMessageEvent):
                await bili_account_matcher.finish("二维码别发群里，私聊我发 `B站登录`。")
            ok, msg = await qr_login()
            client.refresh()
            await bili_account_matcher.finish(("✅ " if ok else "❌ ") + msg)

        if text.startswith("B站退出"):
            for path in (COOKIE_FILE,):
                try:
                    path.unlink()
                except OSError:
                    pass
            client.refresh()
            await bili_account_matcher.finish("已清除本机 B 站登录态。")
            return

        if text.startswith("B站状态"):
            client.refresh()
            meta = load_account_meta()
            now = time.time()
            records = [r for r in _load_limits() if now - float(r.get("ts", 0)) < 86400]
            today = [r for r in records if r.get("day") == time.strftime("%Y-%m-%d")]
            if not client.logged_in:
                await bili_account_matcher.finish("还没登录，私聊我发 `B站登录`。")
            nav = await client.nav()
            await bili_account_matcher.finish(
                f"🐟 B站账号：{nav.get('uname')}（uid {nav.get('mid')}）\n"
                f"今日已互动 {len(today)}/{MAX_PER_DAY} 条；10 分钟内 "
                f"{len([r for r in records if now - float(r.get('ts', 0)) < WINDOW_SECONDS])}/{MAX_PER_WINDOW} 条\n"
                f"主人：示例主人"
            )

        if text.startswith("B站看看"):
            client.refresh()
            bvid = await _resolve_target(text)
            aid, info = await client.video_aid(bvid)
            comments = await client.comments(aid, 8)
            if not comments:
                await bili_account_matcher.finish(f"《{info.get('title')}》下面还没人评论。")
            lines = [f"💬 《{info.get('title')}》热门评论："]
            for idx, c in enumerate(comments, 1):
                lines.append(f"{idx}. {c['uname']}（👍{c['like']}）：{c['message'][:60]}")
            await bili_account_matcher.finish("\n".join(lines))

        if text.startswith("B站评论"):
            client.refresh()
            bvid = await _resolve_target(text)
            aid, info = await client.video_aid(bvid)
            rest = re.sub(r"^B站评论", "", text).strip()
            rest = bili.LINK_RE.sub("", rest)
            rest = bili.BV_RE.sub("", rest)
            rest = re.sub(r"av\d{3,}", "", rest, flags=re.I).strip(" ：:，,")
            reason = check_limit(bvid, time.strftime("%Y-%m-%d"))
            if reason:
                await bili_account_matcher.finish("⏳ " + reason)
            if rest:
                message = rest
            else:
                message = await generate_text(
                    "写一条给这个视频的评论。",
                    f"视频标题：{info.get('title')}\nUP主：{(info.get('owner') or {}).get('name')}\n"
                    f"简介：{(info.get('desc') or '')[:200]}\n写一条 60 字以内的评论，像真人随口说的，别夸得太假。",
                )
            if message in recent_texts():
                await bili_account_matcher.finish("这条刚发过，换句新的。")
            rpid = await client.post_comment(aid, bvid, message)
            record_action(bvid, "comment", message)
            await bili_account_matcher.finish(f"✅ 已在《{info.get('title')}》下评论：\n> {message}")

        if text.startswith("B站互动"):
            client.refresh()
            bvid = await _resolve_target(text)
            aid, info = await client.video_aid(bvid)
            m = re.search(r"(?:条数|回)\s*(\d)", text)
            count = min(int(m.group(1)), 2) if m else 1
            reason = check_limit(bvid, time.strftime("%Y-%m-%d"))
            if reason:
                await bili_account_matcher.finish("⏳ " + reason)
            comments = await client.comments(aid, 10)
            my_mid = (await client.nav()).get("mid")
            picked = [c for c in comments if c.get("mid") != my_mid][:count]
            if not picked:
                await bili_account_matcher.finish("没找到能回的评论。")
            done = []
            for c in picked:
                reason = check_limit(bvid, time.strftime("%Y-%m-%d"))
                if reason:
                    break
                message = await generate_text(
                    "回复别人的评论，用大肥鱼的口吻。",
                    f"视频标题：{info.get('title')}\nUP主：{(info.get('owner') or {}).get('name')}\n"
                    f"对方昵称：{c['uname']}\n对方评论：{c['message'][:200]}\n"
                    "写一句 40 字以内的回复，接地气、有梗，别客套。",
                )
                if message in recent_texts():
                    continue
                await client.post_comment(aid, bvid, message, root=c["rpid"], parent=c["rpid"])
                record_action(bvid, "reply", message)
                done.append(f"@{c['uname']}：{message}")
                await asyncio.sleep(2)
            if not done:
                await bili_account_matcher.finish("这轮没发出回复（重复或到限额）。")
            await bili_account_matcher.finish("✅ 已回复：\n" + "\n".join(done))

        await bili_account_matcher.finish("B站互动指令：私聊我发 `B站帮助` 看用法。")

    except BiliAccountError as exc:
        await bili_account_matcher.finish(f"❌ {exc}")
    except FinishedException:
        raise
    except Exception as exc:
        logger.exception(f"[B站互动] 未预期错误：{exc}")
        await bili_account_matcher.finish("B站这边出错了，看看后台日志。")
