"""一次性补丁：给 ai_group 加「游戏解说」定时任务。

用法：python tools/patch_pvz_narrate.py
"""
import pathlib
import sys

P = pathlib.Path(r"D:\qq-ai-bot\plugins\ai_group.py")
ANCHOR = '@scheduler.scheduled_job("interval", seconds=3, id="learn_receipt",'

BLOCK = '''# ===== 游戏解说：读 tools/pvz_live.py 写的事件流，让她用自己的口吻播报 =====
_PVZ_EVENT_FILE = ROOT / "memory" / "pvz_events.jsonl"
_pvz_offset = {"pos": 0}


@scheduler.scheduled_job("interval", seconds=3, id="pvz_narrate",
                         misfire_grace_time=30, coalesce=True)
async def _pvz_narrate_tick() -> None:
    """游戏事件 -> 她生成一句解说 -> 私聊主人。"""
    try:
        if not _PVZ_EVENT_FILE.exists():
            return
        _sz = _PVZ_EVENT_FILE.stat().st_size
        if _sz <= _pvz_offset["pos"]:
            return
        with _PVZ_EVENT_FILE.open("r", encoding="utf-8") as f:
            f.seek(_pvz_offset["pos"])
            _lines = f.readlines()
            _pvz_offset["pos"] = f.tell()
        _events = []
        for _ln in _lines[-6:]:
            try:
                _events.append(json.loads(_ln))
            except Exception:  # noqa: BLE001
                continue
        if not _events:
            return
        _text = " ; ".join(f"{e.get('kind')}:{e.get('text')}" for e in _events)
        _prompt = (
            "你在玩植物大战僵尸，正在直播。下面是刚才发生的游戏事件：\\n"
            f"{_text}\\n"
            "用「蓝色大肥鱼」的口吻说一句 15~35 字的实时解说：傲娇、嘴损、有点自恋，\\n"
            "可以吐槽僵尸、夸自己、喊主人看。不要括号动作，不要解释，只输出这一句。"
        )
        _say = await call_ai_raw(_prompt, "deepseek", temperature=0.95, max_tokens=120)
        _say = (_say or "").strip()
        if not _say:
            return
        _bot = get_bot()
        await _bot.send_private_msg(user_id=int(YOUR_QQ_ID), message="🎮 " + _say[:120])
        logger.info(f"[游戏解说] {_say[:60]}")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[游戏解说] 失败：{type(e).__name__}: {e}")


'''


def main() -> int:
    src = P.read_text(encoding="utf-8")
    if "_pvz_narrate_tick" in src:
        print("已存在，跳过")
        return 0
    if ANCHOR not in src:
        print("锚点没找到")
        return 1
    src = src.replace(ANCHOR, BLOCK + ANCHOR, 1)
    P.write_text(src, encoding="utf-8")
    print("补丁完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
