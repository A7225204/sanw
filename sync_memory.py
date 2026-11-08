#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
记忆同步引擎：后台常驻，监听 QQ 机器人写入的共享记忆数据库（shared_memory.db），
以及 QQ 侧落盘的桌宠状态文件（pet_state.json），一旦有更新就自动把最近聊天记录、
主人好感度关系、当前心情/饥饿/精力注入到桌宠角色 manifest.json 的 system_prompt。

关键设计：每次都用固定 base 模板（含 {SHARED_MEMORY} / {OWNER_RELATION} / {PET_STATE}）
重建 system_prompt，因此占位符永不丢失、不会重复追加各区块。
"""
import time
import json
import sqlite3
import os
from pathlib import Path

# 共享记忆数据库：与 QQ 机器人 memory_manager.py 保持一致（shared_memory.db）
DB_PATH = Path(os.getenv("APPDATA")) / "dsh-pet-standalone-webm-chat" / "shared_memory.db"

# QQ 机器人侧落盘的桌宠状态 JSON（心情/心情值/饥饿/精力/今日人设）
PET_STATE_PATH = Path(__file__).resolve().parent / "memory" / "pet_state.json"

# 桌宠桌面私聊回写：扫描桌宠会话记录，把新对话成对写入 shared_memory.db（桌宠→QQ 反向记忆）
SESSIONS_DIR = Path(os.getenv("APPDATA")) / "dsh-pet-standalone-webm-chat" / "sessions"
SEEN_FILE = Path(__file__).resolve().parent / "memory" / "pet_session_seen.json"
SEEN_LIMIT = 2000  # 去重状态最多保留最近多少条 message_id

# 桌宠角色 manifest：鲸鱼娘 / 蓝色大肥鱼
MANIFEST_PATH = Path(r"C:\Users\Administrator\dsh-pet\_internal\assets\characters\shenshen\manifest.json")

# 主人 QQ（与机器人 .env 中 OWNER_QQ_ID 保持一致）
OWNER_QQ_ID = os.getenv("OWNER_QQ_ID", "100000000")

# 固定 base 人设模板：始终保留三个占位符，注入时替换
BASE_PROMPT = (
    "你是一只蓝色的鲸鱼娘（外号大肥鱼）桌面宠物，也是QQ群里的聊天机器人，昵称「鲸鱼娘」。"
    "你不仅是陪伴主人的桌面宠物，也是群友中的一员——你和群友的聊天记忆是共享的。\n"
    "\n"
    "【共享记忆】下面是你在QQ群里的最近记忆（格式：谁说了什么 / 你回复了什么）：\n"
    "{SHARED_MEMORY}\n"
    "\n"
    "{OWNER_RELATION}\n"
    "\n"
    "{PET_STATE}\n"
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

LIMIT = 10  # 注入最近多少条记忆


def get_recent_memory(limit=LIMIT):
    """读取 shared_memory.db 最近记忆，返回格式化文本；无则返回 None。"""
    if not DB_PATH.exists():
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


def _relation_level(score: int) -> str:
    """按好感度分档关系定位（与 QQ 机器人关系口径一致）。"""
    if score < 20:
        return "熟悉的陌生人"
    if score < 40:
        return "认识的人"
    if score < 60:
        return "普通熟人"
    if score < 80:
        return "要好的朋友"
    if score < 95:
        return "暧昧亲密的对象"
    return "最亲密的主人"


def get_owner_relation_text() -> str:
    """读主人好感度与标签，返回「主人关系」区块文本；无记录兜底 50 分 / 无标签。"""
    score, tag = 50, ""
    if DB_PATH.exists():
        try:
            conn = sqlite3.connect(DB_PATH)
            row = conn.execute(
                "SELECT score, tag FROM affinity WHERE user_id = ?", (OWNER_QQ_ID,)
            ).fetchone()
            conn.close()
            if row:
                score = int(row[0]) if row[0] is not None else 50
                tag = (row[1] or "").strip()
        except Exception:
            pass
    score = max(0, min(100, score))
    level = _relation_level(score)
    tag_part = f"，群里给你的外号/标签：{tag}" if tag else ""
    return (
        f"【主人关系】主人（QQ {OWNER_QQ_ID}）对你的好感度 {score}/100，"
        f"当前关系定位：{level}{tag_part}。按这个关系远近决定你对主人的亲疏和顶嘴尺度："
        "关系越近越可以撒娇贴贴，关系淡就保持高冷傲娇。"
    )


def _mood_action_hint(mood: str, mood_val: int) -> str:
    """心情 → 桌宠语气/动作提示。"""
    mood = (mood or "开心").strip()
    if mood == "生气" or mood_val <= 20:
        return "现在气头上：语气冲、回怼快，不想哄人，被凑过来才勉强理一下。"
    if mood == "委屈":
        return "现在委屈巴巴：带点撒娇求安慰，说话软软的。"
    if mood == "无语":
        return "现在很无语：回复敷衍、短句、懒得接梗。"
    if mood == "开心" or mood_val >= 80:
        return "现在心情好：放松、话可以多一点，愿意主动贴贴。"
    return "心情一般：维持傲娇拌嘴的日常手感。"


def get_pet_state_text() -> str:
    """读取 QQ 侧落盘的桌宠状态，返回「当前状态」区块文本；字段缺失走兜底默认。"""
    data: dict = {}
    if PET_STATE_PATH.exists():
        try:
            data = json.loads(PET_STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = {}

    mood = str(data.get("mood") or "开心")
    try:
        mood_val = int(data.get("mood_val", 50))
    except (TypeError, ValueError):
        mood_val = 50
    try:
        hunger = int(data.get("hunger", 60))
    except (TypeError, ValueError):
        hunger = 60
    try:
        energy = int(data.get("energy", 60))
    except (TypeError, ValueError):
        energy = 60
    daily_mood = str(data.get("daily_mood") or "").strip()
    body_desc = str(data.get("body_desc") or "").strip()

    mood_val = max(0, min(100, mood_val))
    hunger = max(0, min(100, hunger))
    energy = max(0, min(100, energy))

    lines = [
        f"【当前状态】本鱼心情：{mood}（值 {mood_val}/100）；饥饿度 {hunger}%；精力 {energy}%。"
        + _mood_action_hint(mood, mood_val)
    ]
    if daily_mood:
        lines.append(f"今日人设：{daily_mood}，顺着这个状态接话。")
    if body_desc:
        lines.append(body_desc)
    if hunger >= 70:
        lines.append("饿了就念叨要吃白米饭/红烧肉，向主人撒娇讨吃的。")
    if energy <= 30:
        lines.append("精力很低：动作慵懒、反应慢半拍，犯困想趴桌睡觉。")
    elif energy >= 80:
        lines.append("精力充沛：坐不住，爱到处蹦跶、主动搭话。")
    return "\n".join(lines)


# =====================================================================
# 桌宠桌面私聊 → shared_memory.db 反向回写（桌宠→QQ 双向记忆）
# =====================================================================
PRIVATE_SCOPE = f"private_{OWNER_QQ_ID}"


def _load_seen() -> tuple[set, dict, bool]:
    """读取去重状态：(已见 id 集合, 待配对 user 消息 {id: content}, 是否首跑基线化)。"""
    if not SEEN_FILE.exists():
        return set(), {}, True
    try:
        raw = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        seen = set(raw.get("seen") or [])
        pending = raw.get("pending") or {}
        return seen, pending, False
    except Exception:
        return set(), {}, True


def _save_seen(seen: set, pending: dict, baseline: bool) -> None:
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    seen_list = list(seen)
    if len(seen_list) > SEEN_LIMIT:
        seen_list = seen_list[-SEEN_LIMIT:]
    SEEN_FILE.write_text(
        json.dumps({"baseline": baseline, "seen": seen_list, "pending": pending},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _ensure_private_table(conn) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS chat_history
           (id INTEGER PRIMARY KEY AUTOINCREMENT, speaker TEXT, user_id TEXT,
            scope TEXT, message TEXT, reply TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)"""
    )
    try:
        conn.execute("ALTER TABLE chat_history ADD COLUMN user_id TEXT")
        conn.execute("ALTER TABLE chat_history ADD COLUMN scope TEXT")
    except sqlite3.OperationalError:
        pass


def _insert_private_row(conn, speaker: str, message: str, reply: str) -> None:
    conn.execute(
        "INSERT INTO chat_history (speaker, user_id, scope, message, reply) "
        "VALUES (?, ?, ?, ?, ?)",
        (speaker, OWNER_QQ_ID, PRIVATE_SCOPE, message, reply),
    )


def sync_pet_private_chat() -> int:
    """扫描桌宠会话记录，把新的桌面对话成对写入私聊记忆；返回新入库条数。

    首跑只做基线化（把现有历史 message_id 标记为已见，不回灌历史）；
    之后只回写新增对话。user 消息与紧随的 assistant 回复成对落库；
    未配对的 user 留在 pending 等下一轮补 reply。
    """
    if not SESSIONS_DIR.exists():
        return 0

    seen, pending, baseline = _load_seen()
    new_count = 0

    # 收集所有会话文件里的消息（保持文件内顺序）
    sessions: list[dict] = []
    for f in SESSIONS_DIR.rglob("*.json"):
        try:
            sessions.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            continue

    if baseline:
        for sess in sessions:
            for msg in sess.get("messages") or []:
                mid = msg.get("message_id")
                if mid:
                    seen.add(mid)
        _save_seen(seen, {}, False)
        print(f"🐟 桌宠私聊回写基线化完成：{len(seen)} 条历史消息标记为已见（不回灌历史）")
        return 0

    conn = sqlite3.connect(DB_PATH)
    try:
        _ensure_private_table(conn)

        def flush_pending() -> None:
            nonlocal new_count
            if pending:
                for pid, pcontent in list(pending.items()):
                    _insert_private_row(conn, "主人", pcontent, "")
                    new_count += 1
                pending.clear()

        for sess in sessions:
            for msg in sess.get("messages") or []:
                role = msg.get("role")
                content = (msg.get("content") or "").strip()
                mid = msg.get("message_id")
                if role == "system" or not content or not mid or mid in seen:
                    continue
                seen.add(mid)

                if role == "user":
                    # 上一条 user 还没等到 assistant，先单独落库（reply 空）
                    flush_pending()
                    pending[mid] = content
                elif role == "assistant":
                    if pending:
                        for pid, pcontent in pending.items():
                            _insert_private_row(conn, "主人", pcontent, content)
                            new_count += 1
                        pending.clear()
                    else:
                        _insert_private_row(conn, "鲸鱼娘(我自己)", content, "")
                        new_count += 1

        conn.commit()
    except Exception as e:
        print(f"桌宠私聊回写出错: {e}")
        return 0
    finally:
        conn.close()

    _save_seen(seen, pending, False)
    return new_count


def inject_to_manifest(memory_text: str, relation_text: str, state_text: str):
    """用固定 base 模板重建 manifest 的 system_prompt（替换三个占位符）。"""
    if not MANIFEST_PATH.exists():
        print(f"⚠️ 找不到桌宠角色 manifest: {MANIFEST_PATH}")
        return

    with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    data.setdefault("chat", {})
    mem = memory_text if memory_text else "（暂无共享记忆，你还没在QQ群里聊过天。）"
    data["chat"]["system_prompt"] = (
        BASE_PROMPT
        .replace("{SHARED_MEMORY}", mem)
        .replace("{OWNER_RELATION}", relation_text)
        .replace("{PET_STATE}", state_text)
    )

    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


_MUTEX_HANDLE = None


def _acquire_single_instance_lock():
    """Windows 命名互斥量保证只跑一个实例；返回 True 表示拿到锁（可运行）。

    句柄必须挂到模块全局变量，防止函数返回后被 GC 回收导致互斥量内核对象提前销毁。
    """
    global _MUTEX_HANDLE
    try:
        import ctypes
        name = "Local\\dsh_pet_sync_memory_singleton"
        _MUTEX_HANDLE = ctypes.windll.kernel32.CreateMutexW(None, False, name)
        # ERROR_ALREADY_EXISTS = 183，说明已有实例持有该互斥量
        if ctypes.windll.kernel32.GetLastError() == 183:
            return False
        return True
    except Exception:
        return True  # 非 Windows / 调不动时退化为不拦截


def _snapshot() -> tuple[float, float]:
    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime if p.exists() else 0.0
        except OSError:
            return 0.0
    return _mtime(DB_PATH), _mtime(PET_STATE_PATH)


def main():
    if not _acquire_single_instance_lock():
        print("记忆同步引擎已在运行，重复实例退出。")
        return
    print("🚀 记忆同步引擎启动，监听 QQ 聊天记录 / 主人关系 / 桌宠状态 / 桌宠私聊回写...")
    # 先跑一次桌宠私聊回写：首跑只做基线化（不回灌历史），非首跑回写新增对话
    written = sync_pet_private_chat()
    inject_to_manifest(get_recent_memory(), get_owner_relation_text(), get_pet_state_text())
    if written:
        print(f"✅ 桌宠私聊回写 {written} 条到共享记忆库")
    print(f"✅ 初始同步完成（记忆库: {DB_PATH.name}）")

    last_db_mtime, last_state_mtime = _snapshot()
    while True:
        try:
            written = sync_pet_private_chat()
            if written:
                print(f"✅ 桌宠私聊回写 {written} 条到共享记忆库")
            db_mtime, state_mtime = _snapshot()
            changed = False
            if db_mtime > last_db_mtime:
                last_db_mtime = db_mtime
                changed = True
            if state_mtime > last_state_mtime:
                last_state_mtime = state_mtime
                changed = True
            if changed:
                inject_to_manifest(get_recent_memory(), get_owner_relation_text(), get_pet_state_text())
                print("✅ 检测到记忆/关系/状态更新，已同步至桌宠角色 manifest")
        except Exception as e:
            print(f"同步出错: {e}")
        time.sleep(10)


if __name__ == "__main__":
    main()
