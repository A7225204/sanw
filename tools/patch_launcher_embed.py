import pathlib

p = pathlib.Path(r"C:\Users\Administrator\Desktop\蓝色大肥鱼\start_all.ps1")
raw = p.read_bytes()
s = raw.decode("utf-8-sig")
orig = s
changed = []

# ---------- 1) 变量：向量服务专用 pythonw + 端口 ----------
if "$EMBED_PORT" not in s:
    s = s.replace(
        "$COMFY_PORT = if ($cfg.comfyui.port) { [int]$cfg.comfyui.port } else { 8188 }",
        "$COMFY_PORT = if ($cfg.comfyui.port) { [int]$cfg.comfyui.port } else { 8188 }\n"
        "# 本地记忆向量服务（bge-m3）：给表情包语义匹配 / 记忆语义召回用\n"
        "$EMBED_PORT = 7801\n"
        "$EMBED_PY   = 'D:\\f5tts\\.venv\\Scripts\\pythonw.exe'",
        1,
    )
    changed.append("变量")

# ---------- 2) 服务清单 ----------
if 'Key="embed"' not in s:
    s = s.replace(
        '    @{ Name="网页仪表盘";          Key="dashboard"; Port=$DASH_PORT }',
        '    @{ Name="本地记忆向量(bge-m3)"; Key="embed"; Port=$EMBED_PORT },\n'
        '    @{ Name="网页仪表盘";          Key="dashboard"; Port=$DASH_PORT }',
        1,
    )
    changed.append("服务清单")

# ---------- 3) 判活 ----------
if '"embed" { $svc.Check' not in s:
    s = s.replace(
        '        "dashboard" { $svc.Check = { (Get-DashboardState) -eq \'running\' } }',
        '        "embed" { $svc.Check = { Test-PortListen $EMBED_PORT } }\n'
        '        "dashboard" { $svc.Check = { (Get-DashboardState) -eq \'running\' } }',
        1,
    )
    changed.append("判活")

# ---------- 4) 启动 ----------
if '"embed" { if (Test-PortListen' not in s:
    s = s.replace(
        '        "dashboard" { Start-Dashboard',
        '        "embed" { if (Test-PortListen $EMBED_PORT) { return "已在运行($EMBED_PORT)" }\n'
        '                   if (-not (Test-Path -LiteralPath $EMBED_PY)) { return "未找到向量服务的 Python（D:\\f5tts\\.venv）" }\n'
        '                   $el = Join-Path $QQBOT_DIR "logs\\embed_server.out.log"\n'
        '                   $ee = Join-Path $QQBOT_DIR "logs\\embed_server.err.log"\n'
        '                   Start-Process -FilePath $EMBED_PY -ArgumentList "tools\\embed_server.py" -WorkingDirectory $QQBOT_DIR -WindowStyle Hidden -RedirectStandardOutput $el -RedirectStandardError $ee\n'
        '                   for ($i=0; $i -lt 90; $i++) { if (Test-PortListen $EMBED_PORT) { return "已就绪($EMBED_PORT，首次加载模型约 20 秒)" }; Start-Sleep 1 }\n'
        '                   return "启动超时($EMBED_PORT 未监听，看 logs\\embed_server.err.log)" }\n'
        '        "dashboard" { Start-Dashboard',
        1,
    )
    changed.append("启动")

# ---------- 5) 停止 ----------
if '"embed"  { $p = Get-NetTCPConnection' not in s and '"embed" {' not in s.split("function Stop-One")[-1][:600]:
    s = s.replace(
        '        "dashboard" { Stop-DashboardProcess; return "已停止" }',
        '        "embed"  { $p = Get-NetTCPConnection -LocalPort $EMBED_PORT -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1\n'
        '                   if ($p) { Stop-Process -Id $p.OwningProcess -Force -ErrorAction SilentlyContinue }\n'
        '                   return "已停止" }\n'
        '        "dashboard" { Stop-DashboardProcess; return "已停止" }',
        1,
    )
    changed.append("停止")

if s != orig:
    p.write_bytes(("\ufeff" + s).encode("utf-8"))  # 必须带 BOM，否则 PowerShell 5.1 中文乱码
print("start_all.ps1 改动:", changed)
print("文件大小:", p.stat().st_size, "字节（BOM 已保留）")
