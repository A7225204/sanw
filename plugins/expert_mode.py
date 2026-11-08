"""专家模式：3ds Max + Corona + tyFlow + Photoshop。

主人私聊说「启动专家模式 / 关闭专家模式」切换。
开启后：
- 主人发图：识图 + 按 Max/CR/tyFlow/PS 工作流分析，给调整参数和提示词；
- 主人问软件：按老手口吻回答，生活化、不端着；
- 普通聊天暂时让位给专家模式。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from nonebot import on_message
from nonebot.adapters.onebot.v11 import PrivateMessageEvent

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "memory" / "expert_mode.json"
KNOWLEDGE = ROOT / "memory" / "expert_knowledge.json"
OWNER = "100000000"

expert = on_message(priority=4, block=False)

_SEED = [
    ("工作", "主人做亚马逊产品图片和视频，关注 listing 主图、A+ 页面、产品视频"),
    ("工具", "主力工具：3ds Max、Corona Renderer、tyFlow、Photoshop"),
    ("工具", "视频与后期：After Effects、Premiere Pro、DaVinci Resolve"),
    ("流程", "会无缝贴图制作、UV 展开、ZBrush 雕刻刷模型"),
]


def _load_knowledge() -> list[dict]:
    try:
        d = json.loads(KNOWLEDGE.read_text(encoding="utf-8"))
        facts = d.get("facts") if isinstance(d, dict) else None
        if isinstance(facts, list) and facts:
            return facts
    except Exception:
        pass
    facts = [{"subject": s, "fact": f, "ts": time.strftime("%Y-%m-%d")} for s, f in _SEED]
    _save_knowledge(facts)
    return facts


def _save_knowledge(facts: list[dict]) -> None:
    try:
        KNOWLEDGE.parent.mkdir(parents=True, exist_ok=True)
        KNOWLEDGE.write_text(
            json.dumps({"facts": facts[-200:]}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def _knowledge_text() -> str:
    facts = _load_knowledge()
    if not facts:
        return ""
    lines = [f"- {x.get('subject') or '工作'}：{x.get('fact')}" for x in facts[-30:]]
    return "【主人工作知识档案】只用于专家模式：\n" + "\n".join(lines)

SYSTEM = (
    "你是「蓝色大肥鱼」，圆滚滚的蓝色鲸鱼拟人少女，同时也是 3ds Max、Corona Renderer、"
    "tyFlow 粒子、Photoshop 的老手。你主人做亚马逊产品图片和视频，常用 3ds Max + Corona + "
    "tyFlow + Photoshop，还会 AE、Premiere Pro、DaVinci Resolve 剪辑调色，"
    "以及无缝贴图、UV 展开、ZBrush 刷模型；关注 listing 主图、A+ 页面和产品视频。"
    "说话生活化、口语化，像带徒弟的熟人，不要书面报告腔，"
    "不要自称 AI/模型。先一句话判断问题，再给能直接照做的步骤和参数；"
    "参数要具体（采样、降噪、曝光、GI、白平衡、粒子碰撞、PS 曲线/蒙版等），"
    "不确定就说不确定，别编。需要给提示词时，单独给一段可直接复制的提示词。"
)


def _load() -> dict:
    try:
        d = json.loads(STATE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(d: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def _enabled() -> bool:
    return bool(_load().get("enabled"))


def _set(on: bool) -> None:
    d = _load()
    d["enabled"] = on
    d["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save(d)


async def _describe(img: bytes) -> str:
    prompt = ("先逐字读图里的文字（没有就说无文字），再判断这张图在 3D/后期上的问题："
              "噪点、曝光、构图、材质、光比、粒子、穿模、边缘等，80 字内。")
    try:
        from plugins import gemini
        d = await gemini.gemini_vision(prompt, img, mime="image/jpeg")
        if d:
            return d.strip()
    except Exception:
        pass
    try:
        from plugins.ai_group import _local_qwen_vision
        d = await _local_qwen_vision(img, prompt, timeout=120)
        if d:
            return d.strip()
    except Exception:
        pass
    return ""


@expert.handle()
async def _(event: PrivateMessageEvent):
    if str(event.user_id) != OWNER:
        return
    text = event.get_plaintext().strip()

    if any(k in text for k in ("启动专家模式", "开启专家模式", "进入专家模式")):
        _set(True)
        await expert.finish(
            "(*把工具摊一桌*) 专家模式开了。\n"
            "发图我帮你看 Max / Corona / tyFlow / PS 哪儿不对；"
            "问软件直接说，别客气。关掉就说「关闭专家模式」。"
        )
        return
    if any(k in text for k in ("关闭专家模式", "退出专家模式", "关掉专家模式")):
        _set(False)
        await expert.finish("(*收工*) 专家模式关了，回到平时聊天。")
        return

    if not _enabled():
        return
    if text.startswith("/"):
        return  # 指令留给其他插件
    if "强化学习" in text:
        return  # 交给 deep_learn 插件

    # 专家模式下说「记住…」就写进工作知识档案
    m_remember = text.lstrip()
    for lead in ("记住", "记一下", "记下"):
        if m_remember.startswith(lead):
            fact = m_remember[len(lead):].strip(" ：:，。,")
            if fact:
                facts = _load_knowledge()
                facts.append({"subject": "主人补充", "fact": fact[:160],
                              "ts": time.strftime("%Y-%m-%d")})
                _save_knowledge(facts)
                await expert.finish(f"(*点头*) 记下了：{fact[:60]}")
                return

    images = []
    for seg in event.get_message():
        if seg.type == "image":
            u = seg.data.get("url") or seg.data.get("file")
            if u:
                images.append(u)

    from plugins.ai_group import call_ai_raw, _image_bytes
    descs = []
    for u in images[:3]:
        try:
            b = await _image_bytes(u)
            if b:
                d = await _describe(b)
                if d:
                    descs.append(d)
        except Exception:
            pass

    if descs:
        user_block = (
            f"主人发来 {len(descs)} 张图，识图结果：\n- " + "\n- ".join(descs) +
            f"\n主人补充的话：{text or '（没说话，自己判断）'}"
        )
        task = (
            "按专家模式回答：先说这图/这组图最明显的问题，"
            "再分 3ds Max / Corona / tyFlow / Photoshop 给具体调整建议（能用的参数就写参数），"
            "最后如果要重绘/生成，给一段可直接复制的提示词。"
        )
    else:
        user_block = f"主人问：{text}"
        task = "按专家模式回答，给具体步骤和参数；能用生活化比喻就用，别写教程目录。"

    try:
        ans = await call_ai_raw(
                                SYSTEM + "\n\n" + _knowledge_text() + "\n\n" + user_block + "\n\n" + task,
                                "deepseek", temperature=0.6, max_tokens=900)
    except Exception as e:  # noqa: BLE001
        await expert.finish(f"(*挠头*) 专家模式这边出错了：{type(e).__name__}: {e}")
        return
    if ans:
        await expert.finish(ans.strip()[:1600])
