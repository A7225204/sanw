"""WD14 Tagger：给图片打动漫标签（含 NSFW），不依赖 ComfyUI。

2026-11-06 加。起因：她发 R-18 库里的图，主人问「发的是什么」，她答不上来 ——
云端识图和本地 QwenVL 都会拒 NSFW（实测本地直接回「我不能提供您所要求的图片内容」）。
WD14 是纯分类器，只输出 tag 概率，不做道德判断。

模型：SmilingWolf/wd-v1-4-moat-tagger-v2（ONNX，338MB）
标签：selected_tags.csv（2000+ 个动漫标签，含 general / character / rating 三类）

用法：
    desc = await describe_sfw(path)      # 只取安全描述
    desc = await describe(path)          # 含 NSFW 标签
"""
from __future__ import annotations

import asyncio
import csv
import os
from pathlib import Path

from nonebot import logger

MODEL_DIR = Path(os.getenv(
    "WD14_MODEL_DIR",
    r"C:\ComfyUI-aki(2)\ComfyUI-aki-v1.7\ComfyUI\custom_nodes\comfyui-WD14-Tagger\models"))
MODEL_FILE = MODEL_DIR / "model.onnx"
TAGS_FILE = MODEL_DIR / "selected_tags.csv"

SIZE = 448
THRESHOLD = 0.35

# NSFW / 情色相关标签（命中就单独拎出来说）
_NSFW_KW = (
    "nude", "naked", "topless", "bottomless", "nipples", "areola", "pussy", "penis",
    "cum", "sex", "vaginal", "anal", "oral", "fellatio", "paizuri", "handjob",
    "masturbation", "spread_legs", "presenting", "bdsm", "bondage", "leash",
    "cameltoe", "underwear", "panties", "bra", "bikini", "lingerie", "navel",
    "cleavage", "ass", "butt", "thighs", "collarbone", "bare_shoulders",
    "completely_nude", "nude_male", "nude_female",
)
# 姿势 / 动作（帮助判断"在干啥"）
_POSE_KW = (
    "lying", "sitting", "standing", "kneeling", "on_back", "on_stomach", "on_side",
    "all_fours", "squatting", "legs_up", "arms_up", "spread", "hugging", "holding",
    "looking_at_viewer", "eye_contact", "blush", "smile", "open_mouth", "closed_eyes",
    "solo", "2girls", "2boys", "multiple_boys", "multiple_girls", "hetero", "yuri", "yaoi",
)

_session = None
_tags: list[dict] = []
_load_lock = asyncio.Lock()


def _load_sync() -> bool:
    """加载模型和标签表（只做一次）。"""
    global _session, _tags
    if _session is not None:
        return True
    if not MODEL_FILE.exists():
        logger.warning(f"[WD14] 模型不存在：{MODEL_FILE}")
        return False
    try:
        import onnxruntime as ort
        so = ort.SessionOptions()
        so.log_severity_level = 3
        providers = ["CPUExecutionProvider"]
        try:
            if "CUDAExecutionProvider" in ort.get_available_providers():
                providers.insert(0, "CUDAExecutionProvider")
        except Exception:  # noqa: BLE001
            pass
        _session = ort.InferenceSession(str(MODEL_FILE), sess_options=so, providers=providers)
        rows = []
        if TAGS_FILE.exists():
            with TAGS_FILE.open(encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    rows.append({"name": r.get("name", ""), "cat": r.get("category", "")})
        _tags = rows
        logger.info(f"[WD14] 模型已加载（{len(rows)} 个标签，provider={_session.get_providers()[0]}）")
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[WD14] 加载失败：{type(e).__name__}: {e}")
        _session = None
        return False


def _prep(buf: bytes):
    """图片 → 模型输入张量（448x448，BGR，0~255 float32）。"""
    import io
    import numpy as np
    from PIL import Image

    im = Image.open(io.BytesIO(buf)).convert("RGB")
    # WD14 要求：保持比例缩放到 448，居中 padding
    w, h = im.size
    scale = SIZE / max(w, h)
    nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
    im = im.resize((nw, nh), Image.BILINEAR)
    canvas = Image.new("RGB", (SIZE, SIZE), (255, 255, 255))
    canvas.paste(im, ((SIZE - nw) // 2, (SIZE - nh) // 2))
    arr = np.asarray(canvas, dtype=np.float32)
    arr = arr[:, :, ::-1]                      # RGB → BGR
    arr = np.expand_dims(arr, 0)
    return arr


def _infer_sync(buf: bytes) -> list[tuple[str, float, str]]:
    if not _load_sync():
        return []
    import numpy as np
    x = _prep(buf)
    inp = _session.get_inputs()[0]
    out = _session.run(None, {inp.name: x})
    probs = np.asarray(out[0])[0]
    res = []
    for i, p in enumerate(probs):
        if i >= len(_tags):
            break
        if float(p) < THRESHOLD:
            continue
        res.append((_tags[i]["name"], float(p), _tags[i]["cat"]))
    res.sort(key=lambda t: -t[1])
    return res


def _fmt(tags: list[tuple[str, float, str]], with_nsfw: bool) -> str:
    """把标签整理成一句人话。"""
    if not tags:
        return ""
    _tr = {
        "nude": "全裸", "naked": "裸着", "completely_nude": "一丝不挂",
        "topless": "上身没穿", "bottomless": "下身没穿",
        "nipples": "露点", "pussy": "下体露出", "penis": "男性性器",
        "sex": "在性交", "vaginal": "插入中", "anal": "后庭", "oral": "口交",
        "fellatio": "口交", "paizuri": "乳交", "handjob": "手交",
        "masturbation": "自慰", "cum": "射了",
        "spread_legs": "张开腿", "presenting": "摆姿势勾引",
        "bondage": "被绑着", "bdsm": "调教", "leash": "被牵着",
        "panties": "只穿内裤", "bra": "只穿胸罩", "lingerie": "内衣",
        "bikini": "比基尼", "underwear": "内衣",
        "lying": "躺着", "sitting": "坐着", "standing": "站着",
        "kneeling": "跪着", "on_back": "仰躺", "on_stomach": "趴着",
        "all_fours": "四肢撑地", "squatting": "蹲着",
        "solo": "单人", "2girls": "两个女生", "2boys": "两个男生",
        "hetero": "男女", "yuri": "百合", "yaoi": "男同",
        "blush": "脸红了", "smile": "在笑", "open_mouth": "张着嘴",
        "looking_at_viewer": "看着镜头", "closed_eyes": "闭着眼",
        "breasts": "有胸", "large_breasts": "胸很大", "ass": "露臀",
        "thighs": "露大腿", "cleavage": "露事业线", "navel": "露肚脐",
        "collarbone": "露锁骨", "bare_shoulders": "露肩",
        "sex toy": "带玩具", "pussy juice": "湿了", "after sex": "刚做完",
        "cum on body": "身上有精液", "cum on breasts": "胸上有精液",
        "bandaid on pussy": "下体贴着创可贴", "holding bucket": "拿着桶",
        "clothed female nude male": "女的穿着男的裸着",
        "standing split": "一字马站着", "glasses": "戴眼镜",
        "large_breasts": "胸很大", "huge_breasts": "胸非常大",
        "spread legs": "张开腿", "legs up": "腿抬起来",
        "penetration": "被插入", "cum in pussy": "内射",
        "cum in mouth": "口爆", "cum on face": "颜射",
        "tentacles": "触手", "rape": "被强上", "mind break": "被干到失神",
        "ahegao": "表情崩了", "saliva": "流口水", "sweat": "出汗",
        "on bed": "在床上", "in bed": "在床里", "bathroom": "在浴室",
        "shower": "在淋浴", "pool": "在泳池", "outdoors": "在户外",
        "pov": "第一人称视角", "from behind": "从后面",
        "ass visible": "能看到屁股", "spread ass": "掰开屁股",
        "panties aside": "内裤拨到一边", "clothes lift": "衣服撩起来",
        "breasts out": "胸露出来", "pussy peek": "下体若隐若现",
        "covered nipples": "隔着衣服露点", "sex from behind": "后入",
        "solo focus": "画面只有一个人", "pussy juice": "湿了",
        "clothed female nude male": "女的穿着男的裸着",
        "nude": "全裸", "nipples": "露点", "pussy": "下体露出",
        # ── BDSM / 束缚（2026-11-06 补：主人发了张 shibari 图，翻译表漏了最关键的几个词）──
        "shibari": "绳缚", "bondage": "被绑着", "bound": "被绑着", "restrained": "被束缚",
        "rope": "绳子", "red_rope": "红绳", "rope_bondage": "绳缚",
        "gag": "戴口球", "ball_gag": "口球", "gagged": "嘴被堵着", "bit_gag": "咬着马嚼",
        "blindfold": "蒙眼", "spread_eagle": "大字形绑着", "suspension": "悬吊",
        "arms_behind_back": "手反绑在背后", "bound_wrists": "手腕绑着",
        "bound_legs": "腿绑着", "bound_arms": "手臂绑着",
        "collar": "戴项圈", "leash": "被牵着", "chain": "锁链",
        "bdsm": "调教", "domination": "被支配", "submission": "臣服",
        "whip": "鞭子", "crop": "马鞭", "paddle": "拍打板",
        "clamp": "夹子", "nipple_clamp": "乳夹", "clit_clamp": "阴蒂夹",
        "anal_beads": "肛珠", "plug": "肛塞", "anal_plug": "肛塞",
        # ── 情色 / 性行为 ──
        "explicit": "明确成人内容", "censored": "打了码", "uncensored": "没有码",
        "sex": "在性交", "vaginal": "插入中", "anal": "后庭", "oral": "口交",
        "fellatio": "口交", "paizuri": "乳交", "handjob": "手交", "footjob": "足交",
        "masturbation": "自慰", "cum": "射了", "cumshot": "射在上面",
        "penetration": "被插入", "cum_in_pussy": "内射", "creampie": "内射",
        "sex_toy": "带玩具", "sex toy": "带玩具", "vibrator": "振动棒", "dildo": "假阳具",
        "onahole": "飞机杯", "pussy_juice": "湿了", "pussy juice": "湿了",
        "ahegao": "表情崩了", "mind_break": "被干到失神", "rape": "被强上",
        "tentacles": "触手", "orgasm": "高潮了", "climax": "高潮",
        # ── 露出 / 身体 ──
        "nipples": "露点", "covered_nipples": "隔着衣服露点", "covered nipples": "隔着衣服露点",
        "pussy": "下体露出", "pussy_peek": "下体若隐若现", "ass": "露臀",
        "large_breasts": "胸很大", "huge_breasts": "胸非常大", "medium_breasts": "胸不小",
        "cleavage": "露事业线", "navel": "露肚脐", "thighs": "露大腿",
        "bare_shoulders": "露肩", "collarbone": "露锁骨", "midriff": "露腰",
        "underboob": "露下乳", "sideboob": "露侧乳", "topless": "上身没穿",
        "bottomless": "下身没穿", "completely_nude": "一丝不挂", "nude": "全裸",
        "naked": "裸着", "nude_female": "女的裸着", "nude_male": "男的裸着",
        # ── 姿势 ──
        "lying": "躺着", "on_back": "仰躺", "on_stomach": "趴着", "on_side": "侧躺",
        "sitting": "坐着", "seiza": "跪坐", "kneeling": "跪着", "standing": "站着",
        "all_fours": "四肢撑地", "squatting": "蹲着", "wariza": "鸭子坐",
        "spread_legs": "张开腿", "spread legs": "张开腿", "legs_up": "腿抬起来",
        "legs_apart": "腿分开", "presenting": "摆姿势勾引", "arched_back": "拱着背",
        "arms_up": "举起手", "bent_over": "弯腰", "from_behind": "从后面",
        "pov": "第一人称视角", "foot_focus": "脚部特写",
        # ── 场景 / 服装 ──
        "indoors": "在屋里", "bedroom": "在卧室", "on_bed": "在床上", "in_bed": "在床里",
        "bathroom": "在浴室", "shower": "在淋浴", "pool": "在泳池", "outdoors": "在户外",
        "maid": "穿女仆装", "apron": "系着围裙", "white_apron": "白围裙",
        "dress": "连衣裙", "thighhighs": "过膝袜", "white_thighhighs": "白过膝袜",
        "panties": "只穿内裤", "bra": "只穿胸罩", "lingerie": "内衣", "bikini": "比基尼",
        "glasses": "戴眼镜", "hairband": "发箍", "headband": "头饰",
        "animal_ears": "兽耳", "whale_tail": "鲸鱼尾巴", "tail": "有尾巴",
        "blue_hair": "蓝头发", "blue_eyes": "蓝眼睛", "long_hair": "长发",
        # ── 表情 / 状态 ──
        "blush": "脸红了", "smile": "在笑", "open_mouth": "张着嘴", "closed_eyes": "闭着眼",
        "looking_at_viewer": "看着镜头", "sweat": "出汗", "saliva": "流口水",
        "tears": "有泪", "half-closed_eyes": "半眯着眼", "expressionless": "面无表情",
        "solo": "单人", "1girl": "一个女生", "2girls": "两个女生", "2boys": "两个男生",
        "hetero": "男女", "yuri": "百合", "yaoi": "男同",
    }
    picked = []
    for name, p, cat in tags:
        low = name.lower()
        # 2026-11-06 修：原来靠 _NSFW_KW/_POSE_KW 两个小列表筛，
        # shibari / gag / ball_gag / bound / rope 全不在里面 —— 一张绳缚图最后只说了
        # 「被绑着」三个字。改成「只要翻译表里有就显示」，按置信度从高到低取。
        _key = low.replace(" ", "_")
        _txt = _tr.get(_key) or _tr.get(low)
        if not _txt:
            continue
        if not with_nsfw and any(k in low for k in _NSFW_KW):
            continue
        if _txt not in picked:
            picked.append(_txt)
        if len(picked) >= 14:
            break
    if not picked:
        # 没有 NSFW/姿势标签，就退到最置信的三个通用标签
        picked = [n.replace("_", " ") for n, p, c in tags[:3] if c == "0"]
    return "·".join(dict.fromkeys(picked)) if picked else ""


async def describe(path_or_buf, with_nsfw: bool = True) -> str:
    """给一张图打标签，返回中文描述（失败返回空串）。"""
    try:
        if isinstance(path_or_buf, (str, Path)):
            p = Path(str(path_or_buf))
            if not p.exists():
                return ""
            buf = await asyncio.to_thread(p.read_bytes)
        else:
            buf = bytes(path_or_buf)
        if not buf:
            return ""
        tags = await asyncio.to_thread(_infer_sync, buf)
        return _fmt(tags, with_nsfw)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[WD14] 打标失败：{type(e).__name__}: {e}")
        return ""


def available() -> bool:
    return MODEL_FILE.exists() and TAGS_FILE.exists()

async def is_nsfw(path_or_buf, threshold: float = 0.7) -> bool:
    """这张图是不是成人内容（用 explicit / nsfw 标签概率判）。

    2026-11-06：给识图分流用 —— NSFW 图不该往云端发（会被封号），
    也不该指望 QwenVL（它一定拒），直接走 WD14。
    """
    try:
        if isinstance(path_or_buf, (str, Path)):
            p = Path(str(path_or_buf))
            if not p.exists():
                return False
            buf = await asyncio.to_thread(p.read_bytes)
        else:
            buf = bytes(path_or_buf)
        if not buf:
            return False
        tags = await asyncio.to_thread(_infer_sync, buf)
        for name, prob, _cat in tags:
            low = name.lower()
            if low in ("explicit", "nsfw", "questionable") and prob >= threshold:
                return low == "explicit" or low == "nsfw"
        return False
    except Exception:  # noqa: BLE001
        return False
