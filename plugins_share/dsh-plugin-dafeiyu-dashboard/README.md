# dsh-plugin-dafeiyu-dashboard

把「蓝色大肥鱼」只读仪表盘嵌进 DSH：设置 → 通用里多一个面板，直接看运行状态、抖音、B站、学习、记忆、日记、令牌和余额。

## 前提

本机先运行大肥鱼面板：

```powershell
cd D:\qq-ai-bot
.\.venv\Scripts\python.exe dashboard\server.py
```

默认地址：`http://127.0.0.1:8090`

## 安装

```powershell
dsh plugin --profile web add file:D:\path\to\dsh-plugin-dafeiyu-dashboard
```

然后重启 `dsh web`，打开 设置 → 通用，底部就是「蓝色大肥鱼仪表盘」。

## 卸载

```powershell
dsh plugin --profile web remove dsh-plugin-dafeiyu-dashboard
```

## 说明

- 只读面板，不会修改数据；
- 数据来源是 `%APPDATA%\dsh-pet-standalone-webm-chat\shared_memory.db` 和 `D:\qq-ai-bot\memory`；
- 面板没启动时，这里会显示空白页面，先启动 8090。
