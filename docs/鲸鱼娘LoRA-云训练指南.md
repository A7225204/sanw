# 鲸鱼娘 LoRA 云训练指南

本机 12GB 显存跑不动 Z-Image（6B）训练，ComfyUI 直接崩溃。已把数据和脚本准备好，
下面两条路任选一条，训完把文件丢回来就行。

---

## 一、你要用的东西

| 文件 | 位置 | 用途 |
|---|---|---|
| 数据集 zip | `D:\qq-ai-bot\data\feiyu_lora_dataset.zip` | 40 张图 + 40 个标注，5.3MB |
| 训练脚本 | `D:\qq-ai-bot\tools\train_lora.py` | 走 ComfyUI 原生 TrainLoraNode |
| 数据集原始目录 | `D:\qq-ai-bot\data\lora_dataset\feiyu\` | zip 里的内容 |

标注格式（每张图同名 .txt）：

```
feiyu_girl, 蓝色鲸鱼拟人少女, 深蓝色长发, 白色荷叶边头饰, 蓝白女仆装, 鲸鱼尾巴, 圆脸大眼睛, 动漫插画风, 单人
```

**触发词就是 `feiyu_girl`**，生成时带上它才会出这个角色。

---

## 二、方案 A1：租一张 24GB 卡自己跑（最稳，约 3~5 元）

推荐 AutoDL（国内，4090 24G 约 ¥1.6/小时）。

### 步骤

```
1. 开机选镜像：PyTorch 2.5 + CUDA 12.4，或者直接用「ComfyUI」社区镜像
2. 上传 feiyu_lora_dataset.zip，解压到 ComfyUI/input/feiyu_lora/
   （必须是 input 下的子目录，训练节点只认这里）
3. 上传 tools/train_lora.py 到 ComfyUI 同级目录
4. 确认这三个模型在 ComfyUI/models 下：
     diffusion_models\z_image_turbo_bf16.safetensors
     text_encoders\qwen_3_4b.safetensors
     vae\diffusion_pytorch_model.safetensors
   （没有就从 ModelScope 下 Comfy-Org/Qwen-Image-2.1 和 Z-Image 仓库，很快）
5. 启动 ComfyUI，然后跑：
     python tools/train_lora.py --steps 1500 --rank 16 --lr 0.0004
6. 训完文件在 models\loras\feiyu_girl_*.safetensors
```

### 参数建议（已写进脚本默认值附近）

```
步数      1500（40 张图，约 37 步/图，够收敛）
rank      16
学习率    4e-4
分辨率    768（数据集已是 768²）
batch     1（24G 卡可以试 2）
优化器    AdamW
bf16      开
梯度检查  开
offload   开（省显存，慢一点）
```

24G 卡预估：**1.5~2.5 小时**。

---

## 三、方案 A2：在线平台（不用自己配环境）

如果平台已经有 **Z-Image 训练模板**，直接把 zip 传上去：

```
LiblibAI（哩布哩布）→ 模型训练 → 选 Z-Image / DiT 基座 → 上传 zip
魔搭 ModelScope → 创空间/训练任务 → 上传数据集
```

⚠️ 一定要确认基座是 **Z-Image**。选成 SDXL / Flux 训出来的 LoRA 在你这套出图流程里**加载不了**。

平台一般会问这些，照填：

```
触发词      feiyu_girl
分辨率      768
rank        16
学习率      4e-4
步数        1500
batch       1
```

---

## 四、训完怎么交付

把 safetensors 放到（或发给我，我来放）：

```
C:\ComfyUI-aki(2)\ComfyUI-aki-v1.7\ComfyUI\models\loras\feiyu_girl.safetensors
```

然后我会做三件事：

```
1. 在 ComfyUI 里确认能加载（列出 LoRA 名）
2. 写进一致性工作流：LoraLoaderModelOnly(lora_name=feiyu_girl, strength 0.8)
3. 出一张验证图，看形象是不是稳定了
```

---

## 五、验收标准

训好的 LoRA 应该满足：

```
· 提示词只写 "feiyu_girl, 站在草地上"，就能出她的脸和女仆装
· 换场景（海边/教室/夜晚）形象不跑偏
· 强度 0.7~0.9 之间最稳，太高会僵、太低没效果
```

不达标就回头调：步数 ±500、rank 8↔32、学习率 ±1e-4。
