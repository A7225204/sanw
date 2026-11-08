import pathlib

p = pathlib.Path(r"D:\qq-ai-bot\server\memory_viz.py")
s = p.read_text(encoding="utf-8")

if '"emotion"' in s:
    print("已有 emotion 分类")
    raise SystemExit

old_helper = '''def _sentiment(text: str) -> float:'''
new_helper = '''_EMOTIONS = (
    ("怒", ("生气", "愤怒", "火大", "怒", "吼", "骂", "怼", "凶", "凶巴巴", "翻脸", "炸")),
    ("累", ("累", "疲惫", "困", "熬", "加班", "歇", "没力气", "不睡", "熬夜", "撑不住", "忙")),
    ("丧", ("丧", "崩溃", "难过", "哭", "失落", "失败", "难受", "抑郁", "绝望", "委屈")),
    ("烦", ("烦", "烦死", "无语", "嫌弃", "敷衍", "挑", "吵", "不爽", "厌倦", "腻")),
    ("害怕", ("害怕", "怕", "吓", "恐怖", "紧张", "担心", "慌", "不敢")),
)
_POS = ("喜欢", "开心", "高兴", "答应", "约定", "夸", "可爱", "萌", "好棒", "谢谢",
        "擅长", "顺利", "成功", "陪", "宠", "温柔", "笑", "靠谱", "在意", "认真", "进步")


def _emotion(text: str) -> str:
    """把记忆分成 怒/累/丧/烦/害怕/正面/中性。顺序就是优先级。"""
    t = str(text or "")
    for name, words in _EMOTIONS:
        if any(w in t for w in words):
            return name
    if any(w in t for w in _POS):
        return "正面"
    return "中性"


def _sentiment(text: str) -> float:'''
if old_helper in s:
    s = s.replace(old_helper, new_helper, 1)

s = s.replace('''        "sentiment": _sentiment(it.get("fact") or ""),''',
              '''        "sentiment": _sentiment(it.get("fact") or ""),
        "emotion": _emotion(it.get("fact") or ""),''', 1)

p.write_text(s, encoding="utf-8")
print("已加 emotion 分类（怒/累/丧/烦/害怕/正面/中性）")
