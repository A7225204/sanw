"""把电脑上的 表情包/<情绪> 同步到手机相册同名文件夹。

用法：
  python tools\\sync_stickers_phone.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "表情包"
ADB = (r"C:\Users\Administrator\AppData\Local\Microsoft\WinGet\Packages"
       r"\Google.PlatformTools_Microsoft.Winget.Source_8wekyb3d8bbwe\platform-tools\adb.exe")
SERIAL = "192.168.1.4:37217"
DEST = "/sdcard/Pictures/大肥鱼表情包"


def run(*args: str, timeout: int = 120) -> str:
    p = subprocess.run([ADB, "-s", SERIAL, *args], capture_output=True,
                       text=True, encoding="utf-8", errors="ignore", timeout=timeout)
    return (p.stdout or "") + (p.stderr or "")


def main() -> None:
    if not SRC.is_dir():
        print("没有表情包目录")
        return
    total = 0
    for folder in sorted(p for p in SRC.iterdir() if p.is_dir()):
        files = [f for f in folder.iterdir() if f.suffix.lower() in (".png", ".jpg", ".jpeg")]
        if not files:
            continue
        run("shell", "mkdir", "-p", f"{DEST}/{folder.name}")
        for f in files:
            out = run("push", str(f), f"{DEST}/{folder.name}/{f.name}", timeout=60)
            if "1 file pushed" in out or "skipped" in out:
                total += 1
        print(f"{folder.name}: {len(files)} 张")
    # 触发一次整卷扫描，让相册识别新目录
    run("shell", "am", "broadcast", "-a", "android.intent.action.MEDIA_MOUNTED",
        "-d", "file:///sdcard")
    print(f"完成，共推送 {total} 张到 {DEST}")


if __name__ == "__main__":
    main()
