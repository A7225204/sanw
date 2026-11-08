"""跑 GPT-SoVITS v2 的完整预处理 + 训练（用仓库自带的 webui 函数驱动）。

不用 Gradio 界面，直接调 open1abc / open1Ba / open1Bb，和点按钮是同一套逻辑。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(r"D:\GPT-SoVITS\GPT-SoVITS")
LIST = Path(r"D:\GPT-SoVITS\dataset\gpt_sovits_ready\gpt_sovits.list")
EXP = "feiyu"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main() -> int:
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    os.environ.setdefault("CUDA_VISIBLE_DEVICES", "0")
    os.environ["_CUDA_VISIBLE_DEVICES"] = "0"

    pm = ROOT / "GPT_SoVITS" / "pretrained_models"
    bert = str(pm / "chinese-roberta-wwm-ext-large")
    hubert = str(pm / "chinese-hubert-base")
    s2g = str(pm / "s2G2333k.pth")
    s2d = str(pm / "s2D2333k.pth")

    for p in (LIST, Path(bert), Path(hubert), Path(s2g), Path(s2d)):
        if not p.exists():
            log(f"缺少：{p}")
            return 1

    log("导入 webui（会加载 gradio，约 10~30 秒）…")
    from webui import open1abc, open1Ba  # noqa: E402

    log("① 特征提取（get-text / hubert / semantic）…")
    open1abc(
        version="v2",
        inp_text=str(LIST),
        inp_wav_dir="",
        exp_name=EXP,
        gpu_numbers1a="0",
        gpu_numbers1Ba="0",
        gpu_numbers1c="0",
        bert_pretrained_dir=bert,
        ssl_pretrained_dir=hubert,
        pretrained_s2G_path=s2g,
    )
    log("① 预处理结束")

    log("② 训练 s2 声学模型（8 epoch，约 15~25 分钟）…")
    open1Ba(
        version="v2",
        batch_size=6,
        total_epoch=8,
        exp_name=EXP,
        text_low_lr_rate=0.4,
        if_save_latest=True,
        if_save_every_weights=True,
        save_every_epoch=4,
        gpu_numbers1Ba="0",
        pretrained_s2G=s2g,
        pretrained_s2D=s2d,
        if_grad_ckpt=False,
        lora_rank=32,
    )
    log("② s2 训练结束")
    log("完成，模型在 logs/%s/" % EXP)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
