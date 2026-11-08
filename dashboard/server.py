# -*- coding: utf-8 -*-
"""
蓝色大肥鱼 · 本地只读仪表盘后端
- 独立进程，只读 SQLite + JSON，不 import 机器人插件、不触发 nonebot/scheduler。
- 路由：GET /            -> index.html
        GET /api/status -> 聚合状态 JSON
        GET /assets/*   -> 静态素材（白名单目录）
- 服务存活检测：psutil 进程名 / 命令行 / 本地端口。
"""
from __future__ import annotations

import json
import hashlib
import hmac
import os
import re
import socket
import sqlite3
import sys
from urllib.parse import parse_qs, urlparse, unquote
import time
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import psutil

# ---------------------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
# ---------------------------------------------------------------------------
# 语音声谱图（2026-11-05）：声音 → 图形
#   librosa 算 Mel 声谱 → numpy 归一化 → PIL 上色 → PNG。
#   不依赖 matplotlib（没装），用 9 段色标模拟 magma。
# ---------------------------------------------------------------------------
TTS_DIR = ROOT / "data" / "tts_local"


def _magma_lut():
    import numpy as np
    stops = [(0, 0, 4), (28, 16, 68), (79, 18, 123), (129, 37, 129), (181, 54, 122),
             (229, 80, 100), (251, 135, 97), (254, 194, 135), (252, 253, 191)]
    lut = []
    for i in range(256):
        t = i / 255.0 * (len(stops) - 1)
        k = int(t)
        f = t - k
        a = stops[k]
        b = stops[min(k + 1, len(stops) - 1)]
        lut.append((int(a[0] + (b[0] - a[0]) * f),
                    int(a[1] + (b[1] - a[1]) * f),
                    int(a[2] + (b[2] - a[2]) * f)))
    import numpy as np
    return np.array(lut, dtype=np.uint8)


def render_spectrogram(path: Path, width: int = 900, height: int = 300) -> bytes:
    """把一段音频画成 Mel 声谱 PNG（黑→紫→橙→白，越亮能量越强）。"""
    import io as _io
    import numpy as np
    import librosa
    from PIL import Image

    y, sr = librosa.load(str(path), sr=22050, mono=True)
    if y.size == 0:
        raise ValueError("空音频")
    S = librosa.feature.melspectrogram(y=y, sr=sr, n_mels=128, fmax=sr // 2)
    S_db = librosa.power_to_db(S, ref=np.max)
    lo, hi = float(S_db.min()), float(S_db.max())
    if hi <= lo:
        hi = lo + 1.0
    img = ((S_db - lo) / (hi - lo) * 255.0).astype(np.uint8)
    img = np.flipud(img)          # 低频在下
    rgb = _magma_lut()[img]
    im = Image.fromarray(rgb, mode="RGB").resize((width, height), Image.BILINEAR)
    buf = _io.BytesIO()
    im.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def render_waveform(path: Path, width: int = 900, height: int = 120) -> bytes:
    """同一段音频的波形图（跟声谱放一起对比）。"""
    import io as _io
    import numpy as np
    import librosa
    from PIL import Image, ImageDraw

    y, sr = librosa.load(str(path), sr=22050, mono=True)
    if y.size == 0:
        raise ValueError("空音频")
    n = min(width, y.size)
    idx = np.linspace(0, y.size - 1, n).astype(np.int64)
    yv = y[idx]
    peak = float(np.abs(yv).max()) or 1.0
    im = Image.new("RGB", (width, height), (8, 8, 14))
    d = ImageDraw.Draw(im)
    mid = height // 2
    for i, v in enumerate(yv):
        h = int(abs(v) / peak * (mid - 2))
        d.line([(i, mid - h), (i, mid + h)], fill=(120, 200, 255))
    d.line([(0, mid), (width, mid)], fill=(40, 40, 60))
    buf = _io.BytesIO()
    im.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def latest_voice_files(limit: int = 12) -> list[dict]:
    """最近生成的语音（按修改时间倒序）。"""
    out: list[dict] = []
    try:
        files = sorted(TTS_DIR.glob("*.mp3"), key=lambda x: x.stat().st_mtime, reverse=True)[:limit]
        for f in files:
            st = f.stat()
            out.append({"name": f.name, "size": st.st_size,
                        "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))})
    except Exception:  # noqa: BLE001
        pass
    return out


sys.path.insert(0, str(ROOT))
try:
    from server import memory_viz  # noqa: E402
    from server import memory_viz_all  # noqa: E402
except Exception:  # noqa: BLE001
    memory_viz = None
    memory_viz_all = None
DASH_DIR = Path(__file__).resolve().parent
ASSETS_DIR = DASH_DIR / "assets"
STATIC_DIR = DASH_DIR

ENV_FILE = ROOT / ".env"
MEMORY_DIR = ROOT / "memory"
DIARY_DIR = MEMORY_DIR / "diary"
LONG_MEMORY_FILE = MEMORY_DIR / "长期记忆.json"
PET_STATE_FILE = MEMORY_DIR / "pet_state.json"
MOOD_FILE = MEMORY_DIR / "daily_mood.json"
BILI_STATE_FILE = ROOT / ".bili_watch_state.json"
DOUYIN_STATE_FILE = MEMORY_DIR / "douyin_comment_state.json"
DOUYIN_LOG_FILE = MEMORY_DIR / "douyin_comments_log.json"

DEFAULT_DB = Path(os.environ.get("APPDATA", str(Path.home()))) / \
    "dsh-pet-standalone-webm-chat" / "shared_memory.db"
DB_PATH = Path(os.environ.get("MEMORY_DB", str(DEFAULT_DB)))

OWNER_QQ = os.environ.get("OWNER_QQ_ID", "100000000")
PORT = int(os.environ.get("DASHBOARD_PORT", "8090"))
# 手机要看面板就得监听所有网卡；用 token 挡一下，局域网里的人不知道 token 进不来。
HOST = os.environ.get("DASHBOARD_HOST", "0.0.0.0")


def _load_token() -> str:
    try:
        f = ROOT / "memory" / "dashboard_token.txt"
        if f.exists():
            tok = f.read_text(encoding="utf-8").strip()
            if len(tok) >= 12:
                return tok
        import secrets
        tok = secrets.token_urlsafe(18)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(tok, encoding="utf-8")
        return tok
    except Exception:  # noqa: BLE001
        return ""


# 口令门禁默认关闭（主人 2026-11-04 19:58 要求直接进）。
# 想开：在 memory/ 下放一个 dashboard_auth_on.flag 文件即可，口令仍读 dashboard_token.txt。
def _auth_enabled() -> bool:
    try:
        return (ROOT / "memory" / "dashboard_auth_on.flag").exists()
    except Exception:  # noqa: BLE001
        return False


TOKEN = _load_token() if _auth_enabled() else ""


# ---------------------------------------------------------------------------
# 通用读取容错
# ---------------------------------------------------------------------------
def _read_json(path: Path) -> dict[str, Any]:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _read_env() -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return out


def _bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "on", "开", "开启"}


def _as_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 服务存活检测
# ---------------------------------------------------------------------------
def _scan_processes() -> tuple[list[str], list[str]]:
    """返回 (进程名小写列表, 命令行小写列表)，跳过无权限进程。"""
    names: list[str] = []
    cmds: list[str] = []
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            info = p.info
            if info.get("name"):
                names.append(str(info["name"]).lower())
            cmd = info.get("cmdline")
            if cmd:
                cmds.append(" ".join(str(x) for x in cmd).lower())
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return names, cmds


def _port_listening(port: int) -> bool:
    if port <= 0:
        return False
    try:
        for c in psutil.net_connections(kind="inet"):
            if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port:
                return True
    except Exception:
        pass
    return False


def _port_established(port: int) -> bool:
    if port <= 0:
        return False
    try:
        for c in psutil.net_connections(kind="inet"):
            if c.status == psutil.CONN_ESTABLISHED and c.laddr and c.laddr.port == port:
                return True
    except Exception:
        pass
    return False


def _port_latency(port: int) -> int | None:
    if port <= 0:
        return None
    t0 = time.perf_counter()
    try:
        with socket.create_connection((HOST, port), timeout=0.4):
            return int((time.perf_counter() - t0) * 1000)
    except OSError:
        return None


def detect_services() -> list[dict[str, Any]]:
    names, cmds = _scan_processes()

    def name_match(kw: str) -> bool:
        kw = kw.lower()
        return any(kw in n for n in names)

    def cmd_match(kw: str) -> bool:
        kw = kw.lower()
        return any(kw in c for c in cmds)

    services = [
        {"key": "pet", "name": "桌面小肥鱼", "running": name_match("dsh-pet-standalone-webm-chat"),
         "kind": "进程", "detail": "dsh-pet-standalone-webm-chat.exe", "port": 0},
        {"key": "sync", "name": "记忆同步引擎", "running": cmd_match("sync_memory.py"),
         "kind": "进程", "detail": "sync_memory.py", "port": 0},
        {"key": "dsh", "name": "dsh web", "running": _port_listening(3080),
         "kind": "端口", "detail": "127.0.0.1:3080", "port": 3080},
        {"key": "napcat", "name": "NapCat(QQ协议端)",
         "running": name_match("napcatwinbootmain") and _port_established(8080),
         "kind": "进程/连接", "detail": "NapCatWinBootMain.exe · 需已登录", "port": 0},
        {"key": "bot", "name": "QQ机器人大脑(bot.py)",
         "running": cmd_match("bot.py") or _port_listening(8080),
         "kind": "进程/端口", "detail": "bot.py · 127.0.0.1:8080", "port": 8080},
        {"key": "bubble", "name": "桌宠头顶气泡",
         "running": cmd_match("pet_bubble.py"),
         "kind": "进程", "detail": "pet_bubble.py", "port": 0},
        {"key": "douyinwin", "name": "抖音评论窗口",
         "running": cmd_match("douyin_window.py"),
         "kind": "进程", "detail": "douyin_window.py", "port": 0},
    ]
    try:
        dy = _read_json(DOUYIN_STATE_FILE)
        auto = "开" if dy.get("auto_reply") else "关"
        services.append({
            "key": "douyin", "name": "抖音", "running": bool(dy.get("enabled")),
            "kind": "任务", "detail": f"每 60 分钟 · 自动回复 {auto}", "port": 0,
        })
    except Exception:
        pass
    for s in services:
        s["latency_ms"] = _port_latency(s["port"]) if s["port"] > 0 else None
    return services


# ---------------------------------------------------------------------------
# SQLite 聚合（只读）
# ---------------------------------------------------------------------------
def _db_connect() -> sqlite3.Connection | None:
    if not DB_PATH.exists():
        return None
    try:
        # 本服务只执行 SELECT。不要在这里 PRAGMA journal_mode=WAL：
        # 那是写操作，机器人正在写库时容易拿不到锁，导致整页记忆变 0。
        conn = sqlite3.connect(str(DB_PATH), timeout=10)
        conn.execute("PRAGMA query_only=ON;")
        return conn
    except Exception:
        return None


def _scope_bucket(scope: str) -> str:
    if scope == f"private_{OWNER_QQ}":
        return "private"
    if scope.startswith("group_"):
        return "group"
    if scope == "bili":
        return "bili"
    if scope == "global_learn":
        return "learn"
    return "other"


def aggregate_memory_db(conn: sqlite3.Connection | None) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "private": {"chat_total": 0, "chat_today": 0, "scopes": []},
        "group": {"chat_total": 0, "chat_today": 0, "scopes": []},
        "bili": {"chat_total": 0, "chat_today": 0},
        "learn": {"chat_total": 0, "chat_today": 0},
        "other": {"chat_total": 0, "chat_today": 0},
        "affinity": {"score": 50, "tag": ""},
        "lexicon": {"enabled": True, "total_terms": 0, "total_mentions": 0, "groups": [], "top": []},
    }
    if conn is None:
        return stats

    cur = conn.cursor()
    # 聊天历史：总数 + 今日数，按 scope 分组
    try:
        cur.execute(
            "SELECT scope, COUNT(*), "
            "SUM(CASE WHEN date(timestamp,'localtime')=date('now','localtime') THEN 1 ELSE 0 END) "
            "FROM chat_history GROUP BY scope"
        )
        group_rows: dict[str, dict[str, int]] = {}
        for scope, total, today in cur.fetchall():
            scope = scope or ""
            bucket = _scope_bucket(scope)
            group_rows.setdefault(bucket, {"total": 0, "today": 0, "scopes": []})
            group_rows[bucket]["total"] += int(total or 0)
            group_rows[bucket]["today"] += int(today or 0)
            if bucket == "group":
                group_rows[bucket]["scopes"].append(
                    {"scope": scope, "chat_total": int(total or 0), "chat_today": int(today or 0)})
        for bucket, agg in group_rows.items():
            stats[bucket]["chat_total"] = agg["total"]
            stats[bucket]["chat_today"] = agg["today"]
            if bucket == "group":
                stats[bucket]["scopes"] = sorted(
                    agg["scopes"], key=lambda x: x["chat_total"], reverse=True)
    except sqlite3.OperationalError:
        pass

    # 今日最近对话：网页上直接看聊天记录（不只给数字）
    # 注意：chat_history.timestamp 是 SQLite CURRENT_TIMESTAMP（UTC），
    # 展示前必须转本地时区，否则 21:xx 会显示成 13:xx。
    try:
        cur.execute(
            "SELECT datetime(timestamp,'localtime'), speaker, substr(message,1,100), substr(reply,1,100) "
            "FROM chat_history WHERE scope = ? AND date(timestamp,'localtime')=date('now','localtime') "
            "ORDER BY id DESC LIMIT 8",
            (f"private_{OWNER_QQ}",),
        )
        stats["private"]["recent_messages"] = [
            {"ts": ts, "speaker": sp, "message": msg, "reply": rep}
            for ts, sp, msg, rep in reversed(cur.fetchall())
        ]
    except sqlite3.OperationalError:
        stats["private"]["recent_messages"] = []

    try:
        cur.execute(
            "SELECT datetime(timestamp,'localtime'), speaker, substr(message,1,100), substr(reply,1,100) "
            "FROM chat_history WHERE scope LIKE 'group_%' "
            "AND date(timestamp,'localtime')=date('now','localtime') "
            "ORDER BY id DESC LIMIT 40"
        )
        stats["group"]["recent_messages"] = [
            {"ts": ts, "speaker": sp, "message": msg, "reply": rep}
            for ts, sp, msg, rep in reversed(cur.fetchall())
        ]
    except sqlite3.OperationalError:
        stats["group"]["recent_messages"] = []

    # 好感度 / 标签
    try:
        cur.execute("SELECT score, tag FROM affinity WHERE user_id = ?", (OWNER_QQ,))
        row = cur.fetchone()
        if row:
            stats["affinity"] = {"score": int(row[0] or 50), "tag": str(row[1] or "")}
    except sqlite3.OperationalError:
        pass

    # 群聊词库
    lex = stats["lexicon"]
    try:
        cur.execute(
            "SELECT scope, COUNT(*), COALESCE(SUM(count),0) FROM group_lexicon "
            "WHERE scope LIKE 'group_%' GROUP BY scope ORDER BY COUNT(*) DESC"
        )
        for scope, n, mentions in cur.fetchall():
            lex["groups"].append({"scope": scope, "terms": int(n), "mentions": int(mentions)})
            lex["total_terms"] += int(n)
            lex["total_mentions"] += int(mentions)
        cur.execute(
            "SELECT term, meaning, count FROM group_lexicon WHERE scope LIKE 'group_%' "
            "ORDER BY count DESC, last_seen DESC LIMIT 40"
        )
        lex["top"] = [
            {"term": r[0], "meaning": r[1] or "-", "count": int(r[2] or 0)}
            for r in cur.fetchall()
        ]
    except sqlite3.OperationalError:
        lex["enabled"] = False
    return stats


# ---------------------------------------------------------------------------
# 令牌用量聚合（只读；表不存在时返回零值结构，不报错）
# ---------------------------------------------------------------------------
def _zero_token_block() -> dict[str, Any]:
    return {"prompt": 0, "completion": 0, "cached": 0,
            "total": 0, "calls": 0, "cost": 0.0}


def _token_cost(pin: int, cached: int, pout: int, prices: dict[str, float]) -> float:
    uncached = max(0, pin - cached)
    return (uncached * prices["in"] + cached * prices["cache"]
            + pout * prices["out"]) / 1_000_000


def _token_block(row: tuple, prices: dict[str, float]) -> dict[str, Any]:
    pin, pout, cached, total, calls, stored = row
    extra = float(stored or 0) if (int(pin or 0) + int(pout or 0) + int(total or 0)) == 0 else 0.0
    return {
        "prompt": int(pin or 0),
        "completion": int(pout or 0),
        "cached": int(cached or 0),
        "total": int(total or 0),
        "calls": int(calls or 0),
        "cost": round(_token_cost(int(pin or 0), int(cached or 0), int(pout or 0), prices) + extra, 6),
    }


_BAL_CACHE: dict[str, Any] = {"at": 0.0, "data": {}}


def _http_json(url: str, headers: dict[str, str], timeout: int = 8) -> dict[str, Any]:
    req = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _deepseek_balance(env: dict[str, str]) -> dict[str, Any]:
    key = env.get("DEEPSEEK_API_KEY") or os.getenv("DEEPSEEK_API_KEY", "")
    if not key:
        return {"available": False, "balance": None, "note": "未配置 DEEPSEEK_API_KEY"}
    try:
        data = _http_json("https://api.deepseek.com/user/balance", {"Authorization": f"Bearer {key}"})
        infos = data.get("balance_infos") or []
        cny = next((x for x in infos if str(x.get("currency") or "").upper() == "CNY"),
                   infos[0] if infos else {})
        return {
            "available": bool(data.get("is_available")),
            "balance": float(cny.get("total_balance") or 0),
            "currency": str(cny.get("currency") or "CNY"),
            "note": "",
        }
    except Exception as e:  # noqa: BLE001
        return {"available": False, "balance": None, "note": f"查询失败：{e}"}


def _ark_balance(env: dict[str, str]) -> dict[str, Any]:
    """火山引擎计费 API：QueryBalanceAcct（AK/SK 签名）。"""
    ak = env.get("VOLC_ACCESS_KEY") or os.getenv("VOLC_ACCESS_KEY", "")
    sk = env.get("VOLC_SECRET_KEY") or os.getenv("VOLC_SECRET_KEY", "")
    if not ak or not sk:
        return {"available": False, "balance": None,
                "note": "未配置 VOLC_ACCESS_KEY / VOLC_SECRET_KEY"}
    try:
        region, service, host = "cn-beijing", "billing", "open.volcengineapi.com"
        now = datetime.utcnow()
        xdate = now.strftime("%Y%m%dT%H%M%SZ")
        day = now.strftime("%Y%m%d")
        params = {"Action": "QueryBalanceAcct", "Version": "2022-01-01"}
        q = "&".join(f"{urllib.parse.quote(k)}={urllib.parse.quote(v)}"
                     for k, v in sorted(params.items()))
        body_hash = hashlib.sha256(b"").hexdigest()
        canonical_headers = f"host:{host}\nx-content-sha256:{body_hash}\nx-date:{xdate}\n"
        signed = "host;x-content-sha256;x-date"
        canonical = f"GET\n/\n{q}\n{canonical_headers}\n{signed}\n{body_hash}"
        scope = f"{day}/{region}/{service}/request"
        sts = (f"HMAC-SHA256\n{xdate}\n{scope}\n"
               f"{hashlib.sha256(canonical.encode()).hexdigest()}")

        def _sign(key: bytes, msg: str) -> bytes:
            return hmac.new(key, msg.encode(), hashlib.sha256).digest()

        k = _sign(sk.encode(), day)
        k = _sign(k, region)
        k = _sign(k, service)
        k = _sign(k, "request")
        sig = hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()
        auth = f"HMAC-SHA256 Credential={ak}/{scope}, SignedHeaders={signed}, Signature={sig}"
        req = urllib.request.Request(
            f"https://{host}/?{q}",
            headers={"Host": host, "X-Date": xdate,
                     "X-Content-Sha256": body_hash, "Authorization": auth},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        result = data.get("Result") or {}
        return {
            "available": True,
            "balance": float(result.get("AvailableBalance") or 0),
            "cash": float(result.get("CashBalance") or 0),
            "arrears": float(result.get("ArrearsBalance") or 0),
            "currency": str(result.get("Currency") or "CNY"),
            "note": "",
        }
    except Exception as e:  # noqa: BLE001
        return {"available": False, "balance": None, "note": f"查询失败：{e}"}


def _siliconflow_usage() -> dict[str, Any]:
    """硅基流动：官方余额接口 /v1/user/info 已下线（410 deprecated），改为展示本机用量。

    今天的 token 来自 token_usage（provider=siliconflow），
    语音识别秒数来自 memory/asr_usage.json（ASR 不按 token 计费）。
    """
    today = time.strftime("%Y-%m-%d")
    today_tokens = 0
    total_tokens = 0
    today_calls = 0
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        cur = conn.execute(
            "select coalesce(sum(total_tokens),0), count(*) from token_usage "
            "where provider='siliconflow' and date=?",
            (today,),
        )
        row = cur.fetchone() or (0, 0)
        today_tokens, today_calls = int(row[0] or 0), int(row[1] or 0)
        cur = conn.execute(
            "select coalesce(sum(total_tokens),0) from token_usage where provider='siliconflow'"
        )
        total_tokens = int((cur.fetchone() or (0,))[0] or 0)
        conn.close()
    except Exception:  # noqa: BLE001
        pass
    asr = _read_json(ROOT / "memory" / "asr_usage.json")
    try:
        credit_total = float(_read_env().get("SF_CREDIT_TOTAL") or 0)
    except (TypeError, ValueError):
        credit_total = 0.0
    return {
        "available": True,
        "balance": None,
        "credit_total": credit_total,
        "currency": "credit",
        "today_tokens": today_tokens,
        "today_calls": today_calls,
        "total_tokens": total_tokens,
        "asr_seconds": float(asr.get("seconds") or 0),
        "asr_ok": int(asr.get("ok") or 0),
        "asr_fail": int(asr.get("fail") or 0),
        "note": "赠送额度 · 官方余额接口已下线，这里看用量",
    }


def fetch_balances(env: dict[str, str]) -> dict[str, Any]:
    now = time.time()
    if _BAL_CACHE["data"] and now - float(_BAL_CACHE["at"]) < 60:
        return _BAL_CACHE["data"]
    data = {"ark": _ark_balance(env), "deepseek": _deepseek_balance(env),
            "siliconflow": _siliconflow_usage()}
    _BAL_CACHE["at"] = now
    _BAL_CACHE["data"] = data
    return data


def _provider_prices(provider: str, ark_prices: dict[str, float],
                     ds_prices: dict[str, float]) -> dict[str, float]:
    p = (provider or "ark").lower()
    if p == "deepseek":
        return ds_prices
    if p == "github":
        return {"in": 0.0, "cached": 0.0, "out": 0.0}
    return ark_prices


def aggregate_token(conn: sqlite3.Connection | None, prices: dict[str, float],
                    ds_prices: dict[str, float] | None = None) -> dict[str, Any]:
    ds_prices = ds_prices or prices
    out: dict[str, Any] = {
        "enabled": False,
        "today": _zero_token_block(),
        "all": _zero_token_block(),
        "by_scope": [],
        "by_provider": [],
    }
    if conn is None:
        return out
    cur = conn.cursor()
    base = ("SELECT COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(completion_tokens),0), "
            "COALESCE(SUM(cached_tokens),0), COALESCE(SUM(total_tokens),0), "
            "COALESCE(SUM(calls),0), COALESCE(SUM(cost),0) FROM token_usage")
    try:
        cur.execute(base)
        out["all"] = _token_block(cur.fetchone(), prices)
        cur.execute(base + " WHERE date = date('now','localtime')")
        out["today"] = _token_block(cur.fetchone(), prices)
        cur.execute(
            "SELECT scope, COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(completion_tokens),0), "
            "COALESCE(SUM(cached_tokens),0), COALESCE(SUM(total_tokens),0), "
            "COALESCE(SUM(calls),0), COALESCE(SUM(cost),0) FROM token_usage GROUP BY scope"
        )
        for scope, pin, pout, cached, total, calls, stored in cur.fetchall():
            out["by_scope"].append({
                "scope": str(scope or "other"),
                "prompt": int(pin or 0),
                "completion": int(pout or 0),
                "cached": int(cached or 0),
                "total": int(total or 0),
                "calls": int(calls or 0),
                "cost": round(_token_cost(int(pin or 0), int(cached or 0), int(pout or 0), prices)
                              + (float(stored or 0) if (int(pin or 0) + int(pout or 0) + int(total or 0)) == 0 else 0.0), 6),
            })
        # 固定顺序，出图/配音始终跟聊天/工具排在一起（没有记录也显示 0）
        order = ["chat", "utility", "image", "vision", "tts", "web_search", "memory_merge", "lexicon"]
        have = {x["scope"] for x in out["by_scope"]}
        for sc in order:
            if sc not in have:
                out["by_scope"].append({"scope": sc, "prompt": 0, "completion": 0, "cached": 0,
                                        "total": 0, "calls": 0, "cost": 0.0})
        rank = {k: i for i, k in enumerate(order)}
        out["by_scope"].sort(key=lambda x: (rank.get(x["scope"], 99), -x["cost"]))

        # 今天按供应商拆分：火山方舟 / DeepSeek 官方
        cur.execute(
            "SELECT COALESCE(provider,'ark'), COALESCE(SUM(prompt_tokens),0), "
            "COALESCE(SUM(completion_tokens),0), COALESCE(SUM(cached_tokens),0), "
            "COALESCE(SUM(total_tokens),0), COALESCE(SUM(calls),0), COALESCE(SUM(cost),0) "
            "FROM token_usage WHERE date = date('now','localtime') GROUP BY provider "
            "ORDER BY SUM(total_tokens) DESC"
        )
        for prov, pin, pout, cached, total, calls, stored in cur.fetchall():
            pp = _provider_prices(str(prov), prices, ds_prices)
            out["by_provider"].append({
                "provider": str(prov or "ark"),
                "prompt": int(pin or 0),
                "completion": int(pout or 0),
                "cached": int(cached or 0),
                "total": int(total or 0),
                "calls": int(calls or 0),
                "cost": round(_token_cost(int(pin or 0), int(cached or 0), int(pout or 0), pp)
                              + (float(stored or 0) if (int(pin or 0) + int(pout or 0) + int(total or 0)) == 0 else 0.0), 6),
            })
        out["enabled"] = True
    except sqlite3.OperationalError:
        # 表尚未建立（机器人计量模块还没跑过）：保持零值
        out["enabled"] = False
    return out


# ---------------------------------------------------------------------------
# JSON 文件聚合
# ---------------------------------------------------------------------------
def _archive_block(data: dict[str, Any], scope: str) -> dict[str, Any]:
    archive = (data.get("archive") or {}).get(scope) or []
    pending = (data.get("pending") or {}).get(scope) or []
    entries = [it for it in archive if isinstance(it, dict) and it.get("fact")]
    chars = sum(len(str(it.get("fact", ""))) for it in entries)
    facts = sorted(entries, key=lambda x: x.get("id", 0))[-30:][::-1]
    _now = time.time()

    def _live(it: dict[str, Any]) -> bool:
        try:
            exp = float(it.get("exp") or 0)
        except (TypeError, ValueError):
            exp = 0.0
        return not exp or exp > _now

    live = [it for it in entries if _live(it)]
    return {
        "entries": len(entries),
        "live": len(live),
        "chars": chars,
        "pending": len(pending),
        "recent": [
            {"subject": str(it.get("subject") or ""), "fact": str(it.get("fact") or ""),
             "time": str(it.get("time") or ""), "src": str(it.get("src") or ""),
             "kind": str(it.get("kind") or ""), "expired": not _live(it)}
            for it in facts if isinstance(it, dict) and it.get("fact")
        ],
    }


def aggregate_long_memory(data: dict[str, Any]) -> dict[str, Any]:
    archive = data.get("archive") or {}
    pending = data.get("pending") or {}
    group_entries = 0
    group_chars = 0
    group_pending = 0
    for scope, items in archive.items():
        if str(scope).startswith("group_") and isinstance(items, list):
            items = [it for it in items if isinstance(it, dict) and it.get("fact")]
            group_entries += len(items)
            group_chars += sum(len(str(it.get("fact", ""))) for it in items)
    for scope, items in pending.items():
        if str(scope).startswith("group_") and isinstance(items, list):
            group_pending += len(items)
    return {
        "private": _archive_block(data, f"private_{OWNER_QQ}"),
        "learn": _archive_block(data, "global_learn"),
        "group": {"entries": group_entries, "chars": group_chars, "pending": group_pending},
    }


def aggregate_diary() -> dict[str, Any]:
    out = {"count": 0, "latest_date": "", "latest_excerpt": "",
           "latest_stats": {}, "entries": [], "today": {}}
    if not DIARY_DIR.exists():
        return out
    files = sorted(DIARY_DIR.glob("*.json"))
    if not files:
        return out
    out["count"] = len(files)
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        date = str(d.get("date") or f.stem)
        body = str(d.get("body") or "")
        out["entries"].append({
            "date": date,
            "body": body,
            "stats": d.get("stats") or {},
        })
    # 2026-11-08：单独给一份「今天在 B 站看了什么」。
    # 日记正文靠模型自己写，写没写不确定；这一份是原始记录，面板直接能显示。
    try:
        _bd = _read_json(MEMORY_DIR / "bili_daily.json") or {}
        _today = datetime.now().strftime("%Y-%m-%d")
        out["today_watched"] = [
            {"title": str(h.get("title") or "")[:80],
             "summary": str(h.get("summary") or "")[:160],
             "at": str(h.get("at") or ""), "by": str(h.get("by") or "")}
            for h in (h for h in (_bd.get("history") or []) if str(h.get("at") or "").startswith(_today))
        ][-12:]
    except Exception:  # noqa: BLE001
        out["today_watched"] = []
    if out["entries"]:
        latest = out["entries"][-1]
        out["latest_date"] = latest["date"]
        out["latest_excerpt"] = latest["body"][:140]
        out["latest_stats"] = latest["stats"]
        today = datetime.now().strftime("%Y-%m-%d")
        out["today"] = next((x for x in out["entries"] if x["date"] == today), {})
    return out


def aggregate_mood(data: dict[str, Any]) -> dict[str, Any]:
    history = data.get("history") or []
    latest = history[-1] if history else {}
    days: dict[str, list[dict[str, Any]]] = {}
    for item in history:
        if not isinstance(item, dict):
            continue
        ts = str(item.get("ts") or "")
        day = ts[:10]
        if not day:
            continue
        days.setdefault(day, []).append({
            "ts": ts,
            "period": item.get("period", ""),
            "text": item.get("text", ""),
        })
    today = datetime.now().strftime("%Y-%m-%d")
    return {
        "enabled": bool(data.get("enabled", True)),
        "history_count": len(history),
        "sent_today": data.get("sent_today") or {},
        "latest": {"ts": latest.get("ts", ""), "period": latest.get("period", ""),
                   "text": latest.get("text", "")},
        "today": days.get(today, []),
        "days": days,
    }


def aggregate_douyin() -> dict[str, Any]:
    state = _read_json(DOUYIN_STATE_FILE)
    log = _read_json(DOUYIN_LOG_FILE)
    items = log.get("items") if isinstance(log, dict) else log
    items = [x for x in (items or []) if isinstance(x, dict)]
    vlog = _read_json(ROOT / "memory" / "douyin_videos.json")
    vitems = vlog.get("items") if isinstance(vlog, dict) else vlog
    vitems = [x for x in (vitems or []) if isinstance(x, dict)]
    # 手机端（ADB 自动化回 @）的记录，来自 memory/douyin_phone.json 的 history
    pstate = _read_json(ROOT / "memory" / "douyin_phone.json")
    phist = [x for x in (pstate.get("history") or []) if isinstance(x, dict)]
    today = time.strftime("%Y-%m-%d")
    phone = {
        "seen": len(pstate.get("seen") or []),
        "replied_today": sum(1 for x in phist if str(x.get("ts") or "").startswith(today)),
        "last_at": str(phist[-1].get("ts") or "") if phist else "",
        "retry": pstate.get("retry") or {},
        "history": list(reversed(phist[-8:])),
    }
    return {
        "enabled": bool(state.get("enabled")),
        "auto_reply": bool(state.get("auto_reply")),
        "last_poll": phone["last_at"] or state.get("last_poll") or "",
        "seen": len(state.get("seen") or []),
        "count": len(items),
        "comments": list(reversed(items[-12:])),
        "videos": len(vitems),
        "video_items": list(reversed(vitems[-6:])),
        "phone": phone,
    }


def aggregate_qzone() -> dict[str, Any]:
    state = _read_json(ROOT / "memory" / "qzone_sync.json")
    return {
        "enabled": bool(state.get("enabled")),
        "last_post": state.get("last_post") or "",
        "history": len(state.get("history") or []),
    }


def aggregate_activity() -> dict[str, Any]:
    data = _read_json(ROOT / "memory" / "activity_state.json")
    now = time.time()
    label = str(data.get("activity") or "")
    if label and float(data.get("expires") or 0) > now:
        return {"active": True, "text": label, "detail": str(data.get("detail") or ""),
                "updated_at": data.get("ts") or 0}
    sleep = _read_json(ROOT / "memory" / "sleep_state.json")
    if bool(sleep.get("sleeping")):
        return {"active": True, "text": "睡觉中", "detail": "", "updated_at": 0}
    return {"active": False, "text": "发呆", "detail": "", "updated_at": 0}


# ---------------------------------------------------------------------------
# 房间：把「她现在在干啥」映射到桌宠自己的动画片段，网页端按锚点贴进房间插画
# ---------------------------------------------------------------------------
ROOM_CLIPS: dict[str, dict[str, Any]] = {
    "idle": {"url": "/assets/pet/idle.webp", "left": 52.0, "bottom": 4.5, "width": 15.0, "label": "发呆"},
    "phone": {"url": "/assets/pet/phone.webp", "left": 62.0, "bottom": 5.0, "width": 15.0, "label": "刷手机"},
    "work": {"url": "/assets/pet/work.webp", "left": 79.0, "bottom": 9.0, "width": 15.0, "label": "干正事"},
    "eat": {"url": "/assets/pet/eat.webp", "left": 40.0, "bottom": 4.5, "width": 15.0, "label": "干饭"},
    "sleep": {"url": "/assets/pet/sleep.webp", "left": 31.0, "bottom": 29.0, "width": 15.0, "label": "睡觉"},
    "play": {"url": "/assets/pet/play.webp", "left": 56.0, "bottom": 4.5, "width": 15.0, "label": "玩会儿"},
    "dance": {"url": "/assets/pet/dance.webp", "left": 48.0, "bottom": 4.5, "width": 15.0, "label": "开心"},
    "tired": {"url": "/assets/pet/tired.webp", "left": 67.0, "bottom": 5.0, "width": 15.0, "label": "累了"},
}

# 成套房间图：白天晴/雨/阴各有整张图（窗户和屏幕都画好了），没有的档位退回夜间撕口版 + 前端叠加
ROOM_DAY_BG = {
    "clear": "room_day.jpg",
    "rain": "room_day_rain.jpg",
    "storm": "room_day_rain.jpg",
    "cloudy": "room_day_cloudy.jpg",
    "fog": "room_day_cloudy.jpg",
    "snow": "room_day_cloudy.jpg",
}
ROOM_NIGHT_BG = {
    "work": "room_night_code.jpg",
    "bili": "room_night_bili.jpg",
    "douyin": "room_night_douyin.jpg",
    "boss": "room_night.jpg",
}


def room_background(outside: dict[str, Any], clip: str, label: str) -> tuple[str, bool]:
    """返回 (背景图, 是否还要前端叠窗户/屏幕)。

    白天三档用整张图；夜里晴且她坐在电脑前时，按屏幕内容选夜图；
    其余情况退回撕口版 + 实时夜空/熄屏叠加。
    """
    phase = str(outside.get("phase") or "night")
    weather = str(outside.get("weather") or "clear")
    if weather in ("rain", "storm"):
        return "/assets/room_day_rain.jpg", False
    if weather in ("cloudy", "fog", "snow"):
        return "/assets/room_day_cloudy.jpg", False
    if phase == "day":
        return "/assets/" + ROOM_DAY_BG.get(weather, "room_day.jpg"), False

    if clip in ("work", "phone"):
        if clip == "work":
            key = "work"
        elif "抖音" in label:
            key = "douyin"
        elif "BOSS" in label.upper() or "招聘" in label:
            key = "boss"
        elif "B站" in label or "bili" in label.lower():
            key = "bili"
        else:
            key = "bili"
        return "/assets/" + ROOM_NIGHT_BG.get(key, "room_night.jpg"), False
    # 夜里没在电脑前：用熄屏版夜图，不再回退到抠洞图
    return "/assets/room_night_off.jpg", False

_ROOM_RULES = [
    ("sleep", re.compile(r"睡|困|打盹|眯|午休|躺")),
    ("eat", re.compile(r"吃|饭|零食|宵夜|外卖|火锅|奶茶|夜宵|早餐|午餐|晚餐")),
    ("work", re.compile(r"代码|写东西|工作|整理|任务|报表|学习|看书|写作|干活|回消息")),
    ("phone", re.compile(r"抖音|B站|视频|手机|刷|看剧|追番|微博|小红书|直播")),
    ("dance", re.compile(r"跳舞|唱歌|开心|庆祝|玩游戏|聊天|陪")),
]


ROOM_CONFIG_PATH = DASH_DIR / "room_config.json"
_ROOM_OUTSIDE_CACHE: dict[str, Any] = {"at": 0.0, "data": None}


def _room_coords() -> tuple[float, float] | None:
    """先读 room_config.json 的坐标；没有就按出口 IP 定位一次并写回配置。"""
    cfg = _read_json(ROOM_CONFIG_PATH)
    lat, lon = cfg.get("lat"), cfg.get("lon")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        return float(lat), float(lon)
    try:
        req = urllib.request.Request(
            "http://ip-api.com/json/?fields=lat,lon,city",
            headers={"User-Agent": "feiyu-dashboard/1.0"},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            j = json.loads(resp.read().decode("utf-8", "replace"))
        lat, lon = float(j["lat"]), float(j["lon"])
        ROOM_CONFIG_PATH.write_text(
            json.dumps({"lat": lat, "lon": lon, "city": j.get("city") or ""},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return lat, lon
    except Exception:  # noqa: BLE001
        return None


def _weather_kind(code: int) -> str:
    """WMO 天气码 → 窗外素材档位。"""
    if code in (0, 1):
        return "clear"
    if code in (2, 3):
        return "cloudy"
    if code in (45, 48):
        return "fog"
    if 51 <= code <= 67 or 80 <= code <= 82:
        return "rain"
    if 71 <= code <= 77 or 85 <= code <= 86:
        return "snow"
    if code >= 95:
        return "storm"
    return "cloudy"


def room_outside() -> dict[str, Any]:
    """窗外：本地时间定昼夜，真实天气（Open-Meteo）定晴雨，10 分钟缓存。"""
    now = time.time()
    cached = _ROOM_OUTSIDE_CACHE.get("data")
    if cached and now - float(_ROOM_OUTSIDE_CACHE.get("at") or 0) < 600:
        return cached

    hour = datetime.now().hour
    if 6 <= hour < 8:
        phase = "dawn"
    elif 8 <= hour < 17:
        phase = "day"
    elif 17 <= hour < 19:
        phase = "dusk"
    else:
        phase = "night"

    data: dict[str, Any] = {"phase": phase, "weather": "clear", "temp": None,
                            "city": "", "source": "clock"}
    cfg = _read_json(ROOM_CONFIG_PATH)
    coords = _room_coords() if cfg.get("weather", True) else None
    if coords:
        lat, lon = coords
        try:
            url = (
                "https://api.open-meteo.com/v1/forecast?latitude=%.4f&longitude=%.4f"
                "&current=temperature_2m,weather_code,is_day&timezone=auto" % (lat, lon)
            )
            with urllib.request.urlopen(url, timeout=4) as resp:
                j = json.loads(resp.read().decode("utf-8", "replace"))
            cur = j.get("current") or {}
            code = int(cur.get("weather_code") or 0)
            data["weather"] = _weather_kind(code) if code else "clear"
            temp = cur.get("temperature_2m")
            data["temp"] = round(float(temp), 1) if isinstance(temp, (int, float)) else None
            data["city"] = _read_json(ROOM_CONFIG_PATH).get("city") or ""
            data["source"] = "open-meteo"
            if cur.get("is_day") == 0 and phase != "night":
                data["phase"] = "night"
            elif cur.get("is_day") == 1 and phase == "night":
                data["phase"] = "night"
        except Exception as exc:  # noqa: BLE001
            data["source"] = "error:" + type(exc).__name__

    _ROOM_OUTSIDE_CACHE["at"] = now
    _ROOM_OUTSIDE_CACHE["data"] = data
    return data


ROOM_MANIFEST = DASH_DIR / "assets" / "pet" / "all" / "manifest.json"
_ROOM_ACTIONS: dict[str, Any] = {"loaded": False, "by_id": {}, "by_name": {}, "width": 34.0}

# 状态 -> 用哪个动作（动作名来自 frameseq 目录名）
ROOM_STATE_ACTION = {
    "idle": "待机呼吸休闲",
    "phone": "碎碎念-对屏碎碎念",
    "work": "写代码",
    "eat": "吃午餐",
    "sleep": "原地小憩沉眠",
    "play": "原地专心玩魔方",
    "dance": "轻快摇摆舞",
    "tired": "哈欠连天",
}
# 没别的事时，她自己轮着玩这些（每 90 秒换一个）
ROOM_IDLE_POOL = [
    "原地专心玩魔方", "可爱宅舞", "撸猫", "荡秋千", "悠闲哼歌", "轻快记录",
    "打瞌睡被惊醒", "照镜子", "三球抛接", "下五子棋", "碎碎念-发呆碎碎念",
    "小幅度原地360度旋转展示", "原地蹲下玩玩具汽车", "轻快摇摆舞", "抽陀螺",
]
ROOM_ZONES = {"rug": (52.0, 2.5), "desk": (79.0, 6.0), "bed": (30.0, 29.0), "eat": (40.0, 2.5)}
ROOM_CLIP_ZONE = {"work": "desk", "phone": "desk", "sleep": "bed", "eat": "eat"}


def room_actions() -> dict[str, Any]:
    """懒加载动作清单（tools/build_room_actions.py 生成）。"""
    if not _ROOM_ACTIONS["loaded"]:
        data = _read_json(ROOM_MANIFEST)
        by_id, by_name = {}, {}
        for row in data.get("actions") or []:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            by_id[row["id"]] = row
            by_name.setdefault(str(row.get("name") or ""), row)
        _ROOM_ACTIONS["by_id"] = by_id
        _ROOM_ACTIONS["by_name"] = by_name
        _ROOM_ACTIONS["width"] = float(data.get("display_width") or 34)
        _ROOM_ACTIONS["loaded"] = True
    return _ROOM_ACTIONS


def aggregate_room() -> dict[str, Any]:
    """房间状态：活动 + 桌宠动画片段 + 锚点（百分比，相对 16:9 舞台）。"""
    act = _read_json(ROOT / "memory" / "activity_state.json")
    sleep = _read_json(ROOT / "memory" / "sleep_state.json")
    pet = _read_json(ROOT / "memory" / "pet_state.json")

    now = time.time()
    live = str(act.get("activity") or "")
    detail = str(act.get("detail") or "")
    if not live or float(act.get("expires") or 0) <= now:
        live, detail = "", ""

    sleeping = bool(sleep.get("sleeping"))
    # 2026-11-05：优先用机器人自己的「能量体征」（memory/energy_state.json）。
    # 桌宠 manifest 里那个 energy 是另一套（只驱动小人表情），
    # 这里让面板的「精力值」跟聊天系统对齐 —— 她聊累了，面板上看得见。
    energy = None
    try:
        _ef = ROOT / "memory" / "energy_state.json"
        if _ef.exists():
            _ed = json.loads(_ef.read_text(encoding="utf-8-sig"))
            if isinstance(_ed.get("value"), (int, float)):
                energy = int(round(float(_ed["value"])))
    except Exception:  # noqa: BLE001
        energy = None
    if energy is None:
        energy = pet.get("energy")
    hunger = pet.get("hunger")
    mood = pet.get("mood_val")
    energy = int(energy) if isinstance(energy, (int, float)) else None
    hunger = int(hunger) if isinstance(hunger, (int, float)) else None
    mood = int(mood) if isinstance(mood, (int, float)) else None

    hay = f"{live} {detail}"
    clip = "idle"
    if sleeping:
        clip = "sleep"
    else:
        for key, pat in _ROOM_RULES:
            if pat.search(hay):
                clip = key
                break
        else:
            if energy is not None and energy < 25:
                clip = "tired"
            elif mood is not None and mood >= 78:
                clip = "play"

    outside = room_outside()
    label = live or ROOM_CLIPS[clip]["label"]
    bg, overlay = room_background(outside, clip, label)

    acts = room_actions()
    picked = ROOM_STATE_ACTION.get(clip, "待机呼吸休闲")
    if clip == "idle" and acts["by_name"]:
        pool = [n for n in ROOM_IDLE_POOL if n in acts["by_name"]]
        if pool:
            picked = pool[int(now // 90) % len(pool)]
    act = acts["by_name"].get(picked)
    left, bottom = ROOM_ZONES[ROOM_CLIP_ZONE.get(clip, "rug")]
    return {
        "clip": act["id"] if act else clip,
        "kind": clip,
        "bg": bg,
        "overlay": overlay,
        "label": label,
        "detail": detail,
        "url": act["url"] if act else ROOM_CLIPS[clip]["url"],
        "left": left,
        "bottom": bottom,
        "width": acts["width"],
        "action_name": picked,
        "action_count": len(acts["by_id"]),
        "sleeping": sleeping,
        "outside": outside,
        "state": {
            "mood": pet.get("mood") or "",
            "mood_val": mood,
            "hunger": hunger,
            "energy": energy,
            "body_desc": pet.get("body_desc") or "",
            "updated_at": pet.get("updated_at") or "",
        },
        "now": int(now),
    }


def aggregate_vision() -> dict[str, Any]:
    """今天识图落在哪条通道（gemini / local / siliconflow / ark），各多少次、平均多久。"""
    out: dict[str, Any] = {"channels": [], "total": 0}
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        rows = conn.execute(
            "SELECT task, COUNT(*), COALESCE(SUM(seconds),0) FROM local_usage "
            "WHERE date = date('now','localtime') AND task LIKE 'vision%' "
            "GROUP BY task ORDER BY 2 DESC"
        ).fetchall()
        names = {"vision": "本地", "vision:local": "本地", "vision:gemini": "谷歌",
                 "vision:siliconflow": "硅基", "vision:ark": "火山"}
        total = 0
        for task, calls, secs in rows:
            key = str(task).replace("vision:", "") if str(task) != "vision" else "local"
            secs = float(secs or 0)
            calls = int(calls or 0)
            total += calls
            out["channels"].append({
                "key": key,
                "name": names.get(str(task), key),
                "calls": calls,
                "avg": round(secs / calls, 1) if calls else 0.0,
            })
        out["total"] = total
    except sqlite3.Error:
        pass
    return out


def aggregate_local() -> dict[str, Any]:
    out: dict[str, Any] = {"calls": 0, "seconds": 0.0, "tasks": {}}
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        cur = conn.execute(
            "SELECT task, COUNT(*), COALESCE(SUM(seconds),0) FROM local_usage "
            "WHERE date = date('now','localtime') GROUP BY task"
        )
        for task, calls, secs in cur.fetchall():
            out["tasks"][str(task or "local")] = {
                "calls": int(calls or 0), "seconds": round(float(secs or 0), 1)
            }
            out["calls"] += int(calls or 0)
            out["seconds"] += float(secs or 0)
        conn.close()
    except Exception:
        pass
    out["seconds"] = round(out["seconds"], 1)
    return out


def aggregate_sleep() -> dict[str, Any]:
    st = _read_json(ROOT / "memory" / "sleep_state.json")
    now = time.time()
    sleeping = bool(st.get("sleeping"))
    start = float(st.get("start_ts") or 0)
    seconds = (now - start) if sleeping and start else float(st.get("last_sleep_seconds") or 0)
    h, m = int(seconds // 3600), int((seconds % 3600) // 60)
    if sleeping:
        return {"sleeping": True, "seconds": seconds, "percent": 0.0,
                "text": f"睡觉中 · 已睡 {h} 小时 {m} 分"}
    wstart = float(st.get("waking_start") or 0)
    wuntil = float(st.get("waking_until") or 0)
    if wuntil > now and wuntil > wstart:
        pct = min(100.0, max(0.0, (now - wstart) / (wuntil - wstart) * 100.0))
        return {"sleeping": False, "seconds": 0.0, "percent": round(pct, 1),
                "text": f"正在醒 {pct:.0f}% · 迷迷糊糊"}
    return {"sleeping": False, "seconds": seconds, "percent": 100.0,
            "text": (f"醒着 · 刚才睡了 {h} 小时 {m} 分" if seconds else "醒着")}


def aggregate_bili_daily() -> dict[str, Any]:
    """B站每日刷视频：今日进度 + 最近看过的标题（网页端同步显示）。"""
    data = _read_json(ROOT / "memory" / "bili_daily.json")
    items = (data.get("history") or [])[-60:]
    out_items = []
    for it in reversed(items):
        out_items.append({
            "title": str(it.get("title") or ""),
            "bvid": str(it.get("bvid") or ""),
            "at": str(it.get("at") or ""),
            "by": str(it.get("by") or ""),
            "summary": str(it.get("summary") or "")[:160],
            "comment": str(it.get("comment") or "")[:120],
        })
    # 2026-11-07：原来只读 .env 的 BILI_DAILY_LIMIT，但运行时用的是
    # memory/bili_watch_config.json 的 daily_limit（那个改完即刻生效、不用重启）。
    # 两处不同源，于是面板显示"还剩 10"，实际能看 40 —— 主人看到的就是错的信息。
    # 现在统一以配置文件为准，读不到再退回环境变量、最后兜底 10。
    try:
        _cfg = _read_json(ROOT / "memory" / "bili_watch_config.json") or {}
        limit = int(float(_cfg.get("daily_limit") or 0)) or 10
    except Exception:  # noqa: BLE001
        try:
            limit = int(float((ROOT / ".env").read_text(encoding="utf-8", errors="ignore")
                              .split("BILI_DAILY_LIMIT=")[1].splitlines()[0].strip() or 10))
        except Exception:
            limit = 10
    count = int(data.get("count") or 0) if str(data.get("date") or "") == time.strftime("%Y-%m-%d") else 0
    prog = data.get("part_progress") or {}
    titles = data.get("series_title") or {}
    series = [{"bvid": b, "title": str(titles.get(b) or b), "part": int(p or 0)}
              for b, p in prog.items()]
    return {"date": str(data.get("date") or ""), "count": count,
            "limit": limit, "left": max(0, limit - count), "items": out_items,
            "series": series}


def aggregate_emotion() -> dict[str, Any]:
    data = _read_json(ROOT / "memory" / "emotion_state.json")
    # 趋势曲线 + 当前激素（新面板用）
    hist = _read_json(ROOT / "memory" / "emotion_history.json")
    horm = _read_json(ROOT / "memory" / "hormones_state.json")
    try:
        data["history"] = (hist.get("points") or [])[-240:]
    except Exception:  # noqa: BLE001
        data["history"] = []
    try:
        data["hormones"] = (horm.get("values") or {})
    except Exception:  # noqa: BLE001
        data["hormones"] = {}
    values = data.get("values") or {}
    order = ["开心", "害羞", "烦", "怒", "丧", "累", "害怕"]
    base = {"开心": 31.6, "害羞": 8.4, "烦": 10.5, "怒": 10.5, "丧": 7.4, "累": 26.3, "害怕": 5.3}
    vals = {k: float(values.get(k) if values.get(k) is not None else base[k]) for k in order}
    if sum(vals.values()) <= 0:
        vals = dict(base)
    return {"values": vals, "updated_at": data.get("updated_at") or "",
            "history": data.get("history") or [], "hormones": data.get("hormones") or {}}


def aggregate_bili(data: dict[str, Any]) -> dict[str, Any]:
    replies = data.get("recent_replies") or []
    latest = replies[0] if replies else {}
    return {
        "seen_count": len(data.get("seen") or []),
        "reply_count": len(replies),
        "latest": {
            "at_time": latest.get("at_time", ""),
            "nickname": latest.get("nickname", ""),
            "title": latest.get("title", ""),
            "bvid": latest.get("bvid", ""),
            "url": latest.get("url", ""),
            "draft": str(latest.get("draft") or "")[:120],
        },
        "recent": [
            {
                "at_time": r.get("at_time", ""),
                "nickname": r.get("nickname", ""),
                "title": r.get("title", ""),
                "bvid": r.get("bvid", ""),
                "url": r.get("url", ""),
                "draft": str(r.get("draft") or "")[:120],
            }
            for r in replies[:5]
        ],
    }


def _hunger_label(h: int) -> str:
    """饥饿度转人话：数字不好懂，给个档位。"""
    if h >= 85:
        return "重度饿"
    if h >= 60:
        return "轻度饿"
    if h >= 35:
        return "不饱不饿"
    if h >= 15:
        return "饱"
    return "很饱"


def aggregate_pet_state(data: dict[str, Any]) -> dict[str, Any]:
    _h = int(data.get("hunger", 0) or 0)
    return {
        "mood": data.get("mood", "—"),
        "mood_val": int(data.get("mood_val", 0) or 0),
        "hunger": _h,                       # 原始饥饿度（越大越饿）
        "fullness": max(0, 100 - _h),       # 饱食度（越大越饱，符合直觉）
        "hunger_label": _hunger_label(_h),
        "energy": _energy_now(data),
        "daily_mood": data.get("daily_mood", "—"),
        "body_desc": data.get("body_desc", ""),
        "updated_at": data.get("updated_at", ""),
    }


def _energy_now(pet_data: dict[str, Any]) -> int:
    """面板「精力值」的数据源。

    2026-11-05：优先读机器人自己的能量体征（memory/energy_state.json）——
    它是按聊天轮次扣的，跟她真实状态一致；桌宠 manifest 里那个 energy 只驱动小人表情，
    两边对不上。新系统没数据时才回落到旧的。
    """
    try:
        ed = _read_json(ROOT / "memory" / "energy_state.json") or {}
        v = ed.get("value")
        if isinstance(v, (int, float)):
            return int(round(float(v)))
    except Exception:  # noqa: BLE001
        pass
    return int(pet_data.get("energy", 0) or 0)


# ---------------------------------------------------------------------------
# 顶层聚合（纯函数，便于隔离测试）
# ---------------------------------------------------------------------------
def _scope_display() -> dict[str, str]:
    """作用域 → 人类可读名字（群名从 group_permissions.json 取）。"""
    names: dict[str, str] = {"global_learn": "自学池", "douyin": "抖音"}
    try:
        gp = _read_json(ROOT / "memory" / "group_permissions.json") or {}
        for gid, info in (gp.get("groups") or {}).items():
            nm = str((info or {}).get("name") or "").strip()
            if nm:
                names[f"group_{gid}"] = nm
    except Exception:  # noqa: BLE001
        pass
    return names



def collect_memory_graph(limit_facts: int = 420) -> dict:
    """把长期记忆整理成"星系图"：作用域 → 主题 → 事实。

    前端只用节点/连线渲染，不做语义聚类（数据量小，主题本身就是天然簇）。
    """
    scope_label = {
        "global_learn": "自学池",
        "douyin": "抖音",
    }

    names = _scope_display()

    def label_of(scope: str) -> str:
        if scope in names:
            return names[scope]
        if scope.startswith("private_ou_"):
            return "飞书主人"
        if scope in scope_label:
            return scope_label[scope]
        if scope.startswith("group_"):
            return "群 " + scope.split("_", 1)[1][-6:]
        if scope.startswith("private_"):
            who = scope.split("_", 1)[1]
            return "私聊 " + ("主人" if who.isdigit() and len(who) <= 9 else who[-6:])
        return scope

    data = _read_json(ROOT / "memory" / "长期记忆.json")
    archive = (data or {}).get("archive") or {}
    nodes: list[dict] = []
    links: list[dict] = []
    seen_subject: set[str] = set()
    fact_total = 0
    scopes = sorted(archive.items(), key=lambda kv: len(kv[1] or []), reverse=True)

    for scope, items in scopes:
        items = [it for it in (items or []) if isinstance(it, dict) and it.get("fact")]
        if not items:
            continue
        sid = f"scope:{scope}"
        nodes.append({"id": sid, "label": label_of(scope), "kind": "scope",
                      "group": scope, "size": min(26.0, 10.0 + len(items) ** 0.5 * 3)})
        for it in items[: max(1, limit_facts)]:
            if fact_total >= limit_facts:
                break
            fid = f"fact:{scope}:{it.get('id')}"
            subject = str(it.get("subject") or "杂项")[:12]
            sub_id = f"subj:{scope}:{subject}"
            if sub_id not in seen_subject:
                seen_subject.add(sub_id)
                nodes.append({"id": sub_id, "label": subject, "kind": "subject",
                              "group": scope, "size": 7.5})
                links.append({"source": sub_id, "target": sid})
            try:
                weight = float(it.get("w") or 1.0)
            except Exception:  # noqa: BLE001
                weight = 1.0
            nodes.append({
                "id": fid,
                "label": str(it.get("fact"))[:60],
                "kind": "fact",
                "group": scope,
                "size": max(2.2, min(7.0, 2.2 + weight * 2.2)),
                "time": str(it.get("time") or ""),
            })
            links.append({"source": fid, "target": sub_id})
            fact_total += 1

    return {
        "nodes": nodes,
        "links": links,
        "labels": {**names, **{sc: label_of(sc) for sc, _ in scopes}},
        "stats": {
            "scopes": len({n["group"] for n in nodes if n["kind"] == "scope"}),
            "subjects": sum(1 for n in nodes if n["kind"] == "subject"),
            "facts": fact_total,
        },
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def collect_memory_detail(scope: str, limit: int = 24) -> dict:
    """某个作用域的详情：长期记忆 + 短期对话 + 记忆锚点。"""
    if not scope or len(scope) > 64:
        return {"error": "scope 不合法"}
    archive = (_read_json(ROOT / "memory" / "长期记忆.json") or {}).get("archive") or {}
    facts = [it for it in (archive.get(scope) or []) if isinstance(it, dict) and it.get("fact")]
    facts_sorted = sorted(facts, key=lambda it: float(it.get("w") or 0), reverse=True)

    def w_of(it):
        try:
            return float(it.get("w") or 0)
        except Exception:  # noqa: BLE001
            return 0.0

    anchors = [{"fact": str(it.get("fact"))[:120], "subject": str(it.get("subject") or ""),
                "time": str(it.get("time") or ""), "w": w_of(it)} for it in facts_sorted[:5]]

    recent: list[dict] = []
    try:
        with sqlite3.connect(str(DB_PATH), timeout=8) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "select timestamp, message, substr(reply,1,160) as reply from chat_history "
                "where scope=? order by rowid desc limit ?", (scope, limit)).fetchall()
            recent = [{"ts": r["timestamp"], "user": r["message"], "bot": r["reply"]} for r in rows]
    except Exception as exc:  # noqa: BLE001
        recent = [{"ts": "", "user": f"(读取失败: {exc})", "bot": ""}]

    return {
        "scope": scope,
        "label": ("飞书主人" if scope.startswith("private_ou_") else (_scope_display().get(scope) or scope)),
        "facts": [{"fact": str(it.get("fact"))[:160], "subject": str(it.get("subject") or ""),
                   "time": str(it.get("time") or ""), "w": w_of(it)} for it in facts_sorted[:80]],
        "facts_total": len(facts),
        "recent": recent,
        "anchors": anchors,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def collect_status() -> dict[str, Any]:
    env = _read_env()
    prices = {
        "in": _as_float(env.get("PRICE_IN"), 3.0),
        "out": _as_float(env.get("PRICE_OUT"), 9.0),
        "cache": _as_float(env.get("PRICE_CACHE"), 0.1),
    }
    ds_prices = {
        "in": _as_float(env.get("DEEPSEEK_PRICE_IN"), 2.0),
        "out": _as_float(env.get("DEEPSEEK_PRICE_OUT"), 8.0),
        "cache": _as_float(env.get("DEEPSEEK_PRICE_CACHE"), 0.5),
    }
    conn = _db_connect()
    db_stats = aggregate_memory_db(conn)
    token_stats = aggregate_token(conn, prices, ds_prices)
    balances = fetch_balances(env)
    if conn is not None:
        conn.close()

    long_data = _read_json(LONG_MEMORY_FILE)
    pet_data = _read_json(PET_STATE_FILE)
    mood_data = _read_json(MOOD_FILE)
    bili_data = _read_json(BILI_STATE_FILE)

    long_mem = aggregate_long_memory(long_data)
    pet = aggregate_pet_state(pet_data)
    pet["affinity"] = db_stats["affinity"]["score"]
    pet["tag"] = db_stats["affinity"]["tag"] or "还没定外号"

    memory = {
        "private": {
            **db_stats["private"],
            "archive": long_mem["private"],
        },
        "group": {
            **db_stats["group"],
            "archive": long_mem["group"],
        },
        "bili": db_stats["bili"],
        "learn": {
            **db_stats["learn"],
            "archive": long_mem["learn"],
        },
        "lexicon": db_stats["lexicon"],
        "diary": aggregate_diary(),
        "mood": aggregate_mood(mood_data),
    }

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "host": f"{HOST}:{PORT}",
        "services": detect_services(),
        "config": {
            "ai_model": env.get("AI_MODEL", "—"),
            "ai_base_url": env.get("AI_BASE_URL", "—"),
            "web_search": _bool(env.get("WEB_SEARCH_ENABLED"), False),
            "learn_enabled": _bool(env.get("LEARN_ENABLED"), False),
            "learn_times": env.get("LEARN_TIMES", "—"),
            "bili_watch": _bool(env.get("BILI_WATCH_ENABLED"), False),
            "bili_reply": _bool(env.get("BILI_WATCH_REPLY_ENABLED"), False),
            "diary_enabled": _bool(env.get("DAILY_DIARY_ENABLED"), False),
            "mood_enabled": bool(mood_data.get("enabled", True)),
        },
        "pet": pet,
        "memory": memory,
        "bili": aggregate_bili(bili_data),
        "douyin": aggregate_douyin(),
        "qzone": aggregate_qzone(),
        "emotion": aggregate_emotion(),
        "bili_daily": aggregate_bili_daily(),
        "sleep": aggregate_sleep(),
        "activity": aggregate_activity(),
        "local": aggregate_local(),
            "vision": aggregate_vision(),
        "token": token_stats,
        "balances": balances,
    }


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------
SAFE_ASSET = re.compile(r"^[A-Za-z0-9_\-\./]+$")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        return  # 静音访问日志

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, ctype: str) -> None:
        try:
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()

    def _read_body_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length) or b"{}") or {}
        except Exception:  # noqa: BLE001
            return {}

    def _memory_write(self, method: str, path: str) -> bool:
        """记忆可视化写操作：retention / merge / delete / growth。命中返回 True。"""
        import re as _re
        m = _re.match(r"^/api/memory/soul/(trait|rule|defense)/(\d+)/(solidify|dismiss)$", path)
        if m and method == "POST":
            try:
                from server import soul_aggregation as sa
                kind, idx, act = m.group(1), int(m.group(2)), m.group(3)
                r = sa.set_status(kind, idx, "solid" if act == "solidify" else "dismiss")
                if r is None:
                    self._send_json({"ok": False, "error": "该标签不存在"}, status=404)
                else:
                    self._send_json(r)
            except Exception as exc:  # noqa: BLE001
                self._send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=500)
            return True

        m = _re.match(r"^/api/memory/hormones/inject$", path)
        if m and method == "POST":
            from urllib.parse import parse_qs as _pq
            from urllib.parse import urlparse as _up
            q = _pq(_up(self.path).query)
            name = str((q.get("name") or ["DA"])[0]).upper()
            try:
                amount = float((q.get("amount") or ["20"])[0])
            except Exception:  # noqa: BLE001
                amount = 20.0
            try:
                import json as _j
                from pathlib import Path as _P
                f = _P(r"D:\qq-ai-bot") / "memory" / "hormones_state.json"
                d = _j.loads(f.read_text(encoding="utf-8")) if f.exists() else {"values": {}}
                v = d.setdefault("values", {})
                if name not in v:
                    self._send_json({"ok": False, "error": f"未知激素 {name}"}, status=400)
                    return True
                v[name] = max(0.0, min(100.0, float(v[name] or 0) + amount))
                d["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                f.write_text(_j.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
                self._send_json({"ok": True, "name": name, "value": round(v[name], 1)})
            except Exception as exc:  # noqa: BLE001
                self._send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=500)
            return True

        m = _re.match(r"^/api/memory/thoughts/clear$", path)
        if m and method == "POST":
            try:
                import json as _j
                from pathlib import Path as _P
                f = _P(r"D:\qq-ai-bot") / "memory" / "thoughts.json"
                d = _j.loads(f.read_text(encoding="utf-8")) if f.exists() else {"items": []}
                n = len(d.get("items") or [])
                d["items"] = []
                f.write_text(_j.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
                self._send_json({"ok": True, "cleared": n})
            except Exception as exc:  # noqa: BLE001
                self._send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=500)
            return True

        m = _re.match(r"^/api/memory/dream/force$", path)
        if m and method == "POST":
            # dashboard 进程没有 bot 的对话上下文：写一个"待做梦"标记，等 bot 侧接线后消费
            try:
                from pathlib import Path as _P
                f = _P(r"D:\qq-ai-bot") / "memory" / "dream_force.json"
                f.write_text('{"at": "%s"}' % time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
                self._send_json({"ok": True, "note": "已写强制入梦标记"})
            except Exception as exc:  # noqa: BLE001
                self._send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=500)
            return True

        m = _re.match(r"^/api/memory/character/extract$", path)
        if m and method == "POST":
            from urllib.parse import parse_qs as _pq
            from urllib.parse import urlparse as _up
            uid = (_pq(_up(self.path).query).get("uid") or [""])[0]
            if not uid:
                self._send_json({"ok": False, "error": "缺少 uid"}, status=400)
                return True
            try:
                import asyncio
                from server import character_cards as cc
                res = asyncio.run(cc.extract(uid))
                self._send_json(res, status=200 if res.get("ok") else 400)
            except Exception as exc:  # noqa: BLE001
                self._send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=500)
            return True
        m = _re.match(r"^/api/memory/growth/extract$", path)
        if m and method == "POST":
            try:
                import asyncio
                from server import memory_growth as mg
                from server import soul_aggregation as sa
                res = asyncio.run(mg.extract())
                # 手动「跑一次提炼」也跑三我聚合（和每晚 3:10 同一套逻辑），
                # 这样本我/自我/超我三个球才吃得到新词条
                try:
                    soul_res = asyncio.run(sa.aggregate(force=True, adopt=True))
                    res["soul"] = {k: soul_res.get(k) for k in ("id", "ego", "superego", "conflicts", "skipped", "reason")}
                except Exception as se:  # noqa: BLE001
                    res["soul_error"] = f"{type(se).__name__}: {se}"
                self._send_json(res)
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)
            return True
        m = _re.match(r"^/api/memory/growth/([^/]+)/solidify$", path)
        if m and method == "POST":
            from server import memory_growth as mg
            item = mg.solidify(m.group(1))
            if item:
                self._send_json(item, status=200)
            else:
                _it = mg.get(m.group(1))
                _why = (_it or {}).get("gate") or "没找到该成长项"
                self._send_json({"error": _why, "gate": bool(_it)}, status=409 if _it else 404)
            return True
        m = _re.match(r"^/api/memory/history$", path)
        if m and method == "GET":
            try:
                from urllib.parse import parse_qs, urlparse
                qs = parse_qs(urlparse(self.path).query)
                kind = (qs.get("kind") or ["growth"])[0]
                iid = (qs.get("id") or [""])[0]
                if kind == "memory":
                    from plugins import long_memory as lm
                    rows = lm.history_by_id(int(iid)) if iid.isdigit() else []
                else:
                    from server import memory_growth as mg
                    rows = mg.history(iid)
                self._send_json({"ok": True, "kind": kind, "id": iid, "rows": rows})
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)
            return True
        m = _re.match(r"^/api/memory/rollback$", path)
        if m and method == "POST":
            try:
                from urllib.parse import parse_qs, urlparse
                qs = parse_qs(urlparse(self.path).query)
                kind = (qs.get("kind") or ["growth"])[0]
                iid = (qs.get("id") or [""])[0]
                if kind == "memory":
                    from plugins import long_memory as lm
                    row = lm.rollback_by_id(int(iid)) if iid.isdigit() else None
                else:
                    from server import memory_growth as mg
                    row = mg.rollback(iid)
                self._send_json(row or {"error": "没有可回滚的修订"},
                                status=200 if row else 409)
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)
            return True
        m = _re.match(r"^/api/memory/growth/([^/]+)/dismiss$", path)
        if m and method == "POST":
            from server import memory_growth as mg
            item = mg.dismiss(m.group(1))
            self._send_json(item or {"error": "没找到该成长项"}, status=200 if item else 404)
            return True
        m = _re.match(r"^/api/room/shot$", path)
        if m and method == "POST":
            try:
                import base64 as _b64
                import time as _t
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                data = str(body.get("image") or "")
                if data.startswith("data:image/png;base64,"):
                    data = data[len("data:image/png;base64,"):]
                out = ROOT / "dashboard" / "webapp" / "room-shots"
                out.mkdir(parents=True, exist_ok=True)
                name = "room-" + _t.strftime("%Y%m%d-%H%M%S") + ".png"
                (out / name).write_bytes(_b64.b64decode(data))
                self._send_json({"ok": True, "path": str(out / name), "url": "/room-shots/" + name})
            except Exception as exc:  # noqa: BLE001
                self._send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=500)
            return True
        m = _re.match(r"^/api/memory/forget$", path)
        if m and method == "POST":
            import json as _json
            try:
                n = int(self.headers.get("Content-Length") or 0)
                payload = _json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            except Exception:  # noqa: BLE001
                payload = {}
            from server import memory_viz_persona as _mvp
            ids = payload.get("ids") or ([payload["id"]] if payload.get("id") else [])
            added = _mvp.mark_forgotten([str(x) for x in ids])
            self._send_json({"ok": True, "added": added, "total": len(_mvp.forgotten_ids())})
            return True

        m = _re.match(r"^/api/memory/node/([^/]+)/retention$", path)
        if m and method == "POST":
            payload = self._read_body_json()
            self._send_json(memory_viz.set_retention(unquote(m.group(1)), float(payload.get("retentionScore") or 0)))
            return True
        m = _re.match(r"^/api/memory/node/([^/]+)/repair$", path)
        if m and method == "POST":
            payload = self._read_body_json()
            node_id = unquote(m.group(1))   # 前端 encodeURIComponent 过，必须解码
            subject = str(payload.get("subject") or "")
            scope = str(payload.get("scope") or "")
            mode = str(payload.get("mode") or "quick")
            minutes = float(payload.get("minutes") or 10)

            # 节点 id 是 "trauma:{索引}" —— 索引不是 scope，必须按索引去库里取真实条目
            if not subject and node_id.startswith("trauma:"):
                try:
                    import json as _j
                    from pathlib import Path as _P
                    data = _j.loads((_P(r"D:\qq-ai-bot") / "memory" / "trauma.json").read_text(encoding="utf-8-sig"))
                    items = [x for x in (data.get("items") or []) if isinstance(x, dict)]
                    idx = int(node_id.split(":", 1)[1])
                    if 0 <= idx < len(items):
                        subject = str(items[idx].get("subject") or "")
                        scope = str(items[idx].get("scope") or "")
                except Exception:  # noqa: BLE001
                    pass

            # scope 可能为空（「主人」那两条没有 scope 字段），只要求 subject 有值
            if not subject:
                self._send_json({"ok": False, "error": f"没找到创伤条目（id={node_id}）"})
                return True

            import importlib.util as _iu
            spec = _iu.spec_from_file_location("trauma_mem", os.path.join(r"D:\qq-ai-bot", "plugins", "trauma_mem.py"))
            mod = _iu.module_from_spec(spec); spec.loader.exec_module(mod)  # type: ignore[union-attr]
            _idx = None
            try:
                if node_id.startswith("trauma:"):
                    _idx = int(node_id.split(":", 1)[1])
            except ValueError:
                _idx = None
            res = mod.start_repair(scope or None, subject, mode, minutes, _idx)
            # 修复成功 → 补上激素事件 repair（血清素↑、皮质醇↓）
            if res:
                try:
                    import importlib.util as _iu2
                    _sp = _iu2.spec_from_file_location("hormones_dash", os.path.join(r"D:\qq-ai-bot", "plugins", "hormones.py"))
                    _hm = _iu2.module_from_spec(_sp); _sp.loader.exec_module(_hm)  # type: ignore[union-attr]
                    _hm.apply_event("repair", 1.0)
                except Exception:  # noqa: BLE001
                    pass
            self._send_json(res or {"ok": False, "error": "没找到对应的创伤条目"})
            return True
        m = _re.match(r"^/api/memory/node/([^/]+)/merge$", path)
        if m and method == "POST":
            payload = self._read_body_json()
            self._send_json(memory_viz.merge_nodes(unquote(m.group(1)), str(payload.get("targetId") or "")))
            return True
        m = _re.match(r"^/api/memory/node/([^/]+)$", path)
        if m and method == "DELETE":
            force = "force=1" in self.path
            self._send_json(memory_viz.delete_node(m.group(1), force=force))
            return True
        return False

    def do_DELETE(self) -> None:
        path = self.path.split("?", 1)[0]
        try:
            if self._memory_write("DELETE", path):
                return
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)
            return
        self.send_response(404); self.end_headers()

    def do_POST(self) -> None:
        if not self._check_token():
            return
        path = self.path.split("?", 1)[0]
        try:
            if self._memory_write("POST", path):
                return
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)
            return
        if path == "/api/qzone_toggle":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                state_path = ROOT / "memory" / "qzone_sync.json"
                state = _read_json(state_path)
                if not isinstance(state, dict):
                    state = {}
                state["enabled"] = bool(payload.get("enabled"))
                state.setdefault("last_post", "")
                state.setdefault("history", [])
                state_path.parent.mkdir(parents=True, exist_ok=True)
                # 原子写：先写 .tmp 再 os.replace，避免 bot 侧读到半截 JSON
                # （审计 C：面板与 qzone_sync 会抢同一个文件）
                _tmp = state_path.with_suffix(".json.tmp")
                _tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(_tmp, state_path)
                self._send_json({"ok": True, "enabled": state["enabled"]})
            except Exception as exc:  # noqa: BLE001
                self._send_json({"ok": False, "error": str(exc)}, status=500)
        else:
            self.send_response(404)
            self.end_headers()

    # ---------------- token 门禁（手机访问用） ----------------
    def _check_token(self) -> bool:
        """返回 True 表示放行；False 表示已经回了 401。"""
        if not TOKEN:
            return True
        try:
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(self.path).query)
            if (qs.get("k") or [""])[0] == TOKEN:
                return True
            cookie = self.headers.get("Cookie") or ""
            for part in cookie.split(";"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    if k.strip() == "dafish_token" and v.strip() == TOKEN:
                        return True
        except Exception:  # noqa: BLE001
            pass
        body = ("<!doctype html><meta charset=utf-8><title>需要口令</title>"
                "<body style='background:#0b1a2e;color:#cfe2f5;font:16px/1.7 system-ui;padding:48px'>"
                "<h2>面板需要口令</h2>"
                "<p>在网址后面加上 <code>?k=你的口令</code> 打开一次，之后这台设备就记住了。</p>"
                "<p>口令在电脑上：<code>D:/qq-ai-bot/memory/dashboard_token.txt</code></p></body>")
        raw = body.encode("utf-8")
        self.send_response(401)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
        return False

    def do_GET(self) -> None:
        if not self._check_token():
            return
        # 带了 ?k= 且校验通过：种 cookie 后把 k 从地址里去掉，免得一直挂在 URL 上
        if TOKEN and "k=" in self.path:
            try:
                # 别名导入：直接 import 这几个名字会把它们变成 do_GET 的局部变量，
                # 后面别处再用 parse_qs 就会 UnboundLocalError（2026-11-04 踩过）
                from urllib.parse import parse_qs as _pq, urlparse as _up
                from urllib.parse import urlunparse as _uup, urlencode as _ue
                u = _up(self.path)
                qs = _pq(u.query)
                if (qs.get("k") or [""])[0] == TOKEN:
                    qs.pop("k", None)
                    newq = _ue({kk: vv[0] for kk, vv in qs.items()})
                    self.send_response(302)
                    self.send_header("Location", _uup(u._replace(query=newq)))
                    self.send_header("Set-Cookie", f"dafish_token={TOKEN}; Path=/; Max-Age=2592000")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    return
            except Exception:  # noqa: BLE001
                pass
        path = self.path.split("?", 1)[0]
        has_v = "v=" in self.path
        if path in ("/", "/index.html"):
            # 没有版本参数就先重定向到 ?v=<index.html 修改时间>：
            # 浏览器会缓存渲染结果，URL 不变就可能一直是旧页面（"启动后还是旧网站"就是这个）。
            if not has_v:
                try:
                    stamp = int((STATIC_DIR / "index.html").stat().st_mtime)
                except Exception:  # noqa: BLE001
                    stamp = 0
                self.send_response(302)
                self.send_header("Location", f"/?v={stamp}")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        elif path == "/api/voice/list":
            try:
                self._send_json({"files": latest_voice_files(14)})
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)
        elif path in ("/api/voice/spectrogram", "/api/voice/waveform"):
            # 声音 → 图形：?f=文件名，不带就取最新一条
            try:
                _qs = parse_qs(urlparse(self.path).query)
                _f = (_qs.get("f") or [""])[0]
                if _f:
                    _p = TTS_DIR / Path(_f).name      # 只允许本目录内的文件名
                else:
                    _cands = sorted(TTS_DIR.glob("*.mp3"),
                                    key=lambda x: x.stat().st_mtime, reverse=True)
                    _p = _cands[0] if _cands else None
                if not _p or not _p.exists():
                    self._send_json({"error": "没有可用的语音文件"}, status=404)
                    return
                _png = (render_spectrogram(_p) if path.endswith("spectrogram")
                        else render_waveform(_p))
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(_png)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(_png)
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)
        elif path == "/api/status":
            try:
                self._send_json(collect_status())
            except Exception as exc:  # 面板不应因单点异常整页 500
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)
        elif path == "/api/room":
            try:
                self._send_json(aggregate_room())
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)
        elif path == "/api/needs":
            # 2026-11-07：马斯洛五层（面板「精神」那栏用）。
            # 缺多少 → 面板显示成「满足度」（100 - 缺），因为条越满越好看。
            try:
                import sys as _s
                _s.path.insert(0, str(ROOT))
                from plugins import hierarchy as _hi
                n = _hi.needs()
                w = _hi.weights()
                name, v = _hi.top()
                # 2026-11-07：身体那栏加了第 4 项「情欲」——
                # 精神栏有 4 项，身体只有 3 项，看着不平衡。
                # 情欲是她真实有的维度（7 情绪里就有），而且弗洛伊德把 libido 算在身体那边。
                # 注意语义不同：饱食/精力/睡眠「越高越好」，情欲「越高越想要」——
                # 所以前端用不同颜色 + 不同标签，别混进「满足度」那个口径。
                _aro = 0
                try:
                    from plugins import hormones as _hm
                    _aro = round(float(_hm.derive_emotions().get("情欲") or 0))
                except Exception:  # noqa: BLE001
                    pass
                self._send_json({
                    "arousal": _aro,
                    # _亲密 是私聊那条通道（下沉在 needs() 里），面板单独一栏
                    "satisfy": {("亲密" if k == "_亲密" else k): round((1.0 - float(x)) * 100)
                                for k, x in n.items() if not str(k).startswith("_") or k == "_亲密"},
                    "need": n,
                    "weights": w,
                    "top": {"name": name, "urgency": v},
                    "mode": _hi.d_or_b(),
                    "line": _hi.line(),
                })
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)
        elif path == "/api/health":
            # 2026-11-07：健康灯。
            #   · 大头（走样/召回/token/人格/语法/链路/检索）读 memory/health.json —— 每 3 小时由
            #     plugins/health.py 更新，不能每次请求都跑（那要 6 秒）。
            #   · **「自检」这一项实时跑** —— 只要 166 ms，但能看到「此刻有几个文件未提交」。
            #     之前是 3 小时的滞后，主人看到「有一个未提交」时其实早就提交掉了。
            try:
                import sys as _s
                _s.path.insert(0, str(ROOT))
                from plugins import health as _hl
                d = _hl.read()
                _live = None
                try:
                    from plugins import self_check as _sc
                    _s0 = _sc.status()
                    _live = {
                        "status": "ok" if _s0.get("ok") else "warn",
                        "msg": (f"{_s0.get('lag_hours', {})}" and
                                f"档案 {_s0['counts'].get('长期档案')} · 成长 {_s0['counts'].get('人格成长')}"
                                + (f" · {'；'.join(_s0['issues'])}" if _s0.get("issues") else "")),
                        "live": True,
                    }
                    d.setdefault("checks", {})["自检"] = _live
                    # 实时那项有问题 → 整体灯要跟着变，否则「自检红、灯绿」更让人糊涂
                    if _live["status"] != "ok":
                        if d.get("status") == "ok":
                            d["status"] = "warn"
                        d["issues"] = list(d.get("issues") or []) + [
                            x for x in (_s0.get("issues") or []) if x not in (d.get("issues") or [])
                        ]
                        d["issues"] = d["issues"][:8]
                except Exception:  # noqa: BLE001
                    pass
                d["self_check_live"] = _live is not None
                self._send_json(d)
            except Exception as exc:  # noqa: BLE001
                self._send_json({"status": "never", "error": f"{type(exc).__name__}: {exc}"}, status=500)
        elif path == "/api/memory/git":
            # 2026-11-07：记忆版本（搬自 Tobs 的「记忆就是一个 Git 仓库」）
            #   ?n=12       最近 n 次提交
            #   ?ref=hash   看某次提交的 diff
            #   ?file=路径   限定某个文件
            try:
                import sys as _s
                _s.path.insert(0, str(ROOT))
                from plugins import memory_git as _mg
                qs = parse_qs(urlparse(self.path).query)
                n = int((qs.get("n") or ["12"])[0])
                ref = (qs.get("ref") or [""])[0]
                fpath = (qs.get("file") or [""])[0]
                if ref:
                    self._send_json({
                        "ref": ref,
                        "stat": _mg.stat(ref),
                        "diff": _mg.diff(ref, fpath),
                    })
                else:
                    self._send_json({
                        "stats": _mg.stats(),
                        "commits": _mg.history(n, fpath),
                    })
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)
        elif path == "/api/memory/scopes":
            try:
                self._send_json(memory_viz.scopes())
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)
        elif path == "/api/memory/tree":
            try:
                qs = parse_qs(urlparse(self.path).query)
                scope = (qs.get("scope") or [""])[0]
                if not scope:
                    self._send_json({"error": "scope 必填"}, status=400); return
                if scope == "__all__":
                    self._send_json(memory_viz_all.tree_all()); return
                self._send_json(memory_viz.tree(scope))
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)
        elif path == "/api/memory/starfield":
            try:
                qs = parse_qs(urlparse(self.path).query)
                scope = (qs.get("scope") or [""])[0]
                if not scope:
                    self._send_json({"error": "scope 必填"}, status=400); return
                if scope == "__all__":
                    code, payload = memory_viz_all.starfield_all()
                else:
                    code, payload = memory_viz.starfield(scope)
                self._send_json(payload, status=code)
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)
        elif path == "/api/memory/pool":
            # 纯记忆池：打破群边界，按激素/类型聚簇（创伤左上 / 开心右上 / 长期中心 / 短期边缘）
            try:
                from server import memory_viz_all as mva
                self._send_json(mva.memory_pool())
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)
        elif path == "/api/memory/entity-tree":
            # 实体树：按分类组装（人格/人物卡/长期/经历/念头/梦境）
            try:
                qs = parse_qs(urlparse(self.path).query)
                scope = (qs.get("scope") or [""])[0] or ""
                from server import memory_entity as me
                self._send_json(me.entity_tree(scope))
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)
        elif path == "/api/memory/flow":
            try:
                _q = parse_qs(urlparse(self.path).query)
                _at = _q.get("at")
                at_ms = None
                if _at:
                    try:
                        at_ms = int(float(_at[0]))
                    except ValueError:
                        at_ms = None
                from server import memory_flow as mf
                _stress = (_q.get("stress") or [""])[0] in ("1", "true", "yes")
                self._send_json(mf.flow_frame(at_ms, stress=_stress))
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)

        elif path == "/api/memory/persona":
            try:
                qs = parse_qs(urlparse(self.path).query)
                scope = (qs.get("scope") or [""])[0] or None
                from server import memory_viz_persona as mvp
                _q = parse_qs(urlparse(self.path).query)
                _args = {}
                for _k in ("shortR", "planetR", "dustR"):
                    _v = _q.get(_k)
                    if _v:
                        try:
                            _args[_k] = max(0.01, min(3.0, float(_v[0])))
                        except ValueError:
                            pass
                # at = 时间指针（毫秒）。拖动历史滑块时传进来，
                # 后端按「那一刻」计算每条记忆的生命进度 → 粒子才会真的流动。
                _at = _q.get("at")
                if _at:
                    try:
                        _args["at_ms"] = int(float(_at[0]))
                    except ValueError:
                        pass
                self._send_json(mvp.persona_view(scope, **_args))
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)
        elif path == "/api/memory/soul/history":
            # 人格演化史：在册词条 + 成长时间线 + 候选池进场时间
            try:
                from server import memory_viz_persona as mvp
                self._send_json(mvp.soul_history())
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)
        elif path == "/api/memory/soul/export":
            # 导出人格档案（浏览器直接下载 JSON）
            try:
                import json as _json
                from server import memory_viz_persona as mvp
                body = _json.dumps(mvp.soul_export(), ensure_ascii=False, indent=2).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Disposition",
                                 'attachment; filename="persona_export.json"')
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)
        elif path == "/api/memory/hormones":
            # 内分泌仪表盘：把 0~100 的激素值归一成 0~1，给顶部进度条用
            try:
                import json as _j
                from pathlib import Path as _P
                _root = _P(r"D:\qq-ai-bot")
                _st = {}
                try:
                    _st = _j.loads((_root / "memory" / "hormones_state.json").read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    _st = {}
                _vals = (_st.get("values") or {})
                _norm = {k: round(float(_vals.get(k) or 0) / 100.0, 3)
                         for k in ("DA", "CORT", "SER", "OXY", "MEL")}
                # 创伤状态：看创伤库里有没有高电荷且未修复的条目
                _tr = "calm"
                try:
                    _t = _j.loads((_root / "memory" / "trauma.json").read_text(encoding="utf-8-sig"))
                    _hot = [x for x in (_t.get("items") or [])
                            if isinstance(x, dict) and float(x.get("charge") or 0) >= 0.5]
                    if _hot:
                        _tr = "anxious" if _norm.get("CORT", 0) >= 0.5 else "guarded"
                except Exception:  # noqa: BLE001
                    pass
                # 派生情绪（和 bot 里同一套口径）
                _emo = {}
                try:
                    _h = _norm
                    _emo = {
                        "开心": round(min(1.0, max(0.0, (_h["DA"] + _h["SER"]) / 2)), 3),
                        "烦": round(min(1.0, max(0.0, (_h["CORT"] - _h["SER"]) / 2)), 3),
                        "累": round(min(1.0, max(0.0, _h["MEL"] - _h["DA"] / 2)), 3),
                        "害怕": round(min(1.0, max(0.0, (_h["CORT"] - _h["OXY"]) / 2)), 3),
                    }
                except Exception:  # noqa: BLE001
                    _emo = {}
                self._send_json({
                    "hormones": _norm,
                    "raw": {k: round(float(_vals.get(k) or 0), 1) for k in ("DA", "CORT", "SER", "OXY", "MEL")},
                    "traumaState": _tr,
                    "emotions": _emo,
                    "updatedAt": _st.get("updated_at") or "",
                })
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)
        elif path == "/api/memory/history":
            try:
                from urllib.parse import parse_qs as _pq, urlparse as _up
                qs = _pq(_up(self.path).query)
                kind = (qs.get("kind") or ["growth"])[0]
                iid = (qs.get("id") or [""])[0]
                if kind == "memory":
                    from plugins import long_memory as lm
                    try:
                        lm._load()   # 面板进程常驻：读盘刷新，别用启动时的旧档案
                    except Exception:  # noqa: BLE001
                        pass
                    rows = lm.history_by_id(int(iid)) if iid.isdigit() else []
                else:
                    from server import memory_growth as mg
                    rows = mg.history(iid)
                self._send_json({"ok": True, "kind": kind, "id": iid, "rows": rows})
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)
        elif path == "/api/memory/detail":
            try:
                qs = parse_qs(urlparse(self.path).query)
                node_id = (qs.get("nodeId") or [""])[0]
                if not node_id:
                    self._send_json({"error": "nodeId 必填"}, status=400); return
                from server import memory_viz_persona as mvp
                data = mvp.persona_detail(node_id) or memory_viz.detail(node_id)
                if not data and node_id.startswith("pool:"):
                    # 2026-11-06：新加的池子（人物池/事件池/反馈闭环/行动空间/执念池）
                    from server import memory_viz_all as mva
                    data = mva.pool_detail(node_id)
                if not data:
                    self._send_json({"error": "节点不存在"}, status=404); return
                self._send_json(data)
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)
        elif path == "/api/memory_graph":
            try:
                self._send_json(collect_memory_graph())
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)
        elif path.startswith("/api/memory_detail"):
            try:
                qs = parse_qs(urlparse(self.path).query)
                scope = (qs.get("scope") or [""])[0]
                self._send_json(collect_memory_detail(scope))
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)
        elif path == "/api/persona":
            try:
                text = (ROOT / "persona.md").read_text(encoding="utf-8")
                self._send_json({"persona": text, "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": str(exc)}, status=500)
        elif path in ("/memory-viz", "/memory-viz/"):
            target = STATIC_DIR / "webapp" / "index.html"
            if not target.exists():
                self._send_json({"error": "webapp 未构建（cd dashboard-web && npm run build）"}, status=404)
                return
            self._send_file(target, "text/html; charset=utf-8")
        elif path.startswith("/memory-viz/"):
            rel = path[len("/memory-viz/"):] or "index.html"
            if ".." in rel:
                self.send_response(404); self.end_headers(); return
            f = STATIC_DIR / "webapp" / rel
            if not f.is_file():
                self.send_response(404); self.end_headers(); return
            ctype = {
                ".html": "text/html; charset=utf-8",
                ".js": "application/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".svg": "image/svg+xml",
                ".png": "image/png",
                ".json": "application/json; charset=utf-8",
                ".ico": "image/x-icon",
            }.get(f.suffix.lower(), "application/octet-stream")
            self._send_file(f, ctype)
        elif path.startswith("/assets/"):
            rel = path[len("/assets/"):]
            if not SAFE_ASSET.match(rel) or ".." in rel:
                self.send_response(403)
                self.end_headers()
                return
            target = (ASSETS_DIR / rel).resolve()
            if ASSETS_DIR not in target.parents or not target.is_file():
                self.send_response(404)
                self.end_headers()
                return
            ctype_map = {
                ".png": "image/png", ".gif": "image/gif",
                ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".svg": "image/svg+xml", ".webp": "image/webp",
            }
            ctype = ctype_map.get(target.suffix.lower(), "application/octet-stream")
            self._send_file(target, ctype)
        else:
            self.send_response(404)
            self.end_headers()


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[dashboard] listening on http://{HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
