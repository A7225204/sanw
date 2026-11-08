# B 站接入（`plugins/bilibili.py`）

> 让 QQ 机器人在群里/私聊里直接“上 B 站”：发链接出视频卡片、查热搜、搜视频、看热门/排行、搜 UP 主。
> 全部走 B 站公开 Web 接口，**不需要登录、不需要 Cookie、不需要额外 Key**。

## 一、能干什么

| 你在 QQ 里发 | 机器人回什么 |
|---|---|
| 任意 B 站视频链接（`bilibili.com/video/BV...`、`b23.tv/...` 短链） | 视频卡片：标题、UP 主、时长、播放/弹幕/点赞、简介、封面图、原链接 |
| 单独一个 BV 号（如 `BV1GJ411x7h7`） | 同上 |
| `B站热搜`（或 `B站热榜`） | 当前 B 站热搜前 10 |
| `搜B站 关键词`（`B站搜索` / `B站搜` 同义） | 视频搜索结果前 5（标题 / UP / 播放 / 时长 / 链接） |
| `B站热门` | 热门视频前 10 |
| `B站排行`（或 `B站排行榜`） | 全站排行榜前 10 |
| `B站UP 名字` | UP 主搜索结果前 3（粉丝数 / 投稿数 / 空间链接） |
| 只发 `B站` | 指令说明 |

**群聊里要先 @ 机器人**（和现有指令一致，避免它乱插话）；私聊直接发即可。
`打开B站` 仍然走原来的分支——在电脑上打开浏览器，不属于本插件。

## 二、实现方式

* 插件文件：`plugins/bilibili.py`（触发词 / 权限 / 冷却 / 发消息）
* 取数与排版：`plugins/bilibili_api.py`（纯函数，可单独测试）
* 用到的公开接口：
  * 视频详情 `api.bilibili.com/x/web-interface/view`
  * 视频搜索 `api.bilibili.com/x/web-interface/search/type?search_type=video`
  * 用户搜索 `api.bilibili.com/x/web-interface/search/type?search_type=bili_user`
  * 热搜 `api.bilibili.com/x/web-interface/search/square`
  * 热门 `api.bilibili.com/x/web-interface/popular`
  * 排行榜 `api.bilibili.com/x/web-interface/ranking/v2`
* 短链（b23.tv）会先跟随 302 拿到真实链接再取 BV 号。
* 封面图带防盗链，插件会先下载再以 base64 发给 QQ，避免群里裂图。
* 同一会话 2 秒冷却，防连点刷屏。

## 三、怎么改

* 换触发词 / 加新指令：改 `plugins/bilibili.py` 顶部的 `_PREFIXES` 与 `handle()` 里的分支。
* 改排版：`plugins/bilibili_api.py` 的 `render_list()` / `video_card()`。
* 单独验证接口（不启动 NoneBot）：

```bash
cd D:\qq-ai-bot
.venv\Scripts\python.exe plugins\bilibili_api.py
```

* 改完先跑导入冒烟测试，再重启后端：

```bash
.venv\Scripts\python.exe tools\smoke_import.py
```

## 四、已知边界

* 视频卡片只取**公开信息**；不登录就看不到「仅粉丝可见 / 充电专属」等内容。
* 搜索结果是 B 站默认排序，不带个性化。
* 如果 B 站风控返回 `-412` / `-352`，会提示「B站接口抽风了」，稍等再试即可；插件每次请求都会带一个随机 `buvid3`，正常使用很少触发。
* 目前**不下发视频文件**（只发信息和封面）。要让它把视频文件直接发到群里，需要再做下载 + 转码 + 大小限制，属于下一步。

## 五、登录后互动（`plugins/bilibili_account.py`，主人专用）

上面那套是**只读**的；这一套会用到登录态、会真的发评论，所以只认主人
（`.env` 的 `OWNER_QQ_ID`，默认 100000000），并且严格限频。

### 命令

| 私聊发（群里要 @，且只有主人可用） | 作用 |
|---|---|
| `B站登录` | 私聊发二维码，用 B 站 App 扫码 + 确认；cookie 存在本机 `.bili_cookies.json`（已 gitignore） |
| `B站状态` | 显示登录身份 + 今日已用额度 |
| `B站看看 <视频>` | 看该视频热门评论（只读） |
| `B站评论 <视频> [内容]` | 在指定视频下发一条评论；不写内容就按大肥鱼人设自动生成 |
| `B站互动 <视频> [条数]` | 回复该视频下的热门评论（默认 1 条，最多 2 条） |
| `B站退出` | 清除本机保存的登录态 |

`<视频>` 支持 B 站链接、`b23.tv` 短链、`BV` 号、`av` 号。

### 安全与限频

* **只在主人指定的视频下**发言，不会自己跑去别人视频里刷评论。
* 10 分钟最多 3 条、每天最多 20 条、同一视频每天最多 3 条，内容重复会跳过。
* 登录态只存本机 `.bili_cookies.json`（权限 0600），已加进 `.gitignore`；`B站退出` 可随时清掉。
* 文案由大模型按人设生成（会强调“主人是示例主人”），发之前不做人工审核，靠上面的限频兜底。
* 风控报错（-412 / -352 等）会直接提示「B站风控拦下来了」，不会硬刷。

### 单独跑扫码登录（不走 QQ）

```bash
cd D:\qq-ai-bot
.venv\Scripts\python.exe tools\bili_login.py
```

会在仓库根目录生成 `bili_qr.png`，手机扫码确认后自动写入 `.bili_cookies.json`；超时或过期重跑一次即可。

### 风险提示

B 站对自动化评论有风控，**账号被限流/封禁的风险由使用者承担**。建议：

* 只在自己或朋友的视频下互动，别去热点视频底下刷；
* 保持上面的低频率，别手动绕过限频；
* 一旦出现验证码/风控提示，停一段时间再试。
