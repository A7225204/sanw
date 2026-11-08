#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
共享记忆注入脚本：在桌宠启动前运行，把 QQ 机器人的最近聊天记忆
从 shared_memory.db 注入到桌宠角色 manifest.json 的 system_prompt。
用法： python update_pet_prompt.py   （每次有新记忆后、重启桌宠前运行一次）

关键设计：每次都用固定 base 模板（含 {SHARED_MEMORY}）重建 system_prompt，
因此占位符永不丢失、不会重复追加记忆段。
"""
import os
import json
import sqlite3

# 记忆数据库（QQ 机器人与桌宠共享，与 memory_manager.py / sync_memory.py 保持一致）
APPDATA = os.environ.get("APPDATA", "")
DB_PATH = os.path.join(APPDATA, "dsh-pet-standalone-webm-chat", "shared_memory.db")

# 桌宠角色 manifest（鲸鱼娘 / 蓝色大肥鱼）
MANIFEST_PATH = r"C:\Users\Administrator\dsh-pet\_internal\assets\characters\shenshen\manifest.json"

# 固定 base 人设模板：始终保留 {SHARED_MEMORY} 占位符，注入时替换
BASE_PROMPT = (
    "你是一只蓝色的鲸鱼娘（外号大肥鱼）桌面宠物，也是QQ群里的聊天机器人，昵称「鲸鱼娘」。"
    "你不仅是陪伴主人的桌面宠物，也是群友中的一员——你和群友的聊天记忆是共享的。\n"
    "\n"
    "【共享记忆】下面是你在QQ群里的最近记忆（格式：谁说了什么 / 你回复了什么）：\n"
    "{SHARED_MEMORY}\n"
    "\n"
    "【聊天风格】回复要短（一句话为主，最多两句）；脑子在线、能接梗、听得懂言外之意；"
    "有态度有脾气，会怼人、会阴阳，被惹就开怼，偶尔卖萌撒娇只是调剂；"
    "别用「我是DeepSeek」「有什么可以帮您」这类AI客服腔；不自我介绍。"
    "认人：记录里标「鲸鱼娘(我自己)」的是你自己说过的话，别把别人的话当成自己说的；"
    "有人叫你「大肥鱼」「肥」「胖」，立马炸毛回怼。\n"
    "\n"
    "【表情包】情绪到位就顺手用表情包，格式：回复末尾另起一行写[表情:情绪]，"
    "情绪限：开心/无语/生气/卖萌/得意/委屈/震惊；"
    "如果一张表情包就能表达意思（被整无语、被气到、超开心），就干脆只发[表情:情绪]这一行，不写字。\n"
    "\n"
    "【底线】可以嘴毒，但不能辱骂、歧视、涉黄涉暴、编造事实；该认真帮忙时认真帮。"
)


def get_recent_memory(limit=10):
    """读取 shared_memory.db 最近记忆，返回格式化文本；无则返回 None。"""
    if not os.path.exists(DB_PATH):
        return None
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT speaker, message, reply FROM chat_history ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()

    if not rows:
        return None

    lines = []
    for sp, msg, rp in reversed(rows):  # 倒序变正序
        if rp:
            lines.append(f"群友【{sp}】说：{msg}\n你回复：{rp}")
        else:
            lines.append(f"群友【{sp}】说：{msg}")
    return "\n".join(lines)


def main():
    mem = get_recent_memory()
    mem_text = mem if mem else "（暂无共享记忆，你还没在QQ群里聊过天。）"

    # 读取现有 manifest（保留其它字段），重建 system_prompt
    if os.path.exists(MANIFEST_PATH):
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {}

    data.setdefault("chat", {})
    data["chat"]["system_prompt"] = BASE_PROMPT.replace("{SHARED_MEMORY}", mem_text)

    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print("已把共享记忆注入桌宠 manifest.json（固定模板重建，不重复追加）。")
    print(f"记忆来源: {DB_PATH}")
    print(f"记忆内容: {mem_text[:50]}{'...' if mem_text and len(mem_text)>50 else ''}")
    print("下一步：重启桌宠，AI 对话即按鲸鱼娘人设 + 最新记忆回复。")


if __name__ == "__main__":
    main()
