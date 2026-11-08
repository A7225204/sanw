第一个公开版。

## 有什么

**情绪**
- 5 条激素（DA 多巴胺 / SER 血清素 / OXY 催产素 / CORT 皮质醇 / MEL 褪黑素），每分钟衰减
- 8 种情绪派生（开心 / 害羞 / 情欲 / 烦 / 怒 / 丧 / 累 / 害怕），可多标签共存
- 认知评价（OCC）：事件先过「相关性 / 利害 / 应对 / 归因」四维，再改激素

**记忆**
- 4 层：原始对话 → 长期档案 → 向量索引 → 人格注入
- 艾宾浩斯遗忘：约定 / 偏好 180 天，吃喝天气 35 天
- 复习增益：每被用到一次，半衰期 ×1.55，封顶 500 天
- 遗忘阈值：权重 <0.12 不进 prompt，但档案里还留着
- 再激活：召回的旧事带回情绪（想起被夸 DA/OXY 微升，想起被凶 CORT 微升）

**人格**
- 成长闭环：事件 → 日/周复盘 → 提案 → 批准 → 固化
- 每条带证据链（≥3 条证据），三态：已固化 / 待定 / 已丢弃
- 固化会破坏 prompt 缓存，所以刻意不频繁

**关系**
- neg / conflict / repair / peak 四个字段
- 修好的关系负面回落更快（1.0 → 1.6 倍速）

**工程**
- 91 个插件、43 个定时任务、58 个命令
- 只读面板（L0~L8 九层，每层点到底）
- prompt 分层，缓存命中率 79.4%（今日）/ 73.5%（累计）

## 怎么装

```bash
git clone https://github.com/A7225204/sanw.git
cd sanw
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env              # 填 AI_API_KEY / AI_BASE_URL / AI_MODEL
copy persona.example.md persona.md  # 改成你自己的角色
.venv\Scripts\python bot.py
```

装完跑 `python check.py` 体检。

## 已知限制

- 3D 房间模型（91MB）没打包，需要自己准备（`dashboard-web/public/models/`）
- 桌宠、启动脚本是 Windows 的；核心代码跨平台
- 部分插件依赖本地服务（ComfyUI / TTS / ASR），不装也能跑基础对话
- 面板的 3D 可视化需要 Node.js（`cd dashboard-web && npm install`）

## 致谢

基于 [chennianlaobai/qq-ai-bot](https://github.com/chennianlaobai/qq-ai-bot)（MIT）改进。
BioCore / MaiBot 两个项目在情绪建模和「像人」上给了不少启发。
