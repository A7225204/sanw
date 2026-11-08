# see-now 实时陪看 · 使用说明（打游戏 / 直播接话）

> 装好的插件：`dsh-see-now@0.0.5`（DSH `web` profile）
> 工具名：**`see_now`**（重启 DSH 后在本会话里直接可见）
> 和 QQ 机器人的「看视频」是两条独立路子，互不影响：机器人那条是
> `D:\qq-ai-bot\plugins\media_fetch.py`（下载 → 分镜 → 字幕/ASR → 缓存）。

---

## 一、调用方法

### 1. 开始看（阻塞，这是常态）

```
see_now({ action: "watch", say: "行，我看着了，有动静我说" })
```

- 第一次约 1 秒返回当前屏幕；之后阻塞到攒够一张图（画面在变时 15~40 秒）
- **回完话立刻再调一次 `watch`** —— 阻塞本身就是「暂停」，不要结束回合
- `say` 是**唯一**能进悬浮窗并被念出来的通道，普通回复用户看不到

### 2. 被插件叫醒 → peek

```
see_now({ action: "peek", say: "这波可以" })
```

### 3. 收工

```
see_now({ action: "stop" })
```

用户关掉悬浮窗后不要再调用，直到他重新要求。

---

## 二、参数怎么选

| 场景 | 参数 |
|---|---|
| 日常陪看（默认） | `pace:"normal"` |
| 用户嫌吵/嫌贵（"少说点""省点钱"） | `pace:"calm"`（唯一真能降频的旋钮） |
| 用户嫌漏/嫌迟钝 | `pace:"dense"`（贵） |
| 打游戏解说 | `pace:"normal"`，只在团战/翻车/名场面说一句 |
| 直播逐场景接话（"每翻一页说一句"） | `scene:true`（粘性，很贵，用户没明说别开） |
| 看不清小字/文件名 | `quality:"high"`（立刻给一张锐利单帧），再不行 `ultra` |
| 屏幕很静、不想被 1 帧图打扰 | `minFrames: 2` |
| 看本地视频文件（调试） | `source:"file", path:"D:\\x.mp4", realtime:true` |

---

## 三、说话原则

用户和你**看的是同一块屏幕**：

- 不要复述画面，不要每张图都说一句
- 说的时候说「为什么值得看」（这波操作、这个报错、这个画面）
- 沉默是合法回答；工具结果会提醒上一句说了什么，别重复
- 一句 `say` 放两三句话，不要连续调用来补话

---

## 四、依赖与排障

| 问题 | 处理 |
|---|---|
| `spawn ffmpeg ENOENT` | 需要 ffmpeg。本机已做：软链 `%APPDATA%\npm\ffmpeg.exe → D:\qq-ai-bot\data\bin\ffmpeg.exe`，并设了用户变量 `DSH_FFMPEG_PATH=D:\qq-ai-bot\data\bin\ffmpeg.exe`（重启后优先生效） |
| 全屏独占游戏抓不到 | 换 `source:"obs"`（OBS 虚拟摄像头）或窗口化 |
| 只有字没有声 | 需要 `~/.dsh/voice.json` 配 TTS 适配器（和陪看共用同一份） |
| 想关掉插件 | 设置 → 插件 → `dsh-see-now` 禁用 |

---

## 五、本机实际验证

| 步骤 | 结果 |
|---|---|
| `see_now({action:"status"})` | 正常（未采集） |
| `see_now({action:"watch", seconds:30})` | 返回 2 帧拼图 800×450，等了 1s；进入常驻模式 |
| `see_now({action:"stop"})` | 正常停止 |

> 首次报过 `capture-stopped：spawn ffmpeg ENOENT`，做出上面的软链/环境变量后恢复。
