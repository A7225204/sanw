"""一次性：生成「20 块表情包惨案」主题抖音视频，放进 outbox 让机器人发主人。"""
from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import nonebot  # noqa: E402

nonebot.init()
from nonebot.adapters.onebot.v11 import Adapter  # noqa: E402

nonebot.get_driver().register_adapter(Adapter)
import importlib  # noqa: E402

m = importlib.import_module("plugins.ai_group")

LINES = [
    "主人让我去下表情包，本鱼寻思这不就是摸鱼时间嘛。",
    "大白饭一扒，尾巴一翘，二十块钱啪就点出去了。",
    "叮——一堆表情包出炉，本鱼还觉得自己可聪明了。",
    "结果主人一句“谁让你乱生图的”，本鱼当场僵住。",
    "尾巴耷拉下来，饭也不香了，这下真闯祸了捏。",
    "委屈巴巴……主人别气了，表情包全交出来还不行嘛。",
]

SCENES = [
    "趴在一大碗白米饭上，眯眼开心地扒饭，尾巴翘起来，暖色背景",
    "盘腿坐着戳手机屏幕，表情得意，屏幕发光，周围飘着几张聊天贴纸",
    "被一大堆可爱的鲸鱼表情包贴纸包围，叉腰得意，背景明亮",
    "突然僵住、瞳孔地震，手机掉在桌上，身后一个模糊的主人黑影",
    "缩成一团跪坐在散落的表情包中间，尾巴耷拉，眼眶含泪，旁边一碗没吃完的白饭",
    "抱着自己的尾巴委屈巴巴，眼泪在眼眶打转，低饱和冷色背景，散落的表情包",
]


async def main() -> None:
    stamp = m.time.strftime("%Y%m%d_%H%M%S")
    outdir = m.DOUYIN_DIR / stamp
    outdir.mkdir(parents=True, exist_ok=True)

    narration = "，".join(LINES)
    audio = await m._doubao_tts(narration)
    if not audio:
        audio_path = outdir / "voice.mp3"
        communicate = m.edge_tts.Communicate(narration, m.TTS_VOICE, rate=m.TTS_RATE, pitch=m.TTS_PITCH)
        await communicate.save(str(audio_path))
    else:
        audio_path = Path(audio)
        if audio_path.parent != outdir:
            shutil.copy(audio_path, outdir / "voice.mp3")
        audio_path = outdir / "voice.mp3"

    images = await m._gen_douyin_images(SCENES, outdir)
    if not images:
        raise RuntimeError("出图失败")
    out = await asyncio.to_thread(m._compose_douyin, outdir, images, audio_path, LINES)
    (outdir / "caption.txt").write_text(
        "标题：20块钱的表情包惨案\n"
        "文案：主人让我下表情包，本鱼反手花20块生了一堆……现在尾巴都耷拉了，下次真不敢了捏。\n"
        "话题：#蓝色大肥鱼 #鲸鱼娘 #搞笑日常 #委屈巴巴 #摸鱼翻车\n"
        "口播：\n" + "\n".join(LINES),
        encoding="utf-8",
    )
    try:
        (outdir / "cover.jpg").write_bytes(images[0].read_bytes())
    except Exception:
        pass
    for p in outdir.glob("scene_*.jpg"):
        p.unlink(missing_ok=True)
    for name in ("list.txt", "subs.ass", "voice.mp3"):
        (outdir / name).unlink(missing_ok=True)

    print("VIDEO", out, flush=True)
    ob = m.DOUYIN_OUTBOX
    ob.mkdir(parents=True, exist_ok=True)
    dst = ob / "20kuai.mp4"
    shutil.copy(out, dst)
    print("OUTBOX", dst, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
