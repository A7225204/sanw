"""桌宠头顶气泡：镜像 QQ 机器人的最新回复 + 心情，不调用任何大模型。

读取 D:\\qq-ai-bot\\memory\\pet_live.json，找到桌宠窗口位置，在头顶显示 10 秒。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import textwrap
import time
import tkinter as tk
from pathlib import Path

ROOT = Path(r"D:\qq-ai-bot")
LIVE = ROOT / "memory" / "pet_live.json"
TRANSPARENT = "#010203"

MOOD_ICON = {
    "开心": "(*^▽^*)", "得意": "(￣▽￣)", "嘴臭": "(¬_¬)",
    "暴躁": "(╬▔皿▔)", "委屈": "(；´д｀)", "生气": "(╬▔皿▔)",
    "emo": "(´-ω-`)", "normal": "(・_・)",
}


def find_pet_rect():
    """优先读桌宠运行时坐标文件；窗口本身是整屏透明层，不能直接用窗口矩形。"""
    data_dir = Path.home() / "AppData" / "Roaming" / "dsh-pet-standalone-webm-chat"
    try:
        import psutil
        alive = {p.pid for p in psutil.process_iter(["pid"])}
    except Exception:
        alive = set()
    files = sorted(data_dir.glob("pet-runtime-v2-*-slot-0.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if data.get("pid") and alive and data["pid"] not in alive:
                continue
            x, y, w, h = int(data["x"]), int(data["y"]), int(data["w"]), int(data["h"])
            if w > 20 and h > 20:
                return x, y, w, h
        except Exception:
            continue

    user32 = ctypes.windll.user32
    pid_to_name = {}
    try:
        import psutil
        for p in psutil.process_iter(["pid", "name"]):
            pid_to_name[p.info["pid"]] = (p.info["name"] or "").lower()
    except Exception:
        pass

    result = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        name = pid_to_name.get(pid.value, "")
        if "dsh-pet" not in name:
            return True
        rect = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w, h = rect.right - rect.left, rect.bottom - rect.top
        if w > 900 or h > 900 or w < 40 or h < 40:
            return True
        result.append((rect.left, rect.top, w, h))
        return True

    user32.EnumWindows(cb, 0)
    return result[0] if result else None


def main() -> None:
    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    try:
        root.attributes("-transparentcolor", TRANSPARENT)
    except Exception:
        pass
    root.configure(bg=TRANSPARENT)

    label = tk.Label(
        root, text="", bg="#10182f", fg="#e9efff", font=("Microsoft YaHei", 11),
        justify="left", padx=14, pady=10, wraplength=320, bd=1, relief="solid",
    )
    label.pack()

    state = {"seq": 0.0, "hide_at": 0.0, "visible": False, "size": (0, 0)}

    def hide():
        state["visible"] = False
        root.withdraw()

    def place():
        """按桌宠当前坐标，把气泡放到头顶；每 0.5 秒跟着它跑。"""
        if not state["visible"]:
            return
        w, h = state["size"]
        root.update_idletasks()
        w = label.winfo_width() or w
        h = label.winfo_height() or h
        state["size"] = (w, h)
        rect = find_pet_rect()
        if rect:
            x, y, pw, ph = rect
            pos_x = x + pw // 2 - w // 2
            pos_y = y - h - 12
            if pos_y < 0:
                pos_y = y + ph + 8
            max_x = root.winfo_screenwidth() - w - 8
            pos_x = max(8, min(pos_x, max_x))
        else:
            pos_x = root.winfo_screenwidth() - w - 40
            pos_y = root.winfo_screenheight() - h - 120
        root.geometry(f"+{pos_x}+{pos_y}")

    def show(payload):
        text = str(payload.get("reply") or "").strip()
        if not text:
            return
        mood = str(payload.get("mood") or "")
        icon = MOOD_ICON.get(mood, "(・_・)")
        wrapped = "\n".join(textwrap.wrap(text, width=24))[:240]
        label.configure(text=f"{icon}  {wrapped}\n— 心情：{mood}")
        state["visible"] = True
        root.deiconify()
        root.update_idletasks()
        state["size"] = (label.winfo_width(), label.winfo_height())
        place()
        state["hide_at"] = time.time() + 10

    def poll():
        try:
            if LIVE.exists() and not state["visible"]:
                data = json.loads(LIVE.read_text(encoding="utf-8"))
                seq = float(data.get("seq") or 0)
                if seq > state["seq"]:
                    state["seq"] = seq
                    show(data)
            if state["visible"]:
                place()
            if state["hide_at"] and time.time() > state["hide_at"]:
                hide()
                state["hide_at"] = 0
        except Exception:
            pass
        root.after(500, poll)

    root.withdraw()
    poll()
    root.mainloop()


if __name__ == "__main__":
    main()
