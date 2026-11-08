# AutoDL 租卡训练鲸鱼娘 LoRA（一次约 3~5 元）

照着点，全程 20 分钟左右操作，训练 1.5~2.5 小时（挂机）。

---

## 第 1 步：开机

```
1. autodl.com 注册登录 → 算力市场
2. 选「RTX 4090 / 24GB」（约 ¥1.6~2.2/小时），地区选便宜的
3. 镜像选：社区镜像 → 搜 ComfyUI → 选一个带 CUDA12 + Torch2.5 的
   （没有就选「基础镜像 PyTorch 2.5.0 / CUDA 12.4」）
4. 创建并开机
```

## 第 2 步：上传这两个文件

在 AutoDL 的「JupyterLab / 文件管理」里上传：

```
autodl_lora_kit.zip          ← 我打好的包（含数据集 + 训练脚本）
```

## 第 3 步：解压到位

打开终端（JupyterLab → Terminal）：

```bash
cd /root/autodl-tmp
unzip autodl_lora_kit.zip
```

如果是**社区 ComfyUI 镜像**，ComfyUI 一般在 `/root/ComfyUI`：

```bash
mkdir -p /root/ComfyUI/input/feiyu_lora
cp -r /root/autodl-tmp/kit/feiyu/* /root/ComfyUI/input/feiyu_lora/
cp /root/autodl-tmp/kit/train_lora.py /root/ComfyUI/
```

如果是**基础镜像**（没装 ComfyUI），先装：

```bash
cd /root
git clone https://github.com/comfyanonymous/ComfyUI
cd ComfyUI && pip install -r requirements.txt
mkdir -p input/feiyu_lora
cp -r /root/autodl-tmp/kit/feiyu/* input/feiyu_lora/
cp /root/autodl-tmp/kit/train_lora.py .
```

## 第 4 步：补三个模型（AutoDL 走魔搭，很快）

```bash
cd /root/ComfyUI/models
mkdir -p diffusion_models text_encoders vae

# 扩散模型 7.26GB
curl -L -o diffusion_models/z_image_turbo_bf16.safetensors \
  "https://modelscope.cn/api/v1/models/Comfy-Org/Z-Image/resolve/master/diffusion_models/z_image_turbo_bf16.safetensors"

# 文本编码器 7.49GB
curl -L -o text_encoders/qwen_3_4b.safetensors \
  "https://modelscope.cn/api/v1/models/Comfy-Org/Qwen-Image_ComfyUI/resolve/master/split_files/text_encoders/qwen_3_4b.safetensors"

# VAE 0.16GB
curl -L -o vae/diffusion_pytorch_model.safetensors \
  "https://modelscope.cn/api/v1/models/Comfy-Org/Z-Image/resolve/master/vae/diffusion_pytorch_model.safetensors"
```

> 三个都下不来就直接从你自己电脑上传（models 下那三个文件拷过去），更保险。

## 第 5 步：开跑

```bash
cd /root/ComfyUI
python main.py --listen 127.0.0.1 --port 8188 &
sleep 60
python train_lora.py --steps 1500 --rank 16 --lr 0.0004
```

脚本会每 2 分钟报一次进度，跑完打印结果路径。

## 第 6 步：取回文件 + 关机

结果在：

```
/root/ComfyUI/models/loras/feiyu_girl_00001_.safetensors
```

在 JupyterLab 文件管理里右键下载，然后**到 AutoDL 控制台点关机**（不然一直扣钱）。

---

## 回到本机

把这个文件放到：

```
C:\ComfyUI-aki(2)\ComfyUI-aki-v1.7\ComfyUI\models\loras\feiyu_girl.safetensors
```

然后告诉我，我来：

1. 确认 ComfyUI 能加载
2. 写进一致性工作流（强度 0.8）
3. 出一张验证图，跑不跑偏一看就知道

---

## 参数备忘

```
触发词   feiyu_girl
步数     1500（不够像就 2000，过头就 1000）
rank     16（想更精细 32，想轻量 8）
学习率   4e-4
分辨率   768（数据集就是这个尺寸，别改）
batch    1（24G 可以试 2，快一倍）
```

## 省钱提醒

```
· 训练完立刻关机，按时长计费
· 关机后磁盘还留着，下次开机能接着跑
· 预计总花费 3~5 元（2 小时 × 1.6~2.2）
```
---

## 附录：RTX 5090（32G）注意点

你现在选的这台是 **5090 32GB / CUDA 驱动 13.2**，比 4090 更宽松，但有两个坑：

```
① 架构是 Blackwell（sm_120），老 PyTorch 不认
   → 必须 PyTorch ≥ 2.7，且是 cu128 及以上的轮子
   → 社区镜像里挑「ComfyUI + torch2.7/cu128」那类
   → 基础镜像下拉里选 PyTorch 2.7.0 或 2.8.0

② 数据盘只有 50GB
   ComfyUI 环境 + torch 约 10GB，三个模型约 15GB，训练输出几 GB
   → 够用但别存多余镜像；不够就把数据盘扩到 100GB（几毛钱）
```

32GB 显存可以放宽参数：

```bash
python train_lora.py --steps 1500 --rank 32 --batch 2
```

预计 1~1.5 小时跑完，花费约 ¥3~4。
