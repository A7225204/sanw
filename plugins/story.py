# -*- coding: utf-8 -*-
"""互动小说 v2：在 QQ 里和大肥鱼一起推剧情。

    /开书                看有哪些剧本；只有一本就直接开
    /开书 雨夜书店        开指定剧本
    1 / 2 / 3            选分支
    直接打字              她会顺着你的话说，必要时自己把剧情带到下一个节点
    /剧情                看当前进度、好感度、可选分支
    /合书                结束（结局写进长期记忆）

三个设计点：
- 剧情骨架在 memory/stories/*.json（节点/选项/条件），加剧情不用改代码；
- 选项带 if 条件（min_affinity / has_flag / no_flag），能按关系度解锁不同走向；
- 选择会改 memory_manager 里的真实好感度，关键节点写进长期记忆，她以后会记得。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from threading import RLock

from nonebot import on_command, on_message
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent
from nonebot.rule import Rule

ROOT = Path(__file__).resolve().parent.parent
STORY_DIR = ROOT / "memory" / "stories"
STATE_FILE = ROOT / "memory" / "story_state.json"
HISTORY_FILE = ROOT / "memory" / "story_history.jsonl"
OWNER = os.getenv("OWNER_QQ_ID", "100000000")
TTL = 45 * 60
MAX_AFFINITY_PER_TURN = 1

logger = logging.getLogger("story")
_lock = RLock()
_EXIT_WORDS = ("退出", "结束", "不玩了", "合书", "quit", "exit")


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------
def _load_state() -> dict:
    try:
        if STATE_FILE.exists():
            data = json.loads(STATE_FILE.read_text(encoding="utf-8-sig"))
            return data if isinstance(data, dict) else {}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[剧情] 读状态失败：{type(e).__name__}: {e}")
    return {}


def _save_state(data: dict) -> None:
    tmp = STATE_FILE.with_suffix(".json.tmp")
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, STATE_FILE)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[剧情] 写状态失败：{type(e).__name__}: {e}")


_STORY_CACHE: dict[str, tuple[float, int, dict]] = {}


def _titles(stories: dict[str, dict], limit: int = 12) -> str:
    names = [str(s.get("title") or s.get("_id")) for s in stories.values()]
    head = "、".join(names[:limit])
    return head if len(names) <= limit else f"{head} 等 {len(names)} 本"


def _load_stories() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not STORY_DIR.exists():
        return out
    for f in sorted(STORY_DIR.glob("*.json")):
        try:
            st = f.stat()
        except OSError:
            continue
        hit = _STORY_CACHE.get(str(f))
        if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
            data = hit[2]
        else:
            try:
                data = json.loads(f.read_text(encoding="utf-8-sig"))
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[剧情] 剧本 {f.name} 解析失败：{type(e).__name__}: {e}")
                continue
            if not isinstance(data, dict) or "nodes" not in data:
                continue
            data["_id"] = str(data.get("id") or f.stem)
            _STORY_CACHE[str(f)] = (st.st_mtime, st.st_size, data)
        sid = str(data.get("_id") or f.stem)
        out[sid] = data
        out.setdefault(f.stem, data)
    return out


def _scope_of(event: MessageEvent) -> str:
    if isinstance(event, GroupMessageEvent):
        return f"group_{event.group_id}"
    return f"private_{event.user_id}"


def _session(scope: str, state: dict) -> dict | None:
    row = state.get(scope)
    if not isinstance(row, dict):
        return None
    if time.time() - float(row.get("ts") or 0) > TTL:
        return None
    return row


def _arg_of(event: MessageEvent, word: str) -> str:
    raw = event.get_plaintext().strip().lstrip("/／")
    if raw.startswith(word):
        raw = raw[len(word):]
    return raw.strip()


def _is_playing(event: MessageEvent) -> bool:
    row = _session(_scope_of(event), _load_state())
    if not row:
        return False
    if isinstance(event, GroupMessageEvent):
        if event.is_tome():
            return True
        return str(event.user_id) in (str(row.get("owner")), str(OWNER))
    return True


# ---------------------------------------------------------------------------
# 关系度 / 记忆：两个薄边界，探针可以直接替换
# ---------------------------------------------------------------------------
def _affinity(uid: str) -> int:
    try:
        from memory_manager import get_affinity

        return int(get_affinity(str(uid)))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[剧情] 读好感度失败：{type(e).__name__}: {e}")
        return 50


def _apply_affinity(uid: str, delta: int) -> int:
    delta = max(-MAX_AFFINITY_PER_TURN, min(MAX_AFFINITY_PER_TURN, int(delta or 0)))
    if not delta:
        return _affinity(uid)
    try:
        from memory_manager import update_affinity

        return int(update_affinity(str(uid), delta))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[剧情] 改好感度失败：{type(e).__name__}: {e}")
        return _affinity(uid)


def _remember_fact(scope: str, text: str) -> bool:
    """写进长期记忆的待合并缓冲；达到阈值就起后台合并。"""
    text = (text or "").strip()
    if not text:
        return False
    try:
        from plugins import long_memory as ltm

        reached = ltm.add_pending(scope, "剧情", text)
        if reached:
            try:
                asyncio.get_running_loop().create_task(ltm.consolidate(scope))
            except RuntimeError:
                pass
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[剧情] 写记忆失败：{type(e).__name__}: {e}")
        return False


# ---------------------------------------------------------------------------
# 分支与条件
# ---------------------------------------------------------------------------
def _cond_ok(choice: dict, affinity: int, flags: set[str]) -> bool:
    cond = choice.get("if")
    if not isinstance(cond, dict):
        return True
    try:
        if "min_affinity" in cond and affinity < int(cond["min_affinity"]):
            return False
        if "max_affinity" in cond and affinity > int(cond["max_affinity"]):
            return False
    except (TypeError, ValueError):
        return False
    if cond.get("has_flag") and str(cond["has_flag"]) not in flags:
        return False
    if cond.get("no_flag") and str(cond["no_flag"]) in flags:
        return False
    return True


def _choices(node: dict, affinity: int, flags: set[str]) -> list[dict]:
    return [c for c in (node.get("choices") or []) if _cond_ok(c, affinity, flags)]


def _choices_text(choices: list[dict]) -> str:
    return "\n".join(f"{i}. {c.get('text', '……')}" for i, c in enumerate(choices, 1))


# ---------------------------------------------------------------------------
# 模型：接这一拍，并判断玩家的自由输入最接近哪个选项
# ---------------------------------------------------------------------------
def _parse_json(text: str) -> dict:
    text = (text or "").strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


async def _converse(story: dict, node: dict, affinity: int, choices: list[dict],
                    history: list[str], player: str, fallback: str) -> dict:
    """返回 {"line": 她的话, "pick": 0..N, "affinity": -1/0/1}。"""
    try:
        from plugins import ai_group as ag

        persona = ag._persona_snippet()
        opts = "\n".join(f"{i}. {c.get('text')}" for i, c in enumerate(choices, 1)) or "（当前没有固定选项）"
        system = (
            "你是「示例角色」，蓝色鲸鱼娘，嘴损、傲娇、心软。"
            f"你正陪主人玩文字冒险《{story.get('title', '')}》，关系度 {affinity}/100。\n"
            f"世界设定：{story.get('context') or '日常小故事'}\n"
            f"当前场景：{node.get('scene', '')}\n"
            f"可选分支：\n{opts}\n"
            f"最近发生：{' / '.join(history[-4:]) or '刚开始'}\n"
            "玩家刚才说/做了一件事。只输出一个 JSON 对象，不要代码块、不要多余文字：\n"
            '{"line": "她这一拍的话，1~2句，动作放括号里", "pick": 最接近的选项序号(没有写0), "affinity": -1或0或1}'
        )
        if story.get("mode") == "read":
            system += "\n这段原文会直接显示在他屏幕上：你只写一句念到这里时的吐槽或铺垫，别复述原文、别剧透后文。"
        messages = [
            {"role": "system", "content": system + "\n\n" + persona},
            {"role": "user", "content": player},
        ]
        raw, _ = await asyncio.wait_for(
            ag._chat(messages, temperature=0.9, max_tokens=260, record_scope="story"),
            timeout=25,
        )
        data = _parse_json(raw)
        line = str(data.get("line") or "").strip()
        pick = int(data.get("pick") or 0)
        delta = int(data.get("affinity") or 0)
        if not line:
            line = (raw or "").strip() or fallback
        if pick < 0 or pick > len(choices):
            pick = 0
        return {"line": line, "pick": pick, "affinity": max(-1, min(1, delta))}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[剧情] 模型调用失败：{type(e).__name__}: {e}")
        return {"line": fallback or "（她抱着饭盆，看着你没说话。）", "pick": 0, "affinity": 0}


# ---------------------------------------------------------------------------
# 结算
# ---------------------------------------------------------------------------
def _remember(scope: str, story: dict, row: dict, ending: bool) -> None:
    try:
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with HISTORY_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": int(time.time()), "scope": scope,
                "story": story.get("title") or story.get("_id"),
                "turns": row.get("turns", 0), "ended": bool(ending),
            }, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[剧情] 写历史失败：{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# 指令
# ---------------------------------------------------------------------------
open_cmd = on_command("开书", priority=5, block=True)
status_cmd = on_command("剧情", priority=5, block=False)
end_cmd = on_command("合书", priority=5, block=True)
play = on_message(rule=Rule(_is_playing), priority=6, block=True)


@open_cmd.handle()
async def _(event: MessageEvent):
    if isinstance(event, GroupMessageEvent) and not event.is_tome():
        return
    stories = _load_stories()
    if not stories:
        await open_cmd.finish("书架上还空着……（memory/stories 里没有剧本）")

    uniq: dict[str, dict] = {}
    for s in stories.values():
        uniq[str(s.get("_id"))] = s

    want = _arg_of(event, "开书")
    if not want:
        if len(uniq) == 1:
            story = next(iter(uniq.values()))
        else:
            await open_cmd.finish(f"书架上有：{_titles(uniq)}\n想听哪本？发「/开书 名字」")
    else:
        story = stories.get(want) or next((s for s in uniq.values() if str(s.get("title")) == want), None)
        if not story:
            await open_cmd.finish(f"没找到这本。有的是：{_titles(uniq)}")

    scope = _scope_of(event)
    uid = str(event.user_id)
    node_id = str(story.get("start") or next(iter(story["nodes"])))
    node = story["nodes"].get(node_id) or {}
    affinity = _affinity(uid)

    with _lock:
        state = _load_state()
        state[scope] = {
            "story": str(story.get("_id")), "node": node_id, "owner": uid,
            "turns": 0, "ts": time.time(), "flags": [], "history": [],
        }
        _save_state(state)

    choices = _choices(node, affinity, set())
    head = story.get("intro") or ""
    await open_cmd.finish(
        f"{head}\n\n{node.get('scene', '')}\n\n{_choices_text(choices)}\n\n"
        f"（回 1/2/3 选，或者直接说你想干嘛；关系度 {affinity}）"
    )


@status_cmd.handle()
async def _(event: MessageEvent):
    scope = _scope_of(event)
    row = _session(scope, _load_state())
    if not row:
        await status_cmd.finish("现在没在看书。发 /开书 开始一本。")
    story = _load_stories().get(str(row.get("story")))
    if not story:
        await status_cmd.finish("这本剧本好像被拿走了……发 /合书 结束吧。")
    node = story["nodes"].get(str(row.get("node"))) or {}
    affinity = _affinity(str(row.get("owner") or event.user_id))
    choices = _choices(node, affinity, set(row.get("flags") or []))
    await status_cmd.finish(
        f"《{story.get('title')}》 第 {int(row.get('turns') or 0) + 1} 幕 · 关系度 {affinity}\n"
        f"{node.get('scene', '')}\n\n{_choices_text(choices)}"
    )


@end_cmd.handle()
async def _(event: MessageEvent):
    scope = _scope_of(event)
    with _lock:
        state = _load_state()
        row = _session(scope, state)
        if not row:
            await end_cmd.finish("本来就没在看书。")
        story = _load_stories().get(str(row.get("story"))) or {}
        _remember(scope, story, row, ending=False)
        state.pop(scope, None)
        _save_state(state)
    await end_cmd.finish(
        f"《{story.get('title', '这本')}》先合上，书签夹在第 {int(row.get('turns') or 0) + 1} 幕。想继续就 /开书。"
    )


@play.handle()
async def _(event: MessageEvent):
    scope = _scope_of(event)
    text = event.get_plaintext().strip()
    if not text:
        return
    if text in _EXIT_WORDS:
        await end_cmd.finish("行，这本先合上。想再听就 /开书。")

    with _lock:
        state = _load_state()
        row = _session(scope, state)
        if not row:
            return
        story = _load_stories().get(str(row.get("story")))
        if not story:
            state.pop(scope, None)
            _save_state(state)
            await play.finish("这本剧本被拿走了，先合上吧。")
        node = story["nodes"].get(str(row.get("node"))) or {}
        flags = set(row.get("flags") or [])
        uid = str(row.get("owner") or event.user_id)

    affinity = _affinity(uid)
    choices = _choices(node, affinity, flags)
    numeric = text.isdigit() and 1 <= int(text) <= len(choices)
    pick = choices[int(text) - 1] if numeric else None

    if pick:
        player_line = f"选了「{pick.get('text')}」"
        fallback = pick.get("result") or ""
    else:
        player_line = f"说/做了「{text}」"
        fallback = "（她抬眼看了你一下，没接话，等你自己想清楚。）"

    out = await _converse(story, node, affinity, choices, list(row.get("history") or []), player_line, fallback)
    line, model_pick, delta = out["line"], out["pick"], out["affinity"]

    # 自由输入：模型觉得像某个分支，就走那个分支
    if not pick and model_pick:
        pick = choices[model_pick - 1]

    if pick:
        delta = int(pick.get("affinity") or delta)

    new_affinity = affinity
    if delta:
        new_affinity = _apply_affinity(uid, delta)

    # 状态推进
    ending = False
    with _lock:
        state = _load_state()
        row = _session(scope, state)
        if not row:
            await play.finish(line)
        row["turns"] = int(row.get("turns") or 0) + 1
        row["ts"] = time.time()
        row["history"] = (list(row.get("history") or []) + [f"玩家{player_line}", line[:60]])[-8:]
        flags = list(row.get("flags") or [])
        if pick:
            if pick.get("flag") and str(pick["flag"]) not in flags:
                flags.append(str(pick["flag"]))
            if pick.get("next"):
                row["node"] = str(pick["next"])
            else:
                ending = True
        row["flags"] = flags
        if ending:
            state.pop(scope, None)
        else:
            state[scope] = row
        _save_state(state)

    # 记忆：选项自带 memory，或走到结局
    if pick and pick.get("memory"):
        _remember_fact(scope, str(pick["memory"]))
    if ending:
        _remember_fact(
            scope,
            f"陪主人走完了《{story.get('title')}》：他最后选了「{pick.get('text')}」，我回的是「{line[:60]}」。",
        )
        _remember(scope, story, row, ending=True)
        await play.finish(f"{line}\n\n—— 完 ——\n关系度 {new_affinity}。想再听一本就 /开书。")

    # 自由输入且没有落到任何分支：只接话，剧情不动
    if not pick:
        hint = _choices_text(choices)
        if hint:
            await play.finish(f"{line}\n\n（剧情没动，她想听你继续。\n{hint}）")
        await play.finish(line)

    nxt = story["nodes"].get(str(row.get("node"))) or {}
    nxt_choices = _choices(nxt, new_affinity, set(row.get("flags") or []))
    tail_text = "—— 完 ——" if nxt.get("ending") and not nxt_choices else _choices_text(nxt_choices)
    await play.finish(f"{line}\n\n{nxt.get('scene', '')}\n\n{tail_text}")
