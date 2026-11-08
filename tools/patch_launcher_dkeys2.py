import pathlib

lp = pathlib.Path(r"C:\Users\Administrator\Desktop\蓝色大肥鱼\start_all.ps1")
raw = lp.read_bytes().decode("utf-8-sig")
lines = raw.splitlines()

start = None
for i, ln in enumerate(lines):
    if "-match '^([DX]" in ln:
        start = i
        break
if start is None:
    raise SystemExit("找不到 D/X 处理块")

# 从 start 往后找 "#}" 结束（该 elseif 块结尾的 "    }"）
end = None
for j in range(start + 1, min(start + 40, len(lines))):
    if lines[j].rstrip() == "    }":
        end = j
        break
if end is None:
    raise SystemExit("找不到块结尾")

new_block = '''    elseif ($cmd -match '^([DX])(\\d{1,2})$') {
        $num = [int]$Matches[2]
        # X1~X9：只停前 9 个（原习惯不变）
        # D1=第10个、D2=第11个、D3=第12个…以此类推，并且是「开关」：在跑就关，没跑就开
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
                    Add-Log ("关闭(第{0}个) {1}：{2}" -f ($idx + 1), $svc.Name, (Stop-One $svc.Key)) "Magenta"
                } else {
                    Add-Log ("开启(第{0}个) {1}…" -f ($idx + 1), $svc.Name) "Green"
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

lines[start:end + 1] = new_block.splitlines()
lp.write_bytes(("\ufeff" + "\n".join(lines) + "\n").encode("utf-8"))
print(f"已替换第 {start+1}~{end+1} 行：D 键改为开关，编号支持到 D*")
