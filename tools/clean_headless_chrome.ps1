# 清理残留的无头 Chrome（headless 实例）
#
# 背景：面板/页面验证常开 `--headless=new --enable-unsafe-swiftshader`，
# 这种实例没有 GPU，WebGL 全靠 CPU 软件渲染，跑 Three.js 页面会持续吃满 CPU。
# 验证脚本异常退出时进程会残留，越积越多（实测曾堆到 11 个主进程 → CPU 49%）。
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\clean_headless_chrome.ps1 -WhatIf   # 只看会杀谁
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\clean_headless_chrome.ps1           # 真杀
#
# 安全约束（重要）：
#   1. 只处理命令行里带 --headless 的 chrome.exe，绝不碰正常浏览器
#   2. 有可见窗口的进程一律跳过
#   3. 项目自有的浏览器 profile 一律跳过（BOSS 直聘投递用的 chrome_profile 等）
#   4. QQ空间 / 抖音 的自动化走 Playwright + channel=msedge（msedge.exe），本脚本根本不涉及
param(
    [switch]$WhatIf
)

$ErrorActionPreference = 'SilentlyContinue'

$all = Get-CimInstance Win32_Process -Filter "Name='chrome.exe'"
if (-not $all -or $all.Count -eq 0) {
    Write-Output "没有 chrome 进程，无需清理"
    exit 0
}

# 项目自有 profile 的关键字（含这些字样的 --user-data-dir 不杀）
$PROTECTED = @('qq-ai-bot\data', 'boss_profile', 'chrome_profile')

$roots = @()
foreach ($p in $all) {
    $cmd = [string]$p.CommandLine
    if ($cmd -notlike '*--headless*') { continue }
    if ($cmd -like '*--type=*') { continue }

    $proc = Get-Process -Id $p.ProcessId -ErrorAction SilentlyContinue
    if ($proc -and $proc.MainWindowHandle -ne 0) { continue }

    $udd = ''
    if ($cmd -match '--user-data-dir=("[^"]+"|\S+)') {
        $udd = $Matches[1].Trim('"')
    }
    $skip = $false
    foreach ($prot in $PROTECTED) {
        if ($udd -and ($udd -like "*$prot*")) { $skip = $true; break }
    }
    if ($skip) { continue }

    $roots += [pscustomobject]@{ Pid = $p.ProcessId; Cmd = $cmd; Profile = $udd }
}

if ($roots.Count -eq 0) {
    Write-Output ("chrome 共 {0} 个进程，其中无头主进程 0 个 —— 无需清理" -f $all.Count)
    exit 0
}

# 父子关系表（用于连同子进程一起结束）
$byParent = @{}
foreach ($p in $all) {
    if (-not $byParent.ContainsKey($p.ParentProcessId)) { $byParent[$p.ParentProcessId] = @() }
    $byParent[$p.ParentProcessId] += $p.ProcessId
}
function Get-Tree([int]$root) {
    $out = @($root)
    $stack = @($root)
    while ($stack.Count -gt 0) {
        $cur = $stack[0]
        $stack = @($stack | Select-Object -Skip 1)
        if ($byParent.ContainsKey($cur)) {
            foreach ($c in $byParent[$cur]) { $out += $c; $stack += $c }
        }
    }
    return $out
}

$targets = @()
foreach ($r in $roots) {
    $tree = Get-Tree $r.Pid
    Write-Output ("  主进程 pid={0,-6} 子进程 {1,-3} profile={2}" -f $r.Pid, ($tree.Count - 1), $r.Profile)
    $targets += $tree
}
$targets = $targets | Sort-Object -Unique

Write-Output ("无头主进程 {0} 个，需结束 {1} 个进程" -f $roots.Count, $targets.Count)

if ($WhatIf) {
    Write-Output "（-WhatIf：只列出，不执行）"
    exit 0
}

$killed = 0
foreach ($targetPid in $targets) {
    taskkill /PID $targetPid /F 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { $killed++ }
}
Write-Output ("已结束 {0} 个进程" -f $killed)
