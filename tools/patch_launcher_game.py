import pathlib

p = pathlib.Path(r"C:\Users\Administrator\Desktop\蓝色大肥鱼\start_all.ps1")
s = p.read_bytes().decode("utf-8-sig")
orig = s
changed = []

# 1) 路径变量：陪游戏状态/指令文件
if "$GAME_STATE" not in s:
    s = s.replace(
        "$EMBED_PY   = 'D:\\f5tts\\.venv\\Scripts\\pythonw.exe'",
        "$EMBED_PY   = 'D:\\f5tts\\.venv\\Scripts\\pythonw.exe'\n"
        "# 陪游戏（截图点评）：面板写指令文件，bot 每 5 秒读一次\n"
        "$GAME_CMD   = Join-Path $QQBOT_DIR 'memory\\game_watch_cmd.json'\n"
        "$GAME_STATE = Join-Path $QQBOT_DIR 'memory\\game_watch_state.json'",
        1,
    )
    changed.append("变量")

# 2) 服务清单：加一行（放在仪表盘前）
if 'Key="game"' not in s:
    s = s.replace(
        '    @{ Name="网页仪表盘";          Key="dashboard"; Port=$DASH_PORT }',
        '    @{ Name="陪游戏(截图点评)";     Key="game"; Port=0 },\n'
        '    @{ Name="网页仪表盘";          Key="dashboard"; Port=$DASH_PORT }',
        1,
    )
    changed.append("服务清单")

# 3) 判活：看状态文件 active
if '"game" { $svc.Check' not in s:
    s = s.replace(
        '        "dashboard" { $svc.Check = { (Get-DashboardState) -eq \'running\' } }',
        '        "game" { $svc.Check = {\n'
        '                   try {\n'
        '                       if (-not (Test-Path -LiteralPath $GAME_STATE)) { return $false }\n'
        '                       $g = Get-Content -LiteralPath $GAME_STATE -Raw -ErrorAction Stop | ConvertFrom-Json\n'
        '                       return [bool]$g.active\n'
        '                   } catch { return $false } } }\n'
        '        "dashboard" { $svc.Check = { (Get-DashboardState) -eq \'running\' } }',
        1,
    )
    changed.append("判活")

# 4) 启动：写 start 指令
if '"game" { ' not in s.split("function Start-One")[-1].split("function Stop-One")[0]:
    s = s.replace(
        '        "embed" { if (Test-PortListen $EMBED_PORT)',
        '        "game" { if (-not (Test-CmdAlive \'bot\\.py\')) { return "机器人没在跑，先启动她说" }\n'
        '                   $dir = Split-Path -Parent $GAME_CMD\n'
        '                   if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }\n'
        '                   \'{"cmd":"start"}\' | Set-Content -LiteralPath $GAME_CMD -Encoding UTF8\n'
        '                   Start-Sleep 6\n'
        '                   try {\n'
        '                       $g = Get-Content -LiteralPath $GAME_STATE -Raw -ErrorAction Stop | ConvertFrom-Json\n'
        '                       if ($g.active) { return "已开始陪玩（会随机截图点评）" }\n'
        '                   } catch { }\n'
        '                   return "已发指令，等她说一声（看 logs\\bot_restart.out.log）" }\n'
        '        "embed" { if (Test-PortListen $EMBED_PORT)',
        1,
    )
    changed.append("启动")

# 5) 停止：写 stop 指令
if '"game"  { ' not in s.split("function Stop-One")[-1]:
    s = s.replace(
        '        "embed"  { $p = Get-NetTCPConnection -LocalPort $EMBED_PORT',
        '        "game"  { $dir = Split-Path -Parent $GAME_CMD\n'
        '                   if (Test-Path -LiteralPath $dir) { \'{"cmd":"stop"}\' | Set-Content -LiteralPath $GAME_CMD -Encoding UTF8 }\n'
        '                   Start-Sleep 3\n'
        '                   return "已停止陪玩" }\n'
        '        "embed"  { $p = Get-NetTCPConnection -LocalPort $EMBED_PORT',
        1,
    )
    changed.append("停止")

if s != orig:
    p.write_bytes(("\ufeff" + s).encode("utf-8"))
print("start_all.ps1 改动:", changed)
err = None
