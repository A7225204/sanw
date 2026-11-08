"""框选学习：全屏半透明遮罩，拖鼠标拉一个框，把框里的画面学成一个视觉实体模板。

用法：
    python tools/learn_entity.py "发送按钮"

不传名字时用时间戳命名。学完写进 memory/vision_packs/packs.json，
之后说「看屏」就能 0 token 认出它。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    name = sys.argv[1].strip() if len(sys.argv) > 1 else f"实体_{time.strftime('%H%M%S')}"

    # 先等 3 秒再弹全屏窗口：让机器人的 QQ 回复先发出去。
    # 全屏置顶窗口会抢焦点，NapCat 发消息时 NTEvent 会超时（retcode=1200）→ 掉线。
    time.sleep(3.0)

    import tkinter as tk
    from PIL import ImageGrab, ImageTk

    # 先把整个屏幕抓下来当底图
    screen = ImageGrab.grab()
    sw, sh = screen.size

    root = tk.Tk()
    root.title(f"框选要教她认的东西：{name}")
    root.attributes("-fullscreen", True)
    root.attributes("-alpha", 0.85)
    root.configure(bg="black")

    photo = ImageTk.PhotoImage(screen)
    canvas = tk.Canvas(root, width=sw, height=sh, highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    canvas.create_image(0, 0, image=photo, anchor="nw")

    hint = canvas.create_text(
        sw // 2, 40,
        text=f"拖一个框圈住「{name}」　·　松开即保存　·　按 Esc 取消",
        fill="#ffe066", font=("Microsoft YaHei", 20, "bold"),
    )

    state = {"x0": 0, "y0": 0, "rect": None}

    def on_down(e):
        state["x0"], state["y0"] = e.x, e.y
        if state["rect"]:
            canvas.delete(state["rect"])
        state["rect"] = canvas.create_rectangle(
            e.x, e.y, e.x, e.y, outline="#ff4444", width=3, dash=(4, 3))

    def on_move(e):
        if state["rect"]:
            canvas.coords(state["rect"], state["x0"], state["y0"], e.x, e.y)

    def on_up(e):
        x1, y1 = min(state["x0"], e.x), min(state["y0"], e.y)
        x2, y2 = max(state["x0"], e.x), max(state["y0"], e.y)
        if x2 - x1 < 8 or y2 - y1 < 8:
            return  # 太小，当误触
        try:
            from tools.vision_entities import learn
            res = learn(name, box=(x1, y1, x2, y2), image=screen)
            # 写回执文件：机器人会轮询它，然后主动说「学到了什么」
            try:
                import json
                from pathlib import Path
                import time as _t
                rf = Path(__file__).resolve().parent.parent / "memory" / "vision_packs" / "last_learn.json"
                rf.write_text(json.dumps({
                    "name": name,
                    "size": res.get("size"),
                    "box": [x1, y1, x2, y2],
                    "at": _t.strftime("%Y-%m-%d %H:%M:%S"),
                    "ts": _t.time(),
                }, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
            canvas.itemconfigure(hint, text=f"已学会「{name}」 {res.get('size')}，窗口即将关闭")
            root.update()
            time.sleep(0.6)
        except Exception as exc:  # noqa: BLE001
            print("保存失败:", exc, file=sys.stderr)
        root.destroy()

    def on_esc(e):
        root.destroy()

    canvas.bind("<ButtonPress-1>", on_down)
    canvas.bind("<B1-Motion>", on_move)
    canvas.bind("<ButtonRelease-1>", on_up)
    root.bind("<Escape>", on_esc)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
