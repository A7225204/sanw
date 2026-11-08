"""删除小游戏（签到/赛马/决斗/查成分/逮捕/恶搞/桌宠/抽卡/轮盘）的死代码。

策略：按已算好的行区间从后往前删，删完编译验证；失败直接还原。
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess
import time

SRC = pathlib.Path(r"D:\qq-ai-bot\plugins\ai_group.py")
PY = r"D:\qq-ai-bot\.venv\Scripts\python.exe"

# (起始行, 结束行) 1-based，已确认块边界
BLOCKS = [
    (7438, 7443),   # 桌宠
    (7399, 7404),   # 赛马
    (7323, 7357),   # 群聊 逮捕/恶搞
    (7247, 7259),   # 查成分
    (7232, 7245),   # 决斗
    (7224, 7230),   # 签到
    (6256, 6285),   # 私聊 逮捕/恶搞
]

# 比赛播报后台任务（赛马专用）
EXTRA_PATTERNS = (
    'async def _race_announce(',
    'async def _horse_race(',
)


def compile_ok() -> bool:
    r = subprocess.run([PY, "-m", "py_compile", str(SRC)], capture_output=True, text=True)
    return r.returncode == 0


def main() -> int:
    backup = SRC.with_name(f"ai_group.py.bak_games_{time.strftime('%Y%m%d_%H%M')}")
    shutil.copy2(SRC, backup)
    lines = SRC.read_text(encoding="utf-8").splitlines(keepends=True)

    removed = 0
    for start, end in BLOCKS:
        del lines[start - 1:end]
        removed += end - start + 1
    text = "".join(lines)

    # 门禁块（小游戏已关闭 → 直接 return）也去掉，让这些词回到正常聊天
    text = text.replace(
        '''    if (user_msg.split() or [""])[0] in GAMES_DISABLED:
        logger.info(f"[小游戏已关闭] 私聊 uid={user_id} cmd={user_msg.split()[0]}")
        return

''', "")

    text = text.replace(
        '''        first_cmd = (user_msg.split() or [""])[0]
        if first_cmd in GAMES_DISABLED:
            logger.info(f"[小游戏已关闭] gid={gid} uid={user_id} cmd={first_cmd}")
            return

''', "")

    SRC.write_text(text, encoding="utf-8")
    if not compile_ok():
        shutil.copy2(backup, SRC)
        print("编译失败，已还原备份")
        return 1
    print(f"删除 {removed} 行，编译通过；备份 {backup.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
