import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\plugins\game_watch.py")
s = p.read_text(encoding="utf-8")

if "game_watch_cmd.json" in s:
    print("已支持面板控制")
    raise SystemExit

patch = '''

# ---------------------------------------------------------------- 面板控制（文件指令）
# 启动器面板按"陪游戏"时写指令文件，这里每 5 秒读一次执行；
# 执行结果写回状态文件，面板显示"在陪/没陪"。
_CMD_FILE = ROOT / "memory" / "game_watch_cmd.json"
_STATE_FILE = ROOT / "memory" / "game_watch_state.json"


def _write_state(active: bool, note: str = "") -> None:
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        import json as _json
        from datetime import datetime as _dt
        _STATE_FILE.write_text(_json.dumps({
            "active": bool(active),
            "note": note[:60],
            "updated_at": _dt.now().strftime("%Y-%m-%d %H:%M:%S"),
        }, ensure_ascii=False), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[看游戏] 写状态失败：{type(e).__name__}: {e}")


def _read_cmd() -> str:
    try:
        import json as _json
        if not _CMD_FILE.exists():
            return ""
        data = _json.loads(_CMD_FILE.read_text(encoding="utf-8")) or {}
        cmd = str(data.get("cmd") or "").strip().lower()
        _CMD_FILE.unlink(missing_ok=True)   # 读一次就删，避免重复执行
        return cmd
    except Exception:  # noqa: BLE001
        return ""


@watch_cmd.scheduled_job if False else (lambda f: f)
def _noop():
    pass


def _start_watch(note: str = "") -> str:
    global _watch_task
    if _watch_task and not _watch_task.done():
        return "already"
    _watch_task = asyncio.get_event_loop().create_task(_watch_loop())
    _write_state(True, note or "panel")
    return "started"


def _stop_watch() -> str:
    global _watch_task, _watch_stop
    _watch_stop = True
    if _watch_task and not _watch_task.done():
        _watch_task.cancel()
    _watch_task = None
    _write_state(False, "stopped")
    return "stopped"


'''
# 插入到"面板控制"之前的触发区之前（放在 watch_cmd 定义之后更安全）
marker = "watch_cmd = on_message(priority=6, block=False)"
i = s.find(marker)
if i > 0:
    s = s[:i] + patch.lstrip("\n") + "\n" + s[i:]

p.write_text(s, encoding="utf-8")
print("已插入面板控制骨架（下一步再接调度）")
