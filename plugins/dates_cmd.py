"""纪念日 / 生日 的命令入口。

    /纪念日                      看所有记着的日子
    /纪念日 加 主人生日 03-15     主人专用
    /纪念日 加 认识纪念日 2026-10-20
    /纪念日 删 3

日期支持 2026-10-20（算"第几天/满几年"）或 03-15（每年同月同日，适合生日）。
"""
from __future__ import annotations

import os

from nonebot import on_command
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent

import dates

OWNER = os.getenv("OWNER_QQ_ID", "100000000")

dates_cmd = on_command("纪念日", priority=5, block=False)


@dates_cmd.handle()
async def _(event: MessageEvent):
    # 群里必须 @ 她再发，免得任何群友随口一句 "/纪念日" 就触发（审计 C）
    if isinstance(event, GroupMessageEvent) and not event.is_tome():
        return
    raw = event.get_plaintext().strip()
    body = raw[len("纪念日"):].strip() if raw.startswith("纪念日") else raw.strip()
    is_owner = str(event.user_id) == str(OWNER)

    if not body:
        items = dates.list_all()
        if not items:
            await dates_cmd.finish("还没有记任何日子。主人可以：/纪念日 加 认识纪念日 2026-10-20")
        lines = ["记着的日子："]
        for it in items:
            kind = "生日" if it.get("kind") == "birthday" else "纪念日"
            lines.append(f"[{it.get('id')}] {it.get('name')} · {it.get('date')}（{kind}）")
        await dates_cmd.finish("\n".join(lines[:15]))

    parts = body.split()
    action = parts[0]

    if action in ("加", "添加", "记") and len(parts) >= 3:
        if not is_owner:
            await dates_cmd.finish("这个只有主人能加。")
        name, day = parts[1], parts[2]
        kind = "birthday" if len(day) <= 5 else "anniversary"
        ok, msg = dates.add(name, day, kind=kind)
        await dates_cmd.finish(msg)

    if action in ("删", "删除") and len(parts) >= 2:
        if not is_owner:
            await dates_cmd.finish("这个只有主人能删。")
        try:
            tid = int(parts[1])
        except ValueError:
            await dates_cmd.finish("编号看不懂，先发 /纪念日 看列表。")
        await dates_cmd.finish("删掉了。" if dates.remove(tid) else "没找到这个编号。")

    await dates_cmd.finish("用法：/纪念日 | /纪念日 加 名字 2026-10-20 | /纪念日 删 编号")
