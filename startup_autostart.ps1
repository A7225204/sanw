# =====================================================================
# 蓝色大肥鱼全家桶 - 登录静默自启脚本
# 登录 Windows 时把未运行的组件后台拉起；已在运行则跳过，不双开。
# 由注册表 Run 项 dafish-autostart 调用；不弹浏览器、不 pause、不阻塞登录。
# 日志: D:\qq-ai-bot\logs\autostart.log
# 兼容 Windows PowerShell 5.1 (登录自启调用器)，命令均为单行，不使用续行符。
# =====================================================================
$ErrorActionPreference = "Continue"

$QQBOT_DIR = "D:\qq-ai-bot"
$PY  = Join-Path $QQBOT_DIR ".venv\Scripts\python.exe"
$PYW = Join-Path $QQBOT_DIR ".venv\Scripts\pythonw.exe"
$PET_EXE    = "C:\Users\Administrator\dsh-pet\dsh-pet-standalone-webm-chat.exe"
$RELAY_PY   = "C:\Users\Administrator\dsh-pet\volc_balance_relay.py"
$RELAY_DIR  = "C:\Users\Administrator\dsh-pet"
$NAPCAT_DIR = "D:\NapCat.52230.Shell"
$NAPCAT_BAT = Join-Path $NAPCAT_DIR "napcat.quick.bat"
$DSH_CMD    = "C:\Users\Administrator\AppData\Roaming\npm\dsh.cmd"
$DSH_WD     = "C:\Users\Administrator\.dsh"
$LOG_DIR    = Join-Path $QQBOT_DIR "logs"
$AUTO_LOG   = Join-Path $LOG_DIR "autostart.log"
$OUT_LOG    = Join-Path $LOG_DIR "bot_restart.out.log"
$ERR_LOG    = Join-Path $LOG_DIR "bot_restart.err.log"
$LOG_KEEP_DAYS = 14

if (-not (Test-Path -LiteralPath $LOG_DIR)) { New-Item -ItemType Directory -Path $LOG_DIR | Out-Null }

function Write-Log([string]$msg) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Add-Content -LiteralPath $AUTO_LOG -Value $line -Encoding utf8
}

# --- 日志轮转：按启动 / 按天归档，绝不覆盖历史 ---
function Rotate-LogByStart([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    $item = Get-Item -LiteralPath $path -ErrorAction SilentlyContinue
    if (-not $item -or $item.Length -le 0) { return $null }
    $dir = Split-Path -Parent $path
    $base = [System.IO.Path]::GetFileNameWithoutExtension($path)
    $ext = [System.IO.Path]::GetExtension($path)
    if (-not $ext) { $ext = ".log" }
    $dest = Join-Path $dir ("{0}.{1}{2}" -f $base, (Get-Date -Format "yyyyMMdd_HHmmss"), $ext)
    if (Test-Path -LiteralPath $dest) { $dest = Join-Path $dir ("{0}.{1}_{2}{3}" -f $base, (Get-Date -Format "yyyyMMdd_HHmmss"), (Get-Random -Minimum 100 -Maximum 999), $ext) }
    try { Move-Item -LiteralPath $path -Destination $dest -Force -ErrorAction Stop; return $dest }
    catch { try { Copy-Item -LiteralPath $path -Destination $dest -Force -ErrorAction Stop; return $dest } catch { return $null } }
}
function Rotate-LogDaily([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    $item = Get-Item -LiteralPath $path -ErrorAction SilentlyContinue
    if (-not $item -or $item.Length -le 0) { return $null }
    if ($item.LastWriteTime.Date -ge (Get-Date).Date -and $item.Length -lt 10MB) { return $null }
    $dir = Split-Path -Parent $path
    $base = [System.IO.Path]::GetFileNameWithoutExtension($path)
    $ext = [System.IO.Path]::GetExtension($path)
    if (-not $ext) { $ext = ".log" }
    $dest = Join-Path $dir ("{0}.{1}{2}" -f $base, (Get-Date -Format "yyyyMMdd"), $ext)
    if (Test-Path -LiteralPath $dest) { $dest = Join-Path $dir ("{0}.{1}_{2}{3}" -f $base, (Get-Date -Format "yyyyMMdd"), (Get-Random -Minimum 100 -Maximum 999), $ext) }
    try { Move-Item -LiteralPath $path -Destination $dest -Force -ErrorAction Stop; return $dest }
    catch { try { Copy-Item -LiteralPath $path -Destination $dest -Force -ErrorAction Stop; return $dest } catch { return $null } }
}
function Remove-OldArchives([string]$dir, [int]$keepDays, [string[]]$patterns) {
    if (-not (Test-Path -LiteralPath $dir)) { return }
    $cut = (Get-Date).AddDays(-$keepDays)
    foreach ($pat in $patterns) {
        Get-ChildItem -LiteralPath $dir -Filter $pat -File -ErrorAction SilentlyContinue |
            Where-Object { $_.LastWriteTime -lt $cut } |
            ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue }
    }
}


function Test-PortListen([int]$port) {
    return [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}

function Test-CmdMatch([string]$pattern) {
    foreach ($p in (Get-CimInstance Win32_Process | Where-Object { $_.Name -like '*python*' -or $_.Name -like '*node*' -or $_.Name -like '*cmd*' })) {
        if ([string]$p.CommandLine -match $pattern) { return $true }
    }
    return $false
}

function Test-ImageName([string]$exeName) {
    $procName = $exeName -replace '\.exe$', ''
    return [bool](Get-Process -Name $procName -ErrorAction SilentlyContinue)
}

# 按天归档自启日志（必须先归档再写本次记录：任何写入都会刷新 mtime，
# 写在前面会让跨天的旧日志被误判成"当天"而永不归档）
$autoArch = Rotate-LogDaily $AUTO_LOG
Remove-OldArchives $LOG_DIR $LOG_KEEP_DAYS @('autostart.*.log', 'bot_restart.out.*.log', 'bot_restart.err.*.log')
Write-Log "===== 登录自启开始 ====="
if ($autoArch) { Write-Log ("[日志] 自启日志已按天归档: {0}" -f (Split-Path -Leaf $autoArch)) }

# 0. 注入共享记忆到桌宠角色(一次性, 跑完退出)
$injectPy = Join-Path $QQBOT_DIR "update_pet_prompt.py"
if (Test-Path -LiteralPath $injectPy) {
    try {
        Start-Process -FilePath $PY -ArgumentList "update_pet_prompt.py" -WorkingDirectory $QQBOT_DIR -WindowStyle Hidden -Wait
        Write-Log "[0] 桌宠记忆注入完成"
    } catch {
        Write-Log "[0] 桌宠记忆注入失败: $($_.Exception.Message)"
    }
} else {
    Write-Log "[0] 未找到 update_pet_prompt.py, 跳过"
}

# 1. 桌面桌宠(需要可见窗口, 单例按映像名)
if (Test-ImageName "dsh-pet-standalone-webm-chat.exe") {
    Write-Log "[1] 桌宠已在运行, 跳过"
} else {
    Start-Process -FilePath $PET_EXE
    Write-Log "[1] 桌宠已拉起"
}

# 2. 记忆同步引擎 sync_memory.py(后台常驻, 单例按命令行)
if (Test-CmdMatch 'sync_memory\.py') {
    Write-Log "[2] sync_memory 已在运行, 跳过"
} else {
    Start-Process -FilePath $PYW -ArgumentList "sync_memory.py" -WorkingDirectory $QQBOT_DIR -WindowStyle Hidden
    Write-Log "[2] sync_memory 已拉起"
}

# 3. dsh web(3080, 隐藏后台, 不弹浏览器)
if (Test-PortListen 3080) {
    Write-Log "[3] dsh web 3080 已在运行, 跳过"
} else {
    $dshOut = Join-Path $DSH_WD "dsh_launch.log"
    $dshErr = Join-Path $DSH_WD "dsh_launch.log.err"
    # 显式指定 profile 环境变量：MCP 类插件从 web profile 解析，避免落到 desktop 默认路径
    $env:DSH_PROFILE     = "web"
    $env:DSH_PROFILE_DIR = Join-Path $DSH_WD "profiles\web"
    Start-Process -FilePath $DSH_CMD -ArgumentList '--profile','web' -WorkingDirectory $DSH_WD -RedirectStandardOutput $dshOut -RedirectStandardError $dshErr -WindowStyle Hidden
    Write-Log "[3] dsh web 3080 已拉起"
}

# 4. NapCat 协议端(自带单例; 按安装目录内进程判断, 避免孤儿 QQ.exe 漏判导致双开互踢)
$napcatAlive = [bool](Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.ExecutablePath -like "$NAPCAT_DIR\*" -and $_.Name -in @('QQ.exe', 'NapCatWinBootMain.exe') })
if ($napcatAlive) {
    Write-Log "[4] NapCat 已在运行(安装目录内进程), 跳过"
} elseif (Test-Path -LiteralPath $NAPCAT_BAT) {
    Start-Process -FilePath "cmd.exe" -ArgumentList '/c', "`"$NAPCAT_BAT`"" -WorkingDirectory $NAPCAT_DIR -WindowStyle Hidden
    Write-Log "[4] NapCat 已拉起"
    Start-Sleep -Seconds 5
} else {
    Write-Log "[4] 未找到 NapCat bat, 跳过"
}

# 5. bot.py 后端(后台常驻, 日志落 bot_restart.out/err.log, 单例按命令行)
if (Test-CmdMatch 'bot\.py') {
    Write-Log "[5] bot.py 已在运行, 跳过"
} else {
    # 按启动归档旧日志：Start-Process -RedirectStandardOutput 会截断目标文件，先改名保住历史
    $oldOut = Rotate-LogByStart $OUT_LOG
    $oldErr = Rotate-LogByStart $ERR_LOG
    if ($oldOut) { Write-Log ("[5] 旧 stdout 日志已归档: {0}" -f (Split-Path -Leaf $oldOut)) }
    if ($oldErr) { Write-Log ("[5] 旧 stderr 日志已归档: {0}" -f (Split-Path -Leaf $oldErr)) }
    Start-Process -FilePath $PY -ArgumentList "bot.py" -WorkingDirectory $QQBOT_DIR -RedirectStandardOutput $OUT_LOG -RedirectStandardError $ERR_LOG -WindowStyle Hidden
    Write-Log "[5] bot.py 已拉起"
}

# 5b. 本地 TTS(F5) —— 她平时说话的合成端,端口 9881
if (Test-PortListen 9881) {
    Write-Log "[5b] 本地TTS 已在运行, 跳过"
} else {
    $ltPy = "D:\f5tts\.venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $ltPy) {
        Start-Process -FilePath $ltPy -ArgumentList "tools\local_tts_server.py" -WorkingDirectory $QQBOT_DIR -WindowStyle Hidden -RedirectStandardOutput (Join-Path $LOG_DIR "local_tts.out.log") -RedirectStandardError (Join-Path $LOG_DIR "local_tts.err.log")
        Write-Log "[5b] 本地TTS 已拉起(9881)"
    } else {
        Write-Log "[5b] 未找到 F5 环境, 跳过"
    }
}

# 5c. RVC 变声(派蒙) —— 把 TTS 输出换成她的音色,端口 8101
if (Test-PortListen 8101) {
    Write-Log "[5c] RVC变声 已在运行, 跳过"
} else {
    $rvPy = "D:\RVC\.venv\Scripts\python.exe"
    $rvModel = "D:\RVC\assets\weights\paimeng.pth"
    if ((Test-Path -LiteralPath $rvPy) -and (Test-Path -LiteralPath $rvModel)) {
        Start-Process -FilePath $rvPy -ArgumentList "rvc_server.py","--model","assets/weights/paimeng.pth","--index","assets/indices/paimeng.index","--port","8101" -WorkingDirectory "D:\RVC" -WindowStyle Hidden -RedirectStandardOutput (Join-Path $LOG_DIR "rvc.out.log") -RedirectStandardError (Join-Path $LOG_DIR "rvc.err.log")
        Write-Log "[5c] RVC变声 已拉起(8101, 加载模型约 40 秒)"
    } else {
        Write-Log "[5c] 未找到 RVC 环境或模型, 跳过"
    }
}


# 5d. 生图格式代理(硅基流动 → OpenAI 格式) —— dsh-image-studio 用,端口 8850
if (Test-PortListen 8850) {
    Write-Log "[5d] 生图代理 已在运行, 跳过"
} else {
    $proxyPy = Join-Path $QQBOT_DIR "tools\siliconflow_image_proxy.py"
    if (Test-Path -LiteralPath $proxyPy) {
        Start-Process -FilePath $PY -ArgumentList "tools\siliconflow_image_proxy.py","--port","8850" -WorkingDirectory $QQBOT_DIR -WindowStyle Hidden -RedirectStandardOutput (Join-Path $LOG_DIR "image_proxy.out.log") -RedirectStandardError (Join-Path $LOG_DIR "image_proxy.err.log")
        Write-Log "[5d] 生图代理 已拉起(8850)"
    } else {
        Write-Log "[5d] 未找到生图代理脚本, 跳过"
    }
}


# 6. DSH 守护脚本自检 —— 计划任务 dsh-web-watchdog 调的是
#    %USERPROFILE%\.dsh\run_watchdog_hidden.vbs，
#    这个 .vbs 丢过一次，结果每 3 分钟弹一次「Windows Script Host 无法找到脚本文件」。
#    这里每次自启检查一遍，缺了就补（内容纯 ASCII，避免 wscript 编码问题）。
$dshWd = Join-Path $env:USERPROFILE ".dsh"
$vbsPath = Join-Path $dshWd "run_watchdog_hidden.vbs"
$ps1Path = Join-Path $dshWd "watchdog_dsh_web.ps1"
if ((Test-Path -LiteralPath $ps1Path) -and -not (Test-Path -LiteralPath $vbsPath)) {
    $vbsBody = @'
' dsh-web-watchdog: silently runs watchdog_dsh_web.ps1 (called every 3 minutes).
' ASCII-only on purpose: WScript does not parse UTF-8 BOM reliably.
Option Explicit
Dim sh, fso, ps1, cmd
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

ps1 = "C:\Users\Administrator\.dsh\watchdog_dsh_web.ps1"
If Not fso.FileExists(ps1) Then
    WScript.Quit 1
End If

cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & ps1 & """"
sh.Run cmd, 0, False
'@
    Set-Content -LiteralPath $vbsPath -Value $vbsBody -Encoding ASCII
    Write-Log "[6] 补回 run_watchdog_hidden.vbs（计划任务 dsh-web-watchdog 需要它）"
}
# 计划任务若卡在 Running，会因 MultipleInstances=IgnoreNew 再也跑不起来
try {
    $st = (Get-ScheduledTask -TaskName 'dsh-web-watchdog' -ErrorAction SilentlyContinue).State
    if ($st -eq 'Running') {
        Stop-ScheduledTask -TaskName 'dsh-web-watchdog' -ErrorAction SilentlyContinue
        Write-Log "[6] 结束卡住的 dsh-web-watchdog 实例"
    }
} catch {}

# --- Kimi WebBridge daemon（/投简历 依赖，端口 10086；浏览器扩展约 20 秒后自动连上）---
try {
    if (-not (Test-PortListen 10086)) {
        $wb = Join-Path $env:USERPROFILE ".kimi-webbridge\bin\kimi-webbridge.exe"
        if (Test-Path -LiteralPath $wb) {
            Start-Process -FilePath $wb -ArgumentList 'start' -WindowStyle Hidden
            Write-Log "[7] kimi-webbridge 已拉起（等扩展自动连接）"
        } else {
            Write-Log "[7] 没找到 kimi-webbridge（$wb），跳过"
        }
    } else {
        Write-Log "[7] kimi-webbridge 已在运行"
    }
} catch {
    Write-Log "[7] kimi-webbridge 启动失败：$($_.Exception.Message)"
}

Write-Log "===== 登录自启结束 ====="


