# 群友 Bot · 一个有内在世界的 QQ 群 AI

一个住在 QQ 群里的 AI 群友。**不是助手，是个人。**

它有自己的情绪曲线（5 条激素 → 8 种情绪）、四层记忆（原始对话 → 长期档案 → 向量索引 → 人格注入）、
91 个插件、43 个定时任务，没人找它也会自己动。

> 基于 [chennianlaobai/qq-ai-bot](https://github.com/chennianlaobai/qq-ai-bot)（MIT）改进。
> 原版是一个能聊天的群友，这一版在「内在世界」上做了大幅扩展。

---

## 它和普通聊天机器人的区别

| 普通机器人 | 这个 |
|---|---|
| 你问它答 | 想说话才说，不想说就沉默 |
| 情绪是提示词里的一句话 | 5 条激素实时衰减，派生 8 种情绪 |
| 记忆是聊天记录 | 4 层：原始 → 档案 → 向量 → 人格，按艾宾浩斯遗忘 |
| 人格写死在 prompt | 人格会固化（证据 ≥3 条 + 人点头） |
| 只在一个平台 | 能刷 B站、发抖音、发 QQ 空间、画画、唱歌 |

---

## 核心：内在世界

### 激素层

5 条激素，每分钟衰减一次，事件会推动它们：

```
DA    多巴胺    奖励与冲动
SER   血清素    稳定与满足
OXY   催产素    亲密与信任
CORT  皮质醇    压力
MEL   褪黑素    困倦
```

**8 种情绪**由激素派生，可多标签共存：`开心 害羞 情欲 烦 怒 丧 累 害怕`

**认知评价**（OCC 模型）：事件不直接改激素，先过四维评价——

```
相关性   这事跟我有关吗
利害     好还是坏
应对     我扛得住吗
归因     怪自己还是怪外面
```

被骂一句，如果当前状态扛得住，伤害打折；扛不住，伤害加剧。

### 记忆层

```
L1  原始对话      chat_history，一字不改
L2  长期档案      按艾宾浩斯分类半衰期衰减（约定 180 天 / 吃喝 35 天）
L3  向量索引      跨字面找语义相近的（"投简历"能召回"找工作"）
L4  人格注入      只取最近一小段，不整段砍
```

**复习增益**：一条记忆每被用到一次，半衰期 ×1.55，封顶 500 天。

**遗忘阈值**：生效权重 <0.12 就不进 prompt，但**档案里还留着**（想不起来 ≠ 删掉）。

**再激活**：召回的旧事会带情绪回来——想起被夸的事 DA/OXY 微升，想起被凶的事 CORT 微升。

### 人格层

```
成长闭环：事件 → 日/周复盘 → 提案 → 批准 → 固化进人设
```

每条成长条带**证据链**（至少 3 条证据），状态分三档：

```
solidified    已固化（写进 persona）
pending       攒够证据，等人点头
dismissed     复盘时判定不成立
```

**固化会破坏 prompt 缓存，所以不频繁**——这也是设计的一部分。

### 关系层

```
neg       负面分（-4/小时自然衰减）
conflict  吵过几回（一次 neg_add≥8 才算）
repair    修好几回（neg 从 ≥30 落到 <20 记一次）
peak      这次吵得多凶
```

**修好的关系，负面回落更快**（1.0 → 1.6 倍速）。

---

## 91 个插件，按功能分组

**对话核心**
`ai_group`（主对话，10000+ 行）、`agency`（能动性）、`hormones`、`mood_compound`、`emotion_residue`

**记忆**
`long_memory`、`memory_graph`、`memory_health`、`memory_importance`、`memory_maintain`、`trauma_mem`、`obsessions`、`dream`、`attention_drift`

**人格与心理**
`adler`（阿德勒）、`hierarchy`（马斯洛 6 层）、`human_touch`、`humanize`、`relation_stage`、`pad_anchor`

**平台**
`bilibili*`（7 个）、`douyin*`（4 个）、`qzone*`、`pixiv_duty`、`feishu_bridge`

**媒体**
`media_fetch`、`media_parse`、`asr_gate`、`seedvc`、`comfy_boot`、`image_edit`、`sticker_rank`

**内容生产**
`story`、`storyboard`、`daily_diary`、`web_search`、`deep_learn`、`reading`

**系统**
`schedule`、`self_check`、`health`、`cache_keepalive`、`memory_git`、`group_guard`

---

## 安装

### 需要什么

```
Python 3.12+
一个 QQ 小号（给机器人用）
NapCat（协议端，接管这个号）
一个 LLM API（默认豆包 / DeepSeek，可换）
```

### 五步

```bash
# 1. 拿代码
git clone <your-fork-url> qq-ai-bot
cd qq-ai-bot

# 2. 装依赖
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt

# 3. 配置（必填三项）
copy .env.example .env
# 打开 .env，填：
#   AI_API_KEY=你的密钥
#   AI_BASE_URL=接口地址
#   AI_MODEL=模型名

# 4. 装 NapCat 并登录 QQ 小号（见 docs/安装部署.md）

# 5. 起
.venv\Scripts\python bot.py
```

**装完先体检**：`python check.py`
它会逐项检查密钥 / 人设 / 记忆 / 依赖，有问题直接告诉你去哪改。

---

## 换成你自己的角色

**只改一个文件**：

```bash
copy persona.example.md persona.md
```

然后编辑 `persona.md`，把里面的角色名和设定改成你的。

**其他都不用动**。激素、记忆、人格进化这些是机制，跟角色是谁无关。

`persona.example.md` 里那份写得很完整（短句、有脾气、不用敬语、动作括号偶尔用），
可以直接当模板改，也可以照它的结构重写。

---

## 面板

自带一个只读面板，看她的内在状态：`http://127.0.0.1:8090`

```
顶部     7 张状态卡（本体/记忆/QQ/DSH/状态/待处理/B站/自检）
左侧     X-RAY：9 个模块 + 激素曲线
右侧     MODULE CONSOLE：L0~L8 详情（可点到底）
```

九个层级：

```
L0 核心     二十层链路是否全通
L1 当下     生理 6 项 + 精神 6 层（马斯洛）
L2 记忆     私聊/群聊/档案/向量
L3 语言     人设/词条/提及
L4 自成长   固化/待定/丢弃
L5 训练     自学知识库（带来源和证据）
L6 渠道     B站/抖音/空间/余额 + 花费明细
L7 调度     43 个定时任务 + 缓存命中率
L8 外部     DSH 桥 / 服务
```

---

## 缓存命中率

prompt 按「变化频率」分层：

```
system(稳定前缀)   ← 人设，逐字节稳定
历史多轮            ← 只增不改
extra_blocks       ← 每轮变的块（相关记忆/自学知识）
volatile           ← 状态/情绪/关系
时间块             ← 每轮都变，放最尾
user               ← 最新一句
```

**任何每轮都会变的内容，只能出现在 extra_blocks 之后**，否则会把整段历史拖出缓存区。

实测命中率：**79.4%**（今日）/ **73.5%**（累计）。面板 L7 能看实时数字。

---

## 目录结构

```
plugins/           91 个插件
tools/             工具脚本
dashboard/         只读面板（单文件 HTML，零框架）
dashboard-web/     面板的 3D 可视化（Vite 构建）
server/            内部服务
docs/              技术文档
bot.py             入口
config.py          配置（只读 .env，不硬编码）
persona.md         你的角色（从 example 复制）
memory/            运行时数据（不进仓库）
```

---

## 常见问题

**Q: 能不能不要那么多功能？**
可以。`plugins/` 里删掉对应文件就行，`bot.py` 会自动跳过。
最小可跑集：`ai_group.py` + `hormones.py` + `long_memory.py` + `config.py`。

**Q: 用哪个模型？**
默认豆包，也能换 DeepSeek / 硅基流动 / 本地 Ollama。改 `.env` 里的 `AI_BASE_URL` 和 `AI_MODEL`。

**Q: 会不会乱发消息？**
不会。默认有冷却窗口、每日限额、凌晨静默概率。所有「主动开口」都走审议，有 `observe` 模式只记录不发。

**Q: 数据存哪？**
```
memory/            JSON 文件（人可读）
data/              SQLite + 缓存
```
全部在本地，不上传。

**Q: 支持 Linux / Mac 吗？**
核心代码是纯 Python，跨平台。启动脚本（`start_all.ps1`）和桌宠是 Windows 的。

---

## 许可

[MIT](LICENSE)

基于 [chennianlaobai/qq-ai-bot](https://github.com/chennianlaobai/qq-ai-bot) 改进。

---

## 致谢

- [NoneBot2](https://nonebot.dev/) — 机器人框架
- [NapCat](https://github.com/NapNeko/NapCatQQ) — QQ 协议端
- [chennianlaobai/qq-ai-bot](https://github.com/chennianlaobai/qq-ai-bot) — 原始版本
- BioCore / MaiBot — 在情绪建模和「像人」上给了不少启发
