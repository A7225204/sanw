import pathlib

# 1) 机器人侧：兼容 BOM
p = pathlib.Path(r"D:\qq-ai-bot\plugins\game_watch.py")
s = p.read_text(encoding="utf-8")
if "utf-8-sig" not in s:
    s = s.replace('data = json.loads(_CMD_FILE.read_text(encoding="utf-8")) or {}',
                  'data = json.loads(_CMD_FILE.read_text(encoding="utf-8-sig")) or {}')
    p.write_text(s, encoding="utf-8")
    print("game_watch: 指令文件兼容 BOM")
else:
    print("game_watch: 已兼容")

# 2) 启动器：写指令改成无 BOM，避免任何解析问题
lp = pathlib.Path(r"C:\Users\Administrator\Desktop\蓝色大肥鱼\start_all.ps1")
ls = lp.read_bytes().decode("utf-8-sig")
orig = ls
ls = ls.replace("'{\"cmd\":\"start\"}' | Set-Content -LiteralPath $GAME_CMD -Encoding UTF8",
                "[System.IO.File]::WriteAllText($GAME_CMD, '{\"cmd\":\"start\"}', (New-Object System.Text.UTF8Encoding($false)))")
ls = ls.replace("'{\"cmd\":\"stop\"}' | Set-Content -LiteralPath $GAME_CMD -Encoding UTF8",
                "[System.IO.File]::WriteAllText($GAME_CMD, '{\"cmd\":\"stop\"}', (New-Object System.Text.UTF8Encoding($false)))")
ls = ls.replace("$g = Get-Content -LiteralPath $GAME_STATE -Raw -ErrorAction Stop | ConvertFrom-Json",
                "$g = [System.IO.File]::ReadAllText($GAME_STATE) | ConvertFrom-Json")
if ls != orig:
    lp.write_bytes(("\ufeff" + ls).encode("utf-8"))
    print("start_all.ps1: 已改为无 BOM 写入 + 读取")
else:
    print("start_all.ps1: 未变化")
