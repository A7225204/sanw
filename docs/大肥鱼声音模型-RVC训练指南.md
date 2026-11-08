# 大肥鱼声音模型（RVC）云训练指南

本机 12G 显存能跑，但 RVC 训练在 Windows 上要编译 fairseq，很折腾；租一张 24G 卡半小时就训完，
花费约 3~5 元。训出来的模型是长期资产：以后她唱歌、说话都能用这个音色。

---

## 一、包里有什么

| 文件 | 位置 | 说明 |
|---|---|---|
| 数据集 | `D:\qq-ai-bot\data\voice_train\wavs\` | 56 条，9.8 分钟，44.1kHz 单声道、去静音、响度归一 |
| 训练包 | `D:\qq-ai-bot\data\rvc_kit.zip` | 数据集 + 脚本 + 说明，上传即用 |
| 参考音 | `D:\Seed-VC\ref_feiyu_clean.wav` | 单段干净参考音（免训也能先用） |

⚠️ 9.8 分钟偏少：RVC 建议 10~30 分钟。现在够训出可用模型，但想更稳可以再加素材
（她以后的语音消息、生成的歌都可以补进去）。

---

## 二、AutoDL 流程

### 1. 开机

```
镜像：社区镜像 → 搜 RVC 或选基础镜像 PyTorch 2.5 / CUDA 12.4
显卡：RTX 4090 24G（约 ¥1.6~2.2/小时）
```

### 2. 上传解压

```bash
cd /root/autodl-tmp
unzip rvc_kit.zip
```

### 3. 装 RVC（首次约 5 分钟）

```bash
cd /root
git clone https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI.git RVC
cd RVC
pip install -r requirements.txt
# Linux 上 fairseq 需要单独装
pip install fairseq==0.12.2 --no-build-isolation
# 若 fairseq 编译失败（常见），改用：
pip install git+https://github.com/facebookresearch/fairseq.git@v0.12.2
```

预训练模型（必须，否则训练报缺模型）：

```bash
mkdir -p assets/hubert assets/rmvpe assets/pretrained_v2 logs
python - <<'PY'
from huggingface_hub import hf_hub_download as d
import shutil, os
os.makedirs("assets/hubert", exist_ok=True); os.makedirs("assets/rmvpe", exist_ok=True)
shutil.move(d("lj1995/VoiceConversionWebUI", "hubert_base.pt"), "assets/hubert/hubert_base.pt")
shutil.move(d("lj1995/VoiceConversionWebUI", "rmvpe.pt"), "assets/rmvpe/rmvpe.pt")
for f in ("pretrained_v2/f0G48k.pth", "pretrained_v2/f0D48k.pth",
          "pretrained_v2/G48k.pth", "pretrained_v2/D48k.pth"):
    os.makedirs("assets/pretrained_v2", exist_ok=True)
    shutil.move(d("lj1995/VoiceConversionWebUI", f), "assets/" + f)
PY
```

### 4. 预处理 → 训练

```bash
cp -r /root/autodl-tmp/voice_train/wavs dataset/feiyu
python infer/modules/train/preprocess.py dataset/feiyu logs/feiyu 48000 4 python 0.3
python infer/modules/train/extract/extract_f0_print.py logs/feiyu 4 python
python infer/modules/train/extract_feature_print.py cuda:0 1 0 1 logs/feiyu python
python infer/modules/train/train.py \
  -e feiyu -sr 48k -f0 1 -bs 8 -g 0 -te 200 -se 20 \
  -pg assets/pretrained_v2/f0G48k.pth -pd assets/pretrained_v2/f0D48k.pth \
  -l 1 -c 0 -sw 1 -v v2
```

参数含义：

```
-te 200      总 epoch（9.8 分钟素材建议 150~250）
-se 20       每 20 epoch 存一次
-bs 8        batch（24G 卡可 8~12）
-sr 48k      采样率（唱歌用 48k，清晰度最高）
-f0 1        用 f0 引导，唱歌必须开
```

24G 卡预计 **20~40 分钟**。

### 5. 取出模型

```bash
ls assets/weights/       # feiyu_e200_s2000.pth 之类
ls logs/feiyu/           # 还有 index 文件
```

下载最小的那个 `.pth`（约 55MB）+ `added_*.index`（可选，检索用）。

### 6. 关机

AutoDL 控制台点关机，别让它在后台计费。

---

## 三、回来后怎么用

把 `.pth`（和 index）放到：

```
D:\qq-ai-bot\data\rvc_models\feiyu.pth
```

然后我改成：

```
ACE 生成 → demucs 分离 → RVC 变声（她的音色）→ 混回伴奏
```

RVC 推理比 Seed-VC 快很多（30 秒歌约 5~10 秒），而且**音色准确度和杂音控制都更好** —— 这就是为什么值得训。

---

## 四、验收标准

```
· 唱歌时能听出是她（而不是"某个女声"）
· 杂音/金属味明显少于 Seed-VC 零样本
· 换不同歌曲音色一致
· 音高不跑（f0 打开时必须）
```

不达标就调：`-te` 加到 300、`-bs` 加到 12、或补素材到 20 分钟重训。

---

## 五、省钱要点

```
· 4090 24G 按小时计费，训 40 分钟约 1.5 元
· 云端存盘 5GB 内免费，模型留在盘上下次还能接着训
· 训完立刻关机，别只关网页
```
