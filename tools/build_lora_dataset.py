"""为鲸鱼娘训练 LoRA 准备数据集：挑图、统一尺寸、写标注。

素材来源：
  C:\\Users\\Administrator\\dsh-pet\\_internal\\assets\\big_blue_fat_fish
  D:\\qq-ai-bot\\表情包\\<情绪>
  C:\\ComfyUI...\\output\\feiyu_*（她自己生成的）
输出：
  D:\\qq-ai-bot\\data\\lora_dataset\\feiyu\\  图片 + .txt 标注
"""
import hashlib
import pathlib
import shutil
from PIL import Image

ROOT = pathlib.Path(r"D:\qq-ai-bot")
OUT = ROOT / "data" / "lora_dataset" / "feiyu"
TRIGGER = "feiyu_girl"
CAPTION = (f"{TRIGGER}, 蓝色鲸鱼拟人少女, 深蓝色长发, 白色荷叶边头饰, "
           "蓝白女仆装, 鲸鱼尾巴, 圆脸大眼睛, 动漫插画风, 单人")

SOURCES = [
    pathlib.Path(r"C:\Users\Administrator\dsh-pet\_internal\assets\big_blue_fat_fish"),
    pathlib.Path(r"D:\qq-ai-bot\表情包"),
    pathlib.Path(r"C:\ComfyUI-aki(2)\ComfyUI-aki-v1.7\ComfyUI\output"),
]

MIN_SIDE = 700          # 太小/太糊的不要
SIZE = 768              # 训练统一尺寸（768 对 12G 卡比较稳）
MAX_ITEMS = 40


def collect() -> list[pathlib.Path]:
    out = []
    for src in SOURCES:
        if not src.exists():
            continue
        for p in src.rglob("*"):
            if p.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
                continue
            if src.name == "output" and not p.name.startswith("feiyu"):
                continue
            if p.stat().st_size < 60 * 1024:
                continue
            out.append(p)
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*"):
        old.unlink()
    seen = set()
    kept = 0
    for p in collect():
        if kept >= MAX_ITEMS:
            break
        try:
            im = Image.open(p)
            if min(im.size) < MIN_SIDE:
                continue
            im = im.convert("RGB")
            w, h = im.size
            side = min(w, h)
            im = im.crop(((w - side) // 2, (h - side) // 2,
                          (w + side) // 2, (h + side) // 2)).resize((SIZE, SIZE), Image.LANCZOS)
            data = im.tobytes()
            hsh = hashlib.md5(data).hexdigest()[:12]
            if hsh in seen:
                continue
            seen.add(hsh)
            dst = OUT / f"{kept:03d}_{hsh}.jpg"
            im.save(dst, "JPEG", quality=92)
            dst.with_suffix(".txt").write_text(CAPTION, encoding="utf-8")
            kept += 1
        except Exception:
            continue
    print(f"数据集完成：{kept} 张 → {OUT}")
    print(f"重名去重后唯一哈希：{len(seen)}")


if __name__ == "__main__":
    main()
