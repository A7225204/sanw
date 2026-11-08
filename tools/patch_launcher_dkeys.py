import pathlib

lp = pathlib.Path(r"C:\Users\Administrator\Desktop\蓝色大肥鱼\start_all.ps1")
s = lp.read_bytes().decode("utf-8-sig")
orig = s
changed = []

# ---------- 1) D 键改成"开关"（运行中就停，没运行就启），编号 = 服务序号 ----------
old = '''    elseif ($cmd -match '^([DX])(\\d{1,2})$') {
        $num = [int]$Matches[2]
        # D1=第10个(本地识图)，D2=第11个(仪表盘)；X1~X9 停前9个；D3~D9 兼容旧习惯
        if ($Matches[1] -eq 'D' -and $num -le 2) { $idx = 8 + $num } else { $idx = $num - 1 }
        if ($idx -ge 0 -and $idx -lt $Services.Count) {
            $svc = $Services[$idx]
            if ($svc.Key -eq 'dsh') {
                $script:Prompt = "确认停止 dsh web？当前 DSH 页面会断开 (Y/N) "
                Draw-Dashboard
                $ans = [Console]::ReadKey($true).KeyChar
                $script:Prompt = $null
                if ($ans -notmatch '[yY]') { Draw-Dashboard; continue }
            }
            Add-Log ("停止 {0}：{1}" -f $svc.Name, (Stop-One $svc.Key)) "Magenta"
        }
    }'''
new = '''    elseif ($cmd -match '^([DX])(\\d{1,2})$') {
        $num = [int]$Matches[2]
        # X1~X9：停前 9 个（原来的习惯不变）
        # D1=第10个、D2=第11个、D3=第12个…以此类推，且是「开关」：在跑就停，没跑就启
        if ($Matches[1] -eq 'D') { $idx = 8 + $num } else { $idx = $num - 1 }
        if ($idx -ge 0 -and $idx -lt $Services.Count) {
            $svc = $Services[$idx]
            $running = $false
            try { $running = [bool](& $svc.Check) } catch { }
            if ($Matches[1] -eq 'D') {
                if ($running) {
                    if ($svc.Key -eq 'dsh') {
                        $script:Prompt = "确认停止 dsh web？当前 DSH 页面会断开 (Y/N) "
                        Draw-Dashboard
                        $ans = [Console]::ReadKey($true).KeyChar
                        $script:Prompt = $null
                        if ($ans -notmatch '[yY]') { Draw-Dashboard; continue }
                    }
                    Add-Log ("关闭 {0}：{1}" -f $svc.Name, (Stop-One $svc.Key)) "Magenta"
                } else {
                    Add-Log ("开启 {0}…" -f $svc.Name) "Green"
                    Start-One-Async $svc.Key $svc.Name
                }
            } else {
                if ($svc.Key -eq 'dsh') {
                    $script:Prompt = "确认停止 dsh web？当前 DSH 页面会断开 (Y/N) "
                    Draw-Dashboard
                    $ans = [Console]::ReadKey($true).KeyChar
                    $script:Prompt = $null
                    if ($ans -notmatch '[yY]') { Draw-Dashboard; continue }
                }
                Add-Log ("停止 {0}：{1}" -f $svc.Name, (Stop-One $svc.Key)) "Magenta"
            }
        }
    }'''
if old in s:
    s = s.replace(old, new, 1)
    changed.append("D 键改为开关")
else:
    print("D 键逻辑没匹配上")

# ---------- 2) 面板按键提示 ----------
s = s.replace('Write-Host "X1~X9 / D1 D2" -NoNewline -ForegroundColor Magenta',
              'Write-Host "X1~X9 / D1~D$($Services.Count - 9)" -NoNewline -ForegroundColor Magenta', 1)

# ---------- 3) 帮助文字 ----------
s = s.replace('Write-Host "  D1 / D2      停止第 10 / 11 个服务（本地识图 / 仪表盘）" -ForegroundColor Gray',
              'Write-Host "  D1 ~ D$($Services.Count - 9)      第 10 个起的服务「开/关」切换（D1=第10个…以此类推）" -ForegroundColor Gray', 1)

# ---------- 4) 服务清单里本地识图那行写明 ComfyUI，方便对照 ----------
s = s.replace('@{ Name="本地识图(ComfyUI $VisionLabel)"; Key="comfy"; Port=$COMFY_PORT },',
              '@{ Name="ComfyUI 本地识图($VisionLabel)"; Key="comfy"; Port=$COMFY_PORT },', 1)

if s != orig:
    lp.write_bytes(("\ufeff" + s).encode("utf-8"))
print("start_all.ps1 改动:", changed)
