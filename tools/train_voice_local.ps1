# 本机训练大肥鱼声音模型（so-vits-svc-fork，12G 卡够用）
# 用法：开着梯子跑这个脚本一次（要下 380MB 的 hubert），之后训练不需要网。
#   powershell -ExecutionPolicy Bypass -File D:\qq-ai-bot\tools\train_voice_local.ps1

$ErrorActionPreference = 'Stop'
$Venv = 'D:\so-vits-svc\.venv\Scripts'
$Work = 'D:\so-vits-svc'
$Log  = 'D:\qq-ai-bot\logs\voice_train.log'

function Log($msg) {
    $line = "$(Get-Date -Format 'HH:mm:ss')  $msg"
    $line | Tee-Object -FilePath $Log -Append
}

Log "===== 开始本机声音训练 ====="
Set-Location $Work

# 1) 取 hubert（content-vec-best）：有缓存就跳过
Log "检查 hubert 模型…"
$cache = Join-Path $env:USERPROFILE '.cache\huggingface\hub\models--lengyue233--content-vec-best'
if (Test-Path $cache) {
    Log "hubert 已缓存，跳过下载"
} else {
    Log "下载 hubert（约 380MB，需要梯子）…"
    $env:HF_ENDPOINT = 'https://huggingface.co'
    Remove-Item Env:HF_HUB_OFFLINE -ErrorAction SilentlyContinue
    & "$Venv\python.exe" -c "from huggingface_hub import snapshot_download; snapshot_download('lengyue233/content-vec-best')"
    if ($LASTEXITCODE -ne 0) {
        Log "❌ hubert 下载失败 —— 请确认梯子已开，然后重跑本脚本"
        exit 1
    }
    Log "hubert 下载完成"
}

# 2) 之后全部离线
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'

# 3) 预处理（已有可跳过）
if (-not (Test-Path 'D:\so-vits-svc\dataset\44k\feiyu')) {
    Log "重采样…"
    & "$Venv\svc.exe" pre-resample 2>&1 | Out-File -Append $Log
    Log "生成配置…"
    & "$Venv\svc.exe" pre-config -c configs\config.json 2>&1 | Out-File -Append $Log
}
if (-not (Get-ChildItem 'D:\so-vits-svc\dataset\44k\feiyu' -Filter '*.npz' -ErrorAction SilentlyContinue)) {
    Log "提取 hubert+f0 特征（约 3~5 分钟）…"
    & "$Venv\svc.exe" pre-hubert -fm crepe -c configs\config.json 2>&1 | Out-File -Append $Log
}

# 4) 训练：先 120 epoch（约 1~1.5 小时），跑完看效果再决定加不加到 200
Log "开始训练 120 epoch…"
& "$Venv\svc.exe" train -c configs\config.json -m feiyu -t 120 2>&1 | Out-File -Append $Log
Log "===== 训练结束，模型在 logs\44k\feiyu\G_*.pth ====="
