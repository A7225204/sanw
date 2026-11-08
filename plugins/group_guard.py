"""群发言权限：默认只在她被主人批准过的群里说话。

- 启动时把机器人当前所在的群登记为「已允许」（首次运行）；
- 之后被拉进新群 → 标记 pending，私聊通知主人，主人在私聊里 `允许群 X` 才会开口；
- 主人可以随时 `禁止群 X`，她在那个群完全静默（连 @ 也不回）。

状态文件：memory/group_permissions.json
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import nonebot

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "memory" / "group_permissions.json"

_state: dict | None = None
_state_mtime: float = 0.0


def _load() -> dict:
    global _state, _state_mtime
    try:
        mtime = STATE_FILE.stat().st_mtime
    except OSError:
        mtime = 0.0
    if _state is not None and mtime == _state_mtime:
        return _state
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("groups"), dict):
            _state = data
            _state_mtime = mtime
            return _state
    except Exception:
        pass
    _state = {"groups": {}, "updated_at": ""}
    _state_mtime = mtime
    return _state


def _save() -> None:
    global _state_mtime
    data = _load()
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            _state_mtime = STATE_FILE.stat().st_mtime
        except OSError:
            _state_mtime = 0.0
    except Exception as e:  # noqa: BLE001
        nonebot.logger.warning(f"[群权限] 保存失败：{e}")


def exists(gid: int | str) -> bool:
    return str(gid) in _load()["groups"]


def is_allowed(gid: int | str) -> bool:
    groups = _load()["groups"]
    entry = groups.get(str(gid))
    if entry is None:
        # 没登记过 = 不放行（原来空表时整体放行，等于"登记一失败所有群都能说话"）。
        # 首次启动的空窗由 install_bootstrap 的 ensure_existing 补登记，
        # 真登记不上时宁可静音，也不要把被关掉的群放出来。
        return False
    return bool(entry.get("allowed"))


def needs_notice(gid: int | str) -> bool:
    """只有“从没见过的新群”才需要问主人；已拒绝/已待定的群不再重复问。"""
    groups = _load()["groups"]
    if not groups:
        return False
    return str(gid) not in groups


def mark_pending(gid: int | str, name: str = "") -> None:
    groups = _load()["groups"]
    entry = groups.setdefault(str(gid), {"allowed": False})
    entry.update({"name": name or entry.get("name", ""), "pending": True,
                  "allowed": False, "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    _save()


def allow(gid: int | str, name: str = "") -> None:
    groups = _load()["groups"]
    entry = groups.setdefault(str(gid), {})
    entry.update({"allowed": True, "pending": False,
                  "name": name or entry.get("name", ""),
                  "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    _save()


def deny(gid: int | str, name: str = "") -> None:
    groups = _load()["groups"]
    entry = groups.setdefault(str(gid), {})
    entry.update({"allowed": False, "pending": False,
                  "name": name or entry.get("name", ""),
                  "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    _save()


def note_message(gid: int | str) -> None:
    """黑名单群只记消息条数，不读内容、不进记忆。"""
    groups = _load()["groups"]
    entry = groups.get(str(gid))
    if entry is None:
        return
    entry["msg_count"] = int(entry.get("msg_count", 0)) + 1
    entry["last_msg_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save()


def forget(gid: int | str) -> None:
    """退群后清掉记录，下次再被拉进来会重新问主人。"""
    groups = _load()["groups"]
    groups.pop(str(gid), None)
    _save()


def ensure_existing(group_list: list[dict]) -> None:
    """首次运行时，把机器人当前所在的群登记为「待主人确认」还是「允许」？

    原来一律登记 allowed=True —— 只要 bot 被拉进某个群，它立刻就能说话，
    等于绕过了「新群要主人批准」。现在改为：
      - 白名单 env ALLOW_GROUPS 里明确列出的群 → allowed=True
      - 其余新群 → allowed=False + pending=True（等主人批准）
    """
    groups = _load()["groups"]
    changed = False
    import os as _os
    _allow_env = {x.strip() for x in (_os.getenv("ALLOW_GROUPS") or "").split(",") if x.strip()}
    for g in group_list:
        gid = str(g.get("group_id") or "")
        if not gid or gid in groups:
            continue
        _ok = gid in _allow_env
        groups[gid] = {"name": g.get("group_name") or "", "allowed": _ok,
                       "pending": not _ok,
                       "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        changed = True
    if changed:
        _save()


def all_groups() -> list[dict]:
    return [
        {"gid": gid, **info}
        for gid, info in _load()["groups"].items()
    ]


def last_pending() -> dict | None:
    pend = [g for g in all_groups() if g.get("pending")]
    if not pend:
        return None
    pend.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    return pend[0]


async def resolve_group(text: str, bot=None) -> tuple[int | None, str]:
    """把“群号 / 群名 / 那个群”解析成 (gid, name)。"""
    text = (text or "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 5:
        return int(digits), ""

    if any(k in text for k in ("那个群", "这个群", "刚才的群", "新群")):
        pend = last_pending()
        if pend:
            return int(pend["gid"]), pend.get("name", "")
        groups = all_groups()
        if groups:
            groups.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
            return int(groups[0]["gid"]), groups[0].get("name", "")

    keywords = [text] + [w for w in text.replace("群", " ").split() if len(w) >= 2]
    for g in all_groups():
        name = g.get("name") or ""
        if name and any(k and k in name for k in keywords):
            return int(g["gid"]), name

    if bot is not None:
        try:
            remote = await bot.get_group_list()
        except Exception:
            remote = []
        for g in remote:
            name = g.get("group_name") or ""
            if name and any(k and k in name for k in keywords):
                return int(g["group_id"]), name
    return None, ""


def install_bootstrap() -> None:
    """启动后把现有群登记为允许（只做一次；已登记的不动）。"""
    try:
        driver = nonebot.get_driver()

        async def _bootstrap_task() -> None:
            await asyncio.sleep(8)
            for _ in range(10):
                try:
                    bot = nonebot.get_bot()
                    groups = await bot.get_group_list()
                    ensure_existing(groups)
                    nonebot.logger.info(f"[群权限] 已登记 {len(all_groups())} 个群")
                    return
                except Exception as e:  # noqa: BLE001
                    nonebot.logger.warning(
                        f"[群权限] 登记失败（第 {_ + 1}/10 次）：{type(e).__name__}: {e}；"
                        "此时未登记的群按拒绝处理"
                    )
                    await asyncio.sleep(6)
            nonebot.logger.error(
                "[群权限] 10 次登记全部失败：未登记的群一律拒绝发言，请检查 NapCat 连接后重启"
            )

        @driver.on_startup
        async def _bootstrap_groups() -> None:
            # 不要 await：等 NapCat 连接会拖慢 uvicorn 启动
            asyncio.create_task(_bootstrap_task())

        @driver.on_bot_connect
        async def _bootstrap_on_connect(bot) -> None:
            # QQ 掉线重登后补登记：启动时 NapCat 还没连上就会错过那 60 秒窗口
            try:
                groups = await bot.get_group_list()
                ensure_existing(groups)
                nonebot.logger.info(f"[群权限] 连接后补登记，当前共 {len(all_groups())} 个群")
            except Exception as e:  # noqa: BLE001
                nonebot.logger.warning(f"[群权限] 连接后补登记失败：{type(e).__name__}: {e}")
    except Exception:
        pass


install_bootstrap()
